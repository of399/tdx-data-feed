"""日期化 ST 事件面板的守卫（A 任务的验收面）。

面板是**事件长表**：`code, ann_date, impl_date, action, kind, source, confidence`。
逐日「那天是不是 5% 档」由消费侧从事件行派生，不落第二张表（避免两处真相）。

本模块**只依赖合成帧与 tmp_path，不读真实行情目录**，所以 `NEEDS_DATA = False`
（否则 CI 无数据时整片被 skip，守卫就白写了）。真正读盘的那条留给 `tests/test_ci_data_skip.py`
的等值登记表——本文件不进去。

钉住的判据（对应验收 ④）：
  G1 schema 与词表闭合：列序、`action` 只认「实施/撤销」、`source` 只认四个源
  G2 同一 `(code, impl_date, action)` 不得重复
  G3 推断行（`price_inferred`）必须带 `confidence`，且权威行不得带（否则读侧无法分层）
  G4 权威源与推断源**不得混读**：`day_states()` 拒绝混合层，必须按层调用
  G5 路径单源：`st_history` 必须在那条「业务代码不得写死表路径」守卫的词表里，
     且除 `tdxfeed/paths.py` 外没有任何代码自己拼这个目录
  G6 派生与事件可互推：`事件 → 逐日区间 → 事件` 回到同一批事件（形状不变即判红）
  G7 F10 原文解析逐字段对上（用真实段落文本做 fixture，段内错位必须判红）

G2/G3/G4/G5/G6/G7 都配了**变异对照**（`test_control_*`）：判据必须能被抓到红，
否则「绿」只是判据没接线。
"""
from __future__ import annotations

import datetime as dt
import importlib
import importlib.util
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 只读仓库源码文本 + 合成帧；不碰真实行情目录。显式退出「无数据即跳过」分类。
NEEDS_DATA = False

import tdxfeed.paths as paths  # noqa: E402
import tdxfeed.st_history as st  # noqa: E402

COLUMNS = ["code", "ann_date", "impl_date", "action", "kind",
           "source", "confidence", "fetched_at"]
SNAPSHOT_COLUMNS = ["snapshot_date", "code", "block_name", "source"]
_FETCHED = dt.datetime(2026, 11, 5, 12, 30, 0)


def _frame(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=COLUMNS)


def _ev(code, ann, impl, action, kind="*ST", source="f10_ledger", confidence=None,
        fetched_at=_FETCHED) -> dict:
    return {"code": code, "ann_date": ann, "impl_date": impl, "action": action,
            "kind": kind, "source": source,
            "confidence": confidence, "fetched_at": fetched_at}


def _diff_rows(left: pd.DataFrame, right: pd.DataFrame) -> str:
    """round-trip 对不上时给出具体行，而不是只有一句 equals() == False。"""
    rows = []
    for i in range(max(len(left), len(right))):
        l = left.iloc[i].tolist() if i < len(left) else None
        r = right.iloc[i].tolist() if i < len(right) else None
        if l != r:
            rows.append(f"  #{i} 写入={l} 读回={r}")
    return "\n".join(rows[:10]) or "(无差异)"


def _load_path_guard():
    """按文件路径加载既有那条「不得写死表路径」守卫，复用它的词表与扫描函数。"""
    spec = importlib.util.spec_from_file_location(
        "guard_no_hardcoded", ROOT / "tests" / "test_no_hardcoded_data_paths.py")
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)
    return guard


def _clean_events() -> pd.DataFrame:
    """两只票、四段区间，形状与真实台账一致（实施/撤销交替）。"""
    return _frame([
        _ev("000609", dt.date(2024, 4, 26), dt.date(2024, 4, 29), "实施", "*ST"),
        _ev("000609", dt.date(2025, 6, 10), dt.date(2025, 6, 11), "撤销", "撤销退市风险实施其他"),
        _ev("000609", dt.date(2026, 4, 22), dt.date(2026, 4, 23), "实施", "*ST"),
        _ev("000010", dt.date(2023, 5, 4), dt.date(2023, 5, 8), "实施", "ST"),
        _ev("000010", dt.date(2024, 6, 1), dt.date(2024, 6, 3), "撤销", "撤销特别处理"),
    ])


# G1 ---------------------------------------------------------------------------
def test_panel_schema_and_vocabulary_are_closed() -> None:
    assert list(st.EVENT_COLUMNS) == COLUMNS
    assert set(st.ACTIONS) == {"实施", "撤销"}
    assert set(st.SOURCES) == {"f10_ledger", "block_gn", "akshare_sz", "price_inferred"}
    assert set(st.AUTHORITY_SOURCES) == {"f10_ledger", "block_gn", "akshare_sz"}
    assert st.INFERRED_SOURCE == "price_inferred"
    assert set(st.AUTHORITY_SOURCES) | {st.INFERRED_SOURCE} == set(st.SOURCES)
    assert not st.validate(_clean_events())


def test_validate_rejects_unknown_action_and_source() -> None:
    bad = _frame([
        _ev("000001", dt.date(2024, 1, 1), dt.date(2024, 1, 2), "暂停"),
        _ev("000002", dt.date(2024, 1, 1), dt.date(2024, 1, 2), "实施", source="manual"),
    ])
    problems = st.validate(bad)
    assert any("action" in p for p in problems), problems
    assert any("source" in p for p in problems), problems


