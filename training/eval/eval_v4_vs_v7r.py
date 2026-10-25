#!/usr/bin/env python3
"""
eval_v4_vs_v7r.py — v4 测试集方向准确率/格式遵循率 双模型并排对比
===============================================================
用途：在 data/v4/test.jsonl 上，对两个「合并后的完整 HF 模型」
（如新训 v4 合并模型 与 现役 v7r 合并模型）分别计算：
    - 方向准确率：模型预测方向 vs 测试集参考 output 方向
    - 格式遵循率：模型输出可被解析出方向(parsed_ok)的占比
    - 解析失败率
复用 run_v4_eval.py 的解析与评测逻辑，保证口径一致；两模型均以
4-bit(nf4) 加载以在单卡 16GB 上放下 14B，对比公平。

用法:
    python eval_v4_vs_v7r.py \
        --model-a /home/jiuben/models/Qwen3-14B-tdx-v4-merged --name-a "v4-new" \
        --model-b /home/jiuben/models/Qwen3-14B-v7r-merged   --name-b "v7r" \
        --quantize-4bit --limit 120 --batch-size 8

输出: training/eval/compare_<timestamp>.json + .md
"""
import argparse
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path

import numpy as np

ROOT = Path("/home/jiuben/tdx-data-feed")
V4_TEST = ROOT / "data/v4/test.jsonl"
EVAL_OUT_DIR = ROOT / "training/eval"

# === 解析逻辑（与 run_v4_eval.py 保持一致）==================================
DIRECTION_RE = re.compile(r"(?:方向|预测|判断|涨跌)[：:]\s*(涨|上涨|看涨|看多|up|跌|下跌|看跌|看空|down|平|震荡|横盘|flat)")
PCT_RE = re.compile(r"(?:幅度|涨跌幅|预期)[：:]\s*(-?\d+(?:\.\d+)?)\s*%")


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
def parse_output(text: str) -> dict:
    result = {"parsed_ok": False, "direction": None, "pct": None, "raw": text}
    if not text:
        return result
        ans = re.sub(_THINK_STRAY, '', re.sub(_THINK_BLOCK, '', text, flags=re.S)).strip()
    work = ans if ans else text
    md = DIRECTION_RE.search(work)
    if md:
        d = md.group(1).lower()
        if d in ("涨", "上涨", "看涨", "看多", "up"):
            result["direction"] = 1
        elif d in ("跌", "下跌", "看跌", "看空", "down"):
            result["direction"] = -1
        else:
            result["direction"] = 0
        result["parsed_ok"] = True
    else:
        if re.search(r"上涨|看涨|看多|走强|上行|偏多|低吸|做多", work):
            result["direction"] = 1; result["parsed_ok"] = True
        elif re.search(r"下跌|看跌|看空|走弱|下行|偏空|高抛|做空", work):
            result["direction"] = -1; result["parsed_ok"] = True
        elif re.search(r"震荡|横盘|盘整|观望|中性", work):
            result["direction"] = 0; result["parsed_ok"] = True
    mp = PCT_RE.search(work)
    if mp:
        result["pct"] = float(mp.group(1))
    return result


def infer_batch(tok, model, samples: list, max_new_tokens: int = 256):
    import torch
    tok.padding_side = "left"
    prompts = []
    for s in samples:
        content = s["instruction"]
        if s.get("input"):
            content = content + "\n" + s["input"]
        content = content + "\n<no_think>"
        messages = [{"role": "user", "content": content}]
        prompts.append(tok.apply_chat_template(messages, add_generation_prompt=True, tokenize=False))
    enc = tok(prompts, padding=True, truncation=True, max_length=1536, return_tensors="pt").to(model.device)
    with torch.no_grad():
        out = model.generate(**enc, max_new_tokens=max_new_tokens, do_sample=False,
                             temperature=1.0, pad_token_id=tok.pad_token_id)
    return tok.batch_decode(out[:, enc["input_ids"].shape[1]:], skip_special_tokens=True)


