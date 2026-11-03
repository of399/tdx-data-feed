"""股本反推层（只读）：finance 长表里没有股本，用「总量 ÷ 对应每股量」把它反推出来。

两条独立通道（option, metric 配对是 2026-11-02 实测选定的，不是猜的）：
  revenue_period —— 常用指标/营业总收入 ÷ 每股指标/每股营业总收入
      分母按**报告期期末股本**摊薄，所以这条通道随报告期走，是能当 as-of 序列用的那一条。
  att_np_latest —— 常用指标/归母净利润 ÷ 每股指标/摊薄每股收益_最新股数
      分母名字里就写着「最新股数」，同一标的跨报告期几乎恒定，只给**现值**，不要拿去回填历史。

配对为什么不是任务书里那两组：
  「每股营业收入」的分子是「营业收入」，本表没有这一行；而分子「营业总收入」与它在券商、
  保险等科目结构不同的公司并不相等（实测东方财富 2026H1 差 4.6 倍），配错会把股本放大成
  假的。同组的「每股营业总收入」才是「营业总收入」的对应每股量（30 只抽样、同一最近报告期
  上两条通道相对差中位 1e-6）。
  试过并弃用的第三条：常用指标/股东权益合计(净资产) ÷ 常用指标/每股净资产 —— 分子含少数
  股东权益、分母按归母股本，对有少数股东的公司系统性偏大（与 revenue_period 中位差 7.8%）。

覆盖范围只有一个来源能说明：本表 300 只（core_stocks.json 那批），不是全市场 5221 只。
本模块不写盘、不改表，读 finance.parquet 时做列裁剪 + metric 谓词下推。
"""
from __future__ import annotations

import calendar
from collections import Counter
from pathlib import Path

import pandas as pd
from pyarrow import parquet as pq

from .paths import exchange_prefix, finance_parquet, read_path

# 吞错计数（SEM002）：每处被吞掉的错误都计入统计，按函数名分桶，便于事后追溯失败率
_ERROR_COUNT: Counter[str] = Counter()
# 清洗计数按原因分桶：被丢弃的行数必须看得见，不能静默少一批
_CLEAN_COUNT: Counter[str] = Counter()

BASIS_REVENUE = "revenue_period"
BASIS_ATT_NP = "att_np_latest"

# basis -> (分子 (option, metric), 分母 (option, metric))
CHANNELS: dict[str, tuple[tuple[str, str], tuple[str, str]]] = {
    BASIS_REVENUE: (("常用指标", "营业总收入"), ("每股指标", "每股营业总收入")),
    BASIS_ATT_NP: (("常用指标", "归母净利润"), ("每股指标", "摊薄每股收益_最新股数")),
}

COLUMNS = ("symbol", "report_date", "option", "metric", "value")
# A 股总股本的量级带：带外只能是脏数据（分母被四舍五入到 1e-6 会造出 1e15 级假股本）
SHARES_BAND = (1e6, 1e12)
# 披露滞后的保守近似：季/三季 +1、半年 +2、年度 +4，落到目标月月末
DISCLOSURE_LAG_MONTHS = {3: 1, 6: 2, 9: 1, 12: 4}
OUTPUT_COLUMNS = ["symbol", "report_date", "shares", "basis", "available_after"]


def _prefixed(code: str) -> str:
    """finance 表的 symbol 带市场前缀；入参允许裸 6 位。"""
    c = str(code).lower().strip()
    digits = c[2:] if c[:2] in ("sh", "sz", "bj") else c
    return f"{exchange_prefix(c)}{digits.zfill(6)}"


def _col(wide: pd.DataFrame, pair: tuple[str, str]) -> pd.Series | None:
    col = f"{pair[0]}|{pair[1]}"
    return wide[col] if col in wide.columns else None


