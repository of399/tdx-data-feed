"""对照评测器的判据：行集同源、逐日配对、NaN 语义、比较集记账。

这个脚本产出的数字要进研究文档并支撑「要不要把 47 维并进候选集」的裁定，所以它自己的
形状必须钉住四件事：

① 三个契约吃**同一批行**——否则「维数差」与「样本差」在数字上长得一样；
② Δ 是**同日相减**，池化等于把「哪天好」的水位差当成效应（与 IC 必须逐日算同一个坑）；
③ 任一侧当天给不出 IC ⇒ Δ 是 NaN，不是 0（0 会被均值当成"这一天两版打平"的证据）；
④ q 值带比较集大小——2 个比较的 q 与 184 个比较的 q 不是同一个数（KI-DATA-005 踩过）。

外加一条自证：把同一个契约传两遍，Δ 必须逐日为 0。这条抓的是"两份预测其实来自两次
不同拟合"（随机性没锁死）——真出问题时会立刻红，而不是给一个看起来很小但非零的 Δ。
"""
from __future__ import annotations

import importlib.util
import json
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "stage1_union_compare.py"
sys.path.insert(0, str(ROOT / "scripts"))

_spec = importlib.util.spec_from_file_location("stage1_union_compare", SCRIPT)
cmp_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cmp_mod)  # type: ignore[union-attr]


def _synthetic(tmp_path: Path, n_days: int = 20, n_codes: int = 60) -> Path:
    """一个有真信号的合成宽表：`sig` 与标签正相关，其余列（含 ALL35）都是纯噪声。

    `run()` 走的是生产入口 `load_and_validate(table, ALL35)`——行集由 ALL35 固定，与契约
    无关，这正是 ① 的机制。所以合成表必须把 ALL35 的列凑齐，否则用例测的是"缺列报错"
    而不是对照逻辑。行数按 `fit_oos_predictions` 的下界（train≥500）留出一倍余量。
    """
    from stage1_baseline import ALL35

    rng = np.random.default_rng(7)
    rows = []
    for d in range(n_days):
        day = date(2024, 1, 2) + timedelta(days=d)
        sig = rng.normal(size=n_codes)
        noise = rng.normal(size=(n_codes, 3))
        # 标签 = 信号 + 噪声 + 一点水位，让 OOS 上确实能排出序
        y = 0.9 * sig + 0.1 * noise[:, 0] + rng.normal(scale=0.4, size=n_codes)
        for i in range(n_codes):
            rec = {
                "code": f"sh{600000 + i}", "signal_date": day.isoformat(),
                "hypothetical_pnl": float(y[i]), "is_positive": int(y[i] > 0),
                "sig": float(sig[i]),
                "noise_0": float(noise[i, 0]), "noise_1": float(noise[i, 1]),
                "noise_2": float(noise[i, 2]),
            }
            rec.update({c: float(rng.normal()) for c in ALL35})
            rows.append(rec)
    df = pd.DataFrame(rows)
    out = tmp_path / "wide.parquet"
    df.to_parquet(out, index=False)
    return out


def _contract(tmp_path: Path, cid: str, feats: list[str]) -> Path:
    p = tmp_path / f"contract_{cid}.json"
    p.write_text(json.dumps({
        "contract_id": cid, "label": f"test {cid}", "selected_features": feats,
        "n_features": len(feats), "n_alpha158_added": 0,
        "hyperparams": {"learning_rate": 0.1, "n_estimators": 40, "num_leaves": 8,
                        "max_depth": 3, "min_child_samples": 5},
        "objective": "huber", "alpha": 0.5, "target": "hypothetical_pnl",
    }), encoding="utf-8")
    return p


def test_paired_delta_is_day_by_day_and_nan_not_zero():
    ic = pd.DataFrame({"b0": [0.10, 0.20, np.nan, 0.40],
                       "b1": [0.12, np.nan, 0.30, 0.40]},
                      index=["2025-01-02", "2025-01-03", "2025-01-06", "2025-01-07"])
    d = cmp_mod.paired_delta(ic, "b0")
    assert list(d.columns) == ["b1_minus_b0"]
    vals = d["b1_minus_b0"].tolist()
    assert vals[0] == pytest.approx(0.02)
    assert np.isnan(vals[1]) and np.isnan(vals[2]), "单边缺值必须 NaN，填 0 等于谎报打平"
    assert vals[3] == pytest.approx(0.0)


