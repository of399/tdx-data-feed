"""Alpha158 求值器：把 ① 的 158 条表达式在**主 venv** 里算成因子表。

这一层是「只借 qlib 的表达式引擎、数值计算回主 venv」的落点：读 `config/alpha158_expressions.json`
（表达式字符串）+ `config/alpha158_ops.json` 的语义实现（`tdxfeed.alpha158.ops`），
产出一个按工件顺序命名的 DataFrame。

守卫锁的是四件事，每件都对应一类会静默出错的改法：

1. **封闭解释器，不用 eval**。表达式来自岛内导出的工件，但「工件里的字符串能执行任意代码」
   这条通道一次都不该存在：求值器只认 `BinOp(+ - * /)` / `UnaryOp(- +)` / 数字 / `$列` /
   `REGISTRY` 里的函数名，别的节点（Attribute、Subscript、keyword、lambda……）一律报错。
   本模块的 AST 里也不许出现 `eval` / `exec` / `compile`——这条用测试钉住，不是靠措辞。
2. **名单闭合**：列名集合取自工件的 `columns`，函数名集合取自 `REGISTRY`。工件清单里冒出
   一个 REGISTRY 没有的算子（qlib 改了默认配置、或有人手改工件）必须**报错**，
   而不是悄悄把那一条算成 NaN——158 维少算一维在下游是看不见的。
3. **输出列名与顺序 = 工件**：下游因子宽表按名字取列，顺序漂了不会报错，只会把标签
   对错列。所以这里断言的是 `list(frame.columns) == [f["name"] for f in payload["features"]]`。
4. **口径提示不能丢**：`vwap` 那一条 caveat（2026-09-16 起 amount 是代理值 ⇒ 该段
   vwap 恒等于 close）必须还在，消费方才不会被一个形状正确的伪信号骗过去。

数据依赖：本模块只用合成 DataFrame（`NEEDS_DATA = False`），CI 里真跑；
真行情上的装配与逐列核对是下一步（读侧 `tdxfeed.adjusted.frame` + hfq 口径）的活，
不进本文件，免得 CI 拿不到 data/ 时这条守卫静默变成空跑。
"""
from __future__ import annotations

import ast
import json
import math
from pathlib import Path

import pandas as pd
import pytest

NEEDS_DATA = False

ROOT = Path(__file__).resolve().parents[1]
ART = ROOT / "config" / "alpha158_expressions.json"

COLS = ("open", "high", "low", "close", "volume", "vwap")


def payload() -> dict:
    assert ART.exists(), f"缺 {ART.relative_to(ROOT)}"
    return json.loads(ART.read_text(encoding="utf-8"))


def eval_module():
    try:
        from tdxfeed.alpha158 import eval as alpha_eval
    except ImportError as exc:
        pytest.fail(f"tdxfeed.alpha158.eval 不可导入：{exc}")
    return alpha_eval


def synth_frame(n: int = 60, offset: int = 0) -> pd.DataFrame:
    """确定性合成日线：不用随机数，任何环境重跑得到同一张表。

    造得"不像真行情"是有意的——各列互不相等、有升有降、都非零，
    这样算错的列（比如把 high 当 low 用）一定会露出来。
    """
    rows = {}
    for i in range(n):
        t = i + offset
        base = 10.0 + 3.0 * math.sin(t / 5.0) + 0.2 * (t % 7)
        rows.setdefault("close", []).append(base)
        rows.setdefault("open", []).append(base + 0.3 + 0.1 * (t % 3))
        rows.setdefault("high", []).append(base + 0.9 + 0.05 * (t % 4))
        rows.setdefault("low", []).append(base - 0.7 - 0.05 * (t % 5))
        rows.setdefault("volume", []).append(float(1000 + 137 * (t % 11)))
        rows.setdefault("vwap", []).append(base + 0.05 * (t % 6))
    frame = pd.DataFrame(rows, columns=list(COLS))
    frame.index = pd.Index([f"2026-01-{i:02d}" if i < 31 else f"d{i}" for i in range(n)])
    return frame


