#!/usr/bin/env python3
"""
v5/v6/v7 推理对标（F1）
  - 生成质量：BLEU(sacreBLEU) + ROUGE-1/2/L，对比 train/data/test.jsonl 的 output 真值
  - 分类准确率：构造「窗口末尾→未来120交易日是否翻倍(>=100%)」标签集，模型输出多空判定比对
  - P95 延迟：逐请求墙钟时间（已预热，排除冷启动）
  - 显存峰值：nvidia-smi 轮询 memory.used 最大值

用法:
  venv/bin/python scripts/bench_v567.py --n-bleu 20 --n-cls 30
  venv/bin/python scripts/bench_v567.py --models v5 --n-bleu 2 --n-cls 2   # 快速验证

输出: data/bench_v567.json + 终端对比表(markdown)
"""
from __future__ import annotations

import argparse
import json
import random
import re
import subprocess
import threading
import time
from pathlib import Path

import pandas as pd
import sacrebleu
from rouge_score import rouge_scorer

ROOT = Path("/home/jiuben/tdx-data-feed")

from _common import daily_dir  # noqa: E402

PARQUET_DIR = daily_dir()
TEST_JSONL = ROOT / "train" / "data" / "test.jsonl"
OUT = ROOT / "data" / "bench_v7r.json"
OLLAMA_URL = "http://127.0.0.1:11434"

MODELS = {
    "v5": "qwen3-14b-tdx-v5",
    "v6": "qwen3-14b-tdx-v6",
    "v7": "qwen3-14b-tdx-v7",
    "v7r": "qwen3-14b-tdx-v7r",
    "v7r1115": "qwen3-14b-tdx-v7r1115",
}

GEN_OPTS = {"temperature": 0.3, "top_p": 0.95, "top_k": 20,
            "num_predict": 350, "num_ctx": 4096}


# ---------- Ollama ----------
def call_ollama(model: str, prompt: str, timeout: int = 600) -> tuple[str, float, float, int]:
    """返回 (response, wall_s, eval_s, eval_count)。wall_s 含排队/加载。"""
    payload = {"model": model, "prompt": prompt, "stream": False, "options": GEN_OPTS}
    req = urllib_request_payload(payload)
    t0 = time.time()
    with urllib_request_urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read())
    wall = time.time() - t0
    return d.get("response", ""), wall, d.get("eval_duration", 0) / 1e9, d.get("eval_count", 0)


# 兼容 py<3.11 的 urllib 引用
import urllib.request as _urllib

urllib_request_payload = lambda p: _urllib.Request(
    f"{OLLAMA_URL}/api/generate", data=json.dumps(p).encode(),
    headers={"Content-Type": "application/json"})
urllib_request_urlopen = _urllib.urlopen


# ---------- 显存轮询 ----------
class VramProbe:
    def __init__(self, idx: int = 0):
        self.idx = idx
        self._stop = False
        self.peak = 0
        self._t = None

    def _loop(self):
        while not self._stop:
            try:
                out = subprocess.run(
                    ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits",
                     f"--id={self.idx}"], capture_output=True, text=True, timeout=5)
                v = int(out.stdout.strip().split("\n")[0])
                if v > self.peak:
                    self.peak = v
            except Exception as e:  # P0-2 2026-10-17: 静默吞错改留痕, 控制流不变
                print(f"[WARN] swallowed: {e!r}")
            time.sleep(0.3)

    def start(self):
        self._stop = False
        self.peak = 0
        self._t = threading.Thread(target=self._loop, daemon=True)
        self._t.start()

    def stop(self):
        self._stop = True
        if self._t:
            self._t.join(timeout=2)


# ---------- 输入构造 ----------
# 统一日期解析：单一数据源见 tdxfeed/date_utils.to_date_str
from _common import to_date_str as parse_date  # noqa: E402


def build_input_from_rows(rows) -> str:
    return json.dumps(rows, ensure_ascii=False)


