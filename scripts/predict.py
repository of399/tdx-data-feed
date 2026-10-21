#!/usr/bin/env python3
"""
方案 D 预测脚本：每日全市场预测翻倍潜力股
========================================

【背景】
  方案 D 用 LightGBM 在"翻倍前 5 日"窗口识别信号。
  本脚本用于生产化：每日拉一遍全市场 → 动态特征 → 预测 → Top 排序。
  默认模型 daban（29 维二分类，次日收盘涨停，small 模式 Top 5）；
  full 模式用 vpnl_huber_stage1（21 维回归，Top 20）；
  回滚到旧模型用 --model vpnl_huber / prod。

【输入数据】
  - data/parquet/daily/{sh|sz|bj}{code}.parquet     日 K（含 open/high/low/close/volume/amount/turnover）
  - data/adj_factor/{code}.parquet                 复权因子
  - data/all_a_stocks.json                         5565 只全 A 清单
  - data/double_up/d_signal_5d_model_daban.txt     默认 LightGBM 模型

【特征维度】
  由 model.feature_name() 动态决定：daban 29 维、vpnl_huber_stage1 21 维、prod 18 维。
  下方为脚本内 FEATURE_COLS 的基础 18 维，不是所有模型的最终列数。

| # | 特征 | 类型 | 含义 | 计算 |
|---|------|------|------|------|
| 1 | prev_close | float | T-1 收盘价 | df['close'].iloc[-1] |
| 2 | ret_5d | float | 5 日累计涨幅 | close[-1]/close[-6]-1 |
| 3 | ret_3d | float | 3 日累计涨幅 | close[-1]/close[-4]-1 |
| 4 | max_5d_pct | float | 5 日最大单日涨幅 | max(pct_change[-5:]) |
| 5 | limit_up_5d | int | 5 日涨停次数 | sum(close/prev_close >= 1+limit) |
| 6 | limit_up_t1 | int | T-1 是否涨停 | close[-1]/close[-2] >= 1+limit |
| 7 | pct_t1 | float | T-1 当日涨幅 | close[-1]/close[-2]-1 |
| 8 | avg_amount_5d | float | 5 日均成交额 | mean(amount[-5:]) |
| 9 | vol_ratio_t1 | float | T-1 量比（末日/5日均量） | volume[-1]/mean(volume[-5:]) |
| 10 | volatility_5d | float | 5 日波动率 | std(pct_change[-5:]) |
| 11 | close_pos_5d | float | T-1 收盘价在 5 日 K 线位置 | (close-min)/(max-min) |
| 12 | max_drawdown_5d | float | 5 日最大跌幅 | min(close/max_so_far - 1) |
| 13 | turnover_proxy | float | T-1 换手代理 | volume[-1]/shares |
| 14 | is_st | 0/1 | 是否 ST | name 含 "ST" |
| 15 | is_new | 0/1 | 是否新股（上市<60 日） | days_since_list < 60 |
| 16 | is_bj | 0/1 | 是否北交所 | code startswith 'bj' |
| 17 | is_gem | 0/1 | 是否创业板/科创板 | code 'sz30' 或 'sh688' |
| 18 | limit | float | 涨停限制（10%/20%/30%） | 见代码 |

【输出】
  data/predictions/predict_YYYYMMDD_HHMMSS.csv      全部股票预测（5565 行）
  data/predictions/predict_top20_YYYYMMDD_HHMMSS.csv Top 20 高分
  data/predictions/predict_high_YYYYMMDD_HHMMSS.csv  阈值 0.7+ 高分
  data/predictions/predict_YYYYMMDD_HHMMSS.md       Markdown 简明报告

【运行】
  # 默认：daban + small 模式（Top 5），阈值 0.7
  venv/bin/python scripts/predict.py

  # 指定日期 + 显式 Top N
  venv/bin/python scripts/predict.py --date 2026-09-20 --top 10

  # 自定义阈值
  venv/bin/python scripts/predict.py --threshold 0.6

  # 指定几只股（调试）
  venv/bin/python scripts/predict.py --codes 600519,000001,300750

【关键设计】
  - 跳过北交所 920XXX：训练时虽然包含，但 production 数据不连续，跳过避免噪声
  - 跳过 ST 股：st 涨跌幅限制 5%，与正常 10/20/30 不同
  - 跳过新股（上市<30 日）：新股前 N 日一字板，特征失真
  - 至少 30 日历史：少于 30 日 K 线无法计算 5 日窗口 + 复权因子
"""

import argparse
import json
import os
import sys
import warnings
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import lightgbm as lgb
import pandas as pd

warnings.filterwarnings('ignore')

# ============== 路径 ==============
ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
PARQUET_DIR = ROOT / 'data' / 'parquet' / 'daily'
ADJ_DIR = ROOT / 'data' / 'adj_factor'
MODEL_FILE = ROOT / 'data' / 'double_up' / 'd_signal_5d_model.txt'
OUTPUT_DIR = ROOT / 'data' / 'predictions'
UNIVERSE_FILE = ROOT / 'data' / 'all_a_stocks.json'

# ============== 多模型注册表(信号突破:分类 → PnL 回归)==============
# kind: cls=分类器(输出概率,阈值过滤); reg=回归器(输出预期 PnL,Top-N 排序)
# 评估见 scripts/eval_signal_forward.py / eval_signal_honest.py(时序前向,去泄漏)
MODEL_REGISTRY = {
    "prod":           (MODEL_FILE, "cls"),                                                       # 当前生产(18维分类,回滚用)
    "v9":             (ROOT / "data" / "double_up" / "d_signal_5d_model_v9.txt", "cls"),          # 26维分类
    "v10_dart":       (ROOT / "data" / "double_up" / "d_signal_5d_model_v10.txt", "cls"),         # DART 随机化分类
    "vpnl":           (ROOT / "data" / "double_up" / "d_signal_5d_model_vpnl.txt", "reg"),        # PnL 回归
    "vpnl_asym":      (ROOT / "data" / "double_up" / "d_signal_5d_model_vpnl_asym.txt", "reg"),    # PnL 回归(非对称)
    "vpnl_huber":     (ROOT / "data" / "double_up" / "d_signal_5d_model_vpnl_huber.txt", "reg"),   # 原部署默认(回滚用)
    "vpnl_huber_ind": (ROOT / "data" / "double_up" / "d_signal_5d_model_vpnl_huber_industry.txt", "reg"),
    "vpnl_huber_mkt": (ROOT / "data" / "double_up" / "d_signal_5d_model_vpnl_huber_market.txt", "reg"),  # 需当日市场特征
    "vpnl_huber_stage1": (ROOT / "data" / "double_up" / "d_signal_5d_model_vpnl_huber_stage1.txt", "reg"),  # ★ Step 7 新模型(21维精选+候选因子)
    # 打板专用：29 维特征（26 旧 + 3 新涨停特征），二分类目标 = 次日收盘涨停
    "daban":             (ROOT / "data" / "double_up" / "d_signal_5d_model_daban.txt", "cls"),
}
# ============== 模型切换配置（2026-10-19 切流，daban 小仓位启用）==============
# 切换依据：历史累积验证 334 交易日 / 199,283 样本，分年度稳定领先
#   · AUC        : daban 0.8090 vs stage1 0.7526（+0.0563）
#   · Top5 命中率: daban 24.52%/21.70% vs stage1 11.64%/11.70%（约 2 倍）
# 详见 docs/COMPARE_EXTENDED_2026-10-19.md
#
# 生效范围：仅 predict.py 主流程；plan_e（规则策略）不加载模型，不受影响。
#
# 仓位模式与模型绑定（小仓位 = 少而精；大仓位 = 稳健优先）：
POSITION_MODE_MODEL = {
    "small": "daban",              # 小仓位：专用二分类模型（次日涨停），Top 5
    "full":  "vpnl_huber_stage1",  # 大仓位：稳健回归模型（波段 PnL），Top 20
}
DEFAULT_POSITION_MODE = "small"
DEFAULT_MODEL = POSITION_MODE_MODEL[DEFAULT_POSITION_MODE]  # ★ 切流后默认 = daban
# 小仓位场景默认 Top N（打板策略：少而精，命中率随 TopN 缩小显著提升）
DEFAULT_TOP = 5
# 回退用模型（异常时自动回退到此，保证流程不中断）
FALLBACK_MODEL = "vpnl_huber_stage1"
ACTIVE_MODEL_PATH = MODEL_REGISTRY[DEFAULT_MODEL][0]
ACTIVE_IS_REG = MODEL_REGISTRY[DEFAULT_MODEL][1] == "reg"
# Step 7：特征管线模式（legacy=26 基础维; polars=26 基础维+9 候选因子，新模型 vpnl_huber_stage1 必需）
ACTIVE_FEATURE_MODE = "polars"  # 随 DEFAULT_MODEL=stage1 对齐（legacy 26 基础维+候选因子）
# 新模型必需的 polars 候选因子列（legacy 26 维不产出这些 → 缺列会 KeyError）
_CANDIDATE_FACTOR_COLS = {"ret_skew_20", "vol_term", "vp_corr_20", "amihud_20"}

