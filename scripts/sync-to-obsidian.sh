#!/bin/bash
# sync-to-obsidian.sh
# 同步 tdx-data-feed 文档到 Obsidian Vault
# 用法: bash sync-to-obsidian.sh [--dry-run] [--watch]

set -e

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
    "方案 E：翻倍当日跟随策略"

copy_doc "$SRC/docs/PLAN_E_LIVE.md" \
    "$VAULT/04-策略与方案/PLAN_E_LIVE-实盘启动指南.md" \
    "方案 E：实盘启动指南"

# 6. 数据与配置
copy_doc "$SRC/docs/tdx-data-feed-数据字典.md" \
    "$VAULT/05-数据与配置/数据字典.md" \
    "数据字典"

copy_doc "$SRC/docs/volume-单位治理计划书.md" \
    "$VAULT/05-数据与配置/volume-单位治理.md" \
    "成交量单位治理"

# 7. 运维（已在 00 同步）
# SECURITY.md 已同步到 00-知识库入口/

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