def _names(expr: str) -> set[str]:
    """表达式里出现的 `$列`。"""
    import re

    return set(re.findall(r"\$([A-Za-z_][A-Za-z0-9_]*)", expr))


# ------------------------------------------------------------------ 基本形状 #
def test_output_columns_are_the_artifact_names_in_order():
    ev = eval_module()
    d = payload()
    out = ev.evaluate_frame(synth_frame())
    want = [f["name"] for f in d["features"]]
    assert list(out.columns) == want, (
        f"列名或顺序与工件不一致：前 10 实={list(out.columns)[:10]} 期={want[:10]}；"
        "下游宽表按名字取列，漂了不会报错只会对错列"
    )
    assert len(out.columns) == 158


def test_all_158_features_finite_where_they_should_be():
    """158 条全出得来，且**不是**靠一片 NaN 蒙过形状检查。"""
    ev = eval_module()
    out = ev.evaluate_frame(synth_frame(n=120))
    d = payload()
    for feat in d["features"]:
        col = out[feat["name"]]
        assert col.notna().sum() > 0, f"{feat['name']} 整列为空：{feat['expr']}"


def test_empty_frame_returns_empty_but_keeps_the_contract():
    ev = eval_module()
    empty = pd.DataFrame(columns=list(COLS))
    out = ev.evaluate_frame(empty)
    assert list(out.columns) == [f["name"] for f in payload()["features"]]
    assert len(out) == 0


def test_evaluation_is_deterministic():
    ev = eval_module()
    a = ev.evaluate_frame(synth_frame(n=80))
    b = ev.evaluate_frame(synth_frame(n=80))
    pd.testing.assert_frame_equal(a, b)


# ------------------------------------------------------------------ 逐条口径 #
def test_kbar_expressions_match_hand_computation():
    """无窗算子（kbar 那 13 条）必须等于手算——它们不经任何滚动，最容易看出接线错。"""
    ev = eval_module()
    f = synth_frame(n=10)
    out = ev.evaluate_frame(f)
    close, open_, high, low = (f["close"], f["open"], f["high"], f["low"])
    eps = 1e-12
    pd.testing.assert_series_equal(out["KMID"], (close - open_) / open_, check_names=False)
    pd.testing.assert_series_equal(out["KLEN"], (high - low) / open_, check_names=False)
    pd.testing.assert_series_equal(out["KMID2"], (close - open_) / (high - low + eps), check_names=False)
    # KUP = (high - Greater(open, close)) / open，Greater 取的是两者的较大值
    pd.testing.assert_series_equal(out["KUP"], (high - pd.concat([open_, close], axis=1).max(axis=1)) / open_, check_names=False)
    pd.testing.assert_series_equal(out["KLOW"], (pd.concat([open_, close], axis=1).min(axis=1) - low) / open_, check_names=False)


def test_rolling_expressions_use_the_warmed_up_head_row():
    """`Mean($close,5)/$close` 在第 0 行必须 = 1.0：min_periods=1 的头行就是它自己。

    这一条直接钉住「形状对、预热段值不同」那类口径错误——照 pandas 默认写会得到 NaN。
    """
    ev = eval_module()
    out = ev.evaluate_frame(synth_frame(n=30))
    ma5 = out["MA5"]
    assert math.isclose(float(ma5.iloc[0]), 1.0, rel_tol=1e-12), (
        f"MA5 头行 = {ma5.iloc[0]}，应为 1.0（qlib 的 rolling 是 min_periods=1）"
    )
    assert ma5.notna().all(), "MA5 出现 NaN：预热段被当成缺失了"


def test_internal_cache_is_per_frame_not_module_global():
    """求值器为省掉重复子式可以缓存，但缓存必须按本次输入分键。

    挂在模块级的缓存会让第二次调用拿到第一次的值——形状、列名、NaN 分布全对，
    只有数值是别的股票的，这正是宽表里最难查的一类错。
    """
    ev = eval_module()
    a = ev.evaluate_frame(synth_frame(n=40, offset=0))
    b = ev.evaluate_frame(synth_frame(n=40, offset=7))
    assert not a["MA20"].equals(b["MA20"]), "两次不同输入拿到同一列：缓存没按 frame 分键"