def test_run_uses_one_row_set_for_every_contract(tmp_path):
    wide = _synthetic(tmp_path)
    c0 = _contract(tmp_path, "b0", ["sig"])
    c1 = _contract(tmp_path, "b1", ["sig", "noise_0", "noise_1", "noise_2"])
    res = cmp_mod.run(wide, [c0, c1], "b0", "2024-01-12", "t", [2, 5], n_boot=120)
    n = res["meta"]["n_rows_all_contracts"]
    assert n == 20 * 60, "行集必须与契约无关（由 ALL35/校验固定），不能各算各的"
    assert res["meta"]["n_train"] + res["meta"]["n_valid"] == n
    assert set(res["contracts"]) == {"b0", "b1"}
    # 截面 60 只 ≥ 判据线 ⇒ 逐日配对 IC 可用，且比较集记成 1（只有一个非基线契约）
    assert res["daily_ic_availability"]["available"] is True
    delta = res["delta_ic"]["b1_minus_b0"]
    assert delta["comparison_set"] == 1
    assert delta["n_days"] == delta["n_days_both_sides"]
    assert res["ic"]["b0"]["mean_ic"] > 0.2, "合成信号没被模型学到，用例本身失效"
    assert set(res["bootstrap"]["b1_minus_b0"]["boot"]) == {"pooled_rank_ic", "top2", "top5"}


def test_boot_cells_carry_q_and_name_the_comparison_set(tmp_path):
    """q 依赖比较集：每个 Δ 单元都要带 q_boot 与它所属的比较集大小，不能只留一个 p。

    三个契约、两条非基线对比、四个指标 ⇒ 家族 = 8。写成断言是因为"引用 p=0.034 而不说
    它在 8 个比较里排第几"正是 KI-DATA-005 登记过的那类误读。
    """
    wide = _synthetic(tmp_path)
    c0 = _contract(tmp_path, "b0", ["sig"])
    c1 = _contract(tmp_path, "b1", ["sig", "noise_0"])
    c2 = _contract(tmp_path, "b2", ["sig", "noise_1"])
    res = cmp_mod.run(wide, [c0, c1, c2], "b0", "2024-01-12", "t", [2, 5, 10], n_boot=40)
    cells = [b for block in res["bootstrap"].values() for b in block["boot"].values()]
    assert len(cells) == 2 * 4, len(cells)
    assert all(c["comparison_set"] == 8 for c in cells)
    for c in cells:
        assert 0.0 <= c["q_boot"] <= 1.0
        assert c["q_boot"] >= c["p_boot"] - 1e-12, "BH 的 q 不会小于自己的 p"
        if c["reject"]:
            assert c["p_boot"] <= 0.05
    md = cmp_mod.render_md(res)
    assert "比较集 = 8" in md


def test_identical_contracts_give_exactly_zero_delta(tmp_path):
    """同一份契约传两遍 ⇒ 每天 Δ 恰为 0。抓的是随机性没锁死（subsample/colsample）。"""
    wide = _synthetic(tmp_path)
    c0 = _contract(tmp_path, "b0", ["sig", "noise_0"])
    c1 = _contract(tmp_path, "b1", ["sig", "noise_0"])
    res = cmp_mod.run(wide, [c0, c1], "b0", "2024-01-12", "t", [2, 5], n_boot=120)
    d = cmp_mod.paired_delta(
        pd.DataFrame({k: pd.Series(v) for k, v in res["ic_daily"].items()}), "b0")
    assert np.nanmax(np.abs(d.to_numpy(dtype=float))) == 0.0, "两次拟合不逐字相同"


def test_missing_dimension_fails_loudly(tmp_path):
    wide = _synthetic(tmp_path)
    bad = _contract(tmp_path, "b0", ["sig"])
    worse = _contract(tmp_path, "b9", ["sig", "NOT_IN_TABLE"])
    with pytest.raises(ValueError, match="NOT_IN_TABLE"):
        cmp_mod.run(wide, [bad, worse], "b0", "2024-01-12", "t", [2, 5], n_boot=20)


