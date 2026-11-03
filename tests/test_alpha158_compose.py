"""步骤 ② 的收尾判据：主 venv 的求值器必须复现「岛里用真 qlib 算子树跑出来的 158 列」。

逐算子金标准（`tests/test_alpha158_semantics.py`）钉的是**单个算子**的语义；本文件钉的是
**组合**——`Mean($close>Ref($close, 1), 5)` 里 bool 序列怎么进滚动、
`Corr($close/Ref($close,1), Log($volume/Ref($volume,1)+1), 5)` 里除零的 ±Inf 怎么污染相关系数。
这些在逐算子用例里看不见，但 158 维宽表出错九成出在这里。

形态与另两份守卫一致：岛内跑 `scripts/qlib_island/check_alpha158_compose.py` 产真值（临时目录，
不入库），主 venv 用同一份输入帧调 `tdxfeed.alpha158.eval.evaluate_frame` 逐值比对。

- **岛不在就 skip**（CI 里就是这样，`venv-qlib/` 不在干净检出里），已登记
  `tests/test_machine_state_skips.py` 的机器状态清单；skip 不等于没判据——两份入库工件
  仍是 CI 侧的闭包判据，本文件是本机侧的等价判据。
- 输入帧的字面量写在岛侧脚本里并由它 `--inputs` 吐出来，**主 venv 不自己造表**：
  两边各造一张表的话，"逐值相同"就成了比两张不同的表。
- 变异用例（把 `>` 接到 `Greater`）用来证明本判据**真的能分辨**比较算子接错这件事——
  否则"全绿"只说明我比了 158 列，不说明这 158 列比对了什么。
"""
from __future__ import annotations

import json
import math
import subprocess
from pathlib import Path

import pandas as pd
import pytest

NEEDS_DATA = False

ROOT = Path(__file__).resolve().parents[1]
QLIB_VENV_PY = ROOT / ("venv" + "-qlib") / "bin" / "python"
COMPOSER = ROOT / "scripts" / "qlib_island" / "check_alpha158_compose.py"
EXPR_ART = ROOT / "config" / "alpha158_expressions.json"

TOL = 1e-9


def island_ready() -> bool:
    return QLIB_VENV_PY.exists() and COMPOSER.exists()


def run_island(tmp: Path, extra: list[str]) -> dict:
    """岛内产出，返回 `{因子名: [float…]}`；非零退出直接把 stderr 端出去。"""
    proc = subprocess.run(
        [str(QLIB_VENV_PY), str(COMPOSER), *extra],
        cwd=ROOT, capture_output=True, text=True, timeout=600,
    )
    assert proc.returncode == 0, (
        f"岛内组合核对器退出码 {proc.returncode}\n命令: {extra}\n"
        f"stderr:\n{proc.stderr[-4000:]}"
    )
    return proc.stdout


def compose_gold(tmp: Path) -> tuple[pd.DataFrame, dict]:
    """先让岛把输入帧吐出来，再用**同一张表**算真值；返回 (输入帧, 158 列真值)。"""
    inputs = tmp / "frame.json"
    gold_path = tmp / "gold.json"
    run_island(tmp, ["--inputs", str(inputs)])
    frame = pd.DataFrame(json.loads(inputs.read_text(encoding="utf-8")))
    assert list(frame.columns) == sorted(frame.columns), (
        f"输入帧列序被重排：{list(frame.columns)}；岛侧按列名建树，主侧按帧装配，"
        "顺序不该参与比对，但漂了要先确认是哪一侧动的"
    )
    run_island(tmp, ["--inputs", str(inputs), "--out", str(gold_path)])
    gold = json.loads(gold_path.read_text(encoding="utf-8"))
    assert len(gold) == 158, f"岛内只算出 {len(gold)} 列"
    return frame, gold


def decode(values: list) -> list[float]:
    return [float(v) for v in values]


def compare(got: list[float], want: list[float]) -> list[tuple[int, float, float]]:
    """NaN 与 NaN 视为相等、±Inf 要同号，其余按相对容差——与逐算子守卫同一判据。"""
    if len(got) != len(want):
        return [(-1, float(len(got)), float(len(want)))]
    diffs: list[tuple[int, float, float]] = []
    for i, (a, b) in enumerate(zip(got, want, strict=True)):
        if math.isnan(a) and math.isnan(b):
            continue
        if math.isinf(a) or math.isinf(b):
            if a != b:
                diffs.append((i, a, b))
            continue
        if abs(a - b) > TOL * max(1.0, abs(b)):
            diffs.append((i, a, b))
    return diffs


