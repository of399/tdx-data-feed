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
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from datetime import datetime
from pathlib import Path

import lightgbm as lgb
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tdxfeed"))

from _common import daily_dir  # noqa: E402

from tdxfeed import adjusted  # noqa: E402
from tdxfeed.daban.sector import load_industry_map  # noqa: E402
from tdxfeed.daban.sector_features import (  # noqa: E402
    EXTRA_FEATURE_COLS,
    build_lookup,
)
from tdxfeed.features_5d import (  # noqa: E402
    compute_window_features_extended,
    get_limit,
    is_limit_up_ret,
    is_open_limit_up,
)

PARQUET = daily_dir()
OUT_DIR = ROOT / "data" / "double_up"
MODEL_OUT = OUT_DIR / "d_signal_5d_model_daban.txt"
REPORT_OUT = OUT_DIR / "daban_train_report.md"

SPLIT_DATE = "2025-06-01"
NEG_RATIO = 5  # 负:正 采样比
RANDOM_SEED = 42


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
) -> pd.DataFrame:
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
        limit_val = get_limit(code)
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
                                                    frame_hfq=frame_hfq)
            if feat is None:
                continue
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
    if exclude_open_limit:
        print(f"       (剔除 T 日开盘即涨停样本 {n_excluded} 条)")
    if hfq_skipped_out is not None:
        hfq_skipped_out.write_text(
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

    params = {
        "objective": "binary",
        "metric": "auc",
        "learning_rate": 0.03,
        "num_leaves": 31,
        "feature_fraction": 0.8,
        "bagging_fraction": 0.8,
        "bagging_freq": 5,
        "min_data_in_leaf": 20,
        "scale_pos_weight": scale,
        "seed": RANDOM_SEED,
        "verbosity": -1,
    }
    print(f"训练 LightGBM (scale_pos_weight={scale:.1f}) ...")
    model = lgb.train(params, dtr, num_boost_round=500,
                      valid_sets=[dte], valid_names=["test"],
                      callbacks=[lgb.early_stopping(30, verbose=False)])
    model.save_model(str(model_out), num_iteration=model.best_iteration)
    print(f"模型保存: {model_out} (best_iter={model.best_iteration})")

    imp = sorted(zip(feat_cols, model.feature_importance("gain"), strict=True),
                 key=lambda x: -x[1])
    return {"feat_cols": feat_cols, "imp": imp,
            "train_n": len(train_df), "test_n": len(test_df),
            "scale": scale, "auc": model.best_score["test"]["auc"],
            "best_iter": model.best_iteration}


def write_report(r: dict, report_out: Path | None = None, basis: str = "raw") -> None:
    report_out = REPORT_OUT if report_out is None else report_out
    lines = [f"# 打板专用模型训练报告 ({datetime.now():%Y-%m-%d %H:%M})", "",
             f"- 样本: train {r['train_n']} / test {r['test_n']} (切分 {SPLIT_DATE})",
             f"- 价格口径: {basis}",
             f"- scale_pos_weight: {r['scale']:.1f}",
             f"- best_iter: {r['best_iter']}",
             f"- **OOS AUC: {r['auc']:.4f}**", "",
             "## 特征重要性 Top 15", "", "| 特征 | gain |", "|---|---:|"]
    for name, g in r["imp"][:15]:
        lines.append(f"| {name} | {g:.0f} |")
    report_out.write_text("\n".join(lines), encoding="utf-8")
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
    args = ap.parse_args()

    # 输出路径：默认走现役路径(保持可复现)；带 --tag 则独立输出，保护对照组
    model_out = MODEL_OUT if not args.tag else OUT_DIR / f"d_signal_5d_model_daban_{args.tag}.txt"
    report_out = REPORT_OUT if not args.tag else OUT_DIR / f"daban_train_report_{args.tag}.md"

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
    )
    if df.empty:
        print("❌ 无样本", file=sys.stderr)
        return 1
    r = train(df, model_out=model_out)
    write_report(r, report_out=report_out, basis=args.price_basis)
    print(f"\n=== ✅ 完成 === OOS AUC={r['auc']:.4f}")
    print("  验收: AUC > 0.60 才考虑切换实盘(替代 vpnl_huber_stage1)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
