"""B2 四臂对照的判据与接线测试（合成数据，零 `data/` 依赖，CI 也必须判到）。

`NEEDS_DATA = False` 是刻意的：这条文件钉的是「四臂的契约」与「判据的形状」，
一遇数据缺失就整片 skip 的判据等于没有（形状同 `tests/test_st_supply_in_features.py`，
坑见 `tests/test_ci_data_skip.py` 的等值登记表）。

真数据上的读数（四臂训练、命中率、CI、q 值）不在这里——那是
`scripts/compare_daban_stfix_arms.py` 的一次实跑，结果写
`docs/research/2026-11-05-daban-stfix-retrain-arms.md`。这里只保证**跑出来的东西
按预登记的判据解释**，而且改不动形状：

  · 臂的唯一定义在 `tdxfeed.st_supply.ARM_PLAN`，训练侧不许再抄一份源名清单
  · 产物路径必须能证明「不可能覆盖现役模型」（`--arm` 强制带臂名后缀的 tag）
  · 三组不变量（超参 / 切分与采样 / 特征管线）必须由**真正被执行的那几个对象**打印，
    不许是手抄的字面量——否则「四臂只差供给」这句话本身没有证据
  · 逐日 Top-5 命中率必须与 `scripts/report_daban_topk.py::_topk_stats` 同口径
  · 切流的必要条件（Δ vs ctl 的 95% CI 不跳 0 且为正）与 BH q 的比较集在 `build_verdict`
"""
from __future__ import annotations

import ast
import datetime as dt
import importlib.util
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tdxfeed import (  # noqa: E402
    st_history as st,
    st_supply as sup,
)

NEEDS_DATA = False

B2_DATE = "20261105"


def _load(rel_path: str, alias: str):
    """按文件路径加载脚本模块（`scripts/` 不是包，且脚本自己会补 sys.path）。"""
    spec = importlib.util.spec_from_file_location(alias, ROOT / rel_path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _train_mod():
    return _load("scripts/train_daban_model.py", "train_daban_model_b2_test")


def _cmp_mod():
    return _load("scripts/compare_daban_stfix_arms.py", "compare_daban_stfix_arms_b2_test")


def ev(code: str, impl, action: str, source: str = "f10_ledger"):
    return {"code": code, "ann_date": None, "impl_date": impl, "action": action,
            "kind": "实施其他风险警示" if action == "实施" else "撤销其他风险警示",
            "source": source,
            "confidence": 0.8 if source == st.INFERRED_SOURCE else None,
            "fetched_at": dt.datetime(2026, 11, 5, 12, 0, 0)}


def frame(events: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(events, columns=list(st.EVENT_COLUMNS))


# --------------------------------------------------------------------------- #
# 训练侧：--arm 的接线与产物命名
# --------------------------------------------------------------------------- #
def test_train_entry_lists_the_four_arms() -> None:
    """`--help` 必须能跑并且列出四个臂名（B1 学到的坑：argparse 建不出 parser 时 38 条测试全绿）。"""
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "train_daban_model.py"),
                        "--help"], capture_output=True, text=True, cwd=str(ROOT))
    assert r.returncode == 0, r.stderr[-800:]
    for arm in sup.ARMS:
        assert arm in r.stdout, f"--help 里没有臂 {arm}：{r.stdout[:600]}"


def test_arm_products_can_never_land_on_the_live_model(tmp_path: Path) -> None:
    """`--arm` 必须强制「带臂名的 tag」，并且解析结果不可能等于现役模型路径。"""
    mod = _train_mod()
    live = tmp_path / "d_signal_5d_model_daban.txt"
    # 没 tag ⇒ 现役路径 ⇒ 必须拒
    with pytest.raises(mod.ProductPathError):
        mod.resolve_product_paths("A2", tag="", out_dir=tmp_path)
    # tag 不带臂名后缀 ⇒ 无法从文件名认出是哪一臂 ⇒ 拒
    with pytest.raises(mod.ProductPathError):
        mod.resolve_product_paths("A2", tag="stfix_20261105", out_dir=tmp_path)
    # tag 里带路径分隔符 ⇒ 拒（产物必须留在 out_dir 里，不能顺着 tag 写到别处去）
    with pytest.raises(mod.ProductPathError):
        mod.resolve_product_paths("A2", tag="../stfix_20261105_A2", out_dir=tmp_path)
    model_out, report_out = mod.resolve_product_paths(
        "A2", tag=f"stfix_{B2_DATE}_A2", out_dir=tmp_path)
    assert model_out.name == f"d_signal_5d_model_daban_stfix_{B2_DATE}_A2.txt"
    assert report_out.name == f"daban_train_report_stfix_{B2_DATE}_A2.md"
    assert model_out != live
    # 不带 --arm 时保持 B1 的行为：留空 tag 仍走现役路径（生产入口不能被这次改造顺手改掉）
    m0, _ = mod.resolve_product_paths(None, tag="", out_dir=tmp_path)
    assert m0 == live


def test_live_path_guard_is_not_dead_code(tmp_path: Path) -> None:
    """`assert_not_live` 必须真的在比路径，不是只在 tag 为空时顺带挡住。"""
    mod = _train_mod()
    live = tmp_path / "d_signal_5d_model_daban.txt"
    with pytest.raises(mod.ProductPathError):
        mod.assert_not_live(live, arm="A2")
    mod.assert_not_live(tmp_path / f"d_signal_5d_model_daban_stfix_{B2_DATE}_A2.txt",
                        arm="A2")
    mod.assert_not_live(live, arm=None)      # 不带臂 ⇒ 现役路径是合法生产出口


