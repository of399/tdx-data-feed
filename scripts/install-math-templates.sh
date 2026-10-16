#!/usr/bin/env bash
# 安装数学笔记库模板到 Obsidian vault。
#
# 单一事实源：etc/obsidian/math/{templates,MOC}
# 目标 vault 为生成副本；已存在的文件在覆盖前会备份为 <name>.bak-<ts>（不删除用户数据）。
#
# 用法：
#   bash scripts/install-math-templates.sh [选项]
#
#   --vault PATH        目标 vault（默认 $HOME/ObsidianVaults/math-study）
#   --prefix PATH       数学笔记在 vault 内的根前缀（默认空，即 vault 根）
#                       例：--prefix "05-知识库/数学" 适配已有编号体系的库
#   --tpl-dir PATH      模板目录（相对 vault，默认 templates/math）
#                       例：--tpl-dir "06-模板/math"
#   --dry-run           只打印计划，不写盘
#   --force             覆盖已存在文件时不保留 .bak 备份（不推荐）
#   --init-config       初始化 vault 的 .obsidian 插件配置（社区插件清单 + dataview + templater）
#   --patch-templater   把「目录 → 模板」映射写入 Templater 插件真正的 data.json（会先备份）
#   --copy-plugin-bins  把已下载的插件本体从 --bins-src 复制到目标 vault（免 UI 安装）
#   --bins-src PATH     插件本体来源目录（默认 $HOME/ObsidianVaults/math-study/.obsidian/plugins）
#   -h, --help          帮助
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC_DIR="$REPO_ROOT/etc/obsidian/math"
SRC_TPL="$SRC_DIR/templates"
SRC_MOC="$SRC_DIR/MOC/总览.md"

DEFAULT_VAULT="${HOME}/ObsidianVaults/math-study"
VAULT="$DEFAULT_VAULT"
PREFIX=""
TPL_REL="templates/math"
DRY_RUN=0
FORCE=0
PATCH_TEMPLATER=0
INIT_CONFIG=0
COPY_BINS=0
BINS_SRC="${HOME}/ObsidianVaults/math-study/.obsidian/plugins"

# 数学库所需的最小插件集（插件二进制仍须在 Obsidian UI 首次安装，JSON 仅控制"启用"）
# 注意：插件目录名必须等于 manifest id，Spaced Repetition 的 id 是 obsidian-spaced-repetition
MATH_PLUGINS='["dataview", "obsidian-spaced-repetition", "templater-obsidian"]'

TS="$(date +%Y%m%d-%H%M%S)"

# 目录骨架：领域目录按项目 ROI 排序（概率统计/优化/数值分析优先于实分析）
SCAFFOLD_DIRS=(
  "00_索引"
  "10_实分析"
  "11_复分析"
  "12_傅里叶分析"
  "13_泛函分析"
  "14_概率统计"
  "15_优化理论"
  "16_数值分析"
  "90_开放问题"
)

# Templater folder_templates 映射（领域目录 -> 模板相对 vault 路径）
# 条目形如 "10_实分析|T-定义.md"，前缀与模板目录在运行时拼接
FOLDER_MAP=(
  "10_实分析|T-定义.md"
  "11_复分析|T-定义.md"
  "12_傅里叶分析|T-定理.md"
  "13_泛函分析|T-定义.md"
  "14_概率统计|T-定义.md"
  "15_优化理论|T-定理.md"
  "16_数值分析|T-定义.md"
  "90_开放问题|T-开放问题.md"
)

log()  { printf '[math-tpl] %s\n' "$*"; }
warn() { printf '[math-tpl][WARN] %s\n' "$*" >&2; }
err()  { printf '[math-tpl][ERROR] %s\n' "$*" >&2; }

usage() {
  sed -n '2,26p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  exit 0
}

# 把 "<目录>|<模板文件名>" 拼成实际路径对，前缀为空时保持原样
render_map() {
  local line folder tpl
  for line in "${FOLDER_MAP[@]}"; do
    folder="${line%%|*}"
    tpl="${line##*|}"
    if [[ -n "$PREFIX" ]]; then
      folder="${PREFIX}/${folder}"
    fi
    printf '%s|%s/%s\n' "$folder" "$TPL_REL" "$tpl"
  done
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --vault)             VAULT="${2:?--vault 需要参数}"; shift 2 ;;
    --vault=*)           VAULT="${1#*=}"; shift ;;
    --prefix)            PREFIX="${2:?--prefix 需要参数}"; shift 2 ;;
    --prefix=*)          PREFIX="${1#*=}"; shift ;;
    --tpl-dir)           TPL_REL="${2:?--tpl-dir 需要参数}"; shift 2 ;;
    --tpl-dir=*)         TPL_REL="${1#*=}"; shift ;;
    --bins-src)          BINS_SRC="${2:?--bins-src 需要参数}"; shift 2 ;;
    --bins-src=*)        BINS_SRC="${1#*=}"; shift ;;
    --dry-run)           DRY_RUN=1; shift ;;
    --force)             FORCE=1; shift ;;
    --init-config)       INIT_CONFIG=1; shift ;;
    --patch-templater)   PATCH_TEMPLATER=1; shift ;;
    --copy-plugin-bins)  COPY_BINS=1; shift ;;
    -h|--help)           usage ;;
    *) err "未知参数: $1"; usage; exit 2 ;;
  esac
