"""data/hfq_factor 的下跳事件白名单：源漂移的绊线，不是修复器。

spec §1.2：这 12 次下跳不修（000002 是一日凹坑，整段缩放会把回补变成新的假跳），
只钉住清单——新增一个即红，先人眼复核是源修订还是增量写坏了。

范围：断言只覆盖 `date >= 2020-01-01`（在役表原有起点，白名单就是在这一段量的）。
回填会把 2020 年前的历史接进来，那一段的下跳没有基线可比，**只记录不断言**——
清单落在 Task 9 的 `data/hfq_backfill_coverage_20261030.json` 的 `pre2020_jumps` 里。
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
HFQ = ROOT / "data" / "hfq_factor"

# (code, date) -> 相对跌幅；2026-10-30 全表实测，11 只 / 12 次
KNOWN_JUMPS = {
    ("000001", "2020-12-31"): -0.16816364,
    ("000002", "2020-11-19"): -0.02555526,
    ("000002", "2025-01-09"): -0.01178885,
    ("000517", "2020-11-17"): -0.01036253,
    ("600372", "2020-11-19"): -0.00387751,
    ("600654", "2020-11-25"): -0.13839379,
    ("600655", "2020-11-25"): -0.09114693,
    ("600681", "2020-11-25"): -0.03255809,
    ("600727", "2020-11-25"): -0.00081152,
    ("600771", "2020-11-25"): -0.00101321,
    ("600817", "2020-11-25"): -0.00271948,
    ("601607", "2020-11-27"): -0.00777702,
}
TOL = 1e-7
SCOPE = "2020-01-01"


def scan(root: Path = HFQ, *,
         since: str | None = SCOPE) -> list[tuple[str, str, float]]:
    """`since=None` 扫全历史（Task 9 审计用）；默认只扫在役段，白名单只对这一段负责。"""
    out: list[tuple[str, str, float]] = []
    for p in sorted(root.glob("*.parquet")):
        code = p.stem
        if not (len(code) == 6 and code.isdigit()):
            continue
        df = pd.read_parquet(p, columns=["date", "hfq_factor"]).sort_values("date")
        f = df["hfq_factor"].astype(float).to_numpy()
        d = df["date"].astype(str).to_numpy()
        for i in range(1, len(f)):
            if f[i - 1] > 0:
                r = f[i] / f[i - 1] - 1.0
                if r < -1e-6 and (since is None or d[i] >= since):
                    out.append((code, d[i], float(r)))
    return out


def test_downward_jumps_match_whitelist():
    found = {(c, d): r for c, d, r in scan()}
    assert set(found) == set(KNOWN_JUMPS), (
        f"新增下跳 {sorted(set(found) - set(KNOWN_JUMPS))}；"
        f"消失 {sorted(set(KNOWN_JUMPS) - set(found))}")
    for key, want in KNOWN_JUMPS.items():
        assert abs(found[key] - want) < TOL, (key, found[key], want)
