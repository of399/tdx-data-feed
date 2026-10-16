#!/usr/bin/env bash
# 从 GitHub Releases 下载 Obsidian 社区插件到 vault 的 .obsidian/plugins/<id>/。
#
# 背景：etc/obsidian/*.json 只控制"启用"，插件本体（main.js）缺失时配置无效。
# 本脚本补齐插件本体，使 Dataview / Spaced Repetition 等真正可用（无需手点 UI 浏览）。
# 装完仍需在 Obsidian 中「设置 → 第三方插件 → 关闭安全模式」并启用对应插件。
#
# 用法：
#   bash scripts/install-obsidian-plugins.sh [--vault PATH] [--plugins a,b] [--dry-run] [--force]
#
#   --vault PATH     目标 vault（默认 $HOME/ObsidianVaults/math-study）
#   --plugins LIST   逗号分隔的插件 id（默认 dataview,spaced-repetition,templater-obsidian）
#   --retries N      单个资产下载重试次数（默认 3，指数退避 1s/4s/9s）
#   --dry-run        只打印计划
#   --force          已存在时覆盖（默认跳过；缺文件则自动补齐）
#   -h, --help       帮助
#
# 可靠性：断点续传 + 指数退避重试 + 按 release 公布的 size 校验字节数；
# 任一下载失败会删除半截文件并整体判定失败（不留残缺插件目录）。
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VAULT="${HOME}/ObsidianVaults/math-study"
PLUGINS="dataview,obsidian-spaced-repetition,templater-obsidian"
# 已成功安装/跳过的插件 id（以 manifest 为准，可能与传入的 key 不同）
INSTALLED_IDS=()
DRY_RUN=0
FORCE=0
RETRIES=3
TS="$(date +%Y%m%d-%H%M%S)"

# 插件 id -> GitHub repo（仅收录本项目用到的插件）
declare -A PLUGIN_REPOS=(
  [dataview]="blacksmithgu/obsidian-dataview"
  [obsidian-spaced-repetition]="st3v3nmw/obsidian-spaced-repetition"
  [templater-obsidian]="SilentVoid13/Templater"
  [periodic-notes]="liamcain/obsidian-periodic-notes"
  [obsidian-git]="denolehov/obsidian-git"
)

log()  { printf '[obs-plugin] %s\n' "$*"; }
warn() { printf '[obs-plugin][WARN] %s\n' "$*" >&2; }
err()  { printf '[obs-plugin][ERROR] %s\n' "$*" >&2; }

usage() { sed -n '2,18p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --vault)     VAULT="${2:?--vault 需要参数}"; shift 2 ;;
    --vault=*)   VAULT="${1#*=}"; shift ;;
    --plugins)   PLUGINS="${2:?--plugins 需要参数}"; shift 2 ;;
    --plugins=*) PLUGINS="${1#*=}"; shift ;;
    --retries)   RETRIES="${2:?--retries 需要参数}"; shift 2 ;;
    --dry-run)   DRY_RUN=1; shift ;;
    --force)     FORCE=1; shift ;;
    -h|--help)   usage ;;
    *) err "未知参数: $1"; usage; exit 2 ;;
  esac
done

command -v curl >/dev/null 2>&1 || { err "需要 curl"; exit 1; }
command -v python3 >/dev/null 2>&1 || { err "需要 python3"; exit 1; }

# 断点续传 + 指数退避重试 + 字节数校验；失败删半截文件
fetch_asset() {
  local url="$1" dst="$2" label="$3" expect="$4"
  local attempt=1 delay=1 sz
  # 已存在且字节数吻合 → 直接复用（配合断点续传，避免重复拉取大文件）
  if [[ -f "$dst" && -n "$expect" && "$expect" != "0" && "$(wc -c <"$dst" | tr -d ' ')" == "$expect" ]]; then
    log "  $label 已存在且完整（$expect bytes）"
    return 0
  fi
  while [[ $attempt -le $RETRIES ]]; do
    if curl -fsSL -C - --max-time 300 --connect-timeout 15 -o "$dst" "$url"; then
      sz="$(wc -c <"$dst" | tr -d ' ')"
      if [[ -n "$expect" && "$expect" != "0" && "$sz" != "$expect" ]]; then
        warn "  $label 大小不符（$sz / $expect）→ 重试 $attempt/$RETRIES"
      else
        log "  $label OK（$sz bytes）"
        return 0
      fi
    else
      warn "  $label 下载失败 → 重试 $attempt/$RETRIES"
    fi
    attempt=$((attempt + 1))
    sleep "$delay"
    delay=$((delay * 4))
  done
  rm -f "$dst"
  return 1
}

