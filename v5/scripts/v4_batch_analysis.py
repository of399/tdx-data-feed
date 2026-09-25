"""批量 v4 微调模型技术面分析。
输入: parquet 目录 + vault 目录
输出: vault 追加 ## v4技术面分析 段 + JSONL 报告

支持:
  --top N     按 amount 选 top N 大市值/活跃股
  --all       全 5566 只
  --limit N   限量（默认无上限）
  --concurrent N  并发数（默认 1，因 v4 单卡并发反而慢）
  --dry-run   只打印，不写文件
  --skip-existing  跳过 vault 中已有 v4 段落的
"""

import argparse
import json
import re
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import pandas as pd

# ===== 路径 =====
VAULT_DIR = Path("/home/jiuben/StockVault/01-标的")
PARQUET_DIR = Path("/home/jiuben/tdx-data-feed/data/parquet/daily")
REPORT_DIR = Path("/home/jiuben/tdx-data-feed/v5/reports")
OLLAMA_URL = "http://127.0.0.1:11434"
V4_MODEL = "qwen3-14b-tdx-fast:14b"
ACTIVE_MODEL = V4_MODEL  # 会被 --model 覆盖

REPORT_DIR.mkdir(parents=True, exist_ok=True)


def parse_date(d) -> str:
    """int64 (20260921) / Timestamp / str → 'YYYY-MM-DD'"""
    if isinstance(d, pd.Timestamp):
        return d.strftime("%Y-%m-%d")
    if isinstance(d, (int, float)):
        s = str(int(d))
        if len(s) == 8:
            return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
        return s
    return str(d)[:10]


def find_vault_note(code: str, name_hint: str) -> Path | None:
    """从 vault 找标的笔记：按 code 前缀匹配"""
    if not VAULT_DIR.exists():
        return None
    for f in VAULT_DIR.iterdir():
        if not f.name.endswith(".md") or "MOC" in f.name:
            continue
        # 匹配 {code}- 前缀
        if f.name.startswith(f"{code}-") or f.name.startswith(f"{code} "):
            return f
    return None


def get_top_stocks(top_n: int) -> list[tuple[str, str]]:
    """按 amount 最近日 Top N 大市值/活跃股
    返回 [(code, parquet_stem), ...]
    """
    pairs = []
    for f in PARQUET_DIR.glob("*.parquet"):
        stem = f.stem  # sh600519 / sz000001
        try:
            df = pd.read_parquet(f, columns=["amount"])
            if len(df) == 0:
                continue
            amount = float(df["amount"].iloc[-1])
            pairs.append((stem, amount))
        except Exception:
            continue
    pairs.sort(key=lambda x: x[1], reverse=True)
    return [(stem, amt) for stem, amt in pairs[:top_n]]


def get_all_stocks() -> list[str]:
    return sorted([f.stem for f in PARQUET_DIR.glob("*.parquet")])


def get_a_share_stocks() -> list[str]:
    """仅 A 股（沪深主板/创业板/科创板）：600/601/603/605/000/002/003/300/688"""
    a_prefixes = ("sh60", "sh688", "sz000", "sz002", "sz003", "sz300", "bj")
    return sorted([f.stem for f in PARQUET_DIR.glob("*.parquet") if f.stem.startswith(a_prefixes)])


def build_prompt(code: str, name: str, df_tail: pd.DataFrame) -> tuple[str, str]:
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
                "成交量": round(float(row["volume"]) * 100, 0),
                "涨跌幅": change_pct,
            }
        )
        prev_close = close
    return instruction, json.dumps(bars, ensure_ascii=False)


def call_v4(instruction: str, input_str: str) -> tuple[str, float, int]:
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


def strip_think(text: str) -> str:
    """移除 <think>...</think> 块"""
    return re.sub(r"<think>.*?</think>\s*", "", text, flags=re.DOTALL).strip()


