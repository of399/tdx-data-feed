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
from tdxfeed.features_5d import (  # noqa: E402
    compute_window_features,
    compute_window_features_extended,
    get_limit,
)

__all__ = [
    "compute_window_features",
    "compute_window_features_extended",
    "get_limit",
    "parse_date",
    "parse_date_series",
    "to_date_str",
]
