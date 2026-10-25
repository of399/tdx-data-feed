"""线上系统单元与仓库单元的漂移守卫。

只读 /etc 与 systemctl，不需要 root。三类漂移分开断言：

1. 磁盘内容漂移 —— 仓库单元改了但没 `cp` 到 /etc（"没切"）。
2. 加载态漂移 —— /etc 内容已更新但没 `daemon-reload`
   （systemd 加载的仍是旧文件里的解释器）。
3. 进程漂移 —— 加载态已更新但没 `restart`（老进程继续用旧解释器）。

`MATH_LIVE_UNIT` 可把"线上单元"指到别处，用于在无 /etc 权限的环境里
自证守卫真的能抓到漂移，而不是永远绿。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
REPO_UNIT = ROOT / "v5" / "systemd" / "math-sympy-http.service"
DEFAULT_LIVE_UNIT = Path("/etc/systemd/system/math-sympy-http.service")
SERVICE_NAME = "math-sympy-http"


def _unit_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _joined_execstart(text: str) -> str:
    """ExecStart 的值，反斜杠续行已拼平、空白已归一。"""
    lines = text.splitlines()
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("ExecStart="):
            chunks = [stripped[len("ExecStart="):].strip()]
            cursor = idx
            while chunks[-1].endswith("\\"):
                chunks[-1] = chunks[-1][:-1].strip()
                cursor += 1
                chunks.append(lines[cursor].strip())
            return re.sub(r"\s+", " ", " ".join(c for c in chunks if c)).strip()
    raise AssertionError(f"单元里没有 ExecStart：\n{text}")


def _interpreter_of(text: str) -> str:
    return _joined_execstart(text).split(" ")[0]


def _path_env_of(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith('Environment="PATH='):
            return stripped[len('Environment="'):-1]
    raise AssertionError(f"单元里没有 Environment=\"PATH=...\"：\n{text}")


def _live_unit() -> Path:
    return Path(os.environ.get("MATH_LIVE_UNIT", str(DEFAULT_LIVE_UNIT)))


@pytest.fixture(scope="module")
def repo_text() -> str:
    if not REPO_UNIT.exists():
        pytest.skip(f"仓库单元缺失 {REPO_UNIT}")
    return _unit_text(REPO_UNIT)


def test_deployed_unit_file_matches_repo(repo_text):
    """磁盘上的 /etc 单元，ExecStart 与 PATH 必须和仓库一致（否则是"没切"）"""
    live = _live_unit()
    if not live.exists():
        pytest.skip(f"线上单元不存在 {live}（非部署机）")
    live_text = _unit_text(live)

    assert _joined_execstart(live_text) == _joined_execstart(repo_text), (
        f"线上单元 ExecStart 与仓库不一致：线上={_interpreter_of(live_text)} "
        f"仓库={_interpreter_of(repo_text)}；"
        "修法 sudo cp v5/systemd/math-sympy-http.service /etc/systemd/system/ "
        "再 daemon-reload + restart"
    )
    assert _path_env_of(live_text) == _path_env_of(repo_text), (
        "线上单元 PATH 与仓库不一致：PATH 首位决定服务 import 到哪个解释器的包"
    )


def _loaded_exec_path() -> str:
    """systemd 当前**已加载**的 ExecStart 可执行文件（磁盘改了没 reload 就仍是旧值）

    ExecMainExePath 在本机是空的，只有 ExecStart 这个结构体里带 path=。
    """
    proc = subprocess.run(
        ["systemctl", "show", SERVICE_NAME, "-p", "ExecStart", "--value"],
        capture_output=True, text=True, timeout=30,
    )
    match = re.search(r"path=(\S+)\s*;", proc.stdout)
    return match.group(1) if match else ""


def _running_interpreter() -> str:
    """正在跑的进程用的是哪个解释器（reload 了没 restart 时这里仍是旧的）

    不能用 /proc/<pid>/exe：venv 的 python 是指向系统解释器的符号链接，
    那里只能看到 /usr/bin/python3.x，区分不出 venv 与 venv-math。
    """
    pid = subprocess.run(
        ["systemctl", "show", SERVICE_NAME, "-p", "MainPID", "--value"],
        capture_output=True, text=True, timeout=30,
    ).stdout.strip()
    if not pid or pid == "0":
        return ""
    cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
    return cmdline[0].decode("utf-8", "replace")


def test_loaded_unit_matches_repo(repo_text):
    """已加载的单元 + 正在跑的进程都必须落在仓库要求的解释器上"""
    if _live_unit() != DEFAULT_LIVE_UNIT:
        pytest.skip("MATH_LIVE_UNIT 指向副本，加载态只对应真实部署")
    if shutil.which("systemctl") is None:
        pytest.skip("没有 systemctl，无法读加载态")

    expected = _interpreter_of(repo_text)
    loaded = _loaded_exec_path()
    if not loaded:
        pytest.skip(f"{SERVICE_NAME} 未安装，加载态无从比对")
    assert loaded == expected, (
        f"systemd 已加载的解释器是 {loaded}，仓库要求 {expected}；"
        "单元文件写了新的但没 systemctl daemon-reload"
    )

    running = _running_interpreter()
    if not running:
        pytest.skip(f"{SERVICE_NAME} 没在跑，进程侧无从比对")
    assert running == expected, (
        f"pid 进程表里的解释器是 {running}，加载态是 {loaded}；"
        "daemon-reload 之后还要 systemctl restart，否则老进程继续用旧解释器"
    )


def test_guard_catches_a_stale_deployed_unit(tmp_path, repo_text):
    """守卫自身的失效能力：把"线上"指向旧解释器单元副本，必须报红

    没有这条，前两个断言在 /etc 缺失时只会 skip，等于没守卫。
    """
    stale = tmp_path / "math-sympy-http.service"
    stale.write_text(
        _unit_text(REPO_UNIT).replace("venv-math/bin/python", "venv/bin/python"),
        encoding="utf-8",
    )
    os.environ["MATH_LIVE_UNIT"] = str(stale)
    try:
        with pytest.raises(AssertionError, match="ExecStart"):
            test_deployed_unit_file_matches_repo(repo_text)
    finally:
        os.environ.pop("MATH_LIVE_UNIT", None)
