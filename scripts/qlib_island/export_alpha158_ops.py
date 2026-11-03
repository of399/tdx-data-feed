#!/usr/bin/env python
"""岛内导出器：把 Alpha158 求值面用到的算子的**真语义**落成金标准工件。

为什么还要第二份工件
--------------------
`config/alpha158_expressions.json`（步骤 ①）给的是形状：158 条表达式字符串、算子名单、窗口。
但把表达式拿到主 venv 重算时，会出错的是语义而不是形状，qlib 0.9.7 有四处与「手写一个
rolling」不等价：

1. `Rolling._load_internal` 一律 `series.rolling(N, min_periods=1)` ⇒ 前 N-1 行**也出值**
   （pandas 默认 `min_periods=N` 会多出一片 NaN，形状一样、值全不同）；
2. `Rank(x, N)` = `rolling(N, min_periods=1).rank(pct=True)`，是**逐标的时序百分位**；
   pandas < 1.4 时它回退到 `percentileofscore`，两条路对并列值的口径不同；
3. `Slope/Rsquare/Resi` 不逐窗重算，而是 `qlib/data/_libs/rolling.pyx` 的**增量 OLS**
   （x 用 `self.window` 常数、出窗按 `i_sum` 衰减、NaN 跳过但占位并计入 `na_count`）；
4. `Rsquare` 与 `Corr` 额外把「滚动 std ≈ 0（atol=2e-5）」的位置显式置 NaN；
   `Log/Abs/Greater/Less` 是 `np.log/np.abs/np.maximum/np.minimum`（模块级
   `np.seterr(invalid="ignore")` ⇒ 负数得 NaN、0 得 −Inf 且不抛）；
   `IdxMax/IdxMin` 是 `apply(lambda x: x.argmax() ± 1, raw=True)`，1 基且不跳过 NaN。

主 venv 装不上 pyqlib，所以这些语义无法在 CI 里现场对照。本脚本在岛里**走 qlib 自己的代码路径**
（`_load_internal`，不经数据提供方）对固定输入算一遍，把输入与输出一起写进
`config/alpha158_ops.json`；主 venv 的 `tdxfeed/alpha158/ops.py` 逐值复现它，守卫
`tests/test_alpha158_semantics.py` 判定。

怎么用
------
    venv-qlib/bin/python scripts/qlib_island/export_alpha158_ops.py          # 写工件
    venv-qlib/bin/python scripts/qlib_island/export_alpha158_ops.py --check   # 岛与工件是否一致

三条约定
--------
1. **只能在岛里跑**：`import qlib` 失败即非零退出并说明该用哪个解释器（与 ① 同一条判据）。
2. **金标准出自真代码路径**：不 import qlib 的解释器产不出这份工件；本脚本不自己算任何一个
   算子，只调用 `qlib.data.ops` 的类。
3. **原子写 + 严格 JSON**：非有限值编码成字符串（`NaN` / `Infinity` / `-Infinity`），
   `allow_nan=False` 保证工件不会写出标准解析器读不了的裸字面量。

内容 hash 覆盖除 `content_sha256` 与 `provenance.generated_at` 之外的全部正文：日期天天变，
纳进去会让 `--check` 天天假报漂移；版本漂移要体现在 `--check` 失败，而不是哈希里。
"""
from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import math
import os
import re
import sys
from datetime import date
from importlib.metadata import version as pkg_version
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# 复用 ① 的原子写与稳定文本形态：两份工件必须走同一个落盘口径，别各抄一份。
from export_alpha158 import atomic_write, dump

ROOT = Path(__file__).resolve().parents[2]
ARTIFACT_REL = Path("config") / "alpha158_ops.json"
SOURCE_LABEL = (
    "qlib.data.ops.<Op>._load_internal + qlib.data._libs.rolling (pyqlib 0.9.7 真代码路径)"
)
INSTRUMENT = "sh600000"  # 只是占位参数：_load_internal 不用它，不经数据提供方
WINDOWS = (5, 20)
QUANTILE_POINTS = (0.5, 0.8)

