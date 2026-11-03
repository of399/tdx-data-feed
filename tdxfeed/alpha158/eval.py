"""Alpha158 表达式求值器：读 ① 的清单工件，用 `ops` 层在主 venv 里算出 158 维因子。

设计约束（守卫 `tests/test_alpha158_eval.py` 逐条钉住）
----------------------------------------------------
**封闭解释器，不用 eval。** 表达式字符串来自岛内导出的工件，但"谁改了工件谁就能在跑因子的
机器上执行代码"这条通道一次都不该开。这里把表达式 `ast.parse` 之后**逐节点自己求值**，
只认：

- `BinOp` 的 `+ - * /` 与 `Compare` 的 `> >= < <= == !=`——两侧都**不直接用 Python
  运算符**，而是按 ② 工件的符号表映射到 qlib 的 `Add`/`Sub`/`Mul`/`Div`/`Gt`/`Lt`…
  走 `ops` 层。`>` 对应的是 `Gt`（比大小）而**不是** `Greater`（取大），这类同名不同义
  的映射抄不得；链式比较（`a<b<c`）qlib 的文法不产，这里直接拒；
- `UnaryOp` 的 `- +`；
- 数字字面量（`1e-12` 这类容差项）；
- `$列` → 帧里的列（先做 `$close → col_close` 的文本改写，再交给解析器）；
- `ops.REGISTRY` 里的算子名，且只按位置传参。

其它节点（Attribute、Subscript、keyword、comprehension、lambda……）一律
`Alpha158Error`。宁可拒收一条表达式，也不要"这一维静默算成 NaN"——158 维里少一维在
下游宽表上看不出来。

名单闭合取自工件：列名必须在 `payload["columns"]` 里，函数名必须在 `REGISTRY` 里。
qlib 改了默认配置或有人手改工件时，这里报错，`tests/test_alpha158_semantics.py` 与
`tests/test_alpha158_artifact.py` 也会同步抓版本与算子集的漂移。

缓存按**本次调用**分键（同一个 AST 形状只算一次，`Ref($close, 1)` 这类子式在清单里
重复出现几十次）。跨调用共享缓存会让第二次求值拿到第一次的股票的值，形状全对、只有
数值是错的，是最难查的一类事故。
"""
from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from tdxfeed.alpha158.ops import REGISTRY

ROOT = Path(__file__).resolve().parents[2]
ARTIFACT_REL = Path("config") / "alpha158_expressions.json"
OPS_ARTIFACT_REL = Path("config") / "alpha158_ops.json"

# `$close` 是 qlib 的列引用写法，改成合法 Python 标识符后交给解析器。
DOLLAR_RE = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)")
COLUMN_PREFIX = "col_"

# AST 的运算符节点 → **符号**，符号 → qlib 类名 由 ② 工件的 `arithmetic_symbols` /
# `comparison_symbols` 给。分成两跳是为了让「`>` 到底走哪个类」这条口径留在权威工件里，
# 而不是在本模块再抄一份常量表——抄了就有两处会分叉。
_BIN_OPS: dict[type[ast.operator], str] = {
    ast.Add: "+",
    ast.Sub: "-",
    ast.Mult: "*",
    ast.Div: "/",
}
_CMP_OPS: dict[type[ast.cmpop], str] = {
    ast.Gt: ">",
    ast.GtE: ">=",
    ast.Lt: "<",
    ast.LtE: "<=",
    ast.Eq: "==",
    ast.NotEq: "!=",
}


class Alpha158Error(ValueError):
    """表达式超出求值面 / 列或算子不认识 / 工件与实现不闭合。"""


def load_payload(path: Path | None = None) -> dict:
    target = ROOT / ARTIFACT_REL if path is None else path
    if not target.exists():
        raise Alpha158Error(
            f"缺清单工件 {target.relative_to(ROOT) if target.is_relative_to(ROOT) else target}："
            "先跑 `make qlib-export`（在 venv-qlib 岛里）导出 158 条表达式"
        )
    return json.loads(target.read_text(encoding="utf-8"))


def load_ops_payload(path: Path | None = None) -> dict:
    target = ROOT / OPS_ARTIFACT_REL if path is None else path
    if not target.exists():
        raise Alpha158Error(
            f"缺算子语义工件 {target.name}：先跑 `make qlib-export-ops`（岛里）——"
            "符号到算子的映射只有那份金标准里有，本模块不抄写第二份"
        )
    return json.loads(target.read_text(encoding="utf-8"))


