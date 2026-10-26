"""回填 runner 的两条不可让渡性质：幂等、且不许把已有真 amount 覆盖成 0。

写盘全走 Writers.merge_write，它内部有三条保护（mergesort + keep='last' 去重、同日
amount 取 max、用合并结果重建 .day）；第四条 fill_missing_amount（给 amount<=0 的有量
行补估算值）2026-10-26 还只在别的工作树改动里，所以钉它的用例自己判接线、未接则 skip。
本文件钉的是 runner 这层：

- 重复跑同一只必须不再增行（回填要能 Ctrl-C 后续跑，靠的就是「重跑无害」）；
- 深取段（腾讯不吐成交额，amount=0）不得改变已有段的真 amount；
- 返回 0 行必须抛错，不能报成「补好了」——full_sync 的 skip 计入失败是同一条理由。
"""
from __future__ import annotations

import inspect

import pandas as pd
import pytest

from tdxfeed.backfill_history import backfill_symbol, load_done, mark_done
from tdxfeed.writers import Writers

# amount 估算层是不是真的接在写盘路径上：tdxfeed/amount_fix.py 与 writers.py 里
# 调用它的每一行，都来自另一个会话的工作树改动（2026-10-26 仍未入库）。所以不能靠
# ImportError 判——文件在但没接线，用例照样会红。读被装饰的函数体本身来判。
_AMOUNT_ESTIMATE_WIRED = "fill_missing_amount" in inspect.getsource(Writers.merge_write)


def bars(start, n, amount):
    d = pd.date_range(start, periods=n, freq="B")
    return [[int(x.strftime("%Y%m%d")), 1.0, 2.0, 0.5, 1.5, 100.0, amount] for x in d]


@pytest.fixture
def writers(tmp_path):
    return Writers(str(tmp_path))


def _p(tmp_path, sym="sz000021"):
    return tmp_path / "parquet" / "daily" / f"{sym}.parquet"


def test_second_run_adds_nothing(tmp_path, writers):
    deep, recent = bars("1996-01-02", 40, 0.0), bars("2026-01-02", 10, 5e8)
    r1 = backfill_symbol("sz000021", 20000101,
                         fetch=lambda s, t: deep + recent, writers=writers)
    before = pd.read_parquet(_p(tmp_path))
    r2 = backfill_symbol("sz000021", 20000101,
                         fetch=lambda s, t: deep + recent, writers=writers)
    after = pd.read_parquet(_p(tmp_path))
    assert r1["rows_added"] == len(deep) + len(recent)
    assert r2["rows_added"] == 0, "幂等：第二次不该再增行"
    assert len(after) == len(before)


def test_existing_amount_survives_deep_segment(tmp_path, writers):
    backfill_symbol("sz000021", 20000101,
                    fetch=lambda s, t: bars("2026-01-02", 5, 5e8), writers=writers)
    before = pd.read_parquet(_p(tmp_path))
    backfill_symbol("sz000021", 20000101,
                    fetch=lambda s, t: bars("1996-01-02", 30, 0.0), writers=writers)
    after = pd.read_parquet(_p(tmp_path))
    m = after[after["date"].isin(before["date"])]
    assert len(m) == len(before)
    assert (m["amount"] > 0).all(), "已有真实成交额不能被深取段的 0 抹掉"


@pytest.mark.skipif(not _AMOUNT_ESTIMATE_WIRED,
                    reason="amount 估算层（tdxfeed/amount_fix.py + writers.py 的调用）尚未入库")
def test_deep_segment_amount_is_estimated_not_zero(tmp_path, writers):
    """深取行 amount 恒 0 → merge_write 里 fill_missing_amount 估成 close×vol(手)×100。

    这条钉的是「深取段落地后 amount 不是 0」这个口径，但它依赖的估算层来自别的会话、
    2026-10-26 还没入库，所以未接线时是 skip 而不是红：假红会让干净 checkout 的 tests/
    不可信，而 skip 计数本身就是「估算层没落地」的可见信号。接线入库后自动生效。
    """
    backfill_symbol("sz000021", 20000101,
                    fetch=lambda s, t: bars("1996-01-02", 5, 0.0), writers=writers)
    df = pd.read_parquet(_p(tmp_path))
    assert list(df["amount"]) == pytest.approx([15000.0] * 5)