# ============== 18 维特征定义（顺序与训练一致）==============
FEATURE_COLS = [
    'limit',          # 涨停限制
    'prev_close',     # T-1 收盘价
    'ret_5d',         # 5 日累计涨幅
    'ret_3d',         # 3 日累计涨幅
    'max_5d_pct',     # 5 日最大单日涨幅
    'limit_up_5d',    # 5 日涨停次数
    'limit_up_t1',    # T-1 是否涨停
    'pct_t1',         # T-1 当日涨幅
    'avg_amount_5d',  # 5 日均成交额
    'vol_ratio_t1',   # T-1 量比
    'volatility_5d',  # 5 日波动率
    'close_pos_5d',   # T-1 收盘价在 5 日 K 线位置
    'max_drawdown_5d',# 5 日最大跌幅
    'turnover_proxy', # T-1 换手代理
    'is_st',          # 是否 ST
    'is_new',         # 是否新股
    'is_bj',          # 是否北交所
    'is_gem',         # 是否创业板/科创板
]


# ============== 特征计算 ==============

# 涨停限制与 18/26 维特征：单一数据源见 tdxfeed/features_5d（向后兼容 v8 18 维与 v9+ 26 维）
# compute_features() 始终返回 26 维（含 8 维扩展），main() 按 model.feature_name() 取子集
# Step 7：polars 候选因子路径（新模型 vpnl_huber_stage1 必需）
import polars as pl  # noqa: E402
from _common import compute_window_features_extended  # noqa: E402

from tdxfeed.daban.limit_price import exchange_prefix  # noqa: E402
from tdxfeed.daban.sector_features import (  # noqa: E402
    EXTRA_FEATURE_COLS,
    build_lookup,
)
from tdxfeed.factors_candidates import compute_candidate_features  # noqa: E402


def is_st(name: str) -> int:
    """判断 ST（影响涨跌幅）"""
    if not isinstance(name, str):
        return 0
    return 1 if ('ST' in name or '*ST' in name) else 0


# 审计 P1-4-1 (2026-10-17): _PARQUET_CACHE 已删除——全市场 predict 每股只算一次,
# 缓存零命中且全量 DataFrame 常驻 ~400MB (8 workers); 实测直读 1.5ms/股 无性能损失。

# 板块分配（与 build_market_features.py 一致）
_BOARD_SET = ["sh_main", "sh_kechuang", "sz_main", "sz_chinext", "bj"]


def _assign_board(code: str) -> str:
    if not isinstance(code, str):
        return "other"
    if code.startswith("bj"):
        return "bj"
    if code.startswith("sz300") or code.startswith("sz301"):
        return "sz_chinext"
    if code.startswith("sz000") or code.startswith("sz001") or code.startswith("sz002"):
        return "sz_main"
    if code.startswith("sh688") or code.startswith("sh689"):
        return "sh_kechuang"
    if code.startswith("sh600") or code.startswith("sh601") or code.startswith("sh603") or code.startswith("sh605"):
        return "sh_main"
    return "other"


_MARKET_CACHE: pd.DataFrame | None = None
_INDUSTRY_FEATURES_CACHE: pd.DataFrame | None = None
_INDUSTRY_MAPPING_CACHE: dict[str, str] = {}


def _get_market_features() -> pd.DataFrame:
    """懒加载 market_features.csv(全市场代理特征,按 signal_date 查)。"""
    global _MARKET_CACHE
    if _MARKET_CACHE is None:
        mkt_csv = ROOT / "data" / "double_up" / "market_features.csv"
        if mkt_csv.exists():
            df = pd.read_csv(mkt_csv)
            df["signal_date"] = pd.to_datetime(df["signal_date"]).dt.strftime("%Y-%m-%d")
            _MARKET_CACHE = df
    return _MARKET_CACHE


def _get_industry_features() -> pd.DataFrame:
    """懒加载 industry_features.csv(akshare 申万行业聚合,按 date × industry_code)。"""
    global _INDUSTRY_FEATURES_CACHE
    if _INDUSTRY_FEATURES_CACHE is None:
        ind_csv = ROOT / "data" / "industry" / "industry_features.csv"
        if ind_csv.exists():
            df = pd.read_csv(ind_csv)
            df["signal_date"] = pd.to_datetime(df["signal_date"]).dt.strftime("%Y-%m-%d")
            # industry_code 可能是 float, 转 str
            df["industry_code"] = df["industry_code"].astype(str).str.replace(r"\.0$", "", regex=True)
            df.loc[df["industry_code"].isin(["nan", "-1.0"]), "industry_code"] = "UNMAPPED"
            _INDUSTRY_FEATURES_CACHE = df
    return _INDUSTRY_FEATURES_CACHE


def _get_industry_mapping() -> dict[str, str]:
    """懒加载 akshare 申万 code → industry_code。"""
    global _INDUSTRY_MAPPING_CACHE
    if not _INDUSTRY_MAPPING_CACHE:
        m = ROOT / "data" / "industry" / "sw_stock_industry.csv"
        if m.exists():
            df = pd.read_csv(m, usecols=["code", "industry_code"])
            df["industry_code"] = df["industry_code"].astype(str).str.replace(r"\.0$", "", regex=True)
            df.loc[df["industry_code"].isin(["nan", "-1.0"]), "industry_code"] = "UNMAPPED"
            _INDUSTRY_MAPPING_CACHE = dict(zip(df["code"], df["industry_code"], strict=True))
    return _INDUSTRY_MAPPING_CACHE


