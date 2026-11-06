"""日期化 ST 事件面板：唯一的读、写、校验、解析与逐日派生入口。

为什么要有这张表
----------------
`scripts/train_daban_model.py:135` 与 `tdxfeed/features_5d.py:148` 调 `get_limit(code)` 时
不传 `on_date`，主板 ST 在 `MAIN_ST_RULE_CHANGE_DATE` 之前是 5% 档这一事实就从来没进过
训练矩阵（取证与污染量见 `docs/research/2026-11-05-daban-st-tier-label-pollution.md`，
台账 KI-DATA-008）。修它的第一步不是改代码，而是先有一张能回答「**那一天**这只票是不是 ST」的表。

面板是**事件长表**（契约 1，定稿 8 列）：
`code, ann_date, impl_date, action, kind, source, confidence, fetched_at`。
逐日「那天按几成档」由消费侧从事件行派生（`day_states`），**不落第二张区间表**——
两处真相迟早漂移。三个字段为什么必须留着：
- `action`（实施/撤销）：区间是**派生物**，方向必须是行自带的信息。源侧截断时
  「只有撤销没有实施」是常态（台账里**最早**一条就是撤销的有 87 只），没有 `action` 就分不清
  一段的缺失是"没采到"还是"根本没发生"，`day_states` 也就无法在不依赖排序假设的情况下
  把两条事件配成一个右开区间。
- `source` + `confidence`：**层的身份**。权威层（`f10_ledger`/`akshare_sz`/`block_gn`）与
  价格反推层给的「那天是不是 ST」置信度不同，读侧必须能只取一层——所以 `confidence`
  只有推断行有值，权威行留 `None`（混成一色就没法分层了，`day_states` 的混读抛错是它的守卫）。
- `fetched_at`：**这行证据是哪一天采的**。它是这张表的生命线：「台账只能回溯最近几次」这个
  语义完全取决于采的那天，没有它，三个月后没人知道某行是本次抓的还是去年重采的残留；
  面板会被日复一日重建，这一列就是唯一的时间锚。
- **不放原文列**（`raw_other` 这类）：原文已经有可审计底座——`raw/` 里的 F10 台账存着
  `section_lines`（整段原文），深市那层存着源给的整行。把原文再挤进 parquet 一列是
  **第二处真相**，会和 raw 漂移。要读原文就去读 raw，别在面板里加列。

四个源与两条硬上限（2026-11-05 实测）
------------------------------------
`source ∈ {f10_ledger, block_gn, akshare_sz, price_inferred}`：

- `f10_ledger`：通达信 F10【特别处理】段落（字段 `公告日期/实施日期/处理类型`）。
  全市场 5,229 只实测（全部 `status=ok`），条数分布
  `{0:4739, 1:182, 2:233, 3:60, 4:11, 5:4}` ⇒ **不存在「每票最多 3 条」这种硬上限**
  （观测最大 5 条；4 条的 11 只、5 条的 4 只：`600710 / 000697 / 000908 / 300506`）。
  **真正压制覆盖率的是「大部分票根本没有这一段」：4,739/5,229 = 90.63% 的票该栏是「暂无数据」**，
  只有 490 只有事件。深度是**不均**的，不是被卡在某个条数上。
  截断确实存在，但它的证据是分开的三个读数（2026-11-05 定稿面板实测）：
  **票级**孤儿撤销（台账里**最早**一条就是撤销）= **87 只**，F10 单层**段级**起点不可见 = **129 段**，
  三层权威并读后 = **74 段**（并读时 akshare 的早期变更日把一部分段的起点补上了）。
  权威面板的整体口径：**490 只有事件 / 865 票有可判区间 / 301,694 票日 / 2,018 段（起点不可见 74）**。
  最早实施只回到 2014-08-06 ⇒ 它是**准但短、且大面积缺席**的那一层。
- `block_gn`：`block_gn.dat` 的 ST 板块成员。一次拉全 1,379,375 行 / 实测 1.8–2.3s，但**字段里没有日期列**
  ⇒ 它只能给「今天谁是 ST」这一个截面，`kind` 写成 `当日成员快照`。
- `akshare_sz`：深市带日期改名面板（akshare 本就是仓库的声明依赖，见 `requirements.lock`）。
  实测 2,283 行 / 673 只，跨 1998-04-28 → 2026-11-04 ⇒ **长**的那一层；但它是「简称变更日」
  不是「特别处理实施日」。与 F10 互证（`crosscheck`，全市场）：可配对的 558 条里 92.1% 精确
  同一天，1..7 天档 **0 条**，>7 天的 44 条是配到了不同轮次（多为 F10 侧
  `撤销退市风险实施其他`：退市风险警示撤了、其他风险警示还在，简称面板不记这一笔）。
  沪市无对应的带日期接口。
- `price_inferred`：价格上限反推（`scripts/audit_daban_st_tier_pollution.py` 的 `regime5`）。
  **它不是权威**：与台账互证的 2,974 个票日上精确率约 0.8（约两成过计、漏计侧未量化），
  所以每一行都必须带 `confidence`，且消费侧必须**分层读**（`day_states(sources=...)`），
  权威行不得带 confidence——混成一层就没法区分了。
  全市场实测（`infer --full`，主板 3,200 只扫描）：**76,919 行 / 3,120 只**，日期跨
  1991-02-19 → 2026-07-10，但**每票中位 22 条事件** ⇒ 它的「实施/撤销」是**窗口判定的翻转**，
  不是行政动作。别把它和权威层的段数并排读：要么只用权威层，要么把它当带权先验。

区间语义
--------
`实施@D0 → 撤销@D1` 记作区间 `[D0, D1)`：**右开**，即 D1 当天已不再适用 5% 档
（撤销自该日起生效）。最后一条是 `实施` 且没有后续撤销 ⇒ `end_date` 为空 = 至今仍开。
`退市` 与「撤销/撤消」两形都归为右开端点（原文 `kind` 不改写，要区分可按 `kind` 过滤），
因为三者同样终止「这一天有没有 5% 档涨停」的适用。

互推不是双向等式（2026-11-05 定稿面板全市场权威层实测：2,859 个事件键 → 回程 3,690 个）：
正向一个都不丢（缺失 0），**多出来的 831 条全是就地右开产生的撤销**，每条都带
`revoke_kind` 里的 `按下一次实施截断` 标记（回程时写进事件行的 `kind`）。它还与
「相邻两条 `实施`」的对数**逐一相等**——这就是它作为判据的意义：多出来的每一条
都必须能被一次切断解释。
按源对分开数（实测）：`akshare_sz→akshare_sz` 572（深市面板对每次简称变更都记一条，
`ST→*ST` 这种升级没有中间的撤销）、`f10_ledger→block_gn` 172（当日快照把仍开着的段又
assert 一次）、`f10_ledger→f10_ledger` 44（`*ST` 之后 `暂停上市`）、跨源 43
（`f10_ledger→akshare_sz` 27 + `akshare_sz→f10_ledger` 12 + `akshare_sz→block_gn` 4）。
这种切断**不改逐日答案**：前一段右开到 D、后一段从 D 起开，D 当天仍被覆盖，区间是连续的，
所以 B1 读到的「那天按几成档」不受影响；要「只信原始事件」就按上面那个标记过滤掉。

不做什么
--------
本模块不发网络请求（抓取在 `scripts/collect_st_history.py`）、不 import 期改全局 socket 超时
（`AGENTS.md` 里 baostock 那条坑的成因）、不自己拼表路径（一律走
`tdxfeed.paths.st_event_path()` / `st_board_snapshot_path()` / `st_snapshot_days_path()`），
写盘一律委托 `tdxfeed.parquet_io.write_table`
（date→`date32[day]`、tmp→`os.replace`），不在这里再写一份 `to_parquet`。
"""
from __future__ import annotations

