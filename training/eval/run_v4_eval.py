#!/usr/bin/env python3
"""
run_v4_eval.py — §14.5 训练后完整评估脚本
==========================================

用法:
    python run_v4_eval.py --ckpt training/output/qwen3-14b-tdx/checkpoint-2400
    python run_v4_eval.py --ckpt <ckpt> --limit 500           # 抽样 500 条快速验证
    python run_v4_eval.py --ckpt <ckpt> --no-backtest         # 跳过回测

输出:
    training/eval/metrics_<timestamp>.json    结构化指标
    training/eval/report_<timestamp>.md       可读评估报告

评估维度（对应计划书 §14.5）:
    1. test loss              LLM 标准
    2. 方向准确率              涨跌方向预测
    3. Top-k 命中率           候选池中真实涨幅 top-k 命中率
    4. 回测夏普比率            按预测信号模拟日频交易
    5. alpaca 格式遵循率       输出格式校验
"""

import argparse
import json
import os
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

import numpy as np

# === 路径与常量 ============================================================
ROOT = Path("/home/jiuben/tdx-data-feed")
V4_TEST = ROOT / "data/v4/test.jsonl"
DEFAULT_CKPT = ROOT / "train/output/qwen3-14b-tdx/checkpoint-2400"
BASE_MODEL_HF = "Qwen/Qwen3-14B"  # 或本地 HF 路径
EVAL_OUT_DIR = ROOT / "training/eval"


@dataclass
class EvalMetrics:
    """评估指标聚合"""
    n_samples: int = 0
    test_loss: float = 0.0
    direction_accuracy: float = 0.0           # 涨跌方向准确率
    top5_hit_rate: float = 0.0                # Top-5 命中率
    top10_hit_rate: float = 0.0
    sharpe_ratio: float = 0.0                 # 日频夏普
    annual_return: float = 0.0                # 年化收益
    max_drawdown: float = 0.0                 # 最大回撤
    format_compliance_rate: float = 0.0       # alpaca 格式遵循率
    parse_fail_rate: float = 0.0              # 解析失败率
    elapsed_sec: float = 0.0
    timestamp: str = ""


# === 模型加载 ===============================================================
def load_model(ckpt_path: Path, base_model: str = BASE_MODEL_HF):
    """加载 base + LoRA adapter（peft + transformers）"""
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    import torch
    from peft import PeftModel
    from transformers import AutoModelForCausalLM, AutoTokenizer

    print(f"[load] base={base_model} adapter={ckpt_path}")
    tok = AutoTokenizer.from_pretrained(base_model, trust_remote_code=True)
    if tok.pad_token is None:
        tok.pad_token = tok.eos_token
    base = AutoModelForCausalLM.from_pretrained(
        base_model,
        torch_dtype=torch.bfloat16,
        device_map="auto",
        trust_remote_code=True,
    )
    model = PeftModel.from_pretrained(base, str(ckpt_path), torch_dtype=torch.bfloat16)
    model.eval()
    return tok, model


# === 推理与解析 =============================================================
DIRECTION_RE = re.compile(r"(?:方向|预测|判断|涨跌)[：:]\s*(涨|跌|平|震荡|上涨|下跌|up|down|flat)")
PCT_RE = re.compile(r"(?:幅度|涨跌幅|预期)[：:]\s*(-?\d+(?:\.\d+)?)\s*%")
ALPACA_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", re.S)


def parse_output(text: str) -> dict:
    """从模型输出解析方向/幅度/置信度。失败返回 parsed_ok=False"""
    result = {"parsed_ok": False, "direction": None, "pct": None, "raw": text}
    # 1) alpaca JSON 围栏
    m = ALPACA_FENCE_RE.search(text)
    m.group(1) if m else text
    # 2) 方向
    md = DIRECTION_RE.search(text)
    if md:
        d = md.group(1).lower()
        if d in ("涨", "上涨", "up"):
            result["direction"] = 1
        elif d in ("跌", "下跌", "down"):
            result["direction"] = -1
        else:
            result["direction"] = 0
        result["parsed_ok"] = True
    # 3) 幅度
    mp = PCT_RE.search(text)
    if mp:
        result["pct"] = float(mp.group(1))
    return result