def test_validate_rejects_missing_or_badly_typed_columns() -> None:
    missing = _clean_events().drop(columns=["confidence"])
    assert any("confidence" in p for p in st.validate(missing))
    extra = _clean_events().assign(note="x")
    assert any("note" in p for p in st.validate(extra))
    # 日期列必须是 date/None，不能混进字符串
    texty = _clean_events().astype({"impl_date": str})
    assert any("impl_date" in p for p in st.validate(texty))


# G2 ---------------------------------------------------------------------------
def test_duplicate_events_are_flagged() -> None:
    dup = _frame([*_clean_events().to_dict("records"),
                  _ev("000609", dt.date(2024, 4, 26), dt.date(2024, 4, 29), "实施", "*ST")])
    problems = st.validate(dup)
    assert any("重复" in p and "000609" in p for p in problems), problems


def test_control_same_date_different_action_is_not_a_duplicate() -> None:
    ok = _frame([*_clean_events().to_dict("records"),
                 _ev("000609", dt.date(2024, 4, 26), dt.date(2024, 4, 29), "撤销", "撤销")])
    assert not [p for p in st.validate(ok) if "重复" in p], st.validate(ok)


# G3 ---------------------------------------------------------------------------
def test_inferred_rows_must_carry_confidence() -> None:
    rows = _clean_events().to_dict("records")
    rows.append(_ev("300001", None, dt.date(2015, 6, 1), "实施",
                    source=st.INFERRED_SOURCE, kind="price_regime5"))
    problems = st.validate(_frame(rows))
    assert any("confidence" in p and "300001" in p for p in problems), problems


def test_inferred_confidence_must_be_a_probability() -> None:
    rows = _clean_events().to_dict("records")
    rows.append(_ev("300002", None, dt.date(2015, 6, 1), "实施",
                    source=st.INFERRED_SOURCE, kind="price_regime5", confidence=1.7))
    assert any("confidence" in p for p in st.validate(_frame(rows)))
    rows2 = _clean_events().to_dict("records")
    rows2.append(_ev("300003", None, dt.date(2015, 6, 1), "实施",
                     source=st.INFERRED_SOURCE, kind="price_regime5", confidence="高"))
    assert any("confidence" in p for p in st.validate(_frame(rows2)))


def test_authority_rows_must_not_carry_confidence() -> None:
    """权威行带置信度会被读成「权威也是概率」，分层就废了。"""
    rows = [{"code": "000609", "ann_date": dt.date(2024, 4, 26),
             "impl_date": dt.date(2024, 4, 29), "action": "实施", "kind": "*ST",
             "source": "f10_ledger", "confidence": 0.9}]
    assert any("confidence" in p for p in st.validate(_frame(rows)))


# G4 ---------------------------------------------------------------------------
def test_day_states_refuse_mixed_layers() -> None:
    mixed = _frame([*_clean_events().to_dict("records"),
                    _ev("300004", None, dt.date(2016, 1, 4), "实施",
                        source=st.INFERRED_SOURCE, kind="price_regime5", confidence=0.8)])
    with pytest.raises(ValueError, match="不得混读"):
        st.day_states(mixed)
    # 单层调用必须可用
    assert not st.validate(st.sub_layer(mixed, st.AUTHORITY_SOURCES))
    one = st.day_states(mixed, sources=st.AUTHORITY_SOURCES)
    two = st.day_states(mixed, sources=(st.INFERRED_SOURCE,))
    assert len(one) + len(two) > 0
    assert set(one["source"]) <= set(st.AUTHORITY_SOURCES)
    assert set(two["source"]) == {st.INFERRED_SOURCE}


def test_control_single_layer_does_not_raise() -> None:
    """对照组：同一批数据只给权威层时不得抛错，否则上一条款是恒真判据。"""
    st.day_states(_clean_events())


# G5 ---------------------------------------------------------------------------
def test_panel_paths_come_from_the_paths_module_only() -> None:
    """定稿（更正后）：面板与它的原始底座同目录归组，不进行情表根目录 `data/parquet/`。"""
    assert paths.st_event_path().name == "st_events.parquet"
    assert paths.st_event_path().parent == paths.st_history_dir()
    assert paths.st_board_snapshot_path().name == "st_board_snapshot.parquet"
    assert paths.st_board_snapshot_path().parent == paths.st_history_dir()
    assert paths.st_raw_dir().parent == paths.st_history_dir()
    assert paths.st_coverage_path().parent == paths.st_history_dir()
    assert paths.st_f10_ledger_path().parent == paths.st_raw_dir()
    assert paths.st_f10_ledger_path().suffix == ".jsonl", "F10 原始台账必须是可审计的 JSONL"
    assert not hasattr(paths, "st_history_path"), "旧名必须退场：一张表两个入口迟早漂移"


def test_hardcoded_path_guard_knows_the_panel_directory() -> None:
    """新表必须进那条既有守卫的词表，否则它对面板目录是瞎的。"""
    guard = _load_path_guard()
    for name in ("st_events.parquet", "st_board_snapshot.parquet", "st_history"):
        assert name in guard.SEGMENTS, f"{name} 没进「业务代码不得写死表路径」的词表"