_INDUSTRY_FEATURE_NAMES = ["ind_ret_5d_avg", "ind_amt_avg_5d", "ind_pct_t1_avg",
                           "ind_volatility_avg", "ind_stock_count"]


_SECTOR_LOOKUP = None


def _get_sector_lookup():
    """懒加载板块特征查表（阶段3）。"""
    global _SECTOR_LOOKUP
    if _SECTOR_LOOKUP is None:
        _SECTOR_LOOKUP = build_lookup()
    return _SECTOR_LOOKUP


def _enrich_industry_features(feats: dict, signal_date: str) -> dict:
    """加 6 维 akshare 申万行业特征(Task D 真实版):含 ret_minus_ind_5d 计算。"""
    code = feats["code"]
    mapping = _get_industry_mapping()
    industry_code = str(mapping.get(code, "UNMAPPED"))

    date_norm = signal_date[:10] if isinstance(signal_date, str) else signal_date.strftime("%Y-%m-%d")
    ind_feats = _get_industry_features()
    if ind_feats is not None:
        row = ind_feats[(ind_feats["signal_date"] == date_norm) & (ind_feats["industry_code"] == industry_code)]
        if not row.empty:
            for c in _INDUSTRY_FEATURE_NAMES:
                feats[c] = float(row[c].iloc[0])
            feats["ret_minus_ind_5d"] = float(feats.get("ret_5d", 0.0)) - float(row["ind_ret_5d_avg"].iloc[0])
        else:
            for c in _INDUSTRY_FEATURE_NAMES:
                feats[c] = 0.0
            feats["ret_minus_ind_5d"] = 0.0
    else:
        for c in _INDUSTRY_FEATURE_NAMES:
            feats[c] = 0.0
        feats["ret_minus_ind_5d"] = 0.0

    # 阶段3：板块特征（lag=1，与训练侧同源）
    # ⚠ 板块强度是信号日收盘后才知道 → 推理在 T-1 收盘后跑、预测 T+1，
    #   故只能用 T-1 的值（lag=1）。旧模型不含该特征，不受影响
    #   （predict.py 按模型 feature_name 选列）。
    feats.update(_sector_lag1_features(code, industry_code, date_norm))
    return feats


def _sector_lag1_features(code: str, industry_code: str, date_norm: str) -> dict:
    """取 T-1 的板块特征（sec_lag1 等）；缺失则填 0。"""
    lookup = _get_sector_lookup()
    if lookup is None:
        return {c: 0.0 for c in EXTRA_FEATURE_COLS}
    row = lookup.get(industry_code, str(date_norm).replace("-", ""), lag=1)
    if row is None:
        return {c: 0.0 for c in EXTRA_FEATURE_COLS}
    return {c: float(row.get(c, 0.0)) for c in EXTRA_FEATURE_COLS}


def _enrich_market_features(feats: dict, signal_date: str) -> dict:
    """加 5 维板块 one-hot + 4 维市场代理特征(Task D 行业代理)。"""
    code = feats["code"]
    board = _assign_board(code)
    for b in _BOARD_SET:
        feats[f"board_{b}"] = 1.0 if board == b else 0.0
    mkt = _get_market_features()
    date_norm = signal_date[:10] if isinstance(signal_date, str) else signal_date.strftime("%Y-%m-%d")
    if mkt is not None:
        row = mkt[mkt["signal_date"] == date_norm]
        if not row.empty:
            for c in ["mkt_ret_5d_avg", "mkt_amt_avg_5d", "mkt_pct_t1_avg", "stock_count_active"]:
                feats[c] = float(row[c].iloc[0])
        else:
            for c in ["mkt_ret_5d_avg", "mkt_amt_avg_5d", "mkt_pct_t1_avg", "stock_count_active"]:
                feats[c] = 0.0
    else:
        for c in ["mkt_ret_5d_avg", "mkt_amt_avg_5d", "mkt_pct_t1_avg", "stock_count_active"]:
            feats[c] = 0.0
    return feats


def _compute_candidate_polars(df_raw: pd.DataFrame, code: str) -> dict | None:
    """用 polars/candidates 路径计算 9 个候选因子（新模型 vpnl_huber_stage1 必需）。"""
    cols = [c for c in ("date", "open", "high", "low", "close", "volume", "amount")
            if c in df_raw.columns]
    if len(cols) < 5:
        return None
    try:
        df_pl = pl.from_pandas(df_raw[cols].reset_index(drop=True))
        return compute_candidate_features(df_pl, code)
    except Exception:
        return None


