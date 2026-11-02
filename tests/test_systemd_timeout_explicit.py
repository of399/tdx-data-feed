"""P0-5（KI-OPS-003）：在役的重任务 / 周期服务必须显式 TimeoutStartSec。

默认 user manager 的 TimeoutStartSec 仅 90s，而 `predict-daily` 要跑 5221 只 × 8 进程，
贴着默认值边跑——一旦拉长就会被 systemd 杀掉且没有任何完成标记。这里锁三件事：
1. 在役重任务（predict-daily / plan-e-journal）明确 >= 1800s；
2. 周期巡检（canary-health）明确 >= 120s；
3. 既有已显式设置的单元不被这次改动回退（防止回归）。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SYSTEMD = ROOT / "systemd"


def _timeout_seconds(name: str) -> int | None:
    text = (SYSTEMD / name).read_text(encoding="utf-8")
    m = re.search(r"^TimeoutStartSec\s*=\s*(\d+)", text, re.M)
    return int(m.group(1)) if m else None


def test_predict_and_journal_have_explicit_timeout():
    assert _timeout_seconds("predict-daily.service") == 1800
    assert _timeout_seconds("plan-e-journal.service") == 1800


def test_canary_has_explicit_timeout():
    assert _timeout_seconds("canary-health.service") == 120


def test_existing_timeouts_not_regressed():
    """既有显式设置的单元不得因这次改动被回退。"""
    assert _timeout_seconds("daban-ticket.service") == 180
    assert _timeout_seconds("plan-e-double-up.service") == 1800
    assert _timeout_seconds("plan-e-today.service") == 600
