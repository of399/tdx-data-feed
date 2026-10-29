"""日线文件定位单源：禁止试前缀探针。

daily 目录里 000001/000016/000688/000852/000905 同码双文件（sh 为指数、
sz 为个股），`for prefix in ("sh", "sz", "bj")` 先命中 sh 会把个股读成
指数。定位一律走 paths.daily_path（入参前缀优先，裸码按代码段推断）。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PROBES = (
    'for prefix in ("sh", "sz", "bj")',
    'for prefix in ("", "sh", "sz", "bj")',
)

SWEPT = (
    "scripts/llm_signal_plan_e.py",
    "scripts/build_v4_forward_labels.py",
    "scripts/plan_e_sim_live.py",
    "v5/scripts/v4_aggregate_report.py",
    "v5/scripts/v4_historical_roll.py",
    "v5/scripts/v4_poc_analysis.py",
    "v5/scripts/v4_seq_only.py",
    "v5/scripts/v5_eval.py",
    "v5/scripts/v5_v6_compare.py",
    "v5/scripts/v6_data_prep.py",
    "v5/scripts/v6_eval.py",
    "v5/scripts/v6_eval_direct.py",
    "v5/scripts/v6_seq_fixed.py",
    "v5/scripts/v6_seq_only.py",
    "v5/scripts/v6_vs_v4_baseline.py",
    "v5/scripts/v6_vs_v4_seq.py",
    "v5/scripts/vault_to_v5.py",
)


def test_no_try_prefix_daily_probe():
    offenders = []
    for sub in ("scripts", "v5/scripts"):
        for p in sorted((ROOT / sub).glob("*.py")):
            text = p.read_text(encoding="utf-8")
            for probe in PROBES:
                if probe in text:
                    offenders.append(f"{p.relative_to(ROOT)}: {probe}")
    assert offenders == []


def test_swept_lookup_uses_daily_path():
    missing = [
        rel for rel in SWEPT
        if "daily_path(" not in (ROOT / rel).read_text(encoding="utf-8")
    ]
    assert missing == []


def test_common_norm_code_delegates_to_limits():
    spec = importlib.util.spec_from_file_location(
        "scripts_common_norm_under_test", ROOT / "scripts/_common.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    from tdxfeed.execution.limits import normalize_code
    for raw in ("sh600000", "BJ920001", "1", "000001", " sz300750 "):
        assert mod.norm_code(raw) == normalize_code(raw)
