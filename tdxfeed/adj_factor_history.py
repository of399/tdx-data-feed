"""从腾讯 hfq/未复权两条序列反推后复权因子，扩到 2000 年以前。

为什么另建目录：data/adj_factor/ 与 data/hfq_factor/ 都从 2020-01-02 起
（各 5221 只 / 1633 行），锚定口径不同就是两套数字；已发布报告引用的是旧目录，
就地改写会让历史结论失效。

为什么只用 hfq 不用 qfq：qfq 以最新日为锚（§0 实测 sz000021 的 qfq 在 2026-10
与未复权逐行相同），每有一次分红整条历史序列都要重算，存下来的因子隔天就漂；
hfq 锚在上市日，历史段不动，才可当静态数据存。

本目录与 data/hfq_factor/ 仍是两套口径，不可混用：旧表（2026-10-29 之前）存的其实
是 1/前复权因子——那个脚本把 adjustflag=3（不复权）当成后复权、又用前复权价做分母。
该表已于 2026-10-29 重建为真后复权（adjustflag=1 / 3，hfq_factor = hfq_close / raw_close）。
重建后两套仍可能因锚点不同相差常数倍（2026-10-26 实测的那组比值散布属于旧表的错误
口径，已不能作为换算依据），所以这里只写自己的目录，下游引用必须显式选一个，不要做
「读不到就回落到另一套」——回落只会把两套数字混进同一条序列。
"""
from __future__ import annotations

import os
from pathlib import Path

import pandas as pd

COLS = ["date", "raw_close", "hfq_close", "factor"]


def derive_factor(raw_rows, hfq_rows):
    """逐日 factor = hfq_close / raw_close，只出两条序列的交集。

    「地板」不同是正常的（实测未复权比后复权能多回两三年），中间有洞才是危险的：
    那说明两条序列在同一段里对不齐，逐日比值会算出垃圾因子，必须抛出来。
    """
    raw = {r[0]: float(r[4]) for r in raw_rows}
    adj = {r[0]: float(r[4]) for r in hfq_rows}
    common = sorted(set(raw) & set(adj))
    if not common:
        raise ValueError("两条序列没有任何一天重合，无法反推因子")
    lo, hi = common[0], common[-1]
    holes = sorted((d for d in (set(raw) | set(adj)) if lo < d < hi and d not in common))
    if holes:
        raise ValueError(
            f"交集内部有洞（{len(holes)} 天，例 {holes[:3]}）：两条序列在这段对不齐，"
            "逐日比值不可信。地板差（一方更早上市）不在此列，直接取交集。")
    out = []
    for d in common:
        rc = raw[d]
        if rc <= 0:
            raise ZeroDivisionError(f"{d} 未复权收盘 {rc} ≤0，因子无法反推")
        out.append([d, rc, adj[d], adj[d] / rc])
    return out


def write_ext(out_root: str, code: str, factor_rows) -> Path:
    d = Path(out_root) / "adj_factor_ext"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{code}.parquet"
    df = pd.DataFrame(factor_rows, columns=COLS)
    df["date"] = df["date"].astype("int64")
    tmp = p.with_suffix(".parquet.tmp")
    df.to_parquet(tmp, index=False)
    os.replace(tmp, p)   # 半写不可见：中断不留脏文件
    return p
