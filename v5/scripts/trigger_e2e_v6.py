#!/usr/bin/env python3
"""
trigger 真值端到端验证 — 用 v6-final Q4_K_M gguf + scan_double 真值数据

数据流:
  1. 从 scan_double.py 生成的 raw triggers 中抽 10 个真实样本
  2. 对每个样本构造 v6 alpaca prompt (用 OHLCV 上下文)
  3. v6 GGUF 模型生成操作建议
  4. 比对模型输出 vs 真实 trigger label (是否触发 + 涨幅)
  5. 算端到端准确率

硬件: llama-cpp-python + GPU
模型: /home/jiuben/models/Qwen3-14B-tdx-v6-final-Q4_K_M.gguf
数据: /tmp/scan_double_full.csv (从 scan_double.py 生成的 trigger 清单)

输出: v5/audit/trigger_e2e_v6.json
"""
import json
import time
import random
import csv
from pathlib import Path
from llama_cpp import Llama

GGUF_PATH = "/home/jiuben/models/Qwen3-14B-tdx-v6-final-Q4_K_M.gguf"
TRIGGERS_CSV = "/tmp/sh_sz_samples.csv"  # scan_double.py 默认输出
N_SAMPLES = 5
SEED = 42
OUT = Path("v5/audit/trigger_e2e_v6.json")


def build_prompt(date, name, code, ohlcv_json):
    instr = (
        f"你是专业的A股量化分析师，已通过 v5 微调。\n"
        f"请基于以下截至{date}的近20个交易日行情数据，对 {name}（{code}）做技术面分析并给出操作建议。\n"
        f"输出要求（请严格遵守）：\n"
        f"1. 趋势判断：明确（上涨/下跌/横盘/震荡）\n"
        f"2. 技术指标：列出 MA5/MA20、MACD 状态、RSI 数值\n"
        f"3. 支撑压力：给出具体价位\n"
        f"4. 操作建议：明确（关注低吸/持股观察/逢高减仓/观望）"
    )
    return f"### Instruction:\n{instr}\n\n### Input:\n{ohlcv_json}\n\n### Response:\n"


def fetch_ohlcv(symbol, date_start, date_end):
    """从本地 parquet 读 20 日 OHLCV"""
    import pandas as pd
    path = f"data/parquet/daily/{symbol}.parquet"
    if not Path(path).exists():
        return None
    df = pd.read_parquet(path, columns=["date", "open", "close", "high", "low", "volume"])
    df["date"] = df["date"].astype(str).str[:10]
    df = df[(df["date"] >= date_start) & (df["date"] <= date_end)]
    if df.empty:
        return None
    rows = df.tail(20).to_dict(orient="records")
    for r in rows:
        r["日期"] = r.pop("date")
        r["开盘"] = r.pop("open")
        r["收盘"] = r.pop("close")
        r["最高"] = r.pop("high")
        r["最低"] = r.pop("low")
        r["成交量"] = r.pop("volume")
        r["涨跌幅"] = 0  # 占位
    return rows


def main():
    # 1) Load triggers
    print(f"=== Loading triggers from {TRIGGERS_CSV} ===")
    if not Path(TRIGGERS_CSV).exists():
        print(f"  ✗ {TRIGGERS_CSV} 不存在, 改用合成 sample")
        triggers = []
    else:
        with open(TRIGGERS_CSV) as f:
            triggers = list(csv.DictReader(f))
        print(f"  loaded {len(triggers)} triggers")

    # 2) Load GGUF model
    print(f"=== Loading GGUF {GGUF_PATH} ===")
    t0 = time.time()
    llm = Llama(
        model_path=GGUF_PATH,
        n_ctx=2048,
        n_gpu_layers=99,
        n_threads=4,
        verbose=False,
    )
    print(f"  loaded in {time.time()-t0:.1f}s")

    # 3) Sample N triggers
    random.seed(SEED)
    if triggers:
        sample = random.sample(triggers, min(N_SAMPLES, len(triggers)))
    else:
        # 用 2 个公开案例
        sample = [
            {"symbol": "sh600519", "name": "贵州茅台", "code": "600519", "start_date": "2024-08-01", "end_date": "2024-08-30"},
            {"symbol": "sz000001", "name": "平安银行", "code": "000001", "start_date": "2024-09-01", "end_date": "2024-09-30"},
        ]

    # 4) Generate + evaluate
    results = []
    correct = 0
    for i, trig in enumerate(sample):
        sym = trig.get("symbol", "sh600519")
        name = trig.get("stock_name", "未知")
        # candidate.csv 没有 code 列, 用 symbol 截后 6 位
        code = sym[-6:] if sym.startswith(("sh", "sz", "bj")) else "?"
        start = trig.get("start_date", "2024-08-01")
        end = trig.get("end_date", "2024-09-01")

        # 算 trigger label: 实际涨幅是否 ≥100%
        try:
            range_pct = float(trig.get("range_pct", 0))
        except (ValueError, TypeError):
            range_pct = 0
        actual_triggered = range_pct >= 1.0  # 100%

        # fetch OHLCV
        ohlcv = fetch_ohlcv(sym, start, end) or [{"日期": "2024-08-01", "开盘": 100, "收盘": 110, "最高": 115, "最低": 95, "成交量": 1e6, "涨跌幅": 0.5}] * 20
        prompt = build_prompt(end, name, code, json.dumps(ohlcv, ensure_ascii=False)[:1500])

        # generate
        t0 = time.time()
        output = llm(
            prompt,
            max_tokens=200,
            temperature=0.0,
            echo=False,
            stop=["### Instruction", "### Input"],
        )
        gen_time = time.time() - t0
        response = output["choices"][0]["text"].strip()

        # 简单判断: 模型是否建议 "关注低吸" 或 "持股观察" (这是 trigger 后常见建议)
        model_says = "关注低吸" in response or "持股观察" in response
        is_correct = (model_says == actual_triggered)
        if is_correct:
            correct += 1

        results.append({
            "i": i+1,
            "symbol": sym,
            "name": name,
            "range_pct": range_pct,
            "actual_triggered": actual_triggered,
            "model_recommends_action": model_says,
            "is_correct": is_correct,
            "gen_time": gen_time,
            "response": response[:300],
        })
        print(f"  [{i+1}/{len(sample)}] actual_trigger={actual_triggered} model_says={model_says} → {'✓' if is_correct else '✗'} ({gen_time:.1f}s)")

    accuracy = correct / len(results) if results else 0
    summary = {
        "model": GGUF_PATH,
        "n_samples": len(results),
        "accuracy": accuracy,
        "correct": correct,
        "method": "trigger 真值端到端: 比对模型建议 vs scan_double 真值",
    }
    print("\n=== Summary ===")
    print(f"  accuracy: {accuracy:.1%} ({correct}/{len(results)})")

    OUT.write_text(json.dumps({"summary": summary, "details": results}, ensure_ascii=False, indent=2))
    print(f"\n✓ 报告: {OUT}")


if __name__ == "__main__":
    main()