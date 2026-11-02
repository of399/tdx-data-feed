"""名单必须只有一个来源：**路径**单一 + **实现**数量 = 1。

关联台账条目：**KI-SINGLE-005**（路径层）/ **KI-SINGLE-003**（实现层，P1-9 名字/池单源）。

规则（AST，非正则，避免注释与文档字符串误伤）：

**路径层**（R1–R3，沿用原守卫）
  R1 任意调用的直接实参里出现含 all_a_stocks.json 的字面量；
  R2 任何 `a / b` 拼接里出现该字面量；
  R3 赋值/返回的字面量（≤160 字符）里出现该字面量。
  白名单只有两个路径权威层（v5 是零耦合孪生）。

**实现层**（R4–R5，本次新增）
  R4（行为）：除权威层外，**任何代码都不得读取名单文件**——直接
     `universe_path().read_text()`、把 `universe_path()` 存进变量再 `open()/read_text()`、
     在函数体里做同样的事，一律违规。这条管的是「读取行为」，与函数叫什么名字无关：
     改名躲不过去。
  R5（入口）：`load_universe` / `universe_codes` 这类公开入口的**函数定义**只允许出现在
     权威层（历史上 `load_universe` 有 8 份实现、返回类型还分裂成 dict/list）。

**两个依赖域，各一份实现**：
- 主仓（scripts / tdxfeed / mcp / quant）：权威层 = `tdxfeed/names.py`（`load_universe` +
  `universe_codes`）。
- v5：按 `AGENTS.md` 的约定是**零耦合孪生**（不 import tdxfeed），它自己那一份读取留在
  `v5/scripts/scan_double.py`；守卫同时断言 v5 域内也只有这一处（不许第二份）。
"""
from __future__ import annotations

import ast
from pathlib import Path

# 本模块只读源码文本，不读 data/；docstring 含 data/ 字样会被根 conftest 误判，故显式退出。
NEEDS_DATA = False

ROOT = Path(__file__).resolve().parents[1]
MARK = "all_a_stocks.json"
WHITELIST = {"tdxfeed/paths.py", "v5/scripts/_paths.py"}
SKIP_DIRS = {"venv", "venv-math", "_archive", "node_modules", ".git", ".qoder", "tests"}

# ---- 实现层：每个依赖域的权威文件（唯一允许读名单 / 定义入口的地方） ----
# 主仓权威层：股票池加载与代码枚举的唯一入口。
MAIN_AUTHORITY = {"tdxfeed/names.py"}
# v5 是零耦合孪生（AGENTS.md），它自己那一份读取保留在 v5 侧；域内同样只允许这一处。
V5_AUTHORITY = {"v5/scripts/scan_double.py"}

# 各依赖域的源码根
DOMAINS = {
    "main": (("scripts", "tdxfeed", "mcp", "quant"), MAIN_AUTHORITY),
    "v5": (("v5/scripts",), V5_AUTHORITY),
}

# 公开入口名（去掉前导下划线后比对）：这些名字只允许在权威层里定义
ENTRY_NAMES = {"load_universe", "universe_codes", "load_names", "universe"}


def _strs(node: ast.AST) -> list[str]:
    return [n.value for n in ast.walk(node)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)]


def _suspect(node: ast.AST) -> bool:
    return any(MARK in v for v in _strs(node))