def daily_tail(code: str, end_idx: int | None = None, window: int = 20):
    """返回 (last_date, input_json, df_tail) 或 None。end_idx 指定窗口末尾行号。"""
    for cand in (code, f"sh{code}", f"sz{code}", f"bj{code}"):
        p = PARQUET_DIR / f"{cand}.parquet"
        if p.exists():
            df = pd.read_parquet(p, columns=["date", "open", "high", "low", "close", "volume"])
            df = df.sort_values("date").reset_index(drop=True)
            if len(df) < window + 1:
                return None
            if end_idx is None:
                end_idx = len(df) - 1
            start = max(0, end_idx - window + 1)
            df_tail = df.iloc[start:end_idx + 1].reset_index(drop=True)
            break
    else:
        return None
    rows, prev = [], None
    for _, row in df_tail.iterrows():
        d_str = parse_date(row["date"])
        close = float(row["close"])
        pct = 0.0 if prev is None else round((close - prev) / prev * 100, 2)
        rows.append({"日期": d_str, "开盘": round(float(row["open"]), 2), "收盘": round(close, 2),
                     "最高": round(float(row["high"]), 2), "最低": round(float(row["low"]), 2),
                     "成交量": round(float(row["volume"]) * 100, 0), "涨跌幅": pct})
        prev = close
    return parse_date(df_tail.iloc[-1]["date"]), build_input_from_rows(rows), df, end_idx


def extract_advice(text: str) -> str:
    m = re.search(r"建议([\u4e00-\u9fff]+?)(?:[。,.]|$)", text)
    if not m:
        return "其他"
    a = m.group(1)
    if "低吸" in a or "关注" in a or "回调" in a:
        return "关注低吸"
    if "减仓" in a or "高抛" in a:
        return "逢高减仓"
    if "持股" in a:
        return "持股观察"
    return "其他"


def is_bullish(advice: str) -> bool:
    return advice in ("关注低吸", "持股观察")


def build_prompt(instruction: str, inp: str) -> str:
    return f"### Instruction:\n{instruction}\n\n### Input:\n{inp}\n\n### Response:\n"


# ---------- 数据集 ----------
def load_bleu_samples(n: int, seed: int = 42):
    random.seed(seed)
    lines = [json.loads(l) for l in open(TEST_JSONL) if l.strip()]
    idx = random.sample(range(len(lines)), min(n, len(lines)))
    out = []
    for i in idx:
        s = lines[i]
        instr = s["instruction"]
        inp = s["input"]
        truth = s["output"]
        out.append((build_prompt(instr, inp), truth))
    return out


def build_cls_samples(n: int, seed: int = 7, horizon: int = 120):
    """构造 (prompt, label) 标签集：窗口末尾后 horizon 交易日最大涨幅 >=100% 为 1。"""
    random.seed(seed)
    files = [p for p in PARQUET_DIR.glob("*.parquet") if re.match(r"^(sh|sz|bj)?\d{6}$", p.stem)]
    random.shuffle(files)
    out = []
    for p in files:
        code = re.sub(r"^(sh|sz|bj)", "", p.stem)
        # 取一个有充足未来历史的窗口末尾
        res = daily_tail(code, end_idx=None)
        if not res:
            continue
        _, _, df, _ = res
        # 需要末尾之后至少 horizon 交易日
        if len(df) < 30 + horizon:
            continue
        # 随机选一个历史末尾（避开太新/太旧）
        lo, hi = 30, len(df) - horizon - 1
        if lo >= hi:
            continue
        end_idx = random.randint(lo, hi)
        res = daily_tail(code, end_idx=end_idx)
        if not res:
            continue
        last_date, inp, df2, _ = res
        # 标签：未来 horizon 交易日最大涨幅
        fut = df2.iloc[end_idx + 1:end_idx + 1 + horizon]["close"].astype(float)
        base = float(df2.iloc[end_idx]["close"])
        max_ret = (fut.max() / base - 1) if len(fut) >= horizon else -1
        label = 1 if max_ret >= 1.0 else 0
        instr = (f"你是专业的A股量化分析师。请基于以下截至{last_date}的近20个交易日行情数据，"
                 f"对股票 {code} 做技术面分析并给出操作建议。")
        out.append((build_prompt(instr, inp), label, code, last_date, round(max_ret * 100, 1)))
        if len(out) >= n:
            break
    return out


