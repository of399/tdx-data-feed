"""qlib 是「环境孤岛」：只活在 venv-qlib/ 里，生产读路径一律不许 import 它。

背景（2026-11 实测）：pyqlib 0.9.7 的 wheel tag 只到 cp312（cp38~cp312，无 cp313/cp314），
主 venv 是 Python 3.14.4，物理上装不上；就算哪天能装，它的依赖树（mlflow / gym / jupyter /
pymongo / redis / cvxpy）也会把训练栈与 CI 门禁的精简集 requirements-ci.txt 一起污染。
处置与 venv-math 同构：单独一套解释器（conda 前缀环境 venv-qlib/，Python 3.12），
只借 qlib 的表达式引擎产出 Alpha158 表达式字符串，数值计算回主 venv。

锁三件事（前两条无条件跑，第三条按环境存在与否分流）：
1. `tdxfeed/**/*.py` 与 `scripts/*.py` 里不得出现 `import qlib` / `from qlib ...`——
   AST 扫描，不是 grep 全文，注释与文档字符串里提 qlib 不该把门禁打红；
2. **主 venv 里 `import qlib` 必须失败**：这是「不泄漏」的反向证明。CI 没有仓库 venv，
   就跑 pytest 的这个解释器（同样不得装得上 qlib），两条路都真有判据；
3. venv-qlib/ 存在 ⇒ 它必须 import 得动 qlib 且版本与 `requirements-qlib.txt` 钉的一致；
   不存在 ⇒ 安装脚本必须在、且 AGENTS.md 必须指向它（否则下一个人只会照 README 用主 venv）。

跳过条件**只有**「本机没有 conda 且岛也还没建」这一种（那种情况既建不了岛也无岛可验，
显式 skip 而不是让它绿）。CI 里 conda 缺失，所以第三条会是 skip——这是诚实的空，不是
永远绿：前两条照跑。本模块只读源码文本、不读行情数据，故 NEEDS_DATA = False。
"""
from __future__ import annotations

import ast
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

NEEDS_DATA = False

ROOT = Path(__file__).resolve().parents[1]
QLIB_VENV_PY = ROOT / ("venv" + "-qlib") / "bin" / "python"
MAIN_VENV_PY = ROOT / "venv" / "bin" / "python"
INSTALLER = ROOT / "scripts" / ("install_" + "qlib_env.sh")
REQS = ROOT / "requirements-qlib.txt"

# 生产读路径：只有这两棵树。venv*/data/logs/build 等都是产物或第二解释器，不在门禁面上。
PROD_GLOBS = ("tdxfeed/**/*.py", "scripts/*.py")
SKIP_DIR_PARTS = {"venv", "venv-math", "venv-qlib", "data", "logs", "build", "dist", "__pycache__"}


def _conda_available() -> bool:
    """安装岛的唯一前提。PATH 与本机 miniforge 绝对路径都探，脚本用的就是这个判据。"""
    return shutil.which("conda") is not None or Path("/opt/miniforge3/bin/conda").exists()


def qlib_import_lines(path: Path) -> list[int]:
    """返回该文件里 import 了 qlib（含子模块）的行号；解析不了的文件不算违规。"""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return []
    lines: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        else:
            continue
        if any(name.split(".")[0] == "qlib" for name in names):
            lines.append(node.lineno)
    return sorted(set(lines))


def production_files(root: Path = ROOT) -> list[Path]:
    out: list[Path] = []
    for pattern in PROD_GLOBS:
        for p in sorted(root.glob(pattern)):
            if p.is_dir() or any(part in SKIP_DIR_PARTS for part in p.relative_to(root).parts):
                continue
            out.append(p)
    return out


def test_production_read_paths_never_import_qlib():
    files = production_files()
    assert files, "生产读路径一棵树都没扫到，门禁等于没跑"
    bad = {
        p.relative_to(ROOT).as_posix(): qlib_import_lines(p)
        for p in files
        if qlib_import_lines(p)
    }
    assert not bad, (
        f"qlib 只许活在 venv-qlib/ 里（主 venv 无 cp312+ wheel，且它会带进 mlflow/gym/jupyter）。"
        f"这些生产文件 import 了它：{bad}。要么把表达式导出成字符串回主 venv 算，"
        f"要么把这段逻辑挪进 venv-qlib 侧脚本。"
    )


def test_main_venv_cannot_import_qlib():
    """隔离的反向证明：生产侧解释器里 qlib 必须导入失败。

    主 venv 在就用它（本机口径）；不在（CI 只装 requirements-ci.txt）就用跑 pytest 的这个
    解释器——两边都不是「无对象可测」，所以这条不需要 skip，也不会有假绿。
    """
    interp = MAIN_VENV_PY if MAIN_VENV_PY.exists() else Path(sys.executable)
    proc = subprocess.run(
        [str(interp), "-c", "import qlib"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert proc.returncode != 0, (
        f"{interp} 里居然 import 得动 qlib：岛漏了。pyqlib 一旦被装进生产侧解释器，"
        f"它的依赖树就会盖住训练栈与 CI 精简集。\n{proc.stdout}"
    )
    assert "No module named" in proc.stderr or "ModuleNotFoundError" in proc.stderr, (
        f"qlib 导入失败的原因不是「没装」，而是别的东西（这等于把泄漏藏进了报错里）："
        f"\n{proc.stderr}"
    )


def test_qlib_island_is_usable_or_its_recipe_is_documented():
    if QLIB_VENV_PY.exists():
        proc = subprocess.run(
            [str(QLIB_VENV_PY), "-c", "import qlib; print(qlib.__version__)"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=300,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        installed = proc.stdout.strip()
        pinned = [
            ln.split("#")[0].strip()
            for ln in REQS.read_text(encoding="utf-8").splitlines()
            if ln.strip().startswith("pyqlib==")
        ]
        assert pinned, "requirements-qlib.txt 没钉 pyqlib 版本，岛里的版本会随时间漂"
        assert installed == pinned[0].split("==")[1], (
            f"岛里是 qlib {installed}，清单钉的是 {pinned[0].split('==')[1]}：表达式口径会不一致"
        )
        return
    if not _conda_available():
        pytest.skip("本机既没有 conda 也没有 venv-qlib/：岛建不出来也无从验证（CI 就是这种情形）")
    # 有 conda 却没岛 ⇒ 至少「怎么建」必须在，而且要被 AGENTS.md 指到，否则下一个人会用主 venv。
    assert INSTALLER.exists(), f"缺 {INSTALLER.relative_to(ROOT)}：岛没有任何可复现的建法"
    assert INSTALLER.stat().st_mode & 0o111, "install_qlib_env.sh 没有执行位"
    text = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert "venv-qlib" in text and INSTALLER.name in text, (
        "AGENTS.md 没写「qlib 相关一律走 venv-qlib/bin/python」并指向安装脚本"
    )


def test_scanner_really_catches_a_qlib_import(tmp_path):
    """防空转：前两条靠扫描/子进程判定，得证明扫描器本身抓得住违规。"""
    (tmp_path / "bad.py").write_text("import qlib\nfrom qlib.contrib.data.loader import Alpha158DL\n", encoding="utf-8")
    (tmp_path / "ok.py").write_text(
        '"""文档里提一句 qlib：from qlib import x 只是说明文字"""\n'
        "# import qlib 也是注释\n"
        "import numpy\n",
        encoding="utf-8",
    )
    assert qlib_import_lines(tmp_path / "bad.py") == [1, 2]
    assert qlib_import_lines(tmp_path / "ok.py") == []
