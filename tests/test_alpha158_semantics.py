"""Alpha158 算子语义的金标准守卫：主 venv 的实现必须逐值复现岛内真 qlib 的输出。

为什么要有这份工件（2026-11-03）：步骤 ① 把 158 条**表达式字符串**落成了可复核的工件，
但表达式只是形状，真正会出错的是算子语义。qlib 0.9.7 的三处语义与"手写一个 rolling"
并不等价，且都不是靠读一眼签名能发现的：

1. `Rolling._load_internal` 一律 `series.rolling(N, min_periods=1)` —— 前 N-1 行**也出值**
   （pandas 默认 min_periods=N，会静默多出 NaN，特征矩阵形状对得上但值全不同）；
2. `Rank(x, N)` 是 `rolling(N, min_periods=1).rank(pct=True)`，即**逐标的的时序百分位**，
   不是横截面排名；
3. `Slope/Rsquare/Resi` 不逐窗重算，而是 `qlib/data/_libs/rolling.pyx` 里的**增量 OLS**
   （x 轴用 `self.window` 常数 + 出窗时按 `i_sum` 衰减，NaN 跳过但占位、`na_count` 参与
   分母）。增量累积的浮点顺序与 `polyfit` 不同，warm-up 段也与"完整窗 OLS"不等价，
   照公式重推一份几乎必然对不上；
4. `Rsquare`/`Corr` 额外把「滚动 std ≈ 0（atol=2e-5）」的位置**显式置 NaN**；
   `Log` 是 `np.log`（负数→NaN、0→−Inf，模块级 `np.seterr(invalid="ignore")`）；
   `IdxMax/IdxMin` 是 `apply(lambda x: x.argmax() + 1, raw=True)`，1 基且不跳过 NaN。

主 venv 装不上 pyqlib（wheel 只到 cp312，见 tests/test_qlib_env_island.py 与 AGENTS.md），
所以这些语义**不可能**在 CI 里现场对照 qlib 验证。本模块的做法：岛内用真 qlib 代码路径
（`Rolling/PairRolling._load_internal` + `_libs.rolling`）对固定输入算一遍，把输入与输出
一起落成 `config/alpha158_ops.json`，主 venv 的实现逐值复现它。工件里每个算子还带
`qlib_source_sha256`（该类源码文本的哈希），qlib 一升级哈希就漂，逼着重导而不是沿用旧真值。

非有限值（NaN/±Inf）在 JSON 里没有合法字面量，工件一律编码成字符串 "NaN"/"Infinity"/
"-Infinity"，模块内 `test_nonfinite_encoding_is_lossless` 钉住这条口径——把 NaN 悄悄写成
null 会让「全 null 的实现」也能通过比对，那才是本模块最危险的假绿。

跳过条件只有「岛依赖的那条 --check 反向验证」，已登记进 MACHINE_STATE_SKIP_MODULES；
其余用例（含全部数值比对）在 CI 里真跑。本模块只读工件与合成输入，不读行情数据，NEEDS_DATA = False。
"""
from __future__ import annotations

import hashlib
import inspect
import json
import math
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

NEEDS_DATA = False

ROOT = Path(__file__).resolve().parents[1]
ART = ROOT / "config" / "alpha158_ops.json"
EXPR_ART = ROOT / "config" / "alpha158_expressions.json"
EXPORTER = ROOT / ("scripts/qlib_" + "island/export_alpha158_ops.py")
QLIB_VENV_PY = ROOT / ("venv" + "-qlib") / "bin" / "python"

TOP_KEYS = {
    "version",
    "source",
    "provenance",
    "encoding",
    "inputs",
    "windows",
    "operators",
    "content_sha256",
}
OP_KEYS = {"class", "base", "arity", "params", "qlib_source_sha256", "reference"}


def payload() -> dict:
    assert ART.exists(), f"缺 {ART.relative_to(ROOT)}：算子语义没有落盘真值，主 venv 的实现无从对照"
    return json.loads(ART.read_text(encoding="utf-8"))


def decode(values: list) -> list[float]:
    """工件里的数可能是字符串形式的非有限值；还原成 float。"""
    out: list[float] = []
    for v in values:
        assert isinstance(v, (int, float, str)), f"非法的数值编码：{v!r}"
        out.append(float(v))
    return out


def to_list(result) -> list[float]:
    """算子返回 pandas.Series（求值器要靠 index 对齐），比对时统一成 float 列表。"""
    vals = list(getattr(result, "tolist", lambda: result)())
    return [float(v) for v in vals]