# ---------- 单模型评测 ----------
def bench_model(model_tag: str, bleu_samples, cls_samples):
    rouge = rouge_scorer.RougeScorer(["rouge1", "rouge2", "rougeL"], use_stemmer=False)
    vram = VramProbe(0)
    vram.start()

    # 预热（不计入指标）
    try:
        call_ollama(model_tag, "你好", timeout=600)
    except Exception as e:
        print(f"  [warmup] {model_tag} 失败: {e}", flush=True)

    bleu_sent, rouge_f1 = [], {"rouge1": [], "rouge2": [], "rougeL": []}
    walls, evals = [], []
    for i, (prompt, truth) in enumerate(bleu_samples):
        try:
            resp, wall, ev, _ = call_ollama(model_tag, prompt, timeout=600)
        except Exception as e:
            print(f"  [bleu {i}] ERR {e}", flush=True)
            continue
        walls.append(wall); evals.append(ev)
        try:
            bleu_sent.append(sacrebleu.sentence_bleu(resp, [truth]).score)
        except Exception:
            bleu_sent.append(0.0)
        rs = rouge.score(truth, resp)
        for k in rouge_f1:
            rouge_f1[k].append(rs[k].fmeasure * 100)

    # 分类
    correct = 0
    cls_total = 0
    cls_details = []
    for i, (prompt, label, code, last_date, maxret) in enumerate(cls_samples):
        try:
            resp, wall, ev, _ = call_ollama(model_tag, prompt, timeout=600)
        except Exception as e:
            print(f"  [cls {i}] ERR {e}", flush=True)
            continue
        walls.append(wall); evals.append(ev)
        adv = extract_advice(resp)
        pred = 1 if is_bullish(adv) else 0
        cls_total += 1
        if pred == label:
            correct += 1
        cls_details.append({"code": code, "last_date": last_date, "maxret%": maxret,
                            "label": label, "pred": pred, "advice": adv})

    vram.stop()
    # P95
    sw = sorted(walls)
    p95 = sw[int(0.95 * (len(sw) - 1))] if sw else 0.0

    return {
        "model": model_tag,
        "n_bleu": len(bleu_sent),
        "n_cls": cls_total,
        "bleu_sentence_mean": sum(bleu_sent) / len(bleu_sent) if bleu_sent else 0,
        "bleu_corpus": sacrebleu.corpus_bleu(
            [d["response"] for d in []] or [""], [[""]]).score if False else None,
        "rouge1_mean": sum(rouge_f1["rouge1"]) / len(rouge_f1["rouge1"]) if rouge_f1["rouge1"] else 0,
        "rouge2_mean": sum(rouge_f1["rouge2"]) / len(rouge_f1["rouge2"]) if rouge_f1["rouge2"] else 0,
        "rougeL_mean": sum(rouge_f1["rougeL"]) / len(rouge_f1["rougeL"]) if rouge_f1["rougeL"] else 0,
        "cls_accuracy": correct / cls_total if cls_total else 0,
        "latency_p95_s": p95,
        "latency_mean_s": sum(walls) / len(walls) if walls else 0,
        "eval_mean_s": sum(evals) / len(evals) if evals else 0,
        "vram_peak_mib": vram.peak,
        "cls_details": cls_details,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default="v7,v7r,v7r1115", help="逗号分隔: v5,v6,v7")
    ap.add_argument("--n-bleu", type=int, default=20)
    ap.add_argument("--n-cls", type=int, default=30)
    args = ap.parse_args()

    selected = [m for m in args.models.split(",") if m in MODELS]
    bleu_samples = load_bleu_samples(args.n_bleu)
    cls_samples = build_cls_samples(args.n_cls)
    print(f"BLEU 样本 {len(bleu_samples)} | 分类样本 {len(cls_samples)} | 模型 {selected}", flush=True)

    results = {}
    for key in selected:
        print(f"\n===== 评测 {key} ({MODELS[key]}) =====", flush=True)
        r = bench_model(MODELS[key], bleu_samples, cls_samples)
        results[key] = r
        print(f"  BLEU={r['bleu_sentence_mean']:.2f} ROUGE-L={r['rougeL_mean']:.2f} "
              f"ACC={r['cls_accuracy']*100:.1f}% P95={r['latency_p95_s']:.1f}s VRAM={r['vram_peak_mib']}MiB",
              flush=True)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(results, ensure_ascii=False, indent=2))

    # 对比表
    print("\n\n# F1 v5/v6/v7 推理对标对比\n")
    hdr = "| 模型 | BLEU(sent) | ROUGE-1 | ROUGE-2 | ROUGE-L | 分类准确率 | P95延迟(s) | 平均延迟(s) | 显存峰值(MiB) |"
    sep = "|------|-----------|---------|---------|---------|-----------|-----------|------------|---------------|"
    print(hdr); print(sep)
    for k in selected:
        r = results[k]
        print(f"| {k} | {r['bleu_sentence_mean']:.2f} | {r['rouge1_mean']:.1f} | {r['rouge2_mean']:.1f} | "
              f"{r['rougeL_mean']:.1f} | {r['cls_accuracy']*100:.1f}% | {r['latency_p95_s']:.1f} | "
              f"{r['latency_mean_s']:.1f} | {r['vram_peak_mib']} |")
    print(f"\n报告: {OUT}")


if __name__ == "__main__":
    main()
