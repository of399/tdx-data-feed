#!/bin/bash
# sync-to-obsidian.sh
# 同步 tdx-data-feed 文档到 Obsidian Vault
# 用法: bash sync-to-obsidian.sh [--dry-run] [--watch]

set -e -o pipefail   # 补 pipefail 防管道吞错（保留 -e）

# ============== 配置 ==============
SRC="/home/jiuben/tdx-data-feed"
VAULT="/home/jiuben/ObsidianVaults/tdx-data-feed"

# 颜色
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
RED='\033[0;31m'
NC='\033[0m'

# 参数
DRY_RUN=0
WATCH=0
for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=1 ;;
        --watch) WATCH=1 ;;
        *) ;;
    esac
done

# ============== 单次运行守卫：文件锁 + 冷却 ==============
# 防止 path 单元风暴 / 并发叠加（systemd 重启风暴二次防护）。
# watch 模式由底部循环以"非 watch"方式逐次调用本脚本，此处跳过加锁，
# 避免父进程长期持有 fd 导致子进程 flock 失败。
if [ "$WATCH" != "1" ]; then
    LOCK_FILE=/tmp/obsidian-sync.lock
    exec 9>"$LOCK_FILE" 2>/dev/null || { echo "无法创建锁文件，退出"; exit 1; }
    if ! flock -n 9; then
        echo "已有同步在运行，跳过（避免并发/自激）"
        exit 0
    fi

    # 冷却：距上次成功运行不足 30s 则跳过（吸收高频连续触发）
    LAST_RUN_FILE=/tmp/obsidian-sync.lastrun
    now_ts=$(date +%s)
    if [ -f "$LAST_RUN_FILE" ]; then
        last_ts=$(cat "$LAST_RUN_FILE" 2>/dev/null || echo 0)
        if [ $((now_ts - last_ts)) -lt 30 ]; then
            echo "冷却期内（<30s），跳过"
            exit 0
        fi
    fi
    echo "$now_ts" > "$LAST_RUN_FILE"
fi

log() {
    if [ "$DRY_RUN" = "1" ]; then
        echo -e "${YELLOW}[DRY-RUN]${NC} $@"
    else
        echo -e "${CYAN}[SYNC]${NC} $@"
    fi
}

ok() {
    echo -e "${GREEN}✓${NC} $@"
}

# ============== 复制函数 ==============

copy_doc() {
    local src=$1
    local dst=$2
    local title=$3
    local doc_type=$4
    local related_strategy=$5

    if [ "$DRY_RUN" = "1" ]; then
        log "复制: $src → $dst"
        return
    fi

    if [ ! -f "$src" ]; then
        echo -e "${RED}❌ 源文件不存在: $src${NC}"
        return 1
    fi

    # 计算哈希和行数
    local today=$(date +%Y-%m-%d)
    local src_hash=$(sha256sum "$src" | cut -c1-8)
    local line_count=$(wc -l < "$src")

    # 读取原始内容
    local content=$(cat "$src")

    # 写入目标（含 frontmatter）
    cat > "$dst" <<FRONTMATTER
---
title: $title
source: $src
source_hash: $src_hash
synced_at: $today
line_count: $line_count
doc_type: ${doc_type:-}
related_strategy: ${related_strategy:-}
tags:
  - tdx-data-feed
  - documentation
---

<!--
⚠️ 本文档由 sync-to-obsidian.sh 自动生成
源文件: $src
哈希: $src_hash
同步时间: $today
行数: $line_count
编辑请直接修改源文件，重新运行同步脚本
-->

FRONTMATTER

    # 追加原始内容
    echo "$content" >> "$dst"

    ok "已同步: $(basename $dst) ($line_count 行)"
}

# ============== 报告归档 / MOC ==============

archive_report() {
    local src=$1
    local name=$2
    [ -f "$src" ] || return 0
    if [ "$DRY_RUN" = "1" ]; then log "归档报告: $name"; return 0; fi
    mkdir -p "$VAULT/08-报告/archive"
    local h=$(sha256sum "$src" | cut -c1-12)
    local dest="$VAULT/08-报告/archive/${name}_${h}.md"
    if [ ! -f "$dest" ]; then
        cp "$src" "$dest"
        ok "已归档报告: $(basename "$dest")"
    fi
}

