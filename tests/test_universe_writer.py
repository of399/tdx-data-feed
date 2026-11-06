"""`all_a_stocks.json` 的**写入侧**守卫（补 D6 类失效引用：读侧一直声称有生产者）。

关联台账：KI-SINGLE-003（股票池路径访问器已单源化）的**另一半天**——那一半管住「谁读」，
本模块管住「谁写」。四条判据：

1. **写入者唯一且在场**：全仓只有 `scripts/update_universe.py` 写这份名单。
   判据是「源码里同时出现名单定位（`universe_path` / 文件名）与写盘动词」，
   不是数注释里的字样。
2. **读侧声称的生产者必须真实**：`tdxfeed/names.py` 的 docstring 一旦写出
   「由 `scripts/xxx.py` 更新」，那个文件就必须存在且真的是写入者——
   这条专治「假声明」（此前写着 `scripts/download_universe.py` 每日更新，
   而那个脚本只写日线、一个字的名单都不写）。
3. **读侧契约不坏**：路径访问器与两个入口（`load_universe` / `universe_codes`）的
   签名与默认行为不许被写入者顺带动到。
4. **取名通道不新增**：写入者只能走仓库已有的那一个入口（`akshare.stock_info_a_code_name`，
   与 `tdxfeed/full_sync.py` / `tdxfeed/symbols.py` 同源），不许另起炉灶。

只读源码文本 + tmp 目录里的假名单，**不碰真 `data/`**（`NEEDS_DATA = False`）。
"""
from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

NEEDS_DATA = False

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tdxfeed import names as _names  # noqa: E402

UNIVERSE_FILE = "all_a_stocks.json"
WRITER = "scripts/update_universe.py"
EXPECTED_SOURCE_CALL = "stock_info_a_code_name"
# 扫描域与 tests/test_universe_path_single_source.py 同口径（v5 是零耦合孪生，只读不写）
DOMAINS = ("scripts", "tdxfeed", "mcp", "quant", "v5")
SKIP_DIRS = {"venv", "venv-math", "venv-qlib", "_archive", "node_modules", ".git", ".qoder"}

WRITE_VERBS = {"write_text", "write_bytes"}


def _py_files() -> list[Path]:
    out: list[Path] = []
    for d in DOMAINS:
        base = ROOT / d
        if not base.is_dir():
            continue
        for p in sorted(base.rglob("*.py")):
            if SKIP_DIRS & set(p.parts):
                continue
            out.append(p)
    return out


