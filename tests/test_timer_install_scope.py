"""P0-4（KI-OPS-001）：make timer-install 必须覆盖 obsidian-sync 的 .path 单元。

历史上 `USER_TIMERS` 只列 .service/.timer，`timer-install` 把 `obsidian-sync.path`
漏在管理范围外——单元在跑，但仓库改动传不下去，canary 每 5 分钟报"修复动作无效"。
修复：新增 `USER_PATHS`（service 先于 path）+ `RETIRED_PATHS`，并在 timer-install /
timer-disable 里遍历它们。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAKEFILE = ROOT / "Makefile"


def _var(name: str) -> list[str]:
    text = MAKEFILE.read_text(encoding="utf-8")
    m = re.search(rf"^{name}\s*:=\s*((?:.*\\\n)*.*)$", text, re.M)
    assert m, f"Makefile 里没有 {name}"
    return m.group(1).replace("\\\n", " ").split()


def test_obsidian_path_in_install_whitelist():
    paths = _var("USER_PATHS")
    assert "obsidian-sync.service" in paths
    assert "obsidian-sync.path" in paths
    # service 必须排在 path 之前：.path 触发时 .service 须已存在，否则首触失败。
    assert paths.index("obsidian-sync.service") < paths.index("obsidian-sync.path")


def test_retired_path_is_disabled():
    assert "tdxfeed-sync.path" in _var("RETIRED_PATHS")


def test_path_lists_do_not_leak_into_timer_lists():
    paths = set(_var("USER_PATHS")) | set(_var("RETIRED_PATHS"))
    assert paths & set(_var("USER_TIMERS")) == set(), "path 名单不应混入 timer 名单"
    assert paths & set(_var("RETIRED_TIMERS")) == set(), "path 名单不应混入退役 timer 名单"


def test_timer_install_and_disable_iterate_the_new_lists():
    """名单只写一处：install/disable 必须用 $(USER_PATHS)/$(RETIRED_PATHS)，不能再硬编码。"""
    block = MAKEFILE.read_text(encoding="utf-8")
    install = block[block.index("timer-install:"):block.index("timer-enable:")]
    disable = block[block.index("timer-disable:"):block.index("timer-status:")]
    assert "$(USER_PATHS)" in install, "timer-install 没复制 USER_PATHS"
    assert "$(RETIRED_PATHS)" in disable, "timer-disable 没收口 RETIRED_PATHS"