# 表达式里除了 `+ - * /` 还会写 `>` / `<`（158 条里有 15 条这么写，如
# `CNTP5 = Mean($close>Ref($close, 1), 5)`）。qlib 把它们解析成 Gt/Lt 这类成对算子，
# 语义与 `np.maximum` 那组同族但**结果不同**（`Greater` 取大、`Gt` 比大小）。
# 求值面里这两个符号必须也有金标准，否则比较类的判定只能靠猜。
#
# 符号 → 类名这张表**不手写**：权威是 `qlib.data.base.Expression` 的运算符重载
# （`__gt__` 里写的是 `return Gt(self, other)`），照抄一份常量表就等于把「qlib 以后改
# 了重载」这类漂移藏起来。`_symbol_maps` 从那些方法的源码里把类名抠出来，再核对它
# 真的挂在 `qlib.data.ops` 上——漂移时这里直接红，而不是产出一份语义错位的工件。
# 双字符符号要先判 `>=` 再判 `>`，否则一个算子会被记成另一个。
SYMBOL_DUNDERS = {
    ">": "__gt__", ">=": "__ge__", "<": "__lt__", "<=": "__le__",
    "==": "__eq__", "!=": "__ne__",
    "+": "__add__", "-": "__sub__", "*": "__mul__", "/": "__truediv__",
}
COMPARISON_ORDER = (">", ">=", "<", "<=", "==", "!=")
ARITHMETIC_ORDER = ("+", "-", "*", "/")

ENCODING = {
    "form": "string",
    "tokens": {"nan": "NaN", "positive_infinity": "Infinity", "negative_infinity": "-Infinity"},
    "note": "JSON 没有合法的 NaN/Infinity 字面量；非有限值一律写成字符串，float() 能原样还原",
}

# 输入序列全部是字面量：金标准必须能在任何环境重算出同一个输入，
# 用随机种子就意味着测试环境重导时输入不同、比对失去意义。
# a  价格形（有升有降、非整）      b 成交量形（含 0 ⇒ Log 出 -Infinity）
# c  残缺形（含 NaN 与负数 ⇒ 钉住 NaN 跳过与 Log 的 NaN 分支）
# k  常量形（钉住 Rsquare/Corr 的 std≈0 → NaN 分支）
INPUTS: dict[str, list[float]] = {
    "a": [
        10.0, 10.4, 10.1, 9.8, 10.2, 10.9, 11.3, 11.0, 10.6, 10.3,
        10.8, 11.5, 12.0, 11.7, 11.2, 10.5, 10.1, 10.7, 11.4, 11.9,
        12.4, 12.1, 11.6, 11.1, 10.6, 10.2, 10.5, 11.0, 11.6, 12.2,
        12.8, 13.1, 12.7, 12.2, 11.8, 11.4, 11.0, 10.7, 10.3, 9.9,
    ],
    "b": [
        0, 120, 80, 40, 95, 130, 210, 175, 60, 45,
        70, 150, 240, 300, 260, 100, 55, 85, 160, 220,
        280, 330, 190, 120, 75, 50, 65, 140, 200, 250,
        310, 360, 230, 170, 110, 90, 70, 130, 180, 205,
    ],
    "c": [
        5.0, -1.0, 3.0, float("nan"), 2.0, 7.0, 0.0, -3.0, 4.0, 6.0,
        float("nan"), 1.0, 8.0, 2.0, -2.0, 5.0, 5.0, float("nan"), 5.0, 5.0,
        9.0, -0.5, 3.5, float("nan"), 6.5, 1.5, 4.5, 7.5, -4.0, 2.5,
        8.5, float("nan"), 0.0, 5.5, 3.0, -2.5, 6.0, 9.5, 1.0, float("nan"),
    ],
    "k": [7.5] * 40,
}

OP_MODULES = ("qlib.data.ops",)

_RETURN_CLASS_RE = re.compile(r"return\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(")


