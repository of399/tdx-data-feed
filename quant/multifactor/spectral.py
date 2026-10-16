"""P2-3 频域视角（FFT 频谱）PoC —— 不接主链路。

提供三类工具，作为截面 z-score（3σ winsorize）之外的**去噪 / 周期探查**补充：

1. FFT 周期检测 ``detect_dominant_periods``：从收益/价格序列的实谱中提取占优周期（天）。
2. 频域低通滤波 ``lowpass_filter``：在频率域零化高频分量，作为 3σ winsorize 的频域补刀去噪。
3. **置换检验证伪** ``permutation_test_spectral``：金融序列低信噪比，纯 FFT 峰值极易把噪声误读为
   周期。随机打乱时序重算频谱，比较「观测峰值强度」与「打乱后峰值强度分布」，给出经验 p-value。
   只有 p 显著小（观测峰值强于绝大多数置换）的周期才可信；白噪声序列应被判为不显著（证伪成功）。

全部为纯 numpy 实现；周期为「交易日」单位。
"""
from __future__ import annotations

import logging

import numpy as np

logger = logging.getLogger(__name__)


def _detrended(y: np.ndarray) -> np.ndarray:
    """去均值（收益序列本就 ~0 均值；价格序列去趋势避免 DC 泄漏到极低频）。"""
    y = np.asarray(y, dtype=float)
    y = y[np.isfinite(y)]
    if y.size == 0:
        return y
    return y - np.mean(y)


def fft_periodogram(y: np.ndarray, window: bool = True) -> tuple[np.ndarray, np.ndarray]:
    """返回 (periods, power)。

    periods：正频对应的周期（交易日），跳过 DC（周期=inf）。
    power：对应频率的 |FFT| 幅度（应用 Hann 窗以减少频谱泄漏，window=True）。
    """
    y = _detrended(y)
    n = y.size
    if n < 4:
        return np.array([]), np.array([])
    if window:
        w = np.hanning(n)
        yw = y * w
    else:
        yw = y
    spec = np.fft.rfft(yw)
    mag = np.abs(spec)
    freqs = np.fft.rfftfreq(n, d=1.0)
    with np.errstate(divide="ignore", invalid="ignore"):
        periods = np.where(freqs > 0, 1.0 / freqs, np.inf)
    return periods, mag


def detect_dominant_periods(y: np.ndarray, top_k: int = 3,
                            min_period: float = 2.0,
                            max_period: float = 252.0) -> list[tuple[float, float]]:
    """从频谱中提取占优周期及其归一化强度（mag/Σmag，可解释为该周期占总谱能量的比例）。

    返回 list[(period_days, normalized_power)]，按强度降序，最多 top_k 个。仅保留
    [min_period, max_period] 区间内的局部极大值（避免相邻频点刷榜）。
    """
    periods, mag = fft_periodogram(y, window=True)
    if periods.size == 0:
        return []
    total = mag.sum()
    if total <= 0:
        return []
    mask = (periods >= min_period) & (periods <= max_period)
    idx = np.where(mask)[0]
    if idx.size == 0:
        return []
    m = mag[idx]
    # 局部极大值（端点放宽）
    local = []
    for j, i in enumerate(idx):
        left = idx[j - 1] if j > 0 else i
        right = idx[j + 1] if j < idx.size - 1 else i
        if m[j] >= mag[left] and m[j] >= mag[right]:
            local.append(i)
    local = np.array(local, dtype=int)
    if local.size == 0:
        local = idx[np.argmax(m)][None]
    powers = mag[local] / total
    order = np.argsort(-powers)[:top_k]
    return [(float(periods[local[o]]), float(powers[o])) for o in order]


def lowpass_filter(y: np.ndarray, cutoff_period: float) -> np.ndarray:
    """频域低通：零化周期 < cutoff_period（即频率 > 1/cutoff_period）的分量，逆变换返回实部。

    等价在频域做理想矩形窗；用于剔除高频噪声。返回与输入等长、实数、去趋势后的重构序列
    （未加回均值——调用方按需处理）。
    """
    y = _detrended(y)
    n = y.size
    if n < 4:
        return y.copy()
    cutoff_freq = 1.0 / max(cutoff_period, 1e-9)
    spec = np.fft.rfft(y)
    freqs = np.fft.rfftfreq(n, d=1.0)
    spec[freqs > cutoff_freq] = 0.0
    recon = np.fft.irfft(spec, n=n)
    return recon


def permutation_test_spectral(y: np.ndarray, n_perm: int = 200, top_k: int = 1,
                              min_period: float = 2.0, max_period: float = 252.0,
                              seed: int = 0) -> dict:
    """置换检验证伪：观测占优周期的归一化强度是否强于「随机打乱时序」后重算的强度。

    方法：对时序做 n_perm 次随机置换（破坏时间依赖、保留边际分布），每次重算
    ``detect_dominant_periods(top_k=1)`` 的归一化强度；经验 p-value =
    mean(permuted_strength >= observed_strength)。

    解释：
    - p 显著小（如 <0.05）→ 观测周期强于绝大多数随机序列，**通过证伪**、可信。
    - p 大（如 >0.05）→ 观测峰值与噪声无异，**证伪失败**，应视为伪周期、不可采信。

    返回 dict：observed_period / observed_power / perm_mean / perm_std / p_value / n_perm。
    """
    y = np.asarray(y, dtype=float)
    y = y[np.isfinite(y)]
    if y.size < 8:
        return {"observed_period": float("nan"), "observed_power": float("nan"),
                "perm_mean": float("nan"), "perm_std": float("nan"),
                "p_value": float("nan"), "n_perm": n_perm}
    obs = detect_dominant_periods(y, top_k=top_k, min_period=min_period, max_period=max_period)
    obs_period = obs[0][0] if obs else float("nan")
    obs_power = obs[0][1] if obs else 0.0
    rng = np.random.default_rng(seed)
    perm_powers = np.empty(n_perm, dtype=float)
    for i in range(n_perm):
        perm = rng.permutation(y)
        p = detect_dominant_periods(perm, top_k=top_k, min_period=min_period, max_period=max_period)
        perm_powers[i] = p[0][1] if p else 0.0
    p_value = float(np.mean(perm_powers >= obs_power)) if perm_powers.size else float("nan")
    return {
        "observed_period": float(obs_period),
        "observed_power": float(obs_power),
        "perm_mean": float(np.mean(perm_powers)),
        "perm_std": float(np.std(perm_powers)),
        "p_value": p_value,
        "n_perm": n_perm,
    }
