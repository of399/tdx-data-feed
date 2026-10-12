#!/usr/bin/env python3
"""单卡 QLoRA 训练启动器（替代已删除的 /tmp 训练脚本，恢复 make train-* 可复现性）。

用法:
    venv/bin/python training/scripts/train_lora.py training/config/tdx_v5_lora.yaml

等价于：CUDA_VISIBLE_DEVICES=0 venv/bin/llamafactory-cli train <config.yaml>
日志写入 logs/train_<config_stem>.log。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
LLAMAFACTORY_CLI = ROOT / "venv" / "bin" / "llamafactory-cli"
LOG_DIR = ROOT / "logs"


def main() -> int:
    if len(sys.argv) < 2:
        print("用法: train_lora.py <config.yaml>", file=sys.stderr)
        return 2
    cfg = Path(sys.argv[1])
    if not cfg.exists():
        print(f"❌ 配置不存在: {cfg}", file=sys.stderr)
        return 2

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log = LOG_DIR / f"train_{cfg.stem}.log"

    env = dict(os.environ)
    env["CUDA_VISIBLE_DEVICES"] = env.get("CUDA_VISIBLE_DEVICES", "0")

    print(f"▶ 训练 {cfg.name}（单卡 CUDA_VISIBLE_DEVICES={env['CUDA_VISIBLE_DEVICES']}）")
    print(f"  日志: {log}")

    with log.open("wb") as lf:
        rc = subprocess.run(
            [str(LLAMAFACTORY_CLI), "train", str(cfg)],
            cwd=str(ROOT),
            env=env,
            stdout=lf,
            stderr=lf,
        ).returncode

    print(f"  结束 exit={rc}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
