#!/usr/bin/env python3
"""逐事件特征聚类自动归类启动前形态（任务②）。

对 double_30d_pre90_features.csv 的 per-event 量价特征做 KMeans 聚类，
自动归类启动前 90 日形态为：次新连板 / 平台突破 / N字底 / 上涨中继。

方法：
  1. 次新(new_listing) 直接规则归类为「次新连板」（置信度置 1.0）。
  2. 其余有效事件对 [pre_ret90, pre_maxdd, pre_ann_vol, pre_amt_ratio, pre_vol_ratio]
     做 StandardScaler + 高斯混合 GMM(k=3)，按质心轮廓解释为上述三类之一：
       - 最高 mean(ret90)            → 上涨中继
       - 余下中 mean(maxdd) 最深     → N字底（深跌后回升）
       - 余下另一个                  → 平台突破（窄幅蓄势+近端放量）
     每条样本输出 cluster_label + cluster_confidence（GMM 后验概率最大值）。
  3. 输出带 cluster_label / cluster_confidence 的全量 CSV + 聚类报告。

用法：
  venv/bin/python scripts/cluster_pre90_patterns.py
"""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.metrics import silhouette_score
from sklearn.mixture import GaussianMixture
from sklearn.preprocessing import StandardScaler

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('cluster_pre90')

ROOT = Path(__file__).parent.parent
FEATURES = ROOT / 'data' / 'double_up' / 'double_30d_pre90_features.csv'
OUT = ROOT / 'data' / 'double_up'

FEAT_COLS = ['pre_ret90', 'pre_maxdd', 'pre_ann_vol', 'pre_amt_ratio', 'pre_vol_ratio']
K = 3
RANDOM_STATE = 42


def assign_labels(centers_df: pd.DataFrame) -> dict[int, str]:
    """按质心轮廓把 3 个簇映射为语义标签。

    规则：ret90 最高 → 上涨中继；余下两个中 maxdd 最深 → N字底；另一个 → 平台突破。
    """
    order_ret = centers_df['pre_ret90'].sort_values(ascending=False).index.tolist()
    up = order_ret[0]
    rest = order_ret[1:]
    dd_rest = centers_df.loc[rest, 'pre_maxdd']
    n_bottom = dd_rest.idxmin()          # maxdd 最深（最负）
    platform = next(c for c in rest if c != n_bottom)
    return {int(up): '上涨中继', int(n_bottom): 'N字底', int(platform): '平台突破'}


def main() -> None:
    log.info('=== 启动前形态聚类自动归类 ===')
    feat = pd.read_csv(FEATURES)
    total = len(feat)

    # 标注列（object，初始空串）
    lab = pd.Series('', index=feat.index, dtype=object)

    # 1) 次新且有前窗数据 → 次新连板（规则）；n==0（无数据）的次新留待标"数据缺失"
    is_new = feat['new_listing'].fillna(False) & feat[FEAT_COLS].notna().all(axis=1)
    lab[is_new] = '次新连板'

    # 2) 非次新、非数据缺口、特征完整 → 聚类
    mask = (~feat['new_listing'].fillna(False)) & (~feat['data_gap'].fillna(False)) \
        & feat[FEAT_COLS].notna().all(axis=1)
    sub = feat[mask].copy()
    sub_orig = sub.copy()   # 保留原始值用于质心展示（比率列先缩尾）
    log.info('聚类候选(非次新且非缺口且特征完整): %d / %d', len(sub), total)

    # 比率特征含极端离群点（如量能比 90×），标准化后会单独拉成一个失真簇；
    # 对 pre_amt_ratio / pre_vol_ratio 做 1%/99% 缩尾（winsorize）后再标准化
    RATIO_COLS = ['pre_amt_ratio', 'pre_vol_ratio']
    for c in RATIO_COLS:
        lo, hi = sub[c].quantile(0.01), sub[c].quantile(0.99)
        sub[c] = sub[c].clip(lo, hi)

    X = sub[FEAT_COLS].to_numpy(dtype=float)
    Xs = StandardScaler().fit_transform(X)
    gmm = GaussianMixture(n_components=K, covariance_type='full',
                          random_state=RANDOM_STATE, n_init=5, max_iter=300)
    gmm.fit(Xs)
    comp = gmm.predict(Xs)
    probs = gmm.predict_proba(Xs)
    sub['_c'] = comp
    sub['cluster_confidence'] = probs.max(axis=1)
    sub_orig['_c'] = comp

    # 质心（用原始尺度均值，用于解释；比率列已缩尾仅影响聚类距离）
    centers = sub_orig.groupby('_c')[FEAT_COLS].mean()
    sil = float(silhouette_score(Xs, comp)) if len(sub) > K else float('nan')
    log.info('silhouette = %.3f', sil)

    # 用 GMM 标准化空间质心映射语义标签
    comp_means = pd.DataFrame(gmm.means_, columns=FEAT_COLS)
    label_map = assign_labels(comp_means)
    log.info('质心标签映射: %s', label_map)
    sub['cluster_label'] = sub['_c'].map(label_map)

    # 回填聚类标签 + 置信度到候选行
    lab.loc[sub.index] = sub['cluster_label']
    conf = pd.Series(np.nan, index=feat.index, dtype=float)
    conf[is_new] = 1.0                      # 规则归类，置信度置 1.0
    conf.loc[sub.index] = sub['cluster_confidence']
    # 其余（特征缺失/缺口/无数据）= 数据缺失
    lab[lab == ''] = '数据缺失'
    feat['cluster_label'] = lab
    feat['cluster_confidence'] = conf.round(4)

    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    fp_csv = OUT / f'double_30d_clustered_{ts}.csv'
    out = feat.drop(columns=['_c'] if '_c' in feat.columns else [])
    out.to_csv(fp_csv, index=False, encoding='utf-8-sig')
    log.info('✓ 聚类结果 CSV: %s (%d 行)', fp_csv, len(out))

    _write_report(feat, centers, label_map, sil, ts)


