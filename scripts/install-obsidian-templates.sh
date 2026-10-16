#!/usr/bin/env bash
# install-obsidian-templates.sh (v1)
# 部署 Obsidian 模板 + 配置到目标 vault。
# 覆盖：
#   1. 数学 7 模板 + MOC（etc/obsidian/math → vault/06-模板/math/ + 00_索引/总览.md）
#   2. 非数学 5 模板（etc/obsidian/templates → vault/06-模板/）
#   3. 配置 JSON：
#      - 共享: obsidian-git.json, dataview.json（etc/obsidian/ → vault/.obsidian/）
#      - 分 vault: community-plugins/templater/periodic-notes（etc/obsidian/<kind>/ → vault/.obsidian/）
# 覆盖前备份为 <file>.bak-<时间戳>，不删原数据。幂等。
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# 源
SRC_MATH_TPL="$REPO_ROOT/etc/obsidian/math/templates"
SRC_MATH_MOC="$REPO_ROOT/etc/obsidian/math/MOC/总览.md"
SRC_NONMATH_DIR="$REPO_ROOT/etc/obsidian/templates"
SRC_SHARED_DIR="$REPO_ROOT/etc/obsidian"

# 默认
VAULT="${VAULT:-/home/jiuben/StockVault}"
VAULT_KIND="${VAULT_KIND:-}"
PREFIX="${PREFIX:-}"
MATH_TPL_REL="${MATH_TPL_REL:-06-模板/math}"
NONMATH_TPL_REL="${NONMATH_TPL_REL:-06-模板}"
DRY_RUN=0
CLEANUP_ORPHANS=0

NONMATH_TPLS=(00-MOC.md 个股研究模板.md 回测实验模板.md 日记模板.md v4技术面分析模板.md)

usage() {
  sed -n '2,12p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  cat <<USAGE

用法：
  $(basename "$0") --vault-kind {stockvault|project} [--vault PATH] [其他选项]

必选：
  --vault-kind KIND          stockvault 或 project

可选：
  --vault PATH               目标 vault（默认 /home/jiuben/StockVault）
  --prefix PATH              数学 MOC 根前缀
  --math-tpl-dir REL         数学模板目录（相对 vault）
  --nonmath-tpl-dir REL      非数学模板目录（相对 vault）
  --dry-run                  预演，不写盘
  -h, --help

环境变量：VAULT, VAULT_KIND, PREFIX, MATH_TPL_REL, NONMATH_TPL_REL
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --vault-kind)        VAULT_KIND="${2:?}"; shift 2 ;;
    --vault-kind=*)      VAULT_KIND="${1#*=}"; shift ;;
    --vault)             VAULT="${2:?}"; shift 2 ;;
    --vault=*)           VAULT="${1#*=}"; shift ;;
    --prefix)            PREFIX="${2:?}"; shift 2 ;;
    --prefix=*)          PREFIX="${1#*=}"; shift ;;
    --math-tpl-dir)      MATH_TPL_REL="${2:?}"; shift 2 ;;
    --math-tpl-dir=*)    MATH_TPL_REL="${1#*=}"; shift ;;
    --nonmath-tpl-dir)   NONMATH_TPL_REL="${2:?}"; shift 2 ;;
    --nonmath-tpl-dir=*) NONMATH_TPL_REL="${1#*=}"; shift ;;
    --dry-run)           DRY_RUN=1; shift ;;
    --cleanup-orphans)   CLEANUP_ORPHANS=1; shift ;;
    -h|--help)           usage; exit 0 ;;
    *) echo "未知参数: $1" >&2; usage; exit 2 ;;
  esac
done

# 启发式推断 kind
if [[ -z "$VAULT_KIND" ]]; then
  case "$VAULT" in
    *StockVault*)                VAULT_KIND="stockvault" ;;
    *ObsidianVaults/tdx-data-feed*|*tdx-data-feed*) VAULT_KIND="project" ;;
    *) echo "✗ 缺 --vault-kind 且无法从 VAULT 推断" >&2; exit 2 ;;
  esac
fi
case "$VAULT_KIND" in stockvault|project) ;; *) echo "✗ 非法 kind: $VAULT_KIND" >&2; exit 2 ;; esac
SRC_KIND_DIR="$REPO_ROOT/etc/obsidian/$VAULT_KIND"

# 前置检查
[[ -d "$VAULT" ]]            || { echo "✗ vault 不存在: $VAULT" >&2; exit 1; }
[[ -d "$SRC_MATH_TPL" ]]     || { echo "✗ 数学模板源不存在" >&2; exit 1; }
[[ -f "$SRC_MATH_MOC" ]]     || { echo "✗ 数学 MOC 源不存在" >&2; exit 1; }
[[ -d "$SRC_NONMATH_DIR" ]]  || { echo "✗ 非数学模板源不存在" >&2; exit 1; }
[[ -d "$SRC_KIND_DIR" ]]     || { echo "✗ 分 vault 源不存在: $SRC_KIND_DIR" >&2; exit 1; }

TS="$(date +%Y%m%d-%H%M%S)"
total=0
backups=0

