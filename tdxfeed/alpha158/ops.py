"""Alpha158 的算子层：主 venv 侧实现，语义权威是 `config/alpha158_ops.json`。

`REGISTRY` 收 28 个名字 = 18 个函数算子（kbar 除外，它们由 `eval` 直接用四则算）
+ 6 个比较算子（`>` `>=` `<` `<=` `==` `!=`）+ 4 个四则算子（`+ - * /`）。清单里
15/158 条表达式写了比较符号（`CNTP5 = Mean($close>Ref($close, 1), 5)`），四则则是
每一条价格差的骨架，两者都是求值面的一部分，所以都在金标准里有逐值用例。

为什么不"照定义写一遍"就算数
--------------------------
qlib 0.9.7 的算子有四处与直觉不等价（逐条都在金标准里有对应用例，守卫
`tests/test_alpha158_semantics.py` 拿岛内真 qlib 的输出逐值比对）：

1. **预热段也出值**：`Rolling._load_internal` 一律 `rolling(N, min_periods=1)`。
   pandas 默认 `min_periods=N`，照默认写会让前 N-1 行变 NaN——形状对得上、值全不同，
   训练与 IC 会静默换口径；
2. **`Rank` 是时序百分位**：`rolling(N, min_periods=1).rank(pct=True)`，逐标的算，
   不是横截面排名（横截面要全市场面板，本层拿不到也不该拿）；
3. **`Slope/Rsquare/Resi` 是增量 OLS**：出自 `qlib/data/_libs/rolling.pyx`，
   维护 5~6 个累加量、NaN 跳过但仍占位并计入 `na_count`，且 x 轴用「窗口宽度的常数」
   再在出窗时按 `i_sum` 衰减。它与"对完整窗做一次 polyfit"在预热段**不等价**，
   浮点顺序也不同，所以这里逐句转写那份 .pyx，而不是重推公式；
4. **非有限值不抛异常**：`Log`/`Abs`/`Greater`/`Less` 走 numpy ufunc
   （qlib 模块级 `np.seterr(invalid="ignore")`）⇒ `Log(0) = -Inf`、`Log(负) = NaN`；
   `Rsquare` 与 `Corr` 还会把「滚动 std ≈ 0（`atol=2e-5`）」的位置显式置 NaN。
   成对算子（四则与比较）同为 ufunc ⇒ `Div(x, 0)` 得 ±Inf 而不是 `ZeroDivisionError`，
   `Gt` 一族返回 **bool** 且 NaN 参与比较时得 False（不是 NaN）——后者是 `CNTP*`
   这类「涨跌天数占比」因子的口径来源。

除零语义同样要照 C 来：.pyx 里是 `cdivision=True`，`0/0` 得 NaN、`x/0` 得 ±Inf，
而 Python 的 float 除法直接抛 `ZeroDivisionError` ⇒ 全部走 `_div`。

这一层只认 `pandas.Series`，并且**要求成对算子的两侧 index 相同**：qlib 里两侧是同一
标的同一时间轴加载出来的，天然对齐；本层的调用方（`tdxfeed.alpha158.eval`）如果哪天
传进两个不同 index 的序列，pandas 会按并集对齐并填 NaN——那是会把错值算得"看起来正常"
的一类静默失败，所以这里直接报错。

生产侧不许 `import qlib`（`venv-qlib/` 是环境孤岛，见 AGENTS.md 与
`tests/test_qlib_env_island.py`）；本模块只依赖 numpy / pandas。
"""
from __future__ import annotations

import math
from collections import deque
from collections.abc import Callable
from typing import Any

import numpy as np
import pandas as pd

# qlib 的 Rolling 一律用这两个常数，抄在这里是因为它们就是口径本身。
_MIN_PERIODS = 1
_ZERO_STD_ATOL = 2e-05

SeriesLike = pd.Series | int | float


def _div(num: float, den: float) -> float:
    """C 语义的除法：0/0 → NaN、x/0 → ±Inf，绝不抛异常。"""
    with np.errstate(divide="ignore", invalid="ignore"):
        return float(np.float64(num) / np.float64(den))