def _symbol_maps(ops_module) -> tuple[dict, dict]:
    """从 `Expression` 的运算符重载里读出「符号 → qlib 类名」，分成比较组与四则组。

    每条重载的函数体就一行 `return Gt(self, other)`；正则抠出类名后还要核对它真挂在
    `qlib.data.ops` 上，否则说明 qlib 换了实现方式（比如改成 `OpFactory(...)`），
    这套抽取口径本身失效了——那也必须红，而不是导出一份把 `>` 当 `Greater` 的工件。
    """
    from qlib.data.base import Expression

    comparison: dict[str, str] = {}
    arithmetic: dict[str, str] = {}
    for sym, dunder in SYMBOL_DUNDERS.items():
        fn = getattr(Expression, dunder, None)
        if fn is None:
            raise SystemExit(f"Expression 没有 {dunder}：qlib 的运算符重载形态变了，抽取口径要重设计")
        src = inspect.getsource(fn)
        m = _RETURN_CLASS_RE.search(src)
        if m is None:
            raise SystemExit(
                f"Expression.{dunder} 的返回形态不是 `return <类>(...)`，抠不出类名：\n{src}"
            )
        cls_name = m.group(1)
        if not hasattr(ops_module, cls_name):
            raise SystemExit(
                f"Expression.{dunder} 指向 {cls_name}，但 qlib.data.ops 里没有这个类——"
                "符号到算子的映射需要人工重新核对"
            )
        (comparison if sym in COMPARISON_ORDER else arithmetic)[sym] = cls_name
    if set(comparison) != set(COMPARISON_ORDER) or set(arithmetic) != set(ARITHMETIC_ORDER):
        raise SystemExit("抽出来的符号表不完整，宁可不出工件")
    return comparison, arithmetic


def _qlib_ops():
    try:
        import pandas  # noqa: F401  # 岛里必须有 pandas，qlib 的算子全建立在它上面
        from qlib.data import ops
    except ImportError as exc:
        sys.stderr.write(
            f"需要 qlib（{exc}）：本脚本只在环境孤岛里跑，请用 "
            f"{ROOT / 'venv-qlib' / 'bin' / 'python'} 运行；"
            "生产侧 venv 不装 pyqlib（wheel 只到 cp312，且会带进 mlflow/gym/cvxpy）。\n"
        )
        raise SystemExit(2) from exc
    return ops


_SERIES_BASE = None


def _make_series_cls(ops_module):
    """造一个「假表达式」类：必须真的是 `qlib.data.ops.Expression` 的子类。

    `PairRolling`/`NpPairOperator` 用 `isinstance(feature, Expression)` 决定「走 load」还是
    「当成标量」，鸭子类型会被判成标量然后在 `.rolling(...)` 上炸——第一次跑就踩到了。
    所以这里显式继承 Expression，只把 `load` 换成回吐固定序列，绕开数据提供方。
    """
    class _Series(ops_module.Expression):
        def __init__(self, values: list[float]):
            import pandas as pd

            self.series = pd.Series(list(values), dtype="float64")

        def load(self, instrument, start_index, end_index, *args):
            return self.series

        def _load_internal(self, instrument, start_index, end_index, *args):
            return self.series

        def __str__(self):
            return "<pinned-series>"

        def get_longest_back_rolling(self):
            return 0

        def get_extended_window_size(self):
            return 0, 0

    return _Series


def _Series(values: list[float]):  # 命名与 qlib 的类同形，读调用处才顺
    assert _SERIES_BASE is not None, "只能经 _qlib_ops() 之后调用：岛还没就位"
    return _SERIES_BASE(values)


def _encode(value: float) -> float | str:
    v = float(value)
    if math.isnan(v):
        return ENCODING["tokens"]["nan"]
    if v == math.inf:
        return ENCODING["tokens"]["positive_infinity"]
    if v == -math.inf:
        return ENCODING["tokens"]["negative_infinity"]
    return v


def _encode_series(out) -> list:
    return [_encode(v) for v in list(out)]


def _arity(cls) -> int:
    params = list(inspect.signature(cls.__init__).parameters.values())[1:]  # 去掉 self
    return len([p for p in params if p.kind in (p.POSITIONAL_OR_KEYWORD, p.POSITIONAL_ONLY)])


def _source_material(ops_module, cls) -> str:
    """该类源码 +（若 `_load_internal` 来自基类）基类那段 + 被引用的 `_libs` 源码。

    哈希要能抓住「Mean 的语义变了」这种漂移，而语义其实写在基类 `Rolling._load_internal`
    里；只 hash 子类就得不到。Slope/Rsquare/Resi 的真算法在 `rolling.pyx`，同理必须并进来。
    """
    parts = [inspect.getsource(cls)]
    impl = getattr(cls, "_load_internal", None)
    owner = getattr(impl, "__qualname__", "").split(".")[0]
    if impl is not None and owner and owner != cls.__name__:
        parts.append(inspect.getsource(impl))
    text = parts[0]
    for lib_fn in ("rolling_slope", "rolling_rsquare", "rolling_resi", "rolling_mean"):
        if lib_fn in text:
            parts.append(_libs_source(ops_module))
            break
    return "\n".join(parts)


