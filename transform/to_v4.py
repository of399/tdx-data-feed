"""通用 Parquet → v4 训练样本（alpaca：instruction/input/output，与 v3 一致）。

用法: python transform/to_v4.py <parquet路径> <输出json路径>
单位感知：volume 不再无条件 ×100，用 amount 物理校验判定——
  ratio = amount / (volume * close)
  ratio ∈ (30,300) → volume 单位为"手" → ×100 转"股"
  ratio ∈ (0.3,3)  → volume 单位为"股" → 保持
  amount 缺失/异常 → 保守保持原值并打印提示
"""
import json
import os
import sys

import pandas as pd

WINDOW = 20


def fmt_date(d):
    d = str(d).replace("-", "")
    return f"{d[:4]}-{d[4:6]}-{d[6:8]}"


def unit_of(amount, volume, close):
    """返回 1(股/保持) 或 100(手→×100)；无法判定返回 None。"""
    if amount is None or volume is None or close is None:
        return None
    try:
        a, v, c = float(amount), float(volume), float(close)
    except (TypeError, ValueError):
        return None
    if a <= 0 or v <= 0 or c <= 0:
        return None
    ratio = a / (v * c)
    if 30 < ratio < 300:
        return 100
    if 0.3 < ratio < 3:
        return 1
    return None


def bars_to_input(bars):
    """通用表行 → v3 风格 input JSON 字符串。单位感知：手→股(×100)、股→保持。"""
    rows = []
    prev_close = None
    for b in bars:
        date_fmt = fmt_date(b["date"])
        close = float(b["close"])
        pct = 0.0
        if prev_close:
            pct = round((close - prev_close) / prev_close * 100, 2)
        mult = unit_of(b.get("amount"), b.get("volume"), close)
        vol = int(float(b["volume"]))
        if mult == 100:
            vol = int(float(b["volume"]) * 100)
        elif mult is None:
            # amount 缺失（如转债/部分 ETF）：默认按手×100，与旧行为一致并提示
            vol = int(float(b["volume"]) * 100)
        rows.append({
            "日期": date_fmt,
            "开盘": round(float(b["open"]), 2),
            "收盘": round(close, 2),
            "最高": round(float(b["high"]), 2),
            "最低": round(float(b["low"]), 2),
            "成交量": vol,
            "涨跌幅": pct,
        })
        prev_close = close
    return json.dumps(rows, ensure_ascii=False)


def build_instruction(code, last_date):
    return (f"你是专业的A股量化分析师。请基于以下截至{fmt_date(last_date)}的"
            f"近{WINDOW}个交易日行情数据，对股票 {code} 做技术面分析并给出操作建议。")


def to_v4(parquet_path, out_path):
    df = pd.read_parquet(parquet_path)
    if len(df) < WINDOW:
        print(f"跳过 {parquet_path}: 不足{WINDOW}根 ({len(df)})")
        return 0
    df = df.sort_values("date").reset_index(drop=True)
    code = os.path.basename(parquet_path).replace(".parquet", "")[-6:]
    win = df.iloc[-WINDOW:]
    bars = win.to_dict("records")
    last = bars[-1]
    sample = {
        "instruction": build_instruction(code, last["date"]),
        "input": bars_to_input(bars),
        "output": "",
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump([sample], f, ensure_ascii=False, indent=1)
    print(f"生成样本 1 条 -> {out_path}")
    return 1


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("用法: python transform/to_v4.py <parquet> <out.json>")
        sys.exit(1)
    sys.exit(0 if to_v4(sys.argv[1], sys.argv[2]) else 2)