def _as_series(result: Any, index: pd.Index) -> pd.Series:
    """numpy ufunc 的返回可能是 ndarray，统一包回带原 index 的 Series。"""
    if isinstance(result, pd.Series):
        return result
    return pd.Series(result, index=index, dtype="float64")


def _rolling(series: pd.Series, n: int) -> Any:
    return series.rolling(n, min_periods=_MIN_PERIODS)


def _check_aligned(left: pd.Series, right: pd.Series) -> None:
    if not left.index.equals(right.index):
        raise ValueError(
            "成对算子要求两侧 index 相同：qlib 里两侧是同标的同时间轴加载的，"
            f"而这里左 {len(left)} 行 / 右 {len(right)} 行且 index 不等，"
            "交给 pandas 对齐会静默产出 NaN，等于把口径错误藏进缺失值"
        )


def _pair(left: SeriesLike, right: SeriesLike) -> tuple[pd.Series, pd.Series]:
    """把标量摊成与另一侧同 index 的 Series，并核对两侧对齐。"""
    if isinstance(left, pd.Series) and isinstance(right, pd.Series):
        _check_aligned(left, right)
        return left, right
    if isinstance(left, pd.Series):
        return left, pd.Series(float(right), index=left.index, dtype="float64")  # type: ignore[arg-type]
    assert isinstance(right, pd.Series), "成对算子至少要有一侧是 Series"
    return pd.Series(float(left), index=right.index, dtype="float64"), right


# ------------------------------------------------------------------ 逐元素类 #
def absolute(series: pd.Series) -> pd.Series:
    """qlib `Abs` = `np.abs`。"""
    return _as_series(np.abs(series), series.index)


def logarithm(series: pd.Series) -> pd.Series:
    """qlib `Log` = `np.log`：0 → -Inf、负数 → NaN（不抛）。"""
    with np.errstate(divide="ignore", invalid="ignore"):
        return _as_series(np.log(series), series.index)


def greater(left: SeriesLike, right: SeriesLike) -> pd.Series:
    """qlib `Greater` = `np.maximum`（取大，不是「是否更大」）。"""
    l, r = _pair(left, right)
    return _as_series(np.maximum(l, r), l.index)


def lesser(left: SeriesLike, right: SeriesLike) -> pd.Series:
    """qlib `Less` = `np.minimum`（取小）。"""
    l, r = _pair(left, right)
    return _as_series(np.minimum(l, r), l.index)


# ------------------------------------------------- 成对 ufunc：四则与比较 #
def _np_pair(ufunc: Any, left: SeriesLike, right: SeriesLike) -> pd.Series:
    """qlib 的 `NpPairOperator._load_internal` 就是 `getattr(np, func)(左, 右)`。

    两侧先经 `_pair` 摊齐并硬校验 index；`errstate` 只关告警不改数值——岛里的
    `np.seterr` 是 `{divide: warn, invalid: ignore}`，所以 `Div(x, 0)` 在岛内会打一条
    RuntimeWarning 但产出 ±Inf，本层要的是同一个数，不是同一条噪声。

    dtype 跟着 ufunc 走：`Gt` 一族返回 **bool** Series（与岛内一致），不是 0/1 浮点。
    金标准工件把 bool 编码成 `0.0/1.0`（JSON 没有布尔序列的逐值形态），比对时按数值比。
    """
    l, r = _pair(left, right)
    with np.errstate(divide="ignore", invalid="ignore"):
        return _as_series(ufunc(l, r), l.index)


def add(left: SeriesLike, right: SeriesLike) -> pd.Series:
    return _np_pair(np.add, left, right)


def subtract(left: SeriesLike, right: SeriesLike) -> pd.Series:
    return _np_pair(np.subtract, left, right)


def multiply(left: SeriesLike, right: SeriesLike) -> pd.Series:
    return _np_pair(np.multiply, left, right)


