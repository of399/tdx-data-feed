"""步骤③的判据核：把 (date × feature) 的日频横截面 Rank IC 汇总成可比的每维统计。

它不做因子、不读行情、不算标签——只做**汇总与显著性**，而且四个统计量全部复用
`quant/multifactor/cv_utils.py` 里已经实现并检验过的件（`rank_ic` / `icir` /
`ic_newey_west` / `bh_fdr_correct`）。理由不是省事：158 维 vs 26 维的对照结论完全
取决于这几个数怎么算，两处各写一份就等于两套判据，横比失去意义。调用是**按属性**
取 `cv_utils.xxx`，所以 patch 打得穿——守卫
`tests/test_alpha158_ic.py::test_summary_delegates_to_the_existing_multifactor_helpers`。

三条口径（写错不报错，只静默给出可看的假数字）
--------------------------------------------
① **逐日横截面**，不是把整张面板堆一起做 pooled 相关：日期效应会吃掉一天之内的
   排序信息，实测同一个「当日内严格同序」的因子，pooled Spearman ≈ 0 而逐日 IC = 1。
② 横截面标的数不足 `min_codes` 的那天**丢弃**，不补 0：补 0 是把「没数据」伪装成
   「无信号」，会压低 IC 均值、抬高 ICIR 分母，让弱因子看起来稳。
③ 远期收益标签按天重叠（5 日标签的相邻两天共用 4 天）⇒ IC 序列天然自相关 ⇒
   裸 OLS 标准误会低估，必须走 Newey-West HAC（`maxlags` 由调用方按 horizon 给）。

多重检验
--------
158 个维度同时检验，裸 p 里必然混进假阳性。`summarize_ic` 对全部维度做一次
Benjamini–Hochberg FDR，报 `q_value` 与 `reject`：结论只按 q 说，裸 p 只作参考。
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from quant.multifactor import cv_utils

LABEL_COL_DEFAULT = "label"


@dataclass(frozen=True)
class IcConfig:
    """IC 汇总的参数。

    `min_codes=30` 是横截面下限：一天的截面只有几只票时 Spearman 的抽样噪声比因子
    本身还大，这种天进不进均值会改变结论，所以按「没验到」处理（丢弃）而不是当证据。
    `maxlags` 应与标签的前瞻天数一致——重叠标签的自相关长度就是 horizon。
    """

    label_col: str = LABEL_COL_DEFAULT
    min_codes: int = 30
    maxlags: int = 5
    alpha: float = 0.05


def daily_ic(panel: pd.DataFrame, feature_cols: Sequence[str],
             cfg: IcConfig | None = None, *, date_col: str = "date",
             extra: dict[str, Any] | None = None) -> pd.DataFrame:
    """逐日横截面 Rank IC。返回 index=日期字符串、columns=`feature_cols` 的宽表。

    某天某维给不出 IC 就是 NaN（横截面太小 / 该维整列缺失 / 有效对不足 5），
    不填 0。NaN 是「不知道」，0 是「知道没有」——这两个字态在汇总层必须分得开。
    """
    c = cfg or IcConfig()
    if feature_cols is None:
        raise ValueError("feature_cols 必须显式给出：默认全列等于把标签也当因子测")
    if c.label_col not in panel.columns:
        raise KeyError(f"面板没有标签列 {c.label_col!r}，逐日 IC 无从算起")
    extra = extra or {}
    rows: dict[str, dict[str, float]] = {}
    for day, sub in panel.groupby(str(date_col), sort=True):
        # 门槛只看标签：标签缺的那天整列都不能用，因子缺不缺是逐维的事
        if int(sub[c.label_col].notna().sum()) < c.min_codes:
            continue
        label = sub[c.label_col]
        rec = {f: cv_utils.rank_ic(sub[f], label) for f in feature_cols}
        for name, fn in extra.items():
            rec[name] = float(fn(sub))
        rows[str(day)] = rec
    cols = [*feature_cols, *extra]
    if not rows:
        return pd.DataFrame(columns=cols, dtype=float)
    return pd.DataFrame.from_dict(rows, orient="index").reindex(columns=cols).astype(float)


def summarize_ic(daily: pd.DataFrame, cfg: IcConfig | None = None) -> pd.DataFrame:
    """每维一行的汇总：n_days / mean_ic / ic_std / icir / pos_rate / nw_t / nw_p / q / reject。

    `n_days` 只数**有 IC 的天**（NaN 不算），所以它同时是效应量也是覆盖度：一个维度
    如果 n_days 远小于天数，它的均值是少数几天给的，不能和满覆盖的维度并列排。
    """
    c = cfg or IcConfig()
    records: list[dict[str, Any]] = []
    for feat in daily.columns:
        s = daily[feat]
        ok = s.dropna()
        n = int(ok.size)
        nw = cv_utils.ic_newey_west(s, maxlags=c.maxlags)
        records.append({
            "feature": feat,
            "n_days": n,
            "mean_ic": float(ok.mean()) if n else float("nan"),
            "ic_std": float(ok.std()) if n > 1 else float("nan"),
            "icir": cv_utils.icir(s),
            "pos_rate": float((ok > 0).mean()) if n else float("nan"),
            "nw_t": nw["t_stat"],
            "nw_p": nw["p_value"],
            "nw_se": nw["se_hac"],
        })
    out = pd.DataFrame(records)
    if out.empty:
        return pd.DataFrame(columns=[
            "n_days", "mean_ic", "ic_std", "icir", "pos_rate",
            "nw_t", "nw_p", "nw_se", "q_value", "reject"]).set_index("feature")
    reject, qvals = cv_utils.bh_fdr_correct(out["nw_p"].to_numpy(dtype=float),
                                           alpha=c.alpha)
    out["q_value"] = qvals
    out["reject"] = np.asarray(reject, dtype=bool)
    return out.set_index("feature")


__all__ = ["IcConfig", "daily_ic", "summarize_ic"]