def eval_direction(tok, model, samples: list, batch_size: int = 8, max_new_tokens: int = 256,
                   progress_every: int = 1, status_path: str | None = None):
    preds, reals, recs = [], [], []
    n = len(samples)
    t0 = time.time()
    for bi, i in enumerate(range(0, n, batch_size)):
        batch = samples[i:i + batch_size]
        outs = infer_batch(tok, model, batch, max_new_tokens)
        for s, o in zip(batch, outs):
            parsed = parse_output(o)
            real = parse_output(s.get("output", ""))
            preds.append(parsed["direction"]); reals.append(real["direction"])
            recs.append({"pred_dir": parsed["direction"], "real_dir": real["direction"],
                         "parsed_ok": parsed["parsed_ok"]})
        done = min(i + batch_size, n)
        if bi % progress_every == 0 or done >= n:
            arr_p = np.array([p if p is not None else 0 for p in preds])
            arr_r = np.array([r if r is not None else 0 for r in reals])
            valid = np.array([p is not None and r is not None for p, r in zip(preds, reals)])
            cur_acc = float((arr_p[valid] == arr_r[valid]).mean()) if valid.sum() else 0.0
            cur_fail = sum(1 for r in recs if not r["parsed_ok"]) / len(recs) if recs else 0.0
            elapsed = time.time() - t0
            rate = done / elapsed if elapsed > 0 else 0.0
            print(f"  [direction] {done}/{n} 当前方向准确率≈{cur_acc:.4f} 解析失败率≈{cur_fail:.4f} "
                  f"({elapsed:.0f}s, {rate:.2f}样本/s)", flush=True)
            if status_path:
                try:
                    Path(status_path).write_text(json.dumps(
                        {"done": done, "n": n, "cur_dir_acc": cur_acc, "cur_parse_fail": cur_fail,
                         "elapsed_sec": elapsed, "rate_per_sec": rate}, ensure_ascii=False),
                        encoding="utf-8")
                except Exception:
                    pass
    arr_p = np.array([p if p is not None else 0 for p in preds])
    arr_r = np.array([r if r is not None else 0 for r in reals])
    valid = np.array([p is not None and r is not None for p, r in zip(preds, reals)])
    acc = float((arr_p[valid] == arr_r[valid]).mean()) if valid.sum() else 0.0
    parse_fail = sum(1 for r in recs if not r["parsed_ok"]) / len(recs) if recs else 0.0
    return acc, parse_fail, recs


def load_merged(model_dir: str, quantize_4bit: bool):
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    print(f"[load] merged={model_dir} quantize_4bit={quantize_4bit}", flush=True)
    tok = AutoTokenizer.from_pretrained(model_dir, trust_remote_code=True)
    if tok.pad_token is None:
        tok.pad_token = tok.eos_token
    if quantize_4bit:
        bnb = BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_compute_dtype=torch.bfloat16,
                                 bnb_4bit_quant_type="nf4", bnb_4bit_use_double_quant=True)
        model = AutoModelForCausalLM.from_pretrained(model_dir, quantization_config=bnb,
                                                    device_map="auto", trust_remote_code=True)
    else:
        model = AutoModelForCausalLM.from_pretrained(model_dir, torch_dtype=torch.bfloat16,
                                                    device_map="auto", trust_remote_code=True)
    model.eval()
    return tok, model


