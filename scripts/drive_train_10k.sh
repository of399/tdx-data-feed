#!/bin/bash
exec > /home/jiuben/tdx-data-feed/data/distill/drive.log 2>&1
set -x
export PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
cd /home/jiuben/tdx-data-feed
echo "[drive] 启动蒸馏 $(date)"
NUM_PREDICT=90 venv/bin/python scripts/batch_distill.py --batch 8
echo "[drive] 蒸馏结束 $(date)，开始转换数据集"
venv/bin/python scripts/convert_distill.py
echo "[drive] 转换结束 $(date)，开始双卡训练"
bash /home/workbuddy/lf_project/run_train_10k.sh
echo "[drive] 全部完成 $(date)"
