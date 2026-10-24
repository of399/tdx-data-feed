"""仓库根导入约定守卫：`venv/bin/pytest tests/ -q`（AGENTS.md 记的主测试集命令）必须能收集。

背景：`tests/test_amount_fix.py` 在模块顶层直接 `from tdxfeed.amount_fix import ...`，
没有任何 sys.path 引导。裸 `pytest`（console script）不会把当前工作目录放进 sys.path，
所以这条命令一直在收集阶段就挂掉；只有 `python -m pytest`（cwd 自动入 path）才能跑。
这里用子进程跑那条被写进文档的命令，而不是在本进程里断言 sys.path —— 后者证明不了命令本身可用。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BARE_PYTEST = Path(sys.executable).with_name("pytest")


def test_bare_pytest_console_script_exists():
    assert BARE_PYTEST.exists(), f"没找到裸 pytest 入口：{BARE_PYTEST}"


def test_bare_pytest_collects_a_root_relative_import():
    proc = subprocess.run(
        [str(BARE_PYTEST), "tests/test_amount_fix.py", "-q"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    output = proc.stdout + proc.stderr
    assert "No module named 'tdxfeed'" not in output, output
    assert proc.returncode == 0, output
