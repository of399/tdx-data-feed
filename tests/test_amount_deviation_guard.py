"""成交额量级守卫（KI-DATA-002）：只登记偏差、不改写表内数值。

背景：主源新浪日 K 不返回 amount，`tdxfeed/amount_fix.py` 用
close × volume(手) × 100 给 amount<=0 的行补代理值，并把日期记进旁路清单
`amount_estimated.json`。但「amount>0 却与公式差一个量级」的错值行绕过了这个
触发条件，任何机制都看不见它（2026-10-26 实测 5 行，如 sh600000 表内
72,104,072 对公式 763,597,601）。

本守卫只检测：命中 `abs(amount - est)/est > 0.005` 的日期写进同形状的新清单
`data/.state/amount_deviation.json`，表内数值逐格不变。

NEEDS_DATA=False：全部用例走 tmp_path；末尾两条读真数据但按存在性 skip。
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from tdxfeed.amount_fix import (
    DEVIATION_TOLERANCE,
    backfill_daily_dir,
    detect_deviation_dates,
    deviation_dates,
    deviation_ledger_path,
    estimated_dates,
    fill_missing_amount_marked,
    flush_deviation,
    flush_estimated,
    ledger_path_for,
    load_estimated,
    record_deviation,
)
from tdxfeed.parquet_io import write_table
from tdxfeed.writers import Writers

NEEDS_DATA = False

D_REAL = 20260917      # (a) 真实成交额：dev=0.0012
D_PROXY = 20260918     # (b) amount<=0 的代理补值行
D_DEVIATION = 20260919  # (c) 错值行：dev=0.094
D_NEAR = 20260920      # (d) 近公式行：dev=0.002

AMT_REAL = 1_001_200.0    # est = 10.0 × 1000 × 100 = 1,000,000
AMT_PROXY_EST = 1_000_000.0   # 20.0 × 500 × 100，(b) 补值后的期望
AMT_DEVIATION = 906_000.0
AMT_NEAR = 1_002_000.0


def _rows():
    return [
        [date(2026, 9, 17), 10.0, 11.0, 9.0, 10.0, 1000.0, AMT_REAL],
        [date(2026, 9, 18), 10.0, 11.0, 9.0, 20.0, 500.0, 0.0],
        [date(2026, 9, 19), 10.0, 11.0, 9.0, 10.0, 1000.0, AMT_DEVIATION],
        [date(2026, 9, 20), 10.0, 11.0, 9.0, 10.0, 1000.0, AMT_NEAR],
    ]


def _df(rows=None, date_kind="date"):
    rows = rows or _rows()
    df = pd.DataFrame({
        "date": [r[0] for r in rows],
        "open": [r[1] for r in rows],
        "high": [r[2] for r in rows],
        "low": [r[3] for r in rows],
        "close": [r[4] for r in rows],
        "volume": [r[5] for r in rows],
        "amount": [r[6] for r in rows],
    })
    if date_kind == "int":
        df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y%m%d").astype("int64")
    elif date_kind == "timestamp":
        df["date"] = pd.to_datetime(df["date"])
    elif date_kind == "string":
        df["date"] = df["date"].astype(str)
    return df


def _clean_rows():
    """(c) 的 amount 换成正常量级：整表无偏差。"""
    rows = [list(r) for r in _rows()]
    rows[2][6] = AMT_REAL
    return [tuple(r) for r in rows]


def _bars(rows=None):
    rows = rows or _rows()
    return [[d.strftime("%Y-%m-%d"), o, h, l, c, v, a] for d, o, h, l, c, v, a in rows]


def _daily_dir(root: Path) -> Path:
    d = root / "parquet" / "daily"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _dev_file(root: Path) -> Path:
    return root / ".state" / "amount_deviation.json"


def load_deviation_file(root: Path) -> dict:
    p = _dev_file(root)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def test_tolerance_is_the_documented_threshold():
    assert DEVIATION_TOLERANCE == 0.005


# --------------------------------------------------------------------------
# 纯判据：四类行只有 (c) 命中
# --------------------------------------------------------------------------
@pytest.mark.parametrize("kind", ["date", "int", "timestamp", "string"])
def test_detect_flags_only_the_deviating_row(kind):
    assert detect_deviation_dates(_df(date_kind=kind)) == {D_DEVIATION}


def test_detect_boundary_strictly_above_tolerance():
    # dev 恰为阈值不记；略高才记（判据是 dev > tolerance）
    exact = _df([[date(2026, 9, 17), 1.0, 1.0, 1.0, 10.0, 1000.0,
                  10.0 * 1000 * 100 * (1 - DEVIATION_TOLERANCE)]])
    just_over = _df([[date(2026, 9, 17), 1.0, 1.0, 1.0, 10.0, 1000.0,
                      10.0 * 1000 * 100 * (1 - DEVIATION_TOLERANCE) - 1.0]])
    assert detect_deviation_dates(exact) == set()
    assert detect_deviation_dates(just_over) == {20260917}


def test_detect_skips_rows_without_positive_amount():
    # (b) 那类 amount<=0 的行由估算清单负责，量级守卫不重复记账
    rows = [[date(2026, 9, 18), 10.0, 11.0, 9.0, 20.0, 500.0, 0.0],
            [date(2026, 9, 19), 10.0, 11.0, 9.0, 20.0, 500.0, -5.0]]
    assert detect_deviation_dates(_df(rows)) == set()


def test_detect_is_read_only_on_the_frame():
    df = _df()
    before = df.copy(deep=True)
    detect_deviation_dates(df)
    pd.testing.assert_frame_equal(df, before)


def test_detect_tolerates_missing_columns_and_nan():
    for col in ("amount", "volume", "close", "date"):
        assert detect_deviation_dates(_df().drop(columns=[col])) == set(), col
    # 缺列时不猜：整表不记
    assert detect_deviation_dates(_df([[date(2026, 9, 17), 1.0, 1.0, 1.0,
                                        10.0, 1000.0, AMT_DEVIATION]])
                                  .assign(extra=1)) == {20260917}

    nan_amount = _df()
    nan_amount.loc[2, "amount"] = float("nan")   # (c) 值缺失：判不了就不记
    assert detect_deviation_dates(nan_amount) == set()
    nan_close = _df()
    nan_close.loc[3, "amount"] = AMT_DEVIATION   # (d) 改成错值
    nan_close.loc[3, "volume"] = float("nan")    # 但 volume 缺失 → 分母不成立
    assert detect_deviation_dates(nan_close) == {D_DEVIATION}


# --------------------------------------------------------------------------
# 公开 API
# --------------------------------------------------------------------------
def test_public_api_exported():
    import tdxfeed.amount_fix as af

    for name in ("deviation_dates", "flush_deviation", "deviation_ledger_path"):
        assert name in af.__all__, name
    assert deviation_ledger_path("x/y").as_posix().endswith("x/y/.state/amount_deviation.json")
    # 与估算清单同目录、同形状，只是文件名不同
    assert deviation_ledger_path("x/y").parent == ledger_path_for("x/y").parent


def test_record_and_read_roundtrip_replaces_whole_segment(tmp_path):
    dev = deviation_ledger_path(tmp_path)
    record_deviation("sh600000", _df(), dev)
    assert deviation_dates("sh600000", dev) == {D_DEVIATION}

    # 整段替换：下一轮全表评估没有命中，条目必须消失（自愈）
    record_deviation("sh600000", _df().assign(amount=[AMT_REAL, 0.0, AMT_REAL, AMT_NEAR]), dev)
    assert deviation_dates("sh600000", dev) == set()


def test_flush_deviation_writes_estimated_ledger_shape(tmp_path):
    dev = deviation_ledger_path(tmp_path)
    est = ledger_path_for(tmp_path)
    record_deviation("sh600000", _df(), dev)
    # 全表评估没有命中的 symbol：空集合即不记（同 record_estimated 的整段替换语义）
    record_deviation("sz000001", _df(_clean_rows()), dev)
    flush_deviation(dev)

    raw = json.loads(dev.read_text(encoding="utf-8"))
    assert raw == {"sh600000": [D_DEVIATION]}
    assert list((tmp_path / ".state").glob("*.tmp")) == []
    # 形状与估算清单一致：键为带前缀 symbol，值为升序 int YYYYMMDD 列表
    assert all(isinstance(k, str) and isinstance(v, list) and
               all(isinstance(x, int) for x in v) for k, v in raw.items())
    assert not est.exists()


def test_flush_deviation_keeps_entries_from_another_process(tmp_path):
    dev = deviation_ledger_path(tmp_path)
    record_deviation("sh600000", _df(), dev)
    dev.parent.mkdir(parents=True, exist_ok=True)
    dev.write_text(json.dumps({"sz000001": [20260917]}), encoding="utf-8")

    flush_deviation(dev)
    assert json.loads(dev.read_text(encoding="utf-8")) == {
        "sh600000": [D_DEVIATION], "sz000001": [20260917]}


def test_deviation_dates_empty_when_file_absent(tmp_path):
    assert deviation_dates("sh600000", deviation_ledger_path(tmp_path)) == set()


def test_deviation_ledger_flushes_on_process_exit(tmp_path):
    """复用既有 atexit 缓冲：子进程只调 merge_write，退出后偏差清单必须已在盘上。"""
    repo = Path(__file__).resolve().parents[1]
    script = (
        "from tdxfeed.writers import Writers\n"
        f"Writers({str(tmp_path)!r}).merge_write('sh600000', {_bars_repr()})\n"
    )
    subprocess.run([sys.executable, "-c", script], cwd=str(repo), check=True)
    assert load_deviation_file(tmp_path) == {"sh600000": [D_DEVIATION]}


def _bars_repr() -> str:
    return repr(_bars())


# --------------------------------------------------------------------------
# 接线点 1：Writers.merge_write（full_sync / cli / backfill_history 都走它）
# --------------------------------------------------------------------------
def test_merge_write_records_deviation_and_keeps_table_values(tmp_path):
    from tdxfeed import amount_fix as af

    errors_before = af._ERROR_COUNT.copy()
    Writers(str(tmp_path)).merge_write("sh600000", _bars())
    # 挂钩里的吞错只在真出问题时留痕：正常路径不得新增被吞掉的异常
    assert af._ERROR_COUNT.copy() == errors_before

    dev = deviation_ledger_path(tmp_path)
    assert deviation_dates("sh600000", dev) == {D_DEVIATION}
    # 估算清单口径不变：只有 amount<=0 的 (b) 被标估算
    assert estimated_dates("sh600000", ledger_path_for(tmp_path)) == {D_PROXY}

    got = pd.read_parquet(tmp_path / "parquet" / "daily" / "sh600000.parquet")
    assert got["amount"].tolist() == [AMT_REAL, AMT_PROXY_EST, AMT_DEVIATION, AMT_NEAR]
    assert got.loc[got["date"] == date(2026, 9, 19), "amount"].iloc[0] == AMT_DEVIATION, \
        "守卫只检测，错值不得被改写"


def test_merge_write_clears_deviation_when_source_becomes_correct(tmp_path):
    w = Writers(str(tmp_path))
    w.merge_write("sh600000", _bars())
    assert deviation_dates("sh600000", deviation_ledger_path(tmp_path)) == {D_DEVIATION}

    bars = _bars()
    bars[2][6] = AMT_REAL          # 源侧修好了这行：整段替换后条目应消失
    w.merge_write("sh600000", bars)
    assert deviation_dates("sh600000", deviation_ledger_path(tmp_path)) == set()
    flush_deviation(deviation_ledger_path(tmp_path))
    assert load_deviation_file(tmp_path) == {}


# --------------------------------------------------------------------------
# 接线点 2：parquet_io.write_table（incremental._write_v2 亦经它）
# --------------------------------------------------------------------------
def test_write_table_on_daily_records_deviation_without_flush(tmp_path):
    daily = _daily_dir(tmp_path)
    write_table(daily / "sh600000.parquet", _df(date_kind="timestamp"))
    assert deviation_dates("sh600000", deviation_ledger_path(tmp_path)) == {D_DEVIATION}
    # 未 flush 前不落盘：清单是进程内缓冲
    assert not _dev_file(tmp_path).exists()

    flush_deviation(deviation_ledger_path(tmp_path))
    assert load_deviation_file(tmp_path) == {"sh600000": [D_DEVIATION]}


def test_incremental_write_v2_hooked(tmp_path):
    from tdxfeed import incremental as inc

    daily = _daily_dir(tmp_path)
    inc._write_v2(daily / "sz000001.parquet", _df(date_kind="timestamp"))
    assert deviation_dates("sz000001", deviation_ledger_path(tmp_path)) == {D_DEVIATION}


def test_write_table_ignores_non_daily_paths(tmp_path):
    """只在写日线时评估：derived / 备份名 / 不在 parquet/daily 下的写入都不记账。"""
    df = _df(date_kind="timestamp")

    derived = tmp_path / "parquet" / "derived"
    derived.mkdir(parents=True)
    write_table(derived / "sh600000_indicators.parquet", df)

    write_table(tmp_path / "sh600000.parquet", df)
    write_table(tmp_path / "sh600000.v1.bak.parquet", df)

    assert deviation_dates("sh600000", deviation_ledger_path(tmp_path)) == set()
    assert not _dev_file(tmp_path).exists()
    assert {p.name for p in tmp_path.iterdir() if p.is_file()} == \
        {"sh600000.parquet", "sh600000.v1.bak.parquet"}
    assert not (tmp_path / ".state").exists()


# --------------------------------------------------------------------------
# 接线点 3：backfill_daily_dir（一行都没补也要能发现偏差）
# --------------------------------------------------------------------------
def test_backfill_detects_even_when_it_fills_nothing(tmp_path):
    daily = _daily_dir(tmp_path)
    rows = [r for r in _rows() if r[0] != date(2026, 9, 18)]   # 无 amount<=0 行 → n=0
    write_table(daily / "sh600000.parquet", _df(rows))
    before = pd.read_parquet(daily / "sh600000.parquet")

    stats = backfill_daily_dir(daily, workers=1, out_root=tmp_path, update_day=False)
    assert stats["filled_rows"] == 0 and stats["stocks_updated"] == 0
    assert deviation_dates("sh600000", deviation_ledger_path(tmp_path)) == {D_DEVIATION}

    after = pd.read_parquet(daily / "sh600000.parquet")
    pd.testing.assert_frame_equal(before, after)


def test_backfill_records_both_ledgers(tmp_path):
    daily = _daily_dir(tmp_path)
    write_table(daily / "sh600000.parquet", _df())
    flush_deviation(deviation_ledger_path(tmp_path))

    stats = backfill_daily_dir(daily, workers=1, out_root=tmp_path, update_day=False)
    assert stats["filled_rows"] == 1
    assert deviation_dates("sh600000", deviation_ledger_path(tmp_path)) == {D_DEVIATION}
    assert estimated_dates("sh600000", ledger_path_for(tmp_path)) == {D_PROXY}

    got = pd.read_parquet(daily / "sh600000.parquet")
    assert got["amount"].tolist() == [AMT_REAL, AMT_PROXY_EST, AMT_DEVIATION, AMT_NEAR]


def test_fill_missing_amount_stays_pure():
    """纯函数职责不变：它只补 amount<=0，量级判定由写盘侧挂钩负责。"""
    df = _df()
    out, n, est = fill_missing_amount_marked(df)
    assert n == 1 and list(est) == [False, True, False, False]
    assert out["amount"].tolist() == [AMT_REAL, AMT_PROXY_EST, AMT_DEVIATION, AMT_NEAR]


# --------------------------------------------------------------------------
# 估算清单行为不得改变（回归护栏）
# --------------------------------------------------------------------------
def test_estimated_ledger_semantics_unchanged(tmp_path):
    est = ledger_path_for(tmp_path)
    Writers(str(tmp_path)).merge_write("sh600000", _bars())
    assert estimated_dates("sh600000", est) == {D_PROXY}
    flush_estimated(est)
    assert load_estimated(est) == {"sh600000": [D_PROXY]}

    # 真实成交额回来后撤销标记（既有的 merge 语义）
    bars = _bars()
    bars[1][6] = 1_234_567.0
    Writers(str(tmp_path)).merge_write("sh600000", bars)
    assert estimated_dates("sh600000", est) == set()
    flush_estimated(est)
    assert load_estimated(est) == {}


# --------------------------------------------------------------------------
# 真数据只读校验（CI 无 data/ 时自动跳过）
# --------------------------------------------------------------------------
def _real_daily(code: str) -> Path | None:
    from tdxfeed.paths import daily_path

    p = daily_path(code)
    return p if p.exists() else None


@pytest.mark.parametrize("code", ["sh600000", "sz000001"])
def test_real_data_2026_10_26_is_flagged(code):
    fp = _real_daily(code)
    if fp is None:
        pytest.skip(f"本机无 {fp}")
    df = pd.read_parquet(fp)
    df["date"] = pd.to_datetime(df["date"])
    hit = df[df["date"] == pd.Timestamp("2026-10-26")]
    assert len(hit) == 1
    row = hit.iloc[0]
    est = float(row["close"]) * float(row["volume"]) * 100.0
    dev = abs(float(row["amount"]) - est) / est
    assert dev > DEVIATION_TOLERANCE, f"{code} dev={dev}"
    assert 20261026 in detect_deviation_dates(hit)


# --------------------------------------------------------------------------
# 判据必须按当期口径分档（KI-DATA-002 fix_hint 原话）
#
# 实测依据（2026-11-03，全市场 5,236 个日线文件 / 16,786,827 可判定行）：
#   * 2026-09-16 之后（amount 结构性精确等于公式）：dev>0.005 命中 5 行，
#     正是 KI-DATA-002 登记的 sh600000/sz000001/sz000002/sz000012/sz000021 @20261026；
#   * 2026-09-16 之前（真实观测成交额）：同一判据命中 9,479,112 行、涉及 5,207 只。
#     close × volume 只是当日 VWAP 的近似，历史期 dev 中位 1.05%、p90 2.55%，
#     偏离 0.5% 是常态而不是错值；
#   * 历史期把阈值放宽到 dev>0.5 仍有 7,086 行，其中 7,062 行落在 1990-1993
#     （早期成交额单位口径与现表不同，例：sh600601 1991-01-02 的 amount=20），
#     属数据源历史形态，不是写入侧缺陷。
# ⇒ 单一全局阈值会把清单灌成噪声、淹没真正那 5 行，因此按断点分档。
# --------------------------------------------------------------------------
def _one(day, amount, close=10.0, volume=1000.0):
    return [day, 10.0, 11.0, 9.0, close, volume, amount]


def test_proxy_caliber_start_is_the_measured_breakpoint():
    import tdxfeed.amount_fix as af

    assert af.PROXY_CALIBER_START == 20260916
    assert "PROXY_CALIBER_START" in af.__all__


def test_pre_breakpoint_typical_rows_are_not_judged():
    """历史期 dev 1%~90% 都属正常或早期口径：一律不记（洪水守卫）。"""
    import tdxfeed.amount_fix as af

    rows = [_one(date(2026, 9, 15), 1_000_000 * (1 - 0.02)),
            _one(date(2015, 6, 30), 1_000_000 * (1 - 0.15)),
            _one(date(2003, 5, 6), 1_000_000 * (1 - 0.90)),
            _one(date(1992, 11, 20), 1_000_000 * (1 - 0.99))]
    assert max(int(pd.Timestamp(d).strftime("%Y%m%d")) for d in
               [r[0] for r in rows]) < af.PROXY_CALIBER_START
    assert detect_deviation_dates(_df(rows)) == set()


def test_era_boundary_is_the_breakpoint_itself():
    bad = 1_000_000 * (1 - 0.9)
    assert detect_deviation_dates(_df([_one(date(2026, 9, 16), bad)])) == {20260916}
    assert detect_deviation_dates(_df([_one(date(2026, 9, 15), bad)])) == set()


def test_mixed_table_records_only_proxy_era_dates():
    bad = 1_000_000 * (1 - 0.9)
    rows = [_one(date(1991, 1, 2), bad), _one(date(2026, 9, 20), bad),
            _one(date(2020, 7, 7), bad), _one(date(2026, 10, 26), bad)]
    assert detect_deviation_dates(_df(rows)) == {20260920, 20261026}


def test_record_deviation_does_not_flood_on_history_rebase(tmp_path):
    """整表重拉（rebase）时近万行正常历史偏离不得灌进清单。"""
    import random

    rng = random.Random(42)
    rows = [_one(date(2000 + i // 1200, i % 12 + 1, i % 28 + 1),
                 1_000_000 * (1 - rng.uniform(0.006, 0.30)))
            for i in range(6000)]
    dev = deviation_ledger_path(tmp_path)
    assert record_deviation("sh600000", _df(rows), dev) == set()
    assert deviation_dates("sh600000", dev) == set()


@pytest.mark.parametrize("code", ["sh600000", "sz000001", "sz000002", "sz000012", "sz000021"])
def test_real_data_known_five_rows_are_the_only_hits(code):
    """真表整表评估：结果恰为已知的 20261026，且不含任何断点之前的日期。"""
    import tdxfeed.amount_fix as af

    fp = _real_daily(code)
    if fp is None:
        pytest.skip(f"本机无 {fp}")
    hits = detect_deviation_dates(pd.read_parquet(fp))
    assert hits == {20261026}, f"{code} -> {sorted(hits)}"
    assert all(d >= af.PROXY_CALIBER_START for d in hits)


def test_real_data_early_unit_rows_are_not_recorded():
    """sh600601 的 1990-1991 行量级差到万倍，属早期成交额口径：不得进清单。"""
    fp = _real_daily("sh600601")
    if fp is None:
        pytest.skip(f"本机无 {fp}")
    df = pd.read_parquet(fp)
    early = df[pd.to_datetime(df["date"]) < pd.Timestamp("1994-01-01")]
    assert len(early) > 0
    assert detect_deviation_dates(df) == set(), sorted(detect_deviation_dates(df))