def test_no_business_module_spells_the_panel_directory() -> None:
    guard = _load_path_guard()
    offenders: dict[str, list[int]] = {}
    for p in sorted(ROOT.rglob("*.py")):
        rel = p.relative_to(ROOT)
        if any(part in guard.SKIP_DIRS for part in rel.parts) or rel.as_posix() in guard.WHITELIST:
            continue
        if rel.as_posix().startswith("tests/"):
            continue
        try:
            found = guard.violations(p)
        except SyntaxError:
            continue
        if found:
            offenders[rel] = found
    assert not offenders, offenders


def test_control_injected_hardcoded_panel_path_is_detected(tmp_path) -> None:
    """变异对照：注入一个自己拼面板/快照表路径的文件，词表加进去后必须被抓到。"""
    guard = _load_path_guard()
    bad = tmp_path / "module.py"
    bad.write_text(
        "from pathlib import Path\n"
        "P = Path('data') / 'st_history' / 'st_events.parquet'\n"
        "Q = Path('data') / 'st_history' / 'st_board_snapshot.parquet'\n",
        encoding="utf-8")
    assert guard.violations(bad) == [2, 3], "注入的硬编码路径没被抓到 ⇒ 词表那条是假的"
    ok = tmp_path / "user.py"
    ok.write_text(
        "from tdxfeed import paths\n"
        "P = paths.st_event_path()\n"
        "Q = paths.st_board_snapshot_path()\n",
        encoding="utf-8")
    assert guard.violations(ok) == [], "走 paths 入口的正常写法被误伤"


# G6 ---------------------------------------------------------------------------
def test_events_and_day_states_round_trip() -> None:
    events = _clean_events()
    states = st.day_states(events)
    back = st.states_to_events(states)
    left = events[events["source"] != "block_gn"].sort_values(
        ["code", "impl_date", "action"]).reset_index(drop=True)
    right = back.sort_values(["code", "impl_date", "action"]).reset_index(drop=True)
    assert list(right["code"]) == list(left["code"]), right
    assert list(right["impl_date"]) == list(left["impl_date"]), right
    assert list(right["action"]) == list(left["action"]), right
    assert list(right["kind"]) == list(left["kind"]), right


def test_open_interval_is_reported_as_open_not_closed() -> None:
    """最后一条是「实施」且没有撤销 ⇒ 区间应到今天仍为开（end_date 为空）。"""
    states = st.day_states(_clean_events())
    last = states[states["code"] == "000609"].sort_values("start_date").iloc[-1]
    assert pd.isna(last["end_date"]), last
    assert last["kind"] == "*ST"


def test_control_swapped_action_breaks_round_trip() -> None:
    """变异对照：把一段的撤销改成实施（错位），互推必须判红。"""
    rows = _clean_events().to_dict("records")
    rows[1]["action"] = "实施"          # 000609 第二段本该撤销
    tampered = _frame(rows)
    round_back = st.states_to_events(st.day_states(tampered))
    left = tampered.sort_values(["code", "impl_date", "action"]).reset_index(drop=True)
    right = round_back.sort_values(["code", "impl_date", "action"]).reset_index(drop=True)
    assert list(left["action"]) != list(right["action"]), (
        "错位没被判出 ⇒ 互推那条判据是空转")


# G6b --------------------------------------------------------------------------
# 验收 ① 要「多少票日」：区间对到的 (代码, 交易日) 配对数。日历由调用方注入，
# 本文件因此不必读真实行情目录（NEEDS_DATA 保持 False）。
_CALENDAR = {
    "000609": [dt.date(2024, 4, 29), dt.date(2024, 4, 30), dt.date(2024, 5, 6),
               dt.date(2025, 6, 10), dt.date(2025, 6, 11), dt.date(2025, 6, 12),
               dt.date(2026, 4, 22), dt.date(2026, 4, 23), dt.date(2026, 4, 24)],
    "000010": [dt.date(2023, 5, 4), dt.date(2023, 5, 5), dt.date(2023, 5, 8),
               dt.date(2024, 6, 2)],
}


def _cal(code: str):
    return _CALENDAR.get(code, [])


def test_covered_ticket_days_counts_half_open_intervals() -> None:
    states = st.day_states(_clean_events())
    rep = st.covered_ticket_days(states, dates_of=_cal)

    # 000609 [2024-04-29, 2025-06-11) → 04-29 / 04-30 / 05-06 / 2025-06-10 = 4 天
    #                右开：撤销实施当天 06-11 起已不再适用 5% 档，06-12 也在区间外
    # 000609 [2026-04-23, 开)         → 04-23 / 04-24 = 2 天（开口段算到日历末尾）
    # 000010 [2023-05-08, 2024-06-03) → 05-08 / 2024-06-02 = 2 天
    assert rep["票日数"] == 8, rep
    assert rep["可计段数"] == 3, rep
    assert rep["起点不可见段数"] == 0, rep
    assert rep["分源"]["f10_ledger"]["票日数"] == 8


def test_covered_ticket_days_excludes_truncated_heads_and_counts_them() -> None:
    """首条即「撤销」的截断台账：起点未知 ⇒ 不能计入票日，但必须单独计数。"""
    rows = [
        _ev("000752", dt.date(2025, 1, 9), dt.date(2025, 1, 10), "撤销", "撤销特别处理"),
        _ev("000752", None, dt.date(2026, 4, 24), "撤销", "撤销特别处理"),
    ]
    states = st.day_states(_frame(rows))
    rep = st.covered_ticket_days(states, dates_of=_cal)
    assert rep["起点不可见段数"] == 2, rep
    assert rep["票日数"] == 0, rep
    assert rep["无日历的代码数"] == 0, rep      # 起点未知的段不去查日历


