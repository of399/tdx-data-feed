"""canary 尾部新鲜度（data_files_tail）与解析覆盖率（data_coverage）。

关联台账 KI-OPS-005：`sample_latest_date` 返回**固定 30 只抽样的中位数日期**，
某一只票整表停更时中位数纹丝不动——坏行/停更永远落在盲区。本文件钉住补上来的两维：

  1. `tdxfeed.freshness.table_freshness()` 全量逐文件末日期 →
     {total, parsed, latest, oldest, stale_count, stale_ratio, threshold, examples}
  2. canary 维度 `data_files_tail`（只看 A 股个股、排除 .bak，容忍 1 个交易日）
  3. canary 维度 `data_coverage`（解析失败比例 > 1% ⇒ warn）
  4. **翻页决策**：两维是否进 PAGING_CHECKS 由代码里的实测常量 TAIL_PAGING_ENABLED 决定，
     测试据该常量断言一致性（不许绕过实测直接翻开）。

只用 tmp_path 造表，不读真实 data/（CI 无数据环境照常收集），不发网络。
"""
from __future__ import annotations

import ast
import importlib.util
import re
from datetime import date
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

ROOT = Path(__file__).resolve().parents[1]
CANARY_SRC = ROOT / "scripts" / "canary_health_check.py"

# 本模块只用 tmp_path 造表，不读真实行情目录，无数据环境（CI）照常收集。
NEEDS_DATA = False

# ref=2026-10-29（周四）→ 最近已收盘交易日 10-28；
# allowed_trading_days=1 的阈值 = 10-27，=3 的阈值 = 10-23（10-24/25 是周末）。
REF = date(2026, 10, 29)
FRESH = "2026-10-28"      # 与最近已收盘交易日同天
T1 = "2026-10-27"         # 滞后 1 个交易日 == 阈值：正常态，不算陈旧
LAG3 = "2026-10-23"       # 滞后 3 个交易日：必须进 stale_count

# 规格钉死的两维名字（也是翻页开关作用的对象）
TAIL_NAMES = frozenset({"data_files_tail", "data_coverage"})
_DATE32 = pa.date32()        # B008：别把函数调用当默认参数值


def _canary():
    spec = importlib.util.spec_from_file_location(
        "canary_tail_fresh", CANARY_SRC
    )
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _write(path: Path, last: str, n_extra: int = 0,
           dtype: pa.DataType = _DATE32) -> Path:
    """写最小 parquet（默认 date32[day]，与生产表同 dtype），返回该路径。

    n_extra 在 `last` 之前追加若干天，使「末行」与「最大值」可被分别验证。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    ts = list(pd.date_range(end=pd.Timestamp(last), periods=n_extra + 1, freq="D"))
    values = [t.strftime("%Y-%m-%d") for t in ts] if dtype == pa.string() else ts
    pq.write_table(pa.table({"date": pa.array(values, dtype)}), path)
    return path


def _garbage(path: Path) -> Path:
    """扩展名是 parquet、但 footer 读不出来的文件：模拟截断/损坏残留。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"not a parquet file at all")
    return path


# ---------------------------------------------------------------- table_freshness

def test_table_freshness_fields(tmp_path):
    from tdxfeed.freshness import table_freshness

    files = [_write(tmp_path / f"sh60000{i}.parquet", FRESH) for i in range(5)]
    files.append(_write(tmp_path / "sh600999.parquet", LAG3, n_extra=3))

    r = table_freshness(files, REF, allowed_trading_days=1)

    assert r["total"] == 6
    assert r["parsed"] == 6
    assert r["threshold"] == date(2026, 10, 27)
    assert r["latest"] == date(2026, 10, 28)
    assert r["oldest"] == date(2026, 10, 23)
    assert r["stale_count"] == 1
    assert r["stale_ratio"] == pytest.approx(1 / 6)
    # examples 是「滞后代码」，取文件名去后缀形态（保留 sh/sz 前缀：sh000001 是指数、
    # sz000001 是平安银行，normalize 后会同码撞车，所以不能剥前缀）。
    assert r["examples"] == ["sh600999"]


