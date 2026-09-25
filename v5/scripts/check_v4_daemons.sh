#!/bin/bash
# v4 批量分析明日检查脚本
# 早上跑一下就看到两个 daemon 进度 + vault 状态

echo "===== v4 批量分析状态报告 ====="
echo "  时间: $(date)"
echo

# 1. 进程状态
echo "[1] 进程状态"
for pid in 198712 199178; do
  if ps -p $pid > /dev/null 2>&1; then
    elapsed=$(ps -p $pid -o etime= | xargs)
    echo "  ✓ PID $pid 运行中 ($elapsed)"
  else
    echo "  ✗ PID $pid 已停"
  fi
done
echo

# 2. daemon 进度
echo "[2] daemon 进度（v4_batch A股过滤版）"
if [ -f /tmp/v4-ashare-daemon.log ]; then
  tail -5 /tmp/v4-ashare-daemon.log
  latest=$(grep -oE "\[[ 0-9]+/4629\]" /tmp/v4-ashare-daemon.log | tail -1)
  echo "  最新进度: $latest"
else
  echo "  日志不存在"
fi
echo

# 3. 历史回溯进度
echo "[3] 历史回溯进度（30只×25年）"
if [ -f /tmp/v4-historical.log ]; then
  tail -10 /tmp/v4-historical.log
else
  echo "  日志不存在"
fi
echo

# 4. vault v4 段统计
echo "[4] vault v4 段统计"
tech=$(find /home/jiuben/StockVault/01-标的 -name "*.md" ! -name "*MOC*" -exec grep -l "v4技术面分析" {} \; 2>/dev/null | wc -l)
hist=$(find /home/jiuben/StockVault/01-标的 -name "*.md" ! -name "*MOC*" -exec grep -l "v4历史回溯" {} \; 2>/dev/null | wc -l)
total=$(find /home/jiuben/StockVault/01-标的 -name "*.md" ! -name "*MOC*" | wc -l)
echo "  vault 标的总: $total 个"
echo "  v4技术面段: $tech 个"
echo "  v4历史回溯段: $hist 个"
echo

# 5. JSONL 报告
echo "[5] JSONL 报告"
ls -la /home/jiuben/tdx-data-feed/v5/reports/v4-*.jsonl 2>/dev/null | tail -5
total_size=$(du -sh /home/jiuben/tdx-data-feed/v5/reports/v4-*.jsonl 2>/dev/null | tail -1 | awk '{print $1}')
echo "  总大小: ${total_size:-0}"
echo

# 6. 估计剩余时间
echo "[6] 估计完成度"
if [ -f /tmp/v4-ashare-daemon.log ]; then
  done=$(grep -oE "\[[ 0-9]+/4629\]" /tmp/v4-ashare-daemon.log | tail -1 | grep -oE "[0-9]+" | head -1)
  if [ -n "$done" ]; then
    pct=$(echo "scale=1; $done * 100 / 4629" | bc)
    echo "  daemon: $done / 4629 = ${pct}%"
  fi
fi

echo
echo "===== 检查完毕 ====="