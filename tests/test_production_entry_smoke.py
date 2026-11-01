"""生产入口冒烟：定时任务真正调用的脚本/模块必须能 import、脚本能 --help。

动机：CI 门禁只跑 tests/，入口脚本的 import 期断裂（缺依赖、import 副作用、
顶层读盘）要到凌晨定时任务才暴露。这里把 systemd 单元里 ExecStart 指向的
python 入口逐个冒烟，让这类断裂在 CI 就变红。

口径：
- `scripts/*.py` 跑 `--help`（argparse 在 main 逻辑前返回，不触发业务）；
- `-m tdxfeed.*` 只做 import（full_sync 这类不认 --help、任何调用都会开跑，
  绝不能拿 --help 去冒烟它）；
- 入口清单从单元文件解析，新增 timer 的入口自动纳入，再由 USER_TIMERS 覆盖守卫兜底。
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

NEEDS_DATA = False

ROOT = Path(__file__).resolve().parents[1]
UNIT_DIRS = (ROOT / "systemd", ROOT / "systemd" / "system")
TIMEOUT_S = 120


def _make_target_scripts(target: str) -> set[str]:
    """解析 Makefile 某目标配方里引用的 scripts/*.py（clean-logs 经 make 调用）。"""
    text = (ROOT / "Makefile").read_text(encoding="utf-8")
    m = re.search(rf"^{re.escape(target)}:.*$", text, re.MULTILINE)
    if not m:
        return set()
    out: set[str] = set()
    for line in text[m.end():].splitlines():
        if line and not line[0].isspace():
            break
        out.update(re.findall(r"(?:\$\(SCRIPTS\)/|scripts/)([\w]+\.py)", line))
    return out


def _exec_entries() -> tuple[set[str], set[str]]:
    """从单元 ExecStart/ExecStartPost 解析出 (脚本名, -m 模块名)。

    只收仓库根 scripts/（`(?<!v5/)` 排除 v5/scripts/ 的常驻守护进程——
    它们不认 --help，归 P0-2 用 import/退出码另行收口）；经 make 调用的
    目标再展开其配方里的脚本。
    """
    scripts: set[str] = set()
    modules: set[str] = set()
    for d in UNIT_DIRS:
        for unit in sorted(d.glob("*.service")):
            for line in unit.read_text(encoding="utf-8").splitlines():
                m = re.match(r"^ExecStart(?:Post)?=(?:-)?(.*)$", line)
                if not m:
                    continue
                cmd = m.group(1)
                scripts.update(re.findall(r"(?<!v5/)scripts/([\w]+\.py)", cmd))
                modules.update(re.findall(r"-m\s+([\w.]+)", cmd))
                for target in re.findall(r"make\s+(?:-C\s+\S+\s+)?([\w][\w-]*)", cmd):
                    scripts.update(_make_target_scripts(target))
    return scripts, modules


def _run(argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        argv, cwd=ROOT, capture_output=True, text=True, timeout=TIMEOUT_S,
    )


@pytest.mark.parametrize("script", sorted(_exec_entries()[0]))
def test_script_entry_help_is_clean(script):
    # 先静态要求 argparse：没有它的脚本会忽略 --help 直接跑业务（写样本/下单文本），
    # 绝不能拿 --help 去冒烟那种入口。缺 argparse 本身就是缺陷，在这里变红。
    source = (ROOT / "scripts" / script).read_text(encoding="utf-8")
    assert "ArgumentParser" in source, (
        f"{script} 未声明 argparse：--help 会被忽略并直接跑业务，需先加参数解析"
    )
    proc = _run([sys.executable, str(ROOT / "scripts" / script), "--help"])
    assert proc.returncode == 0, (
        f"{script} --help 退出码 {proc.returncode}\n{proc.stderr[-1500:]}"
    )
    assert "Traceback" not in proc.stderr, f"{script} --help 抛异常:\n{proc.stderr[-1500:]}"


@pytest.mark.parametrize("module", sorted(_exec_entries()[1]))
def test_module_entry_imports_cleanly(module):
    proc = _run([sys.executable, "-c",
                 f"import importlib; importlib.import_module({module!r})"])
    assert proc.returncode == 0, (
        f"import {module} 失败 rc={proc.returncode}\n{proc.stderr[-1500:]}"
    )
    assert "Traceback" not in proc.stderr, f"import {module} 抛异常:\n{proc.stderr[-1500:]}"


def _make_var(name: str) -> list[str]:
    text = (ROOT / "Makefile").read_text(encoding="utf-8")
    m = re.search(rf"^{name}\s*:=\s*((?:.*\\\n)*.*)$", text, re.MULTILINE)
    assert m, f"Makefile 缺 {name}"
    return m.group(1).replace("\\\n", " ").split()


def test_every_user_timer_entry_is_smoked():
    """USER_TIMERS 里每只 timer 的 service 入口都必须落在冒烟清单里。"""
    scripts, modules = _exec_entries()
    missing = []
    for timer in _make_var("USER_TIMERS"):
        unit = ROOT / "systemd" / f"{timer}.service"
        if not unit.exists():
            missing.append(f"{timer}（无 service 文件）")
            continue
        text = unit.read_text(encoding="utf-8")
        tokens = set(re.findall(r"(?<!v5/)scripts/([\w]+\.py)", text))
        tokens.update(re.findall(r"-m\s+([\w.]+)", text))
        for target in re.findall(r"make\s+(?:-C\s+\S+\s+)?([\w][\w-]*)", text):
            tokens.update(_make_target_scripts(target))
        if not tokens:
            missing.append(f"{timer}（ExecStart 非 python 入口，需人工确认）")
        elif not (tokens & (scripts | modules)):
            missing.append(f"{timer} → {sorted(tokens)}")
    assert not missing, f"以下 timer 入口未被冒烟覆盖: {missing}"