def test_table_freshness_tolerates_one_trading_day_lag(tmp_path):
    """与既有 data_files 口径一致：末日期 == 阈值不算陈旧，早一个交易日才算。"""
    from tdxfeed.freshness import table_freshness

    ok = _write(tmp_path / "sh600000.parquet", T1)        # == threshold 2026-10-27
    bad = _write(tmp_path / "sh600076.parquet", "2026-10-26")
    r = table_freshness([ok, bad], REF, allowed_trading_days=1)
    assert r["stale_count"] == 1, r
    assert r["examples"] == ["sh600076"]


def test_table_freshness_examples_capped_five_worst_first(tmp_path):
    from tdxfeed.freshness import table_freshness

    dates = ["2026-09-01", "2026-10-20", "2026-10-21",
             "2026-10-22", "2026-10-19", "2026-08-05"]
    files = [_write(tmp_path / f"sz0000{i}.parquet", d) for i, d in enumerate(dates)]
    r = table_freshness(files, REF, allowed_trading_days=1)
    assert r["stale_count"] == 6
    assert len(r["examples"]) == 5
    # 最滞后在最前；被截断的那条（10-22，最不严重）不该挤进例子
    assert r["examples"] == ["sz00005", "sz00000", "sz00004", "sz00001", "sz00002"]


def test_table_freshness_unparsable_counted_in_total_not_in_stale(tmp_path):
    """读不出日期的文件不进 stale_count（它没有可比较的日期），只压低 parsed。

    两维因此正交：停更归 stale_count，读不了归 data_coverage——一个坏文件同时
    点亮两维就是重复告警。
    """
    from tdxfeed.freshness import table_freshness

    files = [_write(tmp_path / "sh600000.parquet", FRESH),
             _garbage(tmp_path / "sh600076.parquet")]
    r = table_freshness(files, REF, allowed_trading_days=1)
    assert r["total"] == 2
    assert r["parsed"] == 1
    assert r["stale_count"] == 0, r
    assert r["latest"] == date(2026, 10, 28)
    assert r["oldest"] == date(2026, 10, 28)
    assert r["examples"] == []


def test_table_freshness_empty_table(tmp_path):
    from tdxfeed.freshness import table_freshness

    r = table_freshness([], REF, allowed_trading_days=1)
    assert r["total"] == 0 and r["parsed"] == 0
    assert r["stale_count"] == 0 and r["stale_ratio"] == 0.0
    assert r["latest"] is None and r["oldest"] is None
    assert r["examples"] == []


def test_table_freshness_uses_max_across_row_groups(tmp_path):
    """footer 统计是分 row group 的：只读第一个 row group 会把末日期读成 5 年前。"""
    from tdxfeed.freshness import table_freshness

    p = tmp_path / "sh600002.parquet"
    p.parent.mkdir(parents=True, exist_ok=True)
    days = pd.date_range("2020-01-01", periods=2000, freq="D")
    pq.write_table(pa.table({"date": pa.array(days, pa.date32())}), p, row_group_size=200)
    first_rg_max = (pd.Timestamp(days[199]).date())

    r = table_freshness([p], REF, allowed_trading_days=1)
    assert r["parsed"] == 1
    assert r["latest"] == days[-1].date(), r
    assert r["latest"] != first_rg_max, "只吃了第一个 row group"
    # 末日期 2025-07 早于阈值 2026-10-27：这段历史尾部确实「停更」，计数要如实
    assert r["stale_count"] == 1, r


@pytest.mark.parametrize("dtype", [pa.date32(), pa.timestamp("ns"), pa.string()])
def test_table_freshness_handles_date_dtype_drift(tmp_path, dtype):
    """生产表口径不一：daily/adj/hfq 是 date32[day]，抽样测试与旧脚本会落 timestamp，
    string 也有（分钟表时间列）——实现都要吃得下或安全回退。"""
    from tdxfeed.freshness import table_freshness

    p = _write(tmp_path / f"sh600{re.sub(r'\\W', '', str(dtype))[:8]}.parquet",
               FRESH, dtype=dtype)
    r = table_freshness([p], REF, allowed_trading_days=1)
    assert r["parsed"] == 1, r
    assert r["latest"] == date(2026, 10, 28), r
    assert r["stale_count"] == 0


