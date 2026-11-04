"""宽表可选装配 Alpha158 列：锚点约定、名单来源、失败记账。

三条判据各抓一种「形状对了但值是错的」：
① 锚点——26 维在 `sig_idx-1` 上算（信号日前一根），158 维必须在**同一根 bar** 上取。
   取错一根不会报错，只会让特征掺进信号日当天的收盘（前视）。
② 名单来源——列集合只能来自 `config/alpha158_union.json` 这类工件或显式名单，
   不许在装配代码里再抄一份。
③ 失败要记账——evaluate 抛错或帧对不齐时整行跳过并计数；静默少一列等于少一维还没人知道。
"""
from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from tdxfeed import adjusted, stage1_wide_table as swt

N = 30


def _write_universe(tmp_path: Path, closes: list[float],
                    zero_volume_at: Sequence[int] = ()) -> tuple[Path, Path]:
    d = tmp_path / "daily"
    d.mkdir(parents=True)
    f = tmp_path / "hfq"
    f.mkdir(parents=True)
    arr = np.asarray(closes, dtype=float)
    volume = np.full(len(closes), 1000.0)
    amount = np.full(len(closes), 1.0e7)
    for i in zero_volume_at:              # 停牌日：成交量与成交额都是 0
        volume[i] = 0.0
        amount[i] = 0.0
    df = pd.DataFrame({
        "date": [date(2020, 1, 2) + timedelta(days=i) for i in range(len(closes))],
        "open": arr * 0.995,
        "high": arr * 1.01,
        "low": arr * 0.99,
        "close": arr,
        "volume": volume,
        "amount": amount,
    })
    df.to_parquet(d / "sh600000.parquet", index=False)
    fac = pd.DataFrame({
        "code": "600000", "date": df["date"],
        # 因子从 1.0 抬到 2.0：raw 口径与 hfq 口径在这只票上差一倍
        "hfq_factor": [1.0] * 15 + [2.0] * (len(closes) - 15),
        "raw_close": arr,
        "hfq_close": arr * np.asarray([1.0] * 15 + [2.0] * (len(closes) - 15)),
    })
    fac.to_parquet(f / "600000.parquet", index=False)
    return d, f


def _src(tmp_path: Path, signal_dates: list[str]) -> Path:
    src = tmp_path / "src.csv"
    pd.DataFrame({
        "code": ["sh600000"] * len(signal_dates),
        "signal_date": signal_dates,
        "is_positive": [1] * len(signal_dates),
        "hypothetical_pnl": [0.42] * len(signal_dates),
        **{k: [0.0] * len(signal_dates) for k in swt.FEAT26_ORDER},
    }).to_csv(src, index=False)
    return src


def _patch(monkeypatch, d: Path, f: Path) -> None:
    monkeypatch.setattr(adjusted, "daily_path", lambda s: d / "sh600000.parquet")
    monkeypatch.setattr(adjusted, "hfq_factor_path", lambda s: f / "600000.parquet")
    monkeypatch.setattr(swt, "DAILY", d)


def test_alpha158_cols_take_the_same_anchor_as_feat26(monkeypatch, tmp_path):
    """ROC5 = Ref($close,5)/$close：锚点行是 signal_date 前一根，不是它本身。"""
    closes = [10.0 + i for i in range(N)]          # 单调：任何错位都能从比值上看出
    d, f = _write_universe(tmp_path, closes)
    _patch(monkeypatch, d, f)
    sig = date(2020, 1, 2) + timedelta(days=20)
    src = _src(tmp_path, [sig.isoformat()])
    out = tmp_path / "wide.parquet"

    swt.build_wide_table(src_csv=src, out_parquet=out, verify_parity=False,
                         price_basis="hfq", alpha158_cols=["ROC5", "MA5"])
    got = pd.read_parquet(out)
    assert len(got) == 1
    hfq_close = np.asarray(closes[:15] + [2 * c for c in closes[15:]], dtype=float)
    # dates.index(sig)=20 ⇒ anchor=19 ⇒ ROC5 = hfq_close[14] / hfq_close[19]
    expect = hfq_close[14] / hfq_close[19]
    assert got["ROC5"].iloc[0] == pytest.approx(expect, rel=1e-9), (
        f"锚点错位：期望 {expect:.6f}（bar 19），实得 {got['ROC5'].iloc[0]:.6f}")
    assert got["MA5"].iloc[0] == pytest.approx(
        hfq_close[15:20].mean() / hfq_close[19], rel=1e-9)


