"""P2-5 变分法 → 最优执行（Almgren-Chriss）。

PoC 模块，已接入 `scripts/plan_e_journal.py`：每日信号触发时给出日内拆单建议、
周报增加「成交拆单建议」章节（`suggest_execution`）。

最小化「期望执行成本 + λ·执行风险」的变分问题，给出闭式最优执行轨迹。

模型（Almgren & Chriss 2000）：设剩余持仓 x(t)（股），成交速率 v(t)=−ẋ(t)，
总股数 X=x(0)，x(T)=0。目标泛函（L² 内积取样本/连续积分）

    J[x] = E[cost] + λ·Var[cost]
         = (γ/2) ∫₀ᵀ ẋ(t)² dt          # 暂时冲击（二次）期望成本
         +   η   ∫₀ᵀ x(t)  dt          # 永久冲击 / 漂移（期望，可正可负）
         + λ·σ² ∫₀ᵀ x(t)² dt          # 执行风险：持库存货暴露于市场噪声 σ dW

Euler-Lagrange ⇒ ẍ − κ²x = η/γ，κ = σ·√(λ/γ)。闭式：

    x(t) = A·cosh(κt) + B·sinh(κt) + η/(λσ²)      (λ>0)
    A = X − η/(λσ²),  B = −(A·cosh(κT) + η/(λσ²))/sinh(κT)

λ→0（无风险惩罚）退化为**匀速执行** x(t)=X(1−t/T)；λ→∞ 退化为**瞬时执行** x→X·δ(t)。
η=0 时为经典 AC 双曲轨迹 x(t)=X·sinh(κ(T−t))/sinh(κT)。

γ 与 cost_model.impact_gamma 的数量级对应（暂时冲击强度）；σ 取标的日波动；
λ 由「愿为多小风险付多少额外冲击成本」决定。详见 ``ac_params_from_cost``。
"""
from __future__ import annotations

import logging

import numpy as np

logger = logging.getLogger(__name__)


def _inventory_and_rate(t: np.ndarray, X: float, T: float,
                        gamma: float, sigma: float, lam: float, eta: float):
    """返回 (x(t), v(t)=−ẋ(t)) 闭式轨迹（λ=0 单独处理）。κT 较大时用指数形式避免溢出。"""
    if lam <= 0.0 or gamma <= 0.0:
        # 无风险惩罚：匀速 + 永久冲击二次项
        c1 = -(X + (eta / (2.0 * gamma)) * T * T) / T if gamma > 0 else -X / T
        c2 = X
        x = (eta / (2.0 * gamma)) * t * t + c1 * t + c2 if gamma > 0 else X * (1.0 - t / T)
        dx = (eta / gamma) * t + c1 if gamma > 0 else -X / T
        return x, -dx
    kappa = sigma * np.sqrt(lam / gamma)
    C0 = eta / (lam * sigma * sigma)
    A = X - C0
    B = -(A * np.cosh(kappa * T) + C0) / np.sinh(kappa * T) if kappa * T > 1e-9 else 0.0
    kT = kappa * T
    if kT <= 50.0:
        x = A * np.cosh(kappa * t) + B * np.sinh(kappa * t) + C0
        dx = A * kappa * np.sinh(kappa * t) + B * kappa * np.cosh(kappa * t)
    else:
        # 大 κT：cosh/sinh 溢出，改用指数形式（缩放 e^{-κT} 保证各项 ≲1）
        etT = np.exp(kappa * (t - T))
        emT = np.exp(-kappa * (t + T))
        x = ((A + B) * etT + (A - B) * emT) / 2.0 + C0
        dx = kappa * ((A + B) * etT - (A - B) * emT) / 2.0
    return x, -dx


def _integrals(x: np.ndarray, dx: np.ndarray, t: np.ndarray,
               gamma: float, sigma: float, lam: float, eta: float):
    """数值积分各项成本/风险（梯形法）。"""
    temp = (gamma / 2.0) * np.trapezoid(dx * dx, t) if gamma > 0 else 0.0
    perm = eta * np.trapezoid(x, t) if eta != 0.0 else 0.0
    var = sigma * sigma * np.trapezoid(x * x, t)
    expected = temp + perm
    objective = expected + lam * var
    return {"expected_cost": float(expected), "temp_cost": float(temp),
            "perm_cost": float(perm), "variance": float(var),
            "objective": float(objective)}


