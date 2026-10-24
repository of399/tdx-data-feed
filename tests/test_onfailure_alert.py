"""数据链路的失败可见性（2026-10-24）。

`tdxfeed-sync` 每晚 18:00 跑 30 分钟，`predict-daily` 靠 `After=`+`Wants=` 排在它后面。
`Wants=` 是弱依赖：sync 挂掉只会留在 logs/sync.err 里，predict 照样拿着隔夜数据出榜——
除非 `precheck_freshness.sh` 抽样恰好抽到变陈的那批票。日志不是告警，没人半夜翻 journal。

这里锁四件事：
1. predict 硬依赖 sync（`Requires=`），sync 起不来就别出榜。实测 sync 耗时 30+ 分钟，
   predict 的依赖作业会和在跑的那个合流，不会把全市场同步重复跑一遍（10-19~10-23 的
   journal 里 sync 每天只有一次 Starting，predict 都在 sync「Finished」的同一秒才开始）。
   反过来，探针单元验证了 deps 失败时 systemd 会给出
   `Dependency failed for …` 并且 dependent 完全不跑。
2. 三个数据链路单元失败时投递告警（`OnFailure=onfailure-alert@<单位>.service`），
   且同单元有一小时冷却——canary 每 5 分钟一次，没冷却就是每小时 12 条微信。
3. 告警载荷必须能被现存的 pushplus 桥接受（`v5/scripts/pushplus_bridge.py` 读的是
   alertmanager 形状：alerts[].labels.{alertname,service,severity} + annotations +
   status + startsAt）。这条用真桥的转换函数验，不靠猜字段名。
4. sync 单元不许加 `RemainAfterExit=yes`：那会让次日 timer 的 start job 空转，
   数据管道静默停摆（探针实测 ExecMainPID 不变、journal 无新条目）。

不发真实微信推送：所有测试只跑 `--dry-run`、纯函数或本地假 URL。
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
import urllib.error
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT), str(ROOT / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _load_script():
    spec = importlib.util.spec_from_file_location(
        "onfailure_alert_under_test", ROOT / "scripts" / "onfailure_alert.py"
    )
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _unit(name: str) -> str:
    return (ROOT / "systemd" / name).read_text(encoding="utf-8")


def _opt_lines(text: str, key: str) -> list[str]:
    return [ln.strip() for ln in text.splitlines() if ln.strip().startswith(f"{key}=")]


def _values(text: str, key: str) -> set[str]:
    return {ln.split("=", 1)[1].strip() for ln in _opt_lines(text, key)}


# ---------- 1. predict 硬依赖 sync ----------

def test_predict_daily_hard_requires_sync():
    """Wants= 只尽力拉起，失败不拦；sync 挂了 predict 仍会用隔夜数据出榜。"""
    text = _unit("predict-daily.service")
    assert "tdxfeed-sync.service" in _values(text, "Requires"), \
        "predict 对 sync 仍是弱依赖，同步失败照样跑"
    assert "tdxfeed-sync.service" not in _values(text, "Wants"), \
        "Requires= 之外还留着 Wants=，读不出到底硬不硬"
    assert "tdxfeed-sync.service" in _values(text, "After"), \
        "去掉排序会并发起跑，Requires= 变成抢锁"


# ---------- 2. 失败要投递告警 ----------

ALERT_TARGETS = (
    "tdxfeed-sync.service",
    "predict-daily.service",
    "canary-health.service",
)


@pytest.mark.parametrize("unit_name", ALERT_TARGETS)
def test_pipeline_units_alert_on_failure(unit_name):
    """canary 自己是告警员，它静默死掉时最需要有人喊——所以也在名单里。"""
    text = _unit(unit_name)
    expected = f"onfailure-alert@{unit_name}"
    assert expected in _values(text, "OnFailure"), f"{unit_name} 失败只会落进日志"


def test_alert_template_is_oneshot_and_cannot_loop():
    """模板要是带 Restart= 或自己的 OnFailure=，一次失败就变成无限告警。"""
    text = _unit("onfailure-alert@.service")
    assert "oneshot" in _values(text, "Type")
    assert not _opt_lines(text, "Restart"), "告警单元自己重启会把一次故障刷成刷屏"
    assert not _opt_lines(text, "OnFailure"), "告警单元再挂一层 OnFailure 就是自反馈环"
    assert "%i" in text, "没把失败单元名传给脚本，告警里说不出是谁挂了"
    assert "scripts/onfailure_alert.py" in text


def test_sync_unit_keeps_no_remain_after_exit():
    """护栏（当前成立，不是一条先红后绿的功能测试）：探针实测给 oneshot 加
    RemainAfterExit=yes 后，重复 start 是空转（ExecMainPID 不变、journal 无新条目）。
    sync 要是从此一直 active，次日 18:00 的 timer 就不再真的同步——数据管道静默停摆，
    比现在「sync 失败没人知道」更糟。
    """
    assert "RemainAfterExit" not in _unit("tdxfeed-sync.service")


# ---------- 3. 载荷与现存桥的契约 ----------

def _bridge():
    """桥在 import 时没 token 就 sys.exit，所以先塞一个假的；测试不发网络。"""
    import os

    os.environ.setdefault("PUSHPLUS_TOKEN", "dummy-token-for-import")
    spec = importlib.util.spec_from_file_location(
        "pushplus_bridge_under_test", ROOT / "v5" / "scripts" / "pushplus_bridge.py"
    )
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_payload_survives_the_real_bridge_conversion():
    """不猜字段名：把载荷喂进桥自己的转换函数，看它能不能排出标题和正文。"""
    bridge = _bridge()
    al = _load_script()
    payload = al.build_payload("tdxfeed-sync.service", {"result": "exit-code", "status": "2"},
                              ["sync failed: sh600000 | all sources down"],
                              when="2026-10-24T18:31:12+08:00")
    out = bridge._build_pushplus_payload(payload)
    assert "FIRING" in out["title"], "桥没把它当触发中的告警"
    assert "tdxfeed-sync" in out["title"] + out["content"]
    assert "sync failed" in out["content"], "日志尾巴没进正文"
    assert out["template"] == "html"


def test_payload_declares_the_labels_the_bridge_formats():
    al = _load_script()
    payload = al.build_payload("predict-daily.service", {"result": "resources"}, ["boom"],
                              when="2026-10-24T18:05:00+08:00")
    alert = payload["alerts"][0]
    assert alert["labels"]["alertname"]
    assert alert["labels"]["service"] == "predict-daily.service"
    assert alert["labels"]["severity"] == "critical"
    assert alert["status"] == "firing"
    assert alert["startsAt"] == "2026-10-24T18:05:00+08:00"
    assert alert["annotations"]["summary"] and alert["annotations"]["description"]
    assert payload["commonLabels"]["alertname"] == alert["labels"]["alertname"]


# ---------- 脚本自身的取材与投递 ----------

def test_show_output_is_parsed():
    """`systemctl show` 的键是 ExecMainCode/Result/…，脚本要说得出死法。"""
    al = _load_script()
    info = al.parse_show(
        "Result=exit-code\nActiveState=inactive\nSubState=failed\n"
        "ExecMainCode=1\nExecMainStatus=2\nNRestarts=0\n"
    )
    assert info == {"result": "exit-code", "active_state": "inactive",
                    "sub_state": "failed", "exec_code": "1", "status": "2"}


def test_journal_tail_command_is_scoped_and_capped():
    """取日志必须走 --user（这些是用户单元），且要有行数上限，别把整份日志塞进推送。"""
    al = _load_script()
    cmd = al.journal_tail_cmd("tdxfeed-sync.service", lines=8)
    assert cmd[:2] == ["journalctl", "--user"]
    assert "-u" in cmd and "tdxfeed-sync.service" in cmd
    assert cmd[cmd.index("-n") + 1] == "8"


def test_post_alert_returns_failure_instead_of_raising():
    """投递失败要能被上层看见（非零退出），但不能抛栈——OnFailure 单元自己崩了更难查。"""
    al = _load_script()
    ok, detail = al.post_alert({"alerts": []}, url="http://127.0.0.1:1/pushplus", timeout=2)
    assert ok is False
    assert detail, "失败了却说不出原因"


def test_dry_run_touches_no_network(monkeypatch):
    """dry-run 是给「改了告警格式但不想刷屏」用的，它必须真的一条都不发。"""
    al = _load_script()

    def boom(*a, **k):
        raise AssertionError("dry-run 里发生了网络请求")

    monkeypatch.setattr(al.urllib.request, "urlopen", boom)
    rc = al.main(["tdxfeed-sync.service", "--dry-run"])
    assert rc == 0


def test_send_failure_exits_nonzero(tmp_path, monkeypatch, capsys):
    """退出码非零才会留在 journal 里；静默 exit 0 等于告警系统自己也得了一次病。"""
    al = _load_script()

    def refuse(*a, **k):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(al.urllib.request, "urlopen", refuse)
    rc = al.main(["tdxfeed-sync.service", "--url", "http://127.0.0.1:1/pushplus"])
    assert rc != 0
    assert "tdxfeed-sync.service" in capsys.readouterr().out


def test_alert_script_gathers_state_from_the_failing_unit(monkeypatch):
    """单元名 → systemctl show + journalctl 尾巴，都用真实命令；这里只替换命令输出。"""
    al = _load_script()

    fake = {
        ("systemctl", "--user", "show"): "Result=exit-code\nSubState=failed\nExecMainStatus=2\n",
        ("journalctl", "--user"): "sync failed: sh600000 | all sources down\n",
    }

    def fake_run(cmd, *a, **k):
        for prefix, out in fake.items():
            if tuple(cmd[: len(prefix)]) == prefix:
                return subprocess.CompletedProcess(cmd, 0, out, "")
        raise AssertionError(f" unexpected command {cmd}")

    monkeypatch.setattr(subprocess, "run", fake_run)
    info, tail = al.collect_state("tdxfeed-sync.service", lines=8)
    assert info["status"] == "2"
    assert tail == ["sync failed: sh600000 | all sources down"]


def test_repeat_alerts_are_throttled(tmp_path, monkeypatch, capsys):
    """canary 每 5 分钟一次，不加节流会把一次故障刷成一小时 12 条微信。
    冷却只压重复，不压新单元：不同 unit 各自一份标记。
    """
    al = _load_script()
    sent = []
    monkeypatch.setattr(al, "post_alert",
                        lambda payload, url, timeout=5: (sent.append(url), (True, "ok"))[1])
    argv = ["canary-health.service", "--state-dir", str(tmp_path),
            "--cooldown", "3600", "--url", "http://127.0.0.1:5002/pushplus"]

    assert al.main(argv) == 0
    rc = al.main(argv)
    assert len(sent) == 1, "冷却期内又投递了一次，故障期间必然刷屏"
    assert rc == 0, "被节流不是错误，别让它自己变成一条失败记录"
    assert "跳过" in capsys.readouterr().out

    assert al.main(["tdxfeed-sync.service", "--state-dir", str(tmp_path),
                    "--cooldown", "3600"]) == 0
    assert len(sent) == 2, "换了单元还被上一次的标记压住"
