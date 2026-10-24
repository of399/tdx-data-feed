"""翻倍样本刷新：timer 单元 + 生产者守卫。

2026-10-24 彩排盘前链路时查出来的缺口：没有任何 timer 重建
`data/double_up/double_up_2020_2026_detail.csv`，生产者是手工脚本
`scripts/find_double_up_c.py`（`make analyze-double-up`）。`plan-e-today` 的 7 日预筛
只看得见这份 CSV 里已有的触发日，所以新触发日不重建样本就永远进不了清单。

当天 12:57 那次手工刷新把口径改掉了一大块：22972 行 → 17581 行，其中北交所
4213 行**整段归零**（345 只消失的标的里 216 只是 bj，281 只的 parquet 已不存在）。
脚本对这件事一声不吭 —— `if not fp.exists(): return []` 与
`except Exception: return []` 都静默跳过，零命中也照样 `return`（退出码 0）。
把它定时化之前，先要它不会假成功。

锁四件事：
1. `systemd/plan-e-double-up.{service,timer}` 存在，排在 18:00 sync 与 18:10 journal
   之后（`Persistent=true`：全量重算，补跑无害且必要）；
2. 生产者把零命中、读失败的 parquet、以及**任一市场段从有到无**都当成失败，
   非零退出且不覆盖旧样本；
3. 落盘走临时文件 + `os.replace`，不留半截 CSV；
4. 脚本自身不再有裸 `to_csv(生产路径)`。

`tests/test_plan_e_live_retirement.py` 的 `PLAN_E_TIMERS` 是从
`systemd/plan-e-*.timer` glob 推出来的，新单元落地后它会自动要求 Makefile 的
enable/disable 列表与三份文档都点名它。
"""
from __future__ import annotations

import importlib.util
import inspect
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
SYSTEMD = ROOT / "systemd"


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / filename)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def producer():
    return _load("find_double_up_c_guard_test", "find_double_up_c.py")


def _unit(path: Path) -> str:
    assert path.exists(), f"缺少单元文件 {path.relative_to(ROOT)}"
    return path.read_text(encoding="utf-8")


def _on_calendar(text: str) -> str:
    for line in text.splitlines():
        if line.strip().startswith("OnCalendar="):
            return line.split("=", 1)[1].strip()
    raise AssertionError("单元里没有 OnCalendar")


# ---------- 1. 单元文件与调度时刻 ----------

def test_double_up_unit_files_exist():
    _unit(SYSTEMD / "plan-e-double-up.service")
    _unit(SYSTEMD / "plan-e-double-up.timer")


def test_double_up_timer_is_a_weekday_catchup_timer():
    """工作日盘后刷新 + Persistent=true。

    盘前 08:45 用的样本必须来自前一交易日晚上，所以刷新排在收盘数据落地之后；
    Persistent 的补跑对全量重算是安全的（幂等），错过一天不该让清单口径停在上周五。
    """
    timer = _unit(SYSTEMD / "plan-e-double-up.timer")
    assert _on_calendar(timer) == "Mon..Fri 18:30:00", \
        f"刷新时刻应为工作日 18:30，实际 {_on_calendar(timer)}"
    assert "Persistent=true" in timer, "错过调度的机器开机后应补跑，而不是等下一个工作日"


def test_double_up_timer_runs_after_sync_and_journal():
    """18:00 sync 写当日 K 线、18:10 journal 读同一份 CSV，刷新不能插到中间。"""
    mine = _on_calendar(_unit(SYSTEMD / "plan-e-double-up.timer")).split()[1]
    for other, want_later in (("tdxfeed-sync.timer", True), ("plan-e-journal.timer", True)):
        theirs = _on_calendar(_unit(SYSTEMD / other)).split()[1]
        if want_later:
            assert mine > theirs, f"plan-e-double-up 应在 {other}（{theirs}）之后，实际 {mine}"


def test_double_up_service_wires_freshness_precheck_and_alert():
    svc = _unit(SYSTEMD / "plan-e-double-up.service")
    assert "precheck_freshness.sh" in svc, "样本重建必须先过数据新鲜度守卫，否则拿停滞数据重算"
    assert "find_double_up_c.py" in svc, "ExecStart 应跑样本生产者"
    assert "onfailure-alert@plan-e-double-up.service" in svc, \
        "刷新失败要能告警——它失败原先是静默的（写法和 predict-daily/tdxfeed-sync 一致）"