def violations(path: Path) -> list[int]:
    """路径层违规行号（R1–R3）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    lines: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            # f-string 不在其列：错误消息里提到文件名是说明文字，不是路径拼接。
            direct = [a for a in [*node.args, *node.keywords]
                      if isinstance(a, (ast.Constant, ast.BinOp))]
            if any(_suspect(a) for a in direct):
                lines.add(node.lineno)
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            if _suspect(node):
                lines.add(node.lineno)
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.Return)):
            value = getattr(node, "value", None)
            if not isinstance(value, (ast.Constant, ast.JoinedStr)):
                continue
            text = "".join(_strs(value))
            if "\n" in text or len(text) > 160:
                continue
            if _suspect(value):
                lines.add(node.lineno)
    return sorted(lines)


# --------------------------------------------------------------------------- #
# 实现层（R4–R5）
# --------------------------------------------------------------------------- #
def _is_universe_path_call(node: ast.AST) -> bool:
    """是否是 `universe_path()` 调用（兼容 `paths.universe_path()` 形态）。"""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Attribute) and func.attr == "universe_path":
        return True
    return isinstance(func, ast.Name) and func.id == "universe_path"


def _contains_universe_path(node: ast.AST) -> bool:
    return any(_is_universe_path_call(n) for n in ast.walk(node))


def _universe_aliases(tree: ast.AST) -> set[str]:
    """把 `X = universe_path()` 里的 X 收集成「名单路径别名」。

    存进变量再读是最常见的绕过写法（`ALL_STOCKS = universe_path()` 然后 `open(ALL_STOCKS)`），
    所以别名也要认。
    """
    out: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        value = node.value
        if value is None or not _contains_universe_path(value):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for t in targets:
            if isinstance(t, ast.Name):
                out.add(t.id)
            elif isinstance(t, ast.Attribute):
                out.add(t.attr)
    return out


def _mentions_alias(node: ast.AST, aliases: set[str]) -> bool:
    if not aliases:
        return False
    return any(isinstance(n, ast.Name) and n.id in aliases for n in ast.walk(node))


def read_violations(path: Path) -> list[int]:
    """R4：读取名单文件的行为（行号）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    aliases = _universe_aliases(tree)
    lines: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        # `X.read_text()` / `X.read_bytes()`
        if isinstance(func, ast.Attribute) and func.attr in ("read_text", "read_bytes"):
            recv = func.value
            if _contains_universe_path(recv) or _mentions_alias(recv, aliases):
                lines.add(node.lineno)
        # `open(X)`
        elif isinstance(func, ast.Name) and func.id == "open":
            for arg in node.args:
                if _contains_universe_path(arg) or _mentions_alias(arg, aliases):
                    lines.add(node.lineno)
                    break
    return sorted(lines)


