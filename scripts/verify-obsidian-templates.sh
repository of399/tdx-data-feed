#!/usr/bin/env bash
# verify-obsidian-templates.sh (v2.1)
# 校验 Obsidian 库：源（单一事实源 etc/obsidian）↔ 部署 vault 零漂移。
# v2.1 变更：支持 --vault-kind {stockvault,project}，配置 JSON 源按 vault-kind 分流
# 覆盖：
#   1. 数学 7 模板 + 总览 MOC（etc/obsidian/math）
#   2. 非数学 5 模板（etc/obsidian/templates）
#   3. 5 份配置 JSON：
#      - 共享：etc/obsidian/{obsidian-git,dataview}.json
#      - 分 vault：etc/obsidian/<kind>/{community-plugins,templater,periodic-notes}.json
# 只读比对，不改盘。全一致 exit 0，否则 exit 1。
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# 源：数学 / 非数学（不变）
SRC_MATH_DIR="$REPO_ROOT/etc/obsidian/math"
SRC_MATH_TPL="$SRC_MATH_DIR/templates"
SRC_MATH_MOC="$SRC_MATH_DIR/MOC/总览.md"
SRC_NONMATH_DIR="$REPO_ROOT/etc/obsidian/templates"

# 源：配置 JSON（v2.1 拆 共享 + 分 vault）
SRC_SHARED_DIR="$REPO_ROOT/etc/obsidian"          # obsidian-git.json, dataview.json
SRC_KIND_DIR=""                                  # 按 --vault-kind 选 stockvault 或 project

# 非数学 5 模板白名单
NONMATH_TPLS=(00-MOC.md 个股研究模板.md 回测实验模板.md 日记模板.md v4技术面分析模板.md)

# 默认值
VAULT="${VAULT:-/home/jiuben/StockVault}"
VAULT_KIND="${VAULT_KIND:-}"      # 必填（--vault-kind 或启发式）
PREFIX="${PREFIX:-}"
MATH_TPL_REL="${MATH_TPL_REL:-06-模板/math}"
NONMATH_TPL_REL="${NONMATH_TPL_REL:-06-模板}"

usage() {
  sed -n '2,12p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  cat <<USAGE

用法：
  $(basename "$0") --vault-kind {stockvault|project} [--vault PATH] [其他选项]

必选：
  --vault-kind KIND          vault 角色：stockvault 或 project（决定 per-vault 配置源路径）

可选：
  --vault PATH               目标 vault 根路径（默认 /home/jiuben/StockVault）
  --prefix PATH              vault 内的根前缀（数学 MOC 路径会拼到 PREFIX/00_索引/总览.md）
  --math-tpl-dir REL         数学模板目录（相对 vault，默认 06-模板/math）
  --nonmath-tpl-dir REL      非数学模板目录（相对 vault，默认 06-模板）
  -h, --help                 显示本帮助

环境变量（与命令行等价）：
  VAULT, VAULT_KIND, PREFIX, MATH_TPL_REL, NONMATH_TPL_REL

退出码：
  0 = 全部一致
  1 = 存在漂移/缺失
  2 = 参数错误/依赖缺失
USAGE
}

# 解析参数
while [[ $# -gt 0 ]]; do
  case "$1" in
    --vault-kind)            VAULT_KIND="${2:?--vault-kind 需要参数}"; shift 2 ;;
    --vault-kind=*)          VAULT_KIND="${1#*=}"; shift ;;
    --vault)                 VAULT="${2:?--vault 需要参数}"; shift 2 ;;
    --vault=*)               VAULT="${1#*=}"; shift ;;
    --prefix)                PREFIX="${2:?--prefix 需要参数}"; shift 2 ;;
    --prefix=*)              PREFIX="${1#*=}"; shift ;;
    --math-tpl-dir)          MATH_TPL_REL="${2:?--math-tpl-dir 需要参数}"; shift 2 ;;
    --math-tpl-dir=*)        MATH_TPL_REL="${1#*=}"; shift ;;
    --nonmath-tpl-dir)       NONMATH_TPL_REL="${2:?--nonmath-tpl-dir 需要参数}"; shift 2 ;;
    --nonmath-tpl-dir=*)     NONMATH_TPL_REL="${1#*=}"; shift ;;
    -h|--help)               usage; exit 0 ;;
    *) echo "未知参数: $1" >&2; usage; exit 2 ;;
  esac
done

# 启发式：路径含 StockVault/ObsidianVaults 自动推断 kind
if [[ -z "$VAULT_KIND" ]]; then
  case "$VAULT" in
    *StockVault*)                VAULT_KIND="stockvault" ;;
    *ObsidianVaults/tdx-data-feed*|*tdx-data-feed*) VAULT_KIND="project" ;;
    *) echo "✗ 缺 --vault-kind，且无法从 VAULT 路径推断" >&2; usage; exit 2 ;;
  esac
fi

case "$VAULT_KIND" in
  stockvault|project) ;;
  *) echo "✗ 非法 --vault-kind: $VAULT_KIND（仅 stockvault|project）" >&2; exit 2 ;;