def test_arm_and_include_inferred_are_mutually_exclusive() -> None:
    """`--arm I` 已经决定「并推断」，`--include-inferred` 同时给就是两个真相源 ⇒ 拒跑。"""
    mod = _train_mod()
    with pytest.raises(ValueError) as exc:
        mod.check_arm_flags("I", include_inferred=True)
    assert "--include-inferred" in str(exc.value)
    mod.check_arm_flags("A2", include_inferred=False)      # 这条不该抛
    mod.check_arm_flags(None, include_inferred=True)       # B1 的老路仍通


def test_load_st_supply_by_arm_matches_the_single_source_plan(tmp_path: Path) -> None:
    """臂 A 与臂 A2 必须**真的**差在深市改名层上；训练侧不许自己抄源名清单。"""
    mod = _train_mod()
    events = frame([
        ev("600700", dt.date(2024, 1, 2), "实施", source="akshare_sz"),
        ev("600701", dt.date(2011, 7, 1), "实施", source="price_inferred"),
    ])
    panel = tmp_path / "st_events.parquet"
    st.write_panel(events, path=panel)
    assert mod.load_st_supply_for_arm("ctl", panel_path=panel) is None
    a = mod.load_st_supply_for_arm("A", panel_path=panel)
    a2 = mod.load_st_supply_for_arm("A2", panel_path=panel)
    arm_i = mod.load_st_supply_for_arm("I", panel_path=panel)
    assert a.sources == ("f10_ledger", "block_gn") and a.arm == "A"
    assert a.is_st("600700", dt.date(2024, 1, 10)) == (False, sup.TIER_UNSUPPLIED)
    assert a2.is_st("600700", dt.date(2024, 1, 10)) == (True, sup.TIER_AUTHORITY)
    assert a2.is_st("600701", dt.date(2011, 8, 1)) == (False, sup.TIER_UNSUPPLIED)
    assert arm_i.is_st("600701", dt.date(2011, 8, 1)) == (True, sup.TIER_INFERRED)


