"""B1 的行为中性证据：**真数据、逐格、只读**（不重训、不跑 predict、不写产物）。

`NEEDS_DATA = True`：本模块的数据依赖是**间接**的（被测模块自己经
`tdxfeed.paths.daily_files()` / `st_event_path()` 读盘），测试源码里没有 `data/` 字样，
所以按根 conftest 的字面扫描分类会被判成「纯模块」而在 CI 里真跑并假红。显式加入并登记进
`tests/test_ci_data_skip.py::DATA_DEPENDENT_MODULES`（等值判据，两边必须同步）。

三条判据各自钉一件事：
① `test_dim1_is_identical_on_the_last_30_trading_days`——近 30 个交易日（全部在并轨日之后）
   的 dim1 逐格差 **必须为 0**；这是「修的是历史，不动今天」的实证。
   同一次扫描里 dim15 的差**允许非 0**，但只能发生在面板给得出权威档的行上，
   差值行数被单独计数并打印（现役模型是 dim15≡0 训的 ⇒ 这正是 B2 必须重训的原因）。
② `test_pre_rule_change_authority_st_row_changes_dim1`——并轨前主板 ST 的历史行
   dim1 从 0.095 变 0.045（用户点名的例子，代码/日期/两个值都打出来）。
③ `test_supplied_limits_match_the_panel_intervals`——按日供给出来的阈值列，
   与「直接从面板区间算的期望列」在有界样本上逐格一致（防止 `_CodeIndex` 的
   bisect 与区间语义漂走）。
"""
from __future__ import annotations

import datetime as dt
import random
from pathlib import Path

import pandas as pd

from tdxfeed import paths, st_history as st, st_supply as sup
from tdxfeed.execution import limits as _limits
from tdxfeed.features_5d import compute_window_features
from tdxfeed.symbols import INDICES, sym_of_index

NEEDS_DATA = True

RULE_DAY = _limits.MAIN_ST_RULE_CHANGE_DATE
TAIL_DAYS = 30           # 「近 30 个交易日」
TAIL_ROWS = TAIL_DAYS + 4  # 特征要 5 根（sig_idx >= 4）
CROSS_SAMPLE = 200       # 判据 ③ 的有界样本（固定 seed，可复现）
SEED = 20261105

_INDEX_SYMBOLS = frozenset(sym_of_index(c) for c in INDICES)


def _supply() -> sup.StSupply:
    return sup.StSupply.load()


def _active_stock_files() -> list[tuple[str, Path]]:
    """active 日线文件 → (裸 6 位码, 路径)，剔除指数与北交所以外的非个股形状。"""
    out: list[tuple[str, Path]] = []
    for fp in paths.daily_files():
        sym = fp.stem
        if sym in _INDEX_SYMBOLS:
            continue
        code = _limits.strip_exchange_prefix(sym)
        if not code.isdigit() or len(code) != 6:
            continue
        out.append((code, fp))
    return out


def _read(fp: Path, tail: int | None = None) -> pd.DataFrame | None:
    try:
        df = pd.read_parquet(fp)
    except Exception:
        return None
    if df is None or "date" not in df.columns or len(df) < (tail or 5):
        return None
    df = df.sort_values("date").reset_index(drop=True)
    if tail is not None:
        df = df.tail(tail).reset_index(drop=True)
    return df


# --------------------------------------------------------------------------- #
# ① 近 30 个交易日：dim1 逐格差必须为 0
# --------------------------------------------------------------------------- #
def test_dim1_is_identical_on_the_last_30_trading_days() -> None:
    supply = _supply()
    files = _active_stock_files()
    cells = diff_dim1 = diff_dim15 = st_rows = unknown_rows = off_source = 0
    checked_files = 0
    for code, fp in files:
        df = _read(fp, tail=TAIL_ROWS)
        if df is None:
            continue
        checked_files += 1
        dates = list(df["date"])
        limits_new, tiers, flags = sup.limit_series(code, dates, supply)
        limits_old, _t0, _f0 = sup.limit_series(code, dates, None)
        for i in range(4, len(df)):
            d = _limits.as_date(dates[i])
            if d is None or d < RULE_DAY:
                continue          # 只看并轨日当天及之后（近 30 个交易日全在这段里）
            cells += 1
            old = compute_window_features(df, code, sig_idx=i)
            new = compute_window_features(df, code, sig_idx=i, is_st=flags[i])
            if old is None or new is None:
                continue
            # dim1 必须等于 `limit_series` 给的那个数（dim1 与阈值列同源，不许两套实现）
            if abs(float(old["limit"]) - float(limits_old[i])) > 1e-12 or \
                    abs(float(new["limit"]) - float(limits_new[i])) > 1e-12:
                off_source += 1
            if abs(float(old["limit"]) - float(new["limit"])) > 1e-12:
                diff_dim1 += 1
            if int(old["is_st"]) != int(new["is_st"]):
                diff_dim15 += 1
                assert tiers[i] == sup.TIER_AUTHORITY, (
                    f"{code}@{d} 的 dim15 变化不是来自权威档（{tiers[i]}）⇒ 分层策略漏了")
            if flags[i]:
                st_rows += 1
            if tiers[i] == sup.TIER_UNSUPPLIED:
                unknown_rows += 1
    # 先打印再断言：判据红的时候那几个数正是需要的证据，不能只留在断言消息里
    print(f"\n[中性①] 扫描票={checked_files}  比对格={cells:,}  "
          f"dim1 差={diff_dim1}  dim15 差={diff_dim15}  "
          f"其中权威判为 ST 的行={st_rows:,}  无法判定的行={unknown_rows:,}")
    print(f"[中性①] 供给层：{supply.describe()}")
    assert cells > 20_000, f"只比对了 {cells} 格，覆盖面不足（文件读得到吗？）"
    assert off_source == 0, f"{off_source} 格的 dim1 与 limit_series 不同源 ⇒ 供给有两套实现"
    assert diff_dim1 == 0, (
        f"近 {TAIL_DAYS} 个交易里有 {diff_dim1} 格的 dim1 被这批改动了 ⇒ 行为中性破了")


