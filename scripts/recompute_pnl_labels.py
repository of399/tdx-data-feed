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
    df = pd.read_csv(OUT_DATA)
    if "hypothetical_pnl" not in df.columns:
        print(f"❌ 缺 hypothetical_pnl 列: {OUT_DATA}", file=sys.stderr)
        return 1
    features = df.drop(columns=["hypothetical_pnl"])
    renamed = attach_pnl(features, max_workers=8)
    tmp = OUT_DATA.with_name(OUT_DATA.name + ".tmp")
    renamed.to_csv(tmp, index=False)
    tmp.replace(OUT_DATA)
    valid = int(renamed["hypothetical_pnl"].notna().sum())
    print(f"RECOMPUTE_DONE rows={len(renamed)} valid={valid} out={OUT_DATA}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
