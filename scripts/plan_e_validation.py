#!/usr/bin/env python3
"""方案 E 翻倍跟随策略验证：费用/滑点敏感性 + 收益稳定性 + 信号频率 + 最大回撤。

针对某启动前形态子集（默认"横盘蓄势+放量"），在方案 E v2 规则下做：
  1) 基准指标（含成本）
  2) 费用×滑点敏感性网格（评估手续费/滑点敏感性）
  3) 按年收益稳定性（年化/夏普/最大回撤的分年表现 + 离散度）
  4) 信号触发频率（触发数/进入数、过滤率）
并以"平台突破""N字底"簇作对照，定位最具可交易性的前置形态。

复用 plan_e_backtest 的 backtest_one / compute_metrics 与 plan_e_subset_backtest 的 run_events。

用法：
  venv/bin/python scripts/plan_e_validation.py
  venv/bin/python scripts/plan_e_validation.py --cluster 平台突破
  venv/bin/python scripts/plan_e_validation.py --pattern '横盘蓄势+放量' --compare-all
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / 'scripts'))
import plan_e_backtest as pe  # noqa: E402
from plan_e_subset_backtest import run_events  # noqa: E402

EVENTS = ROOT / 'data' / 'double_up' / 'double_30d_2021_2026.csv'
FEATURES = ROOT / 'data' / 'double_up' / 'double_30d_pre90_features.csv'
OUT = ROOT / 'data' / 'plan_e'

# 敏感性网格
FEE_GRID = [0.0, 0.0003, 0.0005]
SLIP_GRID = [0.0005, 0.001, 0.003]


def select_subset(args) -> tuple[pd.DataFrame, str, str]:
    ev = pd.read_csv(EVENTS)
    fe = pd.read_csv(FEATURES)
    merge = ev.merge(fe[['code', 'start_date', 'pre_pattern', 'data_gap', 'new_listing']],
                     on=['code', 'start_date'], how='left')
    if args.cluster:
        cl_files = sorted((ROOT / 'data' / 'double_up').glob('double_30d_clustered_*.csv'))
        cl = pd.read_csv(cl_files[-1])[['code', 'start_date', 'cluster_label']]
        merge = merge.merge(cl, on=['code', 'start_date'], how='left')
        sel, col = args.cluster, 'cluster_label'
    else:
        sel, col = args.pattern, 'pre_pattern'
    sub = merge[merge[col] == sel]
    sub = sub[~sub['new_listing'].fillna(False) & ~sub['data_gap'].fillna(False)]
    return sub, sel, col


def metrics_of(subset, fee, slip, bj):
    bt, daily, n_total, n_none = run_events(subset, fee, slip, bj)
    n_valid = len(bt)
    if n_valid == 0:
        return dict(n_total=n_total, n_valid=0, n_none=n_none,
                    win_rate=float('nan'), avg_ret=float('nan'), sharpe=float('nan'),
                    max_dd=float('nan'), pf=float('nan')), bt, daily
    m = pe.compute_metrics(bt, daily)
    return dict(n_total=n_total, n_valid=n_valid, n_none=n_none,
                win_rate=m['win_rate'], avg_ret=m['avg_ret'], sharpe=m['sharpe'],
                max_dd=m['max_dd'], pf=m['pf']), bt, daily


def pct(x):
    return f"{x*100:.2f}%" if pd.notna(x) else '—'


def walk_forward_report(bt: pd.DataFrame, daily: list, sel: str) -> list[str]:
    """扩展窗口 walk-forward：验证年 T 的 OOS 用 [首年, T-1] 全部事件作定义期，逐层回测。
    复用 pe.compute_metrics 计算各窗指标，输出各年度 OOS 表现与稳定性结论。"""
    if bt is None or len(bt) == 0 or 'signal_date' not in bt.columns:
        return ["## 🔄 Walk-forward 按年分层验证\n\n无有效信号，无法分层。\n"]
    b = bt.copy()
    b['yr'] = pd.to_datetime(b['signal_date']).dt.year
    years = sorted(int(y) for y in b['yr'].dropna().unique())
    rows = []
    for T in years:
        is_sub = b[b['yr'] < T]
        oos_sub = b[b['yr'] == T]
        if len(oos_sub) == 0:
            continue
        daily_is = [daily[i] for i in is_sub.index] if len(is_sub) else []
        daily_oos = [daily[i] for i in oos_sub.index]
        m_is = pe.compute_metrics(is_sub, daily_is) if len(is_sub) else {}
        m_oos = pe.compute_metrics(oos_sub, daily_oos)
        rows.append({
            'T': T,
            'n_is': int(m_is.get('n', 0)), 'n_oos': int(m_oos['n']),
            'wr_is': m_is.get('win_rate', float('nan')), 'wr_oos': m_oos['win_rate'],
            'ann_is': m_is.get('ann_ret', float('nan')), 'ann_oos': m_oos['ann_ret'],
            'sh_is': m_is.get('sharpe', float('nan')), 'sh_oos': m_oos['sharpe'],
            'dd_oos': m_oos['max_dd'], 'avg_oos': m_oos['avg_ret'],
        })
    if not rows:
        return ["## 🔄 Walk-forward 按年分层验证\n\n无有效信号，无法分层。\n"]
    shps = [r['sh_oos'] for r in rows]
    wrs = [r['wr_oos'] for r in rows]
    anns = [r['ann_oos'] for r in rows]
    dds = [r['dd_oos'] for r in rows]
    avgs = [r['avg_oos'] for r in rows]
    nw = len(rows)
    mean_sh, mean_wr = sum(shps) / nw, sum(wrs) / nw
    mean_ann, mean_dd, mean_avg = sum(anns) / nw, sum(dds) / nw, sum(avgs) / nw
    frac_pos = sum(1 for s in shps if s > 0) / nw
    n_infer = sum(1 for r in rows if r['n_oos'] >= pe.MIN_INFER_N)

    def fmt(x):
        return f"{x*100:.2f}%" if pd.notna(x) else '—'

    def fmt_wr(x):  # win_rate 已是百分比值(如 60.0)，不可再 ×100
        return f"{x:.1f}%" if pd.notna(x) else '—'

    L: list[str] = []
    L.append("## 🔄 Walk-forward 按年分层验证（扩展窗口 OOS）\n")
    L.append(f"> 窗口设计：验证年 T 的 OOS 用 [首年, T-1] 全部事件作定义期，验证期 = 信号年 = T（固定 1 年）；"
             f"定义期随 T 扩展。共 {nw} 个 OOS 窗口（验证年 {rows[0]['T']}–{rows[-1]['T']}）。"
             f"策略为固定参数（v2），不逐年重选；本表评估该前置形态 OOS 稳健性。\n")
    L.append("### 📊 核心指标（OOS 汇总）\n")
    L.append(f"- OOS 窗口数：**{nw}**；**正夏普窗口占比：{frac_pos*100:.0f}%**（{sum(1 for s in shps if s > 0)}/{nw}）")
    L.append(f"- **可作统计推断窗口（OOS 样本≥{pe.MIN_INFER_N}）：{n_infer}/{nw}**；"
             f"仅这些窗口点估计具统计显著性，其余仅参考。")
    L.append(f"- OOS 夏普：min {min(shps):.2f} / **mean {mean_sh:.2f}** / max {max(shps):.2f}")
    L.append(f"- OOS 胜率：min {min(wrs):.1f}% / **mean {mean_wr:.1f}%** / max {max(wrs):.1f}%")
    L.append(f"- OOS 平均收益：min {fmt(min(avgs))} / **mean {fmt(mean_avg)}** / max {fmt(max(avgs))}")
    L.append(f"- OOS 年化：mean {fmt(mean_ann)}；OOS 平均最大回撤 {fmt(mean_dd)}")
    L.append("")
    L.append("### 📈 各窗口明细\n")
    L.append("| 验证年 T | 定义期样本 | OOS样本 | 定义期胜率 | OOS胜率 | OOS平均收益 | OOS年化 | 定义期夏普 | OOS夏普 | OOS回撤 | 显著性 |")
    L.append("|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for r in rows:
        L.append(
            f"| {r['T']} | {r['n_is']} | {r['n_oos']} | {fmt_wr(r['wr_is'])} | "
            f"{r['wr_oos']:.1f}% | {fmt(r['avg_oos'])} | {fmt(r['ann_oos'])} | "
            f"{r['sh_is']:.2f} | {r['sh_oos']:.2f} | {fmt(r['dd_oos'])} | {pe._infer_tag(r['n_oos'])} |")
    L.append("")
    L.append(pe._infer_legend(oos=True,
             extra="固定参数版 walk-forward；聚类簇样本偏小，多数窗口 n<30，结论以前述方向性为主"))
    L.append("\n### 🧭 稳定性结论\n")
    if n_infer == 0:
        L.append(f"- 全部 {nw} 个 OOS 窗口的年度样本均 <{pe.MIN_INFER_N}，**无单年可独立统计推断**；"
                 f"稳定性结论基于方向一致性与 pooled 表现。")
    else:
        L.append(f"- {n_infer}/{nw} 个 OOS 窗口样本≥{pe.MIN_INFER_N}，可作统计推断；其余仅参考。")
    stable = '稳健' if (frac_pos >= 0.6 and mean_sh > 0) else ('中等' if frac_pos >= 0.5 else '偏弱')
    L.append(f"- 正夏普窗口占比 {frac_pos*100:.0f}%，OOS 夏普均值 {mean_sh:.2f}"
             f"（{'全为正' if min(shps) > 0 else '存在负值'}）→ OOS 表现**{stable}**。"
             f"{'首窗（最早年）为冷启动、定义期样本极少，不计入严谨性评判。' if rows[0]['n_is'] < pe.MIN_INFER_N else ''}")
    L.append("- walk-forward 消除单次切分(pooled)的样本内乐观偏差；若后续某验证年 OOS 显著转负，"
             "提示该前置形态在该年度失效，需警惕周期/风格切换。")
    L.append("")
    return L


def main() -> None:
    ap = argparse.ArgumentParser(description='方案 E 验证：敏感性/稳定性/信号频率')
    ap.add_argument('--pattern', default='横盘蓄势+放量')
    ap.add_argument('--cluster', default=None)
    ap.add_argument('--fee', type=float, default=0.0003)
    ap.add_argument('--slippage', type=float, default=0.001)
    ap.add_argument('--bj-slippage', type=float, default=0.0)
    ap.add_argument('--compare-all', action='store_true')
    ap.add_argument('--walk-forward', action='store_true',
                    help='按年分层 walk-forward（扩展窗口 OOS），逐验证年输出 OOS 指标 + 稳定性结论')
    args = ap.parse_args()

    sub, sel, col = select_subset(args)
    print(f"[验证] 子集={col}={sel}: {len(sub)} 事件（剔除次新/缺口后）")
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    safe = sel.replace('+', '_').replace(' ', '')

    L: list[str] = []
    now = datetime.now().strftime('%Y-%m-%d %H:%M')
    L.append(f"# 方案 E 翻倍跟随策略验证：{sel} ({now})\n")
    L.append("> 输入：double_30d_2021_2026.csv（1,898 事件）+ pre90 特征/聚类标签")
    L.append("> 方法：以 peak_date 为信号日 T，沿用方案 E v2（T+1 涨停≥9.5% & 量比≥1.5 → T+2 开盘买入，"
             "TP+30%/SL-10%/持有10日 三选一出场），逐事件独立回测。")
    L.append("> 验证维度：基准指标、费用×滑点敏感性、按年收益稳定性、信号触发频率、最大回撤。\n")

    # ---- 1. 基准 + 信号频率 ----
    base, bt, daily = metrics_of(sub, args.fee, args.slippage, args.bj_slippage)
    n_total, n_valid = base['n_total'], base['n_valid']
    filt = n_total - n_valid - base['n_none']
    L.append("## 📊 核心指标（基准：佣金 0.03% + 滑点 0.10%）\n")
    if n_valid == 0:
        L.append(f"- ⚠️ 该子集无事件通过 T+1 涨停+量比过滤（进入 {n_total}，过滤 {filt}，数据不足 {base['n_none']}），不可交易。\n")
    else:
        L.append(f"- **进入回测事件**: {n_total}；**触发信号**: **{n_valid}** 笔；"
                 f"T+1 过滤 {filt} 笔（过滤率 {filt/n_total*100:.1f}%）；数据不足 {base['n_none']} 笔")
        L.append(f"- **信号触发频率**: {n_valid/n_total*100:.1f}%（每 100 个该形态事件约触发 {n_valid/n_total*100:.1f} 笔）")
        L.append(f"- **可交易性判定**: {'✓ 信号充足' if n_valid >= 30 else '⚠️ 样本不足(n<30)，结论仅参考'}")
        L.append(f"- **胜率**: **{f"{base['win_rate']:.1f}%"}** ｜ **平均收益**: **{pct(base['avg_ret'])}** ｜ "
                 f"夏普 {base['sharpe']:.2f} ｜ 盈亏比 {base['pf']:.2f}")
        L.append(f"- **最大回撤**: **{pct(base['max_dd'])}**")
        L.append(f"- **显著性**: {pe._infer_tag(n_valid)}（n={n_valid}）\n")

    # ---- 2. 费用×滑点敏感性 ----
    L.append("## 📈 费用 × 滑点敏感性\n")
    L.append("| 佣金 | 滑点 | 触发笔数 | 胜率 | 平均收益 | 夏普 | 最大回撤 | 盈亏比 |")
    L.append("|---|---:|---:|---:|---:|---:|---:|---:|")
    sens_rows = []
    for fee in FEE_GRID:
        for slip in SLIP_GRID:
            m, _, _ = metrics_of(sub, fee, slip, 0.0)
            sens_rows.append((fee, slip, m))
            L.append(f"| {fee*100:.2f}% | {slip*100:.2f}% | {m['n_valid']} | "
                     f"{f"{m['win_rate']:.1f}%"} | {pct(m['avg_ret'])} | {m['sharpe']:.2f} | "
                     f"{pct(m['max_dd'])} | {m['pf']:.2f} |")
    L.append("")
    L.append("> 解读：若平均收益在各成本档位均显著>0 且对滑点不敏感，说明策略对交易成本稳健；"
             "若滑点升至 0.30% 后平均收益转负，则实盘需优选高流动性标的。\n")

    # ---- 北交所额外滑点压力 ----
    if n_valid > 0:
        m_bj, _, _ = metrics_of(sub, args.fee, args.slippage, 0.003)
        L.append(f"- **北交所额外滑点 0.30% 压力测试**：触发 {m_bj['n_valid']} 笔，"
                 f"平均收益 {pct(m_bj['avg_ret'])}，最大回撤 {pct(m_bj['max_dd'])}"
                 f"（对比基准 {pct(base['avg_ret'])}）。\n")

    # ---- 3. 按年收益稳定性 ----
    L.append("## 🔍 收益稳定性（按年分层）\n")
    if n_valid > 0 and 'signal_date' in bt.columns:
        bt = bt.copy()
        bt['yr'] = pd.to_datetime(bt['signal_date']).dt.year
        years = sorted(bt['yr'].dropna().unique())
        L.append("| 年份 | 笔数 | 胜率 | 平均收益 | 夏普 | 最大回撤 |")
        L.append("|---|---:|---:|---:|---:|---:|")
        yr_avgs = []
        for y in years:
            idx = bt['yr'] == y
            by_bt = bt[idx]
            by_daily = [daily[i] for i in by_bt.index]
            md = pe.compute_metrics(by_bt, by_daily)
            yr_avgs.append(md['avg_ret'])
            L.append(f"| {int(y)} | {md['n']} | {md['win_rate']:.1f}% | {pct(md['avg_ret'])} | "
                     f"{md['sharpe']:.2f} | {pct(md['max_dd'])} |")
        if len(yr_avgs) > 1:
            arr = np.array(yr_avgs, dtype=float)
            mean_a, std_a = float(arr.mean()), float(arr.std(ddof=0))
            cv = std_a / abs(mean_a) if mean_a != 0 else float('nan')
            L.append(f"\n- **年度平均收益均值**: {pct(mean_a)}；**标准差**: {pct(std_a)}；"
                     f"**离散系数(CV)**: {cv:.2f}（越低越稳定）")
            L.append(f"- **最差年份平均收益**: {pct(float(np.min(arr)))}；"
                     f"正收益年份占比: {float((arr > 0).mean()*100):.0f}%")
        L.append("")
    else:
        L.append("- 样本不足或无信号日期，无法做按年稳定性分析。\n")

    # ---- 3b. Walk-forward 按年分层 OOS ----
    if args.walk_forward and n_valid > 0 and 'signal_date' in bt.columns:
        L += walk_forward_report(bt, daily, sel)

    # ---- 4. 对照（全样本） ----
    if args.compare_all:
        ev = pd.read_csv(EVENTS)
        fe = pd.read_csv(FEATURES)
        m_all = ev.merge(fe[['code', 'start_date', 'data_gap', 'new_listing']],
                         on=['code', 'start_date'], how='left')
        m_all = m_all[~m_all['new_listing'].fillna(False) & ~m_all['data_gap'].fillna(False)]
        ma, bt_all, daily_all = metrics_of(m_all, args.fee, args.slippage, args.bj_slippage)
        L.append("**vs 全样本（1,898 事件）**\n")
        L.append(f"- 全样本触发 {ma['n_valid']} 笔，胜率 {f"{ma['win_rate']:.1f}%"}，"
                 f"平均收益 {pct(ma['avg_ret'])}，夏普 {ma['sharpe']:.2f}，最大回撤 {pct(ma['max_dd'])}。")
        L.append("- 若子集平均收益/胜率明显高于全样本且最大回撤更低，则该前置形态更具可交易性。\n")
        if args.walk_forward and ma['n_valid'] > 0:
            L.append("\n**vs 全样本（1,898 事件）walk-forward 对照**\n")
            L += walk_forward_report(bt_all, daily_all, '全样本')

    # ---- 风险提示 ----
    L.append("## ⚠️ 风险提示\n")
    L.append("1. 历史回测不代表未来收益；翻倍妖股策略暴露于极端波动与流动性风险。")
    L.append("2. 样本量(n)偏小时，年度分层与敏感性结论噪声大，仅作方向性参考。")
    L.append("3. 方案 E 信号依赖 T+1 涨停+量比，触发频率天然偏低，实盘需注意信号稀疏性。")
    L.append("4. 北交所（920XXX）流动性低，滑点可能高于测试值；建议对北交所标的单独压力测试。")
    L.append("5. 严格止损（sl=-10%）为必需，防止单笔黑天鹅。\n")

    L.append("---")
    L.append(f"生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    L.append("生成者：scripts/plan_e_validation.py")
    L.append("模型版本：v2（复用 plan_e_backtest）")
    L.append(f"数据日期：{datetime.now().strftime('%Y-%m-%d')}")

    fp_md = OUT / f'plan_e_validation_{safe}_{ts}.md'
    fp_md.write_text('\n'.join(L), encoding='utf-8')
    # 敏感性 CSV
    sens_df = pd.DataFrame([dict(fee=f, slippage=s, **m) for f, s, m in sens_rows])
    fp_csv = OUT / f'plan_e_validation_{safe}_sensitivity_{ts}.csv'
    sens_df.to_csv(fp_csv, index=False, encoding='utf-8-sig')
    print(f"  ✓ MD: {fp_md.name}; CSV: {fp_csv.name}")
    print(f"  基准: 触发 {base['n_valid']}/{base['n_total']} 笔, 胜率 {base['win_rate']:.1f}%, "
          f"均收益 {base['avg_ret']*100:.2f}%, 最大回撤 {base['max_dd']*100:.2f}%")


if __name__ == '__main__':
    main()
