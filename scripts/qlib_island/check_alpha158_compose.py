#!/usr/bin/env python
"""岛内组合式核对器：把 ① 的 158 条表达式在**真 qlib 算子树**上算一遍，供主 venv 逐值比对。

这一步补的是逐算子金标准看不见的那半张图
--------------------------------------
`config/alpha158_ops.json`（步骤 ②）钉的是**单个算子**的语义：`Mean` 怎么滚动、`Gt` 比出
什么 dtype、`Slope` 的增量 OLS 在预热段给什么值。但 158 维真正的风险还有一层在**组合**上：

- `Mean($close>Ref($close, 1), 5)`：`Rolling` 拿到的是 bool 序列，`.rolling().mean()`
  对 bool 的提升政策只有串起来跑才暴露；
- `Corr($close/Ref($close,1), Log($volume/Ref($volume, 1)+1), 5)`：除零产生的 ±Inf
  会不会把整窗 `Corr` 污染成 NaN，逐算子用例覆盖不到这种链路；
- 嵌套 `Ref`/`Max`/`Std` 的 index 传递与空帧形态，同理。

所以这里在岛里用 **qlib 自己的算子类**把整棵表达式树搭起来跑一遍（`Gt(leaf_a, Ref(leaf_a,1))`
→ `Mean(that, 5)`……），把 158 列的输出交给主 venv 的
`tdxfeed.alpha158.eval.evaluate_frame` 逐值比对。两侧共用同一棵 Python 语法树
（`+ - * /` 与 `> <` 的结合律在 sympy 与 Python 里一致），差别只在**叶子之后由谁算**——
正是是要验的那件事。

它**不是** qlib 解析器的复现：表达式字符串由 qlib 自己产出（① 工件），符号到算子类的
映射取自 qlib 的运算符重载（② 的 `comparison_symbols` / `arithmetic_symbols`）。
本文件只回答「同一棵树，qlib 算出来的值和我算出来的一样吗」。

产物不入库
---------
`--inputs` / `--out` 都写在调用方（守卫 `tests/test_alpha158_compose.py`）的临时目录里。
组合真值依赖解释器在场的数值栈，把它钉进 git 只会在换 pandas 时假报漂移；而且它需要岛在场，
CI 里跑不到——所以它是**本机复核**，CI 侧的等价判据仍是两份工件。

怎么用（只在 venv-qlib 里跑）
--------------------------
    venv-qlib/bin/python scripts/qlib_island/check_alpha158_compose.py --emit-inputs IN.json
    venv-qlib/bin/python scripts/qlib_island/check_alpha158_compose.py --inputs IN.json --out OUT.json

输入帧的字面量就写在本文件里（`PINNED_INPUTS`），由 `--emit-inputs` 原样吐给主 venv，
所以两侧拿到的是**同一组数**，不存在两边各造一张表的可能。
"""
from __future__ import annotations

import argparse
import ast
import json
import math
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# 与两份导出器共用同一条落盘口径（tmp + os.replace）：真值文件被读到半截时，比对会报
# 一串莫名其妙的长度差异，而不是「有人在写」——所以这里不各写一份 open()。
from export_alpha158 import atomic_write

ROOT = Path(__file__).resolve().parents[2]
EXPR_ARTIFACT = ROOT / "config" / "alpha158_expressions.json"
OPS_ARTIFACT = ROOT / "config" / "alpha158_ops.json"
INSTRUMENT = "sh600000"  # 占位：叶子自己回吐序列，不经数据提供方
REEXEC_MARKER = "TDX_QLIB_ISLAND_REEXEC"