pytestmark = pytest.mark.skipif(
    not island_ready(),
    reason=f"没有 qlib 岛（{QLIB_VENV_PY} 不在）：组合真值无从产出；CI 侧闭包判据是两份入库工件",
)


# ---------------------------------------------------------------- 主判据 #
def test_evaluator_reproduces_the_island_on_all_158_features(tmp_path):
    frame, gold = compose_gold(tmp_path)
    from tdxfeed.alpha158 import eval as ev

    out = ev.evaluate_frame(frame)
    assert list(out.columns) == [f["name"] for f in json.loads(
        EXPR_ART.read_text(encoding="utf-8"))["features"]], "列名/顺序与 ① 工件不一致"
    bad: dict[str, list] = {}
    for name in out.columns:
        d = compare(decode(out[name].tolist()), decode(gold[name]))
        if d:
            bad[name] = d
    assert not bad, (
        f"{len(bad)} 列与岛内真 qlib 不一致，例：{list(bad.items())[:3]}\n"
        "组合语义漂了：逐算子金标准可能仍然全绿，因为它不经过这条链路"
    )


def test_gold_standard_distinguishes_gt_from_greater(tmp_path):
    """变异对照：把 `>` 接到 `Greater`（取大）上，本判据必须变红。

    `Greater` 同样返回一列浮点、同样不改变 NaN 分布——形状检查完全看不出来，只有值域从
    0~1 变成十几块。这条用例证明上一条不是"比了个永远相等的东西"。
    """
    frame, gold = compose_gold(tmp_path)
    from tdxfeed.alpha158 import eval as ev, ops

    exprs = json.loads(EXPR_ART.read_text(encoding="utf-8"))
    # 按**具体符号**分组：换掉 Gt 时 CNTN*（用 `<`）本来就该仍然一致，
    # 断言"所有用比较的列都变"会把一个正确的实现判成缺陷。
    by_symbol: dict[str, list[str]] = {}
    for feat in exprs["features"]:
        for sym in (">", "<"):
            if sym in feat["expr"]:
                by_symbol.setdefault(sym, []).append(feat["name"])
    assert set(by_symbol) == {">", "<"}, f"清单里的比较符号分布变了：{sorted(by_symbol)}"

    original = dict(ops.REGISTRY)
    try:
        for sym, cls, swap in ((">", "Gt", "Greater"), ("<", "Lt", "Less")):
            names = by_symbol[sym]
            ops.REGISTRY.clear()
            ops.REGISTRY.update(original)
            ops.REGISTRY[cls] = original[swap]
            out = ev.evaluate_frame(frame)
            survivors = [n for n in names
                         if not compare(decode(out[n].tolist()), decode(gold[n]))]
            assert not survivors, (
                f"把 {cls} 换成 {swap} 后仍有 {len(survivors)}/{len(names)} 列与岛内真值一致："
                f"{survivors[:5]}。那说明本判据没在看比较语义，得加断言（值域或逐行占比）"
            )
    finally:
        ops.REGISTRY.clear()
        ops.REGISTRY.update(original)


def test_both_sides_share_the_same_operator_surface():
    """岛侧建树与主侧求值必须认**同一批** AST 节点，否则放宽一侧等于没人在管那一维。

    两侧各写一份分派表是本步的结构性风险：主侧多认一个节点（将来清单里出现 `%` 或链式
    比较时）就没人验证它，岛侧多认一个则产出一个主侧算不出来的真值。所以这里不比对措辞，
    直接比对两张表的键。
    """
    import ast
    import importlib.util

    spec = importlib.util.spec_from_file_location("island_composer", str(COMPOSER))
    assert spec is not None and spec.loader is not None, f"无法加载 {COMPOSER}"
    composer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(composer)  # 岛侧脚本顶层不 import qlib，主 venv 里可以直接读它的表

    from tdxfeed.alpha158 import eval as ev

    assert composer.BIN_SYMBOL == ev._BIN_OPS, "四则的 AST 节点→符号表两侧分叉了"
    assert composer.CMP_SYMBOL == ev._CMP_OPS, "比较的 AST 节点→符号表两侧分叉了"
    assert set(composer.BIN_SYMBOL) | set(composer.CMP_SYMBOL) <= {
        ast.Add, ast.Sub, ast.Mult, ast.Div,
        ast.Gt, ast.GtE, ast.Lt, ast.LtE, ast.Eq, ast.NotEq,
    }, "分派表里出现了预期外的 AST 节点"
    # 岛侧的改写规则必须与主侧同形：都是 $col → col_<name>
    assert composer.DOLLAR_RE.pattern == ev.DOLLAR_RE.pattern, "$列 的改写正则两侧分叉"
    assert composer.COLUMN_PREFIX == ev.COLUMN_PREFIX