generate_moc() {
    local dst="$VAULT/00-知识库入口/索引-MOC.md"
    if [ "$DRY_RUN" = "1" ]; then log "生成 MOC 索引: $dst"; return 0; fi
    mkdir -p "$VAULT/00-知识库入口"
    cat > "$dst" <<'MOC'
---
title: 文档索引（自动生成 MOC）
tags:
  - tdx-data-feed
  - moc
generated_by: sync-to-obsidian.sh
---

# tdx-data-feed 文档索引（自动生成）

> 由 `scripts/sync-to-obsidian.sh` 每次同步自动生成。所有同步文档均带 `#tdx-data-feed` 标签。

## 一、按同步时间（最新在前）
```dataview
TABLE source AS 源文件, synced_at AS 同步时间, source_hash AS 哈希, line_count AS 行数
FROM #tdx-data-feed AND -#moc
SORT synced_at DESC
```

## 二、按源文件查漂移（对比 source_hash 与源文件当前 sha256）
```dataview
TABLE source AS 源文件, source_hash AS 同步时哈希, synced_at AS 同步时间
FROM #tdx-data-feed AND -#moc
SORT source ASC
```

## 三、按类型（doc_type）
```dataview
TABLE doc_type AS 类型, source AS 源文件
FROM #tdx-data-feed AND -#moc
WHERE doc_type
SORT doc_type ASC
```
MOC
    ok "已生成 MOC 索引: $(basename "$dst")"
}

# ============== 主流程 ==============

echo -e "${CYAN}╔════════════════════════════════════════════╗${NC}"
echo -e "${CYAN}║   tdx-data-feed → Obsidian 同步工具       ║${NC}"
echo -e "${CYAN}╚════════════════════════════════════════════╝${NC}"
echo ""
echo "源: $SRC"
echo "目标: $VAULT"
echo ""

# 检查源
if [ ! -d "$SRC" ]; then
    echo -e "${RED}❌ 源目录不存在: $SRC${NC}"
    exit 1
fi

# 1. 核心入口
copy_doc "$SRC/README.md" \
    "$VAULT/00-知识库入口/README-项目总览.md" \
    "项目总览"

copy_doc "$SRC/CONTRIBUTING.md" \
    "$VAULT/00-知识库入口/贡献指南.md" \
    "贡献指南"

copy_doc "$SRC/SECURITY.md" \
    "$VAULT/00-知识库入口/安全策略.md" \
    "安全策略"

# 2. 项目概览 + 交付
copy_doc "$SRC/DELIVERY.md" \
    "$VAULT/01-项目概览/DELIVERY-项目交付报告.md" \
    "项目交付报告"

# 同步 CHANGELOG
copy_doc "$SRC/CHANGELOG.md" \
    "$VAULT/01-项目概览/CHANGELOG-版本变更.md" \
    "Changelog（版本变更记录）"

# 3. 架构与工作流
copy_doc "$SRC/docs/ARCHITECTURE.md" \
    "$VAULT/02-架构与工作流/ARCHITECTURE-架构决策.md" \
    "架构决策记录"

copy_doc "$SRC/docs/WORKFLOW.md" \
    "$VAULT/02-架构与工作流/WORKFLOW-工作流全景.md" \
    "工作流全景"

# 4. 开发与 API
copy_doc "$SRC/docs/API.md" \
    "$VAULT/03-开发与API/API-接口文档.md" \
    "API 接口文档"

copy_doc "$SRC/Makefile" \
    "$VAULT/03-开发与API/Makefile-完整命令参考.md" \
    "Makefile 完整命令参考"

# 5. 策略与方案
copy_doc "$SRC/docs/PLAN_E.md" \
    "$VAULT/04-策略与方案/PLAN_E-翻倍当日跟随策略.md" \
    "方案 E：翻倍当日跟随策略" \
    "策略" "plan_e"

copy_doc "$SRC/docs/PLAN_E_LIVE.md" \
    "$VAULT/04-策略与方案/PLAN_E_LIVE-实盘启动指南.md" \
    "方案 E：实盘启动指南" \
    "策略" "plan_e"

# 6. 数据与配置
copy_doc "$SRC/docs/tdx-data-feed-数据字典.md" \
    "$VAULT/05-数据与配置/数据字典.md" \
    "数据字典" \
    "数据" ""

copy_doc "$SRC/docs/volume-单位治理计划书.md" \
    "$VAULT/05-数据与配置/volume-单位治理.md" \
    "成交量单位治理"

# 7. 运维（已在 00 同步）
# SECURITY.md 已同步到 00-知识库入口/

