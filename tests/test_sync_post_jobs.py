"""同步单元必须把派生表、复权因子与后复权因子挂在后面，且后置失败不判整轮失败。"""
from __future__ import annotations

import importlib
import re
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
    """超时上限必须罩得住「full_sync + 三条串行后置」整条链的实测最坏值。

    2026-11-04 按实测重排预算（旧断言钉的是 `TimeoutStartSec=21600`，且注释/AGENTS 都写
    「adj 与 hfq 各自约 60 分钟」——那个数是错的）：整轮串行墙钟 11-02 = 4h51m59s、
    11-03 = 5h19m20s；单任务实测 adj 122.6/124.9 分钟、hfq 128 分钟、full_sync 40→73 分钟。
    现值 28800（8 小时）= 各任务最坏值取整 400 分钟 + baostock 重连退避余量。

    这里断**下界**而不是字面值：将来因为又变长而调大不该让这条红，压小才该红——
    钉死字面值的守卫会把「按实测重排」这件事本身变成一次次的改测试。
    """
    match = re.search(r"^TimeoutStartSec\s*=\s*(\d+)\s*$", _unit_text(), re.M)
    assert match, "tdx-sync.service 缺显式 TimeoutStartSec（三条后置共享一次 activation，不能落到默认值）"
    timeout_sec = int(match.group(1))
    assert timeout_sec >= 28800, (
        f"TimeoutStartSec={timeout_sec} 罩不住实测整轮 5h19m20s + 退避余量（下界 28800s = 8h）"
    )