def divide(left: SeriesLike, right: SeriesLike) -> pd.Series:
    """`np.divide` ⇒ `0/0 = NaN`、`x/0 = ±Inf`，不抛（Python 的 `/` 对标量 0 会抛）。"""
    return _np_pair(np.divide, left, right)


def gt(left: SeriesLike, right: SeriesLike) -> pd.Series:
    """qlib `Gt` = `np.greater`：表达式里写 `$close>Ref($close,1)` 走的是它。

    与 `Greater`（取大）**同名不同义**，别按名字猜。NaN 参与比较两侧都得 False——
    这是"涨跌停判定"这类计数式因子（`CNTP`）的口径来源，不是漏值。
    """
    return _np_pair(np.greater, left, right)


def ge(left: SeriesLike, right: SeriesLike) -> pd.Series:
    return _np_pair(np.greater_equal, left, right)


def lt(left: SeriesLike, right: SeriesLike) -> pd.Series:
    return _np_pair(np.less, left, right)


def le(left: SeriesLike, right: SeriesLike) -> pd.Series:
    return _np_pair(np.less_equal, left, right)


def eq(left: SeriesLike, right: SeriesLike) -> pd.Series:
    return _np_pair(np.equal, left, right)


def ne(left: SeriesLike, right: SeriesLike) -> pd.Series:
    return _np_pair(np.not_equal, left, right)


# ------------------------------------------------------------------ 滚动类 #
def ref(series: pd.Series, n: int) -> pd.Series:
    """qlib `Ref`：n=0 取首值铺满，否则 `shift(n)`（不是滚动）。"""
    if series.empty:
        return series
    if n == 0:
        return pd.Series(series.iloc[0], index=series.index, dtype="float64")
    return series.shift(n)


def mean(series: pd.Series, n: int) -> pd.Series:
    return _rolling(series, n).mean()


def sum_(series: pd.Series, n: int) -> pd.Series:
    return _rolling(series, n).sum()


def std(series: pd.Series, n: int) -> pd.Series:
    """pandas 的滚动标准差是 ddof=1（样本标准差），qlib 直接用默认值。"""
    return _rolling(series, n).std()


def maximum(series: pd.Series, n: int) -> pd.Series:
    return _rolling(series, n).max()


def minimum(series: pd.Series, n: int) -> pd.Series:
    return _rolling(series, n).min()


def quantile(series: pd.Series, n: int, q: float) -> pd.Series:
    return _rolling(series, n).quantile(q)


def rank(series: pd.Series, n: int) -> pd.Series:
    """时序百分位：`rolling(N, min_periods=1).rank(pct=True)`。

    qlib 在 pandas < 1.4 时回退到 `percentileofscore`，两者对并列值口径不同；
    金标准工件的 provenance 记了导出时用的 pandas 版本，守卫要求 ≥ 1.4。
    """
    return _rolling(series, n).rank(pct=True)


def idx_max(series: pd.Series, n: int) -> pd.Series:
    """窗口内最大值的位置，**1 基**；NaN 不跳过（照 qlib 的 `argmax()+1`）。"""
    return _rolling(series, n).apply(lambda x: x.argmax() + 1, raw=True)


def idx_min(series: pd.Series, n: int) -> pd.Series:
    return _rolling(series, n).apply(lambda x: x.argmin() + 1, raw=True)