# ---------- 2. 生产者守卫：三种失败模式 ----------

def _sample(codes: list[str]) -> pd.DataFrame:
    return pd.DataFrame({"code": codes, "peak_date": ["2026-10-23"] * len(codes)})


def test_market_counts_splits_by_exchange_prefix(producer):
    df = _sample(["sh600000", "sz000001", "bj920001", "bj430047"])
    assert producer.market_counts(df) == {"sh": 1, "sz": 1, "bj": 2}


def test_guard_rejects_zero_hits(producer):
    """零命中不再是「打印一行 ❌ 然后退出 0」。"""
    old = _sample(["sh600000", "sz000001"])
    with pytest.raises(SystemExit) as exc:
        producer.assert_sample_ok(old, _sample([]))
    assert exc.value.code != 0, "零命中必须非零退出"


def test_guard_rejects_a_whole_market_segment_disappearing(producer):
    """北交所 4213 行 → 0 行必须被拦下：那是 281 只 parquet 不见了，不是市场没行情。"""
    old = _sample(["sh600000", "sz000001", "bj920001", "bj920002"])
    new = _sample(["sh600121", "sz000002"])
    with pytest.raises(SystemExit) as exc:
        producer.assert_sample_ok(old, new)
    assert exc.value.code != 0


def test_guard_passes_normal_churn_and_first_run(producer):
    """行数涨跌、标的换血都放行；旧样本不存在（首次刷新）时无从比较。"""
    old = _sample(["sh600000", "sz000001", "bj920001"])
    producer.assert_sample_ok(old, _sample(["sh600121", "sz000002", "bj920002", "bj430047"]))
    producer.assert_sample_ok(None, _sample(["sh600000"]))


def test_guard_reports_which_segment_died(producer, capsys):
    old = _sample(["sh600000", "bj920001"])
    with pytest.raises(SystemExit):
        producer.assert_sample_ok(old, _sample(["sh600121"]))
    printed = capsys.readouterr().out
    assert "bj" in printed, "要说清是哪一段归零，否则只能靠人肉比对两版 CSV"


# ---------- 3. 读失败要计数，不能静默跳过 ----------

def test_read_one_returns_none_on_unreadable_file(producer, tmp_path):
    """parquet 在但读不出来 = schema 漂移，属于要报告的成功率损失。"""
    bad = tmp_path / "sh600999.parquet"
    bad.write_bytes(b"not a parquet at all")
    assert producer.read_one(bad) is None


def test_read_one_parses_iso_and_int_dates(producer, tmp_path):
    good = tmp_path / "sh600998.parquet"
    pd.DataFrame({
        "date": pd.to_datetime(["2026-10-22", "2026-10-23"]),
        "open": [1.0, 2.0], "high": [1.0, 2.2], "low": [0.9, 1.9],
        "close": [1.0, 2.1], "volume": [100, 200], "amount": [100.0, 420.0],
    }).to_parquet(good, index=False)
    df = producer.read_one(good)
    assert df is not None and str(df["date"].iloc[-1])[:10] == "2026-10-23"


def test_screen_one_reports_unreadable_instead_of_swallowing_it(producer, tmp_path, monkeypatch):
    """screen_one 必须把「文件在但读不出来」和「没有文件」分开：前者是池在缩水。"""
    monkeypatch.setattr(producer, "PARQUET", tmp_path)
    (tmp_path / "sh600999.parquet").write_bytes(b"not a parquet at all")
    assert producer.screen_one("sh600999", {}, set()) is None, "读失败被当成无命中静默跳过"
    assert producer.screen_one("sh600998", {}, set()) == [], "退市/未下载不该算故障"


def test_main_trips_on_unreadable_masses(producer):
    """零星脏文件放行，成片的读不出来要非零退出，否则 universe 缩水以「刷新成功」的面目出现。"""
    src = inspect.getsource(producer.main)
    assert "unreadable" in src and "MAX_UNREADABLE" in src, "main 没有把读失败数量写进退出判定"
    assert "assert_sample_ok" in src, "main 没有调用样本守卫"