def _docstring_nodes(tree: ast.AST) -> set[int]:
    """docstring 节点的 id 集合：说明文字里提到文件名不算「定位到名单」。"""
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(n, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                out.add(id(body[0].value))
    return out


def _targets_universe(tree: ast.AST) -> bool:
    """这棵树上有没有出现「名单定位」：`universe_path()` 调用，或代码里的文件名常量。"""
    docs = _docstring_nodes(tree)
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "universe_path":
            return True
        if id(n) in docs:
            continue
        if isinstance(n, ast.Constant) and n.value == UNIVERSE_FILE:
            return True
    return False


def _has_write(tree: ast.AST) -> bool:
    """写盘动词：`x.write_text(...)` / `x.atomic_write_*(...)` / `json.dump(...)` /
    `os.replace(...)` / `open(..., "w")`。

    `tdxfeed.atomic` 是本仓库原子写的唯一出口，所以它也算写盘动词——不然「改用共享出口」
    反而会让写入者从这份清单里消失（判据不能奖励绕行）。

    只认这几个真写盘动作。`str.replace` / `df.replace` 与 `os.replace` 同名不同事，
    收进来会把读侧模块判成写入者（本轮实踩过：`names.py` 里那句
    `str(name).replace(" ", "")` 让唯一的读侧实现看起来像在写名单）。
    """
    for n in ast.walk(tree):
        if not isinstance(n, ast.Call):
            continue
        attr = getattr(n.func, "attr", "")
        base = n.func.value if isinstance(n.func, ast.Attribute) else None
        base_name = getattr(base, "id", "") if isinstance(base, ast.Name) else ""
        if attr in WRITE_VERBS or attr.startswith("atomic_write") \
                or (attr == "dump" and base_name == "json") \
                or (attr == "replace" and base_name == "os") \
                or getattr(n.func, "id", "").startswith("atomic_write"):
            return True
        if getattr(n.func, "id", "") == "open" and any(
                isinstance(a, ast.Constant) and isinstance(a.value, str) and "w" in a.value
                for a in n.args[1:]):
            return True
    return False


def writer_modules() -> set[str]:
    """全仓「写名单」的模块集合（相对路径）。"""
    found: set[str] = set()
    for p in _py_files():
        rel = str(p.relative_to(ROOT))
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except (SyntaxError, OSError):
            continue
        if _targets_universe(tree) and _has_write(tree):
            found.add(rel)
    return found


CLAIM_RE = re.compile(r"scripts/([\w-]+\.py)`?\s*(?:每日)?(?:更新|刷新|维护)")


def claimed_writers(*texts: str) -> set[str]:
    """从说明文字里挑出「谁是生产者」的声明句。

    只认生产者句式（路径后紧跟 更新/刷新/维护），否则连「旧文案那句是假的」这种
    更正记录也会被当成声明，守卫就没法在文档里留下这次修正的来龙去脉。
    """
    return {f"scripts/{m}" for text in texts for m in CLAIM_RE.findall(text)}


def _load_writer():
    import importlib.util
    spec = importlib.util.spec_from_file_location("update_universe_guard", ROOT / WRITER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --------------------------------------------------------------------------- #
# 1) 写入者存在且唯一
# --------------------------------------------------------------------------- #
def test_universe_has_exactly_one_writer_and_it_is_declared() -> None:
    """判据：写名单的模块**恰好**是 `scripts/update_universe.py`。

    哪个生产改动会让它红：有人新写一个补名单的脚本 / 有人把写盘塞进
    `full_sync`（那会变成第二个写入者，谁维护最后一写就没人知道了）。
    """
    assert (ROOT / WRITER).is_file(), f"名单没有写入者：{WRITER} 不存在"
    assert writer_modules() == {WRITER}, (
        f"写入者不唯一或判据漏网：实测 {sorted(writer_modules())}，期望 {{{WRITER!r}}}")


# --------------------------------------------------------------------------- #
# 2) 读侧 docstring 的生产者声明必须为真
# --------------------------------------------------------------------------- #
def test_names_docstring_writer_claim_points_at_a_real_writer() -> None:
    """`tdxfeed/names.py` 里「名单文件由 `scripts/x.py` 更新」这句话必须可核验。

    反向判据：如果哪天写入者真没了，docstring 必须改成照实说「无生产者」，
    不许留着一句指向不存在写入者的声明（就是本轮要修的那条假声明）。
    """
    src = (ROOT / "tdxfeed" / "names.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    docs = [ast.get_docstring(tree) or ""]
    docs += [ast.get_docstring(n) or "" for n in ast.walk(tree)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
    claimed = claimed_writers(*docs)
    assert claimed, "names.py 不再声明生产者是谁 ⇒ 这一格失去可核验性"
    writers = writer_modules()
    for c in sorted(claimed):
        assert (ROOT / c).is_file(), f"names.py 声称由 {c} 更新，但该文件不存在"
    assert claimed <= writers, (
        f"names.py 声称的生产者 {sorted(claimed - writers)} 实际不写这份名单"
        f"（真写入者是 {sorted(writers)}）⇒ 假声明")


# --------------------------------------------------------------------------- #
# 2) 拒写闸门（缩表 / 暴涨 / 空名率）——照 tdxfeed/adj_factor.py 的 fail:shrink 先例
# --------------------------------------------------------------------------- #
def test_gate_refuses_shrink_beyond_tolerance() -> None:
    """旧 5,221 只、新 5,000 只（-4.2%）⇒ 必须给出一条拒写理由，不许静默缩表。"""
    mod = _load_writer()
    old = {f"{i:06d}": f"名称{i}" for i in range(5221)}
    new = {f"{i:06d}": f"名称{i}" for i in range(5000)}
    reasons = mod.check_gates(new, old, max_drop_pct=2.0, max_rise_pct=5.0,
                              min_named_ratio=0.9)
    assert reasons, "缩了 4.2% 却一条理由都没给 ⇒ 闸门形同虚设"
    assert any("缩表" in r or "drop" in r.lower() for r in reasons), reasons


def test_gate_refuses_explosion_beyond_tolerance() -> None:
    """条目数暴涨同样可疑（源出错时会把 ETF/转债全灌进来）。"""
    mod = _load_writer()
    old = {f"{i:06d}": f"名称{i}" for i in range(5221)}
    new = {f"{i:06d}": f"名称{i}" for i in range(6200)}
    reasons = mod.check_gates(new, old, max_drop_pct=2.0, max_rise_pct=5.0,
                              min_named_ratio=0.9)
    assert any("暴涨" in r or "rise" in r.lower() for r in reasons), reasons


def test_gate_refuses_when_names_mostly_empty() -> None:
    """名单退化成「只有代码」时必须拒写：is_st_name 与 predict 的实时 ST 二检都吃名称。"""
    mod = _load_writer()
    old = {f"{i:06d}": f"名称{i}" for i in range(5221)}
    new = {f"{i:06d}": ("" if i % 2 else f"名称{i}") for i in range(5221)}
    reasons = mod.check_gates(new, old, max_drop_pct=2.0, max_rise_pct=5.0,
                              min_named_ratio=0.9)
    assert any("名称" in r for r in reasons), reasons


def test_gate_passes_on_normal_refresh() -> None:
    """日常改名 + 少量新股/退市在容差内 ⇒ 必须放行，否则闸门会被当噪声绕过去。"""
    mod = _load_writer()
    old = {f"{i:06d}": f"旧名{i}" for i in range(5221)}
    new = {f"{i:06d}": f"新名{i}" for i in range(5225)}
    assert mod.check_gates(new, old, max_drop_pct=2.0, max_rise_pct=5.0,
                           min_named_ratio=0.9) == []


# --------------------------------------------------------------------------- #
# 3) 写盘纪律：备份先行 + 原子替换 + 闸门不过不动原文件
# --------------------------------------------------------------------------- #
def _seed_target(tmp_path: Path, mapping: dict[str, str]) -> Path:
    target = tmp_path / "all_a_stocks.json"
    target.write_text(json.dumps(mapping, ensure_ascii=False), encoding="utf-8")
    return target


def _atomic_write_calls(tree: ast.AST) -> set[str]:
    """点名调用 `atomic_write_*`：属性式（`atomic.foo()`）与裸名式（`foo()`）都算。

    `from tdxfeed.atomic import atomic_write_bytes` 之后是 `ast.Name`，只按属性找会漏。
    """
    found: set[str] = set()
    for n in ast.walk(tree):
        if not isinstance(n, ast.Call):
            continue
        name = getattr(n.func, "attr", "") or getattr(n.func, "id", "")
        if name.startswith("atomic_write"):
            found.add(name)
    return found


def _imports_from_tdxfeed_atomic(tree: ast.AST) -> set[str]:
    """从 `tdxfeed.atomic` 那一份实现里导入了哪些名字（判定「用的是共享出口」的依据）。"""
    names: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and (n.module or "").endswith("atomic"):
            names.update(a.name for a in n.names)
    return names


def test_writer_uses_the_shared_atomic_helper_not_its_own_replace(tmp_path: Path) -> None:
    """原子写只允许有一份实现：写盘走 `tdxfeed.atomic`，不在本文件里手搓 tmp→`os.replace`。

    哪个生产改动会让它红：有人把 `atomic_write_*` 换回自己 `tmp.write_bytes` + `os.replace`
    ——那既是 SEM001 的绕行，也让「换 inode」这件事在仓库里出现第二种写法。
    """
    tree = ast.parse((ROOT / WRITER).read_text(encoding="utf-8"))
    used = _atomic_write_calls(tree)
    assert used, "写入者没有调用 tdxfeed.atomic 的原子写出口"
    imported = _imports_from_tdxfeed_atomic(tree)
    assert used <= imported, f"{sorted(used - imported)} 不是从 tdxfeed.atomic 导入的 ⇒ 本地另写了一份"
    os_replaces = [n for n in ast.walk(tree)
                   if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                   and n.func.attr == "replace"
                   and isinstance(n.func.value, ast.Name) and n.func.value.id == "os"]
    assert not os_replaces, "写入者自己 os.replace ⇒ 仓库里出现第二份原子写实现"


def test_write_universe_backs_up_old_bytes_then_replaces(tmp_path: Path) -> None:
    """判据：新内容落盘**之前**，旧字节必须已经有一份带日期的副本（`data/` 不在 git）。"""
    mod = _load_writer()
    old = {"000001": "平安银行", "000002": "万  科Ａ"}
    target = _seed_target(tmp_path, old)
    backup_root = tmp_path / "_backups"
    before = target.read_bytes()

    backup = mod.write_universe({"000001": "平安银行", "689009": "九号公司"},
                                target=target, backup_root=backup_root)

    assert json.loads(target.read_text(encoding="utf-8")) == {"000001": "平安银行",
                                                             "689009": "九号公司"}
    assert backup is not None and backup.read_bytes() == before, "备份里没有旧字节 ⇒ 覆写不可回滚"
    assert backup.is_relative_to(backup_root), f"备份没落到 {backup_root} 下：{backup}"
    assert list(tmp_path.glob("*.tmp*")) == [], "留下了半个临时文件"


def test_main_refuses_and_leaves_file_untouched_when_source_fails(tmp_path: Path,
                                                                 monkeypatch) -> None:
    """源不可用 ⇒ 非零退出 + 原文件逐字节不变 + 不产生备份（拒跑不是「写了个空的」）。"""
    mod = _load_writer()
    target = _seed_target(tmp_path, {"000001": "平安银行"})
    before = target.read_bytes()
    backup_root = tmp_path / "_backups"

    def boom() -> dict[str, str]:
        raise RuntimeError("源不可用")

    rc = mod.main(["--out", str(target), "--backup-root", str(backup_root)], fetch=boom)

    assert rc != 0, "源炸了却返回 0 ⇒ 定时器会当成功"
    assert target.read_bytes() == before
    assert not backup_root.exists(), "什么都没写成却先备份了"


def test_main_refuses_on_shrink_without_touching_the_file(tmp_path: Path) -> None:
    """缩表超容差 ⇒ 非零退出、原文件不动（这条是 adj_factor 的 fail:shrink 同族）。"""
    mod = _load_writer()
    old = {f"{i:06d}": f"名称{i}" for i in range(5221)}
    target = _seed_target(tmp_path, old)
    before = target.read_bytes()
    rc = mod.main(["--out", str(target), "--backup-root", str(tmp_path / "_backups")],
                  fetch=lambda: {f"{i:06d}": f"名称{i}" for i in range(4000)})
    assert rc != 0
    assert target.read_bytes() == before


def test_main_dry_run_writes_nothing_but_reports_reconciliation(tmp_path: Path,
                                                               capsys) -> None:
    """`--dry-run` 必须一行都不写，但仍把对账四个数打出来（先看不等于不动手）。"""
    mod = _load_writer()
    old = {f"{i:06d}": f"旧名{i}" for i in range(100)}
    target = _seed_target(tmp_path, old)
    before = target.read_bytes()

    rc = mod.main(["--out", str(target), "--backup-root", str(tmp_path / "_backups"),
                   "--dry-run"],
                  fetch=lambda: {f"{i:06d}": f"新名{i}" for i in range(102)})

    assert rc == 0
    assert target.read_bytes() == before, "dry-run 写了盘"
    out = capsys.readouterr().out
    for needle in ("条目数", "+2", "-0", "名称变化"):
        assert needle in out, f"对账缺项 {needle!r}：{out}"


def test_main_writes_and_keeps_the_read_side_contract(tmp_path: Path) -> None:
    """写完后读侧必须原样读得回来：键是 6 位数字字符串、值是 str（`load_universe` 的契约）。

    夹具里混进一个带前缀的键与一个脏键：前者要归一成 6 位裸码，后者要丢掉并计数。
    """
    mod = _load_writer()
    old = {f"{i:06d}": f"旧名{i}" for i in range(1, 101)}
    old["000001"] = "平安银行"
    target = _seed_target(tmp_path, old)
    source = {f"{i:06d}": f"新名{i}" for i in range(1, 101)}
    source.update({"sh600000": "浦发银行", "": "脏键"})

    rc = mod.main(["--out", str(target), "--backup-root", str(tmp_path / "_backups")],
                  fetch=lambda: source)

    assert rc == 0, "正常刷新被拒 ⇒ 闸门把合法更新也挡了"
    got = _names.load_universe(target)
    assert "" not in got and "sh600000" not in got, f"脏键/未归一键进了名单：{sorted(got)[:5]}"
    assert got["600000"] == "浦发银行"
    assert all(k.isdigit() and len(k) == 6 and isinstance(v, str) for k, v in got.items())


# --------------------------------------------------------------------------- #
# 4) 取名通道与读侧实现都不许被绕过
# --------------------------------------------------------------------------- #
def test_claim_probe_catches_a_replanted_false_claim() -> None:
    """变异对照：假声明句式一旦回到 names.py，这条守卫必须抓得回来。"""
    docs = ['名单文件由 `scripts/ghost_writer.py` 每日更新，而 MCP server 是长驻进程。',
            '旧文案写的是 `scripts/ghost_writer.py`，那个脚本一个字的名单都不写。']
    assert claimed_writers(*docs) == {"scripts/ghost_writer.py"}, "生产者句式没抓到"
    # 第二句（只是提到文件名、没有生产者动词）不得算声明：两句话合起来只会命中一次
    assert claimed_writers(docs[1]) == set(), "把「更正记录」误判成了声明"


def test_writer_uses_only_the_declared_name_entry() -> None:
    """写入者只准调 `akshare.stock_info_a_code_name`（仓库既有入口），不许另起炉灶。"""
    mod = _load_writer()
    tree = ast.parse((ROOT / WRITER).read_text(encoding="utf-8"))
    ak_calls = {n.func.attr for n in ast.walk(tree)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and getattr(n.func.value, "id", "") == "ak"}
    assert ak_calls == {EXPECTED_SOURCE_CALL}, (
        f"出现了第二个取名通道：{sorted(ak_calls)}（只准 {EXPECTED_SOURCE_CALL}）")
    assert mod.SOURCE_ENTRY.endswith(EXPECTED_SOURCE_CALL)


def test_writer_reads_the_old_universe_through_the_single_entry() -> None:
    """比对旧名单必须经 `names.load_universe`，不许自己再解析一份 JSON（那是第二份读实现）。"""
    src = (ROOT / WRITER).read_text(encoding="utf-8")
    tree = ast.parse(src)
    calls = {getattr(n.func, "attr", "") for n in ast.walk(tree) if isinstance(n, ast.Call)}
    assert "load_universe" in calls, "旧名单没走 names.load_universe ⇒ 读侧又多一份实现"
    own_defs = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    assert not (own_defs & {"_read_universe", "load_universe", "universe_codes"}), (
        f"写入者里出现了读侧同名实现：{sorted(own_defs)}")
