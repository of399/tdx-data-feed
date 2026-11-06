"""P0-1 打板专用模型训练（次日涨停二分类，2026-10-18）。

背景：现有 vpnl_huber_stage1 是 Huber 回归（数日波段 PnL），与打板目标（次日涨停）
错配。本脚本训练"次日涨停概率"二分类模型。

标签：y = 1 当且仅当 close[t+1]/close[t] - 1 >= limit（T+1 收盘涨停，含一字板）。
特征：29 维（26 旧 + 3 新涨停特征）。

样本策略：正样本（次日涨停）全取；负样本每只股票随机抽 K 个非涨停日（K 按正负比
1:5 控制），避免 O(n²) 全量遍历 + 双重不平衡处理。

用法：
    venv/bin/python scripts/train_daban_model.py --limit 200   # 冒烟
    venv/bin/python scripts/train_daban_model.py               # 全量

═══════════════════════════════════════════════════════════════════════════════
ST 供给契约（B1，2026-11-05；缺陷取证 KI-DATA-008）：本脚本是**现役 daban 模型的标签
生产者**，所以它的阈值必须按信号日 T 取档，ST 状态按日来自
`data/st_history/st_events.parquet`（`tdxfeed/st_supply.py`）。三条硬约定：

1. **默认只读权威层**（`f10_ledger` + `block_gn` + `akshare_sz`）。推断层
   （`price_inferred`，占面板 96.4%）只能由 `--include-inferred` 显式并进来，
   且只在权威层判不出时生效。理由：静默把推断当标签供给 = 把 96.4% 的推断行洗成权威，
   下一轮没人读得出来，两臂对照（§13）也就失去意义。
2. **「无台账记录」不等于「非 ST」**。4,739/5,229 只（90.63%）的【特别处理】栏是
   「暂无数据」，深度 1–5 条不均。所以每次运行都打印**来源分布那一行**
   （权威 X 行 / 推断 Y 行 / 无法判定 Z 行），无法判定带实测原因，不折成 NaN 也不折成 0。
3. **面板不在场 ⇒ 响亮拒跑**（`PanelUnavailableError`）。绝不静默退回「不传日期」的旧行为——
   那等于把这个缺陷重新装回生产链，而且没人知道。守卫见
   `tests/test_limit_on_date_in_production.py`（生产闭包由 import 图算出，不由人写）。

⚠ B1（管道）与 B2（重训 + 双跑）都已落地，**但没切流**：B2 产的是
`d_signal_5d_model_daban_stfix_<日期>_<臂>.txt` 对照产物，现役
`data/double_up/d_signal_5d_model_daban.txt` 仍是在旧矩阵（dim1 恒 0.095/0.195、dim15 恒 0）
上训的那一只。**四臂（`ctl` / `A` / `A2` / `I`）全部按预登记判据进表，主判据是 `ctl` vs `A2`
这一对**——2026-11-05 21:21:41 曾收到一条「只跑两臂」的范围改派，送达时四臂已跑完（20:30 前）、
结论快照已定稿（21:20）⇒ **改派未按期生效，本批按四臂验收**（发送时序失误，不是执行偏差）。
`A`/`I` 是**已产出的灵敏度读数**：可以引用方向，不得替代主判据，也不得因为它们与 `A2` 接近
就合并成「修好了」。
在役推理侧 `scripts/predict.py` 也还没接（仍走 `is_st=None` 的未供给档）——
B2 任务书没有授权改它，所以 B1 时写的「重训之前必须先把它一起接上」没做成，
现在 **dim1/dim15 的训练口径与推理口径不一致**：这一格是记账上的错配而不是生产上的坏数
（现役模型与旧口径推理恰好相配），但**换现役模型必须与接线同批做**。
判定与适用边界见 `docs/research/2026-11-05-daban-stfix-retrain-arms.md`。
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tdxfeed"))

# KI-ATOMIC-004：模型/报告落位走原子写（predict 不会读到半截模型）
from _common import daily_dir  # noqa: E402

from tdxfeed import (  # noqa: E402
    adjusted,
    st_supply as sup,  # 臂表(ARM_PLAN/build_supply)的单源
)
from tdxfeed.atomic import (  # noqa: E402
    atomic_write_path,
    atomic_write_text,
    tmp_sibling,
)
from tdxfeed.daban.sector import load_industry_map  # noqa: E402
from tdxfeed.daban.sector_features import (  # noqa: E402
    EXTRA_FEATURE_COLS,
    build_lookup,
)
from tdxfeed.features_5d import (  # noqa: E402
    compute_window_features_extended,
    is_limit_up_ret,
    is_open_limit_up,
)
from tdxfeed.st_supply import (  # noqa: E402
    StSupply,
    TierTally,
    limit_series,
)

# 吞错计数（SEM002）：每处被吞掉的错误都计入统计，按函数名分桶，便于事后追溯失败率
_ERROR_COUNT: Counter[str] = Counter()

PARQUET = daily_dir()
OUT_DIR = ROOT / "data" / "double_up"
# 现役产物文件名（B2 的「绝不覆盖现役模型」这条要把它们当**判据**用，不能只靠注释）
LIVE_MODEL_NAME = "d_signal_5d_model_daban.txt"
LIVE_REPORT_NAME = "daban_train_report.md"
MODEL_OUT = OUT_DIR / LIVE_MODEL_NAME
REPORT_OUT = OUT_DIR / LIVE_REPORT_NAME

SPLIT_DATE = "2025-06-01"
NEG_RATIO = 5  # 负:正 采样比
RANDOM_SEED = 42
NUM_BOOST_ROUND = 500
EARLY_STOPPING_ROUNDS = 30

# 超参提到模块层：`invariant_lines()` 打印的那一组必须**就是** `train()` 交给
# `lgb.Dataset`/`lgb.train` 的那一份。留在函数体里就是手抄字面量——四臂「只差供给」
# 这句话会失去证据（判据见 tests/test_daban_stfix_arms.py::test_invariant_lines_come_from_the_objects_that_run）。
LGB_PARAMS: dict = {
    "objective": "binary",
    "metric": "auc",
    "learning_rate": 0.03,
    "num_leaves": 31,
    "feature_fraction": 0.8,
    "bagging_fraction": 0.8,
    "bagging_freq": 5,
    "min_data_in_leaf": 20,
    # scale_pos_weight 每次按样本现算，不放这里（放这里就变成手抄基线）
    "verbosity": -1,
}


class ProductPathError(RuntimeError):
    """B2 的产物路径不合规（可能覆盖现役模型 / 跑出借不出是哪一臂的文件名）。"""


def assert_not_live(path: Path, *, arm: str | None) -> Path:
    """带了 `--arm` 就不许落在现役模型/报告上；不带 --arm 时保持生产入口既有行为。"""
    resolved = Path(path).resolve()
    live_model = (path.parent / LIVE_MODEL_NAME).resolve()
    live_report = (path.parent / LIVE_REPORT_NAME).resolve()
    if arm is not None and resolved in (live_model, live_report):
        raise ProductPathError(
            f"臂 {arm} 的产物解析到现役文件 {resolved} ⇒ 本批「绝不覆盖现役模型」"
            "被破坏（切流是用户的决定，不是一次训练的副作用）")
    return Path(path)


def resolve_product_paths(arm: str | None, *, tag: str,
                          out_dir: Path | None = None) -> tuple[Path, Path]:
    """(模型, 报告) 两个产物路径。**臂存在就必须把臂名写进文件名**。

    命名规则（B2 任务书第 6 条）：`--arm A2 --tag stfix_20261105_A2` ⇒
    `data/double_up/d_signal_5d_model_daban_stfix_20261105_A2.txt`。
    三条硬拒绝：① 有臂但 tag 空（会算出现役路径）；② tag 不带 `_<arm>` 后缀
    （四臂产物从文件名上分不出来，事后无法追溯哪份是哪臂）；③ tag 里有路径分隔符。
    """
    base = OUT_DIR if out_dir is None else Path(out_dir)
    if arm is None:
        if not tag:
            return base / LIVE_MODEL_NAME, base / LIVE_REPORT_NAME
        return (base / f"d_signal_5d_model_daban_{tag}.txt",
                base / f"daban_train_report_{tag}.md")
    if arm not in sup.ARMS:
        raise ProductPathError(f"未知臂 {arm!r} ⇒ 词表 = {list(sup.ARMS)}")
    if not tag:
        raise ProductPathError(
            f"--arm {arm} 必须配一个非空 --tag：留空会解析到现役模型 "
            f"{base / LIVE_MODEL_NAME}（本批不许覆盖现役）")
    if "/" in tag or "\\" in tag:
        raise ProductPathError(f"--tag {tag!r} 里不许有路径分隔符：产物必须留在 {base}")
    if not tag.endswith(f"_{arm}"):
        raise ProductPathError(
            f"--tag {tag!r} 必须以 _{arm} 结尾，否则四臂产物从文件名上认不出是哪一臂")
    model_out = base / f"d_signal_5d_model_daban_{tag}.txt"
    report_out = base / f"daban_train_report_{tag}.md"
    return (assert_not_live(model_out, arm=arm),
            assert_not_live(report_out, arm=arm))


def check_arm_flags(arm: str | None, *, include_inferred: bool) -> None:
    """`--arm` 与 `--include-inferred` 是同一件事的两个真相源 ⇒ 同时给就拒跑。"""
    if arm is not None and include_inferred:
        raise ValueError(
            f"--arm {arm} 已经决定了「读哪几层 + 是否并推断」（见 st_supply.ARM_PLAN），"
            "不要再加 --include-inferred：两个开关同时生效时，产物到底属于哪一臂没人说得清")


def load_st_supply_for_arm(arm: str | None, *, panel_path: Path | None = None):
    """按臂建供给；`ctl` 返回 None（这是**设计**，不是面板缺席）。"""
    if arm is None:
        return load_st_supply(panel_path=panel_path)
    return sup.build_supply(arm, panel_path=panel_path)


def invariant_lines(arm: str | None) -> list[str]:
    """每个实跑都要打印的三组不变量：超参 / 切分与采样 / 特征管线。

    这四臂**只许差在 is_st 与 limit 的供给上**，所以这三组必须逐字相同并且**当场打印**。
    数字全部从真正被执行的对象读出来（`LGB_PARAMS`、`SPLIT_DATE`、`NEG_RATIO`、
    `compute_window_features_extended` 的列集），手抄一份就成了「报告说不变、代码里改了」。
    """
    from tdxfeed.features_5d import compute_window_features_extended

    probe = pd.DataFrame({
        "date": pd.date_range("2024-01-02", periods=12, freq="B"),
        "open": np.linspace(10, 11, 12), "high": np.linspace(10, 11, 12) + .1,
        "low": np.linspace(10, 11, 12) - .1, "close": np.linspace(10, 11, 12),
        "volume": np.full(12, 1e6), "amount": np.full(12, 1e7)})
    n_feat = len(compute_window_features_extended(probe, "600000", sig_idx=11,
                                                  is_st=False))
    hyper = " ".join(f"{k}={v}" for k, v in sorted(LGB_PARAMS.items()))
    plan = sup.ARM_PLAN.get(arm) if arm else None
    return [
        f"[不变量·超参] {hyper} num_boost_round={NUM_BOOST_ROUND} "
        f"early_stopping={EARLY_STOPPING_ROUNDS} seed={RANDOM_SEED}",
        f"[不变量·切分与采样] SPLIT_DATE={SPLIT_DATE} NEG_RATIO={NEG_RATIO} "
        f"RANDOM_SEED={RANDOM_SEED} 抽样=正样本全取/负样本每票 {NEG_RATIO}×正样本数",
        f"[不变量·特征管线] compute_window_features_extended {n_feat} 维（含 dim1 limit、"
        f"dim15 is_st），price_basis=raw sample_space=all extra_features=none",
        f"[臂 {arm or '未指定'}] {sup.ARM_NOTES.get(arm, '默认生产口径（权威三层全读）')}"
        + ("" if plan is None else f"｜sources={list(plan['sources'])} "
                                  f"include_inferred={plan['include_inferred']}"),
    ]


def load_st_supply(*, include_inferred: bool = False,
                   panel_path: Path | None = None) -> StSupply:
    """按日 ST 供给的装载入口；面板不在场就抛（拒跑），没有「不传日期」这条路。

    `panel_path=None` 走 `tdxfeed.paths.st_event_path()`（路径单源）。测试里传显式路径
    把「面板缺失」演出来，不改真实目录。
    """
    return StSupply.load(include_inferred=include_inferred, path=panel_path)


def limit_series_for_stock(df: pd.DataFrame, code: str,
                           supply: StSupply | None = None
                           ) -> tuple[list[float], list[str], list[bool]]:
    """逐信号日的 (判定阈值, 档位, ST 布尔)。委托 `tdxfeed.st_supply.limit_series`。

    轴与 `scripts/audit_daban_st_tier_pollution.py` 的 52,919 一致：阈值挂 T（信号日），
    不挂 T+1。`supply=None` 时返回恒值列 + 全 `未供给` 档，逐格等于改动前那种
    「只传代码、不传日期」的旧写法（这是「研究脚本可复现」与「生产必须供给」之间的那道接缝）。
    """
    return limit_series(code, list(df["date"]), supply)


def supplied_rows_line(tally: TierTally) -> str:
    """来源分布那一行（权威 / 推断 / 无法判定），由真正供给出去的样本行记账。"""
    return tally.line()


def is_stock_code(code: str) -> bool:
    """仅沪深 A 股个股（60/68 沪, 00/30 深），剔除 ETF/债/指数/北交所。"""
    s = code[2:] if code[:2] in ("sh", "sz") else code
    if s.startswith(("920", "8")):
        return False  # 北交所
    return s.startswith(("60", "68", "00", "30"))


def iter_stock_files(limit: int = 0) -> list[tuple[str, Path]]:
    out = []
    for fp in sorted(PARQUET.glob("*.parquet")):
        name = fp.stem
        if ".bak" in name:
            continue
        code = name[2:] if name[:2] in ("sh", "sz", "bj") else name
        if not is_stock_code(code):
            continue
        out.append((code, fp))
        if limit and len(out) >= limit:
            break
    return out


def build_dataset(
    limit: int = 0,
    exclude_open_limit: bool = False,
    extra_lookup=None,
    industry_map: dict | None = None,
    sample_space: str = "all",
    price_basis: str = "raw",
    exclude_symbols: set[str] | None = None,
    hfq_skipped_out: Path | None = None,
    st_supply: StSupply | None = None,
    tally: TierTally | None = None,
) -> pd.DataFrame:
    """建样本矩阵。`st_supply` 给定时阈值与 dim15 按信号日供给（B1）。

    `st_supply=None` 保留给研究/基准脚本复现旧口径；`main()` 走的是「必须供给」那条路，
    面板缺失直接 `PanelUnavailableError` 拒跑。`tally` 按**真正落进矩阵的行**记档位，
    所以它是样本行分母，不是查询次数分母。
    """
    files = iter_stock_files(limit)
    print(f"遍历 {len(files)} 只股票 ...")
    if exclude_open_limit:
        print("  [开关] exclude_open_limit=True -> 剔除 T 日开盘即涨停(不可买入)样本")
    rng = random.Random(RANDOM_SEED)
    rows = []
    hfq_skipped: list[str] = []
    n_pos = n_neg = 0
    n_excluded = 0
    for i, (code, fp) in enumerate(files):
        symbol = fp.stem                     # sh600000：排除与跳过都按它记账
        if exclude_symbols and symbol in exclude_symbols:
            continue
        try:
            df = pd.read_parquet(fp)
        except Exception:
            _ERROR_COUNT['build_dataset'] += 1
            continue
        if len(df) < 30:
            continue
        df = df.sort_values("date").reset_index(drop=True)
        frame_hfq = None
        if price_basis == "hfq":
            f_hfq = adjusted.frame(symbol, kind="hfq", strict=False)
            if f_hfq.attrs.get("factor_cov", 0.0) < adjusted.MIN_FACTOR_COV:
                hfq_skipped.append(symbol)       # 缺因子 / 同码错配 → 整只跳过
                continue
            f_hfq = f_hfq.sort_values("date").reset_index(drop=True)
            if len(f_hfq) != len(df) or not (
                    pd.to_datetime(f_hfq["date"]).reset_index(drop=True)
                    .equals(pd.to_datetime(df["date"]))):
                hfq_skipped.append(symbol)       # 两帧不同日就不能用同一 sig_idx
                continue
            frame_hfq = f_hfq
        # B1：阈值/ST 状态按信号日逐行供给（并轨前的主板 ST 行取 5% 档）。
        # 两只数只对内核取一次（pre/post），按日选，所以 16.7M 行不是 16.7M 次调用。
        limits, tiers, st_flags = limit_series_for_stock(df, code, st_supply)
        close = df["close"].values
        openp = df["open"].values
        n = len(df)

        # 先定位正样本（次日涨停）sig_idx
        pos_idx = []
        neg_idx = []
        for sig_idx in range(4, n - 1):
            cur_close = close[sig_idx]
            if cur_close <= 0:
                continue
            limit_val = limits[sig_idx]      # 该信号日自己的判定阈值（不是全历史一个数）
            if sample_space == "limit_up":
                # 只保留 T 日封板的样本（打板可交易集）；判定口径与正样本一致
                prev_c_sig = close[sig_idx - 1]
                if prev_c_sig <= 0:
                    continue
                if not is_limit_up_ret(cur_close / prev_c_sig - 1.0, limit_val):
                    continue
            # 审计 P1-5 补丁 (2026-10-18): 剔除不可交易样本。
            # T 日(信号日/买入日)开盘价已达涨停价 -> 一字或准一字, 打板者无法买入,
            # 该样本实盘不可执行。此类样本既不作正样本也不作负样本, 直接剔除。
            if exclude_open_limit:
                prev_c = close[sig_idx - 1]
                if is_open_limit_up(openp[sig_idx], prev_c, limit_val):
                    n_excluded += 1
                    continue
            ret_next = close[sig_idx + 1] / cur_close - 1.0
            if is_limit_up_ret(ret_next, limit_val):
                pos_idx.append(sig_idx)
            else:
                neg_idx.append(sig_idx)
        # 负样本抽样（每只股票最多 NEG_RATIO * len(pos) 个，不足则全取）
        n_neg_pick = max(1, len(pos_idx) * NEG_RATIO) if pos_idx else NEG_RATIO
        if len(neg_idx) > n_neg_pick:
            neg_idx = rng.sample(neg_idx, n_neg_pick)

        for sig_idx in pos_idx + neg_idx:
            feat = compute_window_features_extended(df, code, sig_idx=sig_idx,
                                                    frame_hfq=frame_hfq,
                                                    is_st=st_flags[sig_idx])
            if feat is None:
                continue
            if tally is not None:
                tally.add(tiers[sig_idx])     # 按落进矩阵的行记档（样本行分母）
            label = 1 if sig_idx in pos_idx else 0
            if extra_lookup is not None:
                # 阶段3：板块特征（lag=1，与推理侧同源；lag=0 会造成推理侧取不到 → 泄漏）
                sig_date = str(df["date"].iloc[sig_idx])[:10].replace("-", "")
                ind = (industry_map or {}).get(code, "")
                row = extra_lookup.get(ind, sig_date, lag=1)
                if row is None:
                    feat.update({c: 0.0 for c in EXTRA_FEATURE_COLS})
                else:
                    feat.update({c: row.get(c, 0.0) for c in EXTRA_FEATURE_COLS})
            rows.append({"code": code, "date": str(df["date"].iloc[sig_idx])[:10],
                         "label": label, **feat})
        n_pos += len(pos_idx)
        n_neg += len(neg_idx)
        if (i + 1) % 300 == 0:
            print(f"  {i+1}/{len(files)} 正 {n_pos} 负 {n_neg}")
    print(f"完成: 正样本 {n_pos}, 负样本 {n_neg}")
    if tally is not None:
        for _line in tally.detail():
            print(f"       {_line}")
        print(f"       供给层：{st_supply.describe() if st_supply else '未供给（全部行按现行口径）'}")
    if exclude_open_limit:
        print(f"       (剔除 T 日开盘即涨停样本 {n_excluded} 条)")
    if hfq_skipped_out is not None:
        atomic_write_text(
            hfq_skipped_out,
            json.dumps({"symbols": sorted(set(hfq_skipped))}), encoding="utf-8")
        print(f"       (hfq 覆盖不足跳过 {len(set(hfq_skipped))} 只 -> {hfq_skipped_out})")
    return pd.DataFrame(rows)


def train(df: pd.DataFrame, model_out: Path | None = None) -> dict:
    df = df.sort_values("date").reset_index(drop=True)
    train_df = df[df["date"] < SPLIT_DATE]
    test_df = df[df["date"] >= SPLIT_DATE]
    print(f"train {len(train_df)} / test {len(test_df)} (切分 {SPLIT_DATE})")
    model_out = MODEL_OUT if model_out is None else model_out

    feat_cols = [c for c in df.columns if c not in ("code", "date", "label")]
    pos = int(train_df["label"].sum())
    neg = len(train_df) - pos
    scale = neg / max(pos, 1)

    dtr = lgb.Dataset(train_df[feat_cols], train_df["label"])
    dte = lgb.Dataset(test_df[feat_cols], test_df["label"], reference=dtr)

    params = dict(LGB_PARAMS)
    params["scale_pos_weight"] = scale
    params["seed"] = RANDOM_SEED
    print(f"训练 LightGBM (scale_pos_weight={scale:.1f}) ...")
    model = lgb.train(params, dtr, num_boost_round=NUM_BOOST_ROUND,
                      valid_sets=[dte], valid_names=["test"],
                      callbacks=[lgb.early_stopping(EARLY_STOPPING_ROUNDS,
                                                    verbose=False)])
    # KI-ATOMIC-004：模型先写临时文件再原子落位 —— predict 绝不会读到半截模型，
    # 且覆盖现役模型前自动留一份带时间戳的备份。
    tmp_model = tmp_sibling(model_out)
    try:
        model.save_model(str(tmp_model), num_iteration=model.best_iteration)
        atomic_write_path(tmp_model, model_out, backup=True)
    finally:
        if tmp_model.exists():
            tmp_model.unlink(missing_ok=True)
    print(f"模型保存: {model_out} (best_iter={model.best_iteration})")

    imp = sorted(zip(feat_cols, model.feature_importance("gain"), strict=True),
                 key=lambda x: -x[1])
    return {"feat_cols": feat_cols, "imp": imp,
            "train_n": len(train_df), "test_n": len(test_df),
            "scale": scale, "auc": model.best_score["test"]["auc"],
            "best_iter": model.best_iteration}


def write_report(r: dict, report_out: Path | None = None, basis: str = "raw",
                 arm: str | None = None, extra_lines: list[str] | None = None) -> None:
    """训练报告。B2 起：臂名与「三组不变量 + 来源分布」必须写进产物本身。

    只留模型文件不留「这一臂是什么」，一周后没人能把产物跟臂对上；推断层的行如果
    不在产物里可辨，`I` 臂会被后来人当权威口径复用。
    """
    report_out = REPORT_OUT if report_out is None else report_out
    lines = [f"# 打板专用模型训练报告 ({datetime.now():%Y-%m-%d %H:%M})", ""]
    if arm is not None:
        lines += [f"- **臂**：`{arm}` —— {sup.ARM_NOTES.get(arm, '')}",
                  "- 本产物是对照实验产物，**不是现役模型**，切流需用户拍板", ""]
    lines += [f"- 样本: train {r['train_n']} / test {r['test_n']} (切分 {SPLIT_DATE})",
              f"- 价格口径: {basis}",
              f"- scale_pos_weight: {r['scale']:.1f}",
              f"- best_iter: {r['best_iter']}",
              f"- **OOS AUC: {r['auc']:.4f}**", ""]
    if extra_lines:
        lines += ["## 不变量与供给（实跑时打印的原文）", ""]
        lines += [f"- {ln}" for ln in extra_lines]
        lines += [""]
    lines += ["## 特征重要性 Top 15", "", "| 特征 | gain |", "|---|---:|"]
    for name, g in r["imp"][:15]:
        lines.append(f"| {name} | {g:.0f} |")
    atomic_write_text(report_out, "\n".join(lines), encoding="utf-8")
    print(f"报告: {report_out}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="抽样股票数(冒烟)")
    ap.add_argument("--exclude-open-limit", action="store_true",
                    help="剔除 T 日开盘即涨停(不可买入)样本 [默认关, 保持可复现]")
    ap.add_argument(
        "--sample-space",
        choices=["all", "limit_up"],
        default="all",
        help="训练样本空间：all=全部交易日(现役口径)；limit_up=只取T日封板样本(打板可交易集)",
    )
    ap.add_argument(
        "--extra-features",
        choices=["none", "sec_lag1"],
        default="none",
        help="附加特征：sec_lag1 = 昨日板块强度/涨幅/涨停家数（阶段3，与推理侧同源）",
    )
    ap.add_argument("--tag", default="",
                    help="输出文件后缀, 如 'tradable' -> d_signal_5d_model_daban_tradable.txt "
                         "(留空=覆盖现役模型, 仅在确认切换时才应使用)")
    ap.add_argument("--price-basis", choices=["raw", "hfq"], default="raw")
    ap.add_argument("--exclude-symbols-file", default="",
                    help='JSON {"symbols": [...]}；这些 symbol 在抽样前整只跳过（双跑对齐）')
    ap.add_argument("--hfq-skipped-out", default="",
                    help='hfq 模式下覆盖率不足被跳过的 symbol 写到 {"symbols": [...]}')
    ap.add_argument("--include-inferred", action="store_true",
                    help="并读面板的推断层（price_inferred，占面板 96.4%% 的行，"
                         "与台账互证 precision≈0.8）。**默认关**：只读权威三层。"
                         "与 --arm 互斥（臂已经决定了读哪几层）")
    ap.add_argument("--arm", choices=list(sup.ARMS), default=None,
                    help="B2 四臂对照：ctl=ST 档强制关（归因基线，等价于改动前旧口径）；"
                         "A=权威层只读 f10_ledger+block_gn；A2=权威三层并读；"
                         "I=A2+price_inferred。给了 --arm 就必须给以 _<臂名> 结尾的 --tag，"
                         "产物不可能落在现役模型上")
    args = ap.parse_args()

    check_arm_flags(args.arm, include_inferred=args.include_inferred)
    # 输出路径先解析再干活：命名不合规要在**任何计算发生之前**拒掉（任务书第 6 条）
    model_out, report_out = resolve_product_paths(args.arm, tag=args.tag)

    # 三组不变量逐臂打印：四臂只许差在 is_st/limit 的供给上，这一行就是那句话的证据
    inv = invariant_lines(args.arm)
    for ln in inv:
        print(ln)

    # B1 硬约定：阈值必须按日供给，所以面板是**前置条件**而不是可选开关。
    # 面板不在场 ⇒ StSupply.load 抛 PanelUnavailableError，训练直接失败（不静默退回旧口径）。
    # 唯一合法的例外是 --arm ctl：它是「故意把 ST 档关掉」的归因基线，不是面板缺席。
    if args.arm == sup.CTL_ARM:
        st_supply = None
        print("[供给] ctl 臂：不建供给 ⇒ 全部行按现行档（非 ST）供给，"
              "逐值等于改动前「只传代码不传日期」的旧口径（等价性由 "
              "tests/test_st_supply_in_features.py::test_ctl_arm_is_exactly_the_old_scalar_caliber 钉）")
    else:
        st_supply = (load_st_supply(include_inferred=args.include_inferred)
                     if args.arm is None else load_st_supply_for_arm(args.arm))
        print(f"[供给] {st_supply.describe()}")
        if st_supply.include_inferred:
            print("       ⚠ 本次并读了推断层（price_inferred）：产物不是权威口径，"
                  "只用于两臂/四臂对照，别把它当修复后的正式矩阵")
    tally = TierTally()

    extra_lookup = None
    industry_map = None
    if args.extra_features == "sec_lag1":
        extra_lookup = build_lookup()
        industry_map = load_industry_map()
        if extra_lookup is None:
            print("❌ 板块特征缺失：先跑 scripts/daban_sector_features.py", file=sys.stderr)
            return 1
        print(f"[开关] extra_features=sec_lag1 → 追加 {len(EXTRA_FEATURE_COLS)} 维板块特征（lag=1）")

    exclude_symbols = set()
    if args.exclude_symbols_file:
        exclude_symbols = set(json.loads(
            Path(args.exclude_symbols_file).read_text(encoding="utf-8"))["symbols"])
    df = build_dataset(
        args.limit,
        exclude_open_limit=args.exclude_open_limit,
        extra_lookup=extra_lookup,
        industry_map=industry_map,
        sample_space=args.sample_space,
        price_basis=args.price_basis,
        exclude_symbols=exclude_symbols,
        hfq_skipped_out=(Path(args.hfq_skipped_out) if args.hfq_skipped_out else None),
        st_supply=st_supply,
        tally=tally,
    )
    if df.empty:
        print("❌ 无样本", file=sys.stderr)
        return 1
    r = train(df, model_out=model_out)
    write_report(r, report_out=report_out, basis=args.price_basis,
                 arm=args.arm, extra_lines=[*inv, tally.line()])
    print(f"\n=== ✅ 完成 === OOS AUC={r['auc']:.4f}")
    print(f"  产物: {model_out.name} / {report_out.name}")
    if args.arm is not None:
        print("  ⚠ 对照产物，不切流：现役 d_signal_5d_model_daban.txt 未被本次运行触碰")
    else:
        print("  验收: AUC > 0.60 才考虑切换实盘(替代 vpnl_huber_stage1)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
