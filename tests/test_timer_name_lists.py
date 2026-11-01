"""Makefile 的 user timer 名单必须一处定义、三处收口。

`USER_TIMERS` 是 timer-install 的白名单，`timer-enable` / `timer-disable` / `timer-status`
以及 `status` 的 timer 快照各自还有一份名字列表。名单漏一个不会报错：
停不掉、状态里也看不见它，出事时以为已经停了。`tdx-clean-logs` 2026-11-01 之前
就是这样——unit 装着、timer-install 复制了，但从未被 enable，也没进 status。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAKEFILE = ROOT / "Makefile"

# 只读 Makefile 文本。
NEEDS_DATA = False


def _make_text() -> str:
    return MAKEFILE.read_text(encoding="utf-8")


def _var(name: str) -> list[str]:
    text = _make_text()
    m = re.search(rf"^{name}\s*:=\s*((?:.*\\\n)*.*)$", text, re.M)
    assert m, f"Makefile 里没有 {name}"
    return m.group(1).replace("\\\n", " ").split()


def _target_block(name: str) -> str:
    lines = _make_text().splitlines()
    start = None
    for i, line in enumerate(lines):
        if re.match(rf"^{name}\s*[!:]", line):
            start = i
            break
    assert start is not None, f"Makefile 里没有 {name} 目标"
    block = [lines[start]]
    for line in lines[start + 1:]:
        if re.match(r"^\S.*:", line):
            break
        block.append(line)
    return "\n".join(block)


def test_clean_logs_is_in_the_shared_list():
    assert "tdx-clean-logs" in _var("USER_TIMERS")


def test_every_user_timer_is_installed_as_a_pair():
    """timer-install 同时 cp service 与 timer；缺哪个都会在部署时才炸。"""
    for name in _var("USER_TIMERS"):
        assert (ROOT / "systemd" / f"{name}.service").is_file(), f"缺 {name}.service"
        assert (ROOT / "systemd" / f"{name}.timer").is_file(), f"缺 {name}.timer"


def test_enable_and_disable_iterate_the_shared_list():
    """名单只写一处：enable/disable 必须用 $(USER_TIMERS)，不能再硬编码。"""
    assert "$(USER_TIMERS)" in _target_block("timer-enable")
    assert "$(USER_TIMERS)" in _target_block("timer-disable")


def test_retired_timers_stay_disable_only():
    retired = _var("RETIRED_TIMERS")
    assert set(retired) & set(_var("USER_TIMERS")) == set(), "退役单元不能回到启用白名单"
    disable = _expand(_target_block("timer-disable"))
    for name in retired:
        assert name in disable, f"timer-disable 漏了退役单元 {name}"
    enable = _target_block("timer-enable")
    assert "$(RETIRED_TIMERS)" not in enable, "退役单元不能被 timer-enable 带回来"


def _grep_patterns(block: str) -> list[str]:
    return re.findall(r'grep -E "([^"]+)"', block)


def _expand(block: str) -> str:
    for name in ("USER_TIMERS", "RETIRED_TIMERS"):
        block = block.replace(f"$({name})", " ".join(_var(name)))
    return block


def test_every_user_timer_is_visible_in_status_lists():
    """`status` 第 481 行的 timer 快照用的是另一份 grep 名单，漏了同样看不见。"""
    for target in ("timer-status", "status"):
        patterns = _grep_patterns(_target_block(target))
        assert patterns, f"{target} 里没有 timer 的 grep 名单"
        for name in _var("USER_TIMERS"):
            assert any(re.search(p, name) for p in patterns), (
                f"Makefile 的 {target} 里看不见 {name}（patterns={patterns}）"
            )


def test_clean_logs_target_uses_the_rotation_script():
    """`find -mtime +7 -delete` 对 systemd append 日志永远不生效（mtime 一直是今天）。"""
    block = _target_block("clean-logs")
    assert "clean_logs.py" in block, block
    unit = (ROOT / "systemd" / "tdx-clean-logs.service").read_text(encoding="utf-8")
    assert "clean-logs" in unit
