"""因子只能用 hfq/raw 推，不能用 qfq：qfq 以最新日为锚，历史会随每次分红整体重算。

§0 实测：sz000021 的 qfq 序列在 2026-10 与未复权逐行相同（锚在最新），
而 hfq 在 1997-08-01 是 98.370、未复权收 23.050 ⇒ factor≈4.27 且稳定可存。
"""
import pytest

from tdxfeed.adj_factor_history import derive_factor, write_ext


def row(d, close):
    return [int(d), close, close, close, close, 100.0, 0.0]


def test_factor_is_hfq_over_raw_per_date():
    raw = [row("19970801", 23.05), row("19970804", 20.0)]
    hfq = [row("19970801", 98.37), row("19970804", 80.0)]
    out = derive_factor(raw, hfq)
    assert out[0][0] == 19970801
    assert out[0][3] == pytest.approx(98.37 / 23.05, rel=1e-6)


def test_interior_gap_raises_but_different_floors_intersect():
    """两条序列的「地板」天然不同：2026-10-26 实测 sz000021 未复权回到 1996-10-21，
    后复权只回到 1999-12-21（源对复权序列的历史更短）。这不能当错误——因子就该只出
    交集那段。真正的危险是**中间有洞**：同一段里一边有某日、另一边没有，说明两条序列
    对不上，逐日比值会算出垃圾因子，必须抛。"""
    raw = [row("19970801", 23.05), row("19970804", 20.0), row("19970805", 21.0)]
    hfq = [row("19970801", 98.37), row("19970805", 84.0)]      # 缺 19970804 = 中间洞
    with pytest.raises(ValueError):
        derive_factor(raw, hfq)

    deeper_raw = [row("19961021", 12.0)] + raw            # raw 更早一段，hfq 没有
    out = derive_factor(deeper_raw, raw)
    assert [r[0] for r in out] == [19970801, 19970804, 19970805], "取交集，不报错"


def test_zero_raw_close_is_rejected_not_divided():
    with pytest.raises(ZeroDivisionError):
        derive_factor([row("19970801", 0.0)], [row("19970801", 1.0)])


def test_write_ext_lands_in_its_own_dir_with_the_factor_columns(tmp_path):
    """另建目录：现存 data/hfq_factor/ 与 data/adj_factor/ 从 2020-01-02 起，
    锚定口径不同就是两套数字，就地改写会让已发布报告的历史结论失效。"""
    import pandas as pd

    rows = derive_factor([row("19970801", 23.05)], [row("19970801", 98.37)])
    p = write_ext(str(tmp_path), "000021", rows)
    assert p.parent.name == "adj_factor_ext"
    df = pd.read_parquet(p)
    assert list(df.columns) == ["date", "raw_close", "hfq_close", "factor"]
    assert df["date"].dtype == "int64"