import datetime as dt
import json
import re
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path

import pandas as pd

from tdxfeed.atomic import atomic_write_text  # SEM001 原子写（仓库统一用绝对导入这一形）

from . import paths
from .parquet_io import write_table

# 吞错计数（SEM002）：解析里被放弃的行不静默，按原因分桶
_ERROR_COUNT: Counter[str] = Counter()

EVENT_COLUMNS: tuple[str, ...] = (
    "code", "ann_date", "impl_date", "action", "kind",
    "source", "confidence", "fetched_at",
)
INTERVAL_COLUMNS: tuple[str, ...] = (
    "code", "start_date", "end_date", "kind", "revoke_kind", "source", "confidence",
)
#: 当日快照**累积**表（定稿契约 ①）：`block_gn` 只有当日截面，历史段靠一天一天攒
SNAPSHOT_COLUMNS: tuple[str, ...] = ("snapshot_date", "code", "block_name", "source")

# 翻转分档：训练正样本跨全历史，1998–2005 那段过触发会直接进训练，所以单列一档
FLIP_BANDS: tuple[tuple[str, int, int], ...] = (
    ("1998-2005", 1998, 2005),
    ("2006-2015", 2006, 2015),
    ("2016-2026", 2016, 2026),
)

ACTIONS: tuple[str, ...] = ("实施", "撤销")
SOURCES: tuple[str, ...] = ("f10_ledger", "block_gn", "akshare_sz", "price_inferred")
AUTHORITY_SOURCES: tuple[str, ...] = ("f10_ledger", "block_gn", "akshare_sz")
INFERRED_SOURCE = "price_inferred"

#: 这些类型都表示「区间右开端点」（源里撤销有「撤销/撤消」两种拼写，必须都收）
REVOKE_KINDS: tuple[str, ...] = ("撤销", "撤消", "退市")

# 值可能是日期（ASCII）也可能是处理类型（中文，如 `撤销退市风险实施其他`），所以取「第一个非空串」
_DATE_LINE_RE = re.compile(r"^[｜|]\s*(公告日期|实施日期|处理类型)\s*[｜|]\s*(\S+)")
_ISO_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
_SECTION_MARK = "【特别处理】"
CODE_RE = re.compile(r"^\d{6}$")


def _as_date(value) -> dt.date | None:
    """把各种来源的日期值收敛成 `datetime.date`；不可解析返回 None 并计数。"""
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if isinstance(value, pd.Timestamp):
        return value.date()
    s = str(value).strip()
    if not s or s in {"NaT", "None", "nan"}:
        return None
    m = _ISO_RE.search(s)
    if m:
        try:
            return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:      # 形如 2026-13-45：形状对但值非法
            _ERROR_COUNT["_as_date"] += 1
            return None
    _ERROR_COUNT["_as_date"] += 1      # 完全解析不出日期
    return None


def action_of_kind(kind: str) -> str:
    """`处理类型` → 事件方向。撤销族与退市都表示右开端点，其余（`*ST`/`ST`）表示实施。"""
    text = str(kind).strip()
    return "撤销" if text.startswith(REVOKE_KINDS) else "实施"