# 输入帧：6 条日线列 × 60 行，全部字面量。造得"不像真行情"是有意的——各列互不相等、
# 有升有降，close 含 0（钉 Div 的除零分支）、volume 含 0（钉 Log 的 -Inf 分支）、
# high/low 有并列段（钉 Std/Rsquare 的 std≈0 → NaN 分支）。任何一侧随机生成都会让
# 「比的是同一张表」这条前提失效。
PINNED_INPUTS: dict[str, list[float]] = {
    "close": [
        0.0, 10.4, 10.1, 9.8, 10.2, 10.9, 11.3, 11.0, 10.6, 10.3,
        10.8, 11.5, 12.0, 11.7, 11.2, 10.5, 10.1, 10.7, 11.4, 11.9,
        12.4, 12.1, 11.6, 11.1, 10.6, 10.2, 10.5, 11.0, 11.6, 12.2,
        12.8, 13.1, 12.7, 12.2, 11.8, 11.4, 11.0, 10.7, 10.3, 9.9,
        10.0, 10.6, 11.2, 11.8, 12.3, 12.9, 13.4, 13.0, 12.6, 12.1,
        11.7, 11.3, 10.9, 10.5, 10.1, 9.7, 9.4, 9.1, 8.8, 8.5,
    ],
    "open": [
        10.1, 10.2, 10.3, 10.0, 10.5, 11.0, 11.1, 10.8, 10.4, 10.6,
        10.9, 11.3, 11.9, 12.1, 11.4, 10.7, 10.3, 10.5, 11.2, 11.7,
        12.2, 12.5, 11.8, 11.3, 10.8, 10.4, 10.7, 11.2, 11.9, 12.4,
        12.9, 13.2, 12.8, 12.3, 11.9, 11.5, 11.1, 10.8, 10.4, 10.0,
        9.9, 10.7, 11.3, 11.9, 12.4, 13.0, 13.5, 13.1, 12.7, 12.2,
        11.8, 11.4, 11.0, 10.6, 10.2, 9.8, 9.5, 9.2, 8.9, 8.6,
    ],
    "high": [
        10.6, 10.9, 10.7, 10.3, 10.9, 11.5, 11.9, 11.6, 11.1, 10.9,
        11.3, 12.0, 12.6, 12.3, 11.8, 11.0, 10.7, 11.3, 12.0, 12.5,
        13.0, 12.7, 12.2, 11.7, 11.2, 10.8, 11.1, 11.6, 12.2, 12.8,
        13.4, 13.7, 13.3, 12.8, 12.4, 12.0, 11.6, 11.3, 10.9, 10.5,
        10.6, 11.2, 11.8, 12.4, 12.9, 13.5, 14.0, 13.6, 13.2, 12.7,
        12.3, 11.9, 11.5, 11.1, 10.7, 10.3, 10.0, 9.7, 9.4, 9.1,
    ],
    "low": [
        9.7, 9.9, 9.6, 9.3, 9.8, 10.5, 10.9, 10.6, 10.2, 9.9,
        10.4, 11.1, 11.6, 11.3, 10.8, 10.1, 9.7, 10.3, 11.0, 11.5,
        12.0, 11.7, 11.2, 10.7, 10.2, 9.8, 10.1, 10.6, 11.2, 11.8,
        12.4, 12.7, 12.3, 11.8, 11.4, 11.0, 10.6, 10.3, 9.9, 9.5,
        9.6, 10.2, 10.8, 11.4, 11.9, 12.5, 13.0, 12.6, 12.2, 11.7,
        11.3, 10.9, 10.5, 10.1, 9.7, 9.3, 9.0, 8.7, 8.4, 8.1,
    ],
    "volume": [
        120, 0, 80, 40, 95, 130, 210, 175, 60, 45,
        70, 150, 240, 300, 260, 100, 55, 85, 160, 220,
        280, 330, 190, 120, 75, 50, 65, 140, 200, 250,
        310, 360, 230, 170, 110, 90, 70, 130, 180, 205,
        0, 140, 200, 260, 320, 380, 420, 350, 280, 210,
        150, 110, 90, 70, 60, 50, 40, 30, 20, 10,
    ],
    # vwap 在真读侧是 amount/volume 推出来的（KI-DATA-001）；这里给一条独立字面量，
    # 只为让 158 条里用到 $vwap 的表达式能被算出来，不代表任何真实口径。
    "vwap": [
        10.2, 10.3, 10.0, 9.9, 10.4, 11.1, 11.4, 11.2, 10.7, 10.5,
        11.0, 11.6, 12.1, 11.8, 11.3, 10.6, 10.2, 10.8, 11.5, 12.0,
        12.5, 12.2, 11.7, 11.2, 10.7, 10.3, 10.6, 11.1, 11.7, 12.3,
        12.9, 13.2, 12.8, 12.3, 11.9, 11.5, 11.1, 10.8, 10.4, 10.0,
        10.1, 10.7, 11.3, 11.9, 12.4, 13.0, 13.5, 13.1, 12.7, 12.2,
        11.8, 11.4, 11.0, 10.6, 10.2, 9.8, 9.5, 9.2, 8.9, 8.6,
    ],
}

