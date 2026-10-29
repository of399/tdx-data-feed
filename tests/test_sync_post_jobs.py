"""同步单元必须把派生表、复权因子与后复权因子挂在后面，且后置失败不判整轮失败。"""
from __future__ import annotations

import importlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UNIT = ROOT / "systemd" / "system" / "tdx-sync.service"

NEEDS_DATA = False


def test_sync_unit_has_post_jobs():
    text = UNIT.read_text(encoding="utf-8")
    assert "ExecStartPost=-/home/jiuben/tdx-data-feed/venv/bin/python -m tdxfeed.precompute.indicators --incremental" in text
    assert "ExecStartPost=-/home/jiuben/tdx-data-feed/venv/bin/python -m tdxfeed.adj_factor" in text
    assert "ExecStartPost=-/home/jiuben/tdx-data-feed/venv/bin/python -m tdxfeed.hfq_factor" in text


def test_post_job_modules_expose_main():
    for mod_name in ("tdxfeed.precompute.indicators", "tdxfeed.adj_factor",
                     "tdxfeed.hfq_factor"):
        mod = importlib.import_module(mod_name)
        assert callable(getattr(mod, "main", None)), f"{mod_name} 缺少 main()"


def test_indicators_supports_incremental_flag():
    src = (ROOT / "tdxfeed" / "precompute" / "indicators.py").read_text(encoding="utf-8")
    assert '"--incremental"' in src or "'--incremental'" in src


def test_all_post_jobs_are_optional():
    lines = [
        line.strip()
        for line in UNIT.read_text(encoding="utf-8").splitlines()
        if line.strip().startswith("ExecStartPost=")
    ]
    assert len(lines) == 3, f"预期三条后置任务，实际 {len(lines)} 条：{lines}"
    for line in lines:
        assert line.startswith("ExecStartPost=-"), f"后置任务缺 '-' 前缀（失败会把整轮同步判红）：{line}"


def test_timeout_covers_both_full_market_post_jobs():
    """adj 与 hfq 各自全市场约 60 分钟，3 小时的原值会在源变慢时把 oneshot 拖超时。"""
    text = UNIT.read_text(encoding="utf-8")
    assert "TimeoutStartSec=21600" in text
