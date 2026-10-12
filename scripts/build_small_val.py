#!/usr/bin/env python3
"""build_small_val.py — 从大验证集分层抽样小验证集，加速 periodic eval。

用法:
  venv/bin/python scripts/build_small_val.py \
      --src data/v4fwd/val.jsonl \
      --dst data/v4fwd/val_small.jsonl \
      --n 800 --seed 42 [--label-key label]

说明:
  - 默认随机子抽样 N 条（seed 固定，可复现）。
  - 若记录含 --label-key 指定的字段（如 "label"），则按该字段分层，
    尽量保持各类比例，避免小验证集偏斜导致评估失真。
  - 仅生成「快速监控」用小验证集；eval 只监控、不改权重，不影响训练归因。
"""
from __future__ import annotations

import argparse
import json
import random
from collections import Counter
from pathlib import Path


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="从大验证集分层抽样小验证集")
    p.add_argument("--src", default="data/v4fwd/val.jsonl", help="源验证集 jsonl")
    p.add_argument("--dst", default="data/v4fwd/val_small.jsonl", help="输出小验证集")
    p.add_argument("--n", type=int, default=800, help="目标条数")
    p.add_argument("--seed", type=int, default=42, help="随机种子")
    p.add_argument(
        "--label-key",
        default="label",
        help="用于分层抽样的字段名（记录无此字段则退化为纯随机抽样）",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()
    src = Path(args.src)
    assert src.is_file(), f"源文件不存在: {src}"

    rows = [json.loads(l) for l in src.read_text(encoding="utf-8").splitlines() if l.strip()]
    total = len(rows)
    print(f"源验证集: {src} ({total} 条)")

    rng = random.Random(args.seed)
    n = min(args.n, total)

    has_label = args.label_key in rows[0]
    if not has_label:
        print(f"⚠ 记录无 '{args.label_key}' 字段，退化为纯随机抽样")
        sampled = rng.sample(rows, n)
    else:
        by_label: dict[str, list[int]] = {}
        for i, r in enumerate(rows):
            by_label.setdefault(str(r.get(args.label_key)), []).append(i)
        sampled_idx: list[int] = []
        for lb, idxs in by_label.items():
            k = max(1, round(len(idxs) / total * n))
            k = min(k, len(idxs))
            sampled_idx.extend(rng.sample(idxs, k))
        if len(sampled_idx) < n:
            remain = [i for i in range(total) if i not in sampled_idx]
            sampled_idx.extend(rng.sample(remain, min(n - len(sampled_idx), len(remain))))
        else:
            sampled_idx = rng.sample(sampled_idx, n)
        sampled = [rows[i] for i in sampled_idx]
        dist = Counter(str(r.get(args.label_key)) for r in sampled)
        print(f"分层抽样分布: {dict(dist)}")

    # 保持原顺序（按源索引排序），便于 diff / 复现
    sampled.sort(key=lambda r: rows.index(r))

    dst = Path(args.dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in sampled) + "\n",
        encoding="utf-8",
    )
    print(f"✓ 小验证集: {dst} ({len(sampled)} 条, 占比 {len(sampled) / total:.1%})")


if __name__ == "__main__":
    main()