def compute_features(code: str) -> dict:
    """
    对单只股计算特征（口径与训练侧完全一致，见 tdxfeed/features_5d）。

    返回 26 维(技术面扩展) + 5 维板块 + 4 维市场代理 = 35 维(若模型需要)。
    任意模型期望的特征子集都会被自动选取。

    返回 None 表示无法计算（数据不足等）。
    """
    code = str(code).zfill(6)
    raw = code.lower()
    if raw[:2] in ('sh', 'sz', 'bj'):
        prefix, code_clean = raw[:2], raw[2:].zfill(6)
        adj_code = raw.lstrip('bj')
    else:
        code_clean = raw.zfill(6)
        # 裸码按板块推断交易所：0/3→sz，6→sh，4/8/920→bj。
        # 不能按文件存在顺序猜，否则 000001(平安银行) 会被 sh000001(上证指数) 抢走。
        prefix = exchange_prefix(code_clean)
        adj_code = code_clean

    fp = PARQUET_DIR / f"{prefix}{code_clean}.parquet"
    if not fp.exists():
        fp = None
        for cand in [
            f"{code_clean}.parquet",
            f"sh{code_clean}.parquet",
            f"sz{code_clean}.parquet",
            f"bj{code_clean}.parquet",
        ]:
            f = PARQUET_DIR / cand
            if f.exists():
                fp = f
                break
    if fp is None:
        return None

    try:
        # 审计 P1-4-1 (2026-10-17): 删除 _PARQUET_CACHE 全量缓存
        # 实测: 每股在全市场 predict 中只计算一次, 缓存零命中;
        # 全量 DataFrame 常驻 (8 workers x ~860 股 x ~1028 行) = ~400MB 纯浪费。
        # 实测读取仅 1.5ms/股 (单文件 30-96KB), 直读无性能损失。
        raw = pd.read_parquet(fp)
        df_raw = raw.copy()
    except Exception:
        return None

    if len(df_raw) < 30:
        return None

    # 审计 P1-5-1 (2026-10-17): 数据新鲜度防御
    # 背景: 周末 Top 20 曾出现数据陈旧 36/47 天的仙股(688496/002731), 靠人工剔除。
    # 规则: 最新数据日期距今超过 MAX_STALE_DAYS 天 -> 判定停牌/退市/数据断裂, 剔除并留痕。
    # 阈值 10 天 ≈ 5 个交易日 * 1.6 倍周末/节假日补偿(精确交易日历可后续接入)。
    MAX_STALE_DAYS = 10
    try:
        _last_date = pd.to_datetime(df_raw['date']).max()
        _stale_days = (pd.Timestamp.now().normalize() - _last_date).days
        if _stale_days > MAX_STALE_DAYS:
            print(f"[WARN] {code} 数据陈旧 {_stale_days} 天(最新 {_last_date.date()}), 剔除")
            return None
    except Exception as e:
        print(f"[WARN] {code} date 列解析异常, freshness 检查跳过: {e!r}")

    # 计算 T-1 日的 adj_factor（仅用于 meta 展示 qfq_close；特征用 raw close）
    adj_factor_value = 1.0
    adj_fp = ADJ_DIR / f"{adj_code}.parquet"
    if adj_fp.exists():
        try:
            adj_df = pd.read_parquet(adj_fp)
            adj_df['date'] = pd.to_datetime(adj_df['date'])
            tmp = df_raw[['date', 'close']].copy()
            tmp['date'] = pd.to_datetime(tmp['date'])
            merged = tmp.merge(adj_df[['date', 'adj_factor']], on='date', how='left')
            adj_factor_value = float(merged['adj_factor'].ffill().fillna(1.0).iloc[-1])
        except Exception:
            adj_factor_value = 1.0

    # 26 维特征（始终计算扩展集，main() 按 model.feature_name() 取子集，向后兼容 v8/v9+）
    feats = compute_window_features_extended(df_raw, code)
    if feats is None:
        return None

    # Step 7：特征管线分支（--features）
    #   legacy（默认）：仅 26 基础维，向后兼容 v8/v9+/vpnl_huber 等旧模型（位级一致、is_st 由名称判定）
    #   polars：在 26 基础维之上追加 9 候选因子（含新模型 vpnl_huber_stage1 必需的 4 个：
    #           ret_skew_20/vol_term/vp_corr_20/amihud_20），由 factors_candidates(polars) 计算。
    #   设计取舍：17 个基础维沿用已验证的 legacy features_5d（最大化一致性与安全），仅新候选因子
    #   走 polars 路径，满足「新管线产出 21 列」且零破坏现有模型（模型按 feature_name 取子集，多余列自动忽略）。
    if ACTIVE_FEATURE_MODE == "polars":
        cand = _compute_candidate_polars(df_raw, code)
        if cand is None:
            return None  # 候选因子缺失 → 新模型会 KeyError，跳过该股
        feats.update(cand)

    # meta 字段（仅展示用，非特征）
    last_date = df_raw['date'].iloc[-1]
    last_close = float(df_raw['close'].iloc[-1])
    last_volume = float(df_raw['volume'].iloc[-1])
    meta = {
        'code': code,
        'date': str(last_date)[:10],
        'close': last_close,
        'qfq_close': last_close * adj_factor_value,
        'volume': last_volume,
    }
    result = {**meta, **feats}
    # Task D 行业代理(代码前缀):5 板块 + 4 市场
    result = _enrich_market_features(result, str(last_date)[:10])
    # Task D 行业(真实 akshare 申万):6 行业特征
    result = _enrich_industry_features(result, str(last_date)[:10])
    return result


def load_single_stock(code: str) -> tuple:
    return code, compute_features(code)


def _init_feature_mode(mode: str) -> None:
    """在每个 worker 进程内显式设置特征管线模式（不依赖 fork 继承全局变量）。"""
    global ACTIVE_FEATURE_MODE
    ACTIVE_FEATURE_MODE = mode


def load_all_stocks(codes: list, max_workers: int = 8) -> pd.DataFrame:
    """并行加载所有股票的特征"""
    print(f"  并行加载 {len(codes)} 只股票...", file=sys.stderr)
    rows = []
    failed = 0
    with ProcessPoolExecutor(max_workers=max_workers,
                             initializer=_init_feature_mode,
                             initargs=(ACTIVE_FEATURE_MODE,)) as executor:
        futures = {executor.submit(load_single_stock, code): code for code in codes}
        for i, fut in enumerate(as_completed(futures)):
            _code, data = fut.result()
            if data is not None:
                rows.append(data)
            else:
                failed += 1
            if (i + 1) % 500 == 0:
                print(f"    进度: {i+1}/{len(codes)} (失败 {failed})", file=sys.stderr)
    print(f"    完成: 成功 {len(rows)} / 失败 {failed}", file=sys.stderr)
    return pd.DataFrame(rows)


def load_names() -> dict:
    """加载代码 -> 名称映射（all_a_stocks.json 实为 code->name 结构）。

    审计 P1-5 补丁 (2026-10-18): 用于实时 ST 二次校验。
    背景: 特征层 is_st 基于训练时名称判断, 名称映射已更新为 *ST 但特征未同步
    -> 300338(*ST开元)/301139(*ST元道) 漏进 Top 20。
    """
    fp = ROOT / 'data' / 'all_a_stocks.json'
    try:
        with open(fp, encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, dict) and data and not any(k in data for k in ('codes', 'stocks')):
            return {str(k).zfill(6): str(v) for k, v in data.items()}
    except Exception as e:
        # 审计 Round3 修复: 原静默 pass 会致 ST 实时校验静默失效
        # (load_names 返回空 -> 名称二次校验被跳过 -> ST 股重新漏网)
        print(f"[WARN] 名称映射读取失败, 实时 ST 校验将失效: {e!r}")
    return {}


def load_universe() -> list:
    """加载全 A 股代码清单"""
    fp = ROOT / 'data' / 'all_a_stocks.json'
    if fp.exists():
        with open(fp) as f:
            data = json.load(f)
        if isinstance(data, dict):
            codes = data.get('codes', data.get('stocks', []))
            if not codes:
                # 实际结构是 {code: name}；漏掉这一分支会回退扫 parquet，
                # 把 ETF/指数也纳入预测（实测 6,882 个代码、1,670 次失败）。
                codes = [k for k in data if str(k).isdigit()]
        elif isinstance(data, list):
            codes = data
        else:
            codes = []
        if codes:
            return [str(c).zfill(6) for c in codes]
        print(f"[WARN] {fp.name} 未解析出代码，回退扫描 parquet 目录")
    # 回退：扫 parquet 目录
    codes = set()
    for fp in PARQUET_DIR.glob('*.parquet'):
        name = fp.stem
        if '.bak' in name:  # 跳过 v2 迁移备份 (.v1.bak), 非活数据
            continue
        for prefix in ['sh', 'sz', 'bj']:
            if name.startswith(prefix):
                name = name[len(prefix):]
                break
        codes.add(name)
    return sorted(codes)