# --------------------------------------------------------------------------- #
# ② 并轨前的历史行：dim1 必须变（0.095 → 0.045）
# --------------------------------------------------------------------------- #
def test_pre_rule_change_authority_st_row_changes_dim1() -> None:
    supply = _supply()
    events = st.load_panel()
    states = st.day_states(events, sources=st.AUTHORITY_SOURCES)
    picks: list[tuple[str, dt.date]] = []
    for rec in states.to_dict("records"):
        start = rec["start_date"]
        if start is None or pd.isna(start) or start >= RULE_DAY:
            continue
        code = str(rec["code"])
        if _limits.market_of(code) != _limits.MARKET_MAIN:
            continue      # 5% 档只对主板 ST 成立
        picks.append((code, start))
    # 优先给**并轨前几年**的例子（用户点名的形状是「某只并轨前 ST 票在 2024 年的 dim1
    # 0.095→0.045」）；2024 年一条都取不到时才退回按日期升序取，并照样断言同样的值。
    ordered = sorted([p for p in picks if p[1].year == 2024]) + sorted(picks)
    picked: list[tuple[str, dt.date, float, float, int, Path]] = []
    for code, day in ordered:
        fp = paths.daily_path(code)
        if not fp.exists():
            continue
        df = _read(fp)
        if df is None:
            continue
        idx = [i for i, d in enumerate(list(df["date"]))
               if _limits.as_date(d) == day and i >= 4]
        if not idx:
            continue
        flag, tier = supply.is_st(code, day)
        if not flag or tier != sup.TIER_AUTHORITY:
            continue      # 只要面板**权威层**说「那天是 ST」的例子，不拿推断举例
        old = compute_window_features(df, code, sig_idx=idx[0])
        new = compute_window_features(df, code, sig_idx=idx[0], is_st=flag)
        if old is None or new is None:
            continue
        picked.append((code, day, float(old["limit"]), float(new["limit"]), int(new["is_st"]), fp))
        if len(picked) >= 3:
            break
    assert picked, "找不到任何一只「并轨前有权威 ST 区间且有日线」的票 ⇒ 判据 ② 没跑到东西"
    print("\n[历史②] 并轨前权威 ST 行的 dim1 逐例（旧 → 新，档位都是「权威」）：")
    for code, day, old_v, new_v, dim15, fp in picked:
        print(f"        {code} @ {day}  dim1 {old_v:g} → {new_v:g}  dim15 0→{dim15}  "
              f"（日线={Path(fp).name}）")
        assert old_v == 0.095 and new_v == 0.045 and dim15 == 1, (code, day, old_v, new_v)


# --------------------------------------------------------------------------- #
# ③ 按日供给 vs 面板区间：有界样本逐格一致
# --------------------------------------------------------------------------- #
def test_supplied_limits_match_the_panel_intervals() -> None:
    """期望列**不用** `_CodeIndex`，直接从 `day_states` 的区间逐条比，独立实现一次。"""
    supply = _supply()
    events = st.load_panel()
    states = st.day_states(events, sources=st.AUTHORITY_SOURCES)
    segs: dict[str, list[tuple[dt.date, dt.date | None]]] = {}
    for rec in states.to_dict("records"):
        s, e = rec["start_date"], rec["end_date"]
        if s is None or pd.isna(s):
            continue
        segs.setdefault(str(rec["code"]), []).append(
            (s, None if (e is None or pd.isna(e)) else e))

    files = _active_stock_files()
    rng = random.Random(SEED)
    sample = rng.sample(files, min(CROSS_SAMPLE, len(files)))
    changed = mism = cells = 0
    for code, fp in sample:
        df = _read(fp)
        if df is None:
            continue
        dates = [_limits.as_date(d) for d in df["date"]]
        got, _tiers, _flags = sup.limit_series(code, dates, supply)
        base = _limits.limit_threshold(code)
        st_pre = _limits.limit_threshold(code, is_st=True, on_date=RULE_DAY - dt.timedelta(days=1))
        for d, g in zip(dates, got, strict=True):
            if d is None:
                continue
            cells += 1
            inside = any(s <= d and (e is None or d < e) for s, e in segs.get(code, []))
            want = st_pre if (inside and d < RULE_DAY) else base
            if abs(float(g) - float(want)) > 1e-12:
                mism += 1
            if abs(float(g) - float(base)) > 1e-12:
                changed += 1
    assert mism == 0, f"有界样本里 {mism} 格与面板区间不一致（_CodeIndex 语义漂了）"
    print(f"\n[交叉③] 样本={len(sample)} 只  比对格={cells:,}  "
          f"其中被改成 5% 档的行={changed:,}（历史段的移动量，正是 B1 要的效果）")
    assert changed > 0, "样本里一行都没改 ⇒ 要么面板空，要么这条判据已经判不到东西"