@dataclass(frozen=True)
class Surface:
    """一次求值允许碰到的东西：列名单 + 符号→算子映射。

    两者都取自工件（① 给列，② 给符号映射），所以「清单里出现了新符号」会在
    `op_for()` 当场报错，而不是被本模块的一份本地表悄悄接住。
    """

    columns: tuple[str, ...]
    symbol_ops: dict[str, str] = field(hash=False)

    def op_for(self, symbol: str, kind: str, expr: str) -> str:
        try:
            name = self.symbol_ops[symbol]
        except KeyError as exc:
            raise Alpha158Error(
                f"表达式 {expr!r} 用了 {kind} 运算符 {symbol!r}，但算子工件的符号表里没有它："
                "求值面要扩，先扩金标准（`scripts/qlib_island/export_alpha158_ops.py`），"
                "不要在这里加本地映射"
            ) from exc
        if name not in REGISTRY:
            raise Alpha158Error(
                f"{symbol!r} 在工件里指向 {name}，但 tdxfeed.alpha158.ops 没实现它"
            )
        return name


def build_surface(payload: dict | None = None, ops_payload: dict | None = None) -> Surface:
    d = payload or load_payload()
    o = ops_payload or load_ops_payload()
    symbols = {**o["arithmetic_symbols"], **o["comparison_symbols"]}
    return Surface(columns=tuple(d["columns"]), symbol_ops=symbols)


def feature_names(payload: dict | None = None) -> list[str]:
    return [f["name"] for f in (payload or load_payload())["features"]]


def expression_for(name: str, payload: dict | None = None) -> str:
    for feat in (payload or load_payload())["features"]:
        if feat["name"] == name:
            return feat["expr"]
    raise Alpha158Error(f"清单里没有这一维：{name!r}")


def caveats(payload: dict | None = None) -> list[str]:
    """工件自带的口径提示（vwap 伪信号、hfq 读侧权威、涨跌停不在本清单）。"""
    return list((payload or load_payload()).get("caveats", []))


def _to_python(expr: str) -> str:
    """`$col` → `col_<name>`；改写后仍残留 `$` 说明表达式里有求值面之外的东西。"""
    out = DOLLAR_RE.sub(lambda m: COLUMN_PREFIX + m.group(1), expr)
    if "$" in out:
        raise Alpha158Error(f"表达式里有无法改写的 $ 用法：{expr!r}")
    return out


def _col_name(node: ast.Name) -> str:
    if not node.id.startswith(COLUMN_PREFIX):
        raise Alpha158Error(f"不认识的名字 {node.id!r}：列要写成 $col，函数要在算子表里")
    return node.id[len(COLUMN_PREFIX):]


def _resolve_column(name: str, frame: pd.DataFrame, surface: Surface, expr: str) -> pd.Series:
    if name not in surface.columns:
        raise Alpha158Error(
            f"表达式 {expr!r} 引用了工件没声明的列 ${name}（声明的是 {list(surface.columns)}）"
        )
    if name not in frame.columns:
        hint = ""
        if name == "vwap":
            hint = "：日线表没有 vwap 列，只能由 amount/volume 推，而 2026-09-16 起 amount 是代理值（KI-DATA-001）"
        raise Alpha158Error(f"输入帧缺列 {name!r}{hint}；现有列 {list(frame.columns)}")
    return frame[name]


def _call(name: str, node: ast.AST, args: list[Any], expr: str) -> Any:
    fn = REGISTRY.get(name)
    if fn is None:
        raise Alpha158Error(
            f"表达式 {expr!r} 用了算子表里没有的 {name!r}；"
            f"已实现 {sorted(REGISTRY)}（补齐实现前宁可报错，不要静默跳这一维）"
        )
    # `node` 可能是 Call，也可能是 BinOp/Compare（四则与比较走的是同一个算子入口），
    # 后者没有 keywords 字段——用 getattr 而不是直接取，否则报错会被 AttributeError 顶掉。
    if getattr(node, "keywords", None):
        raise Alpha158Error(f"表达式 {expr!r} 里 {name} 用了关键字参数：qlib 的表达式只有位置参数")
    return fn(*args)


def _eval_node(node: ast.AST, frame: pd.DataFrame, surface: Surface, expr: str,
               cache: dict[str, Any]) -> Any:
    key = ast.dump(node)
    if key in cache:
        return cache[key]
    value = _eval_uncached(node, frame, surface, expr, cache)
    cache[key] = value
    return value


