"""定时器形态守卫：区间写法、有害补跑、一键收口名单（设计 §4.4 / §8）。

三条各对应一个踩过的坑，写错的时候都不会报错：

- 分钟级区间（`09:25..11:25/5:00`）在 `systemd.time` 里**解析不了**——2026-10-25 实测
  `systemd-analyze calendar` 报「无效的参数」，单元装上后 timer 直接不触发。只能粗放到小时，
  精确门在脚本里用 `current_session()` 做，所以这里的 OnCalendar 形态是刻意的，不是没写完。
- `Persistent=true` 对 plan-e-double-up 是对的（全量重算幂等），对这里**是有害的**：开机补跑会拿
  昨天的行情出今天的票。守卫必须在，否则以后有人照着隔壁单元「补齐一致性」把它加上。
- `timer-enable`/`timer-disable`/`timer-status` 是 Makefile 里按名字收口的名单，不在名单里的 timer
  收不到一键收口——状态看不到它、`timer-disable` 停不掉它，出事时以为已经停了。
  名单现在从 `USER_TIMERS` 变量展开，本守卫也按变量展开后再查。
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from scripts.render_systemd_units import render_text

# 只读 systemd 单元与 Makefile 的静态形态。
NEEDS_DATA = False

ROOT = Path(__file__).resolve().parents[1]
# 单元里 `Documentation=` 按 systemd 要求写死作者机器上的仓库绝对路径；CI/其他 checkout
# 用它把 token 映射回当前 ROOT。改仓库位置要连单元一起改，否则本守卫会报「指向不存在的文档」。
AUTHOR_ROOT = Path("/home/jiuben/tdx-data-feed")
SERVICE = ROOT / "systemd" / "daban-ticket.service"
TIMER = ROOT / "systemd" / "daban-ticket.timer"


def _rendered(text: str) -> str:
    """单元是模板（仓库根写作 @TDX_HOME@）；按「作者机器路径」渲染。

    渲染成作者机器的绝对路径而不是当前 checkout，是为了让 `Documentation=` 出处保持既有约定
    ——`_resolve_documented_path` 按 AUTHOR_ROOT 前缀把 token 映射回当前 ROOT，CI 里也能核验。
    """
    return render_text(text, str(AUTHOR_ROOT))

# 小时粗筛 + 分钟步进：09/10/11 与 13/14 五个钟点段，每 5 分钟一次（60 次触发，
# 落在连续竞价窗内的约 48 次，其余由脚本内的会话门丢弃）。
ON_CALENDAR = "Mon..Fri *-*-* 09..11,13..14:00/5"


def _ini(text: str) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    section = None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith(";"):
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1]
            out.setdefault(section, {})
            continue
        if section and "=" in stripped:
            key, _, value = stripped.partition("=")
            out[section][key.strip()] = value.strip()
    return out


def test_unit_files_exist():
    assert SERVICE.exists(), f"缺 {SERVICE}"
    assert TIMER.exists(), f"缺 {TIMER}"


def test_timer_uses_the_hour_stepped_calendar():
    timer = _ini(TIMER.read_text(encoding="utf-8"))
    on_calendar = timer["Timer"]["OnCalendar"]
    assert on_calendar == ON_CALENDAR, (
        f"OnCalendar 改成了 {on_calendar!r}。分钟级区间 systemd.time 解析不了"
        "（09:25..11:25/5:00 → 无效的参数）；要改窗口请同时改脚本里的会话门。"
    )
    assert ".." in on_calendar and "/5" in on_calendar


def test_timer_has_no_persistent_replay():
    """补跑会拿旧行情出今天的票，这里有意不启用。"""
    text = _rendered(TIMER.read_text(encoding="utf-8"))
    live = "\n".join(
        ln for ln in text.splitlines() if not ln.strip().startswith(("#", ";"))
    )
    assert "Persistent" not in live, (
        "daban-ticket.timer 里出现了 Persistent=。plan-e-double-up 用 Persistent 是对的"
        "（全量重算幂等），本 timer 盘中每 5 分钟出票，开机补跑一轮等于用旧行情下单。"
    )
    assert _ini(text)["Timer"]["Unit"] == "daban-ticket.service"


@pytest.mark.parametrize("key,expected", [
    ("Type", "oneshot"),
    ("TimeoutStartSec", "180"),
])
def test_service_run_shape(key, expected):
    service = _ini(_rendered(SERVICE.read_text(encoding="utf-8")))
    assert service["Service"][key] == expected


def test_service_executes_the_entry_script():
    service = _ini(_rendered(SERVICE.read_text(encoding="utf-8")))
    exec_start = service["Service"]["ExecStart"]
    assert exec_start.endswith("scripts/daban_entry.py"), exec_start
    assert "venv/bin/python" in exec_start, "必须走仓库自己的 venv（系统 python3 没装本仓库依赖）"
    assert (ROOT / "scripts" / "daban_entry.py").exists()


def test_service_alerts_and_logs_like_the_neighbours():
    """照 plan-e-double-up 的写法：失败进 OnFailure，输出落到 logs/。"""
    text = _rendered(SERVICE.read_text(encoding="utf-8"))
    service = _ini(text)
    assert service["Unit"]["OnFailure"] == "onfailure-alert@daban-ticket.service"
    assert (ROOT / "systemd" / "onfailure-alert@.service").exists()
    for key in ("StandardOutput", "StandardError"):
        value = service["Service"][key]
        assert value.startswith("append:"), f"{key}={value} 应为 append:，否则覆盖历史日志"
        assert "/logs/" in value


def _resolve_documented_path(token: str) -> Path:
    """把 `file://` token 落到当前 checkout 的一个真实文件上。

    systemd 的 `Documentation=` 要绝对路径，仓库约定写死作者机器上的
    `/home/jiuben/tdx-data-feed/...`。在别的 checkout（CI、worktree）里这段绝对路径不存在，
    但仓库内相对位置没变——按约定前缀映射回 `ROOT`。写成其他前缀的不做补救，让 `exists()`
    直接报红，免得「任意绝对路径 + 同形相对路径」互相顶包。
    """
    raw = Path(token[len("file://"):])
    if raw.exists():
        return raw
    try:
        rel = raw.relative_to(AUTHOR_ROOT)
    except ValueError:
        return raw
    return ROOT / rel


def test_documentation_points_at_the_committed_spec():
    """`Documentation=` 指向的设计文档必须**已入库**，而且路径要能自己定位仓库。

    systemd 要求这里是绝对路径（邻居单元都写死 `/home/jiuben/tdx-data-feed/...`），所以不能拿
    `ROOT` 去相对化：2026-10-25 在 `git worktree add --detach` 的干净 checkout 里复验时，
    ROOT 变成 /tmp/... 而 token 仍是绝对路径，`git ls-files` 报 rc=128「outside repository」——
    那是守卫的位置假设错了，不是单元错了。仓库根交给 git 自己回答。

    未入库的文档在干净 checkout 里必然不存在，这正是横幅守卫那天踩过的坑，所以要连
    `ls-files` 一起查，不能只查 `exists()`。CI 里连作者路径都不存在，`_resolve_documented_path`
    先把 token 映射回当前 `ROOT` 再查。
    """
    service = _ini(_rendered(SERVICE.read_text(encoding="utf-8")))
    tokens = service["Unit"]["Documentation"].split()
    assert len(tokens) == 1, f"Documentation 按空白切段，多写就整段被丢：{tokens}"
    token = tokens[0]
    assert token.startswith("file://"), token

    target = _resolve_documented_path(token).resolve()
    assert target.exists(), f"指向不存在的文档 {target}"

    top = subprocess.run(
        ["git", "-C", str(target.parent), "rev-parse", "--show-toplevel"],
        capture_output=True, text=True,
    )
    assert top.returncode == 0, f"{target} 不在任何 git 仓库里，出处无法核验"
    repo = Path(top.stdout.strip())
    rel = target.relative_to(repo)
    rc = subprocess.run(["git", "-C", str(repo), "ls-files", "--error-unmatch", str(rel)], capture_output=True)
    assert rc.returncode == 0, f"{rel} 未入库（在 {repo}），干净 checkout 里这个出处会消失"


def test_documented_path_remaps_the_author_prefix(monkeypatch):
    """CI 里作者绝对路径不存在，必须靠约定前缀映射回当前 checkout；否则本模块不能进 CI。"""
    monkeypatch.setitem(globals(), "AUTHOR_ROOT", Path("/nonexistent-author-root"))
    token = "file:///nonexistent-author-root/docs/WORKFLOW.md"
    assert _resolve_documented_path(token) == ROOT / "docs" / "WORKFLOW.md"
    # 不在约定前缀下的路径不做补救，交给 exists() 报红。
    assert _resolve_documented_path("file:///elsewhere/docs/WORKFLOW.md") == Path("/elsewhere/docs/WORKFLOW.md")


def _make_target_block(name: str) -> str:
    lines = (ROOT / "Makefile").read_text(encoding="utf-8").splitlines()
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


def _expand_make_vars(block: str) -> str:
    """把配方里的 `$(USER_TIMERS)` / `$(RETIRED_TIMERS)` 按 Makefile 展开。"""
    text = (ROOT / "Makefile").read_text(encoding="utf-8")
    for name in ("USER_TIMERS", "RETIRED_TIMERS"):
        m = re.search(rf"^{name}\s*:=\s*((?:.*\\\n)*.*)$", text, re.M)
        assert m, f"Makefile 里没有 {name}"
        block = block.replace(
            f"$({name})", " ".join(m.group(1).replace("\\\n", " ").split())
        )
    return block


@pytest.mark.parametrize("target", ["timer-enable", "timer-disable", "timer-status", "status"])
def test_daban_ticket_is_in_the_ops_name_lists(target):
    """名单漏一个，一键收口就收不到它：停不掉、或状态里看不见。

    `status` 也在内——它第 457 行的 timer 快照用的是同一类 grep 名单，出事时人读的就是这页。
    """
    block = _expand_make_vars(_make_target_block(target))
    assert "daban-ticket" in block, (
        f"Makefile 的 {target} 里没有 daban-ticket。它按名字收口，"
        "不加进来 `make timer-disable` 停不掉、`make status` 也看不见这个 timer。"
    )
