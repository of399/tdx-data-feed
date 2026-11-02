"""P1-6（KI-COMPETE-002）：任何 unit 都不能引用已退役的 tdxfeed-sync 单元。

`tdxfeed-sync.*` 于 2026-10-28 退役（文件保留以便回滚），`make timer-enable` 不再启用它。
但 `plan-e-double-up.service` 曾写 `After=tdxfeed-sync.service` —— 引用一个不再存在的单元，
既让依赖图指向幽灵，也让 `systemd-analyze verify` 在跨 manager 场景下发不可见警告。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SYSTEMD = ROOT / "systemd"

# 退役单元的基名（不含 .service/.timer）
RETIRED = {"tdxfeed-sync"}

# 依赖指令里可能出现的引用型键
_DEP_KEYS = r"(After|Requires|Wants|PartOf|Conflicts|BindsTo|Before|Triggers)"
_DEP_RE = re.compile(rf"^{_DEP_KEYS}\s*=\s*(.+)$", re.M)


def _iter_refs():
    for p in sorted(SYSTEMD.rglob("*.service")) + sorted(SYSTEMD.rglob("*.timer")):
        text = p.read_text(encoding="utf-8")
        for m in _DEP_RE.finditer(text):
            for ref in m.group(2).split():
                yield p.name, ref.strip()


def test_no_retired_unit_references():
    bad = []
    for name, ref in _iter_refs():
        base = ref.split(".")[0]
        if base in RETIRED:
            bad.append((name, ref))
    assert not bad, f"仍有 unit 引用已退役的 tdxfeed-sync：{bad}"


def test_active_tdx_sync_unit_is_real():
    """守护：活跃依赖指向的 tdx-sync 必须真实存在（系统级），避免把死依赖换成另一个幽灵。"""
    refs = {ref.split(".")[0] for _, ref in _iter_refs() if "tdx-sync" in ref}
    # 允许 tdx-sync（系统级活跃）作为引用目标
    assert "tdx-sync" in refs, "没有任何 unit 引用系统级 tdx-sync（应至少 plan-e-double-up 引用）"
    # tdx-sync 单元文件必须存在
    assert (SYSTEMD / "system" / "tdx-sync.service").is_file()