def operands(payload: dict, params: list[str], args: dict) -> list:
    """按工件记的 `params`（= qlib 的形参名与位置序）还原位置参数。

    不用 args 的字典序：工件是 sort_keys 落盘的，键序与调用序无关（早先正是靠字典序
    把 `N` 当成序列传给了 Rank）。字符串值指 `inputs` 里的序列，数字原样传入——
    qlib 的成对算子允许一侧是标量，Alpha158 里 `Greater($close-Ref($close,1), 0)` 就用了。
    """
    out = []
    for name in params:
        val = args[name]
        out.append(pd.Series(decode(payload["inputs"][val])) if val in payload["inputs"] else val)
    return out


def ops_module():
    try:
        from tdxfeed.alpha158 import ops
    except ImportError as exc:  # 实现还不存在时给出可读的红，而不是收集期炸
        pytest.fail(f"tdxfeed.alpha158.ops 不可导入：{exc}")
    return ops


def close(actual: float, expected: float, tol: float = 1e-9) -> bool:
    if math.isnan(expected) or math.isnan(actual):
        return math.isnan(expected) and math.isnan(actual)
    if math.isinf(expected) or math.isinf(actual):
        return expected == actual
    return abs(actual - expected) <= tol * max(1.0, abs(expected))


# ---------------------------------------------------------------- 工件自身的形状 #
def test_artifact_top_level_shape_is_closed():
    d = payload()
    assert set(d) == TOP_KEYS, f"工件顶层键闭合失败：{sorted(set(d) ^ TOP_KEYS)}"
    assert d["version"] == 1
    assert set(d["provenance"]) == {
        "interpreter",
        "python",
        "pyqlib",
        "pandas",
        "numpy",
        "exported_by",
        "generated_at",
    }
    # Rank 有两条实现路径（pandas>=1.4 的 rolling.rank(pct=True) 与 percentileofscore 回退），
    # 两者对并列值的口径不同；记下岛内当时的 pandas 版本，才能判断真值出自哪条路。
    assert tuple(map(int, d["provenance"]["pandas"].split(".")[:2])) >= (1, 4), (
        f"岛内 pandas 是 {d['provenance']['pandas']}：真值出自 percentileofscore 回退路径，"
        "与主 venv 的 rolling.rank(pct=True) 不是同一口径，必须换岛重导"
    )
    assert d["provenance"]["interpreter"] == "venv-qlib"
    assert d["provenance"]["exported_by"] == EXPORTER.relative_to(ROOT).as_posix()


def test_artifact_is_strict_json_without_nonfinite_literals():
    """NaN/Infinity 必须是字符串，不能是 JSON 里非法的裸字面量——否则 json.load 之外的
    任何标准解析器（包括下游的 Go/Rust 消费者）读到就炸。

    判据用 `parse_constant`：Python 的 json 只在解析到裸 NaN/Infinity/-Infinity 时才调它，
    所以它是精确钩子（早先用文本正则会把说明文字里提到的 "NaN" 也算成违规）。
    """
    text = ART.read_text(encoding="utf-8")

    def _no_bare_constant(name: str):
        raise AssertionError(f"工件里有裸的非法 JSON 字面量 {name!r}")

    json.loads(text, parse_constant=_no_bare_constant)
    # 反向确认：字符串形式确实存在于工件里（否则上面那条"没炸"可能只是因为还没测过非有限值）
    assert '"NaN"' in text or '"Infinity"' in text or '"-Infinity"' in text, (
        "金标准里一个非有限值都没有：encoding 声明就成了空头文件，Log/Rsquare 的 NaN 分支没被钉住"
    )


def test_nonfinite_encoding_is_lossless():
    d = payload()
    assert d["encoding"]["form"] == "string"
    assert set(d["encoding"]["tokens"].values()) == {"NaN", "Infinity", "-Infinity"}
    assert sorted(d["encoding"]["tokens"]) == [
        "nan",
        "negative_infinity",
        "positive_infinity",
    ]
    # float() 必须能把这三个 token 原样还原回来（口径依赖点，写死在守卫里）
    assert math.isnan(float("NaN"))
    assert float("Infinity") == math.inf and float("-Infinity") == -math.inf
    for name, series in d["inputs"].items():
        assert series, f"输入序列 {name} 为空"
        got = decode(series)
        assert len(got) == len(series)