def test_table_freshness_int_yyyymmdd_falls_back(tmp_path):
    """int64 YYYYMMDD：footer 统计给的是 20261028，不能当 epoch days 解释成 1970 年。"""
    from tdxfeed.freshness import table_freshness

    p = tmp_path / "sh600003.parquet"
    p.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"date": pd.Series([20261026, 20261028], dtype="int64")}).to_parquet(p, index=False)
    r = table_freshness([p], REF, allowed_trading_days=1)
    assert r["parsed"] == 1, r
    assert r["latest"] == date(2026, 10, 28), r


def test_sample_latest_date_unchanged(tmp_path):
    """不得改变现有函数行为：抽样中位数仍是中位数（KI-OPS-005 的盲区本身要留着当对照）。"""
    from tdxfeed.freshness import sample_latest_date, table_freshness

    files = [_write(tmp_path / f"sh6000{i}.parquet", d)
             for i, d in enumerate([FRESH, FRESH, FRESH, FRESH, LAG3])]
    # 中位数看不见那一只停更的（正是盲区）
    assert sample_latest_date(files, sample_size=5) == date(2026, 10, 28)
    # 全量尾部看得见
    assert table_freshness(files, REF, allowed_trading_days=1)["stale_count"] == 1


# ---------------------------------------------------------------- canary 两维

def _dirs(tmp_path):
    daily = tmp_path / "daily"
    for i in range(5):
        _write(daily / f"sh60000{i}.parquet", FRESH)
    return daily


def _wire_tail(m, monkeypatch, daily):
    monkeypatch.setattr(m, "_today", lambda: REF)
    monkeypatch.setattr(m, "daily_dir", lambda: daily)


def test_tail_files_are_a_share_stocks_only(tmp_path, monkeypatch):
    """ETF/LOF（159/51/56）、北交所（920/8）与 .v1.bak 迁移备份都不该进尾部统计。"""
    m = _canary()
    daily = _dirs(tmp_path)
    _write(daily / "sh159915.parquet", "2026-01-05")         # ETF：长期滞后但不算
    _write(daily / "bj920026.parquet", "2026-01-05")         # 北交所：同上
    _write(daily / "sh600999.v1.bak.parquet", "2026-01-05")  # 迁移备份：同上
    _wire_tail(m, monkeypatch, daily)

    r = m.check_data_files_tail()
    assert r["name"] == "data_files_tail"
    assert r["status"] == "ok", r
    assert r["details"]["total"] == 5, r["details"]
    assert r["details"]["stale_count"] == 0, r["details"]


def test_data_files_tail_warns_with_stale_count_and_examples(tmp_path, monkeypatch):
    """整表停更的一只票：中位数看不见，尾部统计必须点名。"""
    m = _canary()
    daily = _dirs(tmp_path)
    _write(daily / "sh601999.parquet", LAG3)
    _wire_tail(m, monkeypatch, daily)

    r = m.check_data_files_tail()
    assert r["status"] == "warn", r
    assert r["details"]["stale_count"] == 1, r["details"]
    assert "sh601999" in r["details"]["examples"], r["details"]
    assert "sh601999" in r["message"], r["message"]
    assert "stale_count=1" in r["message"], r["message"]


def test_data_files_tail_ok_when_all_advance(tmp_path, monkeypatch):
    m = _canary()
    daily = _dirs(tmp_path)
    _write(daily / "sh601999.parquet", T1)   # T-1 是正常态（后置任务结构性只能追到 T-1）
    _wire_tail(m, monkeypatch, daily)
    assert m.check_data_files_tail()["status"] == "ok"


def test_data_files_tail_missing_dir_is_error(tmp_path, monkeypatch):
    """目录不存在 = 数据链整段没跑，这是 error 不是 ok。"""
    m = _canary()
    _wire_tail(m, monkeypatch, tmp_path / "nowhere")
    r = m.check_data_files_tail()
    assert r["status"] == "error", r