def _write_report(feat: pd.DataFrame, centers: pd.DataFrame,
                  label_map: dict, sil: float, ts: str) -> None:
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    L: list[str] = []
    L.append(f"# 启动前 90 日形态聚类自动归类 ({now})\n")
    L.append("> 输入：data/double_up/double_30d_pre90_features.csv（1,898 事件 per-event 量价特征）")
    L.append("> 方法：次新(new_listing)规则归「次新连板」（置信度=1.0）；其余对 [pre_ret90, pre_maxdd, pre_ann_vol, "
             "pre_amt_ratio, pre_vol_ratio] 做 StandardScaler + 高斯混合 GMM(k=3, full)。比率特征先 1%/99% 缩尾"
             "（消除量能比 90× 级极端离群点对方差的支配），按质心轮廓解释映射为 "
             "「上涨中继 / N字底 / 平台突破」；每条样本输出 cluster_label + cluster_confidence"
             "（GMM 后验概率最大值）；数据缺失/缺口标「数据缺失」（置信度 NaN）。")
    L.append(f"> 聚类算法：GaussianMixture(k={K}, full, random_state={RANDOM_STATE})；轮廓系数 silhouette={sil:.3f}。\n")

    # ---- 核心指标 ----
    L.append("## 📊 核心指标\n")
    vc = feat['cluster_label'].value_counts()
    for k, v in vc.items():
        L.append(f"- **{k}**: {int(v):,} 个事件（{v/len(feat)*100:.1f}%）")
    L.append(f"- 聚类质量(silhouette): {sil:.3f}（越接近 1 越好；0.25~0.5 为弱-中等结构）")

    # ---- Top 样本（每类 5 个，按 peak_gain 取代表） ----
    L.append("\n## 🎯 Top 样本（每类按峰值涨幅取 5 个代表）\n")
    L.append("| 形态 | 代码 | 名称 | 启动日 | 前90日收益 | 年化波动 | 最大回撤 | 量能比 |")
    L.append("|---|---|---|---|---:|---:|---:|---:|---:|")
    for lab in ['平台突破', 'N字底', '上涨中继', '次新连板', '数据缺失']:
        rows = feat[feat['cluster_label'] == lab].sort_values('peak_gain', ascending=False).head(5)
        for _, r in rows.iterrows():
            ret = f"{r['pre_ret90']*100:.1f}%" if pd.notna(r['pre_ret90']) else 'NA'
            vol = f"{r['pre_ann_vol']*100:.0f}%" if pd.notna(r['pre_ann_vol']) else 'NA'
            dd = f"{r['pre_maxdd']*100:.0f}%" if pd.notna(r['pre_maxdd']) else 'NA'
            ar = f"{r['pre_amt_ratio']:.2f}" if pd.notna(r['pre_amt_ratio']) else 'NA'
            L.append(f"| {lab} | {r['code']} | {r['name']} | {r['start_date']} | "
                     f"{ret} | {vol} | {dd} | {ar} |")
    L.append("")

    # ---- 统计分布：质心画像 + 与原 pre_pattern 交叉 ----
    L.append("## 📈 统计分布\n")
    L.append("**各簇质心画像（原始尺度均值，非次新有效样本）**\n")
    L.append("| 形态 | 样本 | 前90日收益 | 年化波动 | 最大回撤 | 量能比 | 量比(vol) |")
    L.append("|---|---:|---:|---:|---:|---:|---:|")
    for c, lab in label_map.items():
        row = centers.loc[c]
        cnt = int((feat['cluster_label'] == lab).sum())
        L.append(f"| {lab} | {cnt} | {row['pre_ret90']*100:.1f}% | {row['pre_ann_vol']*100:.1f}% | "
                 f"{row['pre_maxdd']*100:.1f}% | {row['pre_amt_ratio']:.2f} | {row['pre_vol_ratio']:.2f} |")
    L.append("")

    L.append("**聚类标签 vs 原 pre_pattern 交叉表（行=聚类，列=pre_pattern）**\n")
    ct = pd.crosstab(feat['cluster_label'], feat['pre_pattern'])
    ct = ct.reindex(columns=ct.columns)  # 保持列序
    L.append("| 聚类 \\ pre_pattern | " + " | ".join(ct.columns) + " |")
    L.append("|---" + "|" * len(ct.columns) + "|")
    for idx, r in ct.iterrows():
        L.append(f"| {idx} | " + " | ".join(str(int(x)) for x in r) + " |")
    L.append("")

    # ---- 每样本置信度分布 ----
    L.append("**各形态置信度分布（GMM 后验概率，仅非次新/非缺失样本）**\n")
    L.append("| 形态 | 样本 | 中位置信度 | 低置信(<0.6)占比 |")
    L.append("|---|---:|---:|---:|")
    for lab in ['平台突破', 'N字底', '上涨中继', '次新连板']:
        rows = feat[feat['cluster_label'] == lab]
        c = rows['cluster_confidence'].dropna()
        if len(c):
            med = float(c.median())
            low = float((c < 0.6).mean() * 100)
            L.append(f"| {lab} | {len(rows)} | {med:.3f} | {low:.1f}% |")
        else:
            L.append(f"| {lab} | {len(rows)} | — | — |")
    L.append("")

    # ---- 风险提示 ----
    L.append("## ⚠️ 风险提示\n")
    L.append("1. 聚类为无监督统计分组，标签为按质心轮廓的**事后解释**，并非严格形态学定义；同一标签内个股差异仍大。")
    L.append("2. 特征仅含窗口聚合量价指标，未纳入形态序列细节（如是否真出现 N 形双底），归类存在近似。")
    L.append("3. 次新连板为规则归类（new_listing），其'前90日'窗口常不足，特征失真，结论需单列。")
    L.append("4. 数据缺失/缺口事件已标「数据缺失」，未参与聚类，避免污染质心。")
    L.append("5. 历史形态不构成未来收益保证；翻倍策略属激进策略，建议小资金验证。\n")

    # ---- 下一步建议 ----
    L.append("## 🔧 下一步建议\n")
    L.append("- 将聚类标签并入方案 E 回测（plan_e_subset_backtest.py），对各簇分别跑跟随回测，定位最优前置形态。")
    L.append("- 对「平台突破」「N字底」子集单独验证可交易性与 OOS 稳健性（walk-forward / 按年分层）。")
    L.append("- 若 silhouette 偏低，可增列形态序列特征（如归一化路径斜率、回撤次数）或改用高斯混合/层次聚类。")
    L.append("- 导出 clean subset（export_clean_subset.py）后再聚类，剔除次新与缺口以提升质心纯度。\n")

    L.append("---")
    L.append(f"生成时间：{now}")
    L.append("生成者：scripts/cluster_pre90_patterns.py")
    L.append("模型版本：GMM(k=3,full)+StandardScaler+置信度")
    L.append(f"数据日期：{datetime.now().strftime('%Y-%m-%d')}")

    fp_md = OUT / f'double_30d_clusters_report_{ts}.md'
    fp_md.write_text('\n'.join(L), encoding='utf-8')
    log.info('✓ 报告: %s', fp_md)


if __name__ == '__main__':
    main()