def test_index_is_carried_through_untouched():
    ev = eval_module()
    f = synth_frame(n=25)
    out = ev.evaluate_frame(f)
    assert list(out.index) == list(f.index), "输出 index 被改写，下游按日期对齐会串位"


# ------------------------------------------------------------------ 比较与四则 #
def test_comparison_expression_is_a_ratio_not_a_price():
    """`CNTP5 = Mean($close>Ref($close,1),5)` 落在 [0,1]，且逐行等于手算的上行天数占比。

    这条专门钉「`>` 映射到哪个算子」：把它接到 `Greater`（`np.maximum`，取大）上，
    得到的仍是**一列有值的浮点**、形状与 NaN 分布都不变，只有量纲从 0~1 变成十几块——
    下游 IC/模型不会报错，只会静默换口径。所以断言的是值域和逐行等值两件事。
    """
    ev = eval_module()
    f = synth_frame(n=60)
    out = ev.evaluate_frame(f)
    col = out["CNTP5"]
    assert col.notna().all(), "CNTP5 有空洞：比较结果没被当成 0/1 进了滚动均值"
    assert float(col.min()) >= 0.0 and float(col.max()) <= 1.0, (
        f"CNTP5 值域 {col.min()}~{col.max()}：涨跌占比必须在 [0,1]，"
        "超出就是 `>` 被接成了取大（Greater）而不是比大小（Gt）"
    )
    up = (f["close"] > f["close"].shift(1)).astype(float)
    want = up.rolling(5, min_periods=1).mean()
    pd.testing.assert_series_equal(col, want, check_names=False, atol=1e-12)


def test_cntp_and_cntn_are_complementary():
    """`CNTP*` 与 `CNTN*` 用的是 `>` 和 `<`；含 NaN 的 Ref 首行两侧都算 False。

    两侧加起来在头行是 0（`close > NaN` 与 `close < NaN` 同为 False）。这条把「NaN 参与
    比较得 False 而不是 NaN」钉在求值面，而不是留给实现者猜。注意 `min_periods=1` 下
    「非涨即跌 = 1」要等整窗都越过第 0 行才成立（第 N-1 行起），预热段里那条恒等式本身
    不成立——所以断言按窗口分三段写，别一把梭成 `iloc[2:]`。
    """
    ev = eval_module()
    out = ev.evaluate_frame(synth_frame(n=60))
    head = out["CNTP5"].iloc[0] + out["CNTN5"].iloc[0]
    assert math.isclose(float(head), 0.0, abs_tol=1e-12), (
        f"头行 CNTP5+CNTN5 = {head}，应为 0：Ref($close,1) 首行是 NaN，两侧比较都得 False"
    )
    warm = out["CNTP5"].iloc[1:5] + out["CNTN5"].iloc[1:5]
    # 第 i 行的窗是 rows 0..i（min_periods=1 下窗还没长满），其中 row 0 两侧皆 False
    # ⇒ 占比 = i/(i+1)。这条把「预热段的窗长是 i+1 而不是 N」一起钉住。
    for offset, value in enumerate(warm, start=1):
        assert math.isclose(float(value), offset / (offset + 1), abs_tol=1e-12), (
            f"CNTP5+CNTN5 第 {offset} 行 = {value}，应为 {offset / (offset + 1)}"
            "（窗里还压着第 0 行那个两侧皆 False 的 NaN 位，且窗长只有 "
            f"{offset + 1}）"
        )
    steady = out["CNTP5"].iloc[5:] + out["CNTN5"].iloc[5:]
    assert (steady - 1.0).abs().max() < 1e-12, (
        "整窗越过第 0 行之后 CNTP5+CNTN5 应恒为 1（每日非涨即跌）；"
        "不等说明某一侧的 NaN 政策漂了"
    )