# --------------------------------------------------------------------------- #
# F10 解析
# --------------------------------------------------------------------------- #
def parse_f10_section(code: str, text: str) -> list[dict]:
    """把一段 F10【特别处理】原文解析成事件行（**逐字段对应，不做推断**）。

    源侧结构是「一条记录 = 公告日期 / 实施日期 / 处理类型 三行」的块，最新在上。
    这里按出现顺序累积，遇到 `处理类型` 就收一条；缺 `实施日期` 或 `处理类型` 的半截记录
    丢弃并计数（不猜日期）。段落不存在返回空列表——**这是合法结果**，多数票从没被处理过。

    「其他」那一格（复牌日、还剩哪一类风险警示等自由文本）**不进面板**：整段原文已经逐字
    存在 `raw/f10_ledger.jsonl` 的 `section_lines` 里，面板再存一份就是第二处真相。
    所以要读原文读 raw，这里只把非结构化行跳过。
    """
    lines = [ln.strip() for ln in str(text).splitlines() if ln.strip()]
    start = next((i for i, ln in enumerate(lines) if _SECTION_MARK in ln), None)
    if start is None:
        return []
    rows: list[dict] = []
    cur: dict[str, str] = {}
    for ln in lines[start + 1:]:
        m = _DATE_LINE_RE.match(ln)
        if not m:
            # 【特别处理】之后可能接下一个段落；段落结束就收手
            if ln.startswith("【"):
                break
            continue                      # 表格线、框线、「其他」正文：都不进结构化行
        field, raw = m.group(1), m.group(2).strip()
        if field == "公告日期":
            if cur.get("处理类型"):
                e = _finish(code, cur)
                if e is not None:
                    rows.append(e)
                cur = {}
            cur["ann"] = raw
        elif field == "实施日期":
            if cur.get("处理类型"):
                e = _finish(code, cur)
                if e is not None:
                    rows.append(e)
                cur = {}
            cur["impl"] = raw
        else:
            cur["处理类型"] = raw
    if cur.get("处理类型"):
        e = _finish(code, cur)
        if e is not None:
            rows.append(e)
    return [r for r in rows if r is not None]


def _finish(code: str, cur: dict) -> dict | None:
    kind = str(cur.get("处理类型") or "").strip()
    impl = _as_date(cur.get("impl"))
    if not kind or impl is None:
        _ERROR_COUNT["_finish"] += 1        # 半截记录：有实施日期却没走到成条
        return None
    return {
        "code": str(code),
        "ann_date": _as_date(cur.get("ann")),
        "impl_date": impl,
        "action": action_of_kind(kind),
        "kind": kind,
        "source": "f10_ledger",
        "confidence": None,
        "fetched_at": None,        # 由 build 从台账记录的抓取时刻补上
    }


# --------------------------------------------------------------------------- #
# 校验
# --------------------------------------------------------------------------- #
def validate(df: pd.DataFrame) -> list[str]:
    """返回问题清单；空列表 = 通过。**写盘前必过**（`write_panel` 会拦）。"""
    problems: list[str] = []
    if list(df.columns) != list(EVENT_COLUMNS):
        missing = [c for c in EVENT_COLUMNS if c not in df.columns]
        extra = [c for c in df.columns if c not in EVENT_COLUMNS]
        order = list(df.columns) != list(EVENT_COLUMNS) and not missing and not extra
        problems.append(
            f"列不对：缺 {missing} / 多 {extra}"
            + (" / 顺序不一致" if order else "")
        )
        return problems
    if not len(df):
        return problems

    bad_action = sorted({str(v) for v in df["action"] if str(v) not in ACTIONS})
    if bad_action:
        problems.append(f"action 词表外：{bad_action}")
    bad_source = sorted({str(v) for v in df["source"] if str(v) not in SOURCES})
    if bad_source:
        problems.append(f"source 词表外：{bad_source}")
    bad_code = sorted({str(v) for v in df["code"] if not CODE_RE.fullmatch(str(v))})
    if bad_code:
        problems.append(f"code 不是 6 位数字：{bad_code[:5]}")
    empty_kind = int(df["kind"].isna().sum() + (df["kind"].astype(str) == "").sum())
    if empty_kind:
        problems.append(f"kind 为空的行 {empty_kind} 条")

    for col in ("ann_date", "impl_date"):
        series = df[col]
        wrong = [v for v in series if v is not None and not pd.isna(v)
                 and not isinstance(v, dt.date)]
        if wrong:
            problems.append(
                f"{col} 含非日期值 {sorted({type(v).__name__ for v in wrong})}"
                "（必须是 datetime.date 或空，字符串会被写成错口径）")
    missing_impl = int(df["impl_date"].isna().sum())
    if missing_impl:
        problems.append(f"impl_date 为空的行 {missing_impl} 条（事件的锚点是实施日，不能缺）")

    # fetched_at：每行都要能回答「这条证据是什么时候抓的」。定稿契约把它钉进 schema——
    # 台账是**当日快照式**取的（F10 只给最近几条），没有时间戳就无法判断某一行有多旧。
    fa = df["fetched_at"]
    bad_fetched = int(fa.isna().sum())
    if bad_fetched:
        problems.append(f"fetched_at 为空的行 {bad_fetched} 条（每条事件必须带抓取时刻）")
    not_ts = [v for v in fa if v is not None and not pd.isna(v)
              and not isinstance(v, (dt.datetime, pd.Timestamp))]
    if not_ts:
        problems.append(
            f"fetched_at 含非时间值 {sorted({type(v).__name__ for v in not_ts})}"
            "（必须是 datetime 或 Timestamp）")

    inferred = df["source"] == INFERRED_SOURCE
    conf = pd.to_numeric(df.loc[inferred, "confidence"], errors="coerce") if inferred.any() \
        else pd.Series(dtype="float64")
    if inferred.any():
        absent = int(conf.isna().sum())
        if absent:
            codes = sorted({str(c) for c, ok in zip(df.loc[inferred, "code"], conf.isna(),
                                                   strict=True) if ok})
            problems.append(
                f"推断行缺 confidence {absent} 条（示例票 {codes[:5]}）："
                "price_inferred 不带置信度就会被读成权威")
        out_of_range = [float(v) for v in conf.dropna() if not 0.0 < float(v) <= 1.0]
        if out_of_range:
            problems.append(f"confidence 不在 (0,1]：{out_of_range[:5]}")
    auth_conf = df.loc[~inferred, "confidence"]
    if auth_conf.notna().any():
        problems.append(
            f"权威行带了 confidence {int(auth_conf.notna().sum())} 条："
            "分层靠『权威无置信度、推断有』这个约定，混了就分不开")

    dup = df[df.duplicated(subset=["code", "impl_date", "action"], keep=False)]
    if len(dup):
        sample = (dup.groupby(["code", "impl_date", "action"], dropna=False)
                  .size().sort_values(ascending=False).head(5))
        problems.append("同一 (code, impl_date, action) 重复："
                        + "; ".join(f"{c}/{d}/{a}×{n}" for (c, d, a), n in sample.items()))
    return problems