DOLLAR_RE = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)")
COLUMN_PREFIX = "col_"
# 与 tdxfeed.alpha158.eval 同一分派：AST 节点 → 符号（符号 → 算子类的映射取自 ② 工件）。
BIN_SYMBOL = {ast.Add: "+", ast.Sub: "-", ast.Mult: "*", ast.Div: "/"}
CMP_SYMBOL = {ast.Gt: ">", ast.GtE: ">=", ast.Lt: "<", ast.LtE: "<=",
              ast.Eq: "==", ast.NotEq: "!="}


def _qlib_ops():
    try:
        from qlib.data import ops
    except ImportError as exc:
        sys.stderr.write(
            f"需要 qlib（{exc}）：本脚本只在环境孤岛里跑，请用 "
            f"{ROOT / 'venv-qlib' / 'bin' / 'python'} 运行；"
            "生产侧 venv 不装 pyqlib（见 AGENTS.md 例外二）。\n"
        )
        raise SystemExit(2) from exc
    return ops


def _ensure_island_only() -> None:
    """与导出器同一条判据：conda 前缀环境不自动关 user-site，会被 ~/.local 盖住 pandas。

    组合真值比对的意义在于「岛里的真值 vs 主 venv 的复现」，如果岛自己用的是被盖住的
    pandas，那比出来的差异属于哪一侧都说不清。这里直接复用导出器的实现，不抄第二份。
    """
    from export_alpha158_ops import _ensure_island_only as ensure

    ensure()


def _leaf_cls(ops_module):
    """把一条日线列包成「假表达式」：必须是 `ops.Expression` 的真子类（理由见导出器同名函数）。

    `__str__` 必须带列名：qlib 的 `Expression.load` 把 `str(self)` 纳入缓存键，所有叶子都
    印成同一个字符串时 `Mean(<leaf>,5)` 会让 $close 和 $open **共用一条缓存**，第二个拿到
    第一个的值——形状全对、数值串列，是这里最容易踩又最难看见的坑。
    """
    import pandas as pd

    class _Leaf(ops_module.Expression):
        def __init__(self, name: str, values: list[float]):
            self.name = name
            self.series = pd.Series([float(v) for v in values], dtype="float64")

        def load(self, instrument, start_index, end_index, *args):
            return self.series

        def _load_internal(self, instrument, start_index, end_index, *args):
            return self.series

        def __str__(self):
            return f"leaf:{self.name}"

        def get_longest_back_rolling(self):
            return 0

        def get_extended_window_size(self):
            return 0, 0

    class _Const(ops_module.Expression):
        """标量摊成的常量序列：只为一元负号（`-x` = `0 - x`）需要两侧同形。"""

        def __init__(self, value: float, n_rows: int):
            self.value = value
            self.series = pd.Series([float(value)] * n_rows, dtype="float64")

        def load(self, instrument, start_index, end_index, *args):
            return self.series

        def _load_internal(self, instrument, start_index, end_index, *args):
            return self.series

        def __str__(self):
            return f"const:{self.value!r}"

        def get_longest_back_rolling(self):
            return 0

        def get_extended_window_size(self):
            return 0, 0

    return _Leaf, _Const


