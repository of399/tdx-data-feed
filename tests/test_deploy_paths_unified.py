"""P1-8 部署脚本路径统一守卫（R-仓库、可验证、无需 root）。

锁两件事：
1. Python 入口默认值（tdxfeed/cli.py / full_sync.py / fin_parquet.py）必须 derive 自
   tdxfeed.paths 单一源，不得出现仓库根字面量 `/home/jiuben/tdx-data-feed`。
2. Shell 部署脚本（scripts/sync-to-obsidian.sh / git_init_vaults.sh / scripts/hooks/pre-commit）
   必须 source scripts/lib/tdx-env.sh（或按脚本位置推导），不留仓库根字面量。

systemd 单元的 ${TDX_HOME} 改造属迁移步骤 4（高耦合、需 root 重新部署），其守卫在
tests/test_systemd_system_units_mirror.py 与本文档扩展项里，见
docs/P1-8_DEPLOY_PATH_UNIFICATION.md。
"""
from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
REPO_LITERAL = "/home/jiuben/tdx-data-feed"

# 外部 vault（StockVault）不在仓库内，允许作为主机默认保留，不纳入守卫
EXTERNAL_LITERAL = "/home/jiuben/StockVault"

PY_FILES = ["tdxfeed/cli.py", "tdxfeed/full_sync.py", "tdxfeed/fin_parquet.py"]
SH_FILES = [
    "scripts/sync-to-obsidian.sh",
    "scripts/git_init_vaults.sh",
    "scripts/hooks/pre-commit",
]


def test_python_entry_paths_derive_from_single_source():
    """Python 入口不得硬编码仓库根，必须引用 tdxfeed.paths 单一源。"""
    for rel in PY_FILES:
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert REPO_LITERAL not in text, (
            f"{rel} 仍硬编码仓库根字面量 {REPO_LITERAL}；应改用 tdxfeed.paths.ROOT/DATA_ROOT"
        )
        assert "tdxfeed.paths" in text, (
            f"{rel} 未引用 tdxfeed.paths 单一源（直接用 ROOT / DATA_ROOT）"
        )


def test_shell_deploy_scripts_derive_repo_root():
    """Shell 部署脚本不得硬编码仓库根，必须 source 单一路径源或按位置推导。"""
    for rel in SH_FILES:
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert REPO_LITERAL not in text, (
            f"{rel} 仍硬编码仓库根字面量 {REPO_LITERAL}（含注释/用法示例）"
        )
        assert "tdx-env.sh" in text or "BASH_SOURCE" in text, (
            f"{rel} 未 source scripts/lib/tdx-env.sh，也未按脚本位置推导仓库根"
        )


def test_tdx_env_sh_is_the_single_source():
    """tdx-env.sh 自身：TDX_HOME 必须推导，不允许把仓库根写死成默认值。"""
    text = (ROOT / "scripts/lib/tdx-env.sh").read_text(encoding="utf-8")
    assert "TDX_HOME" in text
    # TDX_HOME 默认必须来自脚本位置推导（BASH_SOURCE），而非写死的主机路径
    assert "BASH_SOURCE" in text, "tdx-env.sh 的 TDX_HOME 必须按脚本位置推导"
    assert 'TDX_HOME="/home' not in text, "tdx-env.sh 不能把仓库根写死成默认值"


def test_external_vault_default_is_overridable_only():
    """反向确认：仓库根字面量未通过「外部 vault」借口被悄悄保留在受管文件里。"""
    offenders = []
    for rel in PY_FILES + SH_FILES:
        text = (ROOT / rel).read_text(encoding="utf-8")
        # 受管文件里只允许出现外部 vault 默认，不允许出现仓库根默认
        for line in text.splitlines():
            if REPO_LITERAL in line:
                offenders.append(f"{rel}: {line.strip()}")
    assert not offenders, "受管文件出现仓库根字面量：\n" + "\n".join(offenders)
    # 外部 vault 默认必须能被覆盖（出现在 tdx-env.sh 的默认赋值里）
    env = (ROOT / "scripts/lib/tdx-env.sh").read_text(encoding="utf-8")
    assert 'TDX_VAULT_STOCK="${TDX_VAULT_STOCK:-' in env or "TDX_VAULT_STOCK:-" in env