def _libs_source(ops_module) -> str:
    """`qlib/data/_libs/rolling` 的源码：优先 .pyx，取不到就退化成对 .so 字节取哈希。

    退化成字节也够用——它照样会在 qlib 版本变动时漂，只是漂了读不出diff。
    """
    from qlib.data._libs import rolling as rolling_lib

    here = Path(rolling_lib.__file__)
    pyx = here.with_suffix(".pyx")
    if pyx.exists():
        return pyx.read_text(encoding="utf-8")
    return repr(sorted(here.read_bytes()[:0]) or hashlib.sha256(here.read_bytes()).hexdigest())


ISLAND_PREFIX = ROOT / "venv-qlib"
REEXEC_MARKER = "TDX_QLIB_ISLAND_REEXEC"


def _ensure_island_only() -> None:
    """金标准的数值口径必须由岛内解释器独占，所以要先把 user-site 关掉。

    实测踩到的坑：`venv-qlib/` 是 conda **前缀环境**（不是 venv），`~/.local/lib/python3.12/
    site-packages` 在 sys.path 里排在岛自己的 site-packages **前面**。第一次导出时 pandas
    用的是 user-site 的 3.0.5，而岛里装的是 3.1.0——同一份脚本、同一个 qlib，rolling 的
    `rank(pct=True)` / `corr` / 预热段 NaN 政策在不同 pandas 小版本上并不保证一致。
    「金标准」如果出自一个没记录、还会被盖住的环境，它就只是个数字堆。

    做法：没有被显式隔离过就先用 `PYTHONNOUSERSITE=1` 把自己 execve 一遍（execve 换掉的
    是进程本身，前面 import 过的东西都不作数），再核对 pandas / qlib 的模块路径确实落在
    岛内——落在外面就直接退出，而不是导出一份看起来和上次一样的工件。
    """
    if os.environ.get(REEXEC_MARKER) != "1":
        env = dict(os.environ, PYTHONNOUSERSITE="1", **{REEXEC_MARKER: "1"})
        os.execve(sys.executable, [sys.executable, *sys.argv], env)
    import pandas
    import qlib

    for name, mod in (("pandas", pandas), ("numpy", __import__("numpy")), ("qlib", qlib)):
        path = Path(mod.__file__).resolve()
        if ISLAND_PREFIX not in path.parents:
            sys.stderr.write(
                f"{name} 解析到岛外：{path}\n"
                f"金标准必须由 {ISLAND_PREFIX / 'bin' / 'python'} 独占产出；"
                f"手动运行时带上 PYTHONNOUSERSITE=1（本脚本自己会做，被外部屏蔽了就要报错）。\n"
            )
            raise SystemExit(2)


def _reference_plan(comparison: dict[str, str], arithmetic: dict[str, str]) -> dict[str, list[dict]]:
    """每个算子跑哪几组输入。**args 的键序就是位置序**，主 venv 侧按位置调用。

    `comparison` / `arithmetic` 是 `_symbol_maps` 从 qlib 的运算符重载里抽出来的
    「符号 → 类名」表；计划要覆盖的是**求值面全集**，所以类名不写死在这里，由表给。
    """
    rolling_single = [
        "Ref", "Mean", "Sum", "Std", "Max", "Min",
        "Rank", "Slope", "Rsquare", "Resi", "IdxMax", "IdxMin",
    ]
    plan: dict[str, list[dict]] = {
        name: [{"feature": s, "N": n} for s in ("a", "c") for n in WINDOWS]
        for name in rolling_single
    }
    plan["Rsquare"] += [{"feature": "k", "N": n} for n in WINDOWS]  # 常量列 ⇒ std≈0 置 NaN 分支
    plan["Quantile"] = [
        {"feature": s, "N": n, "qscore": q} for s in ("a", "c") for n in WINDOWS for q in QUANTILE_POINTS
    ]
    plan["Abs"] = [{"feature": s} for s in ("a", "c")]
    plan["Log"] = [{"feature": "b"}, {"feature": "c"}]
    plan["Greater"] = [
        {"feature_left": "a", "feature_right": "b"},
        {"feature_left": "c", "feature_right": "k"},
    ]
    plan["Less"] = [
        {"feature_left": "a", "feature_right": "b"},
        {"feature_left": "c", "feature_right": "k"},
    ]
    plan["Corr"] = [
        {"feature_left": "a", "feature_right": "b", "N": n} for n in WINDOWS
    ] + [
        {"feature_left": "k", "feature_right": "b", "N": WINDOWS[0]},  # 常量侧 ⇒ std = 0 → NaN 分支
        {"feature_left": "c", "feature_right": "a", "N": WINDOWS[1]},  # 含 NaN 侧 ⇒ corr 的 NaN 处理
    ]
    # 比较运算：两侧都是序列、序列对常量、序列对标量三种形状都要有——清单里
    # `Mean($close>Ref($close, 1), 5)` 用的是「序列对序列」，而 `Greater(..., 0)` 那类
    # 写法说明标量也必须能原样穿过成对算子。
    pair_shapes = [
        {"feature_left": "a", "feature_right": "b"},
        {"feature_left": "c", "feature_right": "k"},
        {"feature_left": "a", "feature_right": 10.5},
    ]
    for cls in comparison.values():
        plan[cls] = list(pair_shapes)
    for cls in arithmetic.values():
        plan[cls] = [
            *pair_shapes,
            # 除零分支只有 Div 用得上，但对四则统一带上：b 首值是 0 ⇒ Div(a,b) 首点 = +Inf、
            # Div(b,b) 首点 = 0/0 = NaN。Add/Sub/Mul 传同两组不改变语义，只是多一条对照。
            {"feature_left": "b", "feature_right": "b"},
        ]
    return plan