def _eval_uncached(node: ast.AST, frame: pd.DataFrame, surface: Surface, expr: str,
                   cache: dict[str, Any]) -> Any:
    if isinstance(node, ast.Expression):
        return _eval_node(node.body, frame, surface, expr, cache)

    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise Alpha158Error(f"表达式 {expr!r} 里有非数字字面量 {node.value!r}")
        return node.value

    if isinstance(node, ast.Name):
        return _resolve_column(_col_name(node), frame, surface, expr)

    if isinstance(node, ast.UnaryOp):
        operand = _eval_node(node.operand, frame, surface, expr, cache)
        if isinstance(node.op, ast.USub):
            return -operand
        if isinstance(node.op, ast.UAdd):
            return +operand
        raise Alpha158Error(f"表达式 {expr!r} 里的一元运算符 {type(node.op).__name__} 不在求值面上")

    if isinstance(node, ast.BinOp):
        symbol = _BIN_OPS.get(type(node.op))
        if symbol is None:
            raise Alpha158Error(
                f"表达式 {expr!r} 里的运算符 {type(node.op).__name__} 不在求值面"
                f"（只认 {' '.join(_BIN_OPS.values())}）"
            )
        left = _eval_node(node.left, frame, surface, expr, cache)
        right = _eval_node(node.right, frame, surface, expr, cache)
        return _call(surface.op_for(symbol, "四则", expr), node, [left, right], expr)

    if isinstance(node, ast.Compare):
        # qlib 的表达式没有链式比较（`a < b < c` 在它的文法里不成立），所以只认单侧；
        # 两侧都放行会让 `Ops<Ops<Ops` 这种形状在这里静默算成「按 Python 短路语义」的结果，
        # 而 Python 的链式比较与 qlib 的任何算子都不对应。
        if len(node.ops) != 1:
            raise Alpha158Error(
                f"表达式 {expr!r} 是链式比较（{len(node.ops)} 个运算符）："
                "qlib 的表达式文法不产这种形状，本求值器也不猜它该等于哪个算子"
            )
        symbol = _CMP_OPS.get(type(node.ops[0]))
        if symbol is None:
            raise Alpha158Error(
                f"表达式 {expr!r} 里的比较运算符 {type(node.ops[0]).__name__} 不在求值面"
                f"（只认 {' '.join(_CMP_OPS.values())}）"
            )
        left = _eval_node(node.left, frame, surface, expr, cache)
        right = _eval_node(node.comparators[0], frame, surface, expr, cache)
        return _call(surface.op_for(symbol, "比较", expr), node, [left, right], expr)

    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name):
            raise Alpha158Error(f"表达式 {expr!r} 的调用形式不被支持（只认 `算子名(...)`）")
        args = [_eval_node(a, frame, surface, expr, cache) for a in node.args]
        return _call(node.func.id, node, args, expr)

    raise Alpha158Error(
        f"表达式 {expr!r} 含求值面之外的节点 {type(node).__name__}："
        "只认四则与比较运算符、数字、$列 与算子表里的函数"
    )


def evaluate_feature(expr: str, frame: pd.DataFrame, surface: Surface | None = None,
                     cache: dict[str, Any] | None = None) -> Any:
    """求一条表达式的值（供逐列调试用）。"""
    s = surface or build_surface()
    tree = ast.parse(_to_python(expr), mode="eval")
    return _eval_node(tree, frame, s, expr, {} if cache is None else cache)


def evaluate_frame(frame: pd.DataFrame, payload: dict | None = None,
                   ops_payload: dict | None = None) -> pd.DataFrame:
    """按工件顺序把 158 维算成一张表；列名与顺序与 `payload["features"]` 逐项一致。"""
    d = payload or load_payload()
    surface = build_surface(d, ops_payload)
    cache: dict[str, Any] = {}
    out: dict[str, Any] = {}
    for feat in d["features"]:
        name, expr = feat["name"], feat["expr"]
        if name in out:
            raise Alpha158Error(f"工件里因子名 {name!r} 重复，宽表会覆盖上一列")
        try:
            out[name] = evaluate_feature(expr, frame, surface, cache)
        except Alpha158Error:
            raise
        except Exception as exc:  # 报错要带上下文再原样上抛，不许吞
            raise Alpha158Error(f"求值失败 {name} = {expr!r}：{type(exc).__name__}: {exc}") from exc
    return pd.DataFrame(out, index=frame.index)