def run_predict(args):
    """核心预测逻辑：返回 (feat_df, high_score)。CLI 与 Web UI 共用。

    回归模型(reg):输出为预期 PnL(非概率),高分=正期望(PnL>0),Top-N 排序见 head。
    """
    if not ACTIVE_MODEL_PATH.exists():
        raise FileNotFoundError(f"❌ 模型不存在: {ACTIVE_MODEL_PATH}")
    model = lgb.Booster(model_file=str(ACTIVE_MODEL_PATH))
    feature_cols = model.feature_name()
    if model.num_feature() != len(FEATURE_COLS):
        print(f"  ℹ 模型 {model.num_feature()} 维 vs 脚本默认 {len(FEATURE_COLS)} 维 → 动态按模型取 {len(feature_cols)} 列")
    if args.codes:
        codes = [c.strip().zfill(6) for c in args.codes.split(',')]
    else:
        codes = load_universe()
        if not args.no_skip_bj:
            codes = [c for c in codes if not c.startswith('920')]
    feat_df = load_all_stocks(codes, args.workers)
    if feat_df.empty:
        return None, None
    if not args.no_skip_st:
        feat_df = feat_df[feat_df['is_st'] == 0]
    if not args.no_skip_new:
        feat_df = feat_df[feat_df['is_new'] == 0]
    if not args.no_skip_open_limit:
        # 与训练侧 --exclude-open-limit 同口径（is_open_limit_up）：
        # T 日(信号日/买入日)开盘价已达涨停价 -> 一字/准一字板 -> 打板者无法买入，
        # 实盘不可执行。训练时此类样本已被剔除，推理侧必须同步过滤，
        # 否则模型仍会把"T 日一字板"股排到 Top N（2026-10-20 实测 Top5 中 2 只如此）。
        # gap_t1 = open[T] / close[T-1] - 1（factors_candidates.py），容差与 is_limit_up_ret 一致。
        _open_limit_mask = feat_df['gap_t1'] >= (feat_df['limit'] - 0.001)
        _n_ol = int(_open_limit_mask.sum())
        if _n_ol:
            print(f"  过滤 T 日开盘即涨停(不可买入): {len(feat_df)} -> {len(feat_df) - _n_ol} (剔除 {_n_ol} 只)")
        feat_df = feat_df[~_open_limit_mask]
    X = feat_df[feature_cols].fillna(0)
    feat_df['prob'] = model.predict(X)
    feat_df = feat_df.sort_values('prob', ascending=False).reset_index(drop=True)
    feat_df['rank'] = feat_df.index + 1
    if ACTIVE_IS_REG:
        # 回归:预期 PnL,高分=正期望(PnL>0)
        high_score = feat_df[feat_df['prob'] > 0].copy()
    else:
        high_score = feat_df[feat_df['prob'] >= args.threshold].copy()
    return feat_df, high_score


def launch_web(args):
    """gradio Web UI：输入代码/全市场，输出 Top 概率表与高分统计。"""
    import gradio as gr

    _model = None

    def get_model():
        nonlocal _model
        if _model is None:
            if not ACTIVE_MODEL_PATH.exists():
                raise RuntimeError(f"模型不存在: {ACTIVE_MODEL_PATH}")
            _model = lgb.Booster(model_file=str(ACTIVE_MODEL_PATH))
        return _model

    def predict(codes_text, threshold, top_n, skip_bj, skip_st, skip_new, skip_open_limit=True):
        a = argparse.Namespace(
            codes=(codes_text.strip() or None),
            threshold=float(threshold),
            top=int(top_n),
            workers=8,
            no_skip_bj=(not skip_bj),
            no_skip_st=(not skip_st),
            no_skip_new=(not skip_new),
            no_skip_open_limit=(not skip_open_limit),
            # Step 7 修复：Web 命名空间补充 date 默认值（与 main 约定一致：None=今天）。
            # run_predict 当前不读 args.date，但补全可避免任何路径缺失属性导致 AttributeError。
            date=None,
        )
        try:
            feat_df, high = run_predict(a)
        except Exception as e:
            return [], f"❌ 错误: {e}"
        if feat_df is None:
            return [], "❌ 无可用数据"
        show = feat_df.head(int(top_n))[
            ['rank', 'code', 'prob', 'close', 'qfq_close', 'pct_t1', 'ret_5d', 'vol_ratio_t1', 'limit']
        ].copy()
        show['prob'] = show['prob'].map(lambda x: f"{x:.4f}")
        show['pct_t1'] = (show['pct_t1'] * 100).map(lambda x: f"{x:+.2f}%")
        show['ret_5d'] = (show['ret_5d'] * 100).map(lambda x: f"{x:+.2f}%")
        metric_name = "平均预期PnL" if ACTIVE_IS_REG else "平均概率"
        tag = ("正期望" if ACTIVE_IS_REG else f"阈值{threshold}+")
        summary = (f"✅ 共 {len(feat_df)} 只 | {tag} 高分 {len(high)} 只 | "
                   f"{metric_name} {feat_df['prob'].mean():.4f}")
        return show.values.tolist(), summary

    with gr.Blocks(title="方案D 翻倍潜力预测") as demo:
        gr.Markdown("# 方案 D 每日翻倍潜力预测 (LightGBM)\n"
                    "输入代码（逗号分隔，留空=全市场）并点击「预测」。")
        with gr.Row():
            codes_tb = gr.Textbox(label="股票代码（逗号分隔，留空=全市场）",
                                  placeholder="600519,000001,300750", scale=3)
            threshold = gr.Number(value=0.7, label="高分阈值", scale=1)
            top_n = gr.Number(value=20, label="Top N", scale=1)
        with gr.Row():
            skip_bj = gr.Checkbox(value=True, label="跳过北交所")
            skip_st = gr.Checkbox(value=True, label="跳过 ST")
            skip_new = gr.Checkbox(value=True, label="跳过新股")
            skip_open_limit = gr.Checkbox(value=True, label="过滤一字板(开盘即涨停,买不进)")
            btn = gr.Button("预测", variant="primary")
        summary = gr.Textbox(label="统计", interactive=False)
        out = gr.Dataframe(label="Top 结果", interactive=False)
        btn.click(fn=predict,
                  inputs=[codes_tb, threshold, top_n, skip_bj, skip_st, skip_new, skip_open_limit],
                  outputs=[out, summary])
    demo.launch(server_name="0.0.0.0", server_port=args.port, share=args.share)