# --------------------------------------------------------------------------- #
# 分层与逐日派生
# --------------------------------------------------------------------------- #
def sub_layer(df: pd.DataFrame, sources: Iterable[str]) -> pd.DataFrame:
    allow = set(sources)
    return df[df["source"].isin(allow)].copy()


def day_states(df: pd.DataFrame, *, sources: Sequence[str] | None = None) -> pd.DataFrame:
    """事件行 → 每票的 ST 区间 `[start_date, end_date)`。

    `sources=None` 时只允许**单层**：同时出现权威源与 `price_inferred` 会抛错（契约 5：
    推断行不得混读成权威）。要两层就分别调用两次，自己决定优先级。
    """
    data = df.copy() if sources is None else sub_layer(df, sources)
    if not len(data):
        return pd.DataFrame(columns=list(INTERVAL_COLUMNS))
    got = set(data["source"])
    if INFERRED_SOURCE in got and got & set(AUTHORITY_SOURCES):
        raise ValueError(
            "不得混读：面板里同时有权威源与 price_inferred 行，"
            "请分层调用 day_states(sources=st.AUTHORITY_SOURCES) 或 (sources=(st.INFERRED_SOURCE,))")
    out: list[dict] = []
    for code, grp in data.sort_values(["code", "impl_date", "action"]).groupby(
            "code", sort=True):
        open_row: dict | None = None
        for row in grp.to_dict("records"):
            if row["action"] == "实施":
                if open_row is not None:
                    # 上游台账截断时会看到连续两次实施：把上一段就地右开，不丢事件
                    open_row["end_date"] = row["impl_date"]
                    open_row["revoke_kind"] = "实施(未见撤销，按下一次实施截断)"
                    out.append(open_row)
                open_row = {"code": code, "start_date": row["impl_date"], "end_date": None,
                            "kind": row["kind"], "revoke_kind": None,
                            "source": row["source"], "confidence": row["confidence"]}
            else:
                if open_row is None:
                    # 条目被源侧截断时首条常是撤销：它只能说明「更早的实施看不见」
                    out.append({"code": code, "start_date": None, "end_date": row["impl_date"],
                                "kind": None, "revoke_kind": row["kind"],
                                "source": row["source"], "confidence": row["confidence"]})
                    _ERROR_COUNT["day_states"] += 1   # 撤销先于可见实施（源侧截断的形状）
                    continue
                open_row["end_date"] = row["impl_date"]
                open_row["revoke_kind"] = row["kind"]
                out.append(open_row)
                open_row = None
        if open_row is not None:
            out.append(open_row)
    return pd.DataFrame(out, columns=list(INTERVAL_COLUMNS))


def states_to_events(states: pd.DataFrame) -> pd.DataFrame:
    """区间 → 事件行（与 `day_states` 互推；`test_events_and_day_states_round_trip` 钉这条）。"""
    rows: list[dict] = []
    for rec in states.sort_values(["code", "start_date"]).to_dict("records"):
        if rec["start_date"] is None:
            # 只见撤销的截断段：还原成撤销事件本身，不编造实施行
            rows.append({"code": rec["code"], "ann_date": None, "impl_date": rec["end_date"],
                         "action": "撤销", "kind": rec["revoke_kind"], "source": rec["source"],
                         "confidence": rec["confidence"]})
            continue
        rows.append({"code": rec["code"], "ann_date": None, "impl_date": rec["start_date"],
                     "action": "实施", "kind": rec["kind"], "source": rec["source"],
                     "confidence": rec["confidence"]})
        if rec["end_date"] is not None and not pd.isna(rec["end_date"]):
            rows.append({"code": rec["code"], "ann_date": None, "impl_date": rec["end_date"],
                         "action": "撤销", "kind": rec["revoke_kind"], "source": rec["source"],
                         "confidence": rec["confidence"]})
    return pd.DataFrame(rows, columns=list(EVENT_COLUMNS))


