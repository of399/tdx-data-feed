"""v4 历史回溯：核心30只 × 25年 滚动分析
- 默认：2001-2026 每年末做1次技术面分析（25 个时间点/只）
- 可调：--interval {yearly,quarterly,monthly}
- 输出：vault 追加 ## v4历史回溯 (2001-2026) 章节 + JSONL
"""

import argparse
import json
import re
import time
import urllib.request
from datetime import datetime
from pathlib import Path

import pandas as pd

# ===== 路径 =====
VAULT_DIR = Path("/home/jiuben/StockVault/01-标的")
PARQUET_DIR = Path("/home/jiuben/tdx-data-feed/data/parquet/daily")
REPORT_DIR = Path("/home/jiuben/tdx-data-feed/v5/reports")
OLLAMA_URL = "http://127.0.0.1:11434"
V4_MODEL = "qwen3-14b-tdx-fast:14b"

REPORT_DIR.mkdir(parents=True, exist_ok=True)

# 核心 30 只 A 股（沪深300 + 创业板龙头 + 各行业代表）
CORE_STOCKS = [
    ("600519", "贵州茅台"),
    ("000858", "五粮液"),
    ("000333", "美的集团"),
    ("000651", "格力电器"),
    ("600036", "招商银行"),
    ("601398", "工商银行"),
    ("601318", "中国平安"),
    ("002594", "比亚迪"),
    ("300750", "宁德时代"),
    ("601012", "隆基绿能"),
    ("600276", "恒瑞医药"),
    ("000538", "云南白药"),
    ("600887", "伊利股份"),
    ("601888", "中国中免"),
    ("600028", "中国石化"),
    ("601857", "中国石油"),
    ("600585", "海螺水泥"),
    ("000002", "万科A"),
    ("600048", "保利发展"),
    ("300059", "东方财富"),
    ("300124", "汇川技术"),
    ("002415", "海康威视"),
    ("600900", "长江电力"),
    ("601138", "工业富联"),
    ("002475", "立讯精密"),
    ("688981", "中芯国际"),
    ("600009", "上海机场"),
    ("300015", "爱尔眼科"),
    ("600030", "中信证券"),
    ("601628", "中国人寿"),
]


def parse_date(d) -> str:
    if isinstance(d, pd.Timestamp):
        return d.strftime("%Y-%m-%d")
    if isinstance(d, (int, float)):
        s = str(int(d))
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}" if len(s) == 8 else s
    return str(d)[:10]


def find_vault_note(code: str) -> Path | None:
    for f in VAULT_DIR.iterdir():
        if not f.name.endswith(".md") or "MOC" in f.name:
            continue
        if f.name.startswith(f"{code}-") or f.name.startswith(f"{code} "):
            return f
    return None


def find_parquet(code: str) -> Path | None:
    for prefix in ("sh", "sz", "bj"):
        p = PARQUET_DIR / f"{prefix}{code}.parquet"
        if p.exists():
            return p
    return None


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
    return re.sub(r"<think>.*?</think>\s*", "", text, flags=re.DOTALL).strip()


def get_history_points(
    df: pd.DataFrame, start_year: int, end_year: int, interval: str
) -> list[str]:
    """返回历史时点列表（date int64）
    yearly:   每年12月最后一个交易日
    quarterly: 每季度末（3/6/9/12月最后一日）
    monthly:  每月最后一日
    """
    df = df.copy()
    df["date_str"] = df["date"].astype(str).str.zfill(8)
    df["year"] = df["date_str"].str[:4].astype(int)
    df["month"] = df["date_str"].str[4:6].astype(int)
    df = df[(df["year"] >= start_year) & (df["year"] <= end_year)]

    points = []
    if interval == "yearly":
        for y in range(start_year, end_year + 1):
            ydf = df[df["year"] == y]
            if len(ydf) > 0:
                points.append(int(ydf.iloc[-1]["date"]))
    elif interval == "quarterly":
        for y in range(start_year, end_year + 1):
            for m in [3, 6, 9, 12]:
                ydf = df[(df["year"] == y) & (df["month"] <= m)]
                if len(ydf) > 0:
                    points.append(int(ydf.iloc[-1]["date"]))
    elif interval == "monthly":
        for y in range(start_year, end_year + 1):
            for m in range(1, 13):
                ydf = df[(df["year"] == y) & (df["month"] == m)]
                if len(ydf) > 0:
                    points.append(int(ydf.iloc[-1]["date"]))
    return points


