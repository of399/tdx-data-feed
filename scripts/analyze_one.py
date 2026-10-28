#!/usr/bin/env python3
"""单只股票技术面分析：用训练/蒸馏模型对指定标的做 K 线分析（仅模型输出，非投资建议）。"""
import json
import sys
import time

import pandas as pd
import torch
from _common import daily_path  # noqa: E402
from peft import PeftModel
from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig

WINDOW = 20
DM = {"": 0}  # 强制单卡，避开双卡跨卡通信瓶颈


def fmt(d):
    d = str(d).replace('-', '')
    return f"{d[:4]}-{d[4:6]}-{d[6:8]}"


def unit_mult(amount, volume, close):
    try:
        a, v, c = float(amount), float(volume), float(close)
    except (TypeError, ValueError):
        return 100
    if a <= 0 or v <= 0 or c <= 0:
        return 100
    r = a / (v * c)
    if 30 < r < 300:
        return 100
    if 0.3 < r < 3:
        return 1
    return 100


def build_rows(sym):
    df = pd.read_parquet(daily_path(sym)).sort_values('date').reset_index(drop=True)
    if len(df) < WINDOW:
        raise SystemExit(f"{sym} 数据不足 {WINDOW} 条")
    win = df.iloc[-WINDOW:]
    rows, prev = [], None
    for _, b in win.iterrows():
        close = float(b['close'])
        pct = round((close - prev) / prev * 100, 2) if prev else 0.0
        mult = unit_mult(b.get('amount'), b.get('volume'), close)
        vol = int(float(b['volume']))
        if mult == 100:
            vol = int(float(b['volume']) * 100)
        rows.append({'日期': fmt(b['date']), '开盘': round(float(b['open']), 2),
                     '收盘': round(close, 2), '最高': round(float(b['high']), 2),
                     '最低': round(float(b['low']), 2), '成交量': vol, '涨跌幅': pct})
        prev = close
    code = sym[-6:]
    last = win.iloc[-1]
    instr = (f'你是专业的A股量化分析师。请基于以下截至{fmt(last["date"])}的'
             f'近{WINDOW}个交易日行情数据，对股票 {code} 做技术面分析并给出操作建议。')
    return instr, json.dumps(rows, ensure_ascii=False)


def load(md, adapter=None):
    tok = AutoTokenizer.from_pretrained(md, trust_remote_code=True)
    bnb = BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_quant_type="nf4",
                             bnb_4bit_compute_dtype=torch.bfloat16)
    m = AutoModelForCausalLM.from_pretrained(md, quantization_config=bnb,
                                             device_map=DM, trust_remote_code=True)
    if adapter:
        m = PeftModel.from_pretrained(m, adapter)
    m.eval()
    return m, tok


def main():
    sym = sys.argv[1] if len(sys.argv) > 1 else 'sh601988'
    md = sys.argv[2] if len(sys.argv) > 2 else '/home/jiuben/models/Qwen3-14B-tdx-v6-merged'
    adapter = sys.argv[3] if len(sys.argv) > 3 else None
    instr, inp = build_rows(sym)
    prompt = instr + "\n" + inp
    print(f"[load] {md}{' +LoRA' if adapter else ''} ...", flush=True)
    t0 = time.time()
    m, tok = load(md, adapter)
    print(f"[loaded] {time.time()-t0:.1f}s", flush=True)
    txt = tok.apply_chat_template([{"role": "user", "content": prompt}],
                                 tokenize=False, add_generation_prompt=True)
    ids = tok(txt, return_tensors="pt").to(m.device)
    with torch.no_grad():
        g = m.generate(**ids, max_new_tokens=280, do_sample=False,
                       pad_token_id=tok.eos_token_id)
    out = tok.decode(g[0][ids.input_ids.shape[1]:], skip_special_tokens=True).strip()
    tag = md.split('/')[-1] + ('+LoRA' if adapter else '')
    print(f"\n===== {tag} 对 {sym[-6:]} 的技术面分析 =====\n")
    print(out)


if __name__ == '__main__':
    main()