def test_data_coverage_warns_above_one_percent(tmp_path, monkeypatch):
    m = _canary()
    daily = tmp_path / "daily"
    for i in range(100):
        _write(daily / f"sh60{i:04d}.parquet", FRESH)
    _wire_tail(m, monkeypatch, daily)

    # 1/101 = 0.99% ≤ 1%：不 warn
    _garbage(daily / "sh601998.parquet")
    r = m.check_data_coverage()
    assert r["name"] == "data_coverage"
    assert r["status"] == "ok", r
    assert r["details"]["total"] == 101
    assert r["details"]["parsed"] == 100
    assert r["details"]["fail_ratio"] == pytest.approx(1 / 101)

    # 2/102 = 1.96% > 1%：warn
    _garbage(daily / "sh601999.parquet")
    r = m.check_data_coverage()
    assert r["status"] == "warn", r
    assert r["details"]["parsed"] == 100
    assert r["details"]["total"] == 102
    assert "解析" in r["message"] or "parse" in r["message"].lower(), r["message"]


def test_coverage_threshold_is_strictly_above_one_percent(tmp_path, monkeypatch):
    """恰好 1%（100 只里坏 1 只）不该 warn——「> 1%」是字面意思，不是 >=。"""
    m = _canary()
    daily = tmp_path / "daily"
    for i in range(99):
        _write(daily / f"sh60{i:04d}.parquet", FRESH)
    _garbage(daily / "sh601998.parquet")
    _wire_tail(m, monkeypatch, daily)
    r = m.check_data_coverage()
    assert r["details"]["total"] == 100
    assert r["details"]["fail_ratio"] == 0.01, r["details"]
    assert r["status"] == "ok", r


def test_empty_dir_coverage_is_warn(tmp_path, monkeypatch):
    """没有任何文件时不要报「0% 失败 = ok」——空表是最该喊的形态。"""
    m = _canary()
    daily = tmp_path / "empty"
    daily.mkdir()
    _wire_tail(m, monkeypatch, daily)
    assert m.check_data_coverage()["status"] == "warn"
    assert m.check_data_files_tail()["status"] == "error"


# ---------------------------------------------------------------- 翻页决策

def _declared_switch(src_text: str) -> bool:
    """从源码里静态读 TAIL_PAGING_ENABLED 的字面量，防止有人在测试里另写一套。"""
    tree = ast.parse(src_text)
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id == "TAIL_PAGING_ENABLED":
                    assert isinstance(node.value, ast.Constant), \
                        "TAIL_PAGING_ENABLED 必须是可读到的布尔字面量（决策要能被审）"
                    return bool(node.value.value)
    raise AssertionError("canary 源码里没有 TAIL_PAGING_ENABLED 常量")


def test_paging_switch_matches_the_measured_decision():
    src = CANARY_SRC.read_text(encoding="utf-8")
    m = _canary()
    enabled = _declared_switch(src)

    assert enabled is m.TAIL_PAGING_ENABLED
    # 两维名字都要登记在 TAIL_CHECKS 里，否则开关是个空开关
    assert m.TAIL_CHECKS == TAIL_NAMES, m.TAIL_CHECKS
    # 一致性：进不进 PAGING_CHECKS 完全由开关决定，没有第二处硬编码
    for name in m.TAIL_CHECKS:
        assert (name in m.PAGING_CHECKS) is enabled, name
    # 既有翻页维度不受影响
    assert {"tdxfeed_sync", "data_files", "timers", "verification"} <= m.PAGING_CHECKS


def test_measured_decision_is_report_only_because_stale_count_positive():
    """本机 2026-11-03 实测：daily A 股个股 5232 只，阈值 2026-10-30，stale_count=9。

    按判据 stale_count > 0 ⇒ 不进 PAGING_CHECKS（否则每 5 分钟推一次手机 = 刷屏）。
    翻开之前必须先清零注释里那批滞后代码，或按表分级给单独阈值。
    """
    src = CANARY_SRC.read_text(encoding="utf-8")
    assert _declared_switch(src) is False, \
        "翻页开关被翻开：先确认那批实测滞后代码已清零，并同步更新本测试与注释"
    # 决策依据（实测滞后清单）必须留在代码里，不能只活在报告里
    for code in ("sh600001", "sz000016", "sh688496", "sh601059", "sh601198",
                 "sz002717", "sh601326", "sz301139", "sh688121"):
        assert code in src, f"实测滞后清单里的 {code} 没写进 canary 注释"