def run_one(name: str, model_dir: str, samples: list, quantize_4bit: bool, batch_size: int,
            max_new_tokens: int = 256, status_path: str | None = None):
    tok, model = load_merged(model_dir, quantize_4bit)
    t0 = time.time()
    acc, parse_fail, _ = eval_direction(tok, model, samples, batch_size, max_new_tokens,
                                        progress_every=1, status_path=status_path)
    fmt = 1.0 - parse_fail
    elapsed = time.time() - t0
    del model; import torch; torch.cuda.empty_cache()
    print(f"[{name}] 方向准确率={acc:.4f} 格式遵循率={fmt:.4f} 解析失败率={parse_fail:.4f} "
          f"样本={len(samples)} 耗时={elapsed:.1f}s", flush=True)
    return {"name": name, "model_dir": model_dir, "direction_accuracy": acc,
            "format_compliance_rate": fmt, "parse_fail_rate": parse_fail,
            "n_samples": len(samples), "elapsed_sec": elapsed,
            "max_new_tokens": max_new_tokens, "quantize_4bit": quantize_4bit}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model-a", default=None); ap.add_argument("--name-a", default="A")
    ap.add_argument("--model-b", default=None); ap.add_argument("--name-b", default="B")
    ap.add_argument("--quantize-4bit", action="store_true")
    ap.add_argument("--limit", type=int, default=120)
    ap.add_argument("--batch-size", type=int, default=8)
    ap.add_argument("--max-new-tokens", type=int, default=256)
    ap.add_argument("--single", default=None, help="单模型模式: 仅评测该合并模型目录并写指标 JSON")
    ap.add_argument("--single-name", default="single")
    ap.add_argument("--status-file", default=None, help="进度状态文件路径(单模型模式)")
    args = ap.parse_args()

    samples = []
    with open(V4_TEST, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            samples.append(json.loads(line))
            if args.limit and len(samples) >= args.limit:
                break
    print(f"v4 test 样本(limit={args.limit}): {len(samples)} 条", flush=True)

    if not args.single and (not args.model_a or not args.model_b):
        ap.error("非单模型模式下必须提供 --model-a 与 --model-b")

    if args.single:
        r = run_one(args.single_name, args.single, samples, args.quantize_4bit,
                    args.batch_size, args.max_new_tokens, args.status_file)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        EVAL_OUT_DIR.mkdir(parents=True, exist_ok=True)
        jf = EVAL_OUT_DIR / f"metrics_{args.single_name}_{ts}.json"
        jf.write_text(json.dumps(r, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n[单模型指标] {jf}")
        print(f"  方向准确率={r['direction_accuracy']:.4f} 格式遵循率={r['format_compliance_rate']:.4f} "
              f"解析失败率={r['parse_fail_rate']:.4f} 样本={r['n_samples']}")
        return

    ra = run_one(args.name_a, args.model_a, samples, args.quantize_4bit, args.batch_size, args.max_new_tokens)
    rb = run_one(args.name_b, args.model_b, samples, args.quantize_4bit, args.batch_size, args.max_new_tokens)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = {"timestamp": ts, "a": ra, "b": rb,
           "delta_dir_acc": ra["direction_accuracy"] - rb["direction_accuracy"],
           "delta_fmt": ra["format_compliance_rate"] - rb["format_compliance_rate"]}
    EVAL_OUT_DIR.mkdir(parents=True, exist_ok=True)
    jf = EVAL_OUT_DIR / f"compare_{ts}.json"
    jf.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n" + "=" * 60)
    print("v4 测试集 双模型方向/格式对比")
    print("=" * 60)
    print(f"| 指标 | {ra['name']} | {rb['name']} | Δ |")
    print(f"| 方向准确率 | {ra['direction_accuracy']:.4f} | {rb['direction_accuracy']:.4f} | {out['delta_dir_acc']:+.4f} |")
    print(f"| 格式遵循率 | {ra['format_compliance_rate']:.4f} | {rb['format_compliance_rate']:.4f} | {out['delta_fmt']:+.4f} |")
    print(f"| 解析失败率 | {ra['parse_fail_rate']:.4f} | {rb['parse_fail_rate']:.4f} | |")
    print(f"| 样本数 | {ra['n_samples']} | {rb['n_samples']} | |")
    print(f"\n[JSON] {jf}")
    print("对比完成")


if __name__ == "__main__":
    main()