def _to_python(expr: str) -> str:
    """`$close` → `col_close`，与主 venv 的 `tdxfeed.alpha158.eval._to_python` 同一改写。"""
    out = DOLLAR_RE.sub(lambda m: COLUMN_PREFIX + m.group(1), expr)
    if "$" in out:
        raise ValueError(f"表达式里有无法改写的 $ 用法：{expr!r}")
    return out


class _Ctx:
    """建树要用的环境：叶子表、常量类、符号→算子类映射、qlib 的 ops 模块、帧长。"""

    def __init__(self, leaves, const_cls, symbol_ops, ops_module, n_rows, expr):
        self.leaves = leaves
        self.const_cls = const_cls
        self.symbol_ops = symbol_ops
        self.ops = ops_module
        self.n_rows = n_rows
        self.expr = expr


def build_tree(node: ast.AST, ctx: _Ctx):
    """AST → qlib 算子树：函数名直接取 `qlib.data.ops.<名字>`，符号按 ② 的符号表取类。

    这里的分派形状与 `tdxfeed.alpha158.eval._eval_uncached` 一一对应（同一棵语法树、
    同一套拒绝条件），唯一区别是叶子之后交给 qlib 的类而不是主 venv 的函数——所以两边
    一处放宽、另一处没跟上时，比对会红，这正是想要的效果。
    """
    expr = ctx.expr
    if isinstance(node, ast.Expression):
        return build_tree(node.body, ctx)

    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise ValueError(f"{expr!r} 里有非数字字面量 {node.value!r}")
        return node.value

    if isinstance(node, ast.Name):
        if not node.id.startswith(COLUMN_PREFIX):
            raise ValueError(f"{expr!r} 里的名字 {node.id!r} 不是 $列")
        name = node.id[len(COLUMN_PREFIX):]
        if name not in ctx.leaves:
            raise ValueError(f"{expr!r} 用到列 {name!r}，但输入帧里没有这一列")
        return ctx.leaves[name]

    if isinstance(node, ast.UnaryOp):
        operand = build_tree(node.operand, ctx)
        if isinstance(node.op, ast.UAdd):
            return operand
        if isinstance(node.op, ast.USub):
            if isinstance(operand, (int, float)):
                return -operand  # qlib 的文法里 `-1.5` 就是个负数字面量
            # 操作数是表达式时，qlib 没有一元负号算子：它的文法里 `-x` 就是 `0 - x`。
            # 常量按帧长摊开——不能取 `operand.series`，复合表达式的节点没有这个属性。
            sub = getattr(ctx.ops, ctx.symbol_ops["-"])
            return sub(ctx.const_cls(0.0, ctx.n_rows), operand)
        raise ValueError(f"{expr!r} 的一元运算符 {type(node.op).__name__} 不在求值面上")

    if isinstance(node, ast.BinOp):
        symbol = BIN_SYMBOL.get(type(node.op))
        if symbol is None:
            raise ValueError(f"{expr!r} 的运算符 {type(node.op).__name__} 不在求值面上")
        left = build_tree(node.left, ctx)
        right = build_tree(node.right, ctx)
        return getattr(ctx.ops, ctx.symbol_ops[symbol])(left, right)

    if isinstance(node, ast.Compare):
        if len(node.ops) != 1:
            raise ValueError(f"{expr!r} 是链式比较，qlib 的表达式文法不产这种形状")
        symbol = CMP_SYMBOL.get(type(node.ops[0]))
        if symbol is None:
            raise ValueError(f"{expr!r} 的比较运算符 {type(node.ops[0]).__name__} 不在求值面上")
        left = build_tree(node.left, ctx)
        right = build_tree(node.comparators[0], ctx)
        return getattr(ctx.ops, ctx.symbol_ops[symbol])(left, right)

    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name):
            raise ValueError(f"{expr!r} 的调用形式不支持（只认 `算子名(...)`）")
        if node.keywords:
            raise ValueError(f"{expr!r} 里 {node.func.id} 用了关键字参数：qlib 的表达式只有位置参数")
        cls = getattr(ctx.ops, node.func.id, None)
        if cls is None:
            raise ValueError(f"qlib.data.ops 里没有 {node.func.id}：① 清单与岛内版本分叉了")
        args = [build_tree(a, ctx) for a in node.args]
        return cls(*args)

    raise ValueError(f"{expr!r} 含求值面之外的节点 {type(node).__name__}")


