#!/usr/bin/env python3
"""
v6 LoRA BLEU/ROUGE 统计评估 — 从 v6_train_enriched_llmf.jsonl 取 15 个样本

输入: 5238 个 alpaca 格式样本
策略: 取其中 15 个真实样本（带 input OHLCV 数组），用 v6-final 模型生成响应，
      对比 ground truth output 算 BLEU + ROUGE

硬件: GPU 1 (QLoRA 4-bit 加载 14B 模型)
指标:
  - sacreBLEU (sentence_bleu + corpus_bleu)
  - ROUGE-1/2/L (F1)

输出: v5/audit/eval_v6_bleu_rouge.json
"""
import json
import random
import time
from pathlib import Path

import sacrebleu
import torch
from rouge_score import rouge_scorer
from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig

MODEL_PATH = "/home/jiuben/models/Qwen3-14B-tdx-v6-final"
DATA_PATH = "v5/audit/v6_train_enriched_llmf.jsonl"
N_SAMPLES = 15
SEED = 42
OUT = Path("v5/audit/eval_v6_bleu_rouge.json")

INSTR_TEMPLATE = """你是专业的A股量化分析师，已通过 v5 微调。
请基于以下截至{date}的近20个交易日行情数据，对 {name}（{code}）做技术面分析并给出操作建议。
输出要求（请严格遵守）：
1. 趋势判断：明确（上涨/下跌/横盘/震荡）
2. 技术指标：列出 MA5/MA20、MACD 状态、RSI 数值
3. 支撑压力：给出具体价位
4. 操作建议：明确（关注低吸/持股观察/逢高减仓/观望）"""


def build_prompt(sample):
    """alpaca 格式 → 模型 prompt"""
    instr = sample["instruction"]
    inp = sample["input"]
    # alpaca 标准: Instruction + Input + Response
    return f"### Instruction:\n{instr}\n\n### Input:\n{inp}\n\n### Response:\n"


def main():
    # 1) Load model (4-bit)
    print(f"=== Loading {MODEL_PATH} (4-bit) ===")
    t0 = time.time()
    tokenizer = AutoTokenizer.from_pretrained(MODEL_PATH)
    bnb_config = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_quant_type="nf4",
        bnb_4bit_compute_dtype=torch.bfloat16,
        bnb_4bit_use_double_quant=True,
    )
    model = AutoModelForCausalLM.from_pretrained(
        MODEL_PATH,
        quantization_config=bnb_config,
        device_map="cuda:0",
    )
    model.eval()
    print(f"  Loaded in {time.time()-t0:.1f}s, GPU mem: {torch.cuda.memory_allocated()/1024**3:.2f} GB")

    # 2) Load data + sample N
    print(f"=== Sampling {N_SAMPLES} from {DATA_PATH} ===")
    random.seed(SEED)
    with open(DATA_PATH) as f:
        all_lines = f.readlines()
    sample_indices = random.sample(range(len(all_lines)), N_SAMPLES)
    samples = [json.loads(all_lines[i]) for i in sample_indices]
    print(f"  total available: {len(all_lines)}, picked indices: {sample_indices[:5]}...")

    # 3) Generate + evaluate
    rouge = rouge_scorer.RougeScorer(["rouge1", "rouge2", "rougeL"], use_stemmer=False)
    bleu_sent_scores = []
    rouge_f1 = {"rouge1": [], "rouge2": [], "rougeL": []}
    results = []

    for i, s in enumerate(samples):
        prompt = build_prompt(s)
        # truncate input if too long
        inputs = tokenizer(prompt, return_tensors="pt", truncation=True, max_length=2048).to("cuda:0")
        t0 = time.time()
        with torch.no_grad():
            outputs = model.generate(
                **inputs,
                max_new_tokens=300,
                do_sample=False,
                temperature=1.0,
                top_p=1.0,
                pad_token_id=tokenizer.eos_token_id,
            )
        gen_time = time.time() - t0
        response = tokenizer.decode(outputs[0][inputs.input_ids.shape[1]:], skip_special_tokens=True).strip()
        truth = s["output"].strip()

        # BLEU
        try:
            bs = sacrebleu.sentence_bleu(response, [truth])
            bleu_sent_scores.append(bs.score)
        except Exception:
            bleu_sent_scores.append(0.0)
        # ROUGE
        rs = rouge.score(truth, response)
        for k in rouge_f1:
            rouge_f1[k].append(rs[k].fmeasure * 100)

        results.append({
            "sample_idx": sample_indices[i],
            "prompt_chars": len(prompt),
            "truth_chars": len(truth),
            "response_chars": len(response),
            "gen_time": gen_time,
            "truth": truth,
            "response": response,
            "bleu": bleu_sent_scores[-1],
            "rouge1": rs["rouge1"].fmeasure * 100,
            "rouge2": rs["rouge2"].fmeasure * 100,
            "rougeL": rs["rougeL"].fmeasure * 100,
        })
        print(f"  [{i+1}/{N_SAMPLES}] BLEU={bleu_sent_scores[-1]:.2f} ROUGE-L={rs['rougeL'].fmeasure*100:.2f} ({gen_time:.1f}s)")

    # 4) Aggregate
    corpus_bleu = sacrebleu.corpus_bleu([r["response"] for r in results],
                                         [[r["truth"]] for r in results]).score
    summary = {
        "model": MODEL_PATH,
        "n_samples": N_SAMPLES,
        "bleu_sentence_mean": sum(bleu_sent_scores) / len(bleu_sent_scores),
        "bleu_corpus": corpus_bleu,
        "rouge1_mean": sum(rouge_f1["rouge1"]) / len(rouge_f1["rouge1"]),
        "rouge2_mean": sum(rouge_f1["rouge2"]) / len(rouge_f1["rouge2"]),
        "rougeL_mean": sum(rouge_f1["rougeL"]) / len(rouge_f1["rougeL"]),
        "gen_time_mean": sum(r["gen_time"] for r in results) / len(results),
    }
    print("\n=== Summary ===")
    for k, v in summary.items():
        print(f"  {k}: {v:.2f}" if isinstance(v, float) else f"  {k}: {v}")

    OUT.write_text(json.dumps({"summary": summary, "details": results}, ensure_ascii=False, indent=2))
    print(f"\n✓ 报告: {OUT}")


if __name__ == "__main__":
    main()