# 8. 评测 / 回测报告（#5 / #6 流水线产出，动态同步最新一份）
echo ""
echo "📊 评测 / 回测报告（最新）"
mkdir -p "$VAULT/08-报告"
LATEST_F5="$(ls -t "$SRC/training/eval/compare_"*.json 2>/dev/null | head -1)"
if [ -n "$LATEST_F5" ]; then
  F5_MD="${LATEST_F5%.json}.md"
  if [ ! -f "$F5_MD" ]; then
    F5_MD="$(mktemp -t f5_report_XXXX.md)"
    python3 - "$LATEST_F5" "$F5_MD" <<'PY' || F5_MD=""
import sys, json, datetime
try:
    src, dst = sys.argv[1], sys.argv[2]
    d = json.load(open(src, encoding="utf-8"))
    a, b = d.get("a", {}), d.get("b", {})
    L = [f"# #5 评测对比报告（派生自 {src.split('/')[-1]}）", "",
         f"> 生成时间：{datetime.datetime.now():%Y-%m-%d %H:%M:%S} ｜ 来源：training/eval", "",
         f"| 指标 | {a.get('name','A')} | {b.get('name','B')} | Δ |",
         "| --- | --- | --- | --- |",
         f"| 方向准确率 | {a.get('direction_accuracy',0):.4f} | {b.get('direction_accuracy',0):.4f} | {d.get('delta_dir_acc',0):+.4f} |",
         f"| 格式遵循率 | {a.get('format_compliance_rate',0):.4f} | {b.get('format_compliance_rate',0):.4f} | {d.get('delta_fmt',0):+.4f} |",
         f"| 解析失败率 | {a.get('parse_fail_rate',0):.4f} | {b.get('parse_fail_rate',0):.4f} | — |",
         f"| 样本数 | {a.get('n_samples',0)} | {b.get('n_samples',0)} | — |"]
    open(dst, "w", encoding="utf-8").write("\n".join(L) + "\n")
except Exception:
    pass
PY
  fi
  if [ -n "$F5_MD" ] && [ -f "$F5_MD" ]; then
    copy_doc "$F5_MD" "$VAULT/08-报告/05-评测对比报告.md" "方案E 评测对比报告"
  fi
fi
LATEST_F6="$(ls -t "$SRC/data/plan_e/llm_signal_compare_"*.md 2>/dev/null | head -1)"
if [ -n "$LATEST_F6" ]; then
  copy_doc "$LATEST_F6" "$VAULT/08-报告/06-方案E回测对比报告.md" "方案E 回测对比报告"
fi

# 9. 报告历史归档（避免回测演进记录丢失）+ 自动 MOC 索引（配合 Dataview）
archive_report "$VAULT/08-报告/05-评测对比报告.md" "05-评测对比报告"
archive_report "$VAULT/08-报告/06-方案E回测对比报告.md" "06-方案E回测对比报告"
generate_moc

# 10. 跨 vault 互链钩子（防 sync 覆盖 related_stockvault / 00-跨库索引.md）
#     link_cross_vault.py 幂等：仅字段变化时才写盘，可安全每次 sync 重跑。
echo ""
echo "🔗 跨 vault 互链（重建 related_stockvault / 00-跨库索引.md）"
if [ "$DRY_RUN" = "1" ]; then
    log "跨 vault 互链钩子: $SRC/venv/bin/python $SRC/scripts/link_cross_vault.py --dry-run"
else
    if "$SRC/venv/bin/python" "$SRC/scripts/link_cross_vault.py"; then
        ok "跨 vault 互链已重建"
    else
        echo -e "${YELLOW}⚠️ 跨 vault 互链钩子返回非零，请检查 scripts/link_cross_vault.py${NC}"
    fi
fi

echo ""
ok "同步完成！"
echo ""
echo "📊 统计："
echo "  文档数: $(find $VAULT -name '*.md' ! -name 'README.md' | wc -l)"
echo "  总大小: $(du -sh $VAULT | cut -f1)"
echo "  入口: $VAULT/00-知识库入口/README-项目总览.md"
echo ""

# Watch 模式
if [ "$WATCH" = "1" ]; then
    echo -e "${CYAN}👁 Watch 模式（每 60 秒检查变更）${NC}"
    while true; do
        sleep 60
        echo ""
        echo -e "${CYAN}[$(date +%H:%M:%S)] 检查变更...${NC}"
        bash "$0" 2>&1 | grep -E "已同步|✓|❌" || true
    done
fi
