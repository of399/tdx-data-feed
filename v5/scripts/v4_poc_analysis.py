"""POC: v4 微调模型对单只股票做技术面分析。
输入: parquet OHLCV
输出: alpaca prompt → v4 Ollama → vault 追加 + JSONL
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

# ===== 路径常量 =====
VAULT_DIR = Path("/home/jiuben/StockVault")
PARQUET_DIR = Path("/home/jiuben/tdx-data-feed/data/parquet/daily")
REPORT_DIR = Path("/home/jiuben/tdx-data-feed/v5/reports")
OLLAMA_URL = "http://127.0.0.1:11434"
V4_MODEL = "qwen3-14b-tdx-fast:14b"

REPORT_DIR.mkdir(parents=True, exist_ok=True)


def code_to_files(code: str) -> Path | None:
    """600519 → sh600519.parquet"""
    for prefix in ("sh", "sz", "bj"):
        p = PARQUET_DIR / f"{prefix}{code}.parquet"
        if p.exists():
            return p
    return None


def parse_date(d) -> str:
    """统一解析 parquet date 列：int64 (20260921) / Timestamp / str → 'YYYY-MM-DD'"""
    if isinstance(d, pd.Timestamp):
        return d.strftime("%Y-%m-%d")
    if isinstance(d, (int, float)):
        s = str(int(d))
        if len(s) == 8:
            return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
        return s
    return str(d)[:10]


def build_alpaca_prompt(code: str, name: str, df_tail: pd.DataFrame) -> tuple[str, str]:
    """返回 (instruction, input_json_str)
    v4 训练格式：日期/开盘/收盘/最高/最低/成交量/涨跌幅
    """
    last_date_str = parse_date(df_tail.iloc[-1]["date"])

    instruction = (
        f"你是专业的A股量化分析师。请基于以下截至{last_date_str}的近{len(df_tail)}个交易日行情数据，"
        f"对股票 {name}（{code}）做技术面分析并给出操作建议。"
        f"请直接输出分析结论（不要思考过程），包含：趋势判断 / 均线状态 / MACD / RSI / 支撑压力位 / 操作建议。"
        f"格式：简短中文段落，200 字以内。"
    )

    bars = []
    prev_close = None
    for _, row in df_tail.iterrows():
        d_str = parse_date(row["date"])
        close = float(row["close"])
        change_pct = round((close - prev_close) / prev_close * 100, 2) if prev_close else 0.0
        bars.append(
            {
                "日期": d_str,
                "开盘": round(float(row["open"]), 2),
                "收盘": round(close, 2),
                "最高": round(float(row["high"]), 2),
                "最低": round(float(row["low"]), 2),
                "成交量": round(float(row["volume"]) * 100, 0),  # 手 → 股
                "涨跌幅": change_pct,
            }
        )
        prev_close = close

    input_str = json.dumps(bars, ensure_ascii=False)
    return instruction, input_str


def call_v4_ollama(instruction: str, input_str: str) -> tuple[str, float, int]:
    """调 Ollama v4 模型，返回 (response, eval_duration_s, eval_tokens)"""
    import urllib.request

    payload = {
        "model": V4_MODEL,
        "prompt": f"### Instruction:\n{instruction}\n\n### Input:\n{input_str}\n\n### Response:\n",
        "stream": False,
        "options": {
            "temperature": 0.3,
            "top_p": 0.95,
            "top_k": 20,
            "num_predict": 350,
            "num_ctx": 4096,
            "repeat_penalty": 1.1,
        },
    }
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/generate",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=300) as r:
        d = json.loads(r.read())
    return d.get("response", ""), d.get("eval_duration", 0) / 1e9, d.get("eval_count", 0)


def write_vault(
    code: str,
    name: str,
    last_date: str,
    response: str,
    instruction: str,
    input_str: str,
    elapsed_s: float,
):
    """追加 ## v4技术面分析 章节到 vault 标的笔记"""
    # 找标的名字（从 vault 现有文件）
    vault_md = VAULT_DIR / "01-标的" / f"{name}.md"
    if not vault_md.exists():
        # 退化：从 input 提取标的名
        vault_md = VAULT_DIR / "01-标的" / f"{code}.md"
    if not vault_md.exists():
        # 创建新笔记
        vault_md = VAULT_DIR / "01-标的" / f"{name}.md"
        vault_md.write_text(
            f"---\ntags: [标的, A股, {code}]\n创建日期: {datetime.now().strftime('%Y-%m-%d')}\n---\n\n"
            f"# {name} ({code})\n\n> 自动生成（v4 模型 POC）\n\n",
            encoding="utf-8",
        )

    section = (
        f"\n## v4技术面分析 · {last_date}\n\n"
        f"> 模型: `{V4_MODEL}` · 耗时: {elapsed_s:.1f}s · 调用日期: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        f"{response.strip()}\n\n"
        f"### 上下文\n\n"
        f"- 标的: **{name} ({code})**\n"
        f"- 数据日期: {last_date}\n"
        f"- 样本数: 20 个交易日\n\n"
        f"### 相关链接\n\n"
        f"- [[朝堂制量化投研体系]]\n"
        f"- [[v4模型]]\n"
        f"- [[00-收件箱]]\n"
    )

    text = vault_md.read_text(encoding="utf-8")
    # 移除旧 v4 段（幂等）
    import re

    text = re.sub(r"\n## v4技术面分析 · [\d-]+\n.*?(?=\n## |\Z)", "\n", text, flags=re.DOTALL)
    vault_md.write_text(text.rstrip() + section + "\n", encoding="utf-8")
    return vault_md


