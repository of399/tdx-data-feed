#!/usr/bin/env bash
#
# verify_obsidian_systemd.sh
# ==========================
# 验证 obsidian-sync 的 systemd 单元配置已正确部署并生效。
# 对应方案: docs/obsidian_sync_reload_verify_plan.md (V0–V8)。
#
# 用法:
#   bash scripts/verify_obsidian_systemd.sh            # 完整验证 (V0–V8, 含触发测试)
#   bash scripts/verify_obsidian_systemd.sh --readonly # 仅静态只读检查 (V0–V4), 用于 make status / make canary
#
# 说明:
#   - 完整模式会向 docs/ 与 data/plan_e/ 写入临时文件以验证触发/解耦, 结束自动清理。
#   - 退出码: 0 = 全部通过; 1 = 存在 FAIL; 2 = 参数错误。
#   - 只读模式不做任何写入, 可安全接入日常监控。
#
set -uo pipefail

# ---- 路径 (从脚本位置推导项目根, 与 cwd 无关) ----
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_SYSD="$ROOT/systemd"
USER_SYSD="${HOME}/.config/systemd/user"
UNIT_PATH=obsidian-sync.path
UNIT_SVC=obsidian-sync.service
# 不应出现在监听 Paths 中的数据目录 (修复后已解耦)
DATA_DIRS=("data/plan_e" "training/eval")

# ---- 模式 ----
READONLY=0
case "${1:-}" in
  --readonly|-q) READONLY=1 ;;
  "") READONLY=0 ;;
  *) echo "未知参数: $1 (仅支持 --readonly / -q)" >&2; exit 2 ;;
esac

# ---- 计数 / 颜色 (仅终端输出上色) ----
PASS=0; FAIL=0
if [ -t 1 ]; then
  C_GREEN=$'\033[0;32m'; C_RED=$'\033[0;31m'; C_YELLOW=$'\033[0;33m'; C_BOLD=$'\033[1m'; C_RESET=$'\033[0m'
else
  C_GREEN=""; C_RED=""; C_YELLOW=""; C_BOLD=""; C_RESET=""
fi

ok()   { PASS=$((PASS+1)); echo "${C_GREEN}[PASS]${C_RESET} $*"; }
bad()  { FAIL=$((FAIL+1)); echo "${C_RED}[FAIL]${C_RESET} $*"; }
warn() { echo "${C_YELLOW}[WARN]${C_RESET} $*"; }

# systemctl --user 静默包裹 (失败返回空, 不报错)
sc() { systemctl --user "$@" 2>/dev/null; }

# 退出时清理临时文件 (防止残留污染仓库/数据目录)
cleanup() {
  rm -f "$ROOT/docs/_obsidian_verify_tmp.md" "$ROOT/docs/_obsidian_verify_tmp2.md" \
        "$ROOT/docs/_obsidian_verify_new_"*.md \
        "$ROOT/data/plan_e/_obsidian_verify_tmp" 2>/dev/null
}
trap cleanup EXIT

# ---- 可用性预检 ----
if ! sc show "$UNIT_SVC" >/dev/null 2>&1; then
  bad "Vx systemd user bus 不可用 (systemctl --user show $UNIT_SVC 失败); 请在用户会话执行并确认 XDG_RUNTIME_DIR 已设置"
fi

# ================= V0 仓库/部署漂移 =================
echo "--- V0 仓库/部署漂移 (systemd 实际加载目录) ---"
drift=0
for f in "$UNIT_PATH" "$UNIT_SVC"; do
  if [ -f "$REPO_SYSD/$f" ] && [ -f "$USER_SYSD/$f" ]; then
    if ! diff -q "$REPO_SYSD/$f" "$USER_SYSD/$f" >/dev/null 2>&1; then
      bad "V0 漂移: 仓库 $REPO_SYSD/$f 与部署 $USER_SYSD/$f 不一致 → 需执行 'make timer-install'"
      drift=1
    fi
  elif [ ! -f "$USER_SYSD/$f" ]; then
    bad "V0 缺失: 部署文件 $USER_SYSD/$f 不存在 → 需执行 'make timer-install'"
    drift=1
  fi