install_one() {
  local id="$1" repo="${PLUGIN_REPOS[$1]:-}"
  if [[ -z "$repo" ]]; then err "未知插件 id: $id（已知：${!PLUGIN_REPOS[*]}）"; return 1; fi

  local dir="$VAULT/.obsidian/plugins/$id"
  if [[ -d "$dir" && $FORCE -eq 0 && -f "$dir/main.js" && -f "$dir/manifest.json" ]]; then
    log "跳过（已安装且完整）: $id"
    INSTALLED_IDS+=("$id")
    return 0
  fi

  local rel_json tag
  rel_json="$(curl -fsSL --max-time 25 "https://api.github.com/repos/${repo}/releases/latest" || true)"
  [[ -n "$rel_json" ]] || { warn "无法获取 ${repo} release 信息（网络/限流）"; return 1; }
  tag="$(printf '%s' "$rel_json" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("tag_name",""))')"
  [[ -n "$tag" ]] || { err "$id: 无法解析 tag"; return 1; }

  declare -A sizes=()
  local name size
  while read -r name size; do
    [[ -n "$name" ]] && sizes["$name"]="$size"
  done < <(printf '%s' "$rel_json" | python3 -c 'import sys,json;[print(a["name"], a.get("size",0)) for a in json.load(sys.stdin).get("assets",[])]')

  log "$id <- $repo @ $tag（资产：${!sizes[*]}）"
  if [[ $DRY_RUN -eq 1 ]]; then
    log "[dry-run] 将下载到 $dir"
    return 0
  fi

  if [[ -f "$dir/main.js" && $FORCE -eq 1 ]]; then
    cp -p "$dir/main.js" "${dir}/main.js.bak-${TS}"
    log "已备份: ${id}/main.js.bak-${TS}"
  fi
  mkdir -p "$dir"

  local base="https://github.com/${repo}/releases/download/${tag}"
  local a
  # 必需资产：缺任一即判失败
  for a in main.js manifest.json; do
    [[ -n "${sizes[$a]:-}" ]] || { err "$id: release 未提供 $a"; return 1; }
    fetch_asset "${base}/${a}" "$dir/$a" "$a" "${sizes[$a]}" || { err "$id: $a 下载失败"; return 1; }
  done
  # 可选资产：失败不致命
  for a in styles.css data.json; do
    [[ -n "${sizes[$a]:-}" ]] || continue
    fetch_asset "${base}/${a}" "$dir/$a" "$a" "${sizes[$a]}" || warn "$id: 可选资产 $a 下载失败（忽略）"
  done

  # 读取 manifest：目录名必须等于 manifest id，否则 Obsidian 拒绝加载 → 自动改名自愈
  local mid ver
  mid="$(MANIFEST="$dir/manifest.json" python3 -c 'import json,os; print(json.load(open(os.environ["MANIFEST"], encoding="utf-8")).get("id",""))' 2>/dev/null || true)"
  ver="$(MANIFEST="$dir/manifest.json" python3 -c 'import json,os; print(json.load(open(os.environ["MANIFEST"], encoding="utf-8")).get("version",""))' 2>/dev/null || true)"
  if [[ -z "$mid" ]]; then
    err "$id: manifest.json 解析失败"
    return 1
  fi
  log "  manifest: id=$mid version=$ver"
  if [[ "$mid" != "$id" ]]; then
    local root="$VAULT/.obsidian/plugins"
    warn "manifest id 为 $mid，与目录名 $id 不一致 → 自动重命名目录（Obsidian 要求两者相同）"
    if [[ $DRY_RUN -eq 0 ]]; then
      mv "$dir" "$root/$mid"
    fi
    dir="$root/$mid"
    id="$mid"
  fi
  INSTALLED_IDS+=("$id")
  log "已安装: $id"
}

update_community_plugins() {
  local f="$VAULT/.obsidian/community-plugins.json"
  [[ $DRY_RUN -eq 1 ]] && { log "[dry-run] 将更新 community-plugins.json"; return 0; }
  mkdir -p "$VAULT/.obsidian"
  [[ -f "$f" ]] && cp -p "$f" "${f}.bak-${TS}"
  WANT_ENV="$1" CFG="$f" python3 - <<'PY'
import json, os
want = os.environ["WANT_ENV"].split(",")
path = os.environ["CFG"]
have = []
if os.path.exists(path):
    try:
        have = json.load(open(path, encoding="utf-8"))
        if not isinstance(have, list):
            have = []
    except json.JSONDecodeError:
        have = []
merged = list(dict.fromkeys(have + want))
with open(path, "w", encoding="utf-8") as fh:
    json.dump(merged, fh, ensure_ascii=False, indent=2)
    fh.write("\n")
print("community-plugins: " + ", ".join(merged))
PY
}

main() {
  log "目标 vault: $VAULT"
  [[ -d "$VAULT" ]] || { err "vault 不存在: $VAULT（先跑 install-math-templates.sh）"; exit 1; }

  local ok=0 fail=0
  IFS=',' read -r -a ids <<<"$PLUGINS"
  for id in "${ids[@]}"; do
    id="$(printf '%s' "$id" | tr -d ' ')"
    [[ -n "$id" ]] || continue
    if install_one "$id"; then ok=$((ok + 1)); else fail=$((fail + 1)); fi
  done

  # 以实际安装成功的 id（manifest 口径）写回启用清单
  local joined=""
  if [[ ${#INSTALLED_IDS[@]} -gt 0 ]]; then
    joined="$(IFS=,; printf '%s' "${INSTALLED_IDS[*]}")"
  else
    joined="$(IFS=,; printf '%s' "${ids[*]}")"
  fi
  update_community_plugins "$joined"

  log "完成：成功 $ok / 失败 $fail"
  log "下一步：Obsidian → 设置 → 第三方插件 → 关闭安全模式 → 启用：${PLUGINS}"
  [[ $fail -gt 0 ]] && exit 1
  exit 0
}

main "$@"