# ----------------------------------------------------- 增量 OLS（.pyx 的转写） #
class _RollingOls:
    """`qlib/data/_libs/rolling.pyx` 里 Slope/Rsquare/Resi 共用的增量结构。

    逐句对应，不"化简"：`x_sum += self.window` 这个看起来奇怪的写法是 qlib 的滚动技巧
    （新值按窗口宽度计入、每步用 `i_sum` 把出窗的量减掉），化简成 `x = 1..N` 会让预热段
    的值与 qlib 不同——那正是金标准要抓的差别。
    """

    def __init__(self, window: int) -> None:
        self.window = window
        self.na_count = window
        self.barv: deque[float] = deque([math.nan] * window)
        self.i_sum = 0.0
        self.x_sum = 0.0
        self.x2_sum = 0.0
        self.y_sum = 0.0
        self.xy_sum = 0.0
        self.y2_sum = 0.0

    def update(self, val: float) -> int:
        """推进一步，返回窗口内**非 NaN 的个数** N。"""
        self.barv.append(val)
        self.xy_sum = self.xy_sum - self.y_sum
        self.x2_sum = self.x2_sum + self.i_sum - 2 * self.x_sum
        self.x_sum = self.x_sum - self.i_sum
        front = self.barv.popleft()
        if not math.isnan(front):
            self.i_sum -= 1
            self.y_sum -= front
            self.y2_sum -= front * front
        else:
            self.na_count -= 1
        if math.isnan(val):
            self.na_count += 1
        else:
            self.i_sum += 1
            self.x_sum += self.window
            self.x2_sum += self.window * self.window
            self.y_sum += val
            self.y2_sum += val * val
            self.xy_sum += self.window * val
        return self.window - self.na_count

    def _sx(self, n: int) -> float:
        return n * self.x2_sum - self.x_sum * self.x_sum

    def slope(self, n: int) -> float:
        return _div(n * self.xy_sum - self.x_sum * self.y_sum, self._sx(n))

    def rsquare(self, n: int) -> float:
        den = self._sx(n) * (n * self.y2_sum - self.y_sum * self.y_sum)
        with np.errstate(invalid="ignore"):
            rvalue = _div(n * self.xy_sum - self.x_sum * self.y_sum, float(np.sqrt(den)))
        return rvalue * rvalue

    def resi(self, n: int, val: float) -> float:
        slope = self.slope(n)
        x_mean = _div(self.x_sum, n)
        y_mean = _div(self.y_sum, n)
        interp = y_mean - slope * x_mean
        return val - (slope * self.window + interp)


def _ols_series(series: pd.Series, n: int, step: Callable[[_RollingOls, int, float], float]) -> pd.Series:
    ols = _RollingOls(n)
    out = [step(ols, ols.update(float(v)), float(v)) for v in series.to_numpy(dtype="float64")]
    return pd.Series(out, index=series.index, dtype="float64")


def slope(series: pd.Series, n: int) -> pd.Series:
    return _ols_series(series, n, lambda ols, count, val: ols.slope(count))


def resi(series: pd.Series, n: int) -> pd.Series:
    return _ols_series(series, n, lambda ols, count, val: ols.resi(count, val))


def rsquare(series: pd.Series, n: int) -> pd.Series:
    out = _ols_series(series, n, lambda ols, count, val: ols.rsquare(count))
    # qlib 在 ops.Rsquare._load_internal 里补的这一步：滚动 std≈0 的窗口 R² 没有意义
    flat = np.isclose(_rolling(series, n).std(), 0, atol=_ZERO_STD_ATOL)
    out.loc[flat] = np.nan
    return out


def corr(left: pd.Series, right: pd.Series, n: int) -> pd.Series:
    _check_aligned(left, right)
    res = _rolling(left, n).corr(right)
    flat = np.isclose(_rolling(left, n).std(), 0, atol=_ZERO_STD_ATOL) | np.isclose(
        _rolling(right, n).std(), 0, atol=_ZERO_STD_ATOL
    )
    res.loc[flat] = np.nan
    return res


REGISTRY: dict[str, Callable[..., pd.Series]] = {
    "Abs": absolute,
    "Add": add,
    "Corr": corr,
    "Div": divide,
    "Eq": eq,
    "Ge": ge,
    "Gt": gt,
    "Greater": greater,
    "IdxMax": idx_max,
    "IdxMin": idx_min,
    "Le": le,
    "Less": lesser,
    "Log": logarithm,
    "Lt": lt,
    "Max": maximum,
    "Mean": mean,
    "Min": minimum,
    "Mul": multiply,
    "Ne": ne,
    "Quantile": quantile,
    "Rank": rank,
    "Ref": ref,
    "Resi": resi,
    "Rsquare": rsquare,
    "Slope": slope,
    "Std": std,
    "Sub": subtract,
    "Sum": sum_,
}