def parse_args():
    """审计 P1-3-3 (2026-10-17): 从 254 行 main() 拆出——CLI 参数解析。"""
    parser = argparse.ArgumentParser(description='方案 D 每日翻倍潜力预测')
    parser.add_argument('--date', help='基准日（YYYY-MM-DD），默认今天')
    parser.add_argument('--top', type=int, default=None,
                        help='输出 Top N 高分股（默认由仓位模式决定: '
                             'small->5, full->20；显式指定则优先）')
    parser.add_argument('--threshold', type=float, default=0.7, help='高分阈值')
    parser.add_argument('--codes', help='指定股票代码（逗号分隔）')
    parser.add_argument('--workers', type=int, default=8, help='并行进程数')
    parser.add_argument('--position-mode', default=DEFAULT_POSITION_MODE,
                        choices=['small', 'full'],
                        help='仓位模式: small=小仓位(默认, daban+Top5) / '
                             'full=大仓位(vpnl_huber_stage1+Top20)')
    parser.add_argument('--no-skip-bj', action='store_true', help='不跳过北交所')
    parser.add_argument('--no-skip-st', action='store_true', help='不跳过 ST')
    parser.add_argument('--no-skip-new', action='store_true', help='不跳过新股')
    parser.add_argument('--no-skip-open-limit', action='store_true',
                        help='不过滤 T 日开盘即涨停(一字/准一字板, 实盘买不进)')
    parser.add_argument('--no-gem', action='store_true', help='禁用 is_gem 特征(强制置 0),避免重训前 OOD 偏差（短期止血，重训后可移除）')
    parser.add_argument('--web', action='store_true', help='启动 Web UI (gradio)，交互式选股预测')
    parser.add_argument('--port', type=int, default=7860, help='Web UI 端口')
    parser.add_argument('--share', action='store_true', help='gradio share 公网隧道')
    parser.add_argument('--no-freshness-check', action='store_true', help='跳过数据新鲜度校验')
    parser.add_argument('--model', default=None,
                        help='模型选择(最高优先): prod/v9/v10_dart/vpnl/vpnl_asym/'
                             'vpnl_huber/vpnl_huber_ind/vpnl_huber_mkt/'
                             'vpnl_huber_stage1/daban(默认由 --position-mode 决定: '
                             'small->daban, full->vpnl_huber_stage1)')
    parser.add_argument('--features', default='polars', choices=['legacy', 'polars'],
                        help='特征管线: legacy(26 基础维) / polars(26 基础维+9 候选因子,默认,'
                             'vpnl_huber_stage1 必需)')
    # 方案 B 2026-10-16：--emit-top 叠加输出到 data/signals/（与默认输出叠加,默认关闭）
    parser.add_argument('--emit-top', action='store_true',
                        help='额外写一份 Top N 信号到 data/signals/(与默认输出叠加,默认关闭)')
    parser.add_argument('--emit-top-n', type=int, default=50,
                        help='--emit-top 输出 Top N 默认 50')
    parser.add_argument('--emit-threshold', type=float, default=0.7,
                        help='--emit-top 阈值过滤 默认 0.7')
    parser.add_argument('--emit-fields', default='code,score,ts,top_k',
                        help='--emit-top 输出字段 逗号分隔,默认 code,score,ts,top_k')
    args = parser.parse_args()
    return args


def resolve_model(args):
    """审计 P1-3-3: 拆出——模型选择/加载/特征列守卫。返回 (model_name, model, feature_cols)。

    2026-10-19 切流后的模型解析优先级（从高到低）：
      1. `--model X`      显式指定（最高优先，用于回退/调试）
      2. `TDX_MODEL=X`    环境变量覆盖（不改代码即可临时回退）
      3. 仓位模式绑定     `--position-mode small|full` → daban / vpnl_huber_stage1

    异常处理：目标模型文件缺失或加载失败时，**自动回退到 FALLBACK_MODEL** 并告警，
    保证主流程不中断（回退行为一致、可验证）。
    """
    # 1) 显式 --model
    if args.model:
        model_name = args.model
        source = "--model 显式指定"
    # 2) 环境变量覆盖
    elif os.environ.get("TDX_MODEL"):
        model_name = os.environ["TDX_MODEL"]
        source = "TDX_MODEL 环境变量"
    # 3) 仓位模式绑定
    else:
        model_name = POSITION_MODE_MODEL[args.position_mode]
        source = f"--position-mode {args.position_mode}"

    if model_name not in MODEL_REGISTRY:
        sys.exit(f"❌ 未知模型: {model_name}; 可选: {', '.join(MODEL_REGISTRY)}")

    # 小仓位场景提示（生效边界声明）
    if args.position_mode == "small" and model_name == "daban":
        print(f"  [小仓位模式] 模型=daban(次日涨停二分类) Top={args.top} "
              f"(历史 Top5 命中率 21.7~24.5%)")
    elif args.position_mode == "full" and model_name == FALLBACK_MODEL:
        print(f"  [大仓位模式] 模型={FALLBACK_MODEL}(波段PnL回归) Top={args.top} (稳健优先)")

    global ACTIVE_MODEL_PATH, ACTIVE_IS_REG, ACTIVE_FEATURE_MODE
    ACTIVE_MODEL_PATH = MODEL_REGISTRY[model_name][0]
    ACTIVE_IS_REG = MODEL_REGISTRY[model_name][1] == "reg"
    ACTIVE_FEATURE_MODE = args.features

    # 异常回退：目标模型不可用 -> 回退 FALLBACK_MODEL
    if not ACTIVE_MODEL_PATH.exists():
        print(f"  [WARN] 模型文件缺失: {ACTIVE_MODEL_PATH}（来源: {source}）")
        if model_name != FALLBACK_MODEL:
            print(f"  [WARN] 自动回退到 {FALLBACK_MODEL}，保证主流程不中断")
            model_name = FALLBACK_MODEL
            ACTIVE_MODEL_PATH = MODEL_REGISTRY[model_name][0]
            ACTIVE_IS_REG = MODEL_REGISTRY[model_name][1] == "reg"
        else:
            sys.exit(f"❌ 回退模型也不存在: {ACTIVE_MODEL_PATH}")

    # 加载模型
    print(f"  加载模型[{model_name}]: {ACTIVE_MODEL_PATH}（来源: {source}）")
    print(f"    类型: {'回归(PnL 预期)' if ACTIVE_IS_REG else '分类(概率)'}")
    try:
        model = lgb.Booster(model_file=str(ACTIVE_MODEL_PATH))
    except Exception as e:
        print(f"  [WARN] 模型加载失败: {e!r}")
        if model_name != FALLBACK_MODEL:
            print(f"  [WARN] 自动回退到 {FALLBACK_MODEL}")
            model_name = FALLBACK_MODEL
            ACTIVE_MODEL_PATH = MODEL_REGISTRY[model_name][0]
            ACTIVE_IS_REG = MODEL_REGISTRY[model_name][1] == "reg"
            model = lgb.Booster(model_file=str(ACTIVE_MODEL_PATH))
        else:
            sys.exit(f"❌ 回退模型也加载失败: {e!r}")
    print(f"    期望特征数: {model.num_feature()}")
    print(f"    树数: {model.num_trees()}")

    # 动态特征列:从模型读,确保与训练时顺序一致 (向后兼容 v8 18 维 / v9+ 26 维)
    feature_cols = model.feature_name()
    # Step 7 守卫：新模型(vpnl_huber_stage1)需要 polars 候选因子列，若用 legacy 管线会 KeyError
    if ACTIVE_FEATURE_MODE != "polars" and _CANDIDATE_FACTOR_COLS & set(feature_cols):
        sys.exit("❌ 该模型需要 polars 候选因子列({})，请加 --features polars"
                 .format(", ".join(sorted(_CANDIDATE_FACTOR_COLS & set(feature_cols)))))
    if len(feature_cols) > 18:
        print(f"    [扩展模式] {len(feature_cols)} 维特征(18 基础 + {len(feature_cols)-18} 工程化)")
    else:
        print(f"    [基础模式] {len(feature_cols)} 维特征")
    return model_name, model, feature_cols


