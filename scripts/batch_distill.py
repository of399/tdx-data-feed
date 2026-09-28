import json, os, time, re, argparse, torch
from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig
from peft import PeftModel

BASE = "/home/workbuddy/models/Qwen3-14B"
ADAPTER = "/home/workbuddy/lf_project/output/stock_abc"
SK = "/home/jiuben/tdx-data-feed/data/distill/skeleton.jsonl"
OUT = "/home/jiuben/tdx-data-feed/data/distill/full.jsonl"
NUM_PREDICT = int(os.environ.get("NUM_PREDICT", "120"))

def clean(t):
    return re.sub(r'<think>.*?</think>\s*', '', t, flags=re.S).strip()

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