def analyze_one(parquet_stem: str, window: int, dry: bool, skip_existing: bool) -> dict:
    """分析单只：parquet_stem = sh600519"""
    # 解析 code/name
    code = re.search(r"(\d{6})", parquet_stem).group(1)
    parquet = PARQUET_DIR / f"{parquet_stem}.parquet"

    # 找 vault 笔记
    vault_note = find_vault_note(code, "")
    if (
        skip_existing
        and vault_note
        and "## v4技术面分析" in vault_note.read_text(encoding="utf-8", errors="ignore")
    ):
        return {"code": code, "status": "skip_existing"}

    # 提取最近 N 日
    df = pd.read_parquet(parquet, columns=["date", "open", "high", "low", "close", "volume"])
    df = df.sort_values("date").reset_index(drop=True)
    df_tail = df.tail(window).reset_index(drop=True)
    last_date = parse_date(df_tail.iloc[-1]["date"])

    # 用 vault 文件名提取 name（如 "600519-贵州茅台" → "贵州茅台"）
    name = (
        vault_note.stem[len(code) + 1 :]
        if vault_note and vault_note.stem.startswith(code)
        else f"股票{code}"
    )

    instruction, input_str = build_prompt(code, name, df_tail)

    t0 = time.time()
    try:
        response, _eval_dur, eval_tokens = call_v4(instruction, input_str)
        elapsed = time.time() - t0
        clean_response = strip_think(response)
    except Exception as e:
        return {"code": code, "name": name, "status": "error", "error": str(e)[:100]}

    # 写 vault
    if not dry and vault_note:
        section = (
            f"\n## v4技术面分析 · {last_date}\n\n"
            f"> 模型: `{V4_MODEL}` · 耗时: {elapsed:.1f}s · 调用日期: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
            f"{clean_response}\n\n"
            f"### 上下文\n\n"
            f"- 标的: **{name} ({code})**\n"
            f"- 数据日期: {last_date}\n"
            f"- 样本数: {window} 个交易日\n\n"
            f"### 相关链接\n\n"
            f"- [[朝堂制量化投研体系]]\n"
            f"- [[v4模型]]\n"
            f"- [[00-收件箱]]\n"
        )
        text = vault_note.read_text(encoding="utf-8")
        text = re.sub(r"\n## v4技术面分析 · [\d-]+\n.*?(?=\n## |\Z)", "\n", text, flags=re.DOTALL)
        vault_note.write_text(text.rstrip() + section + "\n", encoding="utf-8")

    # 写 JSONL
    if not dry:
        jsonl = REPORT_DIR / f"v4-batch-{datetime.now().strftime('%Y%m%d-%H%M%S')}.jsonl"
        rec = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "code": code,
            "name": name,
            "last_date": last_date,
            "model": V4_MODEL,
            "elapsed_s": round(elapsed, 3),
            "eval_tokens": eval_tokens,
            "input_size": len(input_str),
            "output": clean_response,
        }
        with jsonl.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    return {
        "code": code,
        "name": name,
        "status": "ok",
        "elapsed": round(elapsed, 1),
        "tokens": eval_tokens,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--top", type=int, help="按 amount Top N（默认：全 5566）")
    parser.add_argument("--all", action="store_true", help="全 5566 只")
    parser.add_argument(
        "--a-share-only", action="store_true", help="仅 A 股（沪深主板/创业板/科创板，~4604 只）"
    )
    parser.add_argument("--limit", type=int, default=0, help="限量（默认无上限）")
    parser.add_argument("--concurrent", type=int, default=1, help="并发数（默认 1）")
    parser.add_argument("--window", type=int, default=20, help="回看窗口")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-existing", action="store_true", help="跳过已有 v4 段")
    parser.add_argument("--code", help="单只 code（覆盖 top/all）")
    parser.add_argument("--model", default=V4_MODEL, help=f"Ollama 模型名（默认 {V4_MODEL}）")
    args = parser.parse_args()

    # 选股
    if args.code:
        stems = [f"sh{args.code}", f"sz{args.code}"]
        stems = [s for s in stems if (PARQUET_DIR / f"{s}.parquet").exists()]
    elif args.top:
        stems = [s for s, _ in get_top_stocks(args.top)]
    elif args.a_share_only:
        stems = get_a_share_stocks()
    else:
        stems = get_all_stocks()

    if args.limit:
        stems = stems[: args.limit]

    print("=== v4 Batch Analysis ===")
    print(f"  total: {len(stems)} stocks")
    print(f"  concurrent: {args.concurrent}")
    print(f"  window: {args.window} days")
    print(f"  dry: {args.dry_run}")
    print(f"  skip_existing: {args.skip_existing}")
    est_min = len(stems) * 8.5 / max(args.concurrent, 1) / 60
    print(f"  est: ~{est_min:.0f} min")
    print()

    # 并发执行
    t_start = time.time()
    ok = skip = err = 0
    if args.concurrent == 1:
        for i, stem in enumerate(stems, 1):
            r = analyze_one(stem, args.window, args.dry_run, args.skip_existing)
            if r["status"] == "ok":
                ok += 1
                print(
                    f"  [{i:4d}/{len(stems)}] ✓ {r['code']} {r['name']:8} {r['elapsed']:5.1f}s ({r['tokens']} tok)"
                )
            elif r["status"] == "skip_existing":
                skip += 1
            else:
                err += 1
                print(f"  [{i:4d}/{len(stems)}] ✗ {r['code']}: {r.get('error', '?')[:60]}")
    else:
        with ThreadPoolExecutor(max_workers=args.concurrent) as ex:
            futures = {
                ex.submit(analyze_one, s, args.window, args.dry_run, args.skip_existing): s
                for s in stems
            }
            for i, f in enumerate(as_completed(futures), 1):
                r = f.result()
                if r["status"] == "ok":
                    ok += 1
                    print(
                        f"  [{i:4d}/{len(stems)}] ✓ {r['code']} {r['name']:8} {r['elapsed']:5.1f}s"
                    )
                elif r["status"] == "skip_existing":
                    skip += 1
                else:
                    err += 1
                    print(f"  [{i:4d}/{len(stems)}] ✗ {r['code']}: {r.get('error', '?')[:60]}")

    total = time.time() - t_start
    print("\n=== Done ===")
    print(f"  total time: {total / 60:.1f} min")
    print(f"  ok: {ok}  skip: {skip}  error: {err}")


if __name__ == "__main__":
    main()
