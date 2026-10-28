#!/usr/bin/env python3
"""sim_daily_compare.py — 日K演化模拟 vs 真实日K对比（方案 F 验证工具 v0.1）

【目的】
  从某一指定起始交易日 T0 出发，仅用 T0 之前的历史日K拟合随机过程，
  向前模拟 N 个交易日的 OHLCV 演化，再与 T0 之后的真实日K对比，
  从五个维度（趋势形态 / 波动幅度 / 关键价位 / 成交量变化 / 时间节奏）
  量化模拟的不足，并给出后续调优方向。

【模拟器（无未来泄漏）】
  - 收盘价：EWMA 波动率聚类的几何布朗运动（GBM）
      lr_i ~ N(mu, sigma_i),  sigma_i^2 = λ·σ_{i-1}^2 + (1-λ)·lr_{i-1}^2
  - 开盘价：基于历史"跳空"分布的小幅跳空
  - 最高/最低：基于历史 (high-low)/close 区间因子的随机展宽
  - 成交量：log 正态 + 与 |收益率| 线性耦合（量随波动放大）

【对比维度】
  1. 趋势形态：区间净收益、累计路径相关、上涨天数占比
  2. 波动幅度：已实现波动率、最大回撤、日均振幅
  3. 关键价位：终点价、区间极值，真实值是否落入模拟分位带
  4. 成交量变化：总量、与 |收益| 的耦合强度
  5. 时间节奏：收益一阶自相关（动量/反转）、连涨连跌平均长度

【用法】
  venv/bin/python scripts/sim_daily_compare.py --code 600519 --start 2024-01-02 --days 60
  venv/bin/python scripts/sim_daily_compare.py --code 300750 --start 2024-01-02 --days 60 --n-sim 300
"""
from __future__ import annotations

import argparse
import logging
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("sim_daily_compare")

ROOT = Path(__file__).parent.parent

from _common import daily_dir  # noqa: E402

PARQUET_DIR = daily_dir()
OUT_DIR = ROOT / "data" / "sim"
LAMBDA = 0.94  # EWMA 衰减系数


# --------------------------------------------------------------------------- #
# 数据加载
# --------------------------------------------------------------------------- #
def resolve_parquet(code: str) -> Path:
    """按市场前缀解析 parquet 路径（与 plan_e_backtest 一致）。"""
    code_clean = str(code).zfill(6).lstrip("bj")
    for cand in [f"{code_clean}.parquet", f"sh{code_clean}.parquet",
                 f"sz{code_clean}.parquet", f"bj{code_clean}.parquet"]:
        f = PARQUET_DIR / cand
        if f.exists():
            return f
    raise FileNotFoundError(f"未找到 {code} 的日K文件（已尝试 sh/sz/bj 前缀）")


# 统一日期解析：单一数据源见 tdxfeed/date_utils.parse_date
from _common import parse_date  # noqa: E402


def load_and_split(code: str, start: str, n: int) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """加载日K，按 start 切分为历史(hist) 与 未来(fut)。"""
    fp = resolve_parquet(code)
    df = pd.read_parquet(fp)
    df["date"] = parse_date(df["date"])
    df = df.sort_values("date").reset_index(drop=True)
    start_dt = pd.to_datetime(start)

    hist = df[df["date"] < start_dt].copy()
    fut = df[df["date"] >= start_dt].copy().head(n)
    if len(hist) < 120:
        raise ValueError(f"历史不足 120 日（仅 {len(hist)}），无法稳健拟合")
    if len(fut) < n:
        log.warning("真实未来不足 %d 日（仅 %d），对比窗口截断", n, len(fut))
    log.info("code=%s hist=%d日 fut=%d日 [%s ~ %s]", code, len(hist),
             len(fut), fut["date"].min().date(), fut["date"].max().date())
    return df, hist, fut