def test_covered_ticket_days_reports_codes_without_bars() -> None:
    """退市/无日线的代码要显式报出来，不能当成「覆盖了 0 天」。"""
    states = st.day_states(_frame([_ev("399999", dt.date(2024, 1, 1),
                                       dt.date(2024, 1, 2), "实施")]))
    rep = st.covered_ticket_days(states, dates_of=_cal)
    assert rep["无日历的代码数"] == 1, rep
    assert "399999" in rep["无日历的代码"], rep


def test_control_interval_end_inclusive_would_overcount(tmp_path) -> None:
    """变异对照：若右开写成了右闭，000609 第一段会多算撤销当天 ⇒ 判据必须能分辨。"""
    states = st.day_states(_clean_events())
    rep = st.covered_ticket_days(states, dates_of=_cal)
    inclusive = sum(
        1 for code, rows in _CALENDAR.items()
        for d in rows
        if any(s["start_date"] <= d
               and (s["end_date"] is None or pd.isna(s["end_date"]) or d <= s["end_date"])
               for s in states[states["code"] == code].to_dict("records")
               if s["start_date"] is not None)
    )
    assert inclusive == rep["票日数"] + 1, (inclusive, rep["票日数"])


def test_coverage_report_counts_ticket_days_per_source() -> None:
    """验收 ①「逐源给多少票日」：日历注进去后，每个源单独数，再给权威层合计。"""
    ev = _frame([
        _ev("000609", dt.date(2024, 4, 26), dt.date(2024, 4, 29), "实施", "*ST"),
        _ev("000609", dt.date(2025, 6, 10), dt.date(2025, 6, 11), "撤销", "撤销特别处理"),
        _ev("000010", dt.date(2023, 5, 4), dt.date(2023, 5, 8), "实施",
            "当日成员快照", source="block_gn"),
    ])
    rep = st.coverage_report(ev, dates_of=_cal)
    assert rep["by_source"]["f10_ledger"]["票日数"] == 4, rep
    assert rep["by_source"]["block_gn"]["票日数"] == 2, rep
    assert rep["权威层合计"]["票日数"] == 6, rep
    assert rep["权威层合计"]["段数"] == 2, rep

    no_cal = st.coverage_report(ev)
    assert "票日数" not in no_cal["by_source"]["f10_ledger"], "没注入日历时不该出现票日数"


def test_coverage_report_exposes_the_real_ledger_depth_limit() -> None:
    """上限的正确口径：**没有「每票最多 3 条」这个硬上限**，压覆盖率的是大面积缺席。

    全市场实测分布 `{0:4739, 1:182, 2:233, 3:60, 4:11, 5:4}` ⇒ 观测最大 5 条，
    而 4,739/5,229 = 90.63% 的票【特别处理】栏是「暂无数据」。所以报告必须：
    ① 把「无段落票数 + 占比」摆在最前面（这才是上限本身）；
    ② 仍然给出 3 这一档与 `>3` 的票数（上一批点名的项，不能删）；
    ③ 键名不许写成「截断到 3 条」——那会把一个分布档位读成源侧硬上限。
    """
    ledger = [{"code": c, "status": "ok", "events": [{}] * n}
              for c, n in (("000001", 0), ("000002", 3), ("000003", 3), ("000004", 5))]
    rep = st.coverage_report(_clean_events(), ledger_records=ledger)
    lim = rep["上限"]
    assert lim["F10 无特别处理段落的票数"] == 1, lim
    assert lim["F10 无特别处理段落的占比"] == 0.25, lim
    assert lim["有事件的票数"] == 3, lim
    assert lim["条目数恰为 3 的票数"] == 2, lim
    assert lim["条目数不超过 3 的票数（含有事件的票）"] == 2, lim
    assert lim["台账条目数最大值"] == 5, lim
    assert lim["条目数大于 3 的票数"] == 1, lim
    assert lim["台账条目数分布"] == {"0": 1, "3": 2, "5": 1}, lim
    assert "不存在「每票最多 3 条」这个硬上限" in lim["上限说明"], lim["上限说明"]
    assert "25.00%" in lim["上限说明"], "上限说明必须把缺席占比算出来，不是只写一句定性"
    assert "台账截断到 3 条的票数" not in lim, "旧键名会把分布档位读成源侧硬上限"


