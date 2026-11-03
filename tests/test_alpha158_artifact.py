"""Alpha158 因子清单是「岛内导出、岛外消费」的那份工件——本模块钉它的可信度。

背景：pyqlib 只能活在 venv-qlib/（主 venv 是 Python 3.14，wheel 只到 cp312），而下游真正要的
是「158 条表达式字符串 + 它的来源与闭包」。没有落盘工件时，「借到了」只是安装脚本里的一句
self-check：没人能复核，也没人能消费。于是导出器
`scripts/qlib_island/export_alpha158.py` 把清单落成 `config/alpha158_expressions.json`，
本模块钉住六件事：

1. schema 闭合——缺字段、空表达式、重名都红；
2. 条数恒等式 `158 == 13 条无窗口 + 29 个滚动族 × 5 个窗口`，分组从名字派生，不是硬编码；
3. 内容 hash 自洽——手改某条表达式而不重导就红；`--check` 钉的是另一半（岛内现算 vs 工件记录），
   并用一份「自洽但内容不同」的篡改工件证明它真的在跟岛比对，而不是自我循环；
4. 算子与字段闭包——表达式里出现的函数与 `$列` 必须在声明集合内，且 `$$`（标的级静态字段）
   必须为空：这决定下游只读日线 OHLCV 就能算满 158 维；
5. 读侧口径——`vwap` 不是我们的价格列、且在代理成交额时代它恒等于 close，工件必须带着这条
   caveat，否则下游会把一列伪信号当真的喂进模型；
6. 岛与工件一致、导出器离开岛就跑不起来——前者按机器状态自跳过（登记在
   `tests/test_machine_state_skips.py`），后者在 CI 也用跑 pytest 的这个解释器真跑，不是空过。

抽取口径（算子 / `$列` / 窗口后缀）由本模块定义一次，导出器必须逐字复用同一份正则——
两套实现早晚漂，所以第 4 组里有一条专门钉这件事。

纯读源码与工件文本，不读行情数据，故 NEEDS_DATA = False。
"""
from __future__ import annotations

import ast
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

NEEDS_DATA = False

ROOT = Path(__file__).resolve().parents[1]
ARTIFACT = ROOT / "config" / "alpha158_expressions.json"
EXPORTER = ROOT / "scripts" / "qlib_island" / "export_alpha158.py"
QLIB_VENV_PY = ROOT / "venv-qlib" / "bin" / "python"
MAIN_VENV_PY = ROOT / "venv" / "bin" / "python"

TOP_KEYS = {
    "version", "source", "provenance", "count", "shape", "columns",
    "instrument_fields", "operator_expression_counts", "arithmetic",
    "comparison_operators", "caveats", "features", "content_sha256",
}
FUNC_RE = re.compile(r"\b([A-Z][A-Za-z0-9]*)\(")
DOLLAR_RE = re.compile(r"(\$+)([A-Za-z_][A-Za-z0-9_]*)")
WINDOW_SUFFIX_RE = re.compile(r"^(?P<base>.+?)(?P<win>\d+)$")
# 双字符必须先试：`>=` 里含 `>`，按单字符扫会把一个算子记成另一个。
COMPARISON_RE = re.compile(r">=|<=|!=|==|[><]")
WINDOWS = (5, 10, 20, 30, 60)


def load() -> dict:
    assert ARTIFACT.exists(), (
        f"缺 {ARTIFACT.relative_to(ROOT)}：158 条清单必须先由岛内导出器落盘，"
        "否则「借到了 qlib」只是一句没人能复核的 self-check"
    )
    return json.loads(ARTIFACT.read_text(encoding="utf-8"))


def feature_ops(expr: str) -> list[str]:
    return sorted(set(FUNC_RE.findall(expr)))


def feature_columns(expr: str) -> list[str]:
    """`$close` 是日线列，`$$industry` 是标的级静态字段——两者读侧来源不同，必须分流。"""
    return sorted({col for sig, col in DOLLAR_RE.findall(expr) if sig == "$"})


def feature_instrument_fields(expr: str) -> list[str]:
    return sorted({col for sig, col in DOLLAR_RE.findall(expr) if sig == "$$"})