# --------------------------------------------------------------------------- #
# 拟合
# --------------------------------------------------------------------------- #
def fit_garch(lr: np.ndarray, max_iter: int = 25):
    """简易 GARCH(1,1) 估计（方差定位 + 递归条件方差 + 最小二乘），返回 ω,α,β 与末端条件方差。"""
    n = len(lr)
    su2 = float(np.var(lr))
    alpha, beta = 0.06, 0.90
    for _ in range(max_iter):
        h = np.empty(n)
        h[0] = su2
        for t in range(1, n):
            h[t] = alpha * lr[t - 1] ** 2 + beta * h[t - 1]
        omega = (1.0 - alpha - beta) * su2
        X = np.column_stack([lr[:-1] ** 2, h[:-1]])
        y = lr[1:] ** 2
        coef, *_ = np.linalg.lstsq(X, y, rcond=None)
        alpha, beta = float(coef[0]), float(coef[1])
        alpha = min(max(alpha, 0.0), 0.5)
        beta = min(max(beta, 0.0), 0.95)
        if alpha + beta > 0.99:
            s = alpha + beta
            alpha, beta = alpha / s * 0.99, beta / s * 0.99
    omega = (1.0 - alpha - beta) * su2
    h = np.empty(n)
    h[0] = su2
    for t in range(1, n):
        h[t] = omega + alpha * lr[t - 1] ** 2 + beta * h[t - 1]
    return omega, alpha, beta, float(h[-1])


def fit_jump(lr: np.ndarray):
    """Merton 跳跃扩散：用 |残差| 阈值识别跳跃日，估计 λ, μ_j, σ_j 与扩散波动。"""
    mu = float(np.mean(lr))
    resid = lr - mu
    sigma_diff = float(np.std(resid))
    thr = 4.0 * sigma_diff
    jump_mask = np.abs(resid) > thr
    lam = float(jump_mask.mean())
    if jump_mask.sum() >= 5:
        jump_sizes = resid[jump_mask]
        mu_j = float(np.mean(jump_sizes))
        sigma_j = float(np.std(jump_sizes))
        sigma_diff = float(np.std(resid[~jump_mask]))  # 扩散波动（去掉跳跃后）
    else:
        lam, mu_j, sigma_j = 0.01, 0.0, 0.0  # 跳跃过少：退化为极小强度
    return lam, mu_j, sigma_j, sigma_diff, jump_mask


def fit_params(hist: pd.DataFrame) -> dict:
    """从历史日K拟合模拟所需参数（GARCH + 跳跃 + 量价耦合）。"""
    close = hist["close"].to_numpy(dtype=float)
    lr = np.diff(np.log(close))
    vol = hist["volume"].to_numpy(dtype=float)
    lv = np.log(np.clip(vol, 1.0, None))[1:]  # 对齐 lr（len = len(close)-1）
    abs_lr = np.abs(lr)

    rf = (hist["high"].to_numpy() - hist["low"].to_numpy()) / close  # 区间因子
    gap = hist["open"].to_numpy()[1:] / close[:-1] - 1.0             # 跳空
    ac1 = float(np.corrcoef(lr[:-1], lr[1:])[0, 1]) if len(lr) > 2 else 0.0

    # 量-波耦合：OLS 斜率 + 残差标准差（用于还原真实耦合强度，而非被噪声淹没）
    if np.var(abs_lr) > 0:
        beta_vol = float(np.cov(abs_lr, lv)[0, 1] / np.var(abs_lr))
    else:
        beta_vol = 0.0
    resid_std = float(np.std(lv - beta_vol * abs_lr))

    # GARCH(1,1) 波动率
    omega, alpha, beta, sigma0_sq = fit_garch(lr)
    # 跳跃扩散
    lam, mu_j, sigma_j, sigma_diff, jump_mask = fit_jump(lr)
    # 跳跃日放量：log 量相对均值的额外增量（事件性放量）
    if jump_mask.sum() > 0:
        jump_vol_boost = float(np.mean(lv[jump_mask]) - np.mean(lv))
        mean_jump_mag = float(np.mean(np.abs(lr[jump_mask]))) or 1.0
    else:
        jump_vol_boost, mean_jump_mag = 0.0, 1.0

    return {
        "mu": float(np.mean(lr)),
        "garch_omega": omega, "garch_alpha": alpha, "garch_beta": beta,
        "sigma0_sq": sigma0_sq, "sigma_diff": sigma_diff,
        "jump_lam": lam, "jump_mu": mu_j, "jump_sigma": sigma_j,
        "mean_lv": float(np.mean(lv)), "beta_vol": beta_vol, "resid_std": resid_std,
        "jump_vol_boost": jump_vol_boost, "mean_jump_mag": mean_jump_mag,
        "mean_rf": float(np.mean(rf)), "std_rf": float(np.std(rf)),
        "gap_std": float(np.std(gap)), "ac1": ac1,
        "last_close": float(close[-1]),
    }


