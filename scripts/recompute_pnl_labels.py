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
    args = ap.parse_args(argv)
    src = Path(args.src)
    out = Path(args.out) if args.out else (
        OUT_HFQ if args.price_basis == "hfq" else OUT_DATA)

    # round_trip 解析：默认解析器会让 ~100 个浮点单元格读偏 1 ULP，重写后特征列文本
    # 就不再与快照逐位相同，违反「只换标签列」的契约（守卫 test_non_label_columns_are_untouched）
    # code 钉字符串：标识符不参与类型推断，否则全数字子集会被读成 int 而与文件名失配。
    df = pd.read_csv(src, float_precision="round_trip", dtype={"code": str})
    if "hypothetical_pnl" not in df.columns:
        print(f"❌ 缺 hypothetical_pnl 列: {src}", file=sys.stderr)
        return 1
    # 写前闸门：attach_pnl 逐行吞异常返 None，没有这道检查，一次空跑就会 exit 0
    # 覆盖掉唯一产物（data/ 不在 git 里）。期望值取自上一版在有日线行上的非空数。
    have = {p.stem for p in daily_files()}
    expected = int(df.loc[df["code"].isin(have), "hypothetical_pnl"].notna().sum())
    features = df.drop(columns=["hypothetical_pnl"])
    renamed = attach_pnl(features, max_workers=8, price_basis=args.price_basis)
    valid = int(renamed["hypothetical_pnl"].notna().sum())
    if valid < expected - args.expect_drop:
        print(f"❌ 重算后非空 {valid} < 上一版有日线行的 {expected}"
              f"（已申报允许减少 {args.expect_drop}），拒写 {out}", file=sys.stderr)
        return 1
    if valid != expected:
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