def optimal_execution(X: float, T: float, gamma: float, sigma: float, lam: float,
                      eta: float = 0.0, n: int = 400) -> dict:
    """求解最优执行轨迹并回报成本/风险（对比匀速基准）。

    参数：
        X：总执行股数（>0）；T：执行周期（时间单位，如「天」）；
        gamma：暂时冲击系数（同 cost_model.impact_gamma 量级）；
        sigma：标的日价格波动（价格变动/√时间）；
        lam：风险厌恶（>0 才偏离匀速）；
        eta：永久冲击/漂移（默认 0）；
        n：数值积分网格点数。

    返回 dict：t / inventory / rate / expected_cost / variance / objective，
    以及 uniform_* 基准（匀速执行）各项，便于评估「最优相比匀速省了多少」。
    """
    if X <= 0 or T <= 0 or sigma < 0:
        raise ValueError("X,T 必须 >0；sigma ≥ 0")
    if gamma < 0 or lam < 0:
        raise ValueError("gamma, lam 必须 ≥ 0")
    t = np.linspace(0.0, T, n)
    x, v = _inventory_and_rate(t, X, T, gamma, sigma, lam, eta)
    metrics = _integrals(x, v, t, gamma, sigma, lam, eta)
    # 匀速基准
    xu = X * (1.0 - t / T)
    vu = np.full_like(t, -X / T)
    uni = _integrals(xu, vu, t, gamma, sigma, lam, eta)
    out = {
        "t": t, "inventory": x, "rate": v,
        "expected_cost": metrics["expected_cost"],
        "temp_cost": metrics["temp_cost"],
        "perm_cost": metrics["perm_cost"],
        "variance": metrics["variance"],
        "objective": metrics["objective"],
        "per_share_cost": metrics["expected_cost"] / X if X > 0 else float("nan"),
        "uniform_expected_cost": uni["expected_cost"],
        "uniform_variance": uni["variance"],
        "uniform_objective": uni["objective"],
        "uniform_per_share_cost": uni["expected_cost"] / X if X > 0 else float("nan"),
    }
    # 自检：库存非负、末端归零、单调不增（数值容差）
    out["_check_nonneg"] = bool(np.all(x >= -1e-6))
    out["_check_end_zero"] = bool(abs(x[-1]) <= 1e-6 * max(1.0, abs(X)))
    out["_check_monotone"] = bool(np.all(np.diff(x) <= 1e-6))
    return out


def ac_params_from_cost(cfg, adv_shares: float, T_days: float = 1.0) -> dict:
    """由 cost_model.CostConfig 粗略推导 AC 参数（PoC 用，供把冲击成本模型接到目标项）。

    - gamma：暂时冲击强度 ∝ impact_gamma·√(τ/ADV)，τ=单时间单位长度（天），ADV=日均成交量。
      直觉：参与率 pr≈(v·τ)/ADV，sqrt 冲击 cost≈impact_gamma·√pr·notional，
      对应二次暂时冲击 γ/2·v² 中的 γ≈impact_gamma·τ/ADV（对股数/时间量纲）。
    - sigma：调用方应传入标的日波动（此处无法从 CostConfig 获知，留待外部）。
    - lam：风险厌恶，需按组合风险预算给定（此处不推导）。
    """
    tau = T_days
    gamma = cfg.impact_gamma * (tau / adv_shares) if adv_shares > 0 else cfg.impact_gamma
    return {"gamma": float(gamma), "adv_shares": float(adv_shares), "T_days": float(T_days)}


def calibrate_gamma_ac(price: float, X: float, adv_shares: float, T_days: float = 1.0,
                       impact_gamma: float = 0.1) -> float:
    """按「总参与率」把成本模型的 sqrt 冲击系数折算成 AC 二次暂时冲击系数 γ。

    令 AC 匀速执行的暂时冲击成本占比 ≈ 成本模型 sqrt 冲击占比：

        (γ/2)·(X / (T·price)) = impact_gamma·√(X / (ADV·T))

    ⇒ γ = 2·impact_gamma·√(pr_avg)·T·price / X，pr_avg = X/(ADV·T)。

    该标度使 γ 随「单子相对 ADV 的大小」单调：小单→γ 大→近似匀速（TWAP）；
    大单 / 高波动→γ 小→κT 大→前置成交（front-load）。与实盘直觉一致。
    """
    if adv_shares <= 0 or X <= 0 or T_days <= 0 or price <= 0:
        return float(impact_gamma)
    pr_avg = X / (adv_shares * T_days)
    gamma = 2.0 * impact_gamma * np.sqrt(max(pr_avg, 0.0)) * (T_days * price) / X
    return float(gamma)