def test_empty_fetch_raises_not_silently_ok(tmp_path, writers):
    with pytest.raises(RuntimeError):
        backfill_symbol("sz000021", 20000101, fetch=lambda s, t: [], writers=writers)


def test_target_becomes_an_iso_want_from_for_the_fetcher(writers):
    """fetch_history 的 want_from 是 'YYYY-MM-DD'，runner 收到的是 int YYYYMMDD。"""
    seen = {}

    def fake_fetch(sym, want_from):
        seen["args"] = (sym, want_from)
        return bars("1996-01-02", 3, 0.0)

    backfill_symbol("sh600000", 20000101, fetch=fake_fetch, writers=writers)
    assert seen["args"] == ("sh600000", "2000-01-01")


def test_resume_done_set_roundtrip(tmp_path):
    p = str(tmp_path / ".state" / "history_backfill.json")
    assert load_done(p) == set()
    mark_done(p, "sh600000")
    mark_done(p, "sz000021")
    mark_done(p, "sh600000")           # 重复标记不该翻倍
    assert load_done(p) == {"sh600000", "sz000021"}


def _load_cli():
    import importlib.util
    from pathlib import Path

    fp = Path(__file__).resolve().parent.parent / "scripts" / "backfill_daily_history.py"
    espec = importlib.util.spec_from_file_location("backfill_cli_under_test", fp)
    mod = importlib.util.module_from_spec(espec)
    assert espec.loader
    espec.loader.exec_module(mod)
    return mod


def test_plan_drops_done_symbols_and_honours_limit():
    """续跑清单只减不增：已补过的不再拉一遍，非 extend 的绝不进清单。"""
    mod = _load_cli()
    audit = {"rows": [
        {"symbol": "sh600000", "state": "extend"},
        {"symbol": "sh600001", "state": "extend"},
        {"symbol": "sz000021", "state": "ok"},
        {"symbol": "sh600925", "state": "listed_after_target"},
        {"symbol": "sh601398", "state": "unknown"},
    ]}
    extend, todo = mod.plan(audit, {"sh600000"})
    assert extend == ["sh600000", "sh600001"]
    assert todo == ["sh600001"], "unknown/ok 不能混进回填清单"
    assert mod.plan(audit, set(), limit=1)[1] == ["sh600000"]


def test_cli_breaks_circuit_on_consecutive_channel_failures(tmp_path, monkeypatch, capsys):
    """连续失败是通道级故障，不是逐只的数据问题：2026-10-26 实测整段 IP 被 WAF 拦时
    每只要重试 2 台域 × 3 次（退避 30s）才抛，不熔断就是拿剩下的几千只继续砸被封的 IP。
    """
    import json
    import sys
    import time

    mod = _load_cli()
    audit = tmp_path / "history_depth_20261099.json"
    audit.write_text(json.dumps({"target": 20000101, "rows": [
        {"symbol": f"sh6000{n:02d}", "state": "extend"} for n in range(40)]}),
        encoding="utf-8")
    attempts = []

    def boom(symbol, target, *, writers=None):
        # 真一次深取是秒级 I/O；这里给 20ms 是为了让主线程来得及撤掉排队任务，
        # 否则瞬时返回的假函数会被 worker 一口气跑完，测不出熔断。
        time.sleep(0.02)
        attempts.append(symbol)
        raise RuntimeError("腾讯页请求失败：HTTP Error 501")

    monkeypatch.setattr(mod, "latest_audit", lambda: audit)
    monkeypatch.setattr(mod, "backfill_symbol", boom)
    monkeypatch.setattr(sys, "argv", ["backfill", "--workers", "1", "--resume",
                                      str(tmp_path / ".state" / "resume.json")])
    rc = mod.main()
    out = capsys.readouterr().out
    assert rc == 1, "熔断必须以非零退出，不能报成「今天不用补」"
    assert "ABORT" in out
    assert len(attempts) < 40, f"熔断后不该把 40 只全砸完，实际试了 {len(attempts)} 只"
    assert len(attempts) >= mod.TRIP_FAIL_STREAK
