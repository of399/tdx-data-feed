#!/usr/bin/env bash
# run_f6.sh — #6 新LLM + 方案E 回测 编排
# 等待 #5 全量评测结束 -> 量化 v7r f16->q8 -> 起 Ollama -> 生成 v4/v7r 方向信号 -> 方案E 过滤对比
set -uo pipefail
cd /home/jiuben/tdx-data-feed
LOG=logs/f6_pipeline.log
exec > >(tee -a "$LOG") 2>&1

echo "[f6] $(date) 启动编排"

echo "[f6] 等待 #5 评测进程(eval_v4_vs_v7r)结束..."
while pgrep -f eval_v4_vs_v7r >/dev/null 2>&1; do sleep 60; done
echo "[f6] $(date) #5 已结束"

# 兜底释放显存（#5 期间 ollama 应已停）
pkill -9 -f "ollama serve" 2>/dev/null || true
pkill -9 -f "snap/ollama" 2>/dev/null || true
sleep 3

# 量化 v7r f16 -> q8_0（若缺失）
V7R_F16=/home/jiuben/models/Qwen3-14B-v7r-merged-f16.gguf
V7R_Q8=/home/jiuben/models/Qwen3-14B-v7r-merged-q8_0.gguf
LQ=/snap/ollama/137/lib/ollama/llama-quantize
if [ ! -f "$V7R_Q8" ]; then
  echo "[f6] 量化 v7r f16 -> q8_0 ..."
  "$LQ" "$V7R_F16" "$V7R_Q8" q8_0 || { echo "[f6] 量化失败，退出"; exit 1; }
fi

# v7r-q8 Modelfile
cat > /home/jiuben/models/Modelfile-v7r-q8 <<EOF
FROM $V7R_Q8
PARAMETER stop "<|im_end|>"
PARAMETER temperature 0.0
PARAMETER num_ctx 4096
PARAMETER repeat_penalty 1
EOF

# 启动 ollama（setsid 脱离，避免被脚本退出信号杀死）
echo "[f6] 启动 ollama ..."
setsid ollama serve > logs/ollama_f6.log 2>&1 < /dev/null &
sleep 15

# 创建两个 Ollama 模型：去掉 `|| true` 吞错；失败后置 WARN（脚本设计为不中断流水线）
for m in "qwen3-14b-tdx-v4:/home/jiuben/models/Modelfile-v4" "qwen3-14b-tdx-v7r-q8:/home/jiuben/models/Modelfile-v7r-q8"; do
  mname="${m%%:*}"; mf="${m##*:}"
  echo "[f6] ollama create $mname ..."
  ollama create "$mname" -f "$mf" 2>&1 | tail -5
  if ! ollama list 2>/dev/null | grep -q "$mname"; then
    echo "[f6] ⚠ WARN: ollama create 未生成模型 $mname（后续信号可能 0 覆盖）"
  fi
done

# 预热 v4（触发模型加载）
echo "[f6] 预热 v4 ..."
curl -s -X POST http://127.0.0.1:11434/api/chat -H 'Content-Type: application/json' \
  -d '{"model":"qwen3-14b-tdx-v4","messages":[{"role":"user","content":"你好"}],"stream":false}' >/dev/null || true

echo "[f6] 生成 v4 信号（约1661只A股翻倍事件）..."
venv/bin/python scripts/llm_signal_plan_e.py gen --model qwen3-14b-tdx-v4 \
  --out data/plan_e/llm_sig_v4.csv --resume 2>&1 | tail -3

echo "[f6] 生成 v7r 信号 ..."
venv/bin/python scripts/llm_signal_plan_e.py gen --model qwen3-14b-tdx-v7r-q8 \
  --out data/plan_e/llm_sig_v7r.csv --resume 2>&1 | tail -3