def entry_violations(path: Path) -> list[int]:
    """R5：股票池公开入口的函数定义（行号）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    lines: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and \
                node.name.lstrip("_").lower() in ENTRY_NAMES:
            lines.add(node.lineno)
    return sorted(lines)


# --------------------------------------------------------------------------- #
# 全仓扫描
# --------------------------------------------------------------------------- #
def _domain_files(domains: tuple[str, ...], root: Path = ROOT) -> list[Path]:
    out: list[Path] = []
    for base in domains:
        p = root / base
        if not p.is_dir():
            continue
        out += [f for f in sorted(p.rglob("*.py")) if f.is_file()]
    return out


def scan(root: Path = ROOT) -> dict[str, list[int]]:
    """路径层扫描（沿用原语义）。"""
    bad: dict[str, list[int]] = {}
    for p in sorted(root.rglob("*.py")):
        rel = p.relative_to(root)
        if any(part in SKIP_DIRS for part in rel.parts) or rel.as_posix() in WHITELIST:
            continue
        try:
            found = violations(p)
        except SyntaxError:
            continue
        if found:
            bad[rel.as_posix()] = found
    return bad


def scan_implementations(domains: dict[str, tuple[tuple[str, ...], set[str]]] | None = None,
                         root: Path = ROOT) -> dict[str, list[str]]:
    """实现层扫描：返回 {相对路径: [违规原因, ...]}。

    Args:
        domains: 依赖域定义（源码根 + 该域的权威文件），默认用仓库自身的。
        root: 扫描根，默认仓库根；自检用例会指向临时目录。
    """
    doms = DOMAINS if domains is None else domains
    bad: dict[str, list[str]] = {}
    for _domain, (roots, authority) in doms.items():
        for p in _domain_files(roots, root):
            rel_path = p.relative_to(root)
            if any(part in SKIP_DIRS for part in rel_path.parts):
                continue
            rel = rel_path.as_posix()
            if rel in authority:
                continue
            reasons: list[str] = []
            try:
                reads = read_violations(p)
                entries = entry_violations(p)
            except SyntaxError:
                continue
            if reads:
                reasons.append(f"L{reads[0]} 读取名单文件")
            if entries:
                reasons.append(f"L{entries[0]} 定义股票池入口函数")
            if reasons:
                bad[rel] = reasons
    return bad


# --------------------------------------------------------------------------- #
# 用例
# --------------------------------------------------------------------------- #
def test_no_self_concatenated_universe_path():
    bad = scan()
    rows = [f"{f}  {lines}" for f, lines in sorted(bad.items())]
    assert not bad, "以下文件自拼名单路径，应改用 paths.universe_path()：\n" + "\n".join(rows)


def test_universe_has_exactly_one_implementation():
    """实现数量 = 1：除权威层外没人读名单、没人自定义股票池入口。"""
    bad = scan_implementations()
    rows = [f"{f}  {'; '.join(reasons)}" for f, reasons in sorted(bad.items())]
    assert not bad, (
        "股票池必须只有一个来源（tdxfeed.names）：以下文件仍在自读名单或自定义入口 ——\n"
        + "\n".join(rows)
    )


def test_guard_catches_the_three_shapes(tmp_path):
    sample = tmp_path / "sample.py"
    sample.write_text(
        "from pathlib import Path\n"
        "ROOT = Path('/x')\n"
        "A = ROOT / 'data' / 'all_a_stocks.json'\n"
        "B = Path('/home/u/proj/data/all_a_stocks.json')\n"
        "C = 'all_a_stocks.json'\n",
        encoding="utf-8",
    )
    assert violations(sample) == [3, 4, 5]


def test_guard_ignores_comments_and_docstrings(tmp_path):
    sample = tmp_path / "ok.py"
    sample.write_text(
        '"""名单在 data/all_a_stocks.json 下（只是说明文字）。"""\n'
        "# all_a_stocks.json 也是\n"
        "META = {'note': 'data/all_a_stocks.json'}\n",
        encoding="utf-8",
    )
    assert violations(sample) == []


def test_guard_whitelists_the_authority_modules():
    for rel in WHITELIST:
        assert (ROOT / rel).exists(), rel


# --------------------------------------------------------------------------- #
# 反例必红：反例样本必须被判违规
# --------------------------------------------------------------------------- #
def test_read_guard_catches_direct_and_aliased_reads(tmp_path):
    direct = tmp_path / "direct.py"
    direct.write_text(
        "from tdxfeed.paths import universe_path\n"
        "raw = universe_path().read_text(encoding='utf-8')\n",
        encoding="utf-8",
    )
    assert read_violations(direct) == [2], "直接读名单文件必须被抓"

    aliased = tmp_path / "aliased.py"
    aliased.write_text(
        "from tdxfeed.paths import universe_path\n"
        "ALL = universe_path()\n"
        "with open(ALL) as fh:\n"
        "    data = fh.read()\n",
        encoding="utf-8",
    )
    assert read_violations(aliased) == [3], "别名后读取名单文件必须被抓"


def test_entry_guard_catches_duplicate_definitions(tmp_path):
    dup = tmp_path / "dup.py"
    dup.write_text(
        "def load_universe():\n"
        "    return {}\n",
        encoding="utf-8",
    )
    assert entry_violations(dup) == [1], "第二份 load_universe 定义必须被抓"

    nested = tmp_path / "nested.py"
    nested.write_text(
        "def _load_universe():\n"
        "    return {}\n"
        "def universe_codes():\n"
        "    return []\n",
        encoding="utf-8",
    )
    assert entry_violations(nested) == [1, 3], "改名藏起来的入口也必须被抓"


def test_guard_accepts_the_single_source_usage(tmp_path):
    """合规用法：只导入并调用权威入口 → 不算违规（别把正常收编判红）。"""
    good = tmp_path / "good.py"
    good.write_text(
        "from tdxfeed.names import load_universe, universe_codes\n"
        "def main():\n"
        "    mapping = load_universe()\n"
        "    codes = universe_codes(prefixes=('60',))\n"
        "    return mapping, codes\n",
        encoding="utf-8",
    )
    assert read_violations(good) == []
    assert entry_violations(good) == []


def test_authority_modules_are_registered():
    """权威层必须真的存在（登记了不存在的文件 = 守卫放行一切）。"""
    for rel in MAIN_AUTHORITY | V5_AUTHORITY:
        assert (ROOT / rel).exists(), rel


def test_scan_implementations_flags_a_second_reader(tmp_path):
    """端到端自检：扫描域里放进一份「第二实现」，扫描器必须报出来。

    只测探测器不够——探测器认得出来、但扫描器没把它算进去，守卫照样是哑的。
    """
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "ok.py").write_text(
        "from tdxfeed.names import load_universe, universe_codes\n"
        "def main():\n"
        "    return load_universe(), universe_codes()\n",
        encoding="utf-8",
    )
    (pkg / "bad.py").write_text(
        "import json\n"
        "from tdxfeed.paths import universe_path\n"
        "\n"
        "\n"
        "def load_universe():\n"
        "    return json.loads(universe_path().read_text(encoding='utf-8'))\n",
        encoding="utf-8",
    )
    bad = scan_implementations(domains={"probe": (("pkg",), set())}, root=tmp_path)
    assert "pkg/bad.py" in bad, "第二份实现必须被扫描器报出来"
    assert "pkg/ok.py" not in bad, "合规调用唯一入口不该被误报"