def test_division_by_zero_follows_np_divide_not_python_operator():
    """`/` 走的是 ② 的 `Div`（= `np.divide`）：除零得 ±Inf / NaN，**不抛**。

    这条专门盯「四则直接用 Python 运算符」的写法：`float(x)/0.0` 抛 `ZeroDivisionError`，
    而 qlib 的成对算子是 ufunc。真行情里 `Ref($volume,1)` 出 0 是会发生的事（停牌/无成交），
    所以这不是理论分支——照 Python 写会让整只票的 158 维装配直接中断。
    """
    ev = eval_module()
    d = payload()
    bad = json.loads(json.dumps(d))
    bad["features"] = [{"name": "D", "expr": "$close/Ref($close, 1)", "ops": ["Ref"]}]
    f = synth_frame(n=6)
    f.loc[f.index[0], "close"] = 0.0  # 让第 1 行的分母（上一日 close）为 0
    out = ev.evaluate_frame(f, payload=bad)
    assert math.isinf(float(out["D"].iloc[1])), (
        f"分母为 0 处得到 {out['D'].iloc[1]}，应为 ±Inf（np.divide 的口径）"
    )
    assert math.isnan(float(out["D"].iloc[0])), "Ref 的头值没有前一日，应为 NaN"
    assert float(out["D"].iloc[1]) > 0 or f["close"].iloc[1] < 0, "符号应随被除数"


def test_chained_comparison_is_rejected():
    """qlib 的表达式文法不产 `a<b<c`，这里也不猜它该等于哪个算子。"""
    ev = eval_module()
    d = payload()
    bad = json.loads(json.dumps(d))
    bad["features"] = [{"name": "CH", "expr": "$open < $close < $high", "ops": []}]
    with pytest.raises(ev.Alpha158Error) as exc:
        ev.evaluate_frame(synth_frame(n=10), payload=bad)
    assert "链式" in str(exc.value), "报错要说明是链式比较被拒，否则排查时看不出方向"


def test_symbol_tables_come_from_the_ops_artifact_not_a_local_copy():
    """求值面没有第二份「符号 → 算子」表：② 的符号表被拿掉一条，这里必须报错而不是兜住。

    否则本模块就偷偷获得了一份本地映射，qlib 改了重载时只有工件变、代码不变，
    158 维会静默按旧口径算。
    """
    ev = eval_module()
    gold = json.loads((ROOT / "config" / "alpha158_ops.json").read_text(encoding="utf-8"))
    for key, expr in (("comparison_symbols", "$close>Ref($close, 1)"),
                      ("arithmetic_symbols", "$close-$open")):
        stripped = json.loads(json.dumps(gold))
        stripped[key].pop(">" if key == "comparison_symbols" else "-")
        d = payload()
        bad = json.loads(json.dumps(d))
        bad["features"] = [{"name": "S", "expr": expr, "ops": []}]
        with pytest.raises(ev.Alpha158Error) as exc:
            ev.evaluate_frame(synth_frame(n=10), payload=bad, ops_payload=stripped)
        msg = str(exc.value)
        assert "export_alpha158_ops" in msg, (
            f"拿掉 ② 的 {key} 后仍算出来了或报了别的错：说明求值器带了本地兜底表"
        )


def test_surface_reports_unimplemented_symbol_target():
    """符号表指向一个注册表里没有的类名时，要说清是工件指向的还是实现缺的。"""
    ev = eval_module()
    surface = ev.Surface(columns=("close",), symbol_ops={"-": "NoSuchOp"})
    with pytest.raises(ev.Alpha158Error) as exc:
        surface.op_for("-", "四则", "$close-$open")
    assert "NoSuchOp" in str(exc.value)


# ------------------------------------------------------------------ 封闭与报错 #
def test_unknown_function_is_an_error_not_a_nan_column():
    ev = eval_module()
    d = payload()
    bad = json.loads(json.dumps(d))
    bad["features"] = [{"name": "X", "expr": "Frobnicate($close, 5)", "ops": ["Frobnicate"]}]
    with pytest.raises(ev.Alpha158Error) as exc:
        ev.evaluate_frame(synth_frame(n=10), payload=bad)
    assert "Frobnicate" in str(exc.value), "报错里必须点出未知算子名，否则排查等于重新二分"


