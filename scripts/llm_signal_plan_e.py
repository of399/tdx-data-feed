#!/usr/bin/env python3
"""
#6 新LLM + 方案E 回测桥接
========================
用新训 Qwen3-14B(v4) 经 Ollama 生成「翻倍事件 universe」的方向信号，作为方案E 交易的闸门，
并对比 新模型(v4) vs 现役(v7r) 在方案E 上的实战效果。

设计：
  1. 方案E 翻倍事件 universe：load_double_up() -> get_first_double_date()（每股票首个 peak_date）。
  2. 对每笔事件 (code, peak_date)，重建 v4 训练分布下的 20 日窗口 prompt（截至 peak_date 的近20交易日），
     经 Ollama /api/chat（与 #5 评测一致的 chat template + <no_think> + greedy）取方向。
  3. 方向解析复用 eval_v4_vs_v7r.parse_output（涨=1 / 跌=-1 / 震荡=0）。
  4. 方案E 回测：baseline(无LLM过滤) / v4_涨过滤 / v7r_涨过滤 三种，对比胜率/均收益/盈亏比/样本数。

用法：
  # 生成单模型信号（Ollama 需运行）
  venv/bin/python scripts/llm_signal_plan_e.py gen --model qwen3-14b-tdx-v4 \
      --out data/plan_e/llm_sig_v4.csv [--limit 200] [--resume]

  # 对比 v4 vs v7r（需先生成两份信号 CSV）
  venv/bin/python scripts/llm_signal_plan_e.py compare \
      --v4-csv data/plan_e/llm_sig_v4.csv --v7r-csv data/plan_e/llm_sig_v7r.csv

  # 仅验证 prompt 重建（不调 Ollama，纯 CPU）
  venv/bin/python scripts/llm_signal_plan_e.py gen --model x --out /tmp/x.csv --dry --limit 3
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

import pandas as pd

ROOT = Path("/home/jiuben/tdx-data-feed")
PARQUET_DIR = ROOT / "data" / "parquet" / "daily"
OLLAMA_URL = "http://127.0.0.1:11434"
WINDOW = 20

# 复用 #5 的方向解析（保证口径一致）
sys.path.insert(0, str(ROOT / "training" / "eval"))
from eval_v4_vs_v7r import parse_output  # noqa: E402

# 复用方案E 翻倍 universe 加载
sys.path.insert(0, str(ROOT / "scripts"))
from plan_e_backtest import get_first_double_date, load_double_up  # noqa: E402
from _common import is_bj, norm_code  # 审计 P1-3-1 2026-10-17: 统一 4 处重复实现


# ---------- 日期 / 窗口重建（与 transform/v4_gen.py 完全一致）----------
def is_a_stock_code(code6: str) -> bool:
    """与 transform/v4_gen.py is_a_stock 一致：仅 A 股（sh60/68、sz00/30）。

    排除转债(11x/12x)、ETF(51x/56x/58x/15x/16x/18x)。
    """
    c = code6
    if c.startswith(("11", "12")):
        return False
    if c.startswith(("51", "56", "58", "15", "16", "18")):
        return False
    return bool(c.startswith(("60", "68", "00", "30", "001", "002", "003")))


def fmt_date(d) -> str:
    d = str(d).replace("-", "")
    return f"{d[:4]}-{d[4:6]}-{d[6:8]}"


def unit_mult(amount, volume, close) -> int:
    try:
        a, v, c = float(amount), float(volume), float(close)
    except (TypeError, ValueError):
        return 100
    if a <= 0 or v <= 0 or c <= 0:
        return 100
    ratio = a / (v * c)
    if 30 < ratio < 300:
        return 100
    if 0.3 < ratio < 3:
        return 1
    return 100


def _get_df(code_clean: str):
    for prefix in ("sh", "sz", "bj"):
        p = PARQUET_DIR / f"{prefix}{code_clean}.parquet"
        if p.exists():
            df = pd.read_parquet(p)
            df["date"] = df["date"].apply(fmt_date)
            return df.sort_values("date").reset_index(drop=True)
    return None


def build_window(code: str, as_of: str):
    """重建 v4 训练格式下的 20 日窗口 prompt 片段。

    返回 (instruction, input_json, last_date) 或 None（数据不足/找不到 as_of）。
    as_of: 窗口末日(=peak_date)，与 v4 训练分布一致（模型预测 as_of 之后 5 日方向）。
    """
    code_clean = str(code).zfill(6).lstrip("bj")
    df = _get_df(code_clean)
    if df is None or len(df) < WINDOW:
        return None
    # 找到 as_of 行（精确优先，否则向前最近 5 日内）
    exact = df[df["date"] == as_of]
    if not exact.empty:
        idx = exact.index[-1]
    else:
        cand = df[df["date"] <= as_of].tail(5)
        if cand.empty:
            return None
        idx = cand.index[-1]
    if idx < WINDOW - 1:
        return None
    win = df.iloc[idx - WINDOW + 1: idx + 1].reset_index(drop=True)
    rows, prev = [], None
    for _, b in win.iterrows():
        close = float(b["close"])
        pct = 0.0 if prev is None else round((close - prev) / prev * 100, 2)
        mult = unit_mult(b.get("amount"), b.get("volume"), close)
        vol = int(float(b["volume"]))
        if mult == 100:
            vol = int(float(b["volume"]) * 100)
        rows.append({
            "日期": fmt_date(b["date"]),
            "开盘": round(float(b["open"]), 2),
            "收盘": round(close, 2),
            "最高": round(float(b["high"]), 2),
            "最低": round(float(b["low"]), 2),
            "成交量": vol,
            "涨跌幅": pct,
        })
        prev = close
    last_date = fmt_date(win.iloc[-1]["date"])
    instruction = (
        f"你是专业的A股量化分析师。请基于以下截至{last_date}的近{WINDOW}个交易日行情数据，"
        f"对股票 {code_clean} 未来5个交易日的价格方向做出判断。\n"
        f"你必须且只能从【涨】【震荡】【跌】三个类别中严格选择唯一一项"
        f"（涨=后市大概率上行；震荡=区间盘整、方向不明；跌=后市大概率下行），禁止给出模糊或复合结论。\n"
        f"请在回答的最后一行，以『方向：涨』或『方向：震荡』或『方向：跌』的精确格式明确输出结论。"
    )
    return instruction, json.dumps(rows, ensure_ascii=False), last_date


# ---------- Ollama 调用（与 #5 一致：chat template + <no_think> + greedy）----------
def call_ollama_chat(model: str, instruction: str, inp: str,
                     num_predict: int = 128, timeout: int = 600) -> str:
    content = f"{instruction}\n{inp}\n<no_think>"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": content}],
        "stream": False,
        "options": {"temperature": 0.0, "num_predict": num_predict, "num_ctx": 2048},
    }
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/chat", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read())
    return d.get("message", {}).get("content", "")


# ---------- 生成信号 ----------
def gen_signals(model: str, out_csv: Path, limit: int | None, dry: bool, resume: bool):
    universe = get_first_double_date(load_double_up())
    print(f"翻倍事件 universe: {len(universe)} 只股", flush=True)

    done = set()
    if resume and out_csv.exists():
        ex = pd.read_csv(out_csv)
        done = set(ex["code"].astype(str))
        print(f"  resume: 已有 {len(done)} 条", flush=True)

    rows = []
    if out_csv.exists() and resume:
        rows = ex.to_dict("records")

    n_ok = 0
    n_skip_bj = 0
    t0 = time.time()
    for _i, (_, r) in enumerate(universe.iterrows()):
        code_raw = str(r["code"]).zfill(6)
        code = re.sub(r"^(sh|sz|bj)", "", code_raw)
        # 北交所(920XXX / 8xxxxx / bj前缀) 不在 v4 训练分布内 -> 跳过，避免 OOD 方向
        if code.startswith("920") or code[0] == "8" or code_raw.startswith("bj"):
            n_skip_bj += 1
            continue
        # 仅 A 股（排除转债/ETF，与 v4 训练分布一致）
        if not is_a_stock_code(code):
            n_skip_bj += 1
            continue
        if code in done:
            continue
        as_of = fmt_date(r["signal_date"])
        built = build_window(code, as_of)
        if built is None:
            rows.append({"code": code, "signal_date": as_of, "direction": None,
                         "parsed_ok": 0, "response_head": "NO_WINDOW"})
            continue
        instruction, inp, _ = built
        if dry:
            rows.append({"code": code, "signal_date": as_of, "direction": None,
                         "parsed_ok": 1, "response_head": instruction[:30]})
            if len(rows) % 50 == 0:
                print(f"  [dry] {len(rows)} windows built", flush=True)
            if limit and len([x for x in rows if x.get('parsed_ok')]) >= limit:
                break
            continue
        try:
            resp = call_ollama_chat(model, instruction, inp)
        except Exception as e:
            print(f"  [ERR] {code} {as_of}: {e}", flush=True)
            rows.append({"code": code, "signal_date": as_of, "direction": None,
                         "parsed_ok": 0, "response_head": f"ERR:{e}"})
            continue
        parsed = parse_output(resp)
        head = re.sub(r"\s+", " ", resp)[:60]
        rows.append({"code": code, "signal_date": as_of, "direction": parsed["direction"],
                     "parsed_ok": int(parsed["parsed_ok"]),
                     "response_head": head})
        n_ok += 1
        if n_ok % 50 == 0:
            rate = n_ok / (time.time() - t0)
            print(f"  [{model}] {n_ok} 信号 ({rate:.2f}/s) 最新 {code} dir={parsed['direction']}", flush=True)
        if limit and n_ok >= limit:
            break

    out_csv.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(out_csv, index=False, encoding="utf-8-sig")
    print(f"[完成] 信号写出: {out_csv}  共 {len(rows)} 行（有效方向 {n_ok}，跳过北交所 {n_skip_bj}）", flush=True)


# ---------- 方案E 过滤对比 ----------
def _metrics(bt: pd.DataFrame) -> dict:
    if bt.empty:
        return {"n": 0, "win_rate": float("nan"), "avg_ret": float("nan"),
                "median_ret": float("nan"), "tp": 0, "sl": 0, "time": 0,
                "cum_ret": float("nan")}
    n = len(bt)
    wins = (bt["return_pct"] > 0).sum()
    avg = bt["return_pct"].mean()
    med = bt["return_pct"].median()
    cum = (1 + bt["return_pct"]).prod() - 1
    return {"n": n, "win_rate": wins / n, "avg_ret": avg, "median_ret": med,
            "tp": int((bt["exit_reason"] == "TP").sum()),
            "sl": int((bt["exit_reason"] == "SL").sum()),
            "time": int((bt["exit_reason"] == "TIME").sum()),
            "cum_ret": cum}


def compare(v4_csv: Path, v7r_csv: Path):
    from plan_e_backtest import backtest

    # 统一代码格式：去 sh/sz/bj 前缀 + 补零到 6 位。
    # 信号 CSV 存裸 6 位，而回测侧 bt_valid["code"] 带 sh/sz/bj 前缀，
    # 必须先归一化再做集合交集，否则两组恒不相交（旧 bug：三组 LLM 过滤全 0 笔）。
    v4 = pd.read_csv(v4_csv)
    v7r = pd.read_csv(v7r_csv)
    # 三分类方向集：涨(1) / 非跌(!=-1,即涨+震荡) / 决断(±1,排除震荡)
    # 仅统计已成功解析方向(notna)的行，避免把解析失败的 NaN 误计入「非跌」。
    def _dir_set(df, pred):
        m = df["direction"].notna() & pred
        return set(df[m]["code"].map(norm_code))

    v4_bull = _dir_set(v4, v4["direction"] == 1)
    v4_nobear = _dir_set(v4, v4["direction"] != -1)
    v4_decisive = _dir_set(v4, v4["direction"].isin([1, -1]))
    v7r_bull = _dir_set(v7r, v7r["direction"] == 1)
    v7r_nobear = _dir_set(v7r, v7r["direction"] != -1)
    v7r_decisive = _dir_set(v7r, v7r["direction"].isin([1, -1]))
    union_bull = v4_bull | v7r_bull
    union_nobear = v4_nobear | v7r_nobear
    union_decisive = v4_decisive | v7r_decisive

    bt_valid, _ = backtest(load_double_up())
    # 限定非北交所事件池（与 LLM 评分分布一致，保证可比）
    bt_valid = bt_valid[~bt_valid["code"].apply(is_bj)].reset_index(drop=True)
    bt_valid["code_n"] = bt_valid["code"].map(norm_code)  # 归一化列，供交集/筛选
    print(f"方案E 基线（无LLM过滤，非北交所）有效交易: {len(bt_valid)} 笔", flush=True)

    def filt(s):
        return set(s["code_n"])

    base_codes = filt(bt_valid)
    subsets = {
        "baseline(无过滤)": base_codes,
        "v4_涨过滤": base_codes & v4_bull,
        "v4_排除跌": base_codes & v4_nobear,
        "v4_排除震荡": base_codes & v4_decisive,
        "v7r_涨过滤": base_codes & v7r_bull,
        "v7r_排除跌": base_codes & v7r_nobear,
        "v7r_排除震荡": base_codes & v7r_decisive,
        "v4∪v7r_涨过滤": base_codes & union_bull,
        "v4∪v7r_排除跌": base_codes & union_nobear,
        "v4∪v7r_排除震荡": base_codes & union_decisive,
    }
    results = {}
    for name, codes in subsets.items():
        sub = bt_valid[bt_valid["code_n"].isin(codes)]
        results[name] = _metrics(sub)
        print(f"  {name}: {results[name]['n']} 笔", flush=True)

    # 对比表
    print("\n# 方案E 回测：新LLM(v4) vs 现役(v7r) 三分类方向闸门对比\n")
    hdr = "| 模式 | 笔数 | 胜率 | 均收益 | 中位收益 | 累计收益 | TP | SL | TIME |"
    sep = "|------|------|------|--------|----------|----------|----|----|------|"
    print(hdr)
    print(sep)
    for name, m in results.items():
        wr = f"{m['win_rate']*100:.2f}%" if m["n"] else "—"
        ar = f"{m['avg_ret']*100:.2f}%" if m["n"] else "—"
        mr = f"{m['median_ret']*100:.2f}%" if m["n"] else "—"
        cr = f"{m['cum_ret']*100:.2f}%" if m["n"] else "—"
        print(f"| {name} | {m['n']} | {wr} | {ar} | {mr} | {cr} | {m['tp']} | {m['sl']} | {m['time']} |")

    # 三分类分布（看模型是否真的区分三类，而非退化成全涨）
    def _dist(df, label):
        d = df["direction"].dropna()
        n = len(d)
        if not n:
            print(f"{label}: 无有效方向"); return
        up = int((d == 1).sum()); dn = int((d == -1).sum()); fl = int((d == 0).sum())
        print(f"{label}: 有效 {n} → 涨 {up}({up/n*100:.1f}%) / 震荡 {fl}({fl/n*100:.1f}%) / 跌 {dn}({dn/n*100:.1f}%)")
    print("")
    _dist(v4, "v4 方向分布")
    _dist(v7r, "v7r 方向分布")
    print(f"\nv4 覆盖: 看涨 {len(v4_bull)} / 非跌 {len(v4_nobear)} / 决断 {len(v4_decisive)} / 基线 {len(base_codes)}")
    print(f"v7r 覆盖: 看涨 {len(v7r_bull)} / 非跌 {len(v7r_nobear)} / 决断 {len(v7r_decisive)} / 基线 {len(base_codes)}")


def main():
    ap = argparse.ArgumentParser(description="#6 新LLM + 方案E 回测桥接")
    sub = ap.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("gen", help="生成单模型方向信号")
    g.add_argument("--model", required=True, help="Ollama 模型 tag（如 qwen3-14b-tdx-v4）")
    g.add_argument("--out", required=True, help="输出 CSV")
    g.add_argument("--limit", type=int, default=None, help="最大信号数（抽样用）")
    g.add_argument("--dry", action="store_true", help="仅重建 prompt 不调 Ollama")
    g.add_argument("--resume", action="store_true", help="跳过已有 code")

    c = sub.add_parser("compare", help="对比 v4 vs v7r 方案E 效果")
    c.add_argument("--v4-csv", required=True)
    c.add_argument("--v7r-csv", required=True)

    args = ap.parse_args()
    if args.cmd == "gen":
        gen_signals(args.model, Path(args.out), args.limit, args.dry, args.resume)
    elif args.cmd == "compare":
        compare(Path(args.v4_csv), Path(args.v7r_csv))


if __name__ == "__main__":
    main()
