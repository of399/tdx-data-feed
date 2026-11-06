"""B1 读侧供给与四处接线的单元测试（合成数据，零 `data/` 依赖，CI 也判）。

真数据上的「近 30 个交易日 dim1 逐格差=0」在 `tests/test_st_supply_neutrality.py`
（数据依赖，已登记进 `tests/test_ci_data_skip.py::DATA_DEPENDENT_MODULES`）。

本模块**不得**写 `data/` 字样以外的行情读取，也不登记进那张等值表——
`NEEDS_DATA = False` 是它必须能被 CI 判到的前提（形状同
`tests/test_teacher_tag_hardcoding.py`：判据只读源码与合成帧，一遇数据缺失就整片 skip
的守卫等于没有）。

每条测试都指明「要改哪一处生产代码它才会动」：
  · `tdxfeed/st_supply.py` 的三态判据与分层策略（档 1–8）
  · `tdxfeed/features_5d.py` 的 `get_limit(is_st=/on_date=)`、dim1 与 dim15 的按日供给（档 9–13）
  · `quant/multifactor/factors.py` 那层壳的签名与 limit 列（档 14–16）
  · `scripts/train_daban_model.py` 的逐信号日阈值与来源分布（档 17–19）
"""
from __future__ import annotations

import ast
import datetime as dt
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tdxfeed import (  # noqa: E402
    st_history as st,
    st_supply as sup,
)
from tdxfeed.execution.limits import MAIN_ST_RULE_CHANGE_DATE, limit_threshold  # noqa: E402
from tdxfeed.features_5d import (  # noqa: E402
    compute_window_features,
    compute_window_features_extended,
    get_limit,
)

NEEDS_DATA = False

RULE_DAY = MAIN_ST_RULE_CHANGE_DATE


def ev(code: str, impl, action: str, source: str = "f10_ledger", kind: str = "实施其他风险警示"):
    return {"code": code, "ann_date": None, "impl_date": impl, "action": action,
            "kind": kind if action == "实施" else "撤销其他风险警示",
            "source": source,
            "confidence": 0.8 if source == st.INFERRED_SOURCE else None,
            "fetched_at": dt.datetime(2026, 11, 5, 12, 0, 0)}


def frame(events: list[dict]) -> pd.DataFrame:
    """合成事件表：列就是契约列，不写就不配被读。"""
    return pd.DataFrame(events, columns=list(st.EVENT_COLUMNS))