def test_unknown_column_is_an_error():
    ev = eval_module()
    d = payload()
    bad = json.loads(json.dumps(d))
    bad["features"] = [{"name": "Y", "expr": "$turnover/$close", "ops": []}]
    with pytest.raises(ev.Alpha158Error) as exc:
        ev.evaluate_frame(synth_frame(n=10), payload=bad)
    msg = str(exc.value)
    assert "turnover" in msg, "报错要指出是哪一列不在工件的 columns 里"


def test_missing_vwap_column_says_why_it_matters():
    """`vwap` 不在日线表里，只能由 amount/volume 推——缺它时必须指名 KI-DATA-001 那段口径。"""
    ev = eval_module()
    f = synth_frame(n=10).drop(columns=["vwap"])
    with pytest.raises(ev.Alpha158Error) as exc:
        ev.evaluate_frame(f)
    assert "vwap" in str(exc.value)


def test_rejects_syntax_the_closed_interpreter_does_not_cover():
    """求值面只有四则 + 数字 + $列 + 已知算子。别的节点一律拒绝，别悄悄执行。"""
    ev = eval_module()
    d = payload()
    for expr in (
        "__import__('os').system('echo hi')",      # Attribute / Call 到未知名字
        "($close).to_numpy()",                     # Attribute
        "$close[0]",                               # Subscript
        "[x for x in $close]",                     # ListComp
        "Mean($close, 5, strict=True)",            # keyword 参数
        "lambda x: x",                              # lambda
    ):
        bad = json.loads(json.dumps(d))
        bad["features"] = [{"name": "Z", "expr": expr, "ops": []}]
        with pytest.raises(ev.Alpha158Error):
            ev.evaluate_frame(synth_frame(n=6), payload=bad)


def test_the_module_never_uses_eval_exec_or_compile():
    """封闭解释器的定义性判据：源文件 AST 里不得出现 eval/exec/compile 调用。

    表达式字符串来自岛内工件，一旦允许 eval，就等于「谁改了工件谁就能在跑因子的机器上执行代码」。
    """
    src = (ROOT / "tdxfeed" / "alpha158" / "eval.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    bad = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"eval", "exec", "compile"}
    ]
    assert not bad, f"求值器用了 eval/exec/compile（行 {bad}）"


def test_the_evaluator_does_not_import_qlib():
    """孤岛原则的直接版：这一层 import qlib 就是边界破了（岛守卫也查，这里就近再钉一次）。"""
    src = (ROOT / "tdxfeed" / "alpha158" / "eval.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    assert "qlib" not in imported


# ------------------------------------------------------------------ 与工件闭合 #
def test_registry_covers_every_function_in_the_manifest():
    d = payload()
    from tdxfeed.alpha158 import ops

    missing: dict[str, list[str]] = {}
    for feat in d["features"]:
        for op in feat["ops"]:
            if op not in ops.REGISTRY:
                missing.setdefault(op, []).append(feat["name"])
    assert not missing, f"清单用了注册表里没有的算子 {missing}：必须报错而不是跳过"


def test_artifact_columns_are_all_accepted():
    d = payload()
    assert set(d["columns"]) == set(COLS), (
        f"工件的列集合变了：{sorted(set(d['columns']) ^ set(COLS))}"
        "——求值器的合成输入与本守卫要一起改，先确认工件是对的"
    )


def test_caveats_survive_and_name_the_vwap_trap():
    ev = eval_module()
    text = "\n".join(ev.caveats())
    assert "vwap" in text and "KI-DATA-001" in text, (
        "口径提示丢了：2026-09-16 起 amount 是代理值，那段里 vwap 恒等于 close，"
        "消费 158 维的人必须被告知这一点"
    )


def test_feature_names_helper_matches_artifact():
    ev = eval_module()
    assert ev.feature_names() == [f["name"] for f in payload()["features"]]


def test_expression_for_helper_round_trips():
    ev = eval_module()
    d = payload()
    first = d["features"][0]
    assert ev.expression_for(first["name"]) == first["expr"]
    with pytest.raises(ev.Alpha158Error):
        ev.expression_for("NOT_A_FEATURE")
