"""用当前内核口径重算 d_signal_5d_features_vpnl.csv 的 hypothetical_pnl 列。

只换标签：特征列、(code, signal_date) 集合、列序都不动。
不使用 train_pnl_proxy.main()——它的特征源 d_signal_5d_features_v9.csv 已不存在，
且存在 90% 非空就复用旧列的短路，二者都会让「重算」变成空操作。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import pandas as pd
from _common import daily_files  # noqa: E402
from train_pnl_proxy import OUT_DATA, attach_pnl

OUT_HFQ = OUT_DATA.with_name("d_signal_5d_features_vpnl_hfq.csv")


def main(argv=None) -> int:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--price-basis", choices=["raw", "hfq"], default="raw")
    ap.add_argument("--src", default=str(OUT_DATA))
    ap.add_argument("--out", default="")
    # hfq 口径下缺因子的票会合法地少标签；差值必须显式申报并写进报告，
    # 不能靠悄悄放宽闸门（0 是默认，任何非零都要人眼确认是哪些票）。
    ap.add_argument("--expect-drop", type=int, default=0)
    # 全新宇宙的特征表还没有 hypothetical_pnl 列。没有上一版可对照，闸门改用
    # 「有日线行」的非空率下限；同时强制显式 --out，避免默认路径覆盖旧标签表。
    ap.add_argument("--init", action="store_true",
                    help="源表无 hypothetical_pnl（全新宇宙）：插入空列，按非空率下限设闸门")
    args = ap.parse_args(argv)
    src = Path(args.src)
    if args.init and not args.out:
        print("❌ --init 必须显式给 --out（默认路径是已有标签表，不能覆盖）", file=sys.stderr)
        return 1
    out = Path(args.out) if args.out else (
        OUT_HFQ if args.price_basis == "hfq" else OUT_DATA)

    # round_trip 解析：默认解析器会让 ~100 个浮点单元格读偏 1 ULP，重写后特征列文本
    # 就不再与快照逐位相同，违反「只换标签列」的契约（守卫 test_non_label_columns_are_untouched）
    # code 钉字符串：标识符不参与类型推断，否则全数字子集会被读成 int 而与文件名失配。
    df = pd.read_csv(src, float_precision="round_trip", dtype={"code": str})
    # 写前闸门：attach_pnl 逐行吞异常返 None，没有这道检查，一次空跑就会 exit 0
    # 覆盖掉唯一产物（data/ 不在 git 里）。有上一版时对照它的非空数；
    # 全新宇宙没有上一版，改看有日线行上的非空率下限。
    have = {p.stem for p in daily_files()}
    rows_with_daily = int(df["code"].isin(have).sum())
    expected = None
    if "hypothetical_pnl" not in df.columns:
        if not args.init:
            print(f"❌ 缺 hypothetical_pnl 列: {src}（全新宇宙请加 --init）", file=sys.stderr)
            return 1
        df["hypothetical_pnl"] = pd.NA
    else:
        expected = int(df.loc[df["code"].isin(have), "hypothetical_pnl"].notna().sum())
    features = df.drop(columns=["hypothetical_pnl"])
    renamed = attach_pnl(features, max_workers=8, price_basis=args.price_basis)
    valid = int(renamed["hypothetical_pnl"].notna().sum())
    if expected is None:
        min_valid = int(rows_with_daily * 0.95)
        gate_msg = f"下限 {min_valid}（有日线 {rows_with_daily} 的 95%）"
    else:
        min_valid = expected - args.expect_drop
        gate_msg = f"上一版有日线行 {expected}（已申报允许减少 {args.expect_drop}）"
    if valid < min_valid:
        print(f"❌ 重算后非空 {valid} < {gate_msg}，拒写 {out}", file=sys.stderr)
        return 1
    if expected is not None and valid != expected:
        print(f"⚠ 非空 {valid} 比上一版 {expected} 少 {expected - valid}"
              f"（已申报 {args.expect_drop}）：确认都是缺因子的票", file=sys.stderr)
    tmp = out.with_name(out.name + ".tmp")
    try:
        renamed.to_csv(tmp, index=False)
        tmp.replace(out)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    print(f"RECOMPUTE_DONE rows={len(renamed)} valid={valid} basis={args.price_basis} out={out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