def test_inputs_are_pinned_literal_numbers():
    """输入必须是字面量，不能由导出器的随机种子决定——否则测试环境重算不出同一条序列。"""
    d = payload()
    assert len(d["inputs"]) >= 3, "至少要有价格形、成交量形、含 NaN/负数形三条输入"
    for name, series in d["inputs"].items():
        assert all(isinstance(v, (int, float, str)) for v in series), name
        assert len(series) >= 40, f"{name} 只有 {len(series)} 个点，撑不住 N=20 的窗"
    assert max(d["windows"]) <= min(len(s) for s in d["inputs"].values())


# ---------------------------------------------------------------- 算子集合双向闭合 #
def test_operator_set_is_closed_against_the_expressions_artifact():
    d = payload()
    exprs = json.loads(EXPR_ART.read_text(encoding="utf-8"))
    in_158 = set(exprs["operator_expression_counts"])
    in_ops = set(d["operators"])
    assert in_ops == in_158, (
        f"算子工件与 158 清单不闭合：只在清单里 {sorted(in_158 - in_ops)}；"
        f"只在算子工件里 {sorted(in_ops - in_158)}"
    )


def test_registry_matches_artifact_exactly():
    """实现侧注册表既不得少（求值器会 KeyError），也不得多（多出来的算子没人验证过语义）。"""
    ops = ops_module()
    d = payload()
    registry = set(ops.REGISTRY)
    assert registry == set(d["operators"]), (
        f"注册表与工件不闭合：只在注册表 {sorted(registry - set(d['operators']))}；"
        f"只在工件 {sorted(set(d['operators']) - registry)}"
    )


def test_declared_arity_matches_the_python_signature():
    ops = ops_module()
    d = payload()
    bad = []
    for op_name, rec in sorted(d["operators"].items()):
        sig = inspect.signature(ops.REGISTRY[op_name])
        params = [
            p
            for p in sig.parameters.values()
            if p.kind in (p.POSITIONAL_OR_KEYWORD, p.POSITIONAL_ONLY)
        ]
        if len(params) != rec["arity"]:
            bad.append(f"{op_name}: 工件 arity={rec['arity']} 签名={sig}")
    assert not bad, "arity 与实现不符，求值器会按错参数个数调用：\n" + "\n".join(bad)


# ---------------------------------------------------------------- 逐值复现金标准 #
def _reference_cases():
    d = payload()
    for op_name, rec in sorted(d["operators"].items()):
        assert set(rec) == OP_KEYS, f"{op_name} 记录键不闭合：{sorted(set(rec) ^ OP_KEYS)}"
        assert rec["class"] == op_name
        for case in rec["reference"]:
            yield op_name, rec, d, case


def test_reference_args_keys_match_the_declared_params():
    """`args` 的键集合必须正好等于 `params`——这条抓住的是「位置序错乱」那一类 bug。

    工件是 sort_keys 落盘的，任何"按键序传参"的实现都会静默把窗口值当序列传进去；
    有了这条闭合，导出器与守卫只能按 params 的名字取值。
    """
    d = payload()
    bad = []
    for op_name, rec in sorted(d["operators"].items()):
        assert len(rec["params"]) == rec["arity"], f"{op_name}: params 长度 {rec['params']} 与 arity {rec['arity']} 不符"
        for case in rec["reference"]:
            if set(case["args"]) != set(rec["params"]):
                bad.append(f"{op_name}: args 键 {sorted(case['args'])} ≠ params {sorted(rec['params'])}")
    assert not bad, "工件自身的键序契约不一致：\n" + "\n".join(bad)


def test_every_operator_reproduces_the_island_gold_standard():
    ops = ops_module()
    seen = 0
    failures: list[str] = []
    for op_name, rec, d, case in _reference_cases():
        actual = to_list(ops.REGISTRY[op_name](*operands(d, rec["params"], case["args"])))
        expected = decode(case["expected"])
        seen += 1
        assert len(actual) == len(expected), f"{op_name}{case['args']} 长度不符"
        bad = [
            (i, a, e)
            for i, (a, e) in enumerate(zip(actual, expected, strict=True))
            if not close(a, e)
        ]
        if bad:
            failures.append(
                f"{op_name} {case['args']}：{len(bad)}/{len(expected)} 个点不符，"
                f"首个 i={bad[0][0]} 实={bad[0][1]} 期={bad[0][2]}"
            )
    assert seen >= 18, f"金标准只有 {seen} 组用例，18 个算子没覆盖全"
    assert not failures, "与岛内真 qlib 不一致：\n" + "\n".join(failures)


