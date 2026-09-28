import json, sys, os, time, re, urllib.request, concurrent.futures as cf

BASE = os.environ.get("TDX_OLLAMA_URL", "http://127.0.0.1:11434")
MODEL = "tdx-lora:latest"
NUM_PREDICT = int(os.environ.get("NUM_PREDICT", "100"))

def clean(text):
    text = re.sub(r'<think>.*?</think>\s*', '', text, flags=re.S)
    return text.strip()

def call(prompt):
    payload = {"model": MODEL, "prompt": prompt, "stream": False,
               "options": {"num_predict": NUM_PREDICT, "temperature": 0.0}}
    req = json.dumps(payload).encode()
    for _ in range(3):
        try:
            r = urllib.request.urlopen(BASE + "/api/generate", data=req, timeout=180)
            return clean(json.loads(r.read()).get("response", ""))
        except Exception:
            time.sleep(2)
    return ""

def main():
    SK = sys.argv[1]
    OUT = sys.argv[2] if len(sys.argv) > 2 else "/home/jiuben/tdx-data-feed/data/distill/full.jsonl"
    LIMIT = int(sys.argv[3]) if len(sys.argv) > 3 else 0
    CONC = int(sys.argv[4]) if len(sys.argv) > 4 else 8
    rows = [json.loads(l) for l in open(SK, encoding='utf-8')]
    if LIMIT:
        rows = rows[:LIMIT]
    done = 0
    if os.path.exists(OUT):
        with open(OUT, encoding='utf-8') as f:
            for _ in f:
                done += 1
    print(f"total={len(rows)} done={done} conc={CONC} num_predict={NUM_PREDICT}")
    rows = rows[done:]
    fout = open(OUT, 'a', encoding='utf-8')
    prompts = [r['instruction'] + "\n" + r['input'] for r in rows]
    t0 = time.time()
    with cf.ThreadPoolExecutor(max_workers=CONC) as ex:
        for i, out in enumerate(ex.map(call, prompts, chunksize=4)):
            s = rows[i]
            s['output'] = out
            fout.write(json.dumps(s, ensure_ascii=False) + '\n')
            if (i + 1) % 200 == 0:
                el = time.time() - t0
                print(f"  {done+i+1}  {el:.0f}s  {(i+1)/el:.2f}/s")
                fout.flush()
    fout.close()
    print("DISTILL_DONE", OUT)

main()
