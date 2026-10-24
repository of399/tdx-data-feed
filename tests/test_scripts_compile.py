"""钉的破坏：把某个 .py 改成编译不过，而没有任何测试 import 它——于是全量测试全绿。

2026-10-24 实测到的形状：一次跨文件批量扫描把 `scripts/bench_cls2000.py` 的函数体首行
多缩进了一级（IndentationError），`pytest tests/ -q` 492 项**全绿**，因为没有任何用例导入
这个脚本。`ruff` 其实看得见（invalid-syntax），但 `make lint-all` 以 `|| true` 收尾，退出码
被吞掉——AGENTS.md 明写它不能当通过证据。所以这条门禁放在测试集里：所有人都跑的那条命令
必须能拦住它。

用例只依赖标准库 `ast`：不启动子进程、不依赖 ruff 装没装，扫 328 个文件不到 1 秒。
"""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 跳过的是「不作为现状规格」的东西：归档快照（AGENTS.md 明写历史产物不是现状）、
# 虚拟环境与依赖、以及 gitignore 掉的运行产物目录。数据目录里的 .py 是历史导出，不参与门禁。
_SKIP_DIRS = {
    "_archive", "venv", ".venv", "node_modules", ".git", "__pycache__", "site-packages",
    "data", "logs", "models", "downloads", ".codebuddy", ".qoder",
}

# 已经坏着、但不在本条守卫职权范围内修的文件。
# scripts/bench_cls2000.py：2026-10-24 一次跨文件批量扫描把函数体首行多缩进一级
# （IndentationError @ 78 行）。修它属于那次扫描的收尾，归另一个作业，不在这里顺手改。
# 它被修好之后这一条就没人引用了——守卫不会因此变红，删掉这行即可。
_KNOWN_BROKEN = {
    "scripts/bench_cls2000.py": (78, "IndentationError"),
}


def _python_files(base: Path):
    """受门禁的 .py 相对路径——跳过规则改宽到「几乎什么都不扫」，守卫就成了摆设。"""
    for f in sorted(base.rglob("*.py")):
        rel = f.relative_to(base)
        if set(rel.parts) & _SKIP_DIRS:
            continue
        yield str(rel)


def _unparseable(base: Path):
    """返回 {相对路径: (行号, 异常类名)}——只给路径不给行号等于没报。"""
    found = {}
    for rel in _python_files(base):
        try:
            ast.parse((base / rel).read_text(encoding="utf-8", errors="replace"), filename=rel)
        except SyntaxError as e:
            found[rel] = (e.lineno, type(e).__name__)
    return found


def test_unparseable_file_is_reported_with_its_line(tmp_path):
    """受控的坏文件必须被报出来，且带行号——只报「有文件坏了」等于没报。"""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    # 与真实事故同一形状：函数体首行多缩进一级。
    (pkg / "broken.py").write_text(
        "def parse(resp):\n"
        '    """文档。"""\n'
        "        r = resp.strip()\n"
        "    return r\n",
        encoding="utf-8",
    )
    (pkg / "fine.py").write_text("def ok():\n    return 1\n", encoding="utf-8")

    found = _unparseable(tmp_path)

    assert set(found) == {str(Path("pkg") / "broken.py")}, (
        f"该报的没报或误报了干净的：{found}"
    )
    lineno, kind = found[str(Path("pkg") / "broken.py")]
    assert lineno == 3, f"行号不对，运维会去改错那一行：{kind} @ {lineno}"
    assert kind == "IndentationError", f"错误类型没带出来：{kind}"


def test_repo_has_no_new_unparseable_script():
    """新出现的编译不过 = 红。已知的 _KNOWN_BROKEN 之外一个都不许有。"""
    found = _unparseable(ROOT)
    new = {p: v for p, v in found.items() if p not in _KNOWN_BROKEN}
    assert not new, (
        "这些文件编译不过，而没有任何测试导入它——所以只有这条守卫会响："
        + "、".join(f"{p}:{v[0]} {v[1]}" for p, v in sorted(new.items()))
    )


def test_gate_actually_covers_the_repository():
    """反向钉住扫描范围：把 _SKIP_DIRS 写宽（或把 rglob 改成只扫 tests/）时，这条先红。"""
    scanned = list(_python_files(ROOT))
    assert len(scanned) >= 250, f"门禁只扫到 {len(scanned)} 个 .py，它已经不拦住任何东西了"
    for must_cover in ("scripts/bench_cls2000.py", "v5/scripts/v4_batch_analysis.py",
                       "quant/multifactor/cv_utils.py", "conftest.py"):
        assert must_cover in scanned, f"{must_cover} 不在门禁覆盖范围内（共 {len(scanned)} 个文件）"