def covered_ticket_days(states: pd.DataFrame, *,
                        dates_of: Callable[[str], Sequence[object]]) -> dict:
    """区间 → 覆盖了多少个 (代码, 交易日)。日历由调用方注入。

    为什么单独有这一个函数：面板的行数是「事件条数」，它回答不了 B1/B2 真正要问的
    ——**有多少个历史行会因此换档**。那一层是票日，不是事件，所以要从区间过一遍
    该代码自己的交易日历。

    三条口径（都不静默）：
      - 区间右开 `[start, end)`：撤销实施当天已不再适用 5% 档，所以 `d == end` 不计。
      - 开口段（`end` 为空 = 至今仍 ST）算到该代码日历的最后一天，不假设未来。
      - 起点不可见的段（条目被截断时首条是「撤销」）**不去查日历**，只计数。
        把它当 0 天会低估覆盖，把它当「从日历第一天起」会高估 —— 所以既不参与
        `票日数`，也不进 `无日历的代码`，单独报 `起点不可见段数`。
    """
    rep: dict = {"段数": 0, "可计段数": 0, "起点不可见段数": 0, "票日数": 0,
                 "无日历的代码数": 0, "无日历的代码": [], "分源": {}}
    if not len(states):
        return rep
    rep["段数"] = len(states)
    missing: set[str] = set()
    for rec in states.to_dict("records"):
        src = str(rec["source"])
        bucket = rep["分源"].setdefault(
            src, {"段数": 0, "可计段数": 0, "起点不可见段数": 0, "票日数": 0})
        bucket["段数"] += 1
        start = rec["start_date"]
        if start is None or pd.isna(start):
            bucket["起点不可见段数"] += 1
            rep["起点不可见段数"] += 1
            continue
        cal = [d for d in (dates_of(str(rec["code"])) or [])]
        if not cal:
            missing.add(str(rec["code"]))
            continue
        end = rec["end_date"]
        closed = end is not None and not pd.isna(end)
        n = sum(1 for d in cal if d >= start and (not closed or d < end))
        bucket["票日数"] += n
        bucket["可计段数"] += 1
        rep["票日数"] += n
        rep["可计段数"] += 1
    rep["无日历的代码数"] = len(missing)
    rep["无日历的代码"] = sorted(missing)[:20]
    return rep


# --------------------------------------------------------------------------- #
# 读写
# --------------------------------------------------------------------------- #
def write_panel(events: pd.DataFrame, path: Path | None = None) -> Path:
    """校验后写盘；不过校验就拒绝（契约 3：写侧只有 `parquet_io.write_table` 一条路）。"""
    problems = validate(events)
    if problems:
        raise ValueError("面板未通过校验：" + " | ".join(problems))
    target = Path(path) if path is not None else paths.st_event_path()
    df = events.loc[:, list(EVENT_COLUMNS)].copy()
    df["confidence"] = pd.to_numeric(df["confidence"], errors="coerce")
    # fetched_at 是时间戳（不是日期）：写成 datetime64，date32 归一只针对 ann/impl 两列
    df["fetched_at"] = pd.to_datetime(df["fetched_at"], errors="coerce")
    return write_table(target, df, date_col=("ann_date", "impl_date"))


def load_panel(path: Path | None = None, *, strict: bool = True) -> pd.DataFrame:
    """读面板并断言两个日期列是 `date32[day]`；缺文件返回空表（不编造）。"""
    import pyarrow.parquet as pq

    target = Path(path) if path is not None else paths.st_event_path()
    if not target.exists():
        return pd.DataFrame(columns=list(EVENT_COLUMNS))
    schema = pq.read_schema(target)
    if strict:
        for col in ("ann_date", "impl_date"):
            if col in schema.names and str(schema.field(col).type) != "date32[day]":
                raise ValueError(
                    f"{target.name} 的 {col} 是 {schema.field(col).type}，"
                    "不是 date32[day]：写侧没走 parquet_io 契约")
    df = pd.read_parquet(target)
    for col in ("ann_date", "impl_date"):
        if col not in df.columns:
            continue
        if pd.api.types.is_datetime64_any_dtype(df[col]):
            df[col] = df[col].dt.date
        else:
            # date32[day] 被 pandas 读回来就是 object-of-date，不是 datetime64；
            # 统一成 datetime.date（个别源可能带 datetime）以便 validate 与消费侧按同一类型比较
            # 反例：这里直接写 .dt.date 会在 object 列上抛
            # AttributeError: Can only use .dt accessor with datetimelike values
            # （由 tests/test_st_history_panel.py::test_load_panel_round_trips_the_events_it_wrote 钉住）
            df[col] = df[col].map(lambda v: v.date() if isinstance(v, dt.datetime) else v)
    return df


def read_f10_ledger(path: Path | None = None) -> list[dict]:
    """读原始逐票台账 JSONL（抓取与解析分离：重析不必再打网络）。"""
    target = Path(path) if path is not None else paths.st_f10_ledger_path()
    if not target.exists():
        return []
    out: list[dict] = []
    with target.open(encoding="utf-8", errors="ignore") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                _ERROR_COUNT["read_f10_ledger"] += 1   # JSONL 行解析失败
    return out


# --------------------------------------------------------------------------- #
# 当日快照的**累积**表（定稿契约 ①）
# --------------------------------------------------------------------------- #
def write_snapshot(snap: pd.DataFrame, path: Path | None = None) -> Path:
    """`block_gn` 当日成员按天累积。列 = `SNAPSHOT_COLUMNS`，日期列走 `date32[day]`。"""
    if list(snap.columns) != list(SNAPSHOT_COLUMNS):
        raise ValueError(f"快照表列必须是 {list(SNAPSHOT_COLUMNS)}，收到 {list(snap.columns)}")
    target = Path(path) if path is not None else paths.st_board_snapshot_path()
    df = snap.copy()
    df["snapshot_date"] = pd.to_datetime(df["snapshot_date"])
    df = (df.sort_values(["snapshot_date", "code"])
            .drop_duplicates(subset=["snapshot_date", "code", "block_name"], keep="first")
            .reset_index(drop=True))
    return write_table(target, df, date_col="snapshot_date")


