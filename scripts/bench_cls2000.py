#!/usr/bin/env python3
"""v7-resume 分类稳健验证：未来 120 交易日是否翻倍（2000+ 样本）。

与 bench_v567.py 的「操作建议代理」不同，本脚本采用**语义一致的判定式任务**：
  给定截至某日的近20交易日行情 → 模型直接判断「未来120交易日是否翻倍(累计>=100%)」。
  label 与预测同为「是否翻倍」二值，指标可直接解释，避免代理偏差。

读 data/cls2000_samples.jsonl（由 scripts/build_cls2000.py 构造，固定种子可重复），
评估 v7(基线) / v7r(续训顶层) / v7r1115(续训 ckpt)，输出：
  Accuracy / Precision / Recall / F1(正类) / 混淆矩阵 / 多数类基线 /
  按年份 & 板块分层准确率（偏差检查）/ 未决率。

用法:
  venv/bin/python scripts/bench_cls2000.py --limit 50 --models v7        # 快速验证解析/速度
  venv/bin/python scripts/bench_cls2000.py                              # 全量三模型
  venv/bin/python scripts/bench_cls2000.py --concurrency 4             # 并发加速(显存足够时)
输出: data/bench_cls2000.json + 终端对比表
"""
from __future__ import annotations

import argparse
import json
import re
import time
import urllib.request as _urllib
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path("/home/jiuben/tdx-data-feed")
SAMPLES = ROOT / "data" / "cls2000_samples.jsonl"
OUT = ROOT / "data" / "bench_cls2000.json"
OLLAMA_URL = "http://127.0.0.1:11434"

MODELS = {
    "v7": "qwen3-14b-tdx-v7",
    "v7r": "qwen3-14b-tdx-v7r",
    "v7r1115": "qwen3-14b-tdx-v7r1115",
}


# ---------- Ollama ----------
def call_ollama(model: str, prompt: str, opts: dict, timeout: int = 120):
    payload = {"model": model, "prompt": prompt, "stream": False, "options": opts, "think": False}
    req = _urllib.Request(
        f"{OLLAMA_URL}/api/generate", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    t0 = time.time()
    with _urllib.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read())
    return d.get("response", ""), time.time() - t0


# ---------- 任务构造 / 解析 ----------
def build_prompt(s: dict) -> str:
    inp = json.loads(s["input"]) if isinstance(s["input"], str) else s["input"]
    return (
        f"你是专业的A股量化分析师。下面是一支股票截至 {s['last_date']} 的近20个交易日行情（JSON）。\n"
        f"请判断：该股票在未来120个交易日内，累计涨幅是否有可能达到或超过100%（即翻倍）。\n"
        f"只回答以下二者之一，不要任何额外解释：\n- 会翻倍\n- 不会翻倍\n\n"
        f"行情数据：\n{json.dumps(inp, ensure_ascii=False)}"
    )


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
def parse_pred(resp: str) -> int:
    """返回 1=会翻倍, 0=不会翻倍, -1=未决(无法解析)。先剥离 <think> 推理块。"""
    r = re.sub(_THINK_STRAY, '', re.sub(_THINK_BLOCK, '', resp, flags=re.S)).strip()
    if "不会翻倍" in r:
        return 0
    if "会翻倍" in r:
        return 1
    if ("翻倍" in r) and ("会" in r):
        return 1
    if ("翻倍" in r) and any(k in r for k in ("不", "难", "无", "未", "否")):
        return 0
    return -1


# ---------- 单模型评测 ----------
def bench_model(model_tag: str, samples: list[dict], opts: dict, concurrency: int = 1):
    try:
        call_ollama(model_tag, "你好", opts)
    except Exception as e:
        print(f"  [warmup] {model_tag} 失败: {e}", flush=True)

    results: list[tuple] = [None] * len(samples)

    def work(i: int, s: dict):
        p = build_prompt(s)
        try:
            resp, _ = call_ollama(model_tag, p, opts)
        except Exception:
            return (i, -2, "")
        return (i, parse_pred(resp), resp)

    if concurrency <= 1:
        for i, s in enumerate(samples):
            results[i] = work(i, s)
            if (i + 1) % 200 == 0:
                print(f"  [{model_tag}] {i + 1}/{len(samples)}", flush=True)
    else:
        with ThreadPoolExecutor(max_workers=concurrency) as ex:
            for res in ex.map(work, range(len(samples)), samples):
                results[res[0]] = res
        print(f"  [{model_tag}] 并发完成 {len(results)}", flush=True)
    return results