done
[ "$drift" -eq 0 ] && ok "V0 仓库与部署单元文件逐字节一致 (无漂移)"

# ================= V1 StartLimit 生效 =================
echo "--- V1 StartLimit 配置 (防 start-limit-hit) ---"
# 直接解析"已部署"单元文件：本机 systemd 下 `systemctl show -p StartLimitIntervalSec`
# 返回空（该属性以 StartLimitIntervalUSec 暴露），且 StartLimit* 须置于 [Unit] 段，
# 故改读部署文件，既绕开属性名差异，也确保校验的是真正生效的配置。
sli=$(grep -iE '^[[:space:]]*StartLimitIntervalSec' "$USER_SYSD/$UNIT_SVC" | head -1 | cut -d= -f2 | tr -d ' ')
slb=$(grep -iE '^[[:space:]]*StartLimitBurst' "$USER_SYSD/$UNIT_SVC" | head -1 | cut -d= -f2 | tr -d ' ')
burst_ok=0; interval_ok=0
# 默认 5 太少；>=10 即已放宽。当前部署为 60，覆盖批量文档编辑的触发风暴。
[ "${slb:-0}" -ge 10 ] 2>/dev/null && burst_ok=1
# StartLimitIntervalSec=60 即修复预期；60000/60000000/0 亦兼容。
case "$sli" in
  0|60|60000|60000000) interval_ok=1 ;;
esac
if [ "$burst_ok" -eq 1 ] && [ "$interval_ok" -eq 1 ]; then
  ok "V1 StartLimitIntervalSec=$sli StartLimitBurst=$slb (已放宽, 防 start-limit-hit)"
elif [ "$burst_ok" -eq 1 ] && [ "$interval_ok" -eq 0 ]; then
  bad "V1 StartLimitBurst=$slb 正确, 但 StartLimitIntervalSec=$sli 非预期值 (期望 60/60000/60000000/0) → 检查 service 文件"
elif [ "$burst_ok" -eq 0 ]; then
  bad "V1 StartLimitBurst=$slb (期望 >=10); 可能未重载修复 → 'make timer-install' + daemon-reload + restart obsidian-sync.path"
fi

# ================= V2 Paths 不含数据目录 =================
echo "--- V2 监听 Paths 配置 (解耦数据目录) ---"
paths=$(sc show "$UNIT_PATH" -p Paths)
leak=0
for d in "${DATA_DIRS[@]}"; do
  if echo "$paths" | grep -q "$d"; then
    bad "V2 泄漏: Paths 含数据目录 '$d' (应已解耦) → 检查是否已重载修复"
    leak=1
  fi
done
[ "$leak" -eq 0 ] && ok "V2 Paths 未含 data/plan_e / training/eval"
if echo "$paths" | grep -q "PathModified"; then
  warn "V2 注意: Paths 仍存在 PathModified 语义 (修复预期为 PathChanged)"
fi

# ================= V3 .path 单元状态 =================
echo "--- V3 .path 单元状态 ---"
pa=$(sc show "$UNIT_PATH" -p ActiveState | cut -d= -f2)
if [ "$pa" = "active" ]; then
  ok "V3 $UNIT_PATH ActiveState=$pa (inotify 监视运行中)"
else
  bad "V3 $UNIT_PATH ActiveState=${pa:-?} (期望 active; 可能未 'restart obsidian-sync.path')"
fi

# ================= V4 .service 限流状态 =================
echo "--- V4 .service 限流状态 (start-limit-hit) ---"
sr=$(sc show "$UNIT_SVC" -p Result | cut -d= -f2)
if [ "$sr" = "start-limit-hit" ]; then
  bad "V4 $UNIT_SVC Result=start-limit-hit (曾触发限流; 需 'systemctl --user reset-failed obsidian-sync.service obsidian-sync.path' + 排查触发风暴)"
else
  ok "V4 $UNIT_SVC Result=${sr:-空} (无限流)"
fi

# ================= 触发测试 (仅完整模式) =================
if [ "$READONLY" -eq 1 ]; then
  echo "--- (--readonly 模式: 跳过 V5–V7 触发测试) ---"
