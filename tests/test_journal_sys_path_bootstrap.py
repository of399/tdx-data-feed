"""systemd 用 `venv/bin/python <abs path>/scripts/plan_e_journal.py` 启动本脚本。

直跑脚本时 sys.path[0] 是 scripts/ 而不是仓库根，因此根目录包 `quant` 只有在
仓库根被显式注入 sys.path 之后才可导入。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "plan_e_journal.py"


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        timeout=180,
        cwd=str(ROOT),
    )


def test_boots_when_invoked_by_absolute_path():
    """`--help` 只需要 argparse，但它跑在所有模块级导入之后 —— 导入崩了就到这里。"""
    proc = _run("--help")

    assert "No module named" not in proc.stderr, proc.stderr[-800:]
    assert proc.returncode == 0, proc.stderr[-800:]
    assert "usage:" in proc.stdout


def test_quant_execution_helpers_are_bound_when_loaded_from_outside_repo(tmp_path):
    """模块级导入自己引导成功后，它依赖的执行函数才真的绑定上。

    driver 落在仓库外：`python <script>` 时 sys.path[0] 只有 driver 所在目录，
    cwd 不会进 sys.path —— 与 systemd 直跑脚本的条件一致。
    """
    driver = tmp_path / "driver.py"
    driver.write_text(
        "import importlib.util\n"
        f"spec = importlib.util.spec_from_file_location('pej', {str(SCRIPT)!r})\n"
        "mod = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(mod)\n"
        "print(callable(mod.suggest_execution), callable(mod.execution_table_markdown))\n",
        encoding="utf-8",
    )
    proc = subprocess.run(
        [sys.executable, str(driver)],
        capture_output=True,
        text=True,
        timeout=180,
        cwd=str(ROOT),
    )

    assert proc.returncode == 0, proc.stderr[-800:]
    assert proc.stdout.strip() == "True True", proc.stdout[-300:]