def test_paging_assembles_from_the_switch():
    """开关翻开后确实会翻页——证明它不是装饰用的常量。"""
    m = _canary()
    m.TAIL_PAGING_ENABLED = True
    try:
        paged = m._paging_checks()
        assert all(name in paged for name in TAIL_NAMES), paged
    finally:
        m.TAIL_PAGING_ENABLED = False
    assert not (TAIL_NAMES & m._paging_checks())


def _run_main(canary, monkeypatch, tmp_path, checks, *argv):
    import sys
    monkeypatch.setattr(canary, "LOG_FILE", tmp_path / "canary.log")
    monkeypatch.setattr(canary, "HISTORY_FILE", tmp_path / "canary_history.jsonl")
    monkeypatch.setattr(canary, "run_all_checks",
                        lambda: {"ts": "2026-11-03T13:00:00", "checks": checks})
    monkeypatch.setattr(sys, "argv", ["canary", *argv])
    with pytest.raises(SystemExit) as exc:
        canary.main()
    return exc.value.code


def _check(name, status, message="m"):
    return {"name": name, "status": status, "message": message, "details": {}}


def test_tail_warn_does_not_page_while_switch_off(monkeypatch, tmp_path):
    """刷屏防护：真数据里已有 9 只滞后，warn 每 5 分钟推一次手机就是噪声。"""
    canary = _canary()
    assert canary.TAIL_PAGING_ENABLED is False
    code = _run_main(canary, monkeypatch, tmp_path,
                     [_check("tdxfeed_sync", "ok"),
                      _check("data_files_tail", "warn", "stale_count=9"),
                      _check("data_coverage", "warn", "fail_ratio=0.02")], "--quiet")
    assert code == 0


def test_tail_warn_pages_when_switch_flipped(monkeypatch, tmp_path):
    """清完滞后就能翻开开关：一条常量，不用改逻辑。"""
    canary = _canary()
    monkeypatch.setattr(canary, "TAIL_PAGING_ENABLED", True)
    monkeypatch.setattr(canary, "PAGING_CHECKS", canary._paging_checks())
    code = _run_main(canary, monkeypatch, tmp_path,
                     [_check("tdxfeed_sync", "ok"),
                      _check("data_files_tail", "warn", "stale_count=1")], "--quiet")
    assert code == 1


def test_both_dimensions_registered_in_run_all_checks():
    """维度必须真的被跑起来（藏在别的函数里 = 又一次 KI-DOC-002 式隐形能力）。"""
    tree = ast.parse(CANARY_SRC.read_text(encoding="utf-8"))
    fn = next(n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name == "run_all_checks")
    ret = next(n for n in ast.walk(fn) if isinstance(n, ast.Return))
    dims = [c.func.id.removeprefix("check_") for c in ast.walk(ret)
            if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
            and c.func.id.startswith("check_")]
    assert "data_files_tail" in dims, dims
    assert "data_coverage" in dims, dims
    # 尾部统计走的是全量逐文件，必须还在 freshness 的导出面上
    from tdxfeed import freshness
    assert "table_freshness" in freshness.__all__


def test_workflow_doc_lists_both_new_dimensions():
    """canary 维度文档与代码必须一致（与 test_canary_dimensions_documented 同一口径）。"""
    text = (ROOT / "docs" / "WORKFLOW.md").read_text(encoding="utf-8")
    mrow = re.search(r"\|\s*canary 维度\s*\|\s*(\d+)[\(（]([^)）]*)[\)）]", text)
    assert mrow, "docs/WORKFLOW.md 里找不到「canary 维度」行"
    names = [x.strip() for x in mrow.group(2).split("/") if x.strip()]
    assert int(mrow.group(1)) == len(names), (mrow.group(1), names)
    assert "data_files_tail" in names and "data_coverage" in names, names
