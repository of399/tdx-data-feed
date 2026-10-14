"""quant.multifactor：多因子选股升级方案核心库（v1）。

以扩展代重建：复用 plan_e_backtest 的 compute_metrics / _bootstrap_ci 与
factor_engine 的日 K 加载口径，新增截面因子库、Barra 风险模型、ERC 组合与
全成本模型，由 plan_e_backtest.py --mf 统一调度。

设计文档：docs/MULTIFACTOR.md
"""
from .engine import run_mf_backtest
from .factors import FACTOR_METAS, FactorMeta, MFConfig

MF_VERSION = "v1"
__all__ = ["FACTOR_METAS", "MF_VERSION", "FactorMeta", "MFConfig", "run_mf_backtest"]