copy_file() {
  local src="$1" dst="$2" label="$3"
  if [[ ! -f "$src" ]]; then
    echo "[源缺失] $label"; return 1
  fi
  if [[ $DRY_RUN -eq 1 ]]; then
    echo "[dry-run] $label"
    echo "           $src -> $dst"
    total=$((total+1)); return 0
  fi
  mkdir -p "$(dirname "$dst")"
  # 若目标存在且内容不同，先备份
  if [[ -f "$dst" ]] && ! cmp -s "$src" "$dst"; then
    cp "$dst" "$dst.bak-$TS"
    backups=$((backups+1))
  fi
  cp "$src" "$dst"
  echo "[写入] $label"
  total=$((total+1))
}

echo "=== install-obsidian-templates v1 ==="
echo "vault: $VAULT  kind: $VAULT_KIND  prefix: ${PREFIX:-<root>}"
echo "TS: $TS  DRY_RUN: $DRY_RUN"
echo "---"

# 数学段仅 stockvault（项目 vault 是 repo-doc 镜像，不应有数学模板）
if [[ "$VAULT_KIND" == "stockvault" ]]; then
  # 1. 数学 7 模板
  for f in "$SRC_MATH_TPL"/T-*.md; do
    [[ -e "$f" ]] || continue
    copy_file "$f" "$VAULT/$MATH_TPL_REL/$(basename "$f")" "数学模板 $(basename "$f")"
  done

  # 2. 数学 MOC
  moc_dst="$VAULT/00_索引/总览.md"
  [[ -n "$PREFIX" ]] && moc_dst="${VAULT}/${PREFIX}/00_索引/总览.md"
  copy_file "$SRC_MATH_MOC" "$moc_dst" "数学总览 MOC"
else
  echo "[跳过] 数学段（kind=$VAULT_KIND，project vault 不含数学子库）"
fi

# 3. 非数学 5 模板
for tpl in "${NONMATH_TPLS[@]}"; do
  src="$SRC_NONMATH_DIR/$tpl"
  [[ -f "$src" ]] || continue
  copy_file "$src" "$VAULT/$NONMATH_TPL_REL/$tpl" "非数学模板 $tpl"
done

# 4. 共享 + 分 vault JSON
for j in obsidian-git dataview; do
  copy_file "$SRC_SHARED_DIR/$j.json" "$VAULT/.obsidian/$j.json" "配置 $j.json (shared)"
done
for j in community-plugins templater periodic-notes; do
  copy_file "$SRC_KIND_DIR/$j.json" "$VAULT/.obsidian/$j.json" "配置 $j.json ($VAULT_KIND)"
done

echo "---"

# 5. 清理孤立文件（仅 project vault：删除不应存在的数学模板/MOC）
if [[ $CLEANUP_ORPHANS -eq 1 && "$VAULT_KIND" == "project" ]]; then
  echo "[cleanup] 扫描 project vault 孤立数学文件..."
  local_orphans=0
  for f in "$VAULT/06-模板/math"/T-*.md; do
    [[ -e "$f" ]] || continue
    if [[ $DRY_RUN -eq 1 ]]; then
      echo "[dry-run cleanup] $f"
    else
      rm "$f"; echo "[清理] $f"
    fi
    local_orphans=$((local_orphans+1))
  done
  # 数学 MOC
  moc_dst="$VAULT/00_索引/总览.md"
  [[ -n "$PREFIX" ]] && moc_dst="${VAULT}/${PREFIX}/00_索引/总览.md"
  if [[ -f "$moc_dst" ]]; then
    if [[ $DRY_RUN -eq 1 ]]; then
      echo "[dry-run cleanup] $moc_dst"
    else
      rm "$moc_dst"; echo "[清理] $moc_dst"
    fi
    local_orphans=$((local_orphans+1))
  fi
  # 尝试清空数学目录
  math_dir="$VAULT/06-模板/math"
  if [[ -d "$math_dir" ]] && [[ -z "$(ls -A "$math_dir" 2>/dev/null)" ]]; then
    if [[ $DRY_RUN -eq 1 ]]; then
      echo "[dry-run cleanup] rmdir $math_dir"
    else
      rmdir "$math_dir" 2>/dev/null && echo "[清理] rmdir $math_dir" || true
    fi
  fi
  idx_dir="$VAULT/00_索引"
  [[ -n "$PREFIX" ]] && idx_dir="${VAULT}/${PREFIX}/00_索引"
  if [[ -d "$idx_dir" ]] && [[ -z "$(ls -A "$idx_dir" 2>/dev/null)" ]]; then
    if [[ $DRY_RUN -eq 1 ]]; then
      echo "[dry-run cleanup] rmdir $idx_dir"
    else
      rmdir "$idx_dir" 2>/dev/null && echo "[清理] rmdir $idx_dir" || true
    fi
  fi
  echo "[cleanup] 共 $local_orphans 个孤立文件/目录被处理"
fi

if [[ $DRY_RUN -eq 1 ]]; then
  echo "[dry-run 完成] 共 $total 个文件将写入；$backups 个将触发 .bak 备份"
else
  echo "✓ install 完成：共 $total 个文件写入；$backups 个触发 .bak-$TS 备份"
fi