# G6c --------------------------------------------------------------------------
def test_interleaved_sources_round_trip_only_adds_marked_closes() -> None:
    """两个权威源描述同一轮 ST 但实施日差一天时，`day_states` 会把前一段就地右开。

    真数据上这条是主要的不对称来源（权威层 2,859 个事件键 → 回程 3,641 个，多的 782 条
    **全部**带 `按下一次实施截断` 标记）。判据两半：正向一个都不能丢；回程多出来的
    必须带标记——不带标记的多出来就是编事件。
    """
    ev = _frame([
        _ev("000100", None, dt.date(2024, 1, 2), "实施", "*ST", source="f10_ledger"),
        _ev("000100", None, dt.date(2024, 1, 3), "实施", "*ST", source="akshare_sz"),
        _ev("000100", None, dt.date(2024, 6, 4), "撤销", "撤销特别处理", source="akshare_sz"),
    ])
    states = st.day_states(ev, sources=st.AUTHORITY_SOURCES)
    back = st.states_to_events(states)

    def keys(frame: pd.DataFrame) -> set:
        return {(r["code"], r["impl_date"], r["action"]) for r in frame.to_dict("records")}

    lost = keys(ev) - keys(back)
    assert not lost, f"正向事件被丢了：{lost}"
    extra = keys(back) - keys(ev)
    assert extra == {("000100", dt.date(2024, 1, 3), "撤销")}, extra
    marked = {(r["code"], r["impl_date"], r["action"]) for r in back.to_dict("records")
              if "按下一次实施截断" in str(r["kind"])}
    assert extra <= marked, f"多出来的撤销没有截断标记：{extra - marked}"

    # 就地右开不改逐日答案：切断那天必须仍被后一段覆盖（区间连续、没有缝）
    segs = states.sort_values("start_date").to_dict("records")
    assert (segs[0]["start_date"], segs[0]["end_date"]) == (dt.date(2024, 1, 2),
                                                            dt.date(2024, 1, 3)), segs
    assert (segs[1]["start_date"], segs[1]["end_date"]) == (dt.date(2024, 1, 3),
                                                            dt.date(2024, 6, 4)), segs
    cut = dt.date(2024, 1, 3)
    covered = [s for s in segs if s["start_date"] <= cut
               and (s["end_date"] is None or s["end_date"] > cut)]
    assert len(covered) == 1, f"切断那天漏了一天（覆盖段={covered}）"


# G7 ---------------------------------------------------------------------------
F10_SECTION_000609 = """【特别处理】
┌────────┬──────────────────────────────────────────────────┐
｜公告日期        ｜2026-04-22
├────────┼──────────────────────────────────────────────────┤
｜实施日期        ｜2026-04-23
├────────┼──────────────────────────────────────────────────┤
｜处理类型        ｜*ST
├────────┼──────────────────────────────────────────────────┤
｜处理原因        ｜根据相关规定的情形…
└────────┴──────────────────────────────────────────────────┘
┌────────┬──────────────────────────────────────────────────┐
｜公告日期        ｜2025-06-10
├────────┼──────────────────────────────────────────────────┤
｜实施日期        ｜2025-06-11
├────────┼──────────────────────────────────────────────────┤
｜处理类型        ｜撤销退市风险实施其他
└────────┴──────────────────────────────────────────────────┘
┌────────┬──────────────────────────────────────────────────┐
｜公告日期        ｜2024-04-26
├────────┼──────────────────────────────────────────────────┤
｜实施日期        ｜2024-04-29
├────────┼──────────────────────────────────────────────────┤
｜处理类型        ｜*ST
└────────┴──────────────────────────────────────────────────┘
"""


def test_f10_parser_maps_every_field() -> None:
    rows = st.parse_f10_section("000609", F10_SECTION_000609)
    assert len(rows) == 3, rows
    got = {(r["ann_date"], r["impl_date"], r["action"], r["kind"]) for r in rows}
    assert got == {
        (dt.date(2026, 4, 22), dt.date(2026, 4, 23), "实施", "*ST"),
        (dt.date(2025, 6, 10), dt.date(2025, 6, 11), "撤销", "撤销退市风险实施其他"),
        (dt.date(2024, 4, 26), dt.date(2024, 4, 29), "实施", "*ST"),
    }, got
    assert all(r["source"] == "f10_ledger" and r["confidence"] is None for r in rows)
    assert all(r["code"] == "000609" for r in rows)


def test_parser_handles_both_spellings_of_revoke() -> None:
    """源里同时有「撤销」与「撤消」两种写法，等值比较会漏 ⇒ 必须都归一成撤销。"""
    text = ("【特别处理】\n｜公告日期        ｜2023-05-10\n｜实施日期        ｜2023-05-11\n"
            "｜处理类型        ｜撤消特别处理\n")
    rows = st.parse_f10_section("002102", text)
    assert len(rows) == 1
    assert rows[0]["action"] == "撤销"
    assert rows[0]["kind"] == "撤消特别处理"      # 原文 kind 不被改写


def test_parser_returns_empty_when_section_absent() -> None:
    assert st.parse_f10_section("600000", "【公司概况】\n没有特别处理段落\n") == []


def test_control_parser_detects_field_misalignment() -> None:
    """变异对照：抽掉一条**实施日期**，剩下两条的配对必须原样对得上。

    位置式解析（把所有公告日与所有实施日 zip 起来）在这里会把 2024-04-29 配给
    2025-06-10 那条 ⇒ 断言会红；逐字段解析则是丢弃半截记录并计数。
    """
    broken = F10_SECTION_000609.replace("｜实施日期        ｜2025-06-11\n", "")
    before = int(st._ERROR_COUNT["_finish"])
    rows = st.parse_f10_section("000609", broken)
    assert len(rows) == 2, rows
    assert {(r["ann_date"], r["impl_date"]) for r in rows} == {
        (dt.date(2026, 4, 22), dt.date(2026, 4, 23)),
        (dt.date(2024, 4, 26), dt.date(2024, 4, 29)),
    }, rows
    assert int(st._ERROR_COUNT["_finish"]) > before, "半截记录没被计数（SEM002）"


