"""baostock 登录超时/重试与因子任务心跳的守卫（不联网）。

对应台账 **KI-OPS-007**（登录握手被模块级 30 秒 socket 上限截断）与 **KI-OPS-004**
后半段（被截断的那一轮没有「还剩多少只」的计数）。

钉三件事：

1. **两个超时数是分开的，且登录那个必须容得下实测的慢握手**（2026-11-04：带
   30 秒上限 → 31 秒返回 `10002007 网络接收错误`；不设上限 → 60 秒成功）。
   只把 30 改成 120 是不行的——紧上限对逐只查询有意义（一只挂死的票不该拖住整轮）。
2. **两个因子模块不得再自己摸 socket 超时或裸调 `bs.login()`**：单源在
   `tdxfeed/baostock_session.py`。这条按接线判据写（查调用点，不查函数存在），
   否则下一版把 `login_with_retry` 换回 `bs.login()` 也能让别的断言继续绿。
3. **心跳的「还剩多少只」只有一处算式**，且两张表共用一份文件时互不覆盖。

只用 tmp_path 造状态文件，不读真实行情目录，无数据环境（CI）照常收集。
"""
from __future__ import annotations

import ast
import json
import socket
from pathlib import Path

import pytest

from tdxfeed import baostock_session as sess
from tdxfeed.factor_run_state import HEARTBEAT_EVERY, read_progress, write_progress

NEEDS_DATA = False

ROOT = Path(__file__).resolve().parents[1]
FACTOR_MODULES = ("tdxfeed/adj_factor.py", "tdxfeed/hfq_factor.py")


class _Res:
    def __init__(self, code, msg):
        self.error_code = code
        self.error_msg = msg