def run_prediction(args, model, feature_cols):
    """审计 P1-3-3: 拆出——股票池/并行特征/ST 新股过滤/打分。返回 (feat_df, high_score)。"""
    # 股票代码
    if args.codes:
        codes = [c.strip().zfill(6) for c in args.codes.split(',')]
    else:
        codes = load_universe()
        # 默认跳过北交所
        if not args.no_skip_bj:
            codes = [c for c in codes if not c.startswith('920')]
        # 默认跳过 ST（在特征计算层）
        # 默认跳过新股（在特征计算层）
    print(f"  股票数: {len(codes)}")

    # 计算特征
    print("\n[1/3] 计算基础特征...")
    feat_df = load_all_stocks(codes, args.workers)
    if feat_df.empty:
        sys.exit("❌ 无可用数据")
    print(f"    有效股票: {len(feat_df)}")

    # 过滤 ST / 新股
    if not args.no_skip_st:
        before = len(feat_df)
        feat_df = feat_df[feat_df['is_st'] == 0]
        # 实时名称 ST 二次校验（审计 P1-5 补丁 2026-10-18）:
        # 特征层 is_st 滞后(名称映射已更新 *ST 但特征未同步) -> 用最新名称兜底
        names = load_names()
        n_extra = 0
        if names:
            st_mask = feat_df['code'].map(
                lambda c: 'ST' in names.get(str(c).zfill(6), '').upper())
            n_extra = int(st_mask.sum())
            if n_extra:
                feat_df = feat_df[~st_mask]
        msg = f"    过滤 ST: {before} → {len(feat_df)}"
        if n_extra:
            msg += f" (实时名称再剔除 {n_extra} 只)"
        print(msg)
    if not args.no_skip_new:
        before = len(feat_df)
        feat_df = feat_df[feat_df['is_new'] == 0]
        print(f"    过滤新股: {before} → {len(feat_df)}")
    # T 日(信号日/买入日)开盘即涨停剔除（与训练侧 --exclude-open-limit 同口径）:
    # 开盘价已达涨停价 -> 一字/准一字板 -> 打板者无法买入, 实盘不可执行。
    # 训练时此类样本已被剔除(2026-10-19 现役模型即如此训练), 推理侧必须同步过滤,
    # 否则模型仍会把"T 日一字板"股排到 Top N。
    # 实测依据: 2026-10-20 未过滤时 Top5 中 002724/003029 均为 T 日一字板(买不进)。
    # gap_t1 = open[T] / close[T-1] - 1（factors_candidates.py），容差与 is_limit_up_ret 一致。
    if not args.no_skip_open_limit and 'gap_t1' in feat_df.columns:
        before = len(feat_df)
        _ol_mask = feat_df['gap_t1'] >= (feat_df['limit'] - 0.001)
        n_ol = int(_ol_mask.sum())
        if n_ol:
            feat_df = feat_df[~_ol_mask]
            print(f"    过滤 T 日开盘即涨停(一字板/买不进): {before} → {len(feat_df)}")
    # 面值 <1 元剔除（审计 P1-5 补丁 2026-10-18）: 收盘价低于 1 元退市线
    if 'close' in feat_df.columns:
        before = len(feat_df)
        feat_df = feat_df[feat_df['close'] >= 1.0]
        if len(feat_df) < before:
            print(f"    面值过滤(<1元): {before} → {len(feat_df)}")

    # 短期止血：禁用 is_gem 特征（强制置 0，匹配旧模型训练分布 is_gem=0 常量）
    if args.no_gem:
        gem_count = int((feat_df['is_gem'] == 1).sum())
        feat_df['is_gem'] = 0
        print(f"    --no-gem: 已将 {gem_count} 只创业板/科创板的 is_gem 强制置 0（重训后建议改用 --no-skip-gem 过滤）")

    # 预测
    print("\n[2/3] 预测概率...")
    X = feat_df[feature_cols].fillna(0)
    feat_df['prob'] = model.predict(X)

    feat_df = feat_df.sort_values('prob', ascending=False).reset_index(drop=True)
    feat_df['rank'] = feat_df.index + 1

    if ACTIVE_IS_REG:
        # 回归:预期 PnL,高分=正期望(PnL>0)
        high_score = feat_df[feat_df['prob'] > 0].copy()
    else:
        high_score = feat_df[feat_df['prob'] >= args.threshold].copy()

    # 统计
    if ACTIVE_IS_REG:
        print(f"    Top {args.top} 预期PnL: {feat_df.head(args.top)['prob'].min():.4f} ~ {feat_df['prob'].max():.4f}")
        print(f"    正期望(PnL>0): {len(high_score)} 只")
        print(f"    平均预期PnL: {feat_df['prob'].mean():.4f}")
        print(f"    中位预期PnL: {feat_df['prob'].median():.4f}")
    else:
        print(f"    Top {args.top} 概率: {feat_df.head(args.top)['prob'].min():.4f} ~ {feat_df['prob'].max():.4f}")
        print(f"    阈值 {args.threshold} 以上: {len(high_score)} 只")
        print(f"    平均概率: {feat_df['prob'].mean():.4f}")
        print(f"    中位数概率: {feat_df['prob'].median():.4f}")

    return feat_df, high_score


