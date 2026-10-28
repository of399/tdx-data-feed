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

from train_pnl_proxy import OUT_DATA, attach_pnl


def main() -> int:
    # round_trip 解析：默认解析器会让 ~100 个浮点单元格读偏 1 ULP，重写后特征列文本
    # 就不再与快照逐位相同，违反「只换标签列」的契约（守卫 test_non_label_columns_are_untouched）
    df = pd.read_csv(OUT_DATA, float_precision="round_trip")
    if "hypothetical_pnl" not in df.columns:
        print(f"❌ 缺 hypothetical_pnl 列: {OUT_DATA}", file=sys.stderr)
        return 1
    # 写前闸门：attach_pnl 逐行吞异常返 None，没有这道检查，一次空跑就会 exit 0
    # 覆盖掉唯一产物（data/ 不在 git 里）。期望值取自上一版在有日线行上的非空数。
    have = {p.stem for p in (ROOT / "data" / "parquet" / "daily").glob("*.parquet")}
    expected = int(df.loc[df["code"].isin(have), "hypothetical_pnl"].notna().sum())
    features = df.drop(columns=["hypothetical_pnl"])
    renamed = attach_pnl(features, max_workers=8)
    valid = int(renamed["hypothetical_pnl"].notna().sum())
    if valid < expected:
        print(f"❌ 重算后非空 {valid} < 上一版有日线行的 {expected}，拒写 {OUT_DATA}",
              file=sys.stderr)
        return 1
    tmp = OUT_DATA.with_name(OUT_DATA.name + ".tmp")
    try:
        renamed.to_csv(tmp, index=False)
        tmp.replace(OUT_DATA)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    print(f"RECOMPUTE_DONE rows={len(renamed)} valid={valid} out={OUT_DATA}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
