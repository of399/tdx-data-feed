"""ST 判定与代码归一的**单一来源**守卫（P1-9 批次 D/E）。

关联台账条目：**KI-SINGLE-001**（代码归一实现分散）、**KI-SINGLE-002**（ST 判定语义分散）。

三条校验目标：

1. **ST 名称判定的实现唯一** —— 权威 = `tdxfeed/names.py::is_st_name`。
   - *函数层*：除权威层与显式登记的例外外，全仓不得再定义 ST 判定函数；
     登记为「转发」的例外必须真的转发（函数体里调用 `is_st_name`），否则名不副实。
   - *内联层*：除权威层与显式登记的例外外，不得出现 ST 字符串判定
     （`"ST" in ...` / `startswith("ST")` 一类）；例外逐条写明理由，
     **新增未登记的必红**。
2. **代码归一的入口唯一** —— 权威 = `tdxfeed/execution/limits.py`，只有两个契约入口：
   - `normalize_code`（**契约 A**：CLI 用户输入 → lower + 剥前缀 + 零填充 6 位）；
   - `strip_exchange_prefix`（**契约 B**：内部生成的文件名/裸代码 → 只剥前缀）。
     全仓不得再有第三份实现（连 `norm_code` 这个名字也不许再定义）。
3. **两个契约共用一条底层** —— `normalize_code` 必须调用 `strip_exchange_prefix`，
   而不是各自复制一份前缀剥离逻辑（"两种契约、一份实现"的落点）。

契约为什么不能合并（审计 Round2 2026-10-18 注记："契约不同, 勿盲目统一"）：
契约 B 喂的是内部生成的文件名（`sh600000`），要与路径/名称映射逐字对齐；
契约 A 喂的是用户随手输入的 `600519`。若只留一个，另一个的调用点会静默改口径
（`"1"` 在 A 下变 `"000001"`，在 B 下保持 `"1"`）。
"""
from __future__ import annotations

import ast
import inspect
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import predict  # noqa: E402

from tdxfeed.names import is_st_name  # noqa: E402

# 只读源码文本做 AST 扫描，不读 data/；docstring 含 data/ 字样会被根 conftest 误判，故显式退出。
NEEDS_DATA = False

# ---- 权威层 ----
ST_AUTHORITY = "tdxfeed/names.py"
NORM_AUTHORITY = "tdxfeed/execution/limits.py"

# 扫描域（v5 是零耦合孪生、不 import tdxfeed，与股票池守卫同口径排除）
DOMAINS = ("scripts", "tdxfeed", "mcp", "quant")
SKIP_DIRS = {"venv", "venv-math", "_archive", "node_modules", ".git", ".qoder", "tests", "v5"}

# ST 判定函数的命名（去掉前导下划线后比对）
ST_DEF_NAMES = {"is_st", "is_st_name", "is_st_name_of"}

# ST 判定的**函数定义**例外（权威层之外）：{相对路径: (种类, 理由)}
# 种类 "forward" = 转发权威实现（会被断言"函数体里真的调用了 is_st_name"）；
# 种类 "other"   = 另一契约（不是名称判定的副本），不检查转发。
ST_DEF_EXCEPTIONS = {
    "scripts/predict.py": ("forward", "int 契约的历史接口（predict.is_st(name) -> int），转发 names.is_st_name"),
    "tdxfeed/daban/limit_price.py": ("forward", "对外别名（limit_price.is_st(name) -> bool），转发 names.is_st_name"),
    "scripts/find_double_up_c_v2.py": ("other", "is_st(code, st_set) 判的是「代码在不在 ST 名单里」，不是名称判定"),
    "tdxfeed/st_supply.py": ("other", "StSupply.is_st(code, on_date) 判的是「日期化面板的权威/推断区间是否覆盖那一天」，"
                                      "**签名里没有 name、函数体里不调 is_st_name**（B1，2026-11-05）⇒ 不是名称判定的副本。"
                                      "这条例外的真实性由 tests/test_st_supply_in_features.py::"
                                      "test_st_supply_is_st_is_not_a_name_judgement 反向钉住"),
}