# --------------------------------------------------------------------------- #
# 模拟
# --------------------------------------------------------------------------- #
def simulate(fit: dict, n: int, m: int, seed: int) -> dict:
    """生成 m 条长度为 n 的 OHLCV 模拟路径。

    模型（第一轮增强）：
      - 波动率：GARCH(1,1) 条件方差（替代 EWMA，更贴合异方差与波动聚集）
      - 收益：Merton 跳跃扩散 = 漂移 + 扩散(GARCH) + 复合泊松跳跃
      - 成交量：log 量 = 均值 + β|收益| + 跳跃放量 + 残差噪声（量价耦合还原）
    """
    rng = np.random.default_rng(seed)
    closes = np.empty((m, n + 1))
    closes[:, 0] = fit["last_close"]
    opens = np.empty((m, n))
    highs = np.empty((m, n))
    lows = np.empty((m, n))
    vols = np.empty((m, n))

    omega, alpha, beta = fit["garch_omega"], fit["garch_alpha"], fit["garch_beta"]
    h = np.full(m, fit["sigma0_sq"])  # GARCH 条件方差初始
    lam, mu_j, sigma_j = fit["jump_lam"], fit["jump_mu"], fit["jump_sigma"]
    for i in range(n):
        sigma = np.sqrt(h)
        z = rng.normal(0.0, 1.0, m)
        diff = sigma * z                              # 扩散项
        jump = np.zeros(m)
        jump_days = rng.random(m) < lam               # 当日是否发生跳跃
        if jump_days.any() and sigma_j > 0:
            jump[jump_days] = rng.normal(mu_j, sigma_j, jump_days.sum())
        lr = fit["mu"] + diff + jump                  # 当日对数收益
        prev = closes[:, i]
        closes[:, i + 1] = prev * np.exp(lr)

        # 跳空开盘
        gap = rng.normal(0.0, fit["gap_std"], m)
        op = prev * (1.0 + gap)
        cl = closes[:, i + 1]
        # 区间展宽：以当日收盘价为基准还原 (high-low)/close ≈ rf
        rf = rng.lognormal(np.log(fit["mean_rf"]), fit["std_rf"], m)
        eta = rng.uniform(0.5, 1.5, m)
        spread = np.maximum(op, cl) - np.minimum(op, cl)
        extra = np.maximum(rf * cl - spread, 0.0) * 0.5 * eta
        hi = np.maximum(op, cl) + extra
        lo = np.minimum(op, cl) - extra
        lo = np.maximum(lo, 0.01 * cl)

        # 成交量：量随波动 + 跳跃放量（耦合）
        jump_mag = np.abs(jump)
        lv = (fit["mean_lv"] + fit["beta_vol"] * np.abs(lr)
              + fit["jump_vol_boost"] * (jump_mag / fit["mean_jump_mag"])
              + fit["resid_std"] * rng.normal(0.0, 1.0, m))
        vol = np.exp(lv)
        opens[:, i], highs[:, i], lows[:, i], vols[:, i] = op, hi, lo, vol

        # 更新 GARCH 条件方差（用扩散冲击，跳跃单独建模）
        eps = sigma * z
        h = omega + alpha * eps ** 2 + beta * h
    return {"close": closes[:, 1:], "open": opens, "high": highs,
            "low": lows, "volume": vols}


# --------------------------------------------------------------------------- #
# 统计与对比
# --------------------------------------------------------------------------- #
def path_stats(rets: np.ndarray) -> dict:
    """对一条日收益序列计算统计。"""
    cum = np.cumprod(1 + rets) - 1
    peak = np.maximum.accumulate(1 + rets)
    dd = (np.cumprod(1 + rets) - peak) / peak
    return {
        "net": float(cum[-1]),
        "vol": float(np.std(rets)),
        "max_dd": float(dd.min()),
        "up_ratio": float(np.mean(rets > 0)),
        "ac1": float(np.corrcoef(rets[:-1], rets[1:])[0, 1]) if len(rets) > 2 else 0.0,
    }


