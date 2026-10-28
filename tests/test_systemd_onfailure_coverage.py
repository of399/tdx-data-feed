"""用户级单元失败必须有人知道：每个 .service 都要挂 OnFailure。

例外只有两个，理由写在下面：
- `onfailure-alert@.service` 自己就是投递器，自指会成环；
- `pushplus-bridge.service` 是被调用的桥，桥挂了只能靠日志与 canary。

系统级单元（`systemd/system/`）不适用：模板实例只能在同一 manager 里解析，
把 user 单元的 OnFailure 写进 /etc 下的单元等于永远不生效。
"""

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SYSTEMD = ROOT / "systemd"
EXEMPT = {"onfailure-alert@.service", "pushplus-bridge.service"}


def _units():
    return sorted(p for p in SYSTEMD.glob("*.service") if p.name not in EXEMPT)


@pytest.mark.parametrize("unit", _units(), ids=[p.name for p in _units()])
def test_user_service_alerts_on_failure(unit):
    text = unit.read_text(encoding="utf-8")
    assert f"OnFailure=onfailure-alert@{unit.name}" in text, (
        f"{unit.name} 失败后无人知晓：补 OnFailure=onfailure-alert@{unit.name}"
        "（必须是 [Unit] 段，写在 [Service] 会被 systemd 静默忽略）"
    )