def test_trainer_does_not_copy_the_arm_source_lists() -> None:
    """训练脚本里不许出现第二份层名清单或第二份臂词表——漂了就没有「四臂只差供给」这回事。"""
    src = (ROOT / "scripts" / "train_daban_model.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    lit_strings = {n.value for n in ast.walk(tree)
                   if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    for layer in st.SOURCES:
        assert layer not in lit_strings, f"训练脚本里手抄了层名 {layer!r}，臂表是单源"
    assert "ARM_PLAN" in src or "build_supply" in src, \
        "训练侧没有走 st_supply 的臂表，四臂定义就变成两处各写一份"


def test_invariant_lines_come_from_the_objects_that_run() -> None:
    """三组不变量必须由**真正被 train() 使用**的对象打印，不是手抄字面量。

    判据：`invariant_lines()` 里出现的超参必须逐键等于 `train()` 交给 `lgb.train` 的那份
    （本批把它提成模块常量 `LGB_PARAMS`），切分数等于 `SPLIT_DATE`/`NEG_RATIO`/`RANDOM_SEED`，
    特征管线那一行必须现算 `compute_window_features_extended` 的列数而不是写「29 维」这句话。
    """
    mod = _train_mod()
    lines = "\n".join(mod.invariant_lines("A2"))
    src = Path(mod.__file__).read_text(encoding="utf-8")
    body = src[src.index("def train("):src.index("def write_report(")]
    assert "LGB_PARAMS" in body, "train() 里仍是内联字面量 ⇒ 不变量那一行是手抄的"
    assert "scale_pos_weight" in body, "scale_pos_weight 必须仍按样本现算，不能进常量表"
    for k, v in mod.LGB_PARAMS.items():
        assert str(k) in lines and str(v) in lines, f"不变量里缺超参 {k}={v}"
    assert mod.SPLIT_DATE in lines and str(mod.NEG_RATIO) in lines
    assert str(mod.RANDOM_SEED) in lines
    # 特征管线的列数必须现算：改 features_5d 加一列，这里要跟着变
    df = pd.DataFrame({
        "date": pd.date_range("2024-01-02", periods=12, freq="B"),
        "open": np.linspace(10, 11, 12), "high": np.linspace(10, 11, 12) + 0.1,
        "low": np.linspace(10, 11, 12) - 0.1, "close": np.linspace(10, 11, 12),
        "volume": np.full(12, 1e6), "amount": np.full(12, 1e7)})
    from tdxfeed.features_5d import compute_window_features_extended

    feat_cols = list(compute_window_features_extended(df, "600000", sig_idx=11,
                                                      is_st=False))
    assert str(len(feat_cols)) in lines, "不变量里的特征列数不是现算出来的"


def test_arm_report_records_the_arm_and_the_supply_distribution(tmp_path: Path) -> None:
    """每一臂的训练报告开头必须写清是哪一臂、读了哪几层、来源分布三档各多少行。

    产物只有模型文件没有「这一臂是什么」的说明，一周后没人能把它跟臂对上；
    而且推断层的行必须**在产物里可辨**（否则 `I` 臂会被后来人当权威口径复用）。
    """
    from tdxfeed.st_supply import TierTally

    mod = _train_mod()
    tally = TierTally()
    tally.add(sup.TIER_AUTHORITY)
    tally.add(sup.TIER_UNSUPPLIED)
    out = tmp_path / f"daban_train_report_stfix_{B2_DATE}_A2.md"
    mod.write_report({"feat_cols": ["limit"], "imp": [("limit", 1.0)],
                      "train_n": 10, "test_n": 5, "scale": 5.0, "auc": 0.7,
                      "best_iter": 3},
                     report_out=out, basis="raw",
                     arm="A2",
                     extra_lines=[*mod.invariant_lines("A2"), tally.line()])
    text = out.read_text(encoding="utf-8")
    assert "A2" in text and "ST 供给来源分布" in text
    assert "权威 1 行" in text and "无法判定 1 行" in text
    assert "compute_window_features_extended 29 维" in text, text[:400]


# --------------------------------------------------------------------------- #
# worker 侧端到端：按行选来源、逐臂整份重算、按臂打标签
# --------------------------------------------------------------------------- #
def _daily_frame(start: dt.date, rows: int, base: float) -> pd.DataFrame:
    idx = pd.date_range(start, periods=rows, freq="B")
    close = base + np.arange(rows, dtype=float) * 0.01
    return pd.DataFrame({
        "date": idx, "open": close * 0.99, "high": close * 1.02,
        "low": close * 0.98, "close": close,
        "volume": np.full(rows, 1e6), "amount": np.full(rows, 1e7)})


def test_score_group_picks_source_per_row_and_labels_by_arm(tmp_path: Path) -> None:
    """裸码撞车时**逐行**分来源，并按各自臂的阈值打标签（这是整条链最容易悄悄错的一格）。

    形状照真实快照：同一裸码 `000001` 有两只文件（指数价 ~3000、个股价 ~11），
    个股那一只在并轨前是主板 ST ⇒ 只有 A2 臂该把它的 5% 涨停行标成正样本，
    ctl 必须与快照自带的旧标签逐行相同。
    """
    import lightgbm as lgb

    from tdxfeed.execution.limits import limit_threshold
    from tdxfeed.features_5d import compute_window_features_extended

    mod = _cmp_mod()
    cols = list(compute_window_features_extended(_daily_frame(dt.date(2024, 1, 2), 30, 10.0),
                                                "000001", sig_idx=20))
    stock = _daily_frame(dt.date(2024, 1, 2), 40, 11.0)
    index = _daily_frame(dt.date(2024, 1, 2), 40, 3000.0)
    # 标签判的是 **T+1 相对 T** 的收盘涨幅，所以「涨停那一天」必须是 i=30 的下一天：
    # 跳变放在 c[31]=c[30]*1.05，i=30 这一行才是落在 5% 档的那一行。
    # 只改个股帧、不改指数帧 ⇒ 翻转行唯一可归因（两帧的 date 序列完全相同，
    # 若两边都涨 5% 就会有两行翻牌，断言再也分不出是来源选错还是档位算错）。
    # **先改再落盘**，否则文件里的 close 与 payload 的 prev_close 不是同一条序列，
    # 来源反查当然对不上。
    c = stock["close"].to_numpy(dtype=float).copy()
    c[31] = c[30] * 1.05
    stock["close"] = c
    stock["high"] = np.maximum(stock["high"], stock["close"])
    f_stock = tmp_path / "sz000001.parquet"
    f_index = tmp_path / "sh000001.parquet"
    stock.to_parquet(f_stock)
    index.to_parquet(f_index)

    rows = []
    for i in (28, 29, 30):
        for f, src in ((stock, "sz000001"), (index, "sh000001")):
            feat = compute_window_features_extended(f, "000001", sig_idx=i)
            row = {"date": str(f["date"].iat[i])[:10], "code": "000001",
                   "prev_close": float(f["close"].iat[i]),
                   "label": int(is_limit_up_ret_old(f, i)), "src_truth": src}
            row.update({k: float(v) for k, v in feat.items()})
            rows.append(row)
    payload = pd.DataFrame(rows)

    rng = np.random.default_rng(1)
    X = pd.DataFrame(rng.random((80, len(cols))), columns=cols)
    y = (rng.random(80) < 0.3).astype(int)
    booster = lgb.train({"objective": "binary", "verbosity": -1, "seed": 7},
                        lgb.Dataset(X, label=y), num_boost_round=4)
    mp = tmp_path / "m.txt"
    booster.save_model(str(mp))
    b = lgb.Booster(model_file=str(mp))

    # 面板只覆盖「翻转那一天及以后」：i=28/29 早于该票**最早可见边界** ⇒ 判「无法判定」
    # ⇒ 供给值与 ctl 同 ⇒ 该复用；i=30 在区间内 ⇒ 该重算。复用条件两头都有行可判。
    flip_day = stock["date"].iat[30].date()
    panel = frame([ev("000001", flip_day, "实施", source="akshare_sz"),
                   ev("000001", stock["date"].iat[36].date(), "撤销",
                      source="akshare_sz")])
    supplies = {"ctl": None, "A2": sup.StSupply(panel)}
    mod._W.clear()
    mod._W.update({"arms": ["ctl", "A2"], "models": {"ctl": b, "A2": b},
                   "feat_cols": {"ctl": cols, "A2": cols},
                   "supplies": supplies, "index": {"000001": [f_index, f_stock]},
                   "live": None, "live_cols": []})
    out, meta = mod.score_group(("000001", payload.drop(columns=["src_truth"])))

    # ① 逐行来源：指数行必须落 sh000001，个股行必须落 sz000001
    assert out["src_stem"].tolist() == payload["src_truth"].tolist(), \
        list(zip(out["src_stem"], payload["src_truth"], strict=True))
    # ② ctl 的标签逐行等于快照旧标签（旧口径复现）
    assert out["label_ctl"].tolist() == payload["label"].tolist()
    # ③ A2 只在「个股 + 并轨前 + T+1 涨幅落在 5% 档」那**一行**翻成 1
    st_rows = [j for j, s in enumerate(payload["src_truth"].tolist())
               if s == "sz000001"]                    # 载荷顺序：i=28,29,30 × (股,指)
    st_day = [payload["date"].iat[j] for j in st_rows]
    expect_flip = [d for d in st_day
                   if limit_threshold("000001", is_st=True,
                                      on_date=dt.date.fromisoformat(d)) == 0.045]
    assert expect_flip, "合成帧里得真有一条 5% 档的日子"
    flips = [j for j, (a, o) in enumerate(zip(out["label_A2"].tolist(),
                                              out["label_ctl"].tolist(), strict=True))
             if a != o]
    # 恰好一行：个股帧 i=30。多一行 ⇒ 指数行也被按个股的 ST 档打了标（来源与档位串台）；
    # 少一行 ⇒ 翻转链根本没通，只是断言写松了。
    assert flips == [st_rows[2]], (flips, st_rows,
                                   payload[["date", "src_truth"]].to_dict("records"))
    assert out["src_stem"].iat[flips[0]] == "sz000001"
    assert out["date"].iat[flips[0]] == str(stock["date"].iat[30])[:10]
    assert out["label_A2"].iat[flips[0]] == 1 and out["label_ctl"].iat[flips[0]] == 0
    # ④ 披露项：档位只给给了的臂记，ctl 不记档（「未供给」不是「判不出」）
    assert meta["tiers"]["ctl"] == {} and meta["tiers"]["A2"]
    assert set(meta["tiers"]["A2"]) <= {sup.TIER_AUTHORITY, sup.TIER_UNSUPPLIED}
    assert meta["axis_mismatch_arm_rows"] == 0
    # ④b 行级披露只数一次（在 ctl 臂上）：按臂×行累加会把同一个形状报成 4 倍，
    #      读起来像缺陷比实际大。指数行必须走**名单**而不是「文件名是不是 sh<裸码>」——
    #      对 600000 来说 `sh600000` 就是个股，那种写法两头都数错。
    assert meta["index_rows"] == 3, meta["index_rows"]
    assert meta["label_mismatch_ctl"] == 0, meta
    # ④d 复用计数：6 行 × 除 ctl 外 1 臂 ⇒ 上限 6 行。面板只从 i=30 起可见，
    #     i=28/29 判「无法判定」（供给值与 ctl 同）→ 复用 4 行；i=30 的两行
    #     （个股与指数同裸码，供给按裸码查）判到 ST → 整份重算 2 行。
    #     复用数=6 说明臂根本没重算，那条翻转行会被吃掉；=0 说明复用分支没接上。
    assert meta["reused_rows"] == 4, meta["reused_rows"]
    # ④c 没有 ctl 就没有归属 ⇒ 拒
    mod._W["arms"] = ["A2"]
    with pytest.raises(mod.SnapshotContractError) as exc2:
        mod.score_group(("000001", payload.drop(columns=["src_truth"])))
    assert "ctl" in str(exc2.value)
    # ⑤ 存值漂移：这轮的存值就是同一份代码算出来的 ⇒ 每列差数应为 0
    assert meta["drift_by_col"] == {}, meta["drift_by_col"]


def is_limit_up_ret_old(f: pd.DataFrame, i: int) -> float:
    """快照旧口径的标签判定：不传日期的阈值 + T+1 收盘涨幅。"""
    from tdxfeed.features_5d import get_limit as _g, is_limit_up_ret as _r

    return _r(float(f["close"].iat[i + 1] / f["close"].iat[i] - 1.0), _g("000001"))


def test_payload_columns_are_deduplicated() -> None:
    """`prev_close` 既是快照元列也是 29 维之一 ⇒ 载荷列必须去重（实跑前演练抓到的）。

    不去重时 `rows["prev_close"]` 返回 **DataFrame**，逐行供给那一格会带着一个假的
    「来源不唯一」错误炸掉，或者更糟——把两列同名列叠成一列继续算。
    """
    mod = _cmp_mod()
    cols = ["date", "code", "label", "prev_close",
            "limit", "prev_close", "ret_5d", "limit"]        # 故意的：元列与特征列交集
    got = mod.select_payload_cols(cols, feature_union=["prev_close", "ret_5d", "limit"])
    assert got == ["date", "code", "label", "prev_close", "ret_5d", "limit"], got
    assert len(got) == len(set(got))


def test_score_group_rejects_duplicate_payload_columns(tmp_path: Path) -> None:
    """worker 侧也要自己挡一道：载荷有同名列 ⇒ 响亮报错，不猜哪一列才是存值。"""
    mod = _cmp_mod()
    rows = pd.DataFrame([["2025-06-03", "600000", 0, 10.0, 10.0]],
                        columns=["date", "code", "label", "prev_close", "prev_close"])
    mod._W.clear()
    mod._W.update({"arms": ["ctl"], "models": {}, "feat_cols": {"ctl": []},
                   "supplies": {"ctl": None}, "index": {"600000": [tmp_path]},
                   "live": None, "live_cols": []})
    with pytest.raises(mod.SnapshotContractError) as exc:
        mod.score_group(("600000", rows))
    assert "prev_close" in str(exc.value)


def test_label_flip_summary_counts_by_year_without_collapsing() -> None:
    """翻牌数必须按年可追（报告 §「OOS 窗口 ≠ 全历史」要靠它）。

    这条是实跑前演练抓到的真错：`Series.groupby(...)` 迭代出来的是**子序列**，
    直接 `int(n)` 会 `TypeError`；换成 `.sum().items()` 才是每年的行数。
    """
    mod = _cmp_mod()
    frame = pd.DataFrame({
        "date": ["2025-06-03", "2025-06-04", "2026-01-05", "2026-01-06"],
        "label_old": [0, 1, 0, 0],
        "label_A2": [1, 1, 1, 0]})
    got = mod.label_flip_summary(frame)
    assert got["flip_0_to_1"] == 2 and got["flip_1_to_0"] == 0
    assert got["flips_total"] == 2 and got["n_rows"] == 4
    assert got["by_year"] == {"2025": 1, "2026": 1}, got["by_year"]
    assert got["old_positive"] == 1 and got["unified_positive"] == 3
    assert got["positive_change_pct"] == pytest.approx(2.0)


def test_evaluate_arms_carries_q_into_the_report_block() -> None:
    """`attach_bh_q` 改成不回写之后，`evaluate_arms` 必须接住它返回的新字典。

    演练时这里炸过一次：报告块里拿不到 `comparison_set` 直接 KeyError——
    「比较集 = 全部非基线臂」这句话一旦丢了，q 值就没法解释。
    """
    mod = _cmp_mod()
    rng = np.random.default_rng(9)
    frame = pd.DataFrame({
        "date": np.repeat([f"2025-06-{d:02d}" for d in range(1, 7)], 6),
        "code": np.tile([f"60{i:04d}" for i in range(6)], 6),
        "label": (rng.random(36) < 0.2).astype(int)})
    frame["label_A2"] = frame["label"]
    for arm in sup.ARMS:
        frame[f"score_{arm}"] = rng.random(36)
    out = mod.evaluate_arms(frame, arms=list(sup.ARMS), baseline="ctl", k=2,
                            n_boot=100, seed=42,
                            unified_label_col="label_A2", old_label_col="label")
    for name in (mod.UNIFIED_KEY, "old"):
        delta = out["label_sets"][name]["delta"]
        assert delta["comparison_set"] == 3, name
        for arm in ("A", "A2", "I"):
            e = delta[f"{arm}_minus_ctl"]
            assert {"point_delta", "ci_lo", "ci_hi", "p_boot", "q_value",
                    "n_days", "n_boot"} <= set(e), (name, arm, sorted(e))
            assert e["n_boot"] == 100 and e["n_days"] == 6
            assert isinstance(mod.necessary_condition(e), bool)
        for arm in sup.ARMS:                      # 基线自己也有一行水平数（不是只有 Δ）
            s = out["label_sets"][name]["per_arm"][arm]
            assert {"hit_rate_day_mean", "lift", "displacement_vs_baseline",
                    "auc", "n_days"} <= set(s), (name, arm)
    assert out["denominators"][mod.UNIFIED_KEY]["n_rows"] == 36


def test_arm_row_reuse_is_exact_and_only_when_supply_matches() -> None:
    """跨臂复用只允许在「(阈值, ST 布尔) 与 ctl 完全一样」时发生，且必须逐值相等。

    为什么需要这条：29 维里 `limit` 会渗进 5 个别的维，所以每臂本该整份重算；但
    绝大多数行四臂的供给值相同，逐臂重算会把 1.73M 行变成 6.9M 次调用。
    复用条件写错（比如只看 is_st 不看阈值，或反过来）就会让某一臂悄悄吃到别的臂的特征值，
    而这种错在数字上长得像「这一臂没变化」——事后看不出来，所以必须当场钉。
    """
    from tdxfeed.features_5d import compute_window_features_extended

    mod = _cmp_mod()
    f = _daily_frame(dt.date(2024, 1, 2), 30, 11.0)
    cols = list(compute_window_features_extended(f, "600000", sig_idx=20))
    ctl_flag = False
    ctl_row = compute_window_features_extended(f, "600000", sig_idx=20, is_st=ctl_flag)
    # ① 供给一样 ⇒ 复用，且复用的就是那份 ctl（逐键相等）
    same = mod.arm_feature_row(f, "600000", 20, ctl_flag, ctl_row,
                               limit_val=ctl_row["limit"], ctl_limit=ctl_row["limit"])
    assert same == ctl_row
    # 列名与列序不能被复用/重算这两条分支改动：`feat_rows` 是按 `[feat[c] for c in cols]`
    # 取值后**按位置**喂模型的，列序一变就和训练时的列契约错位
    assert list(same) == cols
    # ② 并轨前的主板 ST ⇒ 阈值与 is_st 都不同 ⇒ 必须重算，且 dim1/dim15 变了
    fresh = mod.arm_feature_row(f, "600000", 20, True, ctl_row,
                                limit_val=0.045, ctl_limit=ctl_row["limit"])
    assert fresh is not ctl_row
    assert fresh["limit"] == 0.045 and fresh["is_st"] == 1
    assert ctl_row["limit"] == 0.095 and ctl_row["is_st"] == 0
    # ③ 只比阈值也比 ST：这种「flag 与 ctl 相同但阈值被报成不同」的组合在真实供给里
    #    不出现（阈值由 (代码, 日期, flag) 派生），但复用条件必须两个都比——
    #    只比 flag 的写法会在这种组合上把 ctl 那份递出去。重算出来的 dim1 由函数
    #    自己派生（`limit_val` 只用于**是否复用**的判断，不参与算特征）。
    only_limit = mod.arm_feature_row(f, "600000", 20, False, ctl_row,
                                     limit_val=0.045, ctl_limit=ctl_row["limit"])
    assert only_limit is not ctl_row, "flag 同但阈值不同 ⇒ 不该复用"
    assert only_limit["limit"] == 0.095, "重算的 dim1 由 features_5d 自己派生"
    # ④ 没有 ctl 参照（ctl_row=None）⇒ 老老实实算
    first = mod.arm_feature_row(f, "600000", 20, False, None,
                                limit_val=0.095, ctl_limit=None)
    assert first is not None and first["limit"] == 0.095


# --------------------------------------------------------------------------- #
# 对照侧：行集契约、逐日判据、自助与裁定
# --------------------------------------------------------------------------- #
def _snap(n_days: int = 6, n_codes: int = 12) -> pd.DataFrame:
    """一份「够小但形状对」的合成冻结快照：date/code + 29 个模型列 + 两个标签列。"""
    from tdxfeed.features_5d import compute_window_features_extended

    daily = pd.DataFrame({
        "date": pd.date_range("2025-06-02", periods=40, freq="B"),
        "open": np.linspace(10, 14, 40), "high": np.linspace(10, 14, 40) + 0.2,
        "low": np.linspace(10, 14, 40) - 0.2, "close": np.linspace(10, 14, 40),
        "volume": np.full(40, 1e6), "amount": np.full(40, 1e7)})
    feat_cols = list(compute_window_features_extended(daily, "600000", sig_idx=39))
    rng = np.random.default_rng(11)
    rows = []
    for d in range(n_days):
        for c in range(n_codes):
            code = f"60{c:04d}"
            row = {"date": f"2025-06-{3+d:02d}", "code": code,
                   "label": int(rng.random() < 0.2),
                   "label_buyable": int(rng.random() < 0.15)}
            for f in feat_cols:
                row[f] = float(rng.random())
            rows.append(row)
    return pd.DataFrame(rows)


def test_row_set_and_column_contract_is_loud(tmp_path: Path) -> None:
    """列缺 / 行数变 / 键重排 ⇒ 停下报错，不许默默重装配（任务书第 4 条）。"""
    mod = _cmp_mod()
    snap = _snap()
    # ① 模型要一列快照里没有的 ⇒ 报错
    with pytest.raises(mod.SnapshotContractError) as exc:
        mod.check_snapshot_contract(snap, feature_cols=[*list(snap.columns[4:]), "not_here"],
                                    n_rows_expected=len(snap))
    assert "not_here" in str(exc.value)
    # ② 行数与登记值不符（有人顺手加了过滤）⇒ 报错，且报出实际行数
    with pytest.raises(mod.SnapshotContractError) as exc:
        mod.check_snapshot_contract(snap, feature_cols=list(snap.columns[4:]),
                                    n_rows_expected=len(snap) + 5)
    assert str(len(snap)) in str(exc.value), "要说清实测行数是多少"
    # ③ 键不唯一 ⇒ 报错（重装配会 1:N 放大）
    dup = pd.concat([snap, snap.head(2)], ignore_index=True)
    with pytest.raises(mod.SnapshotContractError):
        mod.check_snapshot_contract(dup, feature_cols=list(dup.columns[4:]),
                                    n_rows_expected=len(dup),
                                    allow_duplicate_keys=False)
    # ④ 本批的真快照就是撞码的：允许重复键但必须**报出**撞码数，且不改变行数
    n = mod.check_snapshot_contract(dup, feature_cols=list(dup.columns[4:]),
                                    n_rows_expected=len(dup))
    assert n == len(dup), "行数必须原样，撞码是披露不是过滤"


def test_per_day_top5_matches_the_existing_report_protocol() -> None:
    """逐日 Top-5 命中率必须与 `report_daban_topk._topk_stats` 的 precision_day 同值。

    不同口径就没法和 10-30/11-05 那两轮对照读；这条把「同一协议」钉成可判的等式。
    """
    mod = _cmp_mod()
    top = _load("scripts/report_daban_topk.py", "report_daban_topk_for_b2_test")
    rng = np.random.default_rng(3)
    df = pd.DataFrame({
        "date": np.repeat([f"2025-06-{d:02d}" for d in range(1, 9)], 40),
        "code": np.tile([f"60{i:04d}" for i in range(40)], 8),
        "score": rng.random(320),
        "label": (rng.random(320) < 0.12).astype(int)})
    # _topk_stats 还会算连板构成与主板占比，那三列是真快照里有的
    df["consecutive_boards"] = rng.integers(0, 3, 320)
    df["is_limit_up_today"] = rng.integers(0, 2, 320)
    df["is_gem"] = rng.integers(0, 2, 320)
    stats = top._topk_stats(df, k=5, label_col="label")
    tbl = mod.per_day_top_table(df, k=5, label_col="label", score_col="score")
    assert stats["precision_day"] == pytest.approx(tbl["hit_rate"].mean()), \
        (stats["precision_day"], tbl["hit_rate"].mean())
    # 分母形状：每天恰好 k 个槽位（横截面 ≥ k），池化命中率 == 逐日等权命中率
    assert list(tbl["slots"]) == [5] * 8
    assert stats["precision_pooled"] == pytest.approx(
        tbl["hits"].sum() / tbl["slots"].sum())


def test_displacement_counts_slots_not_rows_and_is_zero_for_self() -> None:
    """位移 = 逐日 Top-K 名单里「换掉的槽位数 / K」；自己跟自己比必须是 0。"""
    mod = _cmp_mod()
    a = mod.per_day_top_table(_scores_frame(), k=5, label_col="label", score_col="s_ctl")
    b = mod.per_day_top_table(_scores_frame(), k=5, label_col="label", score_col="s_A")
    same = mod.per_day_top_table(_scores_frame(), k=5, label_col="label", score_col="s_ctl")
    assert mod.displacement(a, same) == 0.0
    d = mod.displacement(a, b)
    assert 0.0 <= d <= 1.0
    # 逐日、等权（不是按行）：8 天里换 2 天、每天换 1 个槽位 ⇒ 2×(1/5)/8
    assert d == pytest.approx(2 * (1 / 5) / 8), d
    moved = mod.displacement_per_day(a, b)
    assert len(moved) == 8
    assert float(np.mean(moved)) == pytest.approx(d)
    assert sorted(moved) == [0.0] * 6 + [0.2, 0.2], moved


def _scores_frame() -> pd.DataFrame:
    rng = np.random.default_rng(5)
    df = pd.DataFrame({
        "date": np.repeat([f"2025-06-{d:02d}" for d in range(1, 9)], 20),
        "code": np.tile([f"60{i:04d}" for i in range(20)], 8),
        "label": (rng.random(160) < 0.1).astype(int)})
    base = rng.random((8, 20))
    df["s_ctl"] = base.ravel()
    alt = base.copy()
    # 第 3 天与第 6 天：让原第 8 名升到榜首、原榜首掉到第 5 名之外 ⇒ 每天恰好换掉 1 个槽位
    for day in (2, 5):
        order = np.argsort(-base[day])
        v = base[day].copy()
        v[order[7]] = float(base[day].max()) + 1.0
        v[order[0]] = base[day][order[9]]
        alt[day] = v
    df["s_A"] = alt.ravel()
    return df


def test_bootstrap_unit_is_the_trading_day_and_point_delta_matches() -> None:
    """自助只整块抽天：点估计必须等于「逐日等权命中率的差」，与行级重抽无关。"""
    mod = _cmp_mod()
    a = mod.per_day_top_table(_scores_frame(), k=5, label_col="label", score_col="s_ctl")
    b = mod.per_day_top_table(_scores_frame(), k=5, label_col="label", score_col="s_A")
    res = mod.bootstrap_delta({"ctl": a, "A": b}, metric="hit_rate",
                              baseline="ctl", n_boot=200, seed=42)
    entry = res["A_minus_ctl"]
    expect = float(b["hit_rate"].mean() - a["hit_rate"].mean())
    assert entry["point_delta"] == pytest.approx(expect)
    assert entry["n_days"] == 8 and entry["n_boot"] == 200
    assert entry["ci_lo"] <= entry["ci_hi"], entry
    assert entry["mean_delta"] <= entry["ci_hi"] and entry["mean_delta"] >= entry["ci_lo"], \
        "自助均值必须落在自己的 CI 里（否则 CI 不是这条分布算出来的）"
    # 抽样单位是天：同 seed 重跑必须逐值相同
    again = mod.bootstrap_delta({"ctl": a, "A": b}, metric="hit_rate",
                                baseline="ctl", n_boot=200, seed=42)
    assert again["A_minus_ctl"]["boot"] == entry["boot"]
    # 逐日等权：两天各 5 个槽位，一边全中一边全不中 ⇒ Δ=−1.0，按行加权也应是 −1.0，
    # 但**只有一天有数据**的形状必须被当成一天（拆天自助会把它摊薄）
    thin_a = pd.DataFrame({"date": ["d1", "d2"], "hits": [0, 5], "slots": [5, 5],
                           "hit_rate": [0.0, 1.0]})
    thin_b = pd.DataFrame({"date": ["d1", "d2"], "hits": [5, 5], "slots": [5, 5],
                           "hit_rate": [1.0, 1.0]})
    thin = mod.bootstrap_delta({"ctl": thin_a, "X": thin_b}, metric="hit_rate",
                               baseline="ctl", n_boot=50, seed=1)
    assert thin["X_minus_ctl"]["point_delta"] == pytest.approx(0.5), \
        "两天等权是 +0.5；给出别的数说明抽的不是整块天"


def test_pre_registered_protocol_numbers_are_frozen(tmp_path: Path) -> None:
    """预登记的判据数字不许在跑完之后被改：n_boot=2000、seed=42、K=5、基线=ctl。"""
    mod = _cmp_mod()
    assert mod.PROTOCOL == {"primary": "top5_hit_rate_day_mean", "k": 5,
                            "n_boot": 2000, "seed": 42, "baseline": "ctl",
                            "sampling_unit": "trading_day",
                            "bh": "per_family_over_non_baseline_arms"}
    # CLI 默认值就是它，且 --n-boot 之类只能显式覆盖（覆盖要留痕，见下一条）
    import argparse

    ap = mod.build_parser()
    ns = ap.parse_args([])
    assert ns.k == mod.PROTOCOL["k"] and ns.n_boot == mod.PROTOCOL["n_boot"]
    assert ns.seed == mod.PROTOCOL["seed"] and ns.baseline == mod.PROTOCOL["baseline"]


def test_reduced_arm_set_is_refused_not_rescaled(tmp_path: Path) -> None:
    """**少给臂 ⇒ 拒跑**，不许把比较集临时缩小。

    2026-11-05 范围变更（用户拍：主判据只跑 `ctl`+`A2`）之后，本条是被真实触发过的一次：
    两臂实跑 `--model ctl=… --model A2=…` 在 21:22:59 退出码 1，原文
    「缺臂 ['A', 'I'] ⇒ 判据按「三臂 vs ctl」预登记，缺臂就凑不齐比较集，不许临时改成两臂」。
    为什么要钉住这一格：`cluster_bootstrap_delta` 里 `rng` 是**一条流按臂顺序消耗**
    （`scripts/stage1_union_compare.py:196-207`），比较集缩小时留到 `A2` 的随机块位置会变
    ⇒ CI/p 是**那一族**的自助，不是「两臂独立重算」的自助。缩小族只能由人显式授权并留痕，
    不能让脚本在命令行上悄悄替人做这个决定。
    """
    mod = _cmp_mod()
    paths = {}
    for a in ("ctl", "A", "A2", "I"):
        f = tmp_path / f"m_{a}.txt"
        f.write_text("placeholder", encoding="utf-8")
        paths[a] = f
    got = mod._parse_models([f"{a}={paths[a]}" for a in ("ctl", "A", "A2", "I")])
    assert set(got) == set(mod.sup.ARMS)                       # 四臂齐全 ⇒ 通过
    with pytest.raises(SystemExit) as exc:
        mod._parse_models([f"{a}={paths[a]}" for a in ("ctl", "A2")])
    msg = str(exc.value)
    assert "缺臂" in msg and "'A'" in msg and "'I'" in msg, msg
    assert "不许临时改成两臂" in msg, "拒跑理由必须写的是『不许缩小比较集』，不然下轮会被当成 CLI bug"


def test_verdict_requires_ci_lo_above_zero_and_never_says_falsified() -> None:
    """切流必要条件：Δ vs ctl 的 95% CI 下界 > 0（方向为正且不含 0）；否则「证据不足，不切」。"""
    mod = _cmp_mod()
    ok = {"point_delta": 0.01, "ci_lo": 0.002, "ci_hi": 0.02,
          "p_boot": 0.03, "q_value": 0.045, "n_days": 336, "n_boot": 2000}
    cross = {"point_delta": 0.01, "ci_lo": -0.001, "ci_hi": 0.02,
             "p_boot": 0.06, "q_value": 0.09, "n_days": 336, "n_boot": 2000}
    neg = {"point_delta": -0.01, "ci_lo": -0.02, "ci_hi": -0.001,
           "p_boot": 0.03, "q_value": 0.045, "n_days": 336, "n_boot": 2000}
    zero = {"point_delta": 0.02, "ci_lo": 0.0, "ci_hi": 0.04,            # 下界正好=0 ⇒ 不算
            "p_boot": 0.02, "q_value": 0.03, "n_days": 336, "n_boot": 2000}
    assert mod.necessary_condition(ok) is True
    assert mod.necessary_condition(cross) is False
    assert mod.necessary_condition(neg) is False
    assert mod.necessary_condition(zero) is False, "CI 贴着 0 不算「不跳 0」"
    v = mod.verdict_text(cross)
    assert "证据不足" in v and "不切" in v
    assert "证伪" not in v, "CI 跳 0 只能写「证据不足」，写「证伪」是超额结论"


def test_bh_q_is_over_the_whole_comparison_set_not_the_best_arm() -> None:
    """q 值必须按「非基线臂全体」这个比较集算，并且每条都报出来（不得只报最小 p 的那个臂）。"""
    mod = _cmp_mod()
    res = {"A_minus_ctl": {"p_boot": 0.004}, "A2_minus_ctl": {"p_boot": 0.03},
           "I_minus_ctl": {"p_boot": 0.04}}
    out = mod.attach_bh_q(res, alpha=0.05)
    assert {k for k in out if k != "comparison_set"} == set(res), "三条都要有 q"
    assert out["comparison_set"] == 3
    keys = ("A_minus_ctl", "A2_minus_ctl", "I_minus_ctl")
    ps = [out[k]["p_boot"] for k in keys]
    qs = [out[k]["q_value"] for k in keys]
    assert all(0.0 <= q <= 1.0 for q in qs)
    assert all(q >= p for q, p in zip(qs, ps, strict=True)), \
        "q 不能小于 p（BH 是放大 p 去控 FDR，不是缩小）"
    # BH 定义：最小那条 q = p × m（m=比较集大小），且 q 随 p 单调不减
    order = np.argsort(ps)
    assert qs[next(iter(order))] == pytest.approx(min(ps) * 3), "最小 p 的 q 应是 p×m"
    assert all(qs[i] <= qs[j] for i, j in
               [(list(order)[a], list(order)[b]) for a in range(3) for b in range(a, 3)]), \
        f"q 必须随 p 单调不减：p={ps} q={qs}"
    # 单条时比较集是 1（q=p）；不能把「只算了一个臂」当成 3
    one = mod.attach_bh_q({"A_minus_ctl": {"p_boot": 0.03}}, alpha=0.05)
    assert one["comparison_set"] == 1 and one["A_minus_ctl"]["q_value"] == pytest.approx(0.03)


def test_both_label_tables_carry_their_own_denominators() -> None:
    """主表用 A2 标签、附表用旧标签；两套都必须把分母（行数、正样本数、天数）写出来。"""
    mod = _cmp_mod()
    snap = _snap(n_days=4, n_codes=10)
    rng = np.random.default_rng(21)
    snap["label_a2"] = (rng.random(len(snap)) < 0.3).astype(int)
    tables = mod.label_tables(snap=snap, unified_label_col="label_a2",
                              old_label_col="label")
    assert set(tables) == {"unified_a2", "old"}
    for name, t in tables.items():
        assert {"n_rows", "n_positive", "base_rate", "n_days"} <= set(t), (name, t)
        assert t["n_rows"] == 40, name
    col = mod.PRIMARY_LABEL_SOURCE
    assert mod.PRIMARY_ARM == "A2" and col == f"label_{mod.PRIMARY_ARM}", \
        "主判据的标签来源必须点名到某一臂，不能靠上下文"
    assert mod.UNIFIED_KEY == "unified_a2"