def _params(cls) -> list[str]:
    """qlib 该类 `__init__` 的形参名（去 self）——工件里 `args` 的键与位置序都以它为准。

    为什么要把形参名写进工件而不是靠字典序：工件是 `sort_keys=True` 落盘的，键序会被
    重排成 `{"N": 5, "feature": "a"}`，「插入序就是位置序」这条约定当场作废（第一版就
    因此把 5 当成序列传给了 Rank）。按名取值之后，键序不再有意义。
    """
    params = list(inspect.signature(cls.__init__).parameters.values())[1:]
    return [p.name for p in params if p.kind in (p.POSITIONAL_OR_KEYWORD, p.POSITIONAL_ONLY)]


def _positional(cls, args: dict) -> list:
    """按 `_params` 的顺序换成 qlib 的操作数：数字原样（qlib 的成对算子允许标量），
    字符串视作 `INPUTS` 里的序列名。"""
    out = []
    for name in _params(cls):
        val = args[name]
        out.append(val if isinstance(val, (int, float)) and not isinstance(val, bool) else _Series(INPUTS[val]))
    return out


def _series_len(args: dict) -> int:
    return len(INPUTS[next(v for v in args.values() if isinstance(v, str))])


def _build_case(cls, args: dict) -> list:
    """走 qlib 真代码路径取一组的输出。"""
    inst = cls(*_positional(cls, args))
    out = inst._load_internal(INSTRUMENT, 0, _series_len(args) - 1)
    return _encode_series(out)


def build_payload() -> dict:
    global _SERIES_BASE
    ops_module = _qlib_ops()  # 先给「不在岛里」一个好读的退出，再谈解释器隔离
    _ensure_island_only()
    _SERIES_BASE = _make_series_cls(ops_module)
    comparison, arithmetic = _symbol_maps(ops_module)
    plan = _reference_plan(comparison, arithmetic)
    operators: dict[str, dict] = {}
    for name in sorted(plan):
        cls = getattr(ops_module, name)
        operators[name] = {
            "class": name,
            "base": cls.__mro__[1].__name__,
            "arity": _arity(cls),
            "params": _params(cls),
            "qlib_source_sha256": hashlib.sha256(
                _source_material(ops_module, cls).encode("utf-8")
            ).hexdigest(),
            "reference": [
                {"args": args, "expected": _build_case(cls, args)}
                for args in plan[name]
            ],
        }
    payload = {
        "version": 1,
        "source": SOURCE_LABEL,
        "provenance": {
            "interpreter": "venv-qlib",
            "python": sys.version.split()[0],
            "pyqlib": pkg_version("pyqlib"),
            "pandas": pkg_version("pandas"),
            "numpy": pkg_version("numpy"),
            "exported_by": Path(__file__).resolve().relative_to(ROOT).as_posix(),
            "generated_at": date.today().isoformat(),
        },
        "encoding": ENCODING,
        # 两张表都是从 qlib 的运算符重载现读出来的（见 _symbol_maps），不是抄写的常量。
        "comparison_symbols": comparison,
        "arithmetic_symbols": arithmetic,
        "inputs": {k: [_encode(v) for v in vals] for k, vals in sorted(INPUTS.items())},
        "windows": list(WINDOWS),
        "operators": operators,
    }
    payload["content_sha256"] = content_sha(payload)
    return payload