def test_vwap_column_is_the_same_caliber_as_close(monkeypatch, tmp_path):
    """VWAP0 必须与 $close 同口径：amount/(volume×100) 再按当日因子 rebase，值应在 1 附近。"""
    closes = [10.0 + i for i in range(N)]
    d, f = _write_universe(tmp_path, closes)
    _patch(monkeypatch, d, f)
    src = _src(tmp_path, [(date(2020, 1, 2) + timedelta(days=20)).isoformat()])
    out = tmp_path / "wide.parquet"
    swt.build_wide_table(src_csv=src, out_parquet=out, verify_parity=False,
                         price_basis="hfq", alpha158_cols=["VWAP0"])
    v = float(pd.read_parquet(out)["VWAP0"].iloc[0])
    # 每股 VWAP = 1e7/(1000×100) = 100（raw 口径）；锚点 bar 19 的因子是 2 ⇒ vwap_hfq = 200
    # VWAP0 = $vwap/$close = 200 / (2×29)。关键是它**带着当日因子**，
    # 而不是 raw÷hfq 那种跨口径（那会给出 100/29 或再除一次因子）。
    hfq_close = np.asarray(closes[:15] + [2 * c for c in closes[15:]], dtype=float)
    expect = 100.0 * 2.0 / hfq_close[19]
    assert v == pytest.approx(expect, rel=1e-6), f"跨口径回归: 期望 {expect:.4f}，实得 {v:.4f}"


def test_alpha158_all_lands_the_whole_artifact_and_nothing_else(monkeypatch, tmp_path):
    """`alpha158_cols="all"` 出满 158 列；不传就一列都不加（现役宽表逐值不变）。"""
    closes = [10.0 + (i % 5) for i in range(N)]
    d, f = _write_universe(tmp_path, closes)
    _patch(monkeypatch, d, f)
    src = _src(tmp_path, [(date(2020, 1, 2) + timedelta(days=25)).isoformat()])

    plain = tmp_path / "plain.parquet"
    swt.build_wide_table(src_csv=src, out_parquet=plain, verify_parity=False,
                         price_basis="hfq")
    base_cols = set(pd.read_parquet(plain).columns)
    assert not [c for c in base_cols if c.isupper()], "默认路径不该带进 158 列"

    full = tmp_path / "full.parquet"
    swt.build_wide_table(src_csv=src, out_parquet=full, verify_parity=False,
                         price_basis="hfq", alpha158_cols="all")
    got = pd.read_parquet(full)
    added = sorted(set(got.columns) - base_cols)
    from tdxfeed.alpha158.eval import load_payload
    names = [x["name"] for x in load_payload()["features"]]
    assert added == sorted(names), f"多出/少列：{len(added)} vs {len(names)}"
    # 不传时既有的列值必须一字不变
    assert np.allclose(got[list(base_cols - {"code", "signal_date"})].to_numpy(dtype=float),
                       pd.read_parquet(plain)[list(base_cols - {"code", "signal_date"})]
                       .to_numpy(dtype=float), equal_nan=True)


def test_evaluate_failure_is_counted_not_silently_dropped(monkeypatch, tmp_path):
    """求值抛错时整行跳过并计数——少一列而不记账，下游会把「没装配」读成「这一维是 0」。"""
    closes = [10.0 + i for i in range(N)]
    d, f = _write_universe(tmp_path, closes)
    _patch(monkeypatch, d, f)
    src = _src(tmp_path, [(date(2020, 1, 2) + timedelta(days=20)).isoformat()])

    def boom(*a, **k):
        raise RuntimeError("求值面炸了")

    monkeypatch.setattr(swt, "evaluate_frame", boom)
    out = tmp_path / "wide.parquet"
    stats = swt.build_wide_table(src_csv=src, out_parquet=out, verify_parity=False,
                                 price_basis="hfq", alpha158_cols=["ROC5"])
    assert stats["n_alpha158_fail"] == 1, stats
    assert len(pd.read_parquet(out)) == 0