def test_operators_return_series_aligned_with_the_input_index():
    """求值器靠 index 对齐两个操作数；算子若退回裸 list，Corr/Greater 这类成对算子就会错位。"""
    ops = ops_module()
    d = payload()
    case = d["operators"]["Mean"]["reference"][0]
    s = pd.Series(decode(d["inputs"][case["args"]["feature"]]), index=[f"t{i}" for i in range(40)])
    out = ops.REGISTRY["Mean"](s, int(case["args"]["N"]))
    assert list(out.index) == list(s.index), "输出 index 与输入不一致，下游按日期对齐会串位"


def test_gold_standard_actually_discriminates_a_min_periods_default_implementation():
    """防空转：把 min_periods 用成 pandas 默认（=N）的那份实现必须在这里失败。

    本仓最早的坑就是这个——前 N-1 行出 NaN 还是出值，两种口径的特征矩阵形状相同、
    数值完全不同，训练与 IC 都会静默换口径。这里显式跑一遍"错误实现"证明工件抓得住它。
    """
    d = payload()
    case = d["operators"]["Mean"]["reference"][0]
    series = pd.Series(decode(d["inputs"][case["args"]["feature"]]))
    n = int(case["args"]["N"])
    expected = decode(case["expected"])
    naive = [float(v) for v in series.rolling(n).mean()]  # min_periods 默认 = n
    assert any(not close(a, e) for a, e in zip(naive, expected, strict=True)), (
        "用默认 min_periods 竟然也能匹配金标准：说明 warm-up 段没被钉住，本工件的判据失效"
    )


def test_rank_is_time_series_percentile_not_cross_section():
    """措辞与实现都要钉：Rank(x, N) 在 qlib 里是 rolling 百分位，不是横截面排名。

    横截面写法（同期全体标的排名）需要全市场面板，本求值器按逐标的设计；若有人改成
    横截面，这条会红——因为它拿单标的序列比对的是岛内单序列产出的真值。
    """
    ops = ops_module()
    d = payload()
    rec = d["operators"]["Rank"]
    case = rec["reference"][0]
    got = to_list(ops.REGISTRY["Rank"](*operands(d, rec["params"], case["args"])))
    assert all(0.0 < v <= 1.0 for v in got if not math.isnan(v)), f"Rank 输出越界：{got[:8]}"
    assert math.isclose(got[0], 1.0, abs_tol=1e-12), (
        "首行 Rank 应为 1.0（min_periods=1 的 rolling rank，单元素窗内自己是满分位）；"
        f"实际 {got[0]}"
    )


def test_rsquare_and_corr_zero_std_branches_are_pinned():
    """Rsquare/Corr 把「滚动 std≈0」置 NaN 是 qlib 的显式行为，必须有常量序列的用例钉住。"""
    d = payload()
    const_keys = [
        name
        for name, series in d["inputs"].items()
        if not any(isinstance(v, str) for v in series) and len({round(float(v), 12) for v in series}) == 1
    ]
    assert const_keys, "输入里没有常量序列：std≈0 → NaN 那两条分支无从验证"
    for op_name in ("Rsquare", "Corr"):
        refs = d["operators"][op_name]["reference"]
        used_const = any(
            any(isinstance(v, str) and v in const_keys for v in case["args"].values())
            for case in refs
        )
        assert used_const, f"{op_name} 的金标准没用上常量序列 {const_keys}：分支没被触发"
        assert any(
            any(isinstance(v, str) and v == "NaN" for v in case["expected"])
            for case in refs
        ), f"{op_name} 的金标准里一个 NaN 都没有：常量序列分支没被钉住"


def test_log_of_zero_and_negative_are_encoded_as_nonfinite():
    """np.log(0) = -Inf、np.log(负) = NaN：这两个点必须出现在金标准里并被复现。"""
    d = payload()
    ops = ops_module()
    log_params = d["operators"]["Log"]["params"]
    flat: list[float] = []
    for case in d["operators"]["Log"]["reference"]:
        expected = decode(case["expected"])
        flat.extend(expected)
        got = to_list(ops.REGISTRY["Log"](*operands(d, log_params, case["args"])))
        assert all(close(a, e) for a, e in zip(got, expected, strict=True)), "Log 与岛内不一致"
    assert any(math.isinf(v) and v < 0 for v in flat), "金标准里没有 -Infinity：Log(0) 分支未钉住"
    assert any(math.isnan(v) for v in flat), "金标准里没有 NaN：Log(负数) 分支未钉住"


