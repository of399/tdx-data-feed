"""按 (代码, 交易日) 供给 ST 状态：三态、分层、缺面板就拒跑（B1 的读侧）。

═══════════════════════════════════════════════════════════════════════════════
【为什么要有这一层，而不是让训练脚本自己读面板】三个坑必须在同一个地方堵住：

1. **推断不许被洗白成权威。** 面板 79,552 行里 `price_inferred` 占 76,693（96.4%），
   权威三层合计只有 2,859 行。把整张表当「ST 状态」读，等于把价格反推（与台账互证的
   precision ≈0.8）静默当标签供给，下一轮没人读得出来。所以**默认只读权威层**
   （`f10_ledger` + `block_gn` + `akshare_sz`），推断层只能由显式
   `include_inferred=True` 进来（给 B2 的 `I` 臂对照用），而且**只在权威层判不出时**才生效：
   权威说「不是」而推断说「是」，听权威的（`test_authority_no_beats_inferred_yes`）。
2. **「无台账记录」不等于「非 ST」。** 全市场 5,229 只里 4,739 只（**90.63%**）的
   【特别处理】栏是「暂无数据」，有记录的深度还不均（实测 1–5 条，不存在「每票最多 3 条」
   那种硬上限）。所以对没有记录的代码、以及早于该代码最早一条**可见边界**的日期，
   答案是 `无法判定` 而不是 False。返回三态就是为了把这一层留在可见状态。
3. **面板不在场就响亮报错。** `st.load_panel` 缺文件返回空表（那是读侧的诚实），
   但供给层拿到空/缺失面板必须抛 `PanelUnavailableError`：静默退回「不传日期」等于把
   KI-DATA-008 的缺陷本体重新装回生产链，而且没人知道。这与 `AGENTS.md`
   「判据在当下样本跑不动就写不可用+实测原因，别交 NaN/空表」是同一条约定。

【档位词表】`权威` / `推断` / `无法判定` 是**给了面板**的三种结论；`未供给` 是第四种情形
（调用方根本没给面板——现役 `scripts/predict.py` 与一批研究脚本仍在这一档）。
两者分开，报告里才分得清「面板的结构性上限」与「这一批还没接线」。

【计数归谁】`is_st()` / `st_mask()` / `limit_series()` 都是**纯查询**，不自计次数。
来源分布那一行由调用方按**它真正供给出去的样本行**记账（`TierTally`）——A 批
`coverage.json` 里 `day_states` 那个 420 就是跨多次分层调用累加出来的次数，
既不是票数也不是段数；这里不重犯那个分母混淆。

【与阈值合同的接法】本模块只回答「那天是不是 ST」，档值一律由
`tdxfeed.execution.limits.limit_threshold` 给（单一实现，见 `tdxfeed/features_5d.py:64`）。
`limit_series()` 每只票只对 `limit_threshold` 取两个数（并轨前 ST 档、现行档），
再按日选，所以逐日供给不会把 16.7M 行的算术变成 16.7M 次调用。
"""
from __future__ import annotations

import bisect
import datetime as dt
from collections import Counter
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

import pandas as pd

from . import paths, st_history as st
from .execution import limits as _limits

__all__ = [
    "ARMS",
    "ARM_NOTES",
    "ARM_PLAN",
    "CTL_ARM",
    "TIERS",
    "TIER_AUTHORITY",
    "TIER_INFERRED",
    "TIER_NO_SUPPLY",
    "TIER_UNSUPPLIED",
    "PanelUnavailableError",
    "StSupply",
    "TierTally",
    "build_supply",
    "limit_series",
]

TIER_AUTHORITY = "权威"
TIER_INFERRED = "推断"
TIER_UNSUPPLIED = "无法判定"
TIER_NO_SUPPLY = "未供给"

# `TierTally` 认的四档；前三个是「给了面板的三种结论」，第四个是「没给面板」。
TIERS: tuple[str, ...] = (TIER_AUTHORITY, TIER_INFERRED, TIER_UNSUPPLIED, TIER_NO_SUPPLY)

# 写进报告的实测原因（数字取自本轮 `data/st_history/coverage.json`，不是手抄的估计）
_UNREACHABLE_NOTE = (
    "无法判定 = 该票该日没有任何一层给得出可见边界。实测原因：全市场 5,229 只里 "
    "4,739 只（90.63%）F10【特别处理】栏「暂无数据」，权威层只有 490 只有事件、"
    "865 只有可判区间；深度 1–5 条不均 ⇒ 更早的区间可能被台账藏掉。"
)
_NO_SUPPLY_NOTE = (
    "未供给 = 调用方没有传面板（现役 predict 与研究脚本仍在这一档），"
    "这些行按现行「不传日期」口径供给，是待收编面，不是判不出。"
)
_INFERRED_OFF_NOTE = "推断档 0 行 ⇒ 本次为默认权威层（显式 include_inferred=True 才并推断）"

