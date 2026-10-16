#!/usr/bin/env bash
# install-math-moc.sh
# 同步数学笔记库"知识库总览" MOC（etc/obsidian/math/MOC/总览.md）到目标 vault。
#
# 单一事实源：etc/obsidian/math/MOC/总览.md
# 默认同步到两 vault：math-study（独立 vault）+ StockVault（嵌入 05-知识库/数学/）。
# 已存在文件覆盖前会备份为 <name>.bak-<ts>（不删除用户数据）。
# 幂等：内容一致时跳过。
#
# 与 install-math-templates.sh 的区别：
#   - 本脚本只同步 MOC（一个文件），轻量、可独立调
#   - 默认双 vault；旧脚本需手动调 2 次
#   - 旧脚本的 MOC 段保留为 backward-compat（同步同一文件）
#
# 用法：
#   bash scripts/install-math-moc.sh                              # 双 vault 同步
#   bash scripts/install-math-moc.sh --vault PATH                 # 单 vault 同步
#   bash scripts/install-math-moc.sh --vault PATH --prefix "05-知识库/数学"  # 嵌入子库
#   bash scripts/install-math-moc.sh --dry-run                    # 预演
#   bash scripts/install-math-moc.sh --force                      # 覆盖时不留 .bak
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC_MOC="$REPO_ROOT/etc/obsidian/math/MOC/总览.md"

# 默认双 vault
MATH_STUDY_VAULT="${MATH_STUDY_VAULT:-$HOME/ObsidianVaults/math-study}"
STOCKVAULT_VAULT="${STOCKVAULT_VAULT:-$HOME/StockVault}"
STOCKVAULT_PREFIX="${STOCKVAULT_PREFIX:-05-知识库/数学}"

VAULT_ONLY=""
PREFIX=""
DRY_RUN=0
FORCE=0
TS="$(date +%Y%m%d-%H%M%S)"

log()  { printf '[math-moc] %s\n' "$*"; }
warn() { printf '[math-moc][WARN] %s\n' "$*" >&2; }
err()  { printf '[math-moc][ERROR] %s\n' "$*" >&2; }

usage() {
  sed -n '2,18p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  cat <<USAGE

环境变量：MATH_STUDY_VAULT, STOCKVAULT_VAULT, STOCKVAULT_PREFIX
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --vault)        VAULT_ONLY="${2:?--vault 需要参数}"; shift 2 ;;
    --vault=*)      VAULT_ONLY="${1#*=}"; shift ;;
    --prefix)       PREFIX="${2:?--prefix 需要参数}"; shift 2 ;;
    --prefix=*)     PREFIX="${1#*=}"; shift ;;
    --dry-run)      DRY_RUN=1; shift ;;
    --force)        FORCE=1; shift ;;
    -h|--help)      usage ;;
    *) err "未知参数: $1"; usage; exit 2 ;;
  esac
done

[[ -f "$SRC_MOC" ]] || { err "MOC 源不存在: $SRC_MOC"; exit 1; }

copy_file() {
  local src="$1" dst="$2" label="$3"
  if [[ -f "$dst" ]] && cmp -s "$src" "$dst"; then
    log "跳过（内容一致）: $label"
    return 0
  fi
  if [[ $DRY_RUN -eq 1 ]]; then
    log "[dry-run] 将写入: $dst"
    return 0
  fi
  if [[ -f "$dst" && $FORCE -eq 0 ]]; then
    local bak="${dst}.bak-${TS}"
    cp -p "$dst" "$bak"
    log "已备份原文件: ${bak#${dst%/*}/}"
  fi
  mkdir -p "$(dirname "$dst")"
  cp -p "$src" "$dst"
  log "已写入: $dst"
}

sync_to_vault() {
  local vault="$1" prefix="$2" label="$3"
  local dst="$vault/00_索引/总览.md"
  [[ -n "$prefix" ]] && dst="${vault}/${prefix}/00_索引/总览.md"

  if [[ ! -d "$vault" ]]; then
    warn "vault 不存在，跳过 [$label]: $vault"
    return 0
  fi
  log "[$label] 目标: $dst"
  copy_file "$SRC_MOC" "$dst" "$label 总览 MOC"
}

main() {
  log "源: ${SRC_MOC#${REPO_ROOT}/}"
  log "时间戳: $TS  DRY_RUN: $DRY_RUN  FORCE: $FORCE"

  if [[ -n "$VAULT_ONLY" ]]; then
    log "模式: 单 vault"
    sync_to_vault "$VAULT_ONLY" "$PREFIX" "单 vault"
  else
    log "模式: 双 vault 同步（默认）"
    sync_to_vault "$MATH_STUDY_VAULT" "" "math-study"
    sync_to_vault "$STOCKVAULT_VAULT" "$STOCKVAULT_PREFIX" "StockVault"
  fi

  log "完成。"
  log "下一步："
  log "  1. 在 Obsidian 打开 vault，让 Dataview 重新渲染（Cmd/Ctrl+R 或重新打开）"
  log "  2. 校对：math-study 与 StockVault 两份总览内容应一致"
}

main "$@"