def build_prompt_for_history(
    code: str, name: str, df_tail: pd.DataFrame, as_of: str
) -> tuple[str, str]:
    """历史回溯 prompt：强调 '截至 {as_of} 当时看'"""
    instruction = (
        f"你是专业的A股量化分析师。请站在历史视角，基于截至 {as_of} 当时可见的近{len(df_tail)}个交易日行情数据，"
        f"对股票 {name}（{code}）做技术面分析。"
        f"请直接输出分析结论（不要思考过程），包含：当时趋势 / 均线 / MACD / RSI / 支撑压力 / 当时建议。"
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


def analyze_history(code: str, name: str, as_of_int: int, window: int) -> dict:
    """分析 1 个历史时点"""
    parquet = find_parquet(code)
    if not parquet:
        return {"code": code, "as_of": parse_date(str(as_of_int)), "status": "no_data"}

    df = pd.read_parquet(parquet, columns=["date", "open", "high", "low", "close", "volume"])
    df = df.sort_values("date").reset_index(drop=True)
    # 截到 as_of
    df = df[df["date"] <= as_of_int].reset_index(drop=True)
    if len(df) < window:
        return {
            "code": code,
            "as_of": parse_date(str(as_of_int)),
            "status": "insufficient",
            "rows": len(df),
        }

    df_tail = df.tail(window).reset_index(drop=True)
    as_of_str = parse_date(str(as_of_int))
    ins, inp = build_prompt_for_history(code, name, df_tail, as_of_str)

    t0 = time.time()
    try:
        response, _, tokens = call_v4(ins, inp)
        elapsed = time.time() - t0
        clean = strip_think(response)
    except Exception as e:
        return {"code": code, "as_of": as_of_str, "status": "error", "error": str(e)[:80]}

    # 写 JSONL
    jsonl = REPORT_DIR / f"v4-history-{datetime.now().strftime('%Y%m%d-%H%M%S')}.jsonl"
    rec = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "code": code,
        "name": name,
        "as_of": as_of_str,
        "window": window,
        "model": V4_MODEL,
        "elapsed_s": round(elapsed, 3),
        "eval_tokens": tokens,
        "input_size": len(inp),
        "output": clean,
    }
    with jsonl.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    return {
        "code": code,
        "as_of": as_of_str,
        "status": "ok",
        "elapsed": round(elapsed, 1),
        "tokens": tokens,
        "output": clean,
        "vault_note": find_vault_note(code),
    }