def available_after(report_date: int | str) -> int:
    """报告期 → 保守生效近似日（YYYYMMDD int）：按月份映射披露滞后并落到月末。

    真实披露日未在本仓库取证（本表只有报告期，没有公告日），故这是**近似**：
    直接拿 report_date 当生效日会让回测提前看到还没发布的数，即引入前视。
    非标准月份按季/半年/年度的最近档处理，并计入清洗统计。
    """
    s = str(int(report_date))
    year, month, day = int(s[:4]), int(s[4:6]), int(s[6:8])
    lag = DISCLOSURE_LAG_MONTHS.get(month)
    if lag is None:
        _CLEAN_COUNT["unexpected_report_month"] += 1
        lag = 1 if month in (1, 2) else 2 if month in (4, 5, 7, 8) else 4
    ty, tm = year + (month + lag - 1) // 12, (month + lag - 1) % 12 + 1
    return int(f"{ty}{tm:02d}{calendar.monthrange(ty, tm)[1]:02d}")


def _read_long(path: Path, codes: list[str] | None) -> pd.DataFrame:
    metrics = sorted({m for num, den in CHANNELS.values() for m in (num[1], den[1])})
    filters: list[tuple[str, str, object]] = [("metric", "in", metrics)]
    if codes is not None:
        filters.append(("symbol", "in", codes))
    df = pq.read_table(path, columns=list(COLUMNS), filters=filters).to_pandas()

    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    bad = int(df["value"].isna().sum())
    if bad:
        _CLEAN_COUNT["non_finite_value"] += bad
    wanted = {(o, m) for num, den in CHANNELS.values() for o, m in (num, den)}
    keep = [(o, m) in wanted for o, m in zip(df["option"], df["metric"])]
    return df[keep & df["value"].notna().to_numpy()].copy()


def _derive(wide: pd.DataFrame, basis: str) -> pd.DataFrame | None:
    """单一通道：总量 ÷ 每股量，带清洗与量级带。"""
    num_pair, den_pair = CHANNELS[basis]
    num, den = _col(wide, num_pair), _col(wide, den_pair)
    if num is None or den is None:
        _ERROR_COUNT["shares_frame"] += 1
        return None

    _CLEAN_COUNT["zero_numerator"] += int((num == 0).sum()) + int(num.isna().sum())
    _CLEAN_COUNT["non_positive_denominator"] += int((den <= 0).sum()) + int(den.isna().sum())
    shares = num / den.where(den > 0)
    shares = pd.to_numeric(shares, errors="coerce").replace([float("inf"), float("-inf")], None)

    lo, hi = SHARES_BAND
    in_band = (shares > lo) & (shares < hi)
    _CLEAN_COUNT["out_of_band"] += int((shares.notna() & ~in_band.fillna(False)).sum())
    out = shares[in_band.fillna(False)].rename("shares")
    if out.empty:
        return None
    return out.reset_index()[["symbol", "report_date", "shares"]].assign(basis=basis)


def shares_frame(symbols=None, *, path: str | Path | None = None) -> pd.DataFrame:
    """反推股本长表：[symbol, report_date, shares, basis, available_after]。

    report_date / available_after 都是 8 位 YYYYMMDD（int64），与表内既有形态一致。
    每只标的每个报告期最多两行，用 basis 区分口径（见模块 docstring）：
    revenue_period 随报告期变化，att_np_latest 只当现值。
    available_after 是披露滞后的**保守近似**（季/三季 +1、半年 +2、年度 +4，落月末）：
    真实披露日未在本仓库取证，直接把 report_date 当生效日会引入前视。
    清洗丢弃的行数（分母 ≤0、缺值、量级带外）走 clean_counts()，不静默吞掉。
    """
    file = read_path(Path(path) if path is not None else finance_parquet())
    codes = None if symbols is None else sorted({_prefixed(s) for s in symbols})
    df = _read_long(file, codes)
    if df.empty:
        return pd.DataFrame(columns=OUTPUT_COLUMNS)

    df["report_date"] = pd.to_numeric(df["report_date"], errors="coerce")
    dropped = int(df["report_date"].isna().sum())
    if dropped:
        _CLEAN_COUNT["unparsable_report_date"] += dropped
    wide = df.pivot_table(index=["symbol", "report_date"],
                          columns=df["option"] + "|" + df["metric"],
                          values="value", aggfunc="first")
    parts = [p for p in (_derive(wide, b) for b in CHANNELS) if p is not None]
    if not parts:
        return pd.DataFrame(columns=OUTPUT_COLUMNS)

    out = pd.concat(parts, ignore_index=True)
    out["report_date"] = out["report_date"].astype("int64")
    out["available_after"] = out["report_date"].map(available_after).astype("int64")
    out = out.sort_values(["symbol", "report_date", "basis"], kind="stable").reset_index(drop=True)
    return out[OUTPUT_COLUMNS]