def emit_outputs(args, model_name, feat_df, high_score):
    """审计 P1-3-3: 拆出——CSV/--emit-top/Markdown 报告/latest 同步。"""
    # 保存
    print("\n[3/3] 输出结果...")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')

    fp_all = OUTPUT_DIR / f'predict_{ts}.csv'
    feat_df.to_csv(fp_all, index=False, encoding='utf-8-sig')
    print(f"    ✓ 全部: {fp_all.name} ({len(feat_df)} 行)")

    fp_top = OUTPUT_DIR / f'predict_top{args.top}_{ts}.csv'
    feat_df.head(args.top).to_csv(fp_top, index=False, encoding='utf-8-sig')
    print(f"    ✓ Top {args.top}: {fp_top.name}")

    if len(high_score) > 0:
        fp_high = OUTPUT_DIR / f'predict_high_{ts}.csv'
        high_score.to_csv(fp_high, index=False, encoding='utf-8-sig')
        print(f"    ✓ 高分 {len(high_score)} 只: {fp_high.name}")
    else:
        print(f"    (无阈值 {args.threshold}+ 高分股)")

    # 方案 B 2026-10-16：--emit-top 叠加输出到 data/signals/
    if args.emit_top:
        signals_dir = ROOT / 'data' / 'signals'
        signals_dir.mkdir(parents=True, exist_ok=True)
        # 阈值过滤（用 emit-threshold,与默认 threshold 独立）
        if ACTIVE_IS_REG:
            emit_pool = feat_df[feat_df['prob'] > args.emit_threshold].copy()
        else:
            emit_pool = feat_df[feat_df['prob'] >= args.emit_threshold].copy()
        emit_pool = emit_pool.sort_values('prob', ascending=False).reset_index(drop=True)
        emit_pool = emit_pool.head(args.emit_top_n)
        emit_pool['top_k'] = range(1, len(emit_pool) + 1)
        emit_pool['ts'] = ts
        # 字段过滤
        requested = [f.strip() for f in args.emit_fields.split(',') if f.strip()]
        # 缺失字段容错:只保留存在的
        present = [c for c in requested if c in emit_pool.columns]
        if not present:
            print(f"    ⚠ --emit-top 字段 {args.emit_fields} 全部缺失,跳过")
        else:
            missing = [c for c in requested if c not in emit_pool.columns]
            if missing:
                print(f"    ⚠ --emit-top 缺失字段: {missing}, 仅输出存在字段: {present}")
            fp_emit = signals_dir / f'predict_{ts}_top{args.emit_top_n}_thr{args.emit_threshold}.csv'
            emit_pool[present].to_csv(fp_emit, index=False, encoding='utf-8-sig')
            print(f"    ✓ emit-top: {fp_emit.relative_to(ROOT)} ({len(emit_pool)} 行 × {len(present)} 列)")

    # Markdown 报告
    fp_md = OUTPUT_DIR / f'predict_{ts}.md'
    with open(fp_md, 'w', encoding='utf-8') as f:
        f.write("# 方案 D 每日预测报告\n\n")
        f.write(f"- **时间**: {datetime.now().isoformat(timespec='seconds')}\n")
        f.write(f"- **基准日**: {args.date or '今天'}\n")
        f.write(f"- **股票数**: {len(feat_df)}\n")
        f.write(f"- **阈值**: {args.threshold}\n")
        f.write(f"- **高分股**: {len(high_score)} 只\n\n")

        score_label = "预期PnL" if ACTIVE_IS_REG else "概率"
        if ACTIVE_IS_REG:
            f.write("## 📊 统计(回归:输出为预期 PnL)\n\n")
            f.write("| 指标 | 值 |\n|---|---|\n")
            f.write(f"| 平均预期PnL | {feat_df['prob'].mean():.4f} |\n")
            f.write(f"| 中位预期PnL | {feat_df['prob'].median():.4f} |\n")
            f.write(f"| 最高预期PnL | {feat_df['prob'].max():.4f} |\n")
            f.write(f"| 正期望(PnL>0) | {len(feat_df[feat_df['prob']>0])} 只 |\n")
            f.write(f"| 负期望 | {len(feat_df[feat_df['prob']<=0])} 只 |\n\n")
        else:
            f.write("## 📊 统计\n\n")
            f.write("| 指标 | 值 |\n|---|---|\n")
            f.write(f"| 平均概率 | {feat_df['prob'].mean():.4f} |\n")
            f.write(f"| 中位概率 | {feat_df['prob'].median():.4f} |\n")
            f.write(f"| 最高概率 | {feat_df['prob'].max():.4f} |\n")
            f.write(f"| ≥ 0.9 | {len(feat_df[feat_df['prob']>=0.9])} 只 |\n")
            f.write(f"| ≥ 0.8 | {len(feat_df[feat_df['prob']>=0.8])} 只 |\n")
            f.write(f"| ≥ 0.7 | {len(feat_df[feat_df['prob']>=0.7])} 只 |\n")
            f.write(f"| ≥ 0.6 | {len(feat_df[feat_df['prob']>=0.6])} 只 |\n\n")

        f.write(f"## 🎯 Top {args.top} 高分股\n\n")
        f.write(f"| Rank | 代码 | {score_label} | 收盘 | 前复权 | T-1 涨幅 | 5 日涨幅 | 量比 | 涨停限制 | 连板 |\n")
        f.write("|---:|:---:|---:|---:|---:|---:|---:|---:|---:|---:|\n")
        for _, r in feat_df.head(args.top).iterrows():
            # P1-2 可执行性标注 (2026-10-18): 连板>=3 高位接力一字板风险高
            consec = int(r.get('consecutive_boards', 0) or 0)
            board_tag = f"{consec}⚠" if consec >= 3 else str(consec)
            f.write(f"| {r['rank']} | {r['code']} | **{r['prob']:.4f}** | "
                    f"{r['close']:.2f} | {r['qfq_close']:.2f} | "
                    f"{r['pct_t1']*100:+.2f}% | {r['ret_5d']*100:+.2f}% | "
                    f"{r['vol_ratio_t1']:.2f} | {r['limit']*100:.0f}% | {board_tag} |\n")

        if len(high_score) > 0:
            if ACTIVE_IS_REG:
                f.write(f"\n## 🚨 正期望(PnL>0)候选明细({len(high_score)} 只)\n\n")
            else:
                f.write(f"\n## 🚨 阈值 {args.threshold}+ 高分明细\n\n")
            f.write(f"| 代码 | {score_label} | 收盘 | T-1 涨幅 |\n")
            f.write("|:---:|---:|---:|---:|\n")
            for _, r in high_score.iterrows():
                f.write(f"| {r['code']} | {r['prob']:.4f} | "
                        f"{r['close']:.2f} | {r['pct_t1']*100:+.2f}% |\n")

        f.write("\n## 使用建议\n\n")
        if ACTIVE_IS_REG:
            f.write("- 本模型为**回归器**,输出为未来 10 日预期 PnL(TP+30%/SL-10%/双边成本),非概率。\n")
            f.write(f"- **预期 PnL 越高越优先**;正期望({score_label}>0)共 {len(high_score)} 只,进入候选池。\n")
            f.write(f"- 建议按 Top {args.top} 排序结合题材/新闻人工复核,严格止损(-10%)。\n\n")
        else:
            f.write("- **阈值 0.7+**：高置信度，可直接进入候选池（结合题材/新闻人工复核）\n")
            f.write("- **阈值 0.6-0.7**：中等置信度，建议观察 1-2 日\n")
            f.write("- **阈值 < 0.6**：仅作参考，不建议行动\n\n")
        f.write(f"生成工具: `scripts/predict.py --model {model_name}`\n")
        f.write("---\n\n")
        f.write("生成工具: `scripts/predict.py`\n")
    print(f"    ✓ 报告: {fp_md.name}")

    # 同步 latest
    fp_latest = OUTPUT_DIR / 'predict_latest.md'
    fp_latest.write_text(fp_md.read_text(encoding='utf-8'), encoding='utf-8')


def main():
    """审计 P1-3-3 (2026-10-17): 原 254 行拆为 parse_args/resolve_model/run_prediction/
    emit_outputs 四段, 行为零变化(golden 验证: --codes 600000,600825,000530 Top1=600825)。"""
    args = parse_args()

    # Top N 与仓位模式联动（显式 --top 优先）
    if args.top is None:
        args.top = 20 if args.position_mode == "full" else DEFAULT_TOP

    if not args.no_freshness_check:
        from tdxfeed.freshness import require_fresh
        require_fresh(max_age_days=2)

    if args.web:
        launch_web(args)
        return

    print("=== 方案 D 预测 ===")
    print(f"  时间: {datetime.now().isoformat(timespec='seconds')}")
    print(f"  仓位模式: {args.position_mode} "
          f"(模型: {POSITION_MODE_MODEL[args.position_mode]})")
    print(f"  阈值: {args.threshold}")
    print(f"  Top:  {args.top}")

    model_name, model, feature_cols = resolve_model(args)
    feat_df, high_score = run_prediction(args, model, feature_cols)
    emit_outputs(args, model_name, feat_df, high_score)

    print("\n=== ✅ 完成 ===")
    print(f"  输出目录: {OUTPUT_DIR}")
    print(f"  Top 1: {feat_df.iloc[0]['code']} (P={feat_df.iloc[0]['prob']:.4f})")
    print(f"  Top 5: {feat_df.head(5)['code'].tolist()}")


if __name__ == '__main__':
    main()
