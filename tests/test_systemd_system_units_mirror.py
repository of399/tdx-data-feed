"""系统级单元的仓库镜像与漂移守卫。

运行态里有一批 system 作用域的单元（`/etc/systemd/system/`）直接跑本仓库脚本，
但仓库里一直没有它们的副本：改仓库文件不会影响运行态，改运行态也没人发现——
`systemd-analyze verify --user` 看不见它们，`make timer-install` 也不碰它们。

这里锁三件事：
1. 凡是引用本仓库路径的 system 单元，仓库 `systemd/system/` 下都要有一份副本
   （`math-sympy-http.service` 例外：它在 `v5/systemd/`，由
   `tests/test_math_unit_deployed_matches_repo.py` 分层守）。本机没部署时跳过反向扫描。
   不跑仓库代码的外部二进制服务（`EXTERNAL_SERVICES`）也登记在 `MIRRORED`：
   反向扫描找不到它们，登记只为让第 2 条继续做字节对照。
2. 副本与 `/etc/systemd/system/` 下的部署文件内容一致（本机无部署则跳过）。
3. 每日同步只有一个调度源：16:30 的系统级 `tdx-sync.timer`；用户级
   `tdxfeed-sync.timer` 不再被 `make timer-enable` 启用（文件保留以便回滚）。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MIRROR = ROOT / "systemd" / "system"
DEPLOYED_ROOT = Path("/etc/systemd/system")
REPO_MARK = "/home/jiuben/tdx-data-feed"

# math-sympy-http 有自己的一套（v5/systemd/ + 三层漂移守卫），不重复镜像。
EXEMPT = {"math-sympy-http.service"}

# 跑外部二进制、不跑仓库代码的服务：它们的 ExecStart 本来就不该出现仓库 venv。
EXTERNAL_SERVICES = {"alertmanager.service"}

MIRRORED = {
    "alertmanager.service",
    "audit-log-rotate.service",
    "audit-log-rotate.timer",
    "daily-fallback-report.service",
    "daily-fallback-report.timer",
    "evolution_daily_report.service",
    "evolution_daily_report.timer",
    "pushplus_bridge.service",
    "scheduler_agent.service",
    "scheduler_agent.timer",
    "suggestion_agent.service",
    "suggestion_agent.timer",
    "tdx-autonomy.service",
    "tdx-evolution.service",
    "tdx-monitor.service",
    "tdx-monitor.timer",
    "tdx-sync.service",
    "tdx-sync.timer",
    "tdx-verification.service",
    "tdx-verification.timer",
}


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8").rstrip("\n")


def _deployed_units() -> dict[str, Path]:
    """引用本仓库的部署单元。

    timer 自身通常不写仓库路径（只有 `Unit=` 指向 service），所以要把「激活的 service
    引用了本仓库」的 timer 也算进来，否则反向扫描会把它们误判成仓库里多出来的副本。
    """
    if not DEPLOYED_ROOT.is_dir():
        return {}
    bodies: dict[str, str] = {}
    for p in sorted(DEPLOYED_ROOT.glob("*.service")) + sorted(DEPLOYED_ROOT.glob("*.timer")):
        try:
            bodies[p.name] = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue

    referenced = {name for name, body in bodies.items()
                  if name.endswith(".service") and REPO_MARK in body}
    found = {name: DEPLOYED_ROOT / name for name in bodies
             if name in referenced or (name.endswith(".timer") and REPO_MARK in bodies[name])}
    for name, body in bodies.items():
        if not name.endswith(".timer"):
            continue
        target = "".join(re.findall(r"^Unit=(.+)$", body, re.M)).strip() or name[:-6] + ".service"
        if target in referenced:
            found[name] = DEPLOYED_ROOT / name
    return found


def test_mirror_lists_the_units_we_track():
    """副本清单本身是锁死的：少一个就是运行态失联，多一个就是没登记的新单元。"""
    assert MIRROR.is_dir(), f"缺 {MIRROR}：system 单元在仓库里没有落点"
    present = {p.name for p in MIRROR.iterdir() if p.suffix in {".service", ".timer"}}
    assert present == MIRRORED


def test_every_repo_referencing_system_unit_is_mirrored():
    """反向扫描：/etc 里引用本仓库的单元不能在仓库里查无此文件。"""
    deployed = _deployed_units()
    if not deployed:
        pytest.skip("本机没有部署引用本仓库的 system 单元（CI/干净环境）")
    missing = set(deployed) - MIRRORED - EXEMPT
    assert not missing, f"运行态有单元但仓库没镜像：{sorted(missing)}"
    stale = MIRRORED - set(deployed) - EXTERNAL_SERVICES
    assert not stale, f"仓库镜像了但本机已不存在（删掉副本或补部署）：{sorted(stale)}"


def test_mirror_matches_deployed_bytes():
    """副本是给人读的第二现场，和部署文件不一致就等于两套口径。"""
    deployed = _deployed_units()
    if not deployed:
        pytest.skip("本机没有部署引用本仓库的 system 单元（CI/干净环境）")
    drifts = []
    for name in sorted(MIRRORED):
        local = MIRROR / name
        remote = DEPLOYED_ROOT / name
        if not local.is_file() or not remote.is_file():
            drifts.append(f"{name}: 缺副本或缺部署")
            continue
        if _text(local) != _text(remote):
            drifts.append(f"{name}: 仓库与 /etc 内容不同")
    assert not drifts, "system 单元漂移：\n" + "\n".join(drifts)


def test_mirrored_services_use_the_repo_venv():
    """每条 ExecStart 都要显式指向仓库 venv —— 系统单元的 PATH 里没有它。"""
    problems = []
    for name in sorted(MIRRORED):
        if not name.endswith(".service") or name in EXTERNAL_SERVICES:
            continue
        body = _text(MIRROR / name)
        execs = re.findall(r"^ExecStart=(.+)$", body, re.M)
        if not execs:
            problems.append(f"{name}: 没有 ExecStart")
            continue
        for cmd in execs:
            if f"{REPO_MARK}/venv/bin/python" not in cmd:
                problems.append(f"{name}: ExecStart 未用仓库 venv：{cmd}")
    assert not problems, "\n".join(problems)


def test_sync_has_exactly_one_scheduling_source():
    """16:30 系统级是唯一调度源；用户级 18:00 已退役（决策于 2026-10-28）。"""
    text = (ROOT / "Makefile").read_text(encoding="utf-8")
    block = text.split("timer-enable:", 1)[1].split("\n\n", 1)[0]
    recipe = "\n".join(ln for ln in block.splitlines()
                       if not ln.lstrip().startswith("#"))
    assert "tdxfeed-sync" not in recipe, \
        "timer-enable 仍会拉起用户级 tdxfeed-sync.timer，与系统级 tdx-sync.timer 重复"

    sync_timer = _text(MIRROR / "tdx-sync.timer")
    assert re.search(r"^OnCalendar=Mon\.\.Fri 16:30:00$", sync_timer, re.M), \
        "系统级同步时刻被改过，先确认没有第二个调度源"


def test_canary_watches_the_system_sync_timer():
    """canary 原来只查 --user 管理器，同步搬去 system 后它会一直看不见同步死没死。"""
    body = (ROOT / "scripts" / "canary_health_check.py").read_text(encoding="utf-8")
    assert "'tdx-sync.timer'" in body, "canary 没检查系统级同步 timer"
    assert "'tdx-sync.service'" in body, "canary 还在查用户级 sync 的最后运行时间"
    for retired in ("'tdxfeed-sync.service'", "'tdxfeed-sync.timer'"):
        assert retired not in body, \
            f"canary 还在盯已退役的 {retired}，会一直报 inactive / 时间戳为空"