def content_sha(payload: dict) -> str:
    """与守卫同式：排除 content_sha256 自身与 provenance.generated_at。"""
    body = {k: v for k, v in payload.items() if k != "content_sha256"}
    prov = dict(body["provenance"])
    prov.pop("generated_at", None)
    body["provenance"] = prov
    canon = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def first_content_diff(live: dict, saved: dict) -> str:
    """比对失败时给第一条具体差异，别让人对着两个哈希自己二分二十几个算子。"""
    for key in ("inputs", "windows", "encoding", "comparison_symbols", "arithmetic_symbols"):
        if live.get(key) != saved.get(key):
            return f"{key} 不一致"
    live_ops, saved_ops = live.get("operators", {}), saved.get("operators", {})
    if set(live_ops) != set(saved_ops):
        return f"算子名单不一致：只在岛 {sorted(set(live_ops) - set(saved_ops))} / 只在工件 {sorted(set(saved_ops) - set(live_ops))}"
    for name in sorted(live_ops):
        if live_ops[name] != saved_ops[name]:
            if live_ops[name]["qlib_source_sha256"] != saved_ops[name]["qlib_source_sha256"]:
                return f"{name} 的 qlib 源码哈希漂了：岛里的实现变了，金标准必须重导"
            live_refs, saved_refs = live_ops[name]["reference"], saved_ops[name]["reference"]
            if len(live_refs) != len(saved_refs):
                return (
                    f"{name} 的用例组数变了：岛 {len(live_refs)} vs 工件 {len(saved_refs)}"
                    "（导出计划被改过，必须重导）"
                )
            for i, (a, b) in enumerate(zip(live_refs, saved_refs, strict=True)):
                if a["expected"] != b["expected"]:
                    if len(a["expected"]) != len(b["expected"]):
                        return (
                            f"{name} 第 {i} 组输出的点数变了："
                            f"{len(a['expected'])} vs {len(b['expected'])}"
                        )
                    first = next(
                        (x for x, y in zip(a["expected"], b["expected"], strict=True) if x != y),
                        None,
                    )
                    return f"{name} 第 {i} 组用例输出变了：args={a['args']}，第一个差异 {first}"
            return f"{name} 结构不同但逐值相同：arity/base 漂了"
    return "正文一致但哈希不同——两处 content_sha 口径分叉了"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true", help="只比对岛内重算结果与已存工件，不写盘")
    ap.add_argument("--artifact", default=None, help="比对该路径下的工件副本（默认 config/alpha158_ops.json）")
    args = ap.parse_args(argv)

    path = Path(args.artifact) if args.artifact else ROOT / ARTIFACT_REL
    payload = build_payload()
    text = dump(payload)
    json.loads(text)  # 自证：严格 JSON，读侧不会遇到裸 NaN

    if args.check:
        if not path.exists():
            sys.stderr.write(f"工件不存在：{path}\n")
            return 1
        saved = json.loads(path.read_text(encoding="utf-8"))
        if saved.get("content_sha256") == payload["content_sha256"]:
            print(f"OK 岛↔工件一致 {path.name} content_sha256={payload['content_sha256'][:16]}… "
                  f"pyqlib={payload['provenance']['pyqlib']} "
                  f"pandas={payload['provenance']['pandas']} numpy={payload['provenance']['numpy']}")
            return 0
        sys.stderr.write(f"MISMATCH {first_content_diff(payload, saved)}\n"
                         f"岛内重算 sha={payload['content_sha256'][:16]}… 工件 sha={saved.get('content_sha256','')[:16]}…\n")
        return 1

    atomic_write(path, text)
    n_cases = sum(len(r["reference"]) for r in payload["operators"].values())
    print(f"WROTE {path.relative_to(ROOT)} 算子 {len(payload['operators'])} 个 / 用例 {n_cases} 组 "
          f"content_sha256={payload['content_sha256'][:16]}…")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