def test_unknown_baseline_fails_loudly(tmp_path):
    wide = _synthetic(tmp_path)
    c0 = _contract(tmp_path, "b0", ["sig"])
    with pytest.raises(ValueError, match="基线"):
        cmp_mod.run(wide, [c0], "bzz", "2024-01-12", "t", [2, 5], n_boot=20)


def test_day_blocks_never_split_a_day():
    """整块重抽的最小单位是"天"：一个块里的行必须同日，且块划分覆盖全部行。"""
    values = np.array(["d1", "d1", "d2", "d1", "d2", "d3"], dtype=object)
    blocks = cmp_mod.day_blocks(values)
    assert sorted(len(b) for b in blocks) == [1, 2, 3]
    for b in blocks:
        assert len(set(values[b])) == 1, "块内混了两天"
    assert sorted(np.concatenate(blocks).tolist()) == list(range(6))


def test_bootstrap_delta_is_exactly_zero_for_identical_contracts(tmp_path):
    """同一份契约传两遍 ⇒ 每个指标的 Δ 点值与自助均值都恰为 0。"""
    wide = _synthetic(tmp_path)
    c0 = _contract(tmp_path, "b0", ["sig", "noise_0"])
    c1 = _contract(tmp_path, "b1", ["sig", "noise_0"])
    res = cmp_mod.run(wide, [c0, c1], "b0", "2024-01-12", "t", [2, 5], n_boot=60)
    block = res["bootstrap"]["b1_minus_b0"]
    assert all(v == 0.0 for v in block["point_delta"].values()), block["point_delta"]
    assert all(b["mean_delta"] == 0.0 for b in block["boot"].values())
    assert all(b["ci_lo"] == 0.0 and b["ci_hi"] == 0.0 for b in block["boot"].values())


def test_bootstrap_separates_a_real_edge_from_a_pure_noise_contract(tmp_path):
    """b0 只吃噪声、b1 吃到信号 ⇒ pooled Rank IC 的 Δ 必须与 0 分得开（CI 不跨 0）。"""
    wide = _synthetic(tmp_path)
    c_noise = _contract(tmp_path, "b0", ["noise_1", "noise_2"])
    c_sig = _contract(tmp_path, "b1", ["sig"])
    res = cmp_mod.run(wide, [c_noise, c_sig], "b0", "2024-01-12", "t", [2, 5], n_boot=200)
    boot = res["bootstrap"]["b1_minus_b0"]["boot"]["pooled_rank_ic"]
    assert boot["ci_lo"] > 0, f"真有优势却没被判出来：{boot}"
    assert boot["p_boot"] <= 0.05, boot
    assert res["contracts"]["b0"]["pooled_oos"]["spearman"] < 0.1


def test_thin_cross_section_is_declared_unavailable_not_printed_as_nans(tmp_path):
    """每天只有 8 只时：逐日配对 IC 必须写"不可用 + 原因"，不许交出一张 NaN 表。

    这是事件样本的真实形状（真跑一次就撞上：415 天、日均 6.5 笔，`daily_ic` 一天都
    给不出，报告里却照样印了 n_days=0 的空行）。空表会被读成"验出来是 0"。
    """
    wide = _synthetic(tmp_path, n_days=130, n_codes=8)
    c0 = _contract(tmp_path, "b0", ["sig"])
    c1 = _contract(tmp_path, "b1", ["sig", "noise_0"])
    res = cmp_mod.run(wide, [c0, c1], "b0", "2024-03-21", "thin", [2, 5], n_boot=40)
    avail = res["daily_ic_availability"]
    assert avail["available"] is False, avail
    assert avail["usable_days"] == 0
    assert avail["days_at_or_above_min_codes"] == 0
    assert "min_codes=30" in avail["reason"]
    assert res["delta_ic"] == {} and res["ic"] == {} and res["ic_daily"] == {}
    md = cmp_mod.render_md(res)
    assert "本样本上不可用" in md
    assert "nan" not in md.lower(), "报告里不许出现 NaN 行"
    # 判定改由整块自助法承担，它不依赖横截面宽度
    assert res["bootstrap"]["b1_minus_b0"]["boot"]["pooled_rank_ic"]["n_boot"] == 40