def read_snapshot(path: Path | None = None) -> pd.DataFrame:
    target = Path(path) if path is not None else paths.st_board_snapshot_path()
    if not target.exists():
        return pd.DataFrame(columns=list(SNAPSHOT_COLUMNS))
    df = pd.read_parquet(target)
    df["snapshot_date"] = df["snapshot_date"].map(
        lambda v: v.date() if isinstance(v, (dt.datetime, pd.Timestamp)) else v)
    return df


def _days_path_for(table_path: Path) -> Path:
    """「采到过的日子」登记跟着表走：生产路径用 paths 的入口，测试里的 tmp 表用同目录副本。

    这样 `append_snapshot(..., path=tmp)` 不会把测试日期写进真实登记文件——两张文件
    一旦分家，测试就会污染生产状态，这类串味在仓库里已经踩过。
    """
    table_path = Path(table_path)
    if table_path == paths.st_board_snapshot_path():
        return paths.st_snapshot_days_path()
    return table_path.with_name(f"{table_path.stem}_captured_days.json")


def read_captured_days(path: Path | None = None) -> list[dt.date]:
    """**采到过的日子**清单：空成员那天也要登记，否则「那天没人 ST」与「那天没采」分不开。"""
    target = Path(path) if path is not None else paths.st_snapshot_days_path()
    if not target.exists():
        return []
    return sorted(dt.date.fromisoformat(s) for s in json.loads(target.read_text(encoding="utf-8")))