def infer_batch(tok, model, samples: list, max_new_tokens: int = 256):
    """批量推理（auto batching 处理变长 input）"""
    import torch
    prompts = []
    for s in samples:
        # alpaca 风格 prompt 模板（与训练保持一致）
        prompts.append(
            f"### 指令:\n{s['instruction']}\n\n"
            f"### 输入:\n{s.get('input', '')}\n\n"
            f"### 回答:\n"
        )
    enc = tok(prompts, padding=True, truncation=True, max_length=1024, return_tensors="pt").to(model.device)
    with torch.no_grad():
        out = model.generate(
            **enc,
            max_new_tokens=max_new_tokens,
            do_sample=False,
            temperature=1.0,
            pad_token_id=tok.pad_token_id,
        )
    decoded = tok.batch_decode(out[:, enc["input_ids"].shape[1]:], skip_special_tokens=True)
    return decoded


# === 评估各维度 =============================================================
def eval_direction(tok, model, samples: list, batch_size: int = 8) -> tuple[float, list]:
    """方向准确率：模型预测方向 vs 真实 output 中的方向"""
    preds, reals = [], []
    parsed_records = []
    n = len(samples)
    for i in range(0, n, batch_size):
        batch = samples[i:i + batch_size]
        outs = infer_batch(tok, model, batch)
        for s, o in zip(batch, outs, strict=False):
            parsed = parse_output(o)
            real = parse_output(s.get("output", ""))
            pred_dir = parsed["direction"]
            real_dir = real["direction"]
            preds.append(pred_dir)
            reals.append(real_dir)
            parsed_records.append({
                "code": (s.get("input", "")[:30] if s.get("input") else ""),
                "pred_dir": pred_dir,
                "real_dir": real_dir,
                "pred_pct": parsed["pct"],
                "real_pct": real["pct"],
                "parsed_ok": parsed["parsed_ok"],
            })
        if (i // batch_size) % 5 == 0:
            print(f"  [direction] {i + len(batch)}/{n}", flush=True)

    arr_p = np.array([p if p is not None else 0 for p in preds])
    arr_r = np.array([r if r is not None else 0 for r in reals])
    valid = np.array([p is not None and r is not None for p, r in zip(preds, reals, strict=False)])
    if valid.sum() == 0:
        return 0.0, parsed_records
    acc = (arr_p[valid] == arr_r[valid]).mean()
    parse_fail = 1.0 - parsed_records.count if False else (sum(1 for r in parsed_records if not r["parsed_ok"]) / len(parsed_records))
    return float(acc), parsed_records, parse_fail


def eval_format_compliance(parsed_records: list) -> float:
    """alpaca 格式遵循率 = 含 JSON 围栏/方向字段的占比"""
    if not parsed_records:
        return 0.0
    return sum(1 for r in parsed_records if r["parsed_ok"]) / len(parsed_records)


def eval_topk_hit(samples: list, parsed_records: list, k_list=(5, 10)) -> dict:
    """Top-k 命中率（同日 cross-sectional：候选池 top-k 中真实 top-k 命中率）
    简化版：以 pct 字段对同日样本排序，对真实 top-k 的命中率。
    """
    # 按日期分组（input 中的"日期"字段）
    from collections import defaultdict
    groups = defaultdict(list)
    for s, p in zip(samples, parsed_records, strict=False):
        if p["pred_pct"] is None or p["real_pct"] is None:
            continue
        # 提取日期（input JSON 中第一个"日期"字段）
        m = re.search(r'"日期"\s*:\s*"(\d{8})"', s.get("input", ""))
        if not m:
            continue
        d = m.group(1)
        groups[d].append(p)
    hits = {k: 0 for k in k_list}
    total_days = 0
    for _d, recs in groups.items():
        if len(recs) < max(k_list):
            continue
        total_days += 1
        # 真实 top-k 真实涨幅
        sorted_real = sorted(recs, key=lambda x: x["real_pct"] or 0, reverse=True)
        sorted_pred = sorted(recs, key=lambda x: x["pred_pct"] or 0, reverse=True)
        real_topk = {id(r) for r in sorted_real[:max(k_list)]}
        for k in k_list:
            pred_topk = {id(r) for r in sorted_pred[:k]}
            hits[k] += len(real_topk & pred_topk) / k
    if total_days == 0:
        return {f"top{k}_hit_rate": 0.0 for k in k_list}
    return {f"top{k}_hit_rate": hits[k] / total_days for k in k_list}


def eval_backtest(parsed_records: list, threshold: float = 0.5) -> dict:
    """回测：按模型预测方向（仅当 pred_pct > threshold 视为买入信号）模拟日频收益
    返回: sharpe_ratio, annual_return, max_drawdown
    """
    from collections import defaultdict
    daily_pnl = defaultdict(list)
    for r in parsed_records:
        if r["pred_pct"] is None or r["real_pct"] is None:
            continue
        if r["pred_pct"] > threshold:  # 预测涨幅 > threshold 则视为做多
            daily_pnl["_"].append(r["real_pct"] / 100.0)  # 转成收益率
    if not daily_pnl["_"]:
        return {"sharpe_ratio": 0.0, "annual_return": 0.0, "max_drawdown": 0.0}
    rets = np.array(daily_pnl["_"])
    if rets.std() == 0:
        return {"sharpe_ratio": 0.0, "annual_return": 0.0, "max_drawdown": 0.0}
    sharpe = float(rets.mean() / rets.std() * np.sqrt(252))
    annual_ret = float((1 + rets.mean()) ** 252 - 1)
    cum = np.cumprod(1 + rets)
    peak = np.maximum.accumulate(cum)
    dd = (cum - peak) / peak
    max_dd = float(dd.min())
    return {"sharpe_ratio": sharpe, "annual_return": annual_ret, "max_drawdown": max_dd}


# === 主流程 =================================================================
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ckpt", type=str, default=str(DEFAULT_CKPT),
                        help=f"LoRA checkpoint 路径（默认：{DEFAULT_CKPT}）")
    parser.add_argument("--base", type=str, default=BASE_MODEL_HF,
                        help="HF base model id 或本地路径")
    parser.add_argument("--limit", type=int, default=0,
                        help="抽样 N 条（0=全部）")
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--no-backtest", action="store_true",
                        help="跳过回测（仅算方向/top-k/格式）")
    parser.add_argument("--threshold", type=float, default=0.5,
                        help="回测开仓阈值（pred_pct > threshold 视为买入）")
    parser.add_argument("--out-prefix", type=str, default=None,
                        help="输出文件前缀（默认：eval_<timestamp>）")
    args = parser.parse_args()

    EVAL_OUT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_prefix = args.out_prefix or f"eval_{ts}"

    print("=" * 70)
    print(f"§14.5 v4 评估  |  ckpt={args.ckpt}")
    print("=" * 70)

    # 1. 加载 test.jsonl
    print("\n[1] 加载 test.jsonl ...")
    samples = []
    with open(V4_TEST, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            samples.append(json.loads(line))
            if args.limit and len(samples) >= args.limit:
                break
    print(f"    共 {len(samples)} 条样本")

    # 2. 加载模型
    print("\n[2] 加载 base + LoRA adapter ...")
    t0 = time.time()
    tok, model = load_model(Path(args.ckpt), args.base)
    print(f"    加载耗时 {time.time() - t0:.1f}s")

    # 3. 方向准确率 + 格式遵循率
    print("\n[3] 方向准确率 + 格式遵循率 ...")
    t0 = time.time()
    direction_acc, parsed_records, parse_fail = eval_direction(tok, model, samples, args.batch_size)
    format_compliance = eval_format_compliance(parsed_records)
    print(f"    方向准确率 = {direction_acc:.4f}  格式遵循率 = {format_compliance:.4f}")
    print(f"    解析失败率 = {parse_fail:.4f}  耗时 {time.time() - t0:.1f}s")

    # 4. Top-k 命中率
    print("\n[4] Top-k 命中率 ...")
    topk_metrics = eval_topk_hit(samples, parsed_records)
    for k, v in topk_metrics.items():
        print(f"    {k} = {v:.4f}")

    # 5. 回测（可选）
    bt = {"sharpe_ratio": 0.0, "annual_return": 0.0, "max_drawdown": 0.0}
    if not args.no_backtest:
        print("\n[5] 回测夏普 ...")
        bt = eval_backtest(parsed_records, threshold=args.threshold)
        print(f"    sharpe={bt['sharpe_ratio']:.3f}  "
              f"年化={bt['annual_return']:.3f}  最大回撤={bt['max_drawdown']:.3f}")
    else:
        print("\n[5] 回测 -- 跳过（--no-backtest）")

    # 6. 聚合 + 输出
    metrics = EvalMetrics(
        n_samples=len(samples),
        test_loss=0.0,  # 如需 test loss 可加 DataCollator 走 Trainer.evaluate
        direction_accuracy=direction_acc,
        top5_hit_rate=topk_metrics.get("top5_hit_rate", 0.0),
        top10_hit_rate=topk_metrics.get("top10_hit_rate", 0.0),
        sharpe_ratio=bt["sharpe_ratio"],
        annual_return=bt["annual_return"],
        max_drawdown=bt["max_drawdown"],
        format_compliance_rate=format_compliance,
        parse_fail_rate=parse_fail,
        elapsed_sec=time.time() - t0,
        timestamp=ts,
    )

    # JSON
    metrics_file = EVAL_OUT_DIR / f"metrics_{out_prefix}.json"
    with open(metrics_file, "w", encoding="utf-8") as f:
        json.dump(asdict(metrics), f, ensure_ascii=False, indent=2)
    print(f"\n[JSON] {metrics_file}")

    # Markdown 报告
    report_file = EVAL_OUT_DIR / f"report_{out_prefix}.md"
    with open(report_file, "w", encoding="utf-8") as f:
        f.write(f"""# qwen3-14b-tdx v4 评估报告（{ts}）

## 配置
- Checkpoint: `{args.ckpt}`
- Base model: `{args.base}`
- 样本数: **{metrics.n_samples}**

## 核心指标

| 指标 | 值 |
|---|---|
| 方向准确率 | **{metrics.direction_accuracy:.4f}** |
| Top-5 命中率 | {metrics.top5_hit_rate:.4f} |
| Top-10 命中率 | {metrics.top10_hit_rate:.4f} |
| 回测夏普（年化） | **{metrics.sharpe_ratio:.3f}** |
| 年化收益 | {metrics.annual_return:.3f} |
| 最大回撤 | {metrics.max_drawdown:.3f} |
| alpaca 格式遵循率 | {metrics.format_compliance_rate:.4f} |
| 解析失败率 | {metrics.parse_fail_rate:.4f} |

## 解读阈值（建议）

- 方向准确率 ≥ 0.55 → 模型具备基础方向判断能力
- Top-10 命中率 ≥ 0.30 → 候选池筛选有效
- 夏普比率 ≥ 1.0 → 模拟交易有正向 alpha
- 格式遵循率 ≥ 0.95 → 输出可直接下游消费

## 下一步

1. 若所有阈值通过 → 模型可上线，进 `tdxfeed.cli predict` 子命令
2. 若方向/夏普不达标 → 评估是否需要 DPO（§14.7）或重训（启用 packing + torch.compile）
3. 若格式遵循率不达标 → 抽 100 条人工检查 input/output 错位
4. Top-k/夏普低 → 数据层面：检查 test split 是否时序连续（§6.3 时序切分）
""")
    print(f"[MD  ] {report_file}")
    print("\n" + "=" * 70)
    print("评估完成")
    print("=" * 70)


if __name__ == "__main__":
    main()