def channel_agreement(frame: pd.DataFrame | None = None, *, symbols=None,
                      path: str | Path | None = None) -> pd.DataFrame:
    """两条通道在同一 (symbol, report_date) 上的并排对照 + 相对差 |A/B-1|。

    只保留两通道同时成立的行——单通道能反推不代表能交叉验证。
    """
    if frame is None:
        frame = shares_frame(symbols=symbols, path=path)
    if symbols is not None:
        codes = {_prefixed(s) for s in symbols}
        frame = frame[frame["symbol"].isin(codes)]
    both = [BASIS_REVENUE, BASIS_ATT_NP]
    got = frame[frame["basis"].isin(both)]
    wide = got.pivot_table(index=["symbol", "report_date"], columns="basis",
                           values="shares", aggfunc="first").dropna(subset=both)
    if wide.empty:
        return pd.DataFrame(columns=["symbol", "report_date",
                                     "shares_revenue", "shares_att_np", "rel_diff"])
    wide = wide.reset_index()
    wide["rel_diff"] = (wide[BASIS_REVENUE] / wide[BASIS_ATT_NP] - 1.0).abs()
    wide = wide.rename(columns={BASIS_REVENUE: "shares_revenue", BASIS_ATT_NP: "shares_att_np"})
    return wide[["symbol", "report_date", "shares_revenue", "shares_att_np", "rel_diff"]]


def share_jumps(frame: pd.DataFrame | None = None, *, basis: str = BASIS_REVENUE,
                ratio: float = 10.0, look_ahead: int = 4,
                settle_tol: float = 0.1) -> pd.DataFrame:
    """相邻报告期 ≥ratio 倍的跳变清单；reverts 标出「跳完又回到原位」的孤立尖峰。

    增发、回购、IPO 造成的股本台阶是**永久**的，后续报告期停在 Levels 上；
    reverts=True 意味着台阶在 look_ahead 期内又回到跳变前的水平，那种是脏数据。
    默认只看 revenue_period 通道：att_np_latest 按定义不随报告期走，没有台阶可言。
    """
    if frame is None:
        frame = shares_frame()
    sub = frame[frame["basis"] == basis].sort_values(["symbol", "report_date"])
    rows = []
    for sym, g in sub.groupby("symbol", sort=True):
        s = g.set_index("report_date")["shares"]
        prev = s.shift(1)
        hit = (s / prev >= ratio) | (s / prev <= 1.0 / ratio)
        for d in s.index[hit.fillna(False)]:
            after = s.loc[(s.index > d)][:look_ahead]
            base = float(prev.loc[d])
            rows.append({"symbol": sym, "report_date": int(d), "shares_prev": base,
                         "shares": float(s.loc[d]), "ratio": float(s.loc[d]) / base,
                         "reverts": bool(((after / base - 1.0).abs() < settle_tol).any())})
    cols = ["symbol", "report_date", "shares_prev", "shares", "ratio", "reverts"]
    return pd.DataFrame(rows, columns=cols)


def error_counts() -> Counter[str]:
    """按函数名分桶的吞错计数（读侧配置缺失等）。"""
    return Counter(_ERROR_COUNT)


def clean_counts() -> Counter[str]:
    """按原因分桶的清洗计数：任何下游比率都该知道分母少了多少行。"""
    return Counter(_CLEAN_COUNT)