def _write_captured_days(days: list[dt.date], path: Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # SEM001：这张登记是「哪些天可以判撤销」的唯一凭据，写一半就会让读侧把断档当成采集失败
    return atomic_write_text(path, json.dumps(
        sorted(d.isoformat() for d in days), ensure_ascii=False))


def captured_days_for(path: Path | None = None) -> list[dt.date]:
    """读某张快照表配套的「采到日」登记（测试传 tmp 路径，生产不传）。"""
    target = Path(path) if path is not None else paths.st_board_snapshot_path()
    return read_captured_days(_days_path_for(target))


def append_snapshot(snap: pd.DataFrame, *, captured_on: dt.date | None = None,
                   path: Path | None = None) -> pd.DataFrame:
    """把这一天的成员并进累积表（同日重跑不产生重复），并登记「这天采到了」。"""
    target = Path(path) if path is not None else paths.st_board_snapshot_path()
    day = captured_on
    if day is None:
        if not len(snap):
            raise ValueError("空成员必须显式传 captured_on，否则这一天会被当成没采")
        day = max(v.date() if isinstance(v, (dt.datetime, pd.Timestamp)) else v
                  for v in snap["snapshot_date"])
    existing = read_snapshot(target)
    combined = pd.concat([existing, snap], ignore_index=True) if len(existing) else snap.copy()
    combined = (combined.drop_duplicates(subset=["snapshot_date", "code", "block_name"],
                                         keep="first").reset_index(drop=True))
    write_snapshot(combined, target)
    days_path = _days_path_for(target)
    days = set(read_captured_days(days_path)) | {day}
    _write_captured_days(sorted(days), days_path)
    return combined


def snapshot_gap_days(snap: pd.DataFrame, *, days: list[dt.date] | None = None) -> int:
    """采到的日子之间缺了多少天。这是「累积表历史段盖不全」的直接读数。"""
    got = sorted(days if days is not None else set(snap["snapshot_date"]))
    if len(got) < 2:
        return 0
    span = (got[-1] - got[0]).days + 1
    return span - len(got)


def board_events_from_snapshots(snap: pd.DataFrame, *,
                                days: list[dt.date] | None = None,
                                block_name: str = "ST板块") -> pd.DataFrame:
    """累积快照 → 事件行。**只在真有相邻两次观测的下发那天判撤销**。

    这张表存在的理由就是逃出「台账只回溯几条」：每天多一个观测点，就多一个可裁定的端点。
    但断档那天不能当成出板块（采集失败与真的撤销在数据上同形），所以：
      出现的那天 → `实施`；在**下一个可用的采到日**不在了 → `撤销`；
      最后一个可用采到日仍在 → 保持开口（`end_date` 为空），不编撤销。

    **成员为空的采到日一律不用**（计数并跳过）：采集侧对 block_gn 返回空本来就是按失败
    处理（`cmd_block` 里 `block_empty` → 退码 3）。让它进这里，等于把一次坏采集读成
    「198 只集体出板块」，一夜造出 198 个假撤销端点。
    """
    got = sorted(days if days is not None else set(snap["snapshot_date"]))
    if not got or not len(snap):
        return pd.DataFrame(columns=list(EVENT_COLUMNS))
    present: dict = {d: set() for d in got}
    for r in snap.to_dict("records"):
        if str(r["block_name"]) == block_name:
            present.setdefault(r["snapshot_date"], set()).add(str(r["code"]))
    usable = [d for d in got if present.get(d)]
    for _ in [d for d in got if not present.get(d)]:
        _ERROR_COUNT["board_events_from_snapshots"] += 1   # 空采到日：不当「出板块」证据
    rows: list[dict] = []
    for code in sorted({c for d in usable for c in present[d]}):
        in_state = False
        for d in usable:
            here = code in present.get(d, set())
            if here and not in_state:
                rows.append({"code": code, "ann_date": None, "impl_date": d, "action": "实施",
                             "kind": "当日成员快照", "source": "block_gn",
                             "confidence": None, "fetched_at": None})
                in_state = True
            elif not here and in_state:
                rows.append({"code": code, "ann_date": None, "impl_date": d, "action": "撤销",
                             "kind": "离开ST板块", "source": "block_gn",
                             "confidence": None, "fetched_at": None})
                in_state = False
    return pd.DataFrame(rows, columns=list(EVENT_COLUMNS))


# --------------------------------------------------------------------------- #
# 翻转计数（验收 ⑩-a：两个分母 + 1998–2005 单列一档）
# --------------------------------------------------------------------------- #
def orphan_revoke_codes(ledger_records: Sequence[dict]) -> set[str]:
    """台账里**最早那条**就是「撤销」的票数：这类票的实施日在窗口之外，是截断的直接证据。

    两个坑都在本轮踩过，所以写在这里：
    1. 源给的顺序是**新在前**（000609 的三条按 2026→2025→2024 排列），所以"首条"必须按
       日期取最早的，不能取 `events[0]`——取 `events[0]` 数出来的是「当前已不在 ST」的票数，
       与截断没有关系（早先那一版就把这两个数搞混过，报成了 318）。
    2. 续跑会让同一只票留下多条记录，得先把同码事件并到一起再判最早。
    没有任何日期可比的记录（测试里的极简形态）退到源顺序的第一条。
    """
    by_code: dict[str, list[dict]] = {}
    for rec in ledger_records:
        if rec.get("status") != "ok":
            continue
        by_code.setdefault(str(rec["code"]), []).extend(rec.get("events") or [])

    def _key(e: dict) -> str:
        return str(e.get("impl_date") or e.get("ann_date") or "")

    out: set[str] = set()
    for code, events in by_code.items():
        dated = [e for e in events if _key(e)]
        earliest = min(dated, key=_key) if dated else (events[0] if events else None)
        if earliest is not None and str(earliest.get("action")) == "撤销":
            out.add(code)
    return out


def flip_counts(states: pd.DataFrame, *, rows_of: Callable[[str], Sequence[tuple]],
                bands: Sequence[tuple[str, int, int]] = FLIP_BANDS) -> dict:
    """权威段内「按 5% 档才成立、按现役 10% 档会被判 y=0」的行数。

    两个判定值都从权威层 `limit_threshold` 取（`is_st=True` 与 `is_st=False` 各一次），
    本函数**不写死** 0.044 / 0.094——写死就是第二处口径，正是上一轮 +19.2% 翻车的那类错。
    并轨日之后主板 ST 与普通主板同为 10% 档，段内也不会翻转，所以只数并轨日之前的行。

    为什么不给「翻转 ÷ 段内现役正样本」这个比值：段内本来就几乎不存在现役正样本——
    主板 ST 在并轨前每日上限 5%，一条 10% 档都够不着的行占段内行 0.08%（实测 205/261,687），
    拿它当分母会得到 13000% 这种数。可比的分母是 **quantify 在全市场样本行上的现役正样本**
    （216,739），那个数由调用方从 `quantify --full` 的日志搬进来，本函数不猜、也不重算。
    「段内已达现役档行数」保留下来当**冲突读数**：它非零就意味着「区间说这是 ST」与
    「价格说这不是 5% 档」至少有一边错了，是要查的，不是噪声。
    """
    from .execution.limits import MAIN_ST_RULE_CHANGE_DATE, RET_TOLERANCE, limit_threshold

    def _blank() -> dict:
        return {"段内行数": 0, "翻转行数": 0, "段内已达现役档行数": 0, "票数": 0}

    rep: dict = {"全历史": _blank(), "分档": {name: _blank() for name, _a, _b in bands},
                 "判据": {"ST判定值来源": "limit_threshold(is_st=True, on_date)",
                          "普通判定值来源": "limit_threshold(is_st=False, on_date)",
                          "容差": RET_TOLERANCE,
                          "并轨日": MAIN_ST_RULE_CHANGE_DATE.isoformat()}}
    if not len(states):
        return rep
    codes: dict[str, set[str]] = {"全历史": set()}
    for name, _a, _b in bands:
        codes[name] = set()

    def _bucket(d: dt.date) -> str | None:
        for name, lo, hi in bands:
            if lo <= d.year <= hi:
                return name
        return None

    for rec in states.to_dict("records"):
        start, end = rec["start_date"], rec["end_date"]
        if start is None or pd.isna(start):
            continue                       # 起点不可见：不能声称这段里有翻转
        code = str(rec["code"])
        closed = end is not None and not pd.isna(end)
        for day, pct in rows_of(code) or []:
            d = day.date() if isinstance(day, (dt.datetime, pd.Timestamp)) else day
            if not (start <= d and (not closed or d < end)):
                continue
            if d >= MAIN_ST_RULE_CHANGE_DATE:
                continue                   # 并轨后两档同值，不存在翻转
            thr_st = limit_threshold(code, is_st=True, on_date=d)
            thr_plain = limit_threshold(code, is_st=False, on_date=d)
            if thr_st >= thr_plain:
                _ERROR_COUNT["flip_counts"] += 1
                continue                   # 两档没拉开：口径异常，不静默算成翻转
            in_seg = pct >= thr_st - RET_TOLERANCE
            already_pos = pct >= thr_plain - RET_TOLERANCE
            band = _bucket(d)
            targets = [rep["全历史"]] + ([rep["分档"][band]] if band else [])
            code_targets = [codes["全历史"]] + ([codes[band]] if band else [])
            for t in targets:
                t["段内行数"] += 1
                if already_pos:
                    t["段内已达现役档行数"] += 1
                elif in_seg:
                    t["翻转行数"] += 1
            for s in code_targets:
                s.add(code)
    for key, s in codes.items():
        bucket = rep["全历史"] if key == "全历史" else rep["分档"][key]
        bucket["票数"] = len(s)
    for key in ("全历史", *(b[0] for b in bands)):
        bucket = rep["全历史"] if key == "全历史" else rep["分档"][key]
        rows = bucket["段内行数"]
        bucket["翻转占段内行"] = (bucket["翻转行数"] / rows) if rows else None
        bucket["段内冲突占比"] = (bucket["段内已达现役档行数"] / rows) if rows else None
    return rep


# --------------------------------------------------------------------------- #
# 覆盖度
# --------------------------------------------------------------------------- #
def coverage_report(events: pd.DataFrame, *, name_gap: dict | int | None = None,
                    ledger_records: Sequence[dict] | None = None,
                    dates_of: Callable[[str], Sequence[object]] | None = None,
                    flips: dict | None = None) -> dict:
    """逐源分开报：**推断行绝不与权威行合并计数**。

    `dates_of` 不传就只报事件级计数；传了才加票日级计数（`covered_ticket_days`）——
    因为「覆盖了历史里多少行」只有拿真实交易日历才答得出，而本模块不去猜日线在哪。
    """
    rep: dict = {"总行数": len(events), "by_source": {}, "上限": {}}
    for src in SOURCES:
        sub = events[events["source"] == src] if len(events) else events
        impl = sub["impl_date"] if len(sub) else pd.Series(dtype="object")
        entry = {
            "行数": len(sub),
            "票数": int(sub["code"].nunique()) if len(sub) else 0,
            "实施事件": int((sub["action"] == "实施").sum()) if len(sub) else 0,
            "撤销事件": int((sub["action"] == "撤销").sum()) if len(sub) else 0,
            "最早实施": (str(min(v for v in impl if v is not None)) if len(sub) else None),
            "最晚实施": (str(max(v for v in impl if v is not None)) if len(sub) else None),
        }
        if dates_of is not None:
            days = covered_ticket_days(day_states(sub, sources=(src,)),
                                      dates_of=dates_of) if len(sub) else {}
            entry["票日数"] = days.get("票日数", 0)
            entry["段数"] = days.get("段数", 0)
            entry["起点不可见段数"] = days.get("起点不可见段数", 0)
            entry["无日历的代码数"] = days.get("无日历的代码数", 0)
        rep["by_source"][src] = entry
    if len(events):
        auth = events[events["source"] != INFERRED_SOURCE]
        rep["权威层票日可判区间"] = {
            "票数": int(auth["code"].nunique()),
            "最早": str(min(v for v in auth["impl_date"] if v is not None)),
            "最晚": str(max(v for v in auth["impl_date"] if v is not None)),
        }
        if dates_of is not None and len(auth):
            rep["权威层合计"] = covered_ticket_days(
                day_states(auth, sources=AUTHORITY_SOURCES), dates_of=dates_of)
    if ledger_records is not None:
        counts = Counter(len(r.get("events") or []) for r in ledger_records)
        n_rec = sum(counts.values())
        # 键名不叫「截断到 3 条」：3 不是源侧上限（实测最大 5），它是分布上的一个档；
        # 上一批把它写成上限，读的人就会以为深挖几条换个参数能拿回来——拿不回来是「没有这一段」。
        rep["上限"]["条目数恰为 3 的票数"] = counts[3]
        rep["上限"]["条目数不超过 3 的票数（含有事件的票）"] = sum(
            v for k, v in counts.items() if 0 < k <= 3)
        rep["上限"]["台账条目数分布"] = {str(k): counts[k] for k in sorted(counts)}
        rep["上限"]["台账条目数最大值"] = max(counts) if counts else 0
        rep["上限"]["条目数大于 3 的票数"] = sum(v for k, v in counts.items() if k > 3)
        rep["上限"]["孤儿撤销（最早一条即撤销）票数"] = len(orphan_revoke_codes(ledger_records))
        no_section = sum(1 for r in ledger_records if not (r.get("events") or []))
        rep["上限"]["F10 无特别处理段落的票数"] = no_section
        rep["上限"]["F10 无特别处理段落的占比"] = (no_section / n_rec) if n_rec else None
        rep["上限"]["有事件的票数"] = n_rec - no_section
        rep["上限"]["上限说明"] = (
            "**不存在「每票最多 3 条」这个硬上限**（全市场 5,229 只实测最大 5 条，"
            "4 条 11 只、5 条 4 只）；压制覆盖率的真实原因是 "
            f"{no_section:,}/{n_rec:,} = {(no_section / n_rec * 100) if n_rec else 0:.2f}% 的票"
            "【特别处理】栏是「暂无数据」⇒ 深度是**不均**的，不是被卡在某个条数上。"
            "截断本身仍然存在，但它的证据是三个**不同分母**的读数：条数分布的右端、"
            "票级孤儿撤销（本项）、段级起点不可见（by_source 那列与权威层合计里的"
            "「起点不可见段数」）。解析计数里的 day_states 那一项是跨多次分层调用"
            "**累加**的次数，不要当票数或段数读。")
        rep["上限"]["请求失败的票数"] = sum(1 for r in ledger_records if r.get("status") == "fail")
    if name_gap is not None:
        rep["上限"]["无名称源的代码数"] = name_gap
    if flips is not None:
        rep["翻转（权威段）"] = flips
    rep["上限"]["累积快照表的结构性上限"] = (
        "block_gn 只有当日截面，历史段靠逐日累积：采到第 N 天才可能有第 N-1 天的端点，"
        "从第一次采集往前一律盖不到。看 解析计数/快照登记 里的采到日数与断档天数。")
    rep["解析计数（未静默）"] = dict(_ERROR_COUNT)
    return rep