def write_jsonl(
    code: str,
    name: str,
    last_date: str,
    instruction: str,
    input_str: str,
    response: str,
    elapsed_s: float,
):
    """追加 JSONL 报告"""
    out = REPORT_DIR / f"v4-analysis-{datetime.now().strftime('%Y%m%d')}.jsonl"
    rec = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "code": code,
        "name": name,
        "last_date": last_date,
        "model": V4_MODEL,
        "elapsed_s": round(elapsed_s, 3),
        "instruction": instruction,
        "input_size": len(input_str),
        "output": response,
    }
    with out.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--code", default="600519", help="股票代码（默认 600519 贵州茅台）")
    parser.add_argument("--name", default="贵州茅台", help="股票名称（vault 文件名）")
    parser.add_argument("--window", type=int, default=20, help="回看窗口（默认 20 交易日）")
    args = parser.parse_args()

    parquet = code_to_files(args.code)
    if not parquet:
        sys.exit(f"❌ 找不到 {args.code} 的 parquet")
    print(f"📊 标的: {args.name} ({args.code})")
    print(f"📁 数据: {parquet.name}")

    df = pd.read_parquet(parquet, columns=["date", "open", "high", "low", "close", "volume"])
    # date 已是 YYYYMMDD 整数（不用 pd.to_datetime，否则变 1970）
    df = df.sort_values("date").reset_index(drop=True)
    df_tail = df.tail(args.window).reset_index(drop=True)
    last_date = parse_date(df_tail.iloc[-1]["date"])
    print(f"📅 数据范围: {parse_date(df_tail.iloc[0]['date'])} → {last_date} ({len(df_tail)} 天)")

    instruction, input_str = build_alpaca_prompt(args.code, args.name, df_tail)
    print(f"\n📝 Prompt 长度: {len(input_str)} chars")
    print(f"   instruction: {instruction[:80]}...")

    print(f"\n🚀 调用 v4 模型 ({V4_MODEL})...")
    import time

    t0 = time.time()
    response, eval_dur, eval_tokens = call_v4_ollama(instruction, input_str)
    elapsed = time.time() - t0
    print(
        f"⏱️  耗时: {elapsed:.1f}s · 生成 {eval_tokens} tokens ({eval_tokens / eval_dur:.1f} tok/s)"
    )

    print("\n" + "=" * 60)
    print("📊 v4 模型输出：")
    print("=" * 60)
    print(response.strip())
    print("=" * 60)

    # 写 vault
    vault_path = write_vault(
        args.code, args.name, last_date, response, instruction, input_str, elapsed
    )
    print(f"\n✅ vault 写入: {vault_path}")

    # 写 JSONL
    jsonl_path = write_jsonl(
        args.code, args.name, last_date, instruction, input_str, response, elapsed
    )
    print(f"✅ JSONL 写入: {jsonl_path}")


if __name__ == "__main__":
    main()