_RULE_DAY = _limits.MAIN_ST_RULE_CHANGE_DATE
_ONE_DAY = dt.timedelta(days=1)

# `sources=` 的合法词表：**只有权威三层**。推断层不在这里——它靠
# `include_inferred=True` 单独进来并单独记档（塞进 sources 就等于把推断洗成权威，
# 是模块开头契约 1 明令禁止的那件事）。
_AUTHORITY_SET = frozenset(st.AUTHORITY_SOURCES)


def _check_sources(sources: Sequence[str] | None) -> tuple[str, ...]:
    """臂的层次必须**可判**：写错的源名、空层、把推断当权威，都在这里拒掉。"""
    if sources is None:
        return tuple(st.AUTHORITY_SOURCES)
    got: list[str] = []
    for s in sources:
        if s not in got:
            got.append(str(s))
    if not got:
        raise ValueError(
            "sources=() 表示「一层权威都不读」，那会把每一行都报成「无法判定」——"
            "这是**面板覆盖不到**的形状，不是「故意关掉 ST 档」。ctl 臂（归因基线）请走 "
            "supply=None / `build_supply(CTL_ARM)`，档位记「未供给」，两者不能混。")
    bad = [s for s in got if s not in _AUTHORITY_SET]
    if bad:
        raise ValueError(
            f"未知或非权威的源名 {bad}：sources 只收权威三层 {tuple(st.AUTHORITY_SOURCES)}；"
            f"推断层 {st.INFERRED_SOURCE} 要用 include_inferred=True，不混进「权威」那一档")
    return tuple(got)


# --------------------------------------------------------------------------- #
# 四臂表（B2 单源定义）：训练侧与对照侧共用，两处各写一份迟早会漂
# --------------------------------------------------------------------------- #
CTL_ARM = "ctl"
ARMS: tuple[str, ...] = (CTL_ARM, "A", "A2", "I")

#: 每臂读哪几层权威 + 是否并推断层。`ctl` 的 `sources=()` 是**哨兵**（它不建供给，
#: 见 `build_supply`），不是「读空层」——后者会被 `_check_sources` 直接拒掉。
ARM_PLAN: dict[str, dict[str, Any]] = {
    "ctl": {"sources": (), "include_inferred": False},
    "A": {"sources": ("f10_ledger", "block_gn"), "include_inferred": False},
    "A2": {"sources": tuple(st.AUTHORITY_SOURCES), "include_inferred": False},
    "I": {"sources": tuple(st.AUTHORITY_SOURCES), "include_inferred": True},
}

#: 每臂一句话说明，训练侧与对照侧都从这里取（两处各写一份就会有一处过期）。
ARM_NOTES: dict[str, str] = {
    "ctl": "ST 档强制关（走「未供给」档）：阈值逐值等于改动前的旧口径 ⇒ 归因基线",
    "A": "权威层只读 f10_ledger + block_gn（不含深市改名层）",
    "A2": "权威三层并读 f10_ledger + block_gn + akshare_sz（B1 之后的默认生产口径）",
    "I": "权威三层 + price_inferred（推断只在权威判不出时生效，档位仍记「推断」）",
}


def build_supply(arm: str, *,
                 panel_path: Path | str | None = None) -> StSupply | None:
    """按臂建供给。`arm` 是**词表**而不是布尔开关：写错就抛，不回落默认。"""
    if arm not in ARM_PLAN:
        raise ValueError(f"未知臂 {arm!r} ⇒ 词表 = {list(ARMS)}（大小写敏感：A2 不是 a2）")
    if arm == CTL_ARM:
        return None
    plan = ARM_PLAN[arm]
    supply = StSupply.load(path=panel_path,
                           sources=plan["sources"],
                           include_inferred=bool(plan["include_inferred"]))
    supply.arm = arm                       # 供 describe() 回显，报告里能认出是哪一臂
    return supply


class PanelUnavailableError(RuntimeError):
    """面板缺失/为空 ⇒ 拒跑。不许静默退回「不传日期」的旧行为。"""