done

[[ -d "$SRC_TPL" ]] || { err "模板源目录不存在: $SRC_TPL"; exit 1; }
[[ -f "$SRC_MOC" ]] || { err "MOC 源文件不存在: $SRC_MOC"; exit 1; }

# ---------- 复制单文件（覆盖前备份，不删用户数据） ----------
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
    log "已备份原文件: ${bak#$VAULT/}"
  fi
  cp -p "$src" "$dst"
  log "已写入: ${dst#$VAULT/}"
}

# 列出目标 vault 已安装插件的 manifest id（目录名可能与 id 不同，如 Templater 目录为 templater）
installed_plugin_ids() {
  local d="$VAULT/.obsidian/plugins"
  [[ -d "$d" ]] || return 0
  python3 - "$d" <<'PY' 2>/dev/null || true
import glob, json, os, sys

root = sys.argv[1]
for mf in glob.glob(os.path.join(root, "*", "manifest.json")):
    try:
        with open(mf, encoding="utf-8") as fh:
            mid = json.load(fh).get("id")
    except Exception:
        continue
    if mid:
        print(mid)
PY
}

# ---------- 复制插件本体（免 UI 安装） ----------
copy_plugin_bins() {
  local plugins=() p
  while IFS= read -r p; do
    plugins+=("$p")
  done < <(printf '%s' "$MATH_PLUGINS" | python3 -c 'import json,sys; [print(x) for x in json.load(sys.stdin)]' 2>/dev/null || true)

  if [[ ${#plugins[@]} -eq 0 ]]; then
    warn "无法解析插件清单，跳过本体复制"
    return 0
  fi

  local have
  have="$(installed_plugin_ids)"

  for p in "${plugins[@]}"; do
    if printf '%s\n' "$have" | grep -qx "$p"; then
      log "插件已安装（按 manifest id 判定），跳过本体复制: $p"
      continue
    fi
    local src="$BINS_SRC/$p"
    local dst="$VAULT/.obsidian/plugins/$p"
    if [[ ! -d "$src" ]]; then
      warn "来源插件不存在，跳过: $src"
      continue
    fi
    if [[ -d "$dst" ]]; then
      log "插件本体已存在，跳过: .obsidian/plugins/$p"
      continue
    fi
    if [[ $DRY_RUN -eq 1 ]]; then
      log "[dry-run] 将复制插件本体: $src -> $dst"
      continue
    fi
    mkdir -p "$dst"
    cp -r "$src"/. "$dst"/
    log "已安装插件本体: .obsidian/plugins/$p"
  done
}

ensure_dir() {
  local d="$1"
  if [[ -n "$PREFIX" ]]; then
    d="${PREFIX}/${d}"
  fi
  if [[ $DRY_RUN -eq 1 ]]; then
    log "[dry-run] 将确保目录: $d"
  else
    mkdir -p "$VAULT/$d"
    log "目录就绪: $d"
  fi
}

# 模板目录始终相对 vault 根，不受 --prefix 影响
ensure_tpl_dir() {
  if [[ $DRY_RUN -eq 1 ]]; then
    log "[dry-run] 将确保目录: $TPL_REL"
  else
    mkdir -p "$VAULT/$TPL_REL"
    log "目录就绪: $TPL_REL"
  fi
}

main() {
  log "源: ${SRC_DIR#$REPO_ROOT/}"
  log "目标 vault: $VAULT"
  [[ -n "$PREFIX" ]] && log "根前缀: $PREFIX"
  log "模板目录: $TPL_REL"

  # 共库风险提示
  if [[ "$VAULT" == *"tdx-data-feed"* ]]; then
    warn "目标是项目 vault：请勿把数学笔记放进 00-知识库入口/ 与 08-报告/（sync-to-obsidian.sh 会 cp 覆盖）。"
    warn "两套内容靠标签隔离：项目文档 #tdx-data-feed，数学卡 #math-notes。"
  fi

  if [[ $DRY_RUN -eq 0 && ! -d "$VAULT" ]]; then
    log "vault 不存在，创建: $VAULT"
    mkdir -p "$VAULT"
    log "首次使用请在 Obsidian 中执行 Open folder as vault 并选择该目录。"
  fi

  # 1. 目录骨架
  for d in "${SCAFFOLD_DIRS[@]}"; do
    ensure_dir "$d"
  done
  ensure_tpl_dir

  # 2. 模板
  local n=0
  for f in "$SRC_TPL"/T-*.md; do
    [[ -e "$f" ]] || continue
    copy_file "$f" "$VAULT/$TPL_REL/$(basename "$f")" "模板 $(basename "$f")"
    n=$((n + 1))
  done
  log "模板数量: $n"

  # 3. MOC
  local moc_dst="$VAULT/00_索引/总览.md"
  [[ -n "$PREFIX" ]] && moc_dst="${VAULT}/${PREFIX}/00_索引/总览.md"
  copy_file "$SRC_MOC" "$moc_dst" "总览 MOC"

  # 4. 可选：复制插件本体
  if [[ $COPY_BINS -eq 1 ]]; then
    copy_plugin_bins
  fi

  # 5. 可选：初始化 .obsidian 插件配置
  if [[ $INIT_CONFIG -eq 1 ]]; then
    init_config
  fi

  # 6. 可选：写 Templater folder_templates
  if [[ $PATCH_TEMPLATER -eq 1 || $INIT_CONFIG -eq 1 ]]; then
    patch_templater
  fi

  log "完成。下一步：关闭安全模式 → 重启 Obsidian → 启用 Dataview + Spaced Repetition + Templater（不是 Flashcards）。"
  log "Templater 还需在 UI 打开「Trigger Templater on new file creation」（该开关存在 localStorage，脚本写不了）。"
  log "字段约定见 etc/obsidian/math/README.md"
}

init_config() {
  local od="$VAULT/.obsidian"
  local plugins_file="$od/community-plugins.json"
  local templates_folder
  templates_folder="$(dirname "$TPL_REL")"

  if [[ $DRY_RUN -eq 1 ]]; then
    log "[dry-run] 将初始化 .obsidian 插件配置"
    return 0
  fi
  mkdir -p "$od"

  # 社区插件清单：与已有条目取并集，不覆盖用户已装的插件
  if command -v python3 >/dev/null 2>&1; then
    if [[ -f "$plugins_file" ]]; then
      cp -p "$plugins_file" "${plugins_file}.bak-${TS}"
      log "已备份: .obsidian/community-plugins.json.bak-${TS}"
    fi
    MATH_PLUGINS_ENV="$MATH_PLUGINS" PLUGINS_FILE="$plugins_file" PLUGINS_DIR="$od/plugins" python3 - <<'PY'
import glob, json, os

path = os.environ["PLUGINS_FILE"]
want = json.loads(os.environ["MATH_PLUGINS_ENV"])
plugins_dir = os.environ["PLUGINS_DIR"]

# 已安装插件的 manifest id（目录名可能与 id 不同，如 Templater 目录为 templater）
installed = {}
if os.path.isdir(plugins_dir):
    for mf in glob.glob(os.path.join(plugins_dir, "*", "manifest.json")):
        try:
            with open(mf, encoding="utf-8") as fh:
                mid = json.load(fh).get("id")
        except Exception:
            continue
        if mid:
            installed[mid] = os.path.basename(os.path.dirname(mf))

have = []
if os.path.exists(path):
    try:
        with open(path, encoding="utf-8") as fh:
            have = json.load(fh)
        if not isinstance(have, list):
            have = []
    except json.JSONDecodeError:
        have = []

add = []
for w in want:
    if w in installed:
        # 目录名可能与 id 不同（Templater 目录为 templater），登记必须用目录名
        dirname = installed[w]
        if dirname in have:
            print(f"已存在等价插件且清单已登记（目录 {dirname}），跳过: {w}")
        else:
            add.append(dirname)
            print(f"本体已装但清单未登记，补登记目录名: {dirname} (id={w})")
    else:
        add.append(w)

merged = list(dict.fromkeys(have + add))
with open(path, "w", encoding="utf-8") as fh:
    json.dump(merged, fh, ensure_ascii=False, indent=2)
    fh.write("\n")
print("community-plugins: " + ", ".join(merged))
PY
  else
    [[ -f "$plugins_file" ]] && cp -p "$plugins_file" "${plugins_file}.bak-${TS}"
    printf '%s\n' "$MATH_PLUGINS" > "$plugins_file"
    log "已写入 .obsidian/community-plugins.json（无 python3，未做并集合并）"
  fi

  # Dataview 配置：沿用项目库那份（enableJs=false）
  local dv="$od/dataview.json"
  if [[ -f "$dv" ]]; then
    log "已存在，跳过: .obsidian/dataview.json"
  else
    cp -p "$REPO_ROOT/etc/obsidian/dataview.json" "$dv"
    log "已写入: .obsidian/dataview.json"
  fi

  # Templater 设置：v2 只读 .obsidian/plugins/<dir>/data.json。
  # 旧版写 .obsidian/templater.json（v1 时代的 wrapper 格式），v2 完全忽略 → 别再落假配置。
  # 实际写入见 patch_templater()。
  log "Templater 设置由 --patch-templater 写入 .obsidian/plugins/<templater>/data.json（templates_folder=${templates_folder}）"
  log "提醒：JSON 只控制\"启用\"，插件本体仍需 --copy-plugin-bins 或在 Obsidian → 第三方插件 → 浏览中安装一次。"
}

# 定位 Templater 插件目录：Obsidian 的插件数据固定写在 plugins/<目录名>/data.json，
# 而目录名不一定等于 manifest id（本库实测：目录 templater / id templater-obsidian）。
templater_plugin_dir() {
  local pdir="$VAULT/.obsidian/plugins"
  [[ -d "$pdir" ]] || return 1
  python3 - "$pdir" <<'PY' 2>/dev/null || true
import glob, json, os, sys

root = sys.argv[1]
for mf in sorted(glob.glob(os.path.join(root, "*", "manifest.json"))):
    try:
        mid = json.load(open(mf, encoding="utf-8")).get("id")
    except Exception:
        continue
    if mid == "templater-obsidian":
        print(os.path.dirname(mf))
        break
PY
}

patch_templater() {
  if ! command -v python3 >/dev/null 2>&1; then
    warn "未找到 python3，跳过 --patch-templater"
    return 0
  fi

  # 用换行分隔传给 python，避免引号地狱
  local map templates_folder
  map="$(render_map)"
  templates_folder="$(dirname "$TPL_REL")"

  local tdir cfg
  tdir="$(templater_plugin_dir)"
  if [[ -z "$tdir" ]]; then
    warn "未找到 Templater 插件（plugins/*/manifest.json 中 id=templater-obsidian 缺失）"
    warn "请先 --copy-plugin-bins，或在 Obsidian → 第三方插件 里装一次 Templater，再重跑本脚本"
    return 0
  fi
  cfg="$tdir/data.json"

  if [[ $DRY_RUN -eq 1 ]]; then
    log "[dry-run] 将写入 folder_templates 到 ${cfg#$VAULT/}"
    printf '%s\n' "$map" | sed 's/^/[dry-run]   /'
    return 0
  fi

  if [[ -f "$cfg" ]]; then
    cp -p "$cfg" "${cfg}.bak-${TS}"
    log "已备份: ${cfg#$VAULT/}.bak-${TS}"
  fi

  FOLDER_MAP_ENV="$map" TEMPLATER_CFG="$cfg" TEMPLATES_FOLDER="$templates_folder" python3 - <<'PY'
import json, os

cfg_path = os.environ["TEMPLATER_CFG"]
raw = os.environ["FOLDER_MAP_ENV"].strip().splitlines()

entries = []
for line in raw:
    if not line.strip():
        continue
    folder, tpl = line.split("|", 1)
    entries.append({"folder": folder, "template": tpl})

cfg = {}
if os.path.exists(cfg_path):
    try:
        with open(cfg_path, encoding="utf-8") as fh:
            cfg = json.load(fh)
    except json.JSONDecodeError:
        cfg = {}

# Templater 2.x 的 schema：设置直接躺在 data.json 顶层，没有 v1 的 "version"/"settings" 包装
cfg["data_version"] = 2
cfg["templates_folder"] = os.environ["TEMPLATES_FOLDER"]
cfg["folder_templates"] = entries
# "none" | "folder" | "regex"：只有 "folder" 才会套用目录模板
cfg["trigger_on_file_creation_mode"] = "folder"
# 清掉 v1 遗留键（插件自身的迁移逻辑也是删这几个）
for k in ("trigger_on_file_creation", "enable_folder_templates",
          "enable_file_templates", "enable_system_commands"):
    cfg.pop(k, None)

with open(cfg_path, "w", encoding="utf-8") as fh:
    json.dump(cfg, fh, ensure_ascii=False, indent=2)
    fh.write("\n")

print(f"folder_templates 已写入 {len(entries)} 条映射 -> {os.path.basename(os.path.dirname(cfg_path))}/data.json")
PY
  log "Templater 设置已更新（templates_folder=${templates_folder}，trigger_on_file_creation_mode=folder）"
  warn "注意：总开关 trigger_on_file_creation 存在 Obsidian 的 localStorage（templater-local-settings），脚本无法写。"
  warn "请在 设置 → Templater → 打开「Trigger Templater on new file creation」并重启 Obsidian 后生效。"
}

main "$@"