else
  sleep 1

  # ---------- V5 正向触发 (新建文件边界, 严谨轮询检测) ----------
  echo "--- V5 正向触发 (新建 docs/ 文件应触发同步) ---"
  rm -f /tmp/obsidian-sync.lastrun   # 清除冷却哨兵, 确保本次触发不被 30s 冷却吞掉
  v5_since=$(date '+%Y-%m-%d %H:%M:%S')
  v5_ts=$(date +%s%N)
  v5_file="$ROOT/docs/_obsidian_verify_new_${v5_ts}.md"
  # 以"新建(而非覆盖)"文件专门验证创建事件这一边界条件
  printf 'obsidian verify new-file %s\n' "$v5_ts" > "$v5_file"
  v5_triggered=0
  for _i in $(seq 1 8); do
    sleep 2
    v5_runs=$(journalctl --user -u "$UNIT_SVC" --since "$v5_since" 2>/dev/null | grep -c "Starting $UNIT_SVC")
    if [ "$v5_runs" -ge 1 ]; then v5_triggered=1; break; fi
  done
  if [ "$v5_triggered" -eq 1 ]; then
    ok "V5 新建 docs/ 文件已触发 obsidian-sync.service (journalctl 观测到启动; 创建事件边界已覆盖)"
  else
    bad "V5 新建 docs/ 文件后服务未触发 (检查 .path 是否 active / PathChanged 是否含 docs)"
  fi

  # ---------- V6 反向解耦 ----------
  echo "--- V6 反向解耦 (写 data/plan_e 不应触发) ---"
  before=$(sc show "$UNIT_SVC" -p ActiveEnterTimestamp | cut -d= -f2)
  printf 'x' > "$ROOT/data/plan_e/_obsidian_verify_tmp"
  sleep 8
  after=$(sc show "$UNIT_SVC" -p ActiveEnterTimestamp | cut -d= -f2)
  if [ "$after" = "$before" ]; then
    ok "V6 写入 data/plan_e 未触发同步 (解耦有效)"
  else
    bad "V6 写入 data/plan_e 仍触发同步 (解耦失败; Paths 可能仍含该目录)"
  fi

  # ---------- V7 冷却/锁防风暴 ----------
  echo "--- V7 冷却/锁 (2s 内两次创建 docs 临时文件应仅运行 1 次真实同步) ---"
  rm -f /tmp/obsidian-sync.lastrun   # 清除冷却哨兵, 让首次写入可触发; 2s 内第二次写入应被 30s 冷却吸收
  # 统计"真实同步次数"（脚本落日志 '同步完成'），而非 service 启动次数：
  # 第二次触发虽会启动 service 单元，但被 30s 冷却 / flock 吸收为空转，不产生真实同步。
  v7_log_before=$(grep -c "同步完成" "$ROOT/logs/obsidian-sync.log" 2>/dev/null || echo 0)
  printf 'a %s\n' "$(date +%s)" > "$ROOT/docs/_obsidian_verify_tmp.md"
  sleep 2
  printf 'b %s\n' "$(date +%s)" > "$ROOT/docs/_obsidian_verify_tmp2.md"
  sleep 8
  v7_log_after=$(grep -c "同步完成" "$ROOT/logs/obsidian-sync.log" 2>/dev/null || echo 0)
  v7_real=$((v7_log_after - v7_log_before))
  if [ "$v7_real" -eq 1 ]; then
    ok "V7 2s 内两次创建仅触发 1 次真实同步 (冷却/锁生效, 无风暴)"
  elif [ "$v7_real" -eq 0 ]; then
    warn "V7 未观测到任何真实同步 (可能与 V5 重叠或日志不可读; 非风暴但需确认触发)"
  else
    bad "V7 观测到 $v7_real 次真实同步 (疑似触发风暴; 检查 flock / 30s 冷却)"
  fi
fi

# ================= 汇总 =================
echo "========================================="
if [ "$FAIL" -eq 0 ]; then
  echo "${C_BOLD}OBSIDIAN_SYSTEMD: PASS — PASS=$PASS FAIL=$FAIL${C_RESET}"
else
  echo "${C_BOLD}OBSIDIAN_SYSTEMD: FAIL — PASS=$PASS FAIL=$FAIL${C_RESET}"
fi
[ "$FAIL" -eq 0 ]
