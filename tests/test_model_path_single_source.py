"""live 入口的模型文件路径必须从 `tdxfeed.paths.model_path()` 取，不得自拼。

范围只含读侧 live 入口：训练/评估脚本的产物路径仍按各自约定（本守卫不扫）。
规则（AST，避免文档字符串里举例 `data/double_up/...` 误伤）：
  R1 任何 `a / b` 拼接链里的字符串常量含 `double_up`；
  R2 任意调用的直接实参是含 `data/double_up/` 的字符串字面量。
"""
from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

# 本模块只读源码文本，不读 data/；docstring 含 data/ 字样会被根 conftest 误判，故显式退出。
NEEDS_DATA = False

ROOT = Path(__file__).resolve().parents[1]
LIVE_READERS = (
    "scripts/predict.py",
    "scripts/plan_e_sim_live.py",
    "mcp/servers/backtest.py",
)


def _str_constants(node: ast.AST) -> list[str]:
    return [n.value for n in ast.walk(node)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)]


def violations(path: Path) -> list[int]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    lines: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            if "double_up" in _str_constants(node):
                lines.add(node.lineno)
        elif isinstance(node, ast.Call):
            for a in [*node.args, *node.keywords]:
                if (isinstance(a, ast.Constant) and isinstance(a.value, str)
                        and "data/double_up/" in a.value):
                    lines.add(node.lineno)
    return sorted(lines)


def test_live_readers_do_not_self_concatenate_model_dir():
    bad = {rel: violations(ROOT / rel) for rel in LIVE_READERS}
    bad = {rel: lines for rel, lines in bad.items() if lines}
    rows = [f"{rel}  {lines}" for rel, lines in sorted(bad.items())]
    assert not bad, "以下 live 入口自拼模型目录，应改用 paths.model_path()：\n" + "\n".join(rows)


def test_live_readers_use_model_path():
    missing = [rel for rel in LIVE_READERS
               if "model_path(" not in (ROOT / rel).read_text(encoding="utf-8")]
    assert missing == []


def test_model_path_is_data_root_relative(tmp_path, monkeypatch):
    from tdxfeed import paths
    monkeypatch.setattr(paths, "DATA_ROOT", tmp_path)
    assert paths.model_dir() == tmp_path / "double_up"
    assert paths.model_path("x.txt") == tmp_path / "double_up" / "x.txt"


def test_common_bridge_reexports_model_path():
    from tdxfeed import paths
    spec = importlib.util.spec_from_file_location(
        "scripts_common_model_under_test", ROOT / "scripts/_common.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.model_path("x.txt") == paths.model_path("x.txt")


def test_guard_catches_both_shapes(tmp_path):
    sample = tmp_path / "sample.py"
    sample.write_text(
        "from pathlib import Path\n"
        "ROOT = Path('/x')\n"
        "A = ROOT / 'data' / 'double_up' / 'm.txt'\n"
        "B = Path('/home/u/proj/data/double_up/m.txt')\n"
        "C = model_path('m.txt')\n",
        encoding="utf-8",
    )
    assert violations(sample) == [3, 4]


def test_guard_ignores_docstrings_mentioning_the_dir(tmp_path):
    sample = tmp_path / "ok.py"
    sample.write_text(
        '"""默认模型 data/double_up/d_signal_5d_model.txt（只是说明文字）。"""\n'
        "M = model_path('d_signal_5d_model.txt')\n",
        encoding="utf-8",
    )
    assert violations(sample) == []
