#!/usr/bin/env bash
# 校验数学笔记库：源（单一事实源 etc/obsidian/math）与部署 vault 之间是否一致。
# 只读比对，不改盘。全一致 exit 0，否则 exit 1（可挂 CI / make verify-math）。
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC_DIR="$REPO_ROOT/etc/obsidian/math"
SRC_TPL="$SRC_DIR/templates"
SRC_MOC="$SRC_DIR/MOC/总览.md"

VAULT="${HOME}/ObsidianVaults/math-study"
PREFIX=""
TPL_REL="templates/math"

usage() {
  sed -n '2,8p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  exit 0
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --vault)      VAULT="${2:?--vault 需要参数}"; shift 2 ;;
    --vault=*)    VAULT="${1#*=}"; shift ;;
    --prefix)     PREFIX="${2:?--prefix 需要参数}"; shift 2 ;;
    --prefix=*)   PREFIX="${1#*=}"; shift ;;
    --tpl-dir)    TPL_REL="${2:?--tpl-dir 需要参数}"; shift 2 ;;
    --tpl-dir=*)  TPL_REL="${1#*=}"; shift ;;
    -h|--help)    usage ;;
    *) echo "未知参数: $1" >&2; usage; exit 2 ;;
  esac
done

[[ -d "$SRC_TPL" ]] || { echo "模板源目录不存在: $SRC_TPL" >&2; exit 1; }
[[ -f "$SRC_MOC" ]] || { echo "MOC 源文件不存在: $SRC_MOC" >&2; exit 1; }
[[ -d "$VAULT"  ]] || { echo "vault 不存在: $VAULT" >&2; exit 1; }

moc_dst="$VAULT/00_索引/总览.md"
[[ -n "$PREFIX" ]] && moc_dst="${VAULT}/${PREFIX}/00_索引/总览.md"

drift=0
check_one() {
  local src="$1" dst="$2" label="$3"
  if [[ ! -f "$dst" ]]; then
    echo "[缺失] $label -> $dst"
    drift=1
    return
  fi
  if cmp -s "$src" "$dst"; then
    echo "[一致] $label"
  else
    echo "[漂移] $label -> $dst"
    drift=1
  fi
}

echo "源: ${SRC_DIR#$REPO_ROOT/}"
echo "目标 vault: $VAULT"
[[ -n "$PREFIX" ]] && echo "根前缀: $PREFIX"
echo "模板目录: $TPL_REL"
echo "---"

for f in "$SRC_TPL"/T-*.md; do
  [[ -e "$f" ]] || continue
  check_one "$f" "$VAULT/$TPL_REL/$(basename "$f")" "模板 $(basename "$f")"
done

check_one "$SRC_MOC" "$moc_dst" "总览 MOC"

echo "---"
if [[ $drift -eq 0 ]]; then
  echo "✓ 全部一致（源 <-> 部署）"
  exit 0
else
  echo "✗ 存在不一致，请先跑 install-math-templates.sh 同步"
  exit 1
fi