esac
SRC_KIND_DIR="$REPO_ROOT/etc/obsidian/$VAULT_KIND"

# 依赖检查
for cmd in cmp find jq; do
  command -v "$cmd" >/dev/null 2>&1 || { echo "✗ 缺少依赖: $cmd" >&2; exit 1; }
done

# 前置检查
[[ -d "$SRC_MATH_TPL" ]]      || { echo "✗ 数学模板源不存在: $SRC_MATH_TPL" >&2; exit 1; }
[[ -f "$SRC_MATH_MOC" ]]      || { echo "✗ 数学 MOC 源不存在: $SRC_MATH_MOC" >&2; exit 1; }
[[ -d "$SRC_NONMATH_DIR" ]]   || { echo "✗ 非数学模板源不存在: $SRC_NONMATH_DIR" >&2; exit 1; }
[[ -d "$SRC_KIND_DIR" ]]      || { echo "✗ 分 vault 源不存在: $SRC_KIND_DIR" >&2; exit 1; }
[[ -d "$VAULT" ]]             || { echo "✗ vault 不存在: $VAULT" >&2; exit 1; }

drift=0
check_one() {
  local src="$1" dst="$2" label="$3"
  if [[ ! -f "$dst" ]]; then
    echo "[缺失] $label -> $dst"
    drift=1; return
  fi
  if cmp -s "$src" "$dst"; then
    echo "[一致] $label"
  else
    echo "[漂移] $label -> $dst"
    drift=1
  fi
}
check_json() {
  local src="$1" dst="$2" label="$3"
  if [[ ! -f "$src" ]]; then
    echo "[源缺失] 配置 $label -> $src"
    drift=1; return
  fi
  if [[ ! -f "$dst" ]]; then
    echo "[部署缺失] 配置 $label -> $dst"
    drift=1; return
  fi
  if cmp -s <(jq -S . "$src") <(jq -S . "$dst" 2>/dev/null); then
    echo "[一致] 配置 $label"
  else
    echo "[漂移] 配置 $label -> $dst"
    drift=1
  fi
}

echo "源仓库: ${REPO_ROOT#$HOME/}"
echo "vault-kind: $VAULT_KIND"
echo "目标 vault: $VAULT"
[[ -n "$PREFIX" ]] && echo "根前缀: $PREFIX"
echo "数学模板目录: $MATH_TPL_REL"
echo "非数学模板目录: $NONMATH_TPL_REL"
echo "分 vault 源: ${SRC_KIND_DIR#$REPO_ROOT/}"
echo "---"

# 数学段仅 stockvault（项目 vault 不应有数学模板）
if [[ "$VAULT_KIND" == "stockvault" ]]; then
  # 1. 数学 7 模板
  for f in "$SRC_MATH_TPL"/T-*.md; do
    [[ -e "$f" ]] || continue
    check_one "$f" "$VAULT/$MATH_TPL_REL/$(basename "$f")" "数学模板 $(basename "$f")"
  done

  # 2. 数学 MOC
  moc_dst="$VAULT/00_索引/总览.md"
  [[ -n "$PREFIX" ]] && moc_dst="${VAULT}/${PREFIX}/00_索引/总览.md"
  check_one "$SRC_MATH_MOC" "$moc_dst" "数学总览 MOC"
else
  echo "[跳过] 数学段验证（kind=$VAULT_KIND，project vault 不含数学子库）"
fi

# 3. 非数学 5 模板
for tpl in "${NONMATH_TPLS[@]}"; do
  src="$SRC_NONMATH_DIR/$tpl"
  [[ -f "$src" ]] || { echo "[源缺失] 非数学模板 $tpl"; drift=1; continue; }
  check_one "$src" "$VAULT/$NONMATH_TPL_REL/$tpl" "非数学模板 $tpl"
done

# 4. 配置 JSON：分流 共享 vs 分 vault
#    - 共享: obsidian-git.json, dataview.json
#    - 分 vault: community-plugins.json, templater.json, periodic-notes.json
check_json "$SRC_SHARED_DIR/obsidian-git.json"   "$VAULT/.obsidian/obsidian-git.json"   "obsidian-git.json (shared)"
check_json "$SRC_SHARED_DIR/dataview.json"       "$VAULT/.obsidian/dataview.json"       "dataview.json (shared)"
check_json "$SRC_KIND_DIR/community-plugins.json" "$VAULT/.obsidian/community-plugins.json" "community-plugins.json ($VAULT_KIND)"
check_json "$SRC_KIND_DIR/templater.json"        "$VAULT/.obsidian/templater.json"        "templater.json ($VAULT_KIND)"
check_json "$SRC_KIND_DIR/periodic-notes.json"   "$VAULT/.obsidian/periodic-notes.json"   "periodic-notes.json ($VAULT_KIND)"

echo "---"
if [[ $drift -eq 0 ]]; then
  echo "✓ 全部一致（源 <-> 部署）"
  exit 0
else
  echo "✗ 存在不一致（数学/非数学模板 或 配置 JSON），请先跑 install-obsidian-templates.sh 同步"
  exit 1
fi