# 写侧契约（不落第二份 to_parquet） -------------------------------------------
def test_write_panel_uses_parquet_io_and_date32(tmp_path) -> None:
    out = tmp_path / "st_events.parquet"
    st.write_panel(_clean_events(), out)
    assert out.exists()
    import pyarrow.parquet as pq

    schema = pq.read_schema(out)
    for col in ("ann_date", "impl_date"):
        assert str(schema.field(col).type) == "date32[day]", (col, schema.field(col).type)
    assert not list(tmp_path.glob("*.tmp")), "tmp 文件没被 rename 掉"


def test_write_panel_refuses_invalid_frame(tmp_path) -> None:
    dup = _frame([*_clean_events().to_dict("records"),
                  _ev("000609", dt.date(2024, 4, 26), dt.date(2024, 4, 29), "实施")])
    with pytest.raises(ValueError, match="面板未通过校验"):
        st.write_panel(dup, tmp_path / "nope.parquet")


def test_load_panel_round_trips_the_events_it_wrote(tmp_path) -> None:
    """写进去的事件必须能原样读回来，且日期列是 `dt.date` 不是 `Timestamp`。

    这条是本轮实测出来的坑：`date32[day]` 被 pandas 读回是 object-of-`date`，
    消费侧照 datetime64 写 `.dt.date` 会 `AttributeError: Can only use .dt accessor
    with datelike`。整列类型不一样 ⇒ `day_states` 的区间比较会静默错位，所以
    读侧要归一，测试要钉住归一后的**类型**，不只是值。
    """
    src = _clean_events()
    out = tmp_path / "st_events.parquet"
    st.write_panel(src, out)

    back = st.load_panel(out)

    assert len(back) == len(src)
    for col in ("ann_date", "impl_date"):
        assert back[col].map(type).unique().tolist() == [dt.date], (col, back[col].dtypes)
    left = src.sort_values(COLUMNS).reset_index(drop=True)[["code", "ann_date", "impl_date", "action"]]
    right = back.sort_values(COLUMNS).reset_index(drop=True)[list(left.columns)]
    assert left.equals(right), _diff_rows(left, right)
    # confidence 是浮点列：None → NaN，读回仍是 NaN（不能变成字符串 "None"）
    assert back["confidence"].isna().all()


def test_load_panel_missing_file_returns_empty_not_fabricated(tmp_path) -> None:
    back = st.load_panel(tmp_path / "absent.parquet")
    assert list(back.columns) == COLUMNS
    assert len(back) == 0, "缺文件必须给空表，不能造数据"


def test_load_panel_strict_rejects_a_timestamp_typed_panel(tmp_path) -> None:
    """写侧契约的绊线：有人绕过 parquet_io 直接 to_parquet，日期列会是 timestamp。"""
    import pyarrow as pa
    import pyarrow.parquet as pq

    src = _clean_events()
    out = tmp_path / "bad_types.parquet"
    tbl = pa.Table.from_pylist(
        [{**row, "ann_date": pd.Timestamp(row["ann_date"]),
          "impl_date": pd.Timestamp(row["impl_date"])}
         for row in src.to_dict("records")])
    pq.write_table(tbl, out)

    with pytest.raises(ValueError, match="date32"):
        st.load_panel(out)
    loose = st.load_panel(out, strict=False)          # 关严格仍可读，消费侧自己决定
    assert len(loose) == len(src)


def test_module_has_no_import_time_socket_timeout() -> None:
    """AGENTS.md 的超时坑：import 期改全局 socket 默认超时会传染别的源。"""
    src = Path(st.__file__).read_text(encoding="utf-8")
    assert "setdefaulttimeout" not in src
    importlib.reload(st)


def test_control_covered_functions_exist() -> None:
    """形状对照：契约点名的入口必须都在，缺一个这里就红。"""
    for name in ("EVENT_COLUMNS", "ACTIONS", "SOURCES", "AUTHORITY_SOURCES",
                 "INFERRED_SOURCE", "validate", "sub_layer", "day_states",
                 "states_to_events", "parse_f10_section", "write_panel", "load_panel",
                 "covered_ticket_days", "coverage_report",
                 "SNAPSHOT_COLUMNS", "write_snapshot", "read_snapshot", "append_snapshot",
                 "board_events_from_snapshots", "flip_counts", "orphan_revoke_codes"):
        assert hasattr(st, name), name


# G8 定稿 schema：8 列，fetched_at 在、原文列不在 -------------------------------
def test_event_schema_is_the_eight_column_contract() -> None:
    assert list(st.EVENT_COLUMNS) == COLUMNS, list(st.EVENT_COLUMNS)
    assert not st.validate(_clean_events())


def test_no_raw_text_column_creeps_back_into_the_panel() -> None:
    """原文不进面板是收拢后的硬约束：raw 台账已经是可审计底座，再存一份是第二处真相。

    断言两件事：① 列名表里没有 `raw_other`；② 解析器遇到「其他」那一格时**跳过正文**
    而不是把它塞进任何一个结构化字段（`kind` 里必须只可能是 `处理类型` 的原文）。
    """
    assert "raw_other" not in st.EVENT_COLUMNS, st.EVENT_COLUMNS
    real_other_lines = ("｜其他            ｜*ST+ST" + " " * 94 + "｜\n",
                        "｜其他            ｜自2024年4月29日开市起复牌\n",
                        "｜其他            ｜-" + " " * 99 + "｜\n")
    for extra in real_other_lines:
        events = st.parse_f10_section("000609", F10_SECTION_000609 + extra)
        assert len(events) == 3, (extra, len(events))
        assert all("复牌" not in str(e["kind"]) for e in events), [e["kind"] for e in events]
        assert set(events[0]) == set(st.EVENT_COLUMNS), sorted(events[0])


