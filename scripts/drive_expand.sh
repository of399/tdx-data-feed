#!/bin/bash
exec > /home/jiuben/tdx-data-feed/data/distill/drive_expand.log 2>&1
set -x
export PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
cd /home/jiuben/tdx-data-feed
echo "[expand] 等待 daily 达 10000 只 $(date)"
while [ $(ls data/parquet/daily/ | wc -l) -lt 10000 ]; do sleep 60; done
echo "[expand] daily 就绪 $(ls data/parquet/daily/ | wc -l) 只 $(date)"
# 每只 2 窗口（多窗口，平衡保真度与蒸馏耗时）
venv/bin/python scripts/build_distill_skeleton.py 2
echo "[expand] 骨架完成 $(wc -l < data/distill/skeleton.jsonl) 条"
# 清旧 v1 残留，生成 v2 多窗口集
rm -f data/distill/full.jsonl
NUM_PREDICT=90 venv/bin/python scripts/batch_distill.py --batch 8
echo "[expand] 蒸馏完成 $(wc -l < data/distill/full.jsonl) 条"
venv/bin/python scripts/convert_distill.py --name stock_10k_v2
echo "[expand] 转换完成"
bash /home/workbuddy/lf_project/run_train_10k_v2.sh
echo "[expand] 全部完成 $(date)"
