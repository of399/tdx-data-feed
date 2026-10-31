"""scripts 公共导入桥。

确保仓库根目录在 sys.path，使脚本可用 ``from tdxfeed...`` 导入包内模块，
并再导出常用工具（日期解析 + 18 维特征单一数据源）。

用法（在 scripts/ 下运行的脚本中）：
    from _common import parse_date, parse_date_series, to_date_str
    from _common import compute_window_features, get_limit
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from tdxfeed.date_utils import parse_date, parse_date_series, to_date_str  # noqa: E402
from tdxfeed.execution.limits import normalize_code as _normalize_code  # noqa: E402
from tdxfeed.features_5d import (  # noqa: E402
    compute_window_features,
    compute_window_features_extended,
    get_limit,
    is_limit_up_ret,
    is_open_limit_up,
    is_yizi,
)
from tdxfeed.paths import (  # noqa: E402
    DATA_ROOT,
    adj_factor_dir,
    adj_factor_files,
    daily_dir,
    daily_files,
    daily_path,
    derived_dir,
    hfq_factor_dir,
    minute_dir,
    model_dir,
    model_path,
    parquet_root,
    read_path,
    universe_path,
    xdxr_path,
)


def norm_code(c) -> str:
    """股票代码规范化: 小写 + 去 sh/sz/bj 前缀 + 6 位零填充（委托 limits）。"""
    return _normalize_code(c)


def is_bj(c) -> bool:
    """北交所判定: 920 开头(新) 或 8 开头(老三板)。统一 3 处重复实现。"""
    c = norm_code(c)
    return c.startswith("920") or (c and c[0] == "8")


__all__ = [
    "DATA_ROOT",
    "adj_factor_dir",
    "adj_factor_files",
    "compute_window_features",
    "compute_window_features_extended",
    "daily_dir",
    "daily_files",
    "daily_path",
    "derived_dir",
    "get_limit",
    "hfq_factor_dir",
    "is_bj",
    "is_limit_up_ret",
    "is_open_limit_up",
    "is_yizi",
    "minute_dir",
    "model_dir",
    "model_path",
    "norm_code",
    "parquet_root",
    "parse_date",
    "parse_date_series",
    "read_path",
    "to_date_str",
    "universe_path",
    "xdxr_path",
]