def test_suspended_day_yields_nan_not_a_dropped_code(monkeypatch, tmp_path):
    """停牌日（volume=0）⇒ `$vwap` 给 NaN，但这一只票必须留在宽表里。

    这是全量装配踩到的真坑：`replace(0, pd.NA)` 之后 `to_numpy(dtype=float)` 会对
    `NAType` 调 `float()` 抛 TypeError，于是**整只票**被失败记账丢掉。形状上看不出来
    ——行数少几条、列一个不少——只有拿台账里的停牌票对一遍才现形。判据分两半：
    锚点没停牌 → 值正常且 `n_alpha158_fail=0`；锚点正好停牌 → 该行留着、VWAP0 是 NaN。
    """
    closes = [10.0 + i for i in range(N)]
    d, f = _write_universe(tmp_path, closes, zero_volume_at=[7])   # 远离锚点的停牌日
    _patch(monkeypatch, d, f)
    src = _src(tmp_path, [(date(2020, 1, 2) + timedelta(days=20)).isoformat()])
    out = tmp_path / "wide.parquet"
    stats = swt.build_wide_table(src_csv=src, out_parquet=out, verify_parity=False,
                                 price_basis="hfq", alpha158_cols=["VWAP0", "ROC5"])
    got = pd.read_parquet(out)
    assert stats["n_alpha158_fail"] == 0, f"停牌日把整只票丢了: {stats}"
    assert len(got) == 1, f"行没了: {len(got)}"
    assert np.isfinite(got["VWAP0"].iloc[0]), "非锚点的停牌日不该污染锚点值"

    d2, f2 = _write_universe(tmp_path / "b", closes, zero_volume_at=[19])  # 锚点当天停牌
    monkeypatch.setattr(adjusted, "daily_path", lambda s: d2 / "sh600000.parquet")
    monkeypatch.setattr(adjusted, "hfq_factor_path", lambda s: f2 / "600000.parquet")
    monkeypatch.setattr(swt, "DAILY", d2)
    src2 = _src(tmp_path / "b", [(date(2020, 1, 2) + timedelta(days=20)).isoformat()])
    out2 = tmp_path / "b" / "wide2.parquet"
    stats2 = swt.build_wide_table(src_csv=src2, out_parquet=out2, verify_parity=False,
                                  price_basis="hfq", alpha158_cols=["VWAP0", "ROC5"])
    got2 = pd.read_parquet(out2)
    assert stats2["n_alpha158_fail"] == 0, f"锚点停牌也把整只票丢了: {stats2}"
    assert len(got2) == 1, f"锚点停牌的行被删了: {len(got2)}"
    assert pd.isna(got2["VWAP0"].iloc[0]), "停牌日的 vwap 必须是 NaN，不能冒充 0 观测"
    assert np.isfinite(got2["ROC5"].iloc[0]), "ROC5 不用 $vwap，不该被停牌污染"


def test_alpha158_needs_the_hfq_basis(monkeypatch, tmp_path):
    """158 维的价格列必须走 hfq 读侧权威（AGENTS 的工件口径 ③）。

    raw basis 下装配出来的是一整套「没复权的价格比率」——形状、行数、列名全都正常，
    只有除权那几天值不同，所以这里宁可拒绝：跨口径是 KI-DATA-005 那一类静默缺陷。
    """
    closes = [10.0 + i for i in range(N)]
    d, f = _write_universe(tmp_path, closes)
    _patch(monkeypatch, d, f)
    src = _src(tmp_path, [(date(2020, 1, 2) + timedelta(days=20)).isoformat()])
    with pytest.raises(ValueError, match="price_basis"):
        swt.build_wide_table(src_csv=src, out_parquet=tmp_path / "x.parquet",
                             verify_parity=False, price_basis="raw",
                             alpha158_cols=["ROC5"])


def test_reader_takes_the_column_list_from_the_union_artifact(tmp_path):
    """名单三种写法都要落到同一个解析器上——不许有第二条「手抄列名」的路。"""
    from tdxfeed.alpha158.eval import load_payload
    art = tmp_path / "union.json"
    art.write_text(json.dumps({"alpha158_net_new": ["ROC5", "MA5"],
                               "feat26_independent": ["ret_5d"],
                               "union": ["ret_5d", "ROC5", "MA5"]}), encoding="utf-8")
    assert swt.resolve_alpha158_cols(str(art)) == ["ROC5", "MA5"]
    assert swt.resolve_alpha158_cols("all") == [x["name"] for x in load_payload()["features"]]
    assert swt.resolve_alpha158_cols("ROC5,MA5") == ["ROC5", "MA5"]
    with pytest.raises(ValueError):
        swt.resolve_alpha158_cols("ROC5,NOT_A_DIM")     # 认不出来的名字要拒绝，不是静默丢
    with pytest.raises(ValueError):
        swt.resolve_alpha158_cols(str(art.with_suffix(".nope.json")))
