"""同步单元必须把派生表、复权因子与后复权因子挂在后面，且后置失败不判整轮失败。"""
from __future__ import annotations

import importlib
from pathlib import Path

from scripts.render_systemd_units import render_text

ROOT = Path(__file__).resolve().parents[1]
UNIT = ROOT / "systemd" / "system" / "tdx-sync.service"

NEEDS_DATA = False


def _unit_text() -> str:
    """单元是模板（仓库根写作 @TDX_HOME@），按当前 checkout 的仓库根渲染后再断言。"""
    return render_text(UNIT.read_text(encoding="utf-8"), str(ROOT))


def test_sync_unit_has_post_jobs():
    text = _unit_text()
    assert f"ExecStartPost=-{ROOT}/venv/bin/python -m tdxfeed.precompute.indicators --incremental" in text
    assert f"ExecStartPost=-{ROOT}/venv/bin/python -m tdxfeed.adj_factor" in text
    assert f"ExecStartPost=-{ROOT}/venv/bin/python -m tdxfeed.hfq_factor" in text


def test_post_job_modules_expose_main():
    for mod_name in ("tdxfeed.precompute.indicators", "tdxfeed.adj_factor",
                     "tdxfeed.hfq_factor"):
        mod = importlib.import_module(mod_name)
        assert callable(getattr(mod, "main", None)), f"{mod_name} 缺少 main()"


def test_indicators_supports_incremental_flag():
    src = (ROOT / "tdxfeed" / "precompute" / "indicators.py").read_text(encoding="utf-8")
    assert '"--incremental"' in src or "'--incremental'" in src


def test_all_post_jobs_are_optional():
    """派生表与两条复权因子是可选后置任务（失败不判整轮红）。

    同步哨兵 mark_sync_done.py 是唯一的例外：它失败就是要判红（P0-1 的完成标记），
    所以它不带 '-' 前缀——这里把「可选的必须都可选、非可选的只允许是哨兵」一起钉住。
    """
    lines = [
        line.strip()
        for line in _unit_text().splitlines()
        if line.strip().startswith("ExecStartPost=")
    ]
    optional = [ln for ln in lines if ln.startswith("ExecStartPost=-")]
    mandatory = [ln for ln in lines if not ln.startswith("ExecStartPost=-")]
    assert len(optional) == 3, f"预期三条可选后置任务，实际 {len(optional)} 条：{optional}"
    for line in mandatory:
        assert "mark_sync_done.py" in line, f"非可选后置任务只允许同步哨兵（失败要判红）：{line}"


def test_timeout_covers_both_full_market_post_jobs():
    """adj 与 hfq 各自全市场约 60 分钟，3 小时的原值会在源变慢时把 oneshot 拖超时。"""
    assert "TimeoutStartSec=21600" in _unit_text()