class TierTally:
    """按调用方真正供给的样本行记账：权威 X / 推断 Y / 无法判定 Z。"""

    def __init__(self) -> None:
        self.counts: Counter[str] = Counter()

    def add(self, tier: str) -> None:
        if tier not in TIERS:
            raise ValueError(f"未知档位 {tier!r}，词表 = {TIERS}")
        self.counts[tier] += 1

    def add_many(self, tiers: Iterable[str]) -> None:
        for t in tiers:
            self.add(t)

    def line(self) -> str:
        """训练脚本必须打印的那一行：三档都在场（哪怕是 0），缺档会被读成「没实现」。"""
        c = self.counts
        return (f"ST 供给来源分布：权威 {c[TIER_AUTHORITY]:,} 行 / "
                f"推断 {c[TIER_INFERRED]:,} 行 / 无法判定 {c[TIER_UNSUPPLIED]:,} 行")

    def detail(self) -> list[str]:
        out = [self.line()]
        if self.counts[TIER_UNSUPPLIED]:
            out.append(f"  注：{_UNREACHABLE_NOTE}")
        if self.counts[TIER_NO_SUPPLY]:
            out.append(f"  注：未供给 {self.counts[TIER_NO_SUPPLY]:,} 行 ⇒ {_NO_SUPPLY_NOTE}")
        if not self.counts[TIER_INFERRED]:
            out.append(f"  注：{_INFERRED_OFF_NOTE}")
        return out


class _CodeIndex:
    """单只代码的可见边界：段起点/段终点两组升序数组 + 最早可见边界日。

    `st.day_states` 给出的段在同一代码内**互不重叠且按起点升序**（它按 impl_date 顺序
    配对实施/撤销），所以「d 落在哪段」用 bisect 取最后一个 `start <= d` 就够，
    不必线性扫。`start=None` 的孤儿撤销段不进 starts，只抬 `earliest`——
    它仍然能证明「撤销日之后不在 ST」。
    """

    __slots__ = ("earliest", "ends", "starts")

    def __init__(self, segments: Sequence[dict[str, Any]]) -> None:
        pairs = sorted(
            ((s["start_date"], s["end_date"]) for s in segments
             if s["start_date"] is not None and not pd.isna(s["start_date"])),
            key=lambda p: p[0])
        self.starts: list = [p[0] for p in pairs]
        self.ends: list = [p[1] for p in pairs]
        # 孤儿撤销段（start=None）不进 starts，但它的 end 仍是**可见边界**：
        # 「撤销那天及以后不在 ST」是能判的，「撤销那天以前」才是看不见的。
        all_ends: list = [s["end_date"] for s in segments
                          if s["end_date"] is not None and not pd.isna(s["end_date"])]
        bounds: list = list(self.starts) + all_ends
        self.earliest: dt.date | None = min(bounds) if bounds else None

    def at(self, d: dt.date) -> str:
        """'yes' / 'no' / 'unknown' 三态；**绝不把 unknown 折成 False**。"""
        if self.earliest is None or d < self.earliest:
            return "unknown"
        i = bisect.bisect_right(self.starts, d) - 1
        if i >= 0:
            end = self.ends[i]
            if end is None or pd.isna(end):
                return "yes"          # 开口段：至今仍 ST
            if d < end:
                return "yes"          # 右开 [实施, 撤销)
        return "no"


def limit_series(code: str, dates: Sequence[Any],
                 supply: StSupply | None = None
                 ) -> tuple[list[float], list[str], list[bool]]:
    """逐交易日的判定阈值 + 档位 + ST 布尔。训练侧与因子侧共用这一条。

    阈值只对 `limit_threshold` 取两个数再按日选，而不是每行调一次：
      · `pre`  = 该票在并轨日**前一天**按 ST 档的判定值（主板 0.045，双创/北交所与其现行档相同）
      · `post` = 该票现行判定值（不传 is_st）
    于是「传日期与真实 ST」只可能改动**并轨日之前、且面板判为 ST** 的那些行——
    这正是行为中性的算术形状（`tests/test_limit_on_date_in_production.py` 钉它）。
    """
    parsed = [_limits.as_date(d) for d in dates]
    bad = [i for i, d in enumerate(parsed) if d is None]
    if bad:
        raise ValueError(
            f"{code} 的第 {bad[:5]} 行日期解析不出（{[dates[i] for i in bad[:5]]}）"
            "⇒ 按日供给必须有交易日，不能回落成「不传日期」")
    if supply is None:
        base = _limits.limit_threshold(code, is_st=False, on_date=parsed[-1])
        n = len(parsed)
        return [base] * n, [TIER_NO_SUPPLY] * n, [False] * n
    pre = _limits.limit_threshold(code, is_st=True, on_date=_RULE_DAY - _ONE_DAY)
    post = _limits.limit_threshold(code, is_st=False, on_date=parsed[-1])
    flags, tiers = supply.st_mask(code, parsed)
    limits = [pre if (f and d < _RULE_DAY) else post
              for f, d in zip(flags, parsed, strict=True)]
    return limits, tiers, flags