# ---------------------------------------------------------------- 溯源与版本 #
def test_pyqlib_version_matches_the_requirements_pin():
    d = payload()
    pinned = dict(
        (ln.split("==")[0].strip(), ln.split("==")[1].split("#")[0].strip())
        for ln in (ROOT / "requirements-qlib.txt").read_text(encoding="utf-8").splitlines()
        if "==" in ln and not ln.strip().startswith("#")
    )
    for dist in ("pyqlib", "pandas", "numpy"):
        assert dist in pinned, (
            f"requirements-qlib.txt 没钉 {dist}：金标准是数值口径，rolling 的 rank/corr/预热段"
            "NaN 政策随 pandas 小版本漂，重建岛就可能换一套真值"
        )
        assert d["provenance"][dist] == pinned[dist], (
            f"金标准出自 {dist} {d['provenance'][dist]}，清单钉的是 {pinned[dist]}："
            "要么重导工件，要么按清单重建岛，别把两套口径混着用"
        )


def test_exporter_islands_itself_even_when_user_site_shadows():
    """conda 前缀环境不像 venv 那样自动关掉 PEP 370 用户站点目录。

    实测踩到的：`~/.local/lib/python3.12/site-packages` 在 sys.path 里排在岛自己的
    site-packages **前面**，直接跑导出器会拿用户站点的 pandas 算金标准（本机是 3.0.5，
    岛里装的是 3.1.0）。本条用 `PYTHONNOUSERSITE=0` 强制把用户站点放回来，要求导出的
    真值仍然出自清单里那份 pandas——导出器必须自己 execve 回隔离环境。
    """
    if not QLIB_VENV_PY.exists():
        pytest.skip("本机没有 venv-qlib/：无从验证解释器隔离")
    if not EXPORTER.exists():
        pytest.skip(f"{EXPORTER.relative_to(ROOT)} 还没写")
    import os

    env = dict(os.environ, PYTHONNOUSERSITE="0")
    proc = subprocess.run(
        [str(QLIB_VENV_PY), str(EXPORTER), "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=900,
        env=env,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert payload()["provenance"]["pandas"] in proc.stdout, (
        "--check 报的 pandas 不是工件记的那份：说明用户站点目录还在盖岛里的包"
    )


def test_each_operator_carries_a_source_hash():
    d = payload()
    for name, rec in d["operators"].items():
        assert re.fullmatch(r"[0-9a-f]{64}", rec["qlib_source_sha256"]), f"{name} 缺源码哈希"


def test_content_sha_is_self_consistent():
    """哈希口径与导出器一致：正文全量规范化 JSON，但排除 content_sha256 与
    provenance.generated_at（日期每天变，纳进去就让 --check 天天假报漂移）。"""
    d = payload()
    body = {k: v for k, v in d.items() if k != "content_sha256"}
    prov = dict(body["provenance"])
    prov.pop("generated_at", None)
    body["provenance"] = prov
    blob = json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    assert hashlib.sha256(blob).hexdigest() == d["content_sha256"], (
        "content_sha256 与正文不一致：工件被手改过（或导出器的哈希口径与守卫不同）"
    )


# ---------------------------------------------------------------- 导出器自护 #
def test_exporter_exists_and_island_check_mode_is_reproducible():
    """岛在⇒用岛内解释器重导一遍并与工件比对（--check 退 0）；岛不在⇒CI 跳过。"""
    if not QLIB_VENV_PY.exists():
        pytest.skip("本机没有 venv-qlib/：金标准的可重导性无从验证（CI 就是这种情形，已登记自跳过）")
    assert EXPORTER.exists(), f"缺 {EXPORTER.relative_to(ROOT)}"
    proc = subprocess.run(
        [str(QLIB_VENV_PY), str(EXPORTER), "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_exporter_refuses_a_non_island_interpreter():
    """离开岛就导不出真值：必须显式拒绝，而不是悄悄产出一份"看起来对"的伪金标准。"""
    if not EXPORTER.exists():
        pytest.skip(f"{EXPORTER.relative_to(ROOT)} 还没写")
    interp = (ROOT / "venv/bin/python") if (ROOT / "venv/bin/python").exists() else Path(sys.executable)
    proc = subprocess.run(
        [str(interp), str(EXPORTER)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert proc.returncode != 0, "主 venv 里居然导出了金标准：那说明它没走 qlib 真代码路径，产物不可信"
    assert "venv-qlib" in (proc.stdout + proc.stderr)


def test_exporter_path_is_the_documented_one():
    if shutil.which("conda") is None and not QLIB_VENV_PY.exists():
        pytest.skip("既没有 conda 也没有岛：无从核对文档口径")
    text = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert EXPORTER.name in text, "AGENTS.md 没提到算子语义导出器：下一个人只会照 ① 的 make 目标走"