def update_vault_history(code: str, name: str, results: list[dict], start_year: int, end_year: int):
    """追加 ## v4历史回溯 (Y1-Y2) 章节到 vault"""
    note = find_vault_note(code)
    if not note:
        return None

    rows = []
    for r in results:
        if r["status"] != "ok":
            continue
        rows.append(f"| {r['as_of']} | {r['elapsed']}s | {r['output'][:150]}... |")

    section = (
        f"\n## v4历史回溯 ({start_year}-{end_year})\n\n"
        f"> 模型: `{V4_MODEL}` · 时间点: {len(rows)} · 生成: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        f"| 时点 | 耗时 | 当时技术面分析 |\n"
        f"|------|------|----------------|\n" + "\n".join(rows) + "\n\n### 相关链接\n\n"
        "- [[朝堂制量化投研体系]]\n"
        "- [[v4模型]]\n"
        "- [[00-收件箱]]\n"
    )
    text = note.read_text(encoding="utf-8")
    text = re.sub(r"\n## v4历史回溯 \(\d{4}-\d{4}\)\n.*?(?=\n## |\Z)", "\n", text, flags=re.DOTALL)
    note.write_text(text.rstrip() + section + "\n", encoding="utf-8")
    return note


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--stocks", default="core30", help="core30 / core10 / all / 或 code 列表逗号分隔"
    )
    parser.add_argument("--start-year", type=int, default=2001)
    parser.add_argument("--end-year", type=int, default=2026)
    parser.add_argument("--interval", default="yearly", choices=["yearly", "quarterly", "monthly"])
    parser.add_argument("--window", type=int, default=20)
    args = parser.parse_args()

    # 选股
    if args.stocks == "core30":
        stocks = CORE_STOCKS
    elif args.stocks == "core10":
        stocks = CORE_STOCKS[:10]
    elif args.stocks == "all":
        stocks = [
            (s.name[:6], s.stem)
            for s in VAULT_DIR.iterdir()
            if s.name.endswith(".md") and "MOC" not in s.name
        ]
        # 提取 code 假设 vault 命名 = "{code}-{name}.md"
        fixed = []
        for code, _ in stocks:
            for f in VAULT_DIR.iterdir():
                if f.name.startswith(f"{code}-") or f.name.startswith(f"{code} "):
                    name = f.stem[len(code) + 1 :] if f.stem.startswith(code) else f.stem
                    fixed.append((code, name))
                    break
        stocks = fixed
    else:
        # 逗号分隔 code
        codes = [c.strip() for c in args.stocks.split(",")]
        stocks = []
        for c in codes:
            note = find_vault_note(c)
            name = note.stem[len(c) + 1 :] if note and note.stem.startswith(c) else f"股票{c}"
            stocks.append((c, name))

    print("=== v4 Historical Backtest ===")
    print(f"  stocks: {len(stocks)}")
    print(f"  period: {args.start_year}-{args.end_year}")
    print(f"  interval: {args.interval}")
    print(
        f"  est: {len(stocks)} × ~{26 if args.interval == 'yearly' else 100 if args.interval == 'quarterly' else 300} timepoints × 8.5s"
    )

    t_start = time.time()
    total_ok = 0
    total_err = 0

    for i, (code, name) in enumerate(stocks, 1):
        parquet = find_parquet(code)
        if not parquet:
            print(f"  [{i:2d}/{len(stocks)}] ⚠ {code} {name}: no parquet")
            continue

        df = pd.read_parquet(parquet, columns=["date"])
        history_points = get_history_points(df, args.start_year, args.end_year, args.interval)
        print(f"  [{i:2d}/{len(stocks)}] {code} {name}: {len(history_points)} timepoints")

        stock_results = []
        for j, as_of in enumerate(history_points, 1):
            r = analyze_history(code, name, as_of, args.window)
            if r["status"] == "ok":
                total_ok += 1
                stock_results.append(r)
                if j % 5 == 0 or j == len(history_points):
                    sum(rr["elapsed"] for rr in stock_results if rr["status"] == "ok")
                    print(f"    [{j:3d}/{len(history_points)}] {r['as_of']} {r['elapsed']}s ✓")
            elif r["status"] == "error":
                total_err += 1
                print(
                    f"    [{j:3d}/{len(history_points)}] {r['as_of']} ✗ {r.get('error', '?')[:60]}"
                )
            else:
                print(f"    [{j:3d}/{len(history_points)}] {r['as_of']} {r['status']}")

        # 批量写 vault
        if stock_results:
            updated = update_vault_history(
                code, name, stock_results, args.start_year, args.end_year
            )
            if updated:
                print(f"    → vault: {updated.name} +{len(stock_results)} rows")

    total_min = (time.time() - t_start) / 60
    print("\n=== Done ===")
    print(f"  total time: {total_min:.1f} min")
    print(f"  ok: {total_ok}  error: {total_err}")


if __name__ == "__main__":
    main()