# ---------- 评估 ----------
def evaluate(samples: list[dict], preds_by_model: dict):
    y_true = [s["label"] for s in samples]
    n = len(samples)
    out = {}
    for tag, plist in preds_by_model.items():
        pred_map = {i: p for i, p, _ in plist}
        tp = fp = tn = fn = und = 0
        for t, i in zip(y_true, range(n)):
            p = pred_map.get(i, -2)
            if p in (-1, -2):
                und += 1
                continue
            if t == 1 and p == 1:
                tp += 1
            elif t == 0 and p == 0:
                tn += 1
            elif t == 1 and p == 0:
                fn += 1
            elif t == 0 and p == 1:
                fp += 1
        valid = tp + fp + tn + fn
        acc = (tp + tn) / valid if valid else 0
        prec = tp / (tp + fp) if (tp + fp) else 0
        rec = tp / (tp + fn) if (tp + fn) else 0
        f1 = 2 * prec * rec / (prec + rec) if (prec + rec) else 0
        majority = max(Counter(y_true).values()) / n  # 全预测多数类的准确率(此处=0.5均衡集)
        by_year, by_mkt = defaultdict(Counter), defaultdict(Counter)
        for t, i, s in zip(y_true, range(n), samples):
            p = pred_map.get(i, -2)
            if p in (-1, -2):
                continue
            by_year[s["year"]][("c" if t == p else "w")] += 1
            by_mkt[s["market"]][("c" if t == p else "w")] += 1
        yr = {y: c["c"] / (c["c"] + c["w"]) * 100 if (c["c"] + c["w"]) else 0
              for y, c in sorted(by_year.items())}
        mk = {m: c["c"] / (c["c"] + c["w"]) * 100 if (c["c"] + c["w"]) else 0
              for m, c in sorted(by_mkt.items())}
        out[tag] = {
            "accuracy": acc, "precision": prec, "recall": rec, "f1": f1,
            "tp": tp, "fp": fp, "tn": tn, "fn": fn, "undetermined": und,
            "majority_baseline_acc": majority,
            "by_year_acc": {str(k): round(v, 1) for k, v in yr.items()},
            "by_market_acc": {str(k): round(v, 1) for k, v in mk.items()},
        }
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default="v7,v7r,v7r1115")
    ap.add_argument("--limit", type=int, default=None, help="仅评前 N 个样本(快速验证)")
    ap.add_argument("--num-predict", type=int, default=64, help="生成 token 上限(判定短答)")
    ap.add_argument("--concurrency", type=int, default=1, help="并发请求数(显存足够时加速)")
    ap.add_argument("--temperature", type=float, default=0.3)
    args = ap.parse_args()

    opts = {"temperature": args.temperature, "top_p": 0.95, "top_k": 20,
            "num_predict": args.num_predict, "num_ctx": 4096}

    samples = [json.loads(l) for l in open(SAMPLES) if l.strip()]
    if args.limit:
        samples = samples[: args.limit]
    selected = [m for m in args.models.split(",") if m in MODELS]
    print(f"样本={len(samples)} 模型={selected} num_predict={args.num_predict} "
          f"concurrency={args.concurrency}", flush=True)

    results = {}
    for tag in selected:
        print(f"\n===== 评测 {tag} ({MODELS[tag]}) =====", flush=True)
        results[tag] = bench_model(MODELS[tag], samples, opts, args.concurrency)

    evals = evaluate(samples, results)
    OUT.write_text(json.dumps({
        "opts": opts, "n": len(samples), "models": selected,
        "evals": evals,
        "raw": {k: [[i, p] for i, p, _ in v] for k, v in results.items()},
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n# 分类稳健验证 (2000+ 样本)\n")
    hdr = "| 模型 | Acc | Prec | Rec | F1 | TP/FP/TN/FN | 未决 | 多数基线 |"
    sep = "|------|-----|------|-----|----|-----------|------|---------|"
    print(hdr); print(sep)
    for tag in selected:
        e = evals[tag]
        print(f"| {tag} | {e['accuracy']*100:.1f}% | {e['precision']*100:.1f}% | "
              f"{e['recall']*100:.1f}% | {e['f1']*100:.1f}% | "
              f"{e['tp']}/{e['fp']}/{e['tn']}/{e['fn']} | {e['undetermined']} | "
              f"{e['majority_baseline_acc']*100:.1f}% |")
    for tag in selected:
        e = evals[tag]
        print(f"\n[{tag}] 按年份准确率: {e['by_year_acc']}")
        print(f"[{tag}] 按板块准确率: {e['by_market_acc']}")
    print(f"\n报告: {OUT}")


if __name__ == "__main__":
    main()