# ST 判定的**内联字符串判定**例外：{相对路径: 理由}
ST_INLINE_EXCEPTIONS = {
    "scripts/find_double_30d.py": "口径与权威**不同**：这里只匹配前缀（startswith），权威是「任意位置含 ST」；"
                                  "改口径会影响翻倍样本，故保持原样待后续批次",
    "scripts/factor_engine.py": "判定对象是 vault 文件名词干(md.stem)，不是股票名",
    "scripts/find_double_up_c_v2.py": "同上：扫描 vault 文件名词干",
}

# 代码归一的**函数定义**例外：{相对路径: (种类, 理由)}
NORM_DEF_EXCEPTIONS = {
    "tdxfeed/daban/limit_price.py": ("forward", "对外别名（limit_price.normalize_code），转发 limits.normalize_code"),
    "mcp/servers/backtest.py": ("other", "_norm_code(code) -> (sym, six)：symbol 解析 + 日K存在性校验，"
                                        "内部剥前缀那一步与权威同源，但不是归一副本"),
    "mcp/servers/finance_deep.py": ("other", "同上：symbol 解析器"),
    "mcp/servers/news.py": ("other", "同上：symbol 解析器（返回 (sym, six)）"),
    "mcp/servers/parquet_reader.py": ("other", "_norm_code(code) -> str：symbol 归一 + 路径安全校验（KI-MCP-003），"
                                            "拒绝 `../`、绝对路径、`/`"),
    "scripts/akshare_industry_loader.py": ("other", "normalize_code(raw) 是**反向**映射：裸 6 位码 → 带交易所前缀的"
                                                    "内部格式（'000001' → 'sz000001'），与「剥前缀」方向相反"),
    "scripts/tdx_concept_loader.py": ("other", "_normalize_code(raw) 清洗通达信 7 字节 utf-8 脏输入（去 \\x00），"
                                            "还可能带指数前缀（1A0001），不是归一副本"),
}

NORM_ENTRY_NAMES = {"normalize_code", "strip_exchange_prefix"}
# 曾经出现过的第三种命名——留着就是又一份实现的入口
LEGACY_NORM_NAMES = {"norm_code"}


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
def _rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def _domain_files(domains: tuple[str, ...] = DOMAINS, root: Path = ROOT) -> list[Path]:
    out: list[Path] = []
    for base in domains:
        p = root / base
        if not p.is_dir():
            continue
        out += [f for f in sorted(p.rglob("*.py")) if f.is_file()]
    return out