def test_validate_requires_fetched_at() -> None:
    """每行必须能追溯到抓取时刻，否则「面板里这条证据是什么时候的」无法回答。"""
    rows = _clean_events().to_dict("records")
    rows[0]["fetched_at"] = None
    bad = _frame(rows)
    assert any("fetched_at" in p for p in st.validate(bad)), st.validate(bad)


def test_control_action_without_kind_still_rejected() -> None:
    """变异对照：把 action 抹空必须判红（否则 G8 只验了列名没验内容）。"""
    rows = _clean_events().to_dict("records")
    rows[1]["action"] = ""
    assert any("action" in p for p in st.validate(_frame(rows)))


# G9 累积快照表 ----------------------------------------------------------------


def _snap(day, codes, block_name="ST板块", source="block_gn") -> pd.DataFrame:
    return pd.DataFrame(
        [{"snapshot_date": dt.date.fromisoformat(day), "code": c,
          "block_name": block_name, "source": source} for c in codes],
        columns=SNAPSHOT_COLUMNS)


def test_snapshot_table_shape_and_append_dedup(tmp_path) -> None:
    out = tmp_path / "st_board_snapshot.parquet"
    st.append_snapshot(_snap("2026-11-05", ["000010", "000609"]), path=out)
    st.append_snapshot(_snap("2026-11-05", ["000010", "000609", "000752"]), path=out)  # 同日重跑
    st.append_snapshot(_snap("2026-11-06", ["000010", "000752"]), path=out)            # 隔天 000609 出板块
    got = st.read_snapshot(out)
    assert list(got.columns) == SNAPSHOT_COLUMNS
    assert len(got) == 5, got                      # 11-05 三只 + 11-06 两只，同日重复被并掉
    assert sorted(got["snapshot_date"].unique()) == [dt.date(2026, 11, 5), dt.date(2026, 11, 6)]
    import pyarrow.parquet as pq
    assert str(pq.read_schema(out).field("snapshot_date").type) == "date32[day]"


def test_snapshot_accumulation_yields_real_transitions(tmp_path) -> None:
    """累积表的意义：第二次及以后的快照能给出「撤销」这一天，而台账只能回溯几条。"""
    out = tmp_path / "snap.parquet"
    st.append_snapshot(_snap("2026-11-05", ["000010", "000609"]), path=out)
    st.append_snapshot(_snap("2026-11-06", ["000010"]), path=out)
    events = st.board_events_from_snapshots(st.read_snapshot(out))
    by = {(r["code"], r["action"]): r["impl_date"] for r in events.to_dict("records")}
    assert by[("000010", "实施")] == dt.date(2026, 11, 5), by
    assert ("000010", "撤销") not in by, "仍在板块里不该造撤销"
    assert by[("000609", "实施")] == dt.date(2026, 11, 5), by
    assert by[("000609", "撤销")] == dt.date(2026, 11, 6), by


def test_snapshot_gap_is_not_read_as_revoke(tmp_path) -> None:
    """采集断档 ≠ 出板块：缺失的那天必须记成「观测不到」，不能造撤销事件。"""
    out = tmp_path / "snap.parquet"
    st.append_snapshot(_snap("2026-11-05", ["000010"]), path=out)
    st.append_snapshot(_snap("2026-11-09", []), captured_on=dt.date(2026, 11, 9), path=out)
    days = st.captured_days_for(out)
    before = int(st._ERROR_COUNT["board_events_from_snapshots"])
    events = st.board_events_from_snapshots(st.read_snapshot(out), days=days)
    assert not len(events[events["action"] == "撤销"]), events
    assert st.snapshot_gap_days(st.read_snapshot(out), days=days) == 3, "断档天数没数出来"
    assert int(st._ERROR_COUNT["board_events_from_snapshots"]) > before, \
        "空采到日被跳过了却没计数 ⇒ 坏采集会被静默读成集体出板块"


def test_control_snapshot_semantics_are_not_invented(tmp_path) -> None:
    """变异对照：如果实现把「最后一天不在名单」直接当撤销，这条会红。"""
    out = tmp_path / "snap.parquet"
    st.append_snapshot(_snap("2026-11-05", ["000010"]), path=out)
    ev = st.board_events_from_snapshots(st.read_snapshot(out))
    assert not len(ev[ev["action"] == "撤销"]), "只有一次观测就造撤销 ⇒ 右开端点是编的"


# G10 必答覆盖度项：孤儿撤销 / 分档翻转 ------------------------------------------
def test_orphan_revoke_counts_tickets_not_segments() -> None:
    ledger = [
        {"code": "000752", "status": "ok", "events": [{"action": "撤销"}, {"action": "实施"}]},
        {"code": "002102", "status": "ok", "events": [{"action": "撤销"}]},
        {"code": "000609", "status": "ok", "events": [{"action": "实施"}, {"action": "撤销"}]},
        {"code": "000010", "status": "ok", "events": []},
    ]
    codes = st.orphan_revoke_codes(ledger)
    assert codes == {"000752", "002102"}, codes
    rep = st.coverage_report(_clean_events(), ledger_records=ledger)
    assert rep["上限"]["孤儿撤销（最早一条即撤销）票数"] == 2, rep["上限"]


