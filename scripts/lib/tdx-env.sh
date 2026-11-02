#!/usr/bin/env bash
# =============================================================================
# tdx-data-feed 部署/同步脚本的单一路径源（P1-8）
#
# 所有 host 侧部署脚本（sync-to-obsidian.sh / git_init_vaults.sh / hooks/pre-commit
# 等）必须 source 本文件，禁止再硬编码仓库根 / venv / 数据 / 配置路径。
#
# 仓库根 TDX_HOME 的优先级：
#   1. 已设环境变量 TDX_HOME（CI / 非标准部署目录时显式覆盖）
#   2. 否则按本文件位置推导：scripts/lib/tdx-env.sh → scripts → 仓库根
#
# 外部 vault（StockVault）不在仓库内，无法从仓库推导，保留主机默认但可被
# TDX_VAULT_STOCK 覆盖——这是「主机配置」而非「仓库路径」，不在统一范围内。
# =============================================================================
set -euo pipefail

if [[ -z "${TDX_HOME:-}" ]]; then
  TDX_HOME="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
fi

TDX_VENV="${TDX_VENV:-$TDX_HOME/venv}"
TDX_DATA_DIR="${TDX_DATA_DIR:-${TDX_OUT_ROOT:-$TDX_HOME/data}}"
TDX_CONFIG="${TDX_CONFIG:-$TDX_HOME/config.yaml}"
TDX_VAULT_PROJ="${TDX_VAULT_PROJ:-$TDX_HOME/../ObsidianVaults/tdx-data-feed}"
TDX_VAULT_STOCK="${TDX_VAULT_STOCK:-/home/jiuben/StockVault}"

export TDX_HOME TDX_VENV TDX_DATA_DIR TDX_CONFIG TDX_VAULT_PROJ TDX_VAULT_STOCK