def test_guard_runs_before_the_sample_is_overwritten(producer):
    """顺序是硬的：守卫不过就不许覆写生产 CSV。"""
    src = inspect.getsource(producer.main)
    assert src.index("assert_sample_ok") < src.index("write_atomic"), \
        "先覆写再守卫等于把坏样本已经写进去了"


# ---------- 4. 原子落盘 ----------

def test_write_atomic_leaves_no_temp_file(producer, tmp_path):
    target = tmp_path / "double_up_2020_2026_detail.csv"
    producer.write_atomic(_sample(["sh600000", "bj920001"]), target)
    assert target.exists()
    assert list(tmp_path.glob("*.tmp")) == [], "临时文件必须被 rename 掉"
    assert len(pd.read_csv(target)) == 2


def test_producer_no_longer_writes_the_sample_in_place(producer):
    src = inspect.getsource(producer)
    assert "df_hits.to_csv(csv_path" not in src, "仍在就地覆写生产 CSV"


def test_producer_target_is_the_sample_today_list_reads(producer):
    """单元跑的那个脚本，产出的必须正是 `plan_e_backtest.py` 读的那份文件。

    刷新错一份没人读的 CSV，timer 会天天成功而清单永远停在旧口径。
    """
    assert producer.DOUBLE_UP_DETAIL.name == "double_up_2020_2026_detail.csv"
    backtest_src = (ROOT / "scripts" / "plan_e_backtest.py").read_text(encoding="utf-8")
    assert "DOUBLE_UP_CSV = ROOT / 'data' / 'double_up' / 'double_up_2020_2026_detail.csv'" \
        in backtest_src, "消费端读的文件名变了，刷新链路要跟着改"


# ---------- 5. 覆盖缺口必须成文：决定已定，别让下一个人当新 bug 重开 ----------

_LIVE_DOC = ROOT / "docs" / "PLAN_E_LIVE.md"
_BACKUP_DIR = ROOT / "data" / "double_up" / "_pre_refresh_20261024"


def test_bj_coverage_gap_is_written_into_the_live_doc():
    """2026-10-24 用户选的是「先只报告，不动策略口径」，那条报告当时只活在提交信息里。

    没成文的决定等于没做过：下一个读到 bj 段为 0 的人只会当成新 bug 重开讨论，
    或者更糟——直接改策略代码让清单"看起来"只剩沪深。
    """
    text = _LIVE_DOC.read_text(encoding="utf-8")
    missing = [
        anchor
        for anchor in ("北交所", "4213", "full_sync", "不动策略口径", "追溯")
        if anchor not in text
    ]
    assert not missing, f"docs/PLAN_E_LIVE.md 缺锚点 {missing}（北交所覆盖缺口未成文）"


def test_documented_bj_numbers_reproduce_from_the_sample_backup(producer):
    """文档里的三个数必须是同一件事的三个口径，不能像提交信息那样把 281 说成 bj 的数。

    实测：bj 段 4213 行、bj 标的 216 只全灭、旧样本 1942 只标的里共 281 只 parquet 已不存在。
    """
    if not _BACKUP_DIR.exists():
        pytest.skip("样本备份在 gitignore 的 data/ 里，换机器可能没有")
    detail = pd.read_csv(_BACKUP_DIR / "double_up_2020_2026_detail.csv")
    by_code = pd.read_csv(_BACKUP_DIR / "double_up_2020_2026_by_code.csv", encoding="utf-8-sig")
    row_counts = producer.market_counts(detail)
    stock_counts = producer.market_counts(by_code)
    assert row_counts["bj"] == 4213
    assert stock_counts["bj"] == 216

    text = _LIVE_DOC.read_text(encoding="utf-8")
    assert str(row_counts["bj"]) in text, "文档没写 bj 段归零的行数 4213"
    assert str(stock_counts["bj"]) in text, "文档没写 bj 标的数 216"
    assert "281" in text, "文档没写 parquet 缺失的标的总数 281"
    for wrong in ("281 只 bj", "281 只北交所", "216 只 parquet"):
        assert wrong not in text, f"文档里「{wrong}」是口径错配：281 属缺失总数，216 属 bj 标的数"