# --------------------------------------------------------------------------- #
# 反向钉住单源例外：`is_st` 这个名字在供给层里必须**不是**名称判定
# --------------------------------------------------------------------------- #
def test_st_supply_is_st_is_not_a_name_judgement() -> None:
    """`tests/test_names_single_source.py::ST_DEF_EXCEPTIONS` 里那条 "other" 例外的真实性。

    那条例外允许 `tdxfeed/st_supply.py` 定义名为 `is_st` 的函数，理由是「它判的是面板区间
    覆盖，不是名称」。这个理由必须可判：签名里不许出现 `name`、函数体里不许调 `is_st_name`、
    整个模块不许出现名称判定。否则哪天有人真往里面塞一份名称判定，例外就把守卫的那只眼
    彻底遮住了（同文件里的 `is_st` 会被例外一口吃掉）。
    """
    src = (ROOT / "tdxfeed" / "st_supply.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "is_st"), None)
    assert fn is not None, "st_supply 里没有 is_st 了 ⇒ 例外登记该删，这条判据也要一起删"
    args = [a.arg for a in fn.args.args] + [a.arg for a in fn.args.kwonlyargs]
    assert "name" not in args, f"is_st 的签名里出现了 name：{args} ⇒ 它开始判名称了，例外不再成立"
    body_calls = {n.func.id for n in ast.walk(fn)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert not (body_calls & {"is_st_name", "_is_st_name"}), \
        f"is_st 的函数体调了名称判定：{sorted(body_calls)}"
    assert "is_st_name" not in src, "供给层不该出现名称判定，否则两份口径会在同一个模块里并存"


# --------------------------------------------------------------------------- #
# 档 1–8：三态与分层策略
# --------------------------------------------------------------------------- #
def test_authority_interval_yes_inside_and_no_after_the_visible_revoke() -> None:
    """实施 2024-01-02 / 撤销 2024-06-01 ⇒ 段内是、段外不是；右开。"""
    s = sup.StSupply(frame([
        ev("000001", dt.date(2024, 1, 2), "实施"),
        ev("000001", dt.date(2024, 6, 1), "撤销"),
    ]))
    assert s.is_st("000001", dt.date(2024, 3, 1)) == (True, sup.TIER_AUTHORITY)
    assert s.is_st("sz000001", dt.date(2024, 3, 1)) == (True, sup.TIER_AUTHORITY), "前缀要归一"
    assert s.is_st("000001", dt.date(2024, 6, 1)) == (False, sup.TIER_AUTHORITY), "[实施, 撤销) 右开"
    assert s.is_st("000001", dt.date(2024, 7, 1)) == (False, sup.TIER_AUTHORITY)


def test_dates_before_the_earliest_visible_event_are_unreachable_not_false() -> None:
    """台账深度不均会藏掉更早区间 ⇒ 早于最早可见边界只能「无法判定」，不许折成 False。

    这是用户点名的第 3 条里最容易被做错的一处：4,739 只「暂无数据」+ 深度 0–5 条不均，
    把「看不见」写成「不是 ST」= 把漏标重新洗成权威。
    """
    s = sup.StSupply(frame([
        ev("600001", dt.date(2018, 5, 6), "实施"),
        ev("600001", dt.date(2019, 5, 6), "撤销"),
    ]))
    assert s.is_st("600001", dt.date(2005, 1, 4)) == (False, sup.TIER_UNSUPPLIED)
    assert s.is_st("600001", dt.date(2018, 5, 5)) == (False, sup.TIER_UNSUPPLIED)


def test_code_with_no_record_at_all_is_unreachable() -> None:
    """90.63% 的票整张表里一条没有 ⇒ 每一天都是无法判定，而不是「非 ST」。"""
    s = sup.StSupply(frame([ev("000002", dt.date(2024, 1, 2), "实施")]))
    assert s.is_st("301999", dt.date(2024, 3, 1)) == (False, sup.TIER_UNSUPPLIED)


def test_open_segment_has_no_end_so_later_days_stay_st() -> None:
    """只有实施、没有撤销 = 至今仍 ST；开口段算到可见的未来，不许判成「已撤销」。"""
    s = sup.StSupply(frame([ev("000003", dt.date(2025, 3, 3), "实施")]))
    assert s.is_st("000003", dt.date(2026, 11, 5)) == (True, sup.TIER_AUTHORITY)
    assert s.is_st("000003", RULE_DAY) == (True, sup.TIER_AUTHORITY), "并轨日仍是 ST，只是档变了"


def test_orphan_revoke_only_proves_no_from_the_revoke_onwards() -> None:
    """孤儿撤销（首条即撤销）：撤销日之后可判「否」，之前一律「无法判定」。"""
    s = sup.StSupply(frame([ev("000004", dt.date(2020, 7, 1), "撤销")]))
    assert s.is_st("000004", dt.date(2020, 7, 1)) == (False, sup.TIER_AUTHORITY)
    assert s.is_st("000004", dt.date(2019, 1, 1)) == (False, sup.TIER_UNSUPPLIED)
    assert s.is_st("000004", dt.date(2022, 1, 1)) == (False, sup.TIER_AUTHORITY)


def test_price_inferred_is_not_read_by_default() -> None:
    """默认只读权威层：只有 price_inferred 记录的票 ⇒ 无法判定（不洗白）。"""
    events = frame([ev("000005", dt.date(2010, 4, 1), "实施", source="price_inferred")])
    assert sup.StSupply(events).is_st("000005", dt.date(2010, 5, 1)) == (False, sup.TIER_UNSUPPLIED)


def test_price_inferred_enters_only_through_the_explicit_switch_and_keeps_its_tier() -> None:
    """`include_inferred=True` 才并推断，且档位必须写成「推断」，让下游分得出来。"""
    events = frame([ev("000006", dt.date(2010, 4, 1), "实施", source="price_inferred")])
    s = sup.StSupply(events, include_inferred=True)
    assert s.is_st("000006", dt.date(2010, 5, 1)) == (True, sup.TIER_INFERRED)


def test_authority_no_beats_inferred_yes() -> None:
    """权威说「不是」而推断说「是」时听权威的 ⇒ 推断永远只在权威判不出时生效。

    这条是「把 96.4% 的推断行洗成权威」的正面防线：反向优先级会让两臂对照失去意义。
    """
    events = frame([
        ev("000007", dt.date(2011, 1, 4), "实施"),
        ev("000007", dt.date(2011, 6, 1), "撤销"),
        ev("000007", dt.date(2011, 7, 1), "实施", source="price_inferred"),
    ])
    s = sup.StSupply(events, include_inferred=True)
    assert s.is_st("000007", dt.date(2011, 7, 4)) == (False, sup.TIER_AUTHORITY)


# --------------------------------------------------------------------------- #
# B2 臂供给：`sources=` 决定读哪几层**权威**（臂 A 与臂 A2 的唯一差别就在这里）
# --------------------------------------------------------------------------- #
def test_sources_subset_decides_which_authority_layers_are_read() -> None:
    """臂 A 必须只看 `f10_ledger` + `block_gn`；只有深市改名层的票在 A 里判不出。

    这条例外是 §13.3 那个「臂 A 只解释 15.81%」能成立的前提：如果 `sources=` 其实没过滤，
    A 与 A2 会给同一套标签，两臂对照就变成同一臂跑两遍（而数字上完全看不出来）。
    """
    events = frame([
        ev("600001", dt.date(2024, 1, 2), "实施"),                        # 只有 f10_ledger
        ev("600001", dt.date(2024, 2, 2), "撤销"),
        ev("600002", dt.date(2024, 1, 2), "实施", source="akshare_sz"),    # 只有深市改名层
        ev("600002", dt.date(2024, 2, 2), "撤销", source="akshare_sz"),
        ev("600003", dt.date(2026, 11, 4), "实施", source="block_gn"),     # 当日成员快照，开口段
    ])
    a = sup.StSupply(events, sources=("f10_ledger", "block_gn"))
    assert a.is_st("600001", dt.date(2024, 1, 10)) == (True, sup.TIER_AUTHORITY)
    assert a.is_st("600002", dt.date(2024, 1, 10)) == (False, sup.TIER_UNSUPPLIED)
    assert a.is_st("600003", dt.date(2026, 11, 5)) == (True, sup.TIER_AUTHORITY)
    a2 = sup.StSupply(events, sources=st.AUTHORITY_SOURCES)
    assert a2.is_st("600002", dt.date(2024, 1, 10)) == (True, sup.TIER_AUTHORITY)


def test_sources_none_is_the_three_authority_layers() -> None:
    """`sources=None` 必须逐值等于 `AUTHORITY_SOURCES`：B1 的默认口径不能因为加参数而漂。"""
    events = frame([
        ev("600010", dt.date(2024, 1, 2), "实施"),
        ev("600011", dt.date(2024, 1, 2), "实施", source="akshare_sz"),
        ev("600012", dt.date(2026, 11, 4), "实施", source="block_gn"),
        ev("600013", dt.date(2010, 1, 4), "实施", source="price_inferred"),
    ])
    days = [dt.date(2024, 1, 10), dt.date(2026, 11, 5), dt.date(2010, 2, 1)]
    codes = ["600010", "600011", "600012", "600013"]
    default = sup.StSupply(events)
    explicit = sup.StSupply(events, sources=st.AUTHORITY_SOURCES)
    got_default = [(c, d, default.is_st(c, d)) for c in codes for d in days]
    got_explicit = [(c, d, explicit.is_st(c, d)) for c in codes for d in days]
    assert got_default == got_explicit


def test_unknown_source_name_in_sources_raises() -> None:
    """源名写错必须抛，不许静默读成「这一层没有」——那会把臂 A 悄悄变成臂 ctl。"""
    events = frame([ev("600020", dt.date(2024, 1, 2), "实施")])
    with pytest.raises(ValueError) as exc:
        sup.StSupply(events, sources=("f10_leder",))        # 故意少一个 g
    assert "f10_leder" in str(exc.value), exc.value
    assert "未知" in str(exc.value), "要说清是未知源名，而不是别的错"


def test_empty_sources_raises_instead_of_faking_the_ctl_arm() -> None:
    """空 `sources` 会把每一行报成「无法判定」，那是**面板缺席**的形状，不是 ctl。

    ctl 臂走 `supply=None`（档位记「未供给」）；把两者混起来，来源分布那一行就会说
    「面板覆盖不到」，而实际是「这次故意没读面板」——两臂对照的分母就此失真。
    """
    events = frame([ev("600021", dt.date(2024, 1, 2), "实施")])
    with pytest.raises(ValueError) as exc:
        sup.StSupply(events, sources=())
    assert "ctl" in str(exc.value), exc.value


def test_inferred_source_cannot_be_packed_into_the_authority_subset() -> None:
    """`sources` 只收权威三层；塞进 `price_inferred` 会把它当权威读（契约 1 禁止）。"""
    events = frame([ev("600022", dt.date(2011, 7, 1), "实施", source="price_inferred")])
    with pytest.raises(ValueError) as exc:
        sup.StSupply(events, sources=(st.INFERRED_SOURCE,))
    assert st.INFERRED_SOURCE in str(exc.value) and "权威" in str(exc.value), exc.value
    with pytest.raises(ValueError):
        sup.StSupply(events, sources=("f10_ledger", st.INFERRED_SOURCE),
                     include_inferred=True)


def test_inferred_layers_on_top_of_the_chosen_authority_subset() -> None:
    """`include_inferred` 叠在**所选**权威子集上：臂 I = A2 + 推断，不是「推断单独一层」。"""
    events = frame([
        ev("600030", dt.date(2011, 1, 4), "实施"),
        ev("600030", dt.date(2011, 6, 1), "撤销"),
        ev("600030", dt.date(2011, 7, 1), "实施", source="price_inferred"),
        ev("600031", dt.date(2011, 7, 1), "实施", source="price_inferred"),
    ])
    s = sup.StSupply(events, sources=("f10_ledger", "block_gn"), include_inferred=True)
    assert s.is_st("600031", dt.date(2011, 8, 1)) == (True, sup.TIER_INFERRED)
    assert s.is_st("600030", dt.date(2011, 8, 1)) == (False, sup.TIER_AUTHORITY)


def test_load_passes_sources_through(tmp_path: Path) -> None:
    """`StSupply.load(sources=...)` 必须真的落到分层，而不是只在构造函数里生效。"""
    events = frame([ev("600040", dt.date(2024, 1, 2), "实施", source="akshare_sz")])
    target = tmp_path / "st_events.parquet"
    st.write_panel(events, path=target)
    a = sup.StSupply.load(path=target, sources=("f10_ledger", "block_gn"))
    a2 = sup.StSupply.load(path=target, sources=st.AUTHORITY_SOURCES)
    assert a.is_st("600040", dt.date(2024, 1, 10)) == (False, sup.TIER_UNSUPPLIED)
    assert a2.is_st("600040", dt.date(2024, 1, 10)) == (True, sup.TIER_AUTHORITY)


# --------------------------------------------------------------------------- #
# 四臂表：单源定义，训练侧与对照侧共用（两处各写一份就会漂）
# --------------------------------------------------------------------------- #
def test_arm_table_is_the_single_definition_of_the_four_arms() -> None:
    assert sup.ARMS == ("ctl", "A", "A2", "I")
    assert sup.ARM_PLAN["ctl"] == {"sources": (), "include_inferred": False}
    assert sup.ARM_PLAN["A"] == {"sources": ("f10_ledger", "block_gn"),
                                 "include_inferred": False}
    assert sup.ARM_PLAN["A2"] == {"sources": tuple(st.AUTHORITY_SOURCES),
                                  "include_inferred": False}
    assert sup.ARM_PLAN["I"] == {"sources": tuple(st.AUTHORITY_SOURCES),
                                 "include_inferred": True}
    # 臂的单调性：A ⊂ A2 ⊂ I， ctl 不含任何权威层（否则「归因基线」不成立）
    assert set(sup.ARM_PLAN["A"]["sources"]) < set(sup.ARM_PLAN["A2"]["sources"])
    assert sup.ARM_PLAN["I"]["include_inferred"] and not sup.ARM_PLAN["A2"]["include_inferred"]


def test_build_supply_for_ctl_returns_none_and_is_not_a_panel_failure() -> None:
    """ctl 是「ST 档强制关」这一**设计**，不是面板缺失；构造函数必须能区分这两件事。"""
    assert sup.build_supply("ctl") is None
    assert sup.CTL_ARM == "ctl"


def test_build_supply_rejects_unknown_arm(tmp_path: Path) -> None:
    events = frame([ev("600050", dt.date(2024, 1, 2), "实施")])
    target = tmp_path / "st_events.parquet"
    st.write_panel(events, path=target)
    with pytest.raises(ValueError) as exc:
        sup.build_supply("a2", panel_path=target)      # 大小写也算未知
    assert "a2" in str(exc.value) and "A2" in str(exc.value)


def test_build_supply_by_arm_reads_only_that_arm_layers(tmp_path: Path) -> None:
    events = frame([
        ev("600060", dt.date(2024, 1, 2), "实施", source="akshare_sz"),
        ev("600061", dt.date(2011, 7, 1), "实施", source="price_inferred"),
    ])
    target = tmp_path / "st_events.parquet"
    st.write_panel(events, path=target)
    a = sup.build_supply("A", panel_path=target)
    a2 = sup.build_supply("A2", panel_path=target)
    arm_i = sup.build_supply("I", panel_path=target)
    assert a.is_st("600060", dt.date(2024, 1, 10)) == (False, sup.TIER_UNSUPPLIED)
    assert a2.is_st("600060", dt.date(2024, 1, 10)) == (True, sup.TIER_AUTHORITY)
    assert a2.is_st("600061", dt.date(2011, 8, 1)) == (False, sup.TIER_UNSUPPLIED)
    assert arm_i.is_st("600061", dt.date(2011, 8, 1)) == (True, sup.TIER_INFERRED)


# --------------------------------------------------------------------------- #
# ctl 臂的等价性：强制关 ST 档 ⇒ 逐值等于改动前「不传日期」的老口径（纯算术，不碰数据）
# --------------------------------------------------------------------------- #
def test_ctl_arm_is_exactly_the_old_scalar_caliber() -> None:
    """`limit_threshold(code, is_st=False, on_date=任何一天)` 与 `get_limit(code)` 逐值相同。

    这条是 ctl 能当**归因基线**的全部依据：内核里唯一的日期规则就是主板 ST 并轨
    （`limits.limit_ratio` 只在 `market==main and is_st` 时看 `on_date`），所以关掉 ST 档
    之后传不传日期都一样。一旦将来给非 ST 行也加日期规则（例如创业板 2020-08-24 之前
    真是 10%），这条会立刻红——那时 ctl 的定义必须重写，不能沿用。
    """
    dates = [dt.date(1998, 1, 5), dt.date(2010, 5, 6), dt.date(2020, 8, 24),
             dt.date(2026, 7, 5), MAIN_ST_RULE_CHANGE_DATE, dt.date(2026, 11, 4)]
    repr_codes = ["600000", "000001", "002309", "300750", "688981", "920001", "830799"]
    for code in repr_codes:
        old = get_limit(code)                       # 现役研究脚本仍在用的「只传代码」形状
        off = sup.limit_series(code, dates, None)
        assert set(off[0]) == {old}, (code, off[0], old)
        assert set(off[2]) == {False}
        assert set(off[1]) == {sup.TIER_NO_SUPPLY}
        for d in dates:
            forced_off = limit_threshold(code, is_st=False, on_date=d)
            assert forced_off == old, (code, d, forced_off, old)


# --------------------------------------------------------------------------- #
# 装载：缺面板必须响亮
# --------------------------------------------------------------------------- #
def test_load_missing_panel_raises_instead_of_degrading(tmp_path: Path) -> None:
    with pytest.raises(sup.PanelUnavailableError) as exc:
        sup.StSupply.load(path=tmp_path / "nope.parquet")
    msg = str(exc.value)
    assert "make data-st-history" in msg and "KI-DATA-008" in msg, msg
    assert "不存在" in msg, "要指名是哪个路径不在场，别只说「不可用」"


def test_empty_panel_is_refused_by_the_supply_layer(tmp_path: Path) -> None:
    """空事件表 ⇒ 供给层拒跑；写侧仍允许空表（A 批契约），职责在供给这一层。"""
    with pytest.raises(sup.PanelUnavailableError):
        sup.StSupply(frame([]))


def test_load_reads_a_real_panel_written_by_the_single_write_entry(tmp_path: Path) -> None:
    """走 `st.write_panel`（唯一写入口）落盘再 `load`，逐格与合成帧一致。"""
    events = frame([
        ev("000909", dt.date(2024, 2, 5), "实施"),
        ev("000909", dt.date(2025, 1, 6), "撤销"),
    ])
    target = tmp_path / "st_events.parquet"
    st.write_panel(events, path=target)
    s = sup.StSupply.load(path=target)
    assert s.is_st("000909", dt.date(2024, 6, 1)) == (True, sup.TIER_AUTHORITY)
    assert s.is_st("000909", dt.date(2025, 2, 1)) == (False, sup.TIER_AUTHORITY)


# --------------------------------------------------------------------------- #
# 来源分布那一行
# --------------------------------------------------------------------------- #
def test_tally_line_names_three_counts_and_explains_the_unreachable_one() -> None:
    t = sup.TierTally()
    t.add(sup.TIER_AUTHORITY)
    t.add(sup.TIER_UNSUPPLIED)
    assert t.line() == "ST 供给来源分布：权威 1 行 / 推断 0 行 / 无法判定 1 行"
    joined = "\n".join(t.detail())
    assert "90.63%" in joined, "无法判定必须带实测原因，不能只给一个数"
    with pytest.raises(ValueError):
        t.add("瞎写的档")


# --------------------------------------------------------------------------- #
# 档 9–13：features_5d 的按日供给
# --------------------------------------------------------------------------- #
def _daily(rows: int = 24, start: dt.date = dt.date(2024, 1, 2)) -> pd.DataFrame:
    d = pd.date_range(start, periods=rows, freq="D")
    close = [10.0 + 0.01 * r for r in range(rows)]
    return pd.DataFrame({
        "date": d, "open": close, "high": [c * 1.01 for c in close],
        "low": [c * 0.99 for c in close], "close": close,
        "volume": [1e6 + r for r in range(rows)], "amount": [1e7 + r for r in range(rows)],
    })


def test_get_limit_is_st_argument_wins_over_the_name_fallback() -> None:
    """面板给得出 ST 状态就直接用，不必绕名称；`is_st=None` 才回落 `_is_st_name(name)`。"""
    pre = dt.date(2024, 1, 2)
    assert get_limit("600000", on_date=pre, is_st=True) == 0.045
    assert get_limit("600000", on_date=pre, is_st=False) == 0.095
    assert get_limit("600000", name="*ST 某某", on_date=pre) == 0.045
    assert get_limit("600000", name="某某股份", on_date=pre) == 0.095
    # 显式 is_st 覆盖名称：面板是权威，名称只是当日快照
    assert get_limit("600000", name="*ST 某某", on_date=pre, is_st=False) == 0.095


def test_dim1_limit_follows_the_signal_date_when_supplied() -> None:
    """并轨前主板 ST 的信号日：dim1 必须是 0.045（这正是用户要的「历史段允许变」）。"""
    df = _daily(start=dt.date(2024, 1, 2))
    plain = compute_window_features(df, "600678", sig_idx=len(df) - 1)
    wired = compute_window_features(df, "600678", sig_idx=len(df) - 1, is_st=True)
    assert plain["limit"] == 0.095
    assert wired["limit"] == 0.045
    assert wired["is_st"] == 1 and plain["is_st"] == 0
    # 5 日内涨停次数这一维跟着阈值走（判定值变了，次数不能不动）
    assert wired["limit_up_5d"] == plain["limit_up_5d"]  # 本合成帧涨幅 0.1%，两档都不触发


def test_default_feature_call_is_bit_identical_to_today() -> None:
    """不传 is_st ⇒ 整份 18 维与今天逐值相同（新增关键字不许动默认行为）。"""
    df = _daily(start=dt.date(2023, 3, 1))
    old_shape = compute_window_features(df, "300111", sig_idx=10)
    explicit_none = compute_window_features(df, "300111", sig_idx=10, is_st=None)
    assert old_shape == explicit_none
    assert old_shape["limit"] == limit_threshold("300111") == 0.195, "双创没有单独 ST 档"


def test_extended_features_column_set_does_not_grow() -> None:
    """29 维契约保护：接日期不能顺手加列（`train_daban_model` 把多余键当特征吃）。"""
    df = _daily(start=dt.date(2024, 1, 2))
    base = compute_window_features(df, "600111", sig_idx=len(df) - 1)
    ext = compute_window_features_extended(df, "600111", sig_idx=len(df) - 1, is_st=True)
    assert ext is not None and base is not None
    assert len(base) == 18 and len(ext) == 29, (len(base), len(ext))
    assert set(ext) - set(base) == {
        "acceleration", "range_efficiency", "vol_norm_ret", "amount_surge_3d",
        "up_down_ratio", "dist_to_5d_high", "dist_to_5d_low", "amt_per_price",
        # 2026-10-18 那轮加的 3 个涨停维（docstring 写「26 旧 + 3 新」，实数 29 列）
        "board_position", "is_limit_up_today", "consecutive_boards"}, \
        "扩展维只许是那 11 个工程化/涨停特征；多出别的键（比如档位、来源）就是往矩阵里塞元数据"
    assert ext["limit"] == 0.045 and ext["is_st"] == 1


# --------------------------------------------------------------------------- #
# 档 14–16：quant/multifactor/factors.py 那层壳
# --------------------------------------------------------------------------- #
def test_multifactor_shell_accepts_date_and_st() -> None:
    from quant.multifactor.factors import get_limit as mf_get_limit

    assert mf_get_limit("600000") == 0.095
    assert mf_get_limit("600000", on_date=dt.date(2024, 1, 2), is_st=True) == 0.045
    assert mf_get_limit("300750", on_date=dt.date(2024, 1, 2), is_st=True) == 0.195


def test_compute_stock_raw_limit_column_is_per_date_when_supplied() -> None:
    """未收编的 stage1 因子层：limit 列要能按日给值；没供给时与今天的标量逐格相同。"""
    from quant.multifactor.factors import compute_stock_raw

    df = _daily(start=dt.date(2024, 1, 2))
    plain = compute_stock_raw(df, "600678")
    assert set(plain["limit"].unique()) == {0.095}
    panel = frame([ev("600678", dt.date(2024, 1, 3), "实施"),
                   ev("600678", dt.date(2024, 1, 20), "撤销")])
    s = sup.StSupply(panel)
    wired = compute_stock_raw(df, "600678", st_supply=s)
    assert 0.045 in set(wired["limit"]), sorted(set(wired["limit"]))
    # 推断/权威以外的日子仍应是 0.095
    assert 0.095 in set(wired["limit"])


def test_compute_stock_raw_without_supply_matches_the_old_scalar_column() -> None:
    from quant.multifactor.factors import compute_stock_raw

    df = _daily(start=dt.date(2019, 1, 2), rows=40)
    plain = compute_stock_raw(df, "600222")
    assert plain["limit"].tolist() == [0.095] * 40
    assert plain["zt_density_20d"].notna().sum() > 0


# --------------------------------------------------------------------------- #
# 档 17–19：训练脚本侧
# --------------------------------------------------------------------------- #
def _train_mod():
    """按文件路径加载训练脚本（`scripts/` 不是包，且它自己会补 sys.path）。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "train_daban_model_under_test", ROOT / "scripts" / "train_daban_model.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_build_dataset_uses_the_signal_date_for_the_threshold() -> None:
    """逐信号日取档：同一只票在并轨前的 ST 段里应拿到 0.045。

    这条测的是训练侧的「阈值向量」构造函数（不读真数据、不训练）。
    """
    m = _train_mod()
    df = _daily(start=dt.date(2024, 1, 2), rows=30)
    panel = frame([ev("600333", dt.date(2024, 1, 3), "实施"),
                   ev("600333", dt.date(2024, 1, 25), "撤销")])
    s = sup.StSupply(panel)
    limits, tiers, flags = m.limit_series_for_stock(df, "600333", s)
    # 2024-01-02 早于该票最早可见边界（实施日 01-03）⇒ 那行是「无法判定」，不是「非 ST」
    assert tiers[0] == sup.TIER_UNSUPPLIED and flags[0] is False
    assert tiers[1:].count(sup.TIER_AUTHORITY) == len(df) - 1
    assert 0.045 in list(limits) and 0.095 in list(limits)
    assert list(flags)[:3] == [False, True, True]
    # 没供给时：逐格仍是今天这一套值（行为中性的单元层证据）
    limits0, tiers0, _flags0 = m.limit_series_for_stock(df, "600333", None)
    assert set(limits0) == {0.095}
    assert set(tiers0) == {sup.TIER_NO_SUPPLY}


def test_train_cli_has_no_way_to_skip_the_panel() -> None:
    """拒跑是硬约束：训练入口的供给装载函数在没有面板时抛 PanelUnavailableError。"""
    m = _train_mod()
    with pytest.raises(sup.PanelUnavailableError):
        m.load_st_supply(panel_path=Path("/nonexistent/st_events.parquet"))


def test_train_entry_argparse_actually_builds() -> None:
    """真跑一次在役入口的 argparse（`--help` 会 short-circuit，不读盘也不写产物）。

    这条是被自己的缺陷逼出来的：2026-11-05 本批新加的 `--include-inferred` 在 help 串里
    写了裸 `96.4%`，Python 3.14 的 argparse 在 **add_argument 当场** 抛
    `ValueError: badly formed help string`，而全量测试当时全绿——因为没有任何用例真的构造过
    这个 parser（`main()` 只在人跑的时候被跑到）。help 串里的百分号必须写成 `%%`。
    """
    import subprocess

    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "train_daban_model.py"),
                        "--help"], capture_output=True, text=True)
    assert r.returncode == 0, f"--help 退出码 {r.returncode}：{r.stderr[-600:]}"
    assert "--include-inferred" in r.stdout, "新开关没进 parser，help 里看不到它"


def test_train_prints_the_distribution_from_the_rows_it_actually_supplied(tmp_path: Path) -> None:
    """来源分布那一行由**样本行**记账（不是查询次数），且三档齐全。"""
    m = _train_mod()
    tally = sup.TierTally()
    for tier in (sup.TIER_AUTHORITY, sup.TIER_UNSUPPLIED, sup.TIER_AUTHORITY):
        tally.add(tier)
    assert m.supplied_rows_line(tally) == (
        "ST 供给来源分布：权威 2 行 / 推断 0 行 / 无法判定 1 行")