def run_length(rets: np.ndarray) -> float:
    """平均连涨/连跌长度（时间节奏代理）。"""
    signs = np.sign(rets)
    runs, cur = [], 0
    for s in signs:
        if cur == 0 or np.sign(cur) == s:
            cur += s
        else:
            runs.append(abs(cur))
            cur = s
    if cur != 0:
        runs.append(abs(cur))
    return float(np.mean(runs)) if runs else 0.0


def _maxdd(rets: np.ndarray) -> float:
    """由日收益序列计算最大回撤（负值）。"""
    eq = np.cumprod(1 + rets)
    peak = np.maximum.accumulate(eq)
    return float(((eq - peak) / peak).min())


def _safe_corr(x: np.ndarray, y: np.ndarray) -> float:
    """安全相关系数（任一方方差过小时返回 0）。"""
    if np.std(x) < 1e-12 or np.std(y) < 1e-12:
        return 0.0
    return float(np.corrcoef(x, y)[0, 1])


def analyze(actual: pd.DataFrame, sim: dict, fit: dict, code: str,
            start: str, n: int, m: int) -> str:
    """生成五维对比（逐路径统计）+ 缺陷分析 + 优化方向 的 Markdown 报告。

    关键方法学：模拟产生 m 条独立路径，先对每条路径计算指标，再取
    中位数与 5/95 分位带；真实值若落在带外即判为模拟缺陷，避免
    "单条中位数路径平滑掉随机性"导致的系统性低估。
    """
    a_close = actual["close"].to_numpy(float)
    a_high = actual["high"].to_numpy(float)
    a_low = actual["low"].to_numpy(float)
    a_vol = actual["volume"].to_numpy(float)
    a_ret = a_close[1:] / a_close[:-1] - 1

    s_close = sim["close"]                 # (m, n)
    s_high = sim["high"]
    s_low = sim["low"]
    s_vol = sim["volume"]
    s_ret = s_close[:, 1:] / s_close[:, :-1] - 1   # (m, n-1)

    # ---- 逐路径统计 ----
    net_p = s_close[:, -1] / s_close[:, 0] - 1
    vol_p = s_ret.std(axis=1)
    dd_p = np.array([_maxdd(s_ret[i]) for i in range(m)])
    rng_p = ((s_high - s_low) / s_close).mean(axis=1)
    vt_p = s_vol.sum(axis=1)
    corr_p = np.array([_safe_corr(np.abs(s_ret[i]), np.log(s_vol[i, 1:])) for i in range(m)])
    ac1_p = np.array([_safe_corr(s_ret[i, :-1], s_ret[i, 1:]) for i in range(m)])
    run_p = np.array([run_length(s_ret[i]) for i in range(m)])

    med = lambda x: float(np.median(x))
    p05 = lambda x: float(np.percentile(x, 5))
    p95 = lambda x: float(np.percentile(x, 95))

    # ---- 真实指标 ----
    a_net = a_close[-1] / a_close[0] - 1
    a_vol_real = float(np.std(a_ret))
    a_dd = _maxdd(a_ret)
    a_rng = float(np.mean((a_high - a_low) / a_close))
    a_vt = float(np.sum(a_vol))
    a_corr = _safe_corr(np.abs(a_ret), np.log(a_vol[1:]))
    a_ac1 = _safe_corr(a_ret[:-1], a_ret[1:])
    a_run = run_length(a_ret)

    # ---- 模拟分布（中位 + 分位带）----
    net_m, net_05, net_95 = med(net_p), p05(net_p), p95(net_p)
    vol_m, vol_05, vol_95 = med(vol_p), p05(vol_p), p95(vol_p)
    dd_m, dd_05, dd_95 = med(dd_p), p05(dd_p), p95(dd_p)
    rng_m = med(rng_p)
    vt_m, vt_05, vt_95 = med(vt_p), p05(vt_p), p95(vt_p)
    corr_m = med(corr_p)
    ac1_m, ac1_05, ac1_95 = med(ac1_p), p05(ac1_p), p95(ac1_p)
    run_m, run_05, run_95 = med(run_p), p05(run_p), p95(run_p)

    # 方向相关：真实日收益 vs 模拟平均日收益
    dir_corr = _safe_corr(a_ret, s_ret.mean(axis=0))
    end_05 = float(np.percentile(s_close[:, -1], 5))
    end_50 = float(np.percentile(s_close[:, -1], 50))
    end_95 = float(np.percentile(s_close[:, -1], 95))
    in_band = bool(end_05 <= a_close[-1] <= end_95)
    a_end = float(a_close[-1])
    a_max = float(np.max(a_high)); a_min = float(np.min(a_low))
    band_hi = float(np.percentile(np.max(s_high, axis=1), 95))
    band_lo = float(np.percentile(np.min(s_low, axis=1), 5))

    def pct(x): return f"{x*100:.2f}%"
    ok = "✅"; bad = "⚠️"

    # ---- 缺陷判定：真实是否落在模拟分位带外 ----
    deficits = []
    if not (net_05 <= a_net <= net_95):
        deficits.append(f"趋势形态：真实净收益 {pct(a_net)} 落在模拟 5–95% 带 [{pct(net_05)}, {pct(net_95)}] 之外，"
                        f"GBM 无趋势持续/反转结构，方向相关仅 {dir_corr:.2f}（仅靠漂移无法复现趋势市）。")
    if not (vol_05 <= a_vol_real <= vol_95):
        deficits.append(f"波动幅度：真实已实现波动 {pct(a_vol_real)} 落在模拟带 [{pct(vol_05)}, {pct(vol_95)}] 之外，"
                        f"正态+GARCH(1,1)+跳跃扩散仍系统性{'高估' if a_vol_real < vol_05 else '低估'}波动/肥尾，"
                        f"真实最大回撤 {pct(a_dd)} vs 模拟带 [{pct(dd_05)}, {pct(dd_95)}]。")
    if not in_band:
        deficits.append(f"关键价位：真实终点 {a_end:.2f} 落在模拟 5–95% 终点带 "
                        f"[{end_05:.2f}, {end_95:.2f}] 之外（支撑/阻力、趋势突破未建模）。")
    if not (vt_05 <= a_vt <= vt_95):
        deficits.append(f"成交量变化：真实总量 {a_vt/1e8:.2f}亿股 落在模拟带 [{vt_05/1e8:.2f}, {vt_95/1e8:.2f}]亿股 之外，"
                        f"量-波相关真实 {a_corr:.2f} vs 模拟中位 {corr_m:.2f}，事件性放量（消息/涨停）缺失。")
    if not (ac1_05 <= a_ac1 <= ac1_95) or not (run_05 <= a_run <= run_95):
        deficits.append(f"时间节奏：真实收益自相关 {a_ac1:.2f}/连涨连跌均长 {a_run:.2f} 落在模拟带 "
                        f"[{ac1_05:.2f},{ac1_95:.2f}]/[{run_05:.2f},{run_95:.2f}] 之外，"
                        f"iid 假设抹平了动量/反转与日历效应。")
    if not deficits:
        deficits.append("五维均未触发显著偏差阈值（但模拟器仍属简化原型，见优化方向）。")

    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    md = f"""# 日K演化模拟对比报告 (v0.2 — GARCH+跳跃扩散+量价耦合)

> code={code} | 起始={start} | 模拟窗口={n}交易日 | 路径数={m} | 拟合历史≥120日
> 模拟器：v0.2（GARCH(1,1) 波动率 + Merton 跳跃扩散 + 量价耦合，首轮增强）
> 生成时间：{ts}

## 📊 核心指标（真实 vs 模拟逐路径中位 [5–95%带]）

| 维度 | 真实 | 模拟(中位) | 模拟带[5%,95%] | 判定 |
|---|---:|---:|---:|---|
| 区间净收益 | {pct(a_net)} | {pct(net_m)} | [{pct(net_05)}, {pct(net_95)}] | {ok if net_05<=a_net<=net_95 else bad} |
| 方向相关(累计) | — | {dir_corr:.2f} | — | 形态拟合度 |
| 已实现波动率 | {pct(a_vol_real)} | {pct(vol_m)} | [{pct(vol_05)}, {pct(vol_95)}] | {ok if vol_05<=a_vol_real<=vol_95 else bad} |
| 最大回撤 | {pct(a_dd)} | {pct(dd_m)} | [{pct(dd_05)}, {pct(dd_95)}] | {ok if dd_05<=a_dd<=dd_95 else bad} |
| 日均振幅 | {pct(a_rng)} | {pct(rng_m)} | — | {'≈' if abs(a_rng-rng_m)<0.01 else bad} |
| 成交量总量 | {a_vt/1e8:.2f}亿股 | {vt_m/1e8:.2f}亿股 | [{vt_05/1e8:.2f}, {vt_95/1e8:.2f}] | {ok if vt_05<=a_vt<=vt_95 else bad} |
| 量-波相关 | {a_corr:.2f} | {corr_m:.2f} | — | {'≈' if abs(a_corr-corr_m)<0.2 else bad} |
| 收益自相关lag1 | {a_ac1:.2f} | {ac1_m:.2f} | [{ac1_05:.2f}, {ac1_95:.2f}] | {ok if ac1_05<=a_ac1<=ac1_95 else bad} |
| 连涨连跌均长 | {a_run:.2f} | {run_m:.2f} | [{run_05:.2f}, {run_95:.2f}] | {ok if run_05<=a_run<=run_95 else bad} |
| 终点价落入5-95%带 | {'是' if in_band else '否'} | — | — | {ok if in_band else bad} |

## 🎯 关键价位对照

- 真实终点：**{a_end:.2f}**（模拟带 [{end_05:.2f}, {end_95:.2f}]）
- 真实区间极值：高 {a_max:.2f} / 低 {a_min:.2f}
- 模拟区间极值带（95/5分位）：高≤{band_hi:.2f} / 低≥{band_lo:.2f}

## 📈 缺陷分析（五维）

"""
    for d in deficits:
        md += f"- {d}\n"

    md += """
## ⚠️ 风险提示

1. 模拟器仅在 T0 前历史拟合，属"样本内拟合+前向生成"，不代表真实分布；
2. 单标的、单窗口结论不具普适性，须多标的多时段复现；
3. 真实市场含政策/消息跳空，随机过程天然低估黑天鹅；
4. 本报告用于"数据/模型校验"，非交易信号。

## 🔧 优化方向（后续修改与验证）

> 第一轮已落地：① 跳跃扩散（Merton）② GARCH(1,1) 替代 EWMA ③ 量价耦合还原（OLS β + 残差噪声 + 跳跃放量）。

1. ~~**肥尾与跳跃**：Merton 跳跃扩散已接入（λ/μ_j/σ_j 由历史极端日拟合）~~ ✅已实现；
2. ~~**波动率模型**：GARCH(1,1) 已替代 EWMA~~ ✅已实现；
3. **趋势/状态切换**：引入 HMM 或 Markov 机制切换（牛/熊/震荡），分别建模 drift 与 vol；
4. ~~**量价反馈**：量随波动 + 跳跃放量已耦合（复现真实量-波相关）~~ ✅已实现；
5. **时间节奏**：加入收益 AR(1) 动量项、跳空模型、星期/月份日历效应；
6. **区间结构**：最高/最低改用条件异方差模型，而非常数区间因子；
7. **逐股校准**：参数按个股滚动窗口自适应，而非全局固定；
8. **验证协议**：walk-forward（[t-k,t) 拟合→[t,t+h) 验证，滚动推进）+ 多标的自助置信区间；
9. **评分指标**：用 CRPS / 分位损失等概率评分，替代单点对比。
"""
    return md


def main() -> None:
    ap = argparse.ArgumentParser(description="日K演化模拟 vs 真实对比")
    ap.add_argument("--code", required=True, help="6位代码（自动加 sh/sz/bj 前缀）")
    ap.add_argument("--start", required=True, help="起始交易日 YYYY-MM-DD")
    ap.add_argument("--days", type=int, default=60, help="模拟交易日数")
    ap.add_argument("--n-sim", type=int, default=200, help="模拟路径数")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    _, hist, fut = load_and_split(args.code, args.start, args.days)
    fit = fit_params(hist)
    sim = simulate(fit, args.days, args.n_sim, args.seed)
    md = analyze(fut, sim, fit, args.code, args.start, args.days, args.n_sim)

    fp = OUT_DIR / f"sim_compare_{args.code}_{args.start}_{ts}.md"
    fp.write_text(md, encoding="utf-8")
    log.info("报告已写出: %s", fp)
    print(md)


if __name__ == "__main__":
    main()