# ============== #6 收尾：推送评测 / 回测报告到 Vault ==============
# 同时支持两种策略：
#   Option A：把报告正文追加到 StockVault 指定笔记（带时间戳 + --- 分隔标记）
#   Option B：把报告（最新一份）复制到 ObsidianVaults（即 sync-to-obsidian.sh 同源库，立即可见）
# 失败处理：所有步骤加 guard，失败仅告警并 continue，绝不中断流水线（脚本未开 set -e）。
push_reports_to_vault() {
  local f6_report="${1:-}"                       # #6 回测报告 md 路径
  local ts_human="$(date '+%Y-%m-%d %H:%M:%S')"
  local OBS_VAULT="/home/jiuben/ObsidianVaults/tdx-data-feed"
  local STOCK_VAULT="/home/jiuben/StockVault"
  local REPORT_DIR_OBS="$OBS_VAULT/08-报告"
  local STOCK_INDEX="$STOCK_VAULT/04-回测/f6_llm_signal_runs.md"

  # 1) 定位 #5 评测报告（最新 compare_*.json），派生可读 md（eval 只落盘 JSON）
  local f5_json f5_report=""
  f5_json="$(ls -t training/eval/compare_*.json 2>/dev/null | head -1)"
  if [ -n "$f5_json" ]; then
    f5_report="training/eval/compare_$(date +%Y%m%d_%H%M%S).md"
    if venv/bin/python - "$f5_json" "$f5_report" <<'PY'
import sys, json, datetime
src, dst = sys.argv[1], sys.argv[2]
d = json.load(open(src, encoding="utf-8"))
a, b = d.get("a", {}), d.get("b", {})
L = [
    f"# #5 评测对比报告（派生自 {src.split('/')[-1]}）", "",
    f"> 生成时间：{datetime.datetime.now():%Y-%m-%d %H:%M:%S} ｜ 来源：training/eval 流水线", "",
    f"| 指标 | {a.get('name','A')} | {b.get('name','B')} | Δ |",
    "| --- | --- | --- | --- |",
    f"| 方向准确率 | {a.get('direction_accuracy',0):.4f} | {b.get('direction_accuracy',0):.4f} | {d.get('delta_dir_acc',0):+.4f} |",
    f"| 格式遵循率 | {a.get('format_compliance_rate',0):.4f} | {b.get('format_compliance_rate',0):.4f} | {d.get('delta_fmt',0):+.4f} |",
    f"| 解析失败率 | {a.get('parse_fail_rate',0):.4f} | {b.get('parse_fail_rate',0):.4f} | — |",
    f"| 样本数 | {a.get('n_samples',0)} | {b.get('n_samples',0)} | — |",
]
open(dst, "w", encoding="utf-8").write("\n".join(L) + "\n")
print("[f6-vault] 派生 #5 报告:", dst)
PY
    then
      echo "[f6-vault] #5 报告已派生: $f5_report"
    else
      echo "[f6-vault] WARN #5 报告派生失败，跳过 #5 推送"
      f5_report=""
    fi
  else
    echo "[f6-vault] WARN 未找到 #5 compare_*.json，跳过 #5 推送"
  fi

  # 2) 逐个报告推送（Option B 复制 + Option A 追加）
  local src title dst_name base hash8
  for src in "$f5_report" "$f6_report"; do
    [ -z "$src" ] && continue
    [ -f "$src" ] || { echo "[f6-vault] WARN 报告不存在，跳过: $src"; continue; }
    case "$src" in
      *llm_signal_compare*) title="#6 方案E 回测对比报告"; dst_name="06-方案E回测对比报告.md" ;;
      *compare_*) title="#5 评测对比报告"; dst_name="05-评测对比报告.md" ;;
      *) title="F6 报告"; dst_name="$(basename "$src")" ;;
    esac
    base="$(basename "$src")"
    hash8="$(sha256sum "$src" | cut -c1-8)"

    # Option B —— 复制到 ObsidianVaults（与 sync-to-obsidian.sh 同源库，固定名=最新视图）
    if [ -d "$OBS_VAULT" ]; then
      mkdir -p "$REPORT_DIR_OBS"
      if { cat <<FRM
---
title: $title
source: $src
source_hash: $hash8
synced_at: $(date +%Y-%m-%d)
tags:
  - tdx-data-feed
  - f6-report
---
FRM
          cat "$src"; } > "$REPORT_DIR_OBS/$dst_name"; then
        echo "[f6-vault] OK 已同步到 ObsidianVaults: $REPORT_DIR_OBS/$dst_name"
      else
        echo "[f6-vault] WARN 复制到 ObsidianVaults 失败: $dst_name"
      fi
    else
      echo "[f6-vault] WARN ObsidianVaults 不存在，跳过复制: $OBS_VAULT"
    fi

    # Option A —— 追加到 StockVault 指定笔记（时间戳 + --- 分隔标记，便于多次运行归档）
    if [ -d "$STOCK_VAULT" ]; then
      mkdir -p "$(dirname "$STOCK_INDEX")"
      if [ ! -f "$STOCK_INDEX" ]; then
        { echo "# #6 LLM + 方案E 回测 运行归档"; echo; echo "> 由 run_f6.sh 自动追加，勿手改。"; echo; } > "$STOCK_INDEX"
      fi
      if {
        echo ""
        echo "---"
        echo ""
        echo "## $title — $ts_human"
        echo ""
        echo "> 源文件: \`$src\` ｜ hash: $hash8"
        echo ""
        cat "$src"
      } >> "$STOCK_INDEX"; then
        echo "[f6-vault] OK 已追加到 StockVault: $STOCK_INDEX"
      else
        echo "[f6-vault] WARN 追加到 StockVault 失败: $STOCK_INDEX"
      fi
    else
      echo "[f6-vault] WARN StockVault 不存在，跳过追加: $STOCK_VAULT"
    fi
  done
}

echo "[f6] 方案E 过滤对比 ..."
F6_TS="$(date +%Y%m%d_%H%M%S)"
F6_REPORT="data/plan_e/llm_signal_compare_${F6_TS}.md"
venv/bin/python scripts/llm_signal_plan_e.py compare \
  --v4-csv data/plan_e/llm_sig_v4.csv --v7r-csv data/plan_e/llm_sig_v7r.csv \
  | tee "$F6_REPORT"
echo "[f6] 回测报告已落盘: $F6_REPORT"

echo "[f6] $(date) 推送评测/回测报告到 Vault ..."
push_reports_to_vault "$F6_REPORT"

echo "[f6] $(date) 完成；Ollama 仍在运行（v4 / v7r-q8 可用）"