def test_orphan_revoke_uses_the_earliest_dated_entry_not_the_first_listed() -> None:
    """真台账是**新在前**：000609 的三条按 2026→2025→2024 排列（逐字取自 raw JSONL 的形状）。

    按 `events[0]` 判会把「当前已不在 ST」的票数当成「实施端点被截断」的票数——
    那是两个完全不同的量，前一版就是这么混的。这里用带日期的形态把语义钉住：
    **最早**那条是撤销才算孤儿。
    """
    ledger = [
        {"code": "000752", "status": "ok", "events": [
            {"impl_date": "2026-04-27", "action": "实施"},
            {"impl_date": "2024-07-18", "action": "撤销"}]},        # 最早=撤销 ⇒ 孤儿
        {"code": "000609", "status": "ok", "events": [
            {"impl_date": "2026-04-23", "action": "实施"},
            {"impl_date": "2025-06-11", "action": "撤销"},
            {"impl_date": "2024-04-29", "action": "实施"}]},        # 最早=实施 ⇒ 不算
        # 续跑：同码留下多条记录，必须并到一起再判最早
        {"code": "000010", "status": "ok", "events": [
            {"impl_date": "2021-01-04", "action": "实施"}]},
        {"code": "000010", "status": "ok", "events": [
            {"impl_date": "2020-01-02", "action": "撤销"}]},
    ]
    assert st.orphan_revoke_codes(ledger) == {"000752", "000010"}, st.orphan_revoke_codes(ledger)


def _fake_rows(mapping):
    def rows_of(code):
        return mapping.get(code, [])
    return rows_of


def test_flip_counts_only_rows_between_the_two_authority_tiers() -> None:
    """翻转 = 段内、够 5% 档判定值、但按 10% 档判会成为 y=0 的行。

    两个判定值都从权威层 `limit_threshold` 取（`is_st=True` vs `is_st=False`），
    本模块不写死 0.044 / 0.094——写死就又造出第二处口径。
    """
    states = st.day_states(_clean_events())
    rows = {
        "000609": [(dt.date(2024, 5, 6), 0.051),      # 段内、够 5% 档、不够 10% 档 ⇒ 翻转
                   (dt.date(2024, 5, 7), 0.098),      # 够 10% 档 ⇒ 现役已是 y=1
                   (dt.date(2024, 5, 8), 0.031),      # 两档都不够
                   (dt.date(2020, 5, 6), 0.051)],     # 段外（面板没说那天是 ST）
        "000010": [(dt.date(2023, 6, 1), 0.049)],     # 段内 ⇒ 翻转
    }
    rep = st.flip_counts(states, rows_of=_fake_rows(rows))
    assert rep["全历史"]["翻转行数"] == 2, rep
    assert rep["全历史"]["段内行数"] == 4, rep          # 段外那行不计
    assert rep["全历史"]["段内已达现役档行数"] == 1, rep
    assert rep["全历史"]["票数"] == 2, rep
    # 那个「÷ 段内现役正样本」的比值是被**故意删掉**的：真面板里它给的是 13080%。
    # 分母只能来自 quantify 的全市场现役正样本，由调用方搬进来（见 同分母口径）。
    assert "翻转占现役正样本" not in rep["全历史"], rep["全历史"]
    assert rep["全历史"]["翻转占段内行"] == 0.5, rep
    assert rep["全历史"]["段内冲突占比"] == 0.25, rep
    assert rep["分档"]["1998-2005"]["段内行数"] == 0, rep
    assert rep["分档"]["1998-2005"]["翻转行数"] == 0, rep


def test_flip_counts_splits_the_1998_2005_band() -> None:
    """1998–2005 单列一档：训练正样本跨全历史，那段过触发会直接进训练。"""
    states = st.day_states(_frame([
        _ev("000001", None, dt.date(1999, 3, 1), "实施", "*ST"),
        _ev("000001", None, dt.date(2011, 3, 1), "撤销", "撤销特别处理"),
    ]))
    rows = {"000001": [(dt.date(1999, 6, 1), 0.052),    # 段内、1998-2005 档
                       (dt.date(2005, 6, 1), 0.052),    # 段内、1998-2005 档
                       (dt.date(2010, 6, 1), 0.052),   # 段内、2006-2015 档
                       (dt.date(2020, 6, 1), 0.052)]}  # 段外（撤销在 2011-03-01）
    rep = st.flip_counts(states, rows_of=_fake_rows(rows))
    assert rep["全历史"]["翻转行数"] == 3, rep
    assert rep["全历史"]["段内行数"] == 3, rep
    assert rep["分档"]["1998-2005"]["翻转行数"] == 2, rep
    assert rep["分档"]["2006-2015"]["翻转行数"] == 1, rep
    assert rep["分档"]["2016-2026"]["翻转行数"] == 0, rep


def test_control_flip_counts_reject_inverted_tier_direction() -> None:
    """变异对照：把 5% 档判据反着写（够 10% 档才算翻转）必须被判出。"""
    states = st.day_states(_clean_events())
    rows = {"000609": [(dt.date(2024, 5, 7), 0.098)]}
    rep = st.flip_counts(states, rows_of=_fake_rows(rows))
    assert rep["全历史"]["翻转行数"] == 0, rep