class StSupply:
    """按日回答「那天是不是 ST」，并给出档位（权威 / 推断 / 无法判定）。"""

    def __init__(self, events: pd.DataFrame, *, include_inferred: bool = False,
                 sources: Sequence[str] | None = None,
                 source_label: str = "") -> None:
        if events is None or not len(events):
            raise PanelUnavailableError(
                "ST 事件表为空 ⇒ 无法按日供给 ST 状态。这不是「验出来是非 ST」，"
                "是面板不在场/未采集；生产链拒绝静默退回「不传日期」的旧口径。")
        missing = [c for c in st.EVENT_COLUMNS if c not in events.columns]
        if missing:
            raise ValueError(f"事件表缺列 {missing}，契约列 = {list(st.EVENT_COLUMNS)}")
        self.include_inferred = bool(include_inferred)
        self.sources = _check_sources(sources)
        self.source_label = source_label
        self.arm = ""
        self.authority = self._index(st.day_states(events, sources=self.sources))
        self.inferred: dict[str, _CodeIndex] = (
            self._index(st.day_states(events, sources=(st.INFERRED_SOURCE,)))
            if self.include_inferred else {})
        self.panel_rows = len(events)
        self.lookups = 0

    @staticmethod
    def _index(states: pd.DataFrame) -> dict[str, _CodeIndex]:
        out: dict[str, _CodeIndex] = {}
        if not len(states):
            return out
        for code, grp in states.groupby("code", sort=True):
            out[str(code)] = _CodeIndex(grp.to_dict("records"))
        return out

    # ------------------------------------------------------------------ #
    # 装载
    # ------------------------------------------------------------------ #
    @classmethod
    def load(cls, *, include_inferred: bool = False,
             sources: Sequence[str] | None = None,
             path: Path | str | None = None) -> StSupply:
        """读面板并建索引；文件缺失/表为空 ⇒ `PanelUnavailableError`（响亮拒跑）。

        `sources=None` 是 B1 的默认口径（权威三层全读）；B2 的臂 A/A2/I 由
        `build_supply(arm)` 传子集，别在这里各写一份字面量。
        """
        target = Path(path) if path is not None else paths.st_event_path()
        if not target.exists():
            raise PanelUnavailableError(
                f"ST 事件表不在场：{target} 不存在。生成入口 `make data-st-history`"
                "（抓取→重析→板块→深市改名→建表）。生产链拒绝静默退回「不传日期」的旧"
                "口径——那正是 KI-DATA-008 的缺陷本体。")
        events = st.load_panel(target)
        if not len(events):
            raise PanelUnavailableError(
                f"{target} 存在但是 0 行 ⇒ 面板还没采集出内容，"
                "按日供给无从谈起（重建入口 `make data-st-history`）。")
        return cls(events, include_inferred=include_inferred, sources=sources,
                   source_label=str(target))

    # ------------------------------------------------------------------ #
    # 查询（纯函数，不自计次数）
    # ------------------------------------------------------------------ #
    @staticmethod
    def _norm(code: str) -> str:
        return _limits.strip_exchange_prefix(str(code))

    def _decide(self, d: dt.date, code: str) -> tuple[bool, str]:
        idx = self.authority.get(code)
        if idx is not None:
            state = idx.at(d)
            if state != "unknown":
                return (state == "yes"), TIER_AUTHORITY
        if self.include_inferred:
            inf = self.inferred.get(code)
            if inf is not None:
                state = inf.at(d)
                if state != "unknown":
                    return (state == "yes"), TIER_INFERRED
        return False, TIER_UNSUPPLIED

    def is_st(self, code: str, on_date: Any) -> tuple[bool, str]:
        """返回 (那天是否 ST, 档位)；档位 ∈ {权威, 推断, 无法判定}。"""
        d = _limits.as_date(on_date)
        if d is None:
            raise ValueError(f"on_date={on_date!r} 解析不出日期 ⇒ 按日供给必须有交易日")
        self.lookups += 1
        return self._decide(d, self._norm(code))

    def st_mask(self, code: str, dates: Sequence[Any]) -> tuple[list[bool], list[str]]:
        """一列交易日 → (是否 ST, 档位)；与 `is_st` 同一条 `_decide`，不分两套判据。"""
        c = self._norm(code)
        out_state: list[bool] = []
        out_tier: list[str] = []
        for raw in dates:
            d = raw if isinstance(raw, dt.date) else _limits.as_date(raw)
            if d is None:
                raise ValueError(f"{c} 的日期 {raw!r} 解析不出 ⇒ 无法按日供给")
            self.lookups += 1
            state, tier = self._decide(d, c)
            out_state.append(state)
            out_tier.append(tier)
        return out_state, out_tier

    def describe(self) -> str:
        return (f"ST 供给层：面板 {self.panel_rows:,} 行"
                f"（{self.source_label or '内存合成表'}）、"
                f"权威子集 [{'+'.join(self.sources)}] 覆盖 {len(self.authority):,} 只、"
                f"推断层{'开' if self.include_inferred else '关（默认）'}、"
                f"已查询 {self.lookups:,} 次"
                + (f"｜臂={self.arm}" if self.arm else ""))