def scan_st_definitions(domains: tuple[str, ...] = DOMAINS, root: Path = ROOT,
                        exceptions: dict[str, tuple[str, str]] | None = None) -> list[str]:
    """扫出「第二份 ST 判定函数定义」。"""
    exc = ST_DEF_EXCEPTIONS if exceptions is None else exceptions
    problems: list[str] = []
    for fp in _domain_files(domains, root):
        rel = fp.relative_to(root).as_posix()
        if rel == ST_AUTHORITY or any(part in SKIP_DIRS for part in fp.relative_to(root).parts):
            continue
        try:
            tree = ast.parse(fp.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for name, lineno in _st_defs(tree):
            if rel not in exc:
                problems.append(f"{rel}:{lineno} 定义了 ST 判定函数 {name}")
    return problems


def scan_st_inline(domains: tuple[str, ...] = DOMAINS, root: Path = ROOT,
                    exceptions: dict[str, str] | None = None) -> list[str]:
    """扫出「未登记的内联 ST 字符串判定」。"""
    exc = ST_INLINE_EXCEPTIONS if exceptions is None else exceptions
    problems: list[str] = []
    for fp in _domain_files(domains, root):
        rel = fp.relative_to(root).as_posix()
        if rel == ST_AUTHORITY or any(part in SKIP_DIRS for part in fp.relative_to(root).parts):
            continue
        try:
            tree = ast.parse(fp.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for lineno in _st_inline(tree):
            if rel not in exc:
                problems.append(f"{rel}:{lineno} 出现未登记的内联 ST 判定")
    return problems


def scan_norm_definitions(domains: tuple[str, ...] = DOMAINS, root: Path = ROOT,
                          exceptions: dict[str, tuple[str, str]] | None = None) -> list[str]:
    """扫出「第三份代码归一实现」。"""
    exc = NORM_DEF_EXCEPTIONS if exceptions is None else exceptions
    problems: list[str] = []
    for fp in _domain_files(domains, root):
        rel = fp.relative_to(root).as_posix()
        if any(part in SKIP_DIRS for part in fp.relative_to(root).parts):
            continue
        try:
            tree = ast.parse(fp.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for name, lineno in _norm_defs(tree):
            if rel == NORM_AUTHORITY or rel in exc:
                continue
            problems.append(f"{rel}:{lineno} 定义了代码归一函数 {name}")
    return problems


def _st_defs(tree: ast.AST) -> list[tuple[str, int]]:
    """ST 判定函数定义：[(函数名, 行号)]。"""
    out = []
    for node in ast.walk(tree):
        if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name.lstrip("_").lower() in ST_DEF_NAMES):
            out.append((node.name, node.lineno))
    return out


def _is_st_marker(value: str) -> bool:
    """这个字符串常量**就是** ST 标记（`ST` / `*ST`）。

    必须精确匹配，不能用「含 ST 子串」——那会把 `"tests/"`、`"state"`、`"step"`、
    `"OBSIDIAN_SYSTEMD:"`、`"inst_net"` 之类全判成 ST 判定（实测误报 9 处）。
    """
    return value.strip().upper() in ("ST", "*ST")


def _st_inline(tree: ast.AST) -> list[int]:
    """内联 ST 字符串判定的行号。

    形态：`"ST" in X` / `"*ST" in X` / `X.startswith("ST")` / `X.endswith("ST")`。
    """
    lines: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            for op in node.ops:
                if not isinstance(op, (ast.In, ast.NotIn)):
                    continue
                left = node.left
                if isinstance(left, ast.Constant) and isinstance(left.value, str) \
                        and _is_st_marker(left.value):
                    lines.add(node.lineno)
                    break
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr not in ("startswith", "endswith"):
                continue
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str) \
                        and _is_st_marker(arg.value):
                    lines.add(node.lineno)
                    break
    return sorted(lines)


def _norm_defs(tree: ast.AST) -> list[tuple[str, int]]:
    """代码归一相关函数定义：[(函数名, 行号)]（含历史第三种命名）。"""
    out = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            name = node.name.lstrip("_").lower()
            if name in NORM_ENTRY_NAMES or name in LEGACY_NORM_NAMES:
                out.append((node.name, node.lineno))
    return out


def _calls_authority(node: ast.AST, name: str, aliases: tuple[str, ...] = ()) -> bool:
    """函数体里是否真的调用了 `name` 或其导入别名（转发检查）。"""
    wanted = {name, *aliases}
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            func = sub.func
            if isinstance(func, ast.Name) and func.id in wanted:
                return True
            if isinstance(func, ast.Attribute) and func.attr in wanted:
                return True
    return False


def _find_func(tree: ast.AST, name: str) -> ast.AST | None:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


# --------------------------------------------------------------------------- #
# 1. ST 名称判定：实现唯一
# --------------------------------------------------------------------------- #
def test_predict_is_st_delegates_to_names():
    src = inspect.getsource(predict.is_st)
    assert "is_st_name(" in src
    assert "'ST' in" not in src


def test_predict_is_st_keeps_int_contract():
    assert predict.is_st("平安银行") == 0
    assert predict.is_st("*ST美丽") == 1
    assert predict.is_st(None) == 0


def test_predict_agrees_with_authority():
    for name in ("平安银行", "ST 康佳", "*ST美丽", "", None):
        assert predict.is_st(name) == int(is_st_name(name)), name


def test_run_prediction_has_no_inline_st_check():
    """run_prediction 的实时名称兜底也必须走 names 单一来源。"""
    src = inspect.getsource(predict.run_prediction)
    assert "is_st_name(" in src
    assert "'ST' in" not in src


def test_st_name_judgement_has_one_implementation():
    """除权威层与登记例外外，全仓不得再定义 ST 判定函数。"""
    problems = scan_st_definitions()
    assert not problems, "ST 名称判定只能有一处实现（tdxfeed/names.py::is_st_name）：\n" + "\n".join(problems)


def test_st_forwarding_exceptions_actually_forward():
    """登记为「转发」的例外必须真的转发，否则名不副实。"""
    problems: list[str] = []
    for rel, (kind, _why) in ST_DEF_EXCEPTIONS.items():
        if kind != "forward":
            continue
        fp = ROOT / rel
        tree = ast.parse(fp.read_text(encoding="utf-8"))
        node = _find_func(tree, "is_st")
        if node is None:
            problems.append(f"{rel}: 例外登记了 is_st，但文件里没有这个函数")
            continue
        if not _calls_authority(node, "is_st_name", aliases=("_is_st_name",)):
            problems.append(f"{rel}: is_st 登记为转发，但函数体里没有调用 is_st_name")
    assert not problems, "转发例外名不副实：\n" + "\n".join(problems)


def test_st_inline_judgements_are_registered():
    """内联 ST 字符串判定必须逐条登记理由——新增未登记的必红。"""
    problems = scan_st_inline()
    assert not problems, (
        "内联 ST 判定必须走 names.is_st_name，或在 ST_INLINE_EXCEPTIONS 里登记理由：\n"
        + "\n".join(problems))


# --------------------------------------------------------------------------- #
# 2. 代码归一：两个入口唯一 + 共用一条底层
# --------------------------------------------------------------------------- #
def test_code_normalisation_has_only_two_entry_points():
    """两个契约入口只在权威层；`norm_code` 这个第三种命名不再允许出现。"""
    problems = scan_norm_definitions()
    assert not problems, (
        "代码归一只能有 limits.normalize_code / limits.strip_exchange_prefix 两个入口：\n"
        + "\n".join(problems))


def test_both_contracts_share_one_implementation():
    """两个契约共用一条底层：`normalize_code` 必须调用 `strip_exchange_prefix`。"""
    fp = ROOT / NORM_AUTHORITY
    tree = ast.parse(fp.read_text(encoding="utf-8"))
    normalize = _find_func(tree, "normalize_code")
    strip = _find_func(tree, "strip_exchange_prefix")
    assert normalize is not None, "权威层缺少 normalize_code"
    assert strip is not None, "权威层缺少 strip_exchange_prefix"
    assert _calls_authority(normalize, "strip_exchange_prefix"), (
        "normalize_code 必须复用 strip_exchange_prefix 的底层逻辑，不能各写一份前缀剥离")
    # 底层剥离逻辑只有一处：strip_exchange_prefix 里那一次正则替换
    strip_src = ast.unparse(strip)
    assert strip_src.count("_EXCHANGE_PREFIX_RE") == 1, (
        "前缀剥离的正则只能出现在 strip_exchange_prefix 一处：\n" + strip_src)


def test_two_contracts_keep_their_own_semantics():
    """两个入口的**契约差异**必须保住（勿盲目统一）。"""
    from tdxfeed.execution.limits import normalize_code, strip_exchange_prefix
    # 契约 A：规范化（lower + 剥前缀 + 零填充 6 位）
    assert normalize_code("sh600000") == "600000"
    assert normalize_code(" 600519 ") == "600519"
    assert normalize_code("SH600000") == "600000"
    assert normalize_code("1") == "000001"
    # 契约 B：只剥前缀，其余原样
    assert strip_exchange_prefix("sh600000") == "600000"
    assert strip_exchange_prefix("SH600000") == "SH600000"
    assert strip_exchange_prefix(" 600519 ") == " 600519 "
    assert strip_exchange_prefix("1") == "1"


def test_common_still_exposes_norm_code_alias():
    """`scripts/_common.norm_code` 仍在（大量脚本在用），但它必须是权威实现的别名而非副本。"""
    import _common

    from tdxfeed.execution import limits
    assert _common.norm_code is limits.normalize_code, (
        "_common.norm_code 必须是 limits.normalize_code 的别名（不要再包一层）")


# --------------------------------------------------------------------------- #
# 3. 反例必红
# --------------------------------------------------------------------------- #
def test_guard_catches_a_duplicate_st_implementation(tmp_path):
    dup = tmp_path / "dup_st.py"
    dup.write_text(
        "def is_st(name: str) -> bool:\n"
        "    return 'ST' in name.upper()\n",
        encoding="utf-8",
    )
    tree = ast.parse(dup.read_text(encoding="utf-8"))
    assert _st_defs(tree) == [("is_st", 1)], "第二份 ST 实现必须被识别出来"
    assert _st_inline(tree) == [2], "内联 ST 判定必须被识别出来"


def test_guard_catches_a_duplicate_normalisation(tmp_path):
    dup = tmp_path / "dup_norm.py"
    dup.write_text(
        "import re\n"
        "def norm_code(code: str) -> str:\n"
        "    return re.sub(r'^(sh|sz|bj)', '', code)\n",
        encoding="utf-8",
    )
    tree = ast.parse(dup.read_text(encoding="utf-8"))
    assert _norm_defs(tree) == [("norm_code", 2)], "第三种归一实现必须被识别出来"


def test_guard_ignores_comments_and_docstrings(tmp_path):
    sample = tmp_path / "ok.py"
    sample.write_text(
        '"""这里写 ST 判定说明，只是文字。"""\n'
        "# 也不是 'ST' in name\n"
        "VALUE = 1\n",
        encoding="utf-8",
    )
    tree = ast.parse(sample.read_text(encoding="utf-8"))
    assert _st_defs(tree) == []
    assert _st_inline(tree) == []
    assert _norm_defs(tree) == []


def test_exception_tables_are_not_stale():
    """例外表不得腐化：登记了却已经没有对应命中 = 代码变了，该重新评估这条例外。

    没有这条，例外表会变成「什么都往里塞也不报错」的垃圾桶——那正是它当初要防的东西。
    """
    problems: list[str] = []
    for rel in ST_INLINE_EXCEPTIONS:
        fp = ROOT / rel
        if not fp.exists():
            problems.append(f"{rel}: 例外登记的文件已不存在")
            continue
        tree = ast.parse(fp.read_text(encoding="utf-8"))
        if not _st_inline(tree):
            problems.append(f"{rel}: 例外已失效（文件里没有内联 ST 判定了）——请重新评估并删除该例外")
    for rel, (_kind, _why) in ST_DEF_EXCEPTIONS.items():
        fp = ROOT / rel
        if not fp.exists():
            problems.append(f"{rel}: 例外登记的文件已不存在")
            continue
        tree = ast.parse(fp.read_text(encoding="utf-8"))
        if not _st_defs(tree):
            problems.append(f"{rel}: 例外已失效（文件里没有 ST 判定函数了）——请重新评估并删除该例外")
    for rel in NORM_DEF_EXCEPTIONS:
        fp = ROOT / rel
        if not fp.exists():
            problems.append(f"{rel}: 例外登记的文件已不存在")
            continue
        tree = ast.parse(fp.read_text(encoding="utf-8"))
        if not _norm_defs(tree):
            problems.append(f"{rel}: 例外已失效（文件里没有归一函数了）——请重新评估并删除该例外")
    assert not problems, "例外表腐化：\n" + "\n".join(problems)


def test_scanners_catch_a_second_implementation(tmp_path):
    """端到端自检：第二份实现放进扫描域，三个扫描器都必须报出来。

    只测探测器不够——探测器认得出来、但扫描器没把它算进去，守卫照样是哑的。
    """
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "bad_norm.py").write_text(
        "import re\n"
        "def norm_code(code: str) -> str:\n"
        "    return re.sub(r'^(sh|sz|bj)', '', code)\n",
        encoding="utf-8",
    )
    (pkg / "bad_st.py").write_text(
        "def is_st(name: str) -> bool:\n"
        "    return 'ST' in name.upper()\n",
        encoding="utf-8",
    )
    (pkg / "bad_inline.py").write_text(
        "def flag(name: str) -> bool:\n"
        "    return 'ST' in name\n",
        encoding="utf-8",
    )
    (pkg / "ok.py").write_text(
        "from tdxfeed.names import is_st_name\n"
        "from tdxfeed.execution.limits import normalize_code, strip_exchange_prefix\n",
        encoding="utf-8",
    )
    dom = ("pkg",)
    assert any("bad_norm.py" in p for p in scan_norm_definitions(dom, tmp_path)), \
        "第二份归一实现必须被扫描器报出来"
    assert any("bad_st.py" in p for p in scan_st_definitions(dom, tmp_path)), \
        "第二份 ST 实现必须被扫描器报出来"
    assert any("bad_inline.py" in p for p in scan_st_inline(dom, tmp_path)), \
        "未登记的内联 ST 判定必须被扫描器报出来"
    assert not any("ok.py" in p for p in scan_norm_definitions(dom, tmp_path))
    assert not any("ok.py" in p for p in scan_st_definitions(dom, tmp_path))
    assert not any("ok.py" in p for p in scan_st_inline(dom, tmp_path))
