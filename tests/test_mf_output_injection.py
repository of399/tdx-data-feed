"""多因子回测的落盘路径必须可注入 —— 否则每跑一次全量测试都往生产输出目录塞报告。

背景：quant/multifactor/engine.py 把 plan_e_backtest_mf_v1_* 写死到 data/plan_e/，
而那个目录同时是 systemd timer 的生产输出目录；tests/test_position_risk.py 里两次
run_mf_backtest 调用因此每次全量测试都留下两对时间戳产物并覆盖 _latest.md。
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from quant.multifactor.factors import MFConfig

PROD_GLOB = "plan_e_backtest_mf_v1_*"


def test_run_mf_backtest_writes_only_into_given_output_dir(tmp_path):
    """给了 output_dir 就只能往它写，生产目录一个文件都不许多。"""
    from quant.multifactor import engine
    from quant.multifactor.engine import run_mf_backtest

    prod = engine.OUTPUT_DIR
    before = set(prod.glob(PROD_GLOB)) if prod.exists() else set()

    _, m = run_mf_backtest(
        MFConfig(limit=25, end="2026-10-15", valuation_mode="proxy"),
        output_dir=tmp_path,
    )

    produced = sorted(p.name for p in tmp_path.glob(PROD_GLOB))
    assert produced, "output_dir 里什么都没写，注入路径没生效"
    assert any(p.endswith("_latest.md") for p in produced), produced
    after = set(prod.glob(PROD_GLOB)) if prod.exists() else set()
    assert after == before, f"生产输出目录被写脏：新增 {sorted(after - before)}"

    # 顺带确认这次小样本确实跑出了东西（否则上面的空目录断言会被假阳性满足）
    assert m.get("universe_size", 0) > 0, m


@pytest.mark.parametrize("py_file", sorted(Path("tests").glob("test_*.py")))
def test_tests_never_call_run_mf_backtest_without_output_dir(py_file):
    """测试里每一次 run_mf_backtest 调用都必须显式给 output_dir。

    默认参数仍然写生产目录（CLI 需要它），所以唯一的护栏是调用点。
    """
    tree = ast.parse(py_file.read_text(encoding="utf-8"))
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = fn.id if isinstance(fn, ast.Name) else (
            fn.attr if isinstance(fn, ast.Attribute) else "")
        if name != "run_mf_backtest":
            continue
        if any(kw.arg == "output_dir" for kw in node.keywords):
            continue
        offenders.append(f"{py_file}:{node.lineno}")
    assert not offenders, f"这些调用会污染 data/plan_e/：{offenders}"
