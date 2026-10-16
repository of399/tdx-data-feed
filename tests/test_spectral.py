"""P2-3 FFT 频谱 PoC 单测：周期检测、频域低通、置换检验证伪。"""
from __future__ import annotations

import numpy as np

from quant.multifactor.spectral import (
    detect_dominant_periods,
    fft_periodogram,
    lowpass_filter,
    permutation_test_spectral,
)


def test_fft_periodogram_positive():
    periods, mag = fft_periodogram(np.sin(2 * np.pi * np.arange(100) / 10))
    assert periods.size > 0
    assert np.all(mag >= 0)


def test_detect_periodic_signal():
    # 400 点、周期 20 交易日的纯正弦 → 应检出周期 ≈ 20
    t = np.arange(400)
    y = np.sin(2 * np.pi * t / 20.0)
    res = detect_dominant_periods(y, top_k=1)
    assert res, "应检测到至少一个占优周期"
    period = res[0][0]
    assert abs(period - 20.0) <= 2.0, f"检出周期 {period} 偏离 20"


def test_permutation_rejects_white_noise():
    # 白噪声不应被判出显著周期（证伪成功，p 不显著）
    rng = np.random.default_rng(1)
    y = rng.standard_normal(300)
    res = permutation_test_spectral(y, n_perm=100, seed=1)
    assert res["p_value"] > 0.05, f"白噪声 p={res['p_value']} 应 >0.05（不显著）"


def test_permutation_supports_real_period():
    # 含周期信号 + 轻噪声 → 通过置换检验（p 显著）
    rng = np.random.default_rng(2)
    t = np.arange(400)
    y = np.sin(2 * np.pi * t / 20.0) + 0.15 * rng.standard_normal(400)
    res = permutation_test_spectral(y, n_perm=100, seed=2)
    assert res["p_value"] < 0.05, f"真实周期 p={res['p_value']} 应 <0.05（显著）"
    assert abs(res["observed_period"] - 20.0) <= 3.0


def test_lowpass_removes_high_freq():
    # 周期 20 信号 + 周期 3 高频噪声；低通(cutoff=10) 应保留周期 20、剔除周期 3
    rng = np.random.default_rng(3)
    t = np.arange(400)
    clean = np.sin(2 * np.pi * t / 20.0)
    y = clean + 0.5 * np.sin(2 * np.pi * t / 3.0) + 0.1 * rng.standard_normal(400)
    filt = lowpass_filter(y, cutoff_period=10)
    corr = np.corrcoef(filt, clean)[0, 1]
    assert corr > 0.9, f"低通后与纯周期20信号相关 {corr} 应 >0.9"