def _intraday_clock(frac: float) -> str:
    """把 [0,1] 的日内连续比例（240 分钟交易时段）映射成 A 股时钟 HH:MM（跳过午休）。"""
    m = frac * 240.0
    total = 9 * 60 + 30 + (m if m <= 120.0 else 210.0 + (m - 120.0))  # 13:00 = 9:30 + 210 分钟墙钟
    h = int(total // 60)
    mm = round(total - h * 60)
    if mm == 60:
        h += 1
        mm = 0
    return f"{h:02d}:{mm:02d}"


def _bucket_labels(edges: np.ndarray, T_days: float) -> list[str]:
    if abs(T_days - 1.0) < 1e-9:
        return [f"{_intraday_clock(edges[j])}-{_intraday_clock(edges[j + 1])}"
                for j in range(len(edges) - 1)]
    n = len(edges) - 1
    return [f"第{j + 1}/{n}段" for j in range(n)]


def execution_table_markdown(sched: dict) -> str:
    """把 suggest_execution 的拆单 schedule 渲染为 Markdown 表。"""
    rows = ["| 时段 | 成交股数 | 占比 | 参与率 |", "| ---: | ---: | ---: | ---: |"]
    for b in sched["schedule"]:
        rows.append(
            f"| {b['label']} | {b['shares']:,.0f} | {b['pct']:.1f}% | {b['participation'] * 100:.1f}% |"
        )
    return "\n".join(rows)


def suggest_execution(qty: float, price: float, adv_shares: float, daily_vol: float,
                      *, T_days: float = 1.0, lam: float = 1.0, n_buckets: int = 6,
                      cost_cfg=None, eta: float = 0.0) -> dict:
    """为「一笔 qty 股、限价参考 price 的订单」给出 Almgren-Chriss 最优执行拆单建议。

    参数：
        qty：执行股数（>0，买/卖同此绝对值）；price：参考价（元）；
        adv_shares：标的近 20 日日均成交量（股）；daily_vol：标的日收益率标准差；
        T_days：执行周期（1.0=日内拆单，>1=跨多日拆单）；
        lam：风险厌恶（默认 1.0）；n_buckets：拆成几段；
        cost_cfg：cost_model.CostConfig（取 impact_gamma）；eta：永久冲击（默认 0）。

    返回 dict：gamma/sigma/adv_shares、per_share_cost（最优）与 uniform_per_share_cost、
    cost_save_pct（相对匀速省的成本占比）、var_reduce_pct（相对匀速降的风险占比）、
    front_load_pct（前 1/3 段成交占比）、schedule（每段 label/股数/占比/参与率）、
    recommendation（自然语言建议）。
    """
    if cost_cfg is None:
        from quant.multifactor.cost_model import CostConfig
        cost_cfg = CostConfig()
    X = float(qty)
    if X <= 0 or adv_shares <= 0 or daily_vol <= 0:
        raise ValueError("qty / adv_shares / daily_vol 必须 > 0")
    sigma = float(daily_vol)
    gamma = calibrate_gamma_ac(price, X, adv_shares, T_days, cost_cfg.impact_gamma)
    r = optimal_execution(X, T_days, gamma, sigma, lam, eta=eta, n=400)

    edges = np.linspace(0.0, T_days, n_buckets + 1)
    x_edges, _ = _inventory_and_rate(edges, X, T_days, gamma, sigma, lam, eta)
    shares = np.clip(np.diff(x_edges) * -1.0, 0.0, None)
    if shares.sum() > 0:
        shares = shares * (X / shares.sum())  # 保和为 X（数值容差）
    adv_per_bucket = adv_shares * (T_days / n_buckets)
    participation = shares / adv_per_bucket if adv_per_bucket > 0 else np.zeros_like(shares)

    labels = _bucket_labels(edges, T_days)
    schedule = [
        {"bucket": j + 1, "label": labels[j], "shares": float(shares[j]),
         "pct": float(shares[j] / X * 100.0), "participation": float(participation[j])}
        for j in range(n_buckets)
    ]

    uni_obj = r["uniform_objective"]
    objective_save_pct = (uni_obj - r["objective"]) / uni_obj * 100.0 if uni_obj > 0 else 0.0
    uni_var = r["uniform_variance"]
    var_reduce_pct = (uni_var - r["variance"]) / uni_var * 100.0 if uni_var > 0 else 0.0
    third = max(1, n_buckets // 3)
    front_load_pct = float(shares[:third].sum() / X * 100.0)
    twap_front_pct = 100.0 / n_buckets * third
    max_part = float(participation.max()) if participation.size else 0.0

    recommendations = []
    if max_part > 0.20:
        recommendations.append(
            f"⚠️ 单笔参与率峰值 {max_part * 100:.0f}% 偏高，建议进一步拆细或择机成交以抑制冲击成本；"
        )
    if front_load_pct > twap_front_pct + 10.0 and var_reduce_pct > 2.0:
        recommendations.append(
            f"建议前置成交（front-load）：高波动 / 大单相对 ADV，集中前半段执行以降低库存暴露。"
            f"相对匀速风险调整后成本省 {objective_save_pct:.1f}%、执行风险降 {var_reduce_pct:.1f}%。"
        )
    else:
        recommendations.append(
            f"建议匀速（TWAP）或按流动性择机：单子相对 ADV 较小，冲击/风险均低，无需刻意前置"
            f"（前 1/3 段占比 {front_load_pct:.0f}% vs 匀速 {twap_front_pct:.0f}%）。"
        )

    return {
        "qty": X, "price": float(price), "notional": X * float(price),
        "T_days": float(T_days), "lam": float(lam), "gamma": gamma,
        "sigma": sigma, "adv_shares": float(adv_shares),
        "per_share_cost": r["per_share_cost"],
        "uniform_per_share_cost": r["uniform_per_share_cost"],
        "cost_save_pct": float(objective_save_pct),
        "variance": r["variance"], "uniform_variance": uni_var,
        "var_reduce_pct": float(var_reduce_pct),
        "front_load_pct": front_load_pct, "twap_front_pct": float(twap_front_pct),
        "schedule": schedule,
        "recommendation": " ".join(recommendations),
    }