def feature_comparisons(expr: str) -> list[str]:
    """表达式里用到的比较符号（`CNTP5 = Mean($close>Ref($close, 1), 5)` 用的是 `>`)。"""
    return sorted(set(COMPARISON_RE.findall(expr)))


def canonical_payload_sha(features: list[dict]) -> str:
    """与导出器同式的独立实现：对 name/expr/ops 三键的规范化 JSON 取 sha256。"""
    canon = json.dumps(
        [{"name": f["name"], "expr": f["expr"], "ops": f["ops"]} for f in features],
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def derive_shape(names: list[str]) -> dict:
    """从名字派生分组：尾数是 5/10/20/30/60 的进滚动族，其余算无窗口项。"""
    families: dict[str, list[int]] = {}
    windowless: list[str] = []
    for name in names:
        m = WINDOW_SUFFIX_RE.match(name)
        if m and int(m.group("win")) in WINDOWS:
            families.setdefault(m.group("base"), []).append(int(m.group("win")))
        else:
            windowless.append(name)
    return {
        "windows": sorted({w for ws in families.values() for w in ws}),
        "rolling_families": len(families),
        "family_sizes": sorted({len(ws) for ws in families.values()}),
        "windowless": len(windowless),
        "windowless_names": sorted(windowless),
    }


# ---------------- 1. schema 闭合 ----------------

def test_artifact_schema_is_closed():
    doc = load()
    assert set(doc) == TOP_KEYS, f"工件顶层键漂移：{set(doc) ^ TOP_KEYS}"
    assert doc["version"] == 1
    assert doc["source"] == "qlib.contrib.data.loader.Alpha158DL.get_feature_config()"
    prov = doc["provenance"]
    assert prov["pyqlib"], "没记 pyqlib 版本：表达式口径会随岛里的版本漂"
    assert prov["interpreter"] == "venv-qlib", "清单必须由岛内解释器产出，主 venv 装不上也不该装"
    assert prov["exported_by"] == EXPORTER.relative_to(ROOT).as_posix()


def test_features_are_unique_and_nonempty():
    feats = load()["features"]
    names = [f["name"] for f in feats]
    assert len(names) == len(set(names)), "重名会让下游宽表的列互相覆盖"
    assert all(f["name"] and f["expr"].strip() for f in feats), "有空名字或空表达式"
    assert all(set(f) == {"name", "expr", "ops"} for f in feats)


# ---------------- 2. 条数恒等式（派生，不硬编码） ----------------

def test_count_and_derived_shape_identity():
    doc = load()
    feats = doc["features"]
    names = [f["name"] for f in feats]
    assert doc["count"] == len(feats) == 158, f"Alpha158 应为 158 条，实得 {len(feats)}"

    derived = derive_shape(names)
    shape = doc["shape"]
    assert derived["windows"] == list(WINDOWS), f"窗口集合不是 qlib 默认的 5/10/20/30/60：{derived}"
    assert derived["rolling_families"] == 29, f"滚动族 {derived['rolling_families']} ≠ 29"
    assert derived["family_sizes"] == [5], f"每个滚动族应有 5 个窗口成员：{derived['family_sizes']}"
    assert derived["windowless"] == 13, f"无窗口项（9 kbar + 4 价格位）应为 13：{derived}"
    assert shape["windows"] == derived["windows"]
    assert shape["rolling_families"] == 29 and shape["windowless"] == 13
    assert shape["windowless_names"] == derived["windowless_names"]
    assert shape["windowless"] + shape["rolling_families"] * len(shape["windows"]) == doc["count"], (
        "恒等式不成立 ⇒ 要么有人手改了清单，要么 qlib 的默认配置变了；两种都必须重导而不是放行"
    )


# ---------------- 3. 内容 hash 自洽 + 咬合证明 ----------------

def test_content_hash_matches_the_stored_features():
    doc = load()
    assert doc["content_sha256"] == canonical_payload_sha(doc["features"]), (
        "hash 与内容不一致：有人绕过导出器手改了表达式。重导 = "
        f"venv-qlib/bin/python {EXPORTER.relative_to(ROOT)}"
    )


def test_check_mode_rejects_a_tampered_artifact(tmp_path):
    """防空转：`--check` 比的是「岛内现算」对「工件记录」，所以喂一份自洽但内容不同的工件必须红。

    上一轮这条写坏了：只改 expr 不重算 hash，于是 `--check` 判「与岛一致」而退出 0——那是
    对的，hash 自洽性由 `test_content_hash_matches_the_stored_features` 单独钉。两条性质
    （岛↔工件、hash↔内容）各咬各的，混在一条用例里只会造出一个假红。
    """
    if not QLIB_VENV_PY.exists():
        pytest.skip("本机没有 venv-qlib/：无法用岛内解释器跑 --check 的反向验证")
    doc = load()
    doc["features"][0]["expr"] = "(" + doc["features"][0]["expr"] + ")"
    doc["content_sha256"] = canonical_payload_sha(doc["features"])  # 篡改后自洽，骗过 hash 检查
    tampered = tmp_path / "alpha158_tampered.json"
    tampered.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")

    assert tampered.exists()
    proc = subprocess.run(
        [str(QLIB_VENV_PY), str(EXPORTER), "--check", "--artifact", str(tampered)],
        cwd=ROOT, capture_output=True, text=True, timeout=600,
    )
    assert proc.returncode != 0, (
        f"--check 对内容不同的工件仍然退出 0，说明它没真跟岛比对：\n{proc.stdout}"
    )
    assert doc["features"][0]["name"] in (proc.stdout + proc.stderr), (
        "比对失败却没指出第一条差异在哪条表达式，等于让人自己二分 158 条："
        f"\n{proc.stdout}"
    )


# ---------------- 4. 算子与字段闭包 + 抽取口径单源 ----------------

def test_operator_and_column_closure():
    doc = load()
    ops_used: set[str] = set()
    cols_used: set[str] = set()
    instr_used: set[str] = set()
    for f in doc["features"]:
        assert f["ops"] == feature_ops(f["expr"]), f"{f['name']} 的 ops 与表达式不一致"
        ops_used.update(f["ops"])
        cols_used.update(feature_columns(f["expr"]))
        instr_used.update(feature_instrument_fields(f["expr"]))
    assert set(doc["operator_expression_counts"]) == ops_used, "声明的算子集合与实际用到的不一致"
    assert set(doc["columns"]) == cols_used, "声明的字段集合与表达式实际用到的不一致"
    assert doc["instrument_fields"] == sorted(instr_used) == [], (
        "$$ 标的级字段要求下游另备一份「每标的一行」的输入；Alpha158 默认配置不该有，"
        "真出现了就要显式扩工件与读侧来源，而不是让下游猜"
    )
    assert {"close", "open", "high", "low", "volume"} <= set(doc["columns"])

    # 求值器要实现的完整闭包 = 函数算子 + 四则运算符 + 比较符号；只声明前两者会漏掉
    # CNTP/CNTN 那 15 条（`Mean($close>Ref($close, 1), 5)`）——它们在表达式串里是符号，
    # 不进 FUNC_RE 的名单，但下游必须实现对应的算子（qlib 里是 Gt/Lt，不是 Greater/Less）。
    arith_used = sorted({ch for f in doc["features"] for ch in "+-*/" if ch in f["expr"]})
    assert doc["arithmetic"] == arith_used, f"四则运算闭包不符：{doc['arithmetic']} vs {arith_used}"
    cmp_used: set[str] = set()
    for f in doc["features"]:
        cmp_used.update(feature_comparisons(f["expr"]))
    assert doc["comparison_operators"] == sorted(cmp_used), (
        f"比较符号闭包不符：{doc['comparison_operators']} vs {sorted(cmp_used)}"
    )
    assert doc["comparison_operators"], (
        "声明的比较符号为空：CNTP*/CNTN* 那批表达式在用 `>`/`<`，"
        "字段没了说明导出器的扫描口径漂了"
    )
    counts = doc["operator_expression_counts"]
    assert all(0 < v <= doc["count"] for v in counts.values())
    assert counts["Ref"] == sum(1 for f in doc["features"] if "Ref(" in f["expr"]), (
        "算子计数口径必须是「用到该算子的表达式条数」，否则下游据此估工作量会算错"
    )


def test_exporter_reuses_the_same_extraction_patterns():
    """两套正则必漂，所以导出器必须逐字复用本模块定义的抽取口径。"""
    src = EXPORTER.read_text(encoding="utf-8")
    literals = {
        node.value for node in ast.walk(ast.parse(src))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    for pat in (FUNC_RE.pattern, DOLLAR_RE.pattern, WINDOW_SUFFIX_RE.pattern,
                COMPARISON_RE.pattern):
        assert pat in literals, f"导出器没用到抽取口径 {pat!r}：两份实现会在某天悄悄分叉"


def test_symbols_declared_here_are_gold_standard_in_the_ops_artifact():
    """① 只声明「清单用了哪些符号」，**符号 → 算子 → 真值**全在 ② 里。

    两份工件必须互相咬合：① 冒出 ② 没钉过语义的符号，就意味着求值器要么报错、
    要么按「Python 里 `>` 大概就是这个意思」猜——后者正是本步要消灭的东西。
    """
    doc = load()
    ops_art = ROOT / "config" / "alpha158_ops.json"
    if not ops_art.exists():
        pytest.skip(f"缺 {ops_art.relative_to(ROOT)}：先跑 `make qlib-export-ops`")
    gold = json.loads(ops_art.read_text(encoding="utf-8"))
    assert set(doc["arithmetic"]) <= set(gold["arithmetic_symbols"]), (
        f"① 声明的四则符号 {sorted(doc['arithmetic'])} 里有 ② 没钉过语义的"
    )
    assert set(doc["comparison_operators"]) <= set(gold["comparison_symbols"]), (
        f"① 声明的比较符号 {sorted(doc['comparison_operators'])} 里有 ② 没钉过语义的"
    )
    for name in {**gold["arithmetic_symbols"], **gold["comparison_symbols"]}.values():
        assert name in gold["operators"], f"{name} 被符号表指向，却没有金标准用例"


# ---------------- 5. 读侧口径 ----------------

def test_vwap_caveat_is_recorded_and_read_side_authority_named():
    doc = load()
    assert "vwap" in doc["columns"], "vwap 确实在 158 条里出现，不能假装看不见"
    caveats = " ".join(doc["caveats"])
    assert "vwap" in caveats, "vwap 需要显式推导策略，否则每个下游各发明一份"
    assert "amount" in caveats, "vwap 的伪值来源是代理成交额，caveat 必须点名"
    assert "adjusted.frame" in caveats, "消费方必须知道价格列走 hfq 读侧权威，不直读因子表"


# ---------------- 6. 岛↔工件一致，导出器离开岛就跑不起来 ----------------

def test_island_and_artifact_agree():
    if not QLIB_VENV_PY.exists():
        pytest.skip("本机没有 venv-qlib/：岛内重导比对无对象（CI 就是这种情形，已登记自跳过）")
    proc = subprocess.run(
        [str(QLIB_VENV_PY), str(EXPORTER), "--check"],
        cwd=ROOT, capture_output=True, text=True, timeout=600,
    )
    assert proc.returncode == 0, (
        f"岛内重新导出的清单与工件不一致（qlib 版本或默认配置漂了？）：\n"
        f"{proc.stdout}\n{proc.stderr}"
    )


def test_exporter_refuses_a_non_island_interpreter():
    """反向证明：导出器在生产侧解释器里必须失败——否则岛边界只是句注释。"""
    interp = MAIN_VENV_PY if MAIN_VENV_PY.exists() else Path(sys.executable)
    proc = subprocess.run(
        [str(interp), str(EXPORTER)],
        cwd=ROOT, capture_output=True, text=True, timeout=300,
    )
    assert proc.returncode != 0, (
        f"{interp} 里居然能把 qlib 清单导出来：导出器没有自护，"
        "哪天主 venv 装得上 pyqlib，岛边界就名存实亡"
    )
    assert "venv-qlib" in (proc.stderr + proc.stdout), (
        f"失败原因不是「离开岛就没有 qlib」，等于把泄漏藏进别的报错里：\n{proc.stderr[-400:]}"
    )