def _encode(value) -> float | str:
    """与 ② 一致的编码：非有限值写成字符串，JSON 才读得回来。"""
    v = float(value)
    if math.isnan(v):
        return "NaN"
    if v == math.inf:
        return "Infinity"
    if v == -math.inf:
        return "-Infinity"
    return v


def evaluate_all(payload: dict, ops_payload: dict) -> dict:
    """在岛里把 158 条逐个算完，返回 {因子名: [编码后的值…]}。"""
    ops_module = _qlib_ops()
    leaf_cls, const_cls = _leaf_cls(ops_module)
    from qlib.data.cache import H

    leaves = {name: leaf_cls(name, vals) for name, vals in PINNED_INPUTS.items()}
    symbol_ops = {**ops_payload["arithmetic_symbols"], **ops_payload["comparison_symbols"]}
    n = len(next(iter(PINNED_INPUTS.values())))
    out: dict[str, list] = {}
    for feat in payload["features"]:
        # 每条表达式重开 qlib 的表达式缓存：叶子标签已足够唯一，但重开一次就没有
        # 「跨条污染」这个变量留在结论里。
        H.clear()
        ctx = _Ctx(leaves, const_cls, symbol_ops, ops_module, n, feat["expr"])
        tree = build_tree(ast.parse(_to_python(feat["expr"]), mode="eval"), ctx)
        series = tree.load(INSTRUMENT, 0, n - 1)
        out[feat["name"]] = [_encode(v) for v in list(series)]
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="在岛里用真 qlib 算子树跑 158 条表达式（供主 venv 的求值器逐值比对）")
    ap.add_argument("--inputs", default=None, help="把输入帧写到这里（单跑这一项 = 只发输入）")
    ap.add_argument("--out", default=None, help="把 158 列真值写到这里（需要 --inputs 之外的两份工件）")
    args = ap.parse_args(argv)

    if args.inputs:
        atomic_write(Path(args.inputs), json.dumps(
            PINNED_INPUTS, ensure_ascii=False, sort_keys=True, allow_nan=False))
        print(f"WROTE {args.inputs} 列 {len(PINNED_INPUTS)} 行 {len(next(iter(PINNED_INPUTS.values())))}")
        if not args.out:
            return 0

    if not args.out:
        sys.stderr.write("需要 --out（组合真值写到哪）；--inputs 可单独跑，只发输入帧\n")
        return 2

    _qlib_ops()
    _ensure_island_only()
    for path in (EXPR_ARTIFACT, OPS_ARTIFACT):
        if not path.exists():
            sys.stderr.write(f"缺工件 {path}\n")
            return 1
    payload = json.loads(EXPR_ARTIFACT.read_text(encoding="utf-8"))
    ops_payload = json.loads(OPS_ARTIFACT.read_text(encoding="utf-8"))
    result = evaluate_all(payload, ops_payload)
    text = json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False)
    json.loads(text)  # 自证严格 JSON
    atomic_write(Path(args.out), text)
    print(f"WROTE {args.out} 因子 {len(result)} 条")
    return 0


if __name__ == "__main__":
    # 与导出器一致：被外部屏蔽隔离时宁可失败，也不产出一份"看起来一样"的真值。
    if os.environ.get(REEXEC_MARKER) != "1":
        _ensure_island_only()
    raise SystemExit(main())
