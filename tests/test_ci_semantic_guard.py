"""M-4 三条 AST 语义守卫的守卫测试。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import ci_semantic_guard as sg  # noqa: E402

SEM_BASELINE = ROOT / "config" / "semantic_baseline.json"


def _scan(src: str, name: str = "t.py") -> list[sg.Finding]:
    """对源码片段跑三条守卫。"""
    import ast
    tree = ast.parse(src)
    lines = src.splitlines()
    out: list[sg.Finding] = []
    for check in sg.CHECKS:
        for f in check(tree, lines):
            f.file = name
            out.append(f)
    return out


def _codes(src: str) -> list[str]:
    return [f.code for f in _scan(src)]


# --------------------------------------------------------------------------- #
# SEM001 原子写
# --------------------------------------------------------------------------- #
def test_sem001_flags_direct_write() -> None:
    src = "def f(p):\n    p.write_text('x')\n"
    assert _codes(src).count("SEM001") == 1


def test_sem001_allows_tmp_plus_replace() -> None:
    src = ("import os\n"
           "def f(p):\n"
           "    tmp = p.with_suffix('.tmp')\n"
           "    tmp.write_text('x')\n"
           "    os.replace(tmp, p)\n")
    assert "SEM001" not in _codes(src)


def test_sem001_flags_tmp_without_replace() -> None:
    src = "def f(p):\n    tmp = p.with_suffix('.tmp')\n    tmp.write_text('x')\n"
    assert _codes(src).count("SEM001") == 1


def test_sem001_flags_open_write_mode() -> None:
    src = "def f(p):\n    with open(p, 'w') as fh:\n        fh.write('x')\n"
    assert _codes(src).count("SEM001") == 1


def test_sem001_flags_read_modify_write() -> None:
    src = ("def f(p):\n"
           "    old = p.read_text()\n"
           "    with open(p, 'a') as fh:\n"
           "        fh.write(old)\n")
    assert any("读-改-写" in f.message for f in _scan(src) if f.code == "SEM001")


def test_sem001_allows_pure_append() -> None:
    src = "def f(p):\n    with open(p, 'a') as fh:\n        fh.write('x')\n"
    assert "SEM001" not in _codes(src)


def test_sem001_flags_to_csv() -> None:
    src = "def f(df, p):\n    df.to_csv(p, index=False)\n"
    assert _codes(src).count("SEM001") == 1


def test_sem001_noqa_requires_reason() -> None:
    ok = "def f(p):\n    p.write_text('x')  # noqa: SEM001 (一次性报告输出)\n"
    bad = "def f(p):\n    p.write_text('x')  # noqa: SEM001\n"
    assert "SEM001" not in _codes(ok)
    assert "SEM001" in _codes(bad)


# --------------------------------------------------------------------------- #
# SEM002 吞错必计数
# --------------------------------------------------------------------------- #
def test_sem002_flags_pass() -> None:
    src = "def f():\n    try:\n        g()\n    except Exception:\n        pass\n"
    assert _codes(src).count("SEM002") == 1


def test_sem002_allows_raise() -> None:
    src = "def f():\n    try:\n        g()\n    except Exception:\n        raise\n"
    assert "SEM002" not in _codes(src)


def test_sem002_allows_counter_increment() -> None:
    src = ("def f():\n"
           "    try:\n"
           "        g()\n"
           "    except Exception:\n"
           "        failed_count += 1\n")
    assert "SEM002" not in _codes(src)


def test_sem002_allows_failure_list_append() -> None:
    src = ("def f():\n"
           "    try:\n"
           "        g()\n"
           "    except Exception as e:\n"
           "        failures.append(e)\n")
    assert "SEM002" not in _codes(src)


def test_sem002_flags_log_only() -> None:
    src = ("def f():\n"
           "    try:\n"
           "        g()\n"
           "    except Exception as e:\n"
           "        logger.exception(e)\n")
    assert _codes(src).count("SEM002") == 1


def test_sem002_flags_continue() -> None:
    src = ("def f():\n"
           "    for x in y:\n"
           "        try:\n"
           "            g(x)\n"
           "        except Exception:\n"
           "            continue\n")
    assert _codes(src).count("SEM002") == 1


# --------------------------------------------------------------------------- #
# SEM003 外部调用必带 timeout
# --------------------------------------------------------------------------- #
def test_sem003_flags_requests_without_timeout() -> None:
    src = "import requests\ndef f():\n    return requests.get('http://x')\n"
    assert _codes(src).count("SEM003") == 1


def test_sem003_allows_requests_with_timeout() -> None:
    src = ("import requests\n"
           "def f():\n"
           "    return requests.get('http://x', timeout=5)\n")
    assert "SEM003" not in _codes(src)


def test_sem003_flags_subprocess_run_without_timeout() -> None:
    src = "import subprocess\ndef f():\n    return subprocess.run(['ls'], capture_output=True)\n"
    assert _codes(src).count("SEM003") == 1


def test_sem003_ignores_request_constructor() -> None:
    src = ("import urllib.request\n"
           "def f():\n"
           "    req = urllib.request.Request('http://x')\n"
           "    return req\n")
    assert "SEM003" not in _codes(src)


def test_sem003_flags_popen_pipe_read_without_timeout() -> None:
    src = ("import subprocess\n"
           "def f():\n"
           "    self.proc = subprocess.Popen(['x'], stdout=subprocess.PIPE)\n"
           "    line = self.proc.stdout.readline()\n")
    assert _codes(src).count("SEM003") == 1


def test_sem003_allows_popen_read_with_timeout() -> None:
    """communicate(timeout=) 带超时的管道读不算违规。"""
    src = ("import subprocess\n"
           "def f():\n"
           "    self.proc = subprocess.Popen(['x'], stdout=subprocess.PIPE)\n"
           "    out = self.proc.communicate(timeout=30)\n")
    assert "SEM003" not in _codes(src)


def test_sem003_flags_unbounded_readline_even_if_communicate_has_timeout() -> None:
    """readline() 自身无超时 → 仍会无限阻塞（正是 KI-MCP-001 的形态）。"""
    src = ("import subprocess\n"
           "def f():\n"
           "    self.proc = subprocess.Popen(['x'], stdout=subprocess.PIPE)\n"
           "    line = self.proc.stdout.readline()\n"
           "    out = self.proc.communicate(timeout=30)\n")
    assert _codes(src).count("SEM003") == 1


# --------------------------------------------------------------------------- #
# 作用域与真实基线
# --------------------------------------------------------------------------- #
def test_scope_excludes_archive_and_frozen() -> None:
    assert "_archive" in sg.EXCLUDE_DIRS
    assert "v5" in sg.EXCLUDE_DIRS


def test_semantic_baseline_is_wellformed() -> None:
    assert SEM_BASELINE.exists(), "语义基线缺失"
    data = json.loads(SEM_BASELINE.read_text(encoding="utf-8"))
    assert data["version"] >= 1
    assert set(data["by_rule"]) <= {"SEM001", "SEM002", "SEM003"}
    assert data["total"] == sum(sum(r.values()) for r in data["by_file_rule"].values())


def test_semantic_baseline_not_below_measured() -> None:
    findings = sg.scan_paths()
    data = json.loads(SEM_BASELINE.read_text(encoding="utf-8"))
    assert data["total"] >= len(findings), (
        f"语义基线 {data['total']} < 实测 {len(findings)}：存在未登记的新语义违规")