class FakeBs:
    """按脚本回放登录结果，并记录每次调用时的 socket 默认超时。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0
        self.timeouts = []
        self.logouts = 0

    def login(self):
        self.calls += 1
        self.timeouts.append(socket.getdefaulttimeout())
        item = self.script.pop(0) if self.script else self.script_last
        self.script_last = item
        if isinstance(item, Exception):
            raise item
        return item

    def logout(self):
        self.logouts += 1


@pytest.fixture
def restore_default_timeout():
    """login_with_retry 会改全局 socket 默认超时——测试必须自己收尾，别污染后续用例。"""
    before = socket.getdefaulttimeout()
    yield
    socket.setdefaulttimeout(before)


# ---------------- 1. 两个超时数 ----------------

def test_login_timeout_is_wider_than_query_timeout():
    assert sess.LOGIN_TIMEOUT_SEC > sess.QUERY_TIMEOUT_SEC, "登录与查询必须是两个数"
    # 实测慢握手 60 秒；给两倍。写死下界是为了让"顺手调小"必须同时改这条注释。
    assert sess.LOGIN_TIMEOUT_SEC >= 120
    # 逐只查询的紧上限保持改造前的 30 秒：一只挂死的票不能拖住整轮。
    assert sess.QUERY_TIMEOUT_SEC == 30


def test_login_runs_under_login_timeout_and_restores_query_timeout(restore_default_timeout):
    socket.setdefaulttimeout(sess.QUERY_TIMEOUT_SEC)
    bs = FakeBs([_Res("10002007", "网络接收错误。"), _Res("0", "success")])
    slept = []
    ok, msg = sess.login_with_retry(bs, sleep=slept.append)

    assert ok is True
    assert msg == "第 2/3 次成功"
    assert bs.timeouts == [sess.LOGIN_TIMEOUT_SEC, sess.LOGIN_TIMEOUT_SEC]
    assert socket.getdefaulttimeout() == sess.QUERY_TIMEOUT_SEC, "返回后必须回到紧上限"
    assert slept == [sess.LOGIN_BACKOFF_SEC[0]], "只在两次尝试之间退避一次"


def test_login_gives_up_after_bounded_attempts(restore_default_timeout):
    socket.setdefaulttimeout(sess.QUERY_TIMEOUT_SEC)
    bs = FakeBs([_Res("10002007", "网络接收错误。")] * 10)
    ok, msg = sess.login_with_retry(bs, sleep=lambda _s: None)

    assert ok is False
    assert bs.calls == sess.LOGIN_ATTEMPTS, "重试必须有界，不能无限握下去"
    assert msg == "网络接收错误。", "msg 要带最后一次的真实错误，供日志定位"
    assert socket.getdefaulttimeout() == sess.QUERY_TIMEOUT_SEC
    assert "已重试 3 次" in sess.describe_failure(sess.LOGIN_ATTEMPTS, msg)


def test_login_exception_is_counted_not_silent(restore_default_timeout):
    """登录抛异常（不是返回错误码）也要重试，且必须进 SEM002 计数——不许静默吞掉。"""
    socket.setdefaulttimeout(sess.QUERY_TIMEOUT_SEC)
    before = sess._ERROR_COUNT["login_with_retry"]
    bs = FakeBs([OSError("boom"), _Res("0", "success")])
    ok, _ = sess.login_with_retry(bs, sleep=lambda _s: None)

    assert ok is True
    assert sess._ERROR_COUNT["login_with_retry"] == before + 1
    assert bs.calls == 2, "异常那次要被重试覆盖，而不是直接放弃"


# ---------------- 2. 接线：两个因子模块不许各写一份 ----------------

@pytest.mark.parametrize("rel", FACTOR_MODULES)
def test_factor_modules_do_not_touch_socket_or_bare_login(rel):
    src = (ROOT / rel).read_text(encoding="utf-8")
    assert "socket.setdefaulttimeout(" not in src, \
        f"{rel} 不得自己设 socket 超时（单源在 baostock_session）"
    assert "import socket" not in src, f"{rel} 已不需要 socket，留着就是给下一版留的坑"

    tree = ast.parse(src)
    bare = [n for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr == "login" and isinstance(n.func.value, ast.Name)
            and n.func.value.id == "bs"]
    lines = [str(n.lineno) for n in bare]
    assert not bare, f"{rel} 仍有裸 bs.login()（{lines} 行）：必须走 login_with_retry"


@pytest.mark.parametrize("rel", FACTOR_MODULES)
def test_factor_modules_wire_retry_and_heartbeat(rel):
    """接线判据：只测 helper 存在没用，要测它真的被调用、心跳真的在三处落。"""
    src = (ROOT / rel).read_text(encoding="utf-8")
    assert "login_with_retry(bs)" in src, f"{rel} 首次登录必须走 login_with_retry"
    assert "re_ok, _ = login_with_retry(bs)" in src, f"{rel} 循环内重连也必须走同一个入口"
    assert src.count("write_progress(") >= 3, f"{rel} 心跳要覆盖 开跑/节流/收尾 三处"
    assert "未跑={max(target - processed, 0)}" in src, f"{rel} 完成行要自带「还剩多少只」"


# ---------------- 3. 心跳的算式与共存 ----------------

def test_unfinished_is_derived_from_target_minus_processed(tmp_path):
    p = tmp_path / "factor_run.json"
    rec = write_progress("adj_factor", today="2026-11-05", started_at="2026-11-04T21:00:00",
                         target=HEARTBEAT_EVERY * 3, processed=HEARTBEAT_EVERY,
                         ok=HEARTBEAT_EVERY, skip=0, fail=0, path=p)
    assert rec["unfinished"] == HEARTBEAT_EVERY * 2


def test_unfinished_never_negative(tmp_path):
    p = tmp_path / "factor_run.json"
    rec = write_progress("adj_factor", today="2026-11-05", started_at="x",
                         target=10, processed=12, ok=12, skip=0, fail=0, path=p)
    assert rec["unfinished"] == 0, "processed 越界时不许写出负数"


def test_two_tables_share_the_file_without_clobbering(tmp_path):
    p = tmp_path / "factor_run.json"
    write_progress("adj_factor", today="2026-11-05", started_at="a",
                   target=100, processed=100, ok=99, skip=1, fail=0, finished=True, path=p)
    write_progress("hfq_factor", today="2026-11-05", started_at="b",
                   target=100, processed=40, ok=38, skip=1, fail=1, path=p)

    disk = json.loads(p.read_text(encoding="utf-8"))
    assert set(disk) == {"adj_factor", "hfq_factor"}
    assert disk["adj_factor"]["finished"] is True
    assert disk["hfq_factor"]["finished"] is False
    assert disk["hfq_factor"]["unfinished"] == 60
    assert read_progress("adj_factor", path=p)["processed"] == 100


def test_read_progress_survives_a_corrupt_file(tmp_path):
    """心跳文件坏了不能把主任务带崩：降级成「读不出」，但必须进 SEM002 计数。"""
    from tdxfeed import factor_run_state as frs
    p = tmp_path / "factor_run.json"
    p.write_text("{ 这不是 json", encoding="utf-8")
    before = frs._ERROR_COUNT["_read"]
    assert read_progress("adj_factor", path=p) == {}
    assert frs._ERROR_COUNT["_read"] == before + 1


def test_read_progress_returns_empty_when_absent(tmp_path):
    assert read_progress("hfq_factor", path=tmp_path / "nope.json") == {}


# ---------------- 4. 心跳的落点可重定向（测试不许写真实 data/）----------------

def test_default_state_path_resolves_at_call_time(tmp_path, monkeypatch):
    """默认落点必须**在调用时**从 `tdxfeed.paths.factor_run_path()` 取。

    写成 `from tdxfeed.paths import factor_run_path` 就把名字绑死了，测试侧
    `monkeypatch.setattr(paths, ...)` 打不中，于是任何跑 `main()` 的用例都会
    往真实 `data/.state/` 里写心跳——`data/` 不在 git，谁也不会发现。
    """
    from tdxfeed import paths as tdx_paths

    target = tmp_path / "state" / "factor_run.json"
    monkeypatch.setattr(tdx_paths, "factor_run_path", lambda: target)
    write_progress("hfq_factor", today="2026-11-05", started_at="a",
                   target=5, processed=1, ok=1, skip=0, fail=0)

    assert target.exists(), "调用期解析没生效：心跳写到了真实目录"
    assert read_progress("hfq_factor")["processed"] == 1, "读侧同样要在调用期解析"


def test_main_level_tests_redirect_state():
    """凡是在测试里调这两个 `main()` 的文件，必须重定向心跳落点。"""
    offenders = []
    for py in sorted((ROOT / "tests").glob("test_*.py")):
        src = py.read_text(encoding="utf-8")
        calls_main = ("adj_factor.main(" in src or "hfq_factor.main(" in src
                      or 'm.main(["--code"' in src)
        if calls_main and "factor_run_path" not in src:
            offenders.append(py.name)
    assert not offenders, f"这些测试会往真实 data/.state/ 写心跳：{offenders}"
