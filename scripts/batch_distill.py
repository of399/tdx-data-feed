import argparse
import json
import os
import re
import time

import torch
from peft import PeftModel
from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig

BASE = "/home/workbuddy/models/Qwen3-14B"
ADAPTER = "/home/workbuddy/lf_project/output/stock_abc"
SK = "/home/jiuben/tdx-data-feed/data/distill/skeleton.jsonl"
OUT = "/home/jiuben/tdx-data-feed/data/distill/full.jsonl"
NUM_PREDICT = int(os.environ.get("NUM_PREDICT", "120"))

_LT, _GT, _PIPE, _SLASH, _BS = chr(60), chr(62), chr(124), chr(47), chr(92)
# 旧写法剥的是尖括号那对标记——模型从不发它，实测剥掉 0 字节。标签一律按字符拼：这些控制标记
# 在部分渲染/传输链路里会被整段吞掉，写错成「从来没出现过的那一对」从源码里也看不出来。
# 形状与用例见 tests/test_distill_think_filter.py，跨文件守卫见 tests/test_think_filter_behavior.py。
_OPT = _BS + _PIPE + "?"                       # 正则里的「可选竖线」
_THINK_OPEN = _LT + _OPT + "think" + _OPT + _GT
_THINK_CLOSE = _LT + _OPT + _SLASH + "think" + _OPT + _GT
_IM_END = _LT + _BS + _PIPE + "im_end" + _OPT + _GT
_THINK_BLOCK = re.compile(
    _THINK_OPEN + ".*?(?:" + _THINK_CLOSE + "|" + _IM_END + ")" + _BS + "s*", re.S)
_THINK_STRAY = re.compile(_THINK_OPEN + "|" + _THINK_CLOSE + "|" + _IM_END)
def clean(t):
    return re.sub(_THINK_STRAY, '', re.sub(_THINK_BLOCK, '', t, flags=re.S)).strip()

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--batch", type=int, default=8)
    a = ap.parse_args()
    print("[load] tokenizer", flush=True)
    tok = AutoTokenizer.from_pretrained(BASE)
    print("[load] base 4bit", flush=True)
    bnb = BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_compute_dtype=torch.bfloat16,
                             bnb_4bit_quant_type="nf4", bnb_4bit_use_double_quant=True)
    model = AutoModelForCausalLM.from_pretrained(BASE, quantization_config=bnb,
                                                 device_map="auto", dtype=torch.bfloat16)
    print("[load] LoRA", flush=True)
    model = PeftModel.from_pretrained(model, ADAPTER)
    model.eval()
    dev = next(model.parameters()).device
    rows = [json.loads(l) for l in open(SK, encoding='utf-8')]
    if a.limit:
        rows = rows[:a.limit]
    done = 0
    if os.path.exists(OUT):
        for _ in open(OUT, encoding='utf-8'):
            done += 1
    print(f"total={len(rows)} done={done} batch={a.batch}", flush=True)
    rows = rows[done:]
    fout = open(OUT, 'a', encoding='utf-8')
    prompts = [r['instruction'] + "\n" + r['input'] for r in rows]
    t0 = time.time()
    for i in range(0, len(prompts), a.batch):
        chunk = prompts[i:i + a.batch]
        msgs = [[{"role": "user", "content": p}] for p in chunk]
        input_ids = tok.apply_chat_template(msgs, tokenize=True, add_generation_prompt=True,
                                            padding=True, return_tensors="pt").to(dev)
        attention_mask = (input_ids != tok.pad_token_id).long()
        in_len = attention_mask.sum(dim=1)
        with torch.no_grad():
            out = model.generate(input_ids=input_ids, attention_mask=attention_mask,
                                max_new_tokens=NUM_PREDICT, do_sample=False,
                                temperature=1.0, top_p=1.0, pad_token_id=tok.eos_token_id)
        for j in range(len(chunk)):
            gen = out[j][in_len[j]:]
            text = clean(tok.decode(gen, skip_special_tokens=True))
            s = rows[i + j]
            s['output'] = text
            fout.write(json.dumps(s, ensure_ascii=False) + '\n')
        fout.flush()
        el = time.time() - t0
        print(f"  {done+i+len(chunk)}/{len(rows)+done}  {el:.0f}s  {(i+len(chunk))/el:.2f}/s", flush=True)
    fout.close()
    print("DISTILL_DONE", OUT)

main()
