#!/usr/bin/env python3
"""方案 E 子集跟随回测：基于 double_30d 事件的 pre_pattern 子集。

复用 plan_e_backtest 的 backtest_one / compute_metrics / _bootstrap_ci / _infer_tag，
对指定启动前形态子集（默认"横盘蓄势+放量"）单独跑翻倍当日跟随回测，
并可与全样本（1898 事件）对比，检验该形态是否更具可交易性。

用法：
  venv/bin/python scripts/plan_e_subset_backtest.py
  venv/bin/python scripts/plan_e_subset_backtest.py --pattern '横盘蓄势+放量' --compare-all
  venv/bin/python scripts/plan_e_subset_backtest.py --no-cost
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / 'scripts'))
import plan_e_backtest as pe  # noqa: E402

EVENTS = ROOT / 'data' / 'double_up' / 'double_30d_2021_2026.csv'
FEATURES = ROOT / 'data' / 'double_up' / 'double_30d_pre90_features.csv'
OUT = ROOT / 'data' / 'plan_e'

# 方案 E v2 默认参数
PARAMS = dict(hold_days=10, tp=0.30, sl=-0.10, min_t_pct=0.095, min_t_vol_ratio=1.5)


def run_events(events: pd.DataFrame, fee: float, slippage: float,
               bj_slippage: float) -> tuple[pd.DataFrame, list, int, int]:
    """逐事件调用 backtest_one（保持每个翻倍事件独立，不过滤重复 code）。

    返回 (bt_valid, daily_records, n_total, n_none)
    - bt_valid: 通过 T+1 涨停+量比过滤的有效回测结果（含 daily 列）
    - n_total: 进入回测的事件数
    - n_none: 因缺数据/无 T+2 而被 backtest_one 返回 None 的事件数
    """
    valid_rows: list[dict] = []
    daily: list = []
    n_total = 0
    n_none = 0
    for _, r in events.iterrows():
        n_total += 1
        res = pe.backtest_one(
            str(r['code']), str(r['peak_date']),
            fee=fee, slippage=slippage, bj_slippage=bj_slippage, **PARAMS)
        if res is None:
            n_none += 1
            continue
        if res.get('filtered'):   # T+1 未涨停 / 量比不足
            continue
        valid_rows.append(res)
        daily.append(res['daily'])
    bt = pd.DataFrame(valid_rows)
    return bt, daily, n_total, n_none


def _binned(bt: pd.DataFrame, sl: float, tp: float) -> pd.DataFrame:
    bins = [-1.0, sl, -0.03, 0.0, 0.1, tp, 1.0]
    labels = ['≤SL', 'SL~-3%', '-3%~0', '0~10%', '10%~TP', '≥TP']
    cut = pd.cut(bt['return_pct'], bins=bins, labels=labels, include_lowest=True)
    return cut.value_counts().reindex(labels).fillna(0).astype(int)


def build_report(pattern: str, subset_ev: pd.DataFrame, all_ev: pd.DataFrame | None,
                 fee: float, slippage: float, bj_slippage: float) -> str:
    now = datetime.now()
    ts = now.strftime('%Y-%m-%d %H:%M')
    m_sub, daily_sub, n_sub, none_sub = run_events(subset_ev, fee, slippage, bj_slippage)
    n_valid_sub = len(m_sub)
    n_filtered_sub = n_sub - n_valid_sub - none_sub
    m = pe.compute_metrics(m_sub, daily_sub)

    def pct(x):
        return f"{x*100:.2f}%"

    L: list[str] = []
    L.append(f"# 方案 E 子集跟随回测：{pattern} ({ts})\n")
    L.append("> 输入：data/double_up/double_30d_2021_2026.csv（1898 个 30 自然日翻倍事件）+ "
             "pre90 特征（pre_pattern 形态标签）")
    L.append("> 方法：对每个事件以 peak_date 为信号日 T，沿用方案 E v2 规则（T+1 涨停≥9.5% & 量比≥1.5 → "
             "T+2 开盘买入，TP+30%/SL-10%/持有10日 三选一出场），逐事件独立回测（不按 code 去重）。")
    L.append(f"> 成本：单边佣金 {fee * 100:.3f}% + 滑点 {slippage * 100:.3f}%（双边 {(fee + slippage) * 2 * 100:.3f}%），净收益已计入。\n" if (fee + slippage) > 0
        else "> 成本：未计入（净价回测）。\n")

    # ---- 核心指标 ----
    L.append("## 📊 核心指标\n")
    if n_valid_sub == 0:
        L.append("- ⚠️ 该子集无任何事件通过 T+1 涨停+量比过滤，无法形成跟随信号。\n")
    else:
        wr_lo, wr_hi = pe._bootstrap_ci(m_sub['return_pct'].values,
                                        lambda x: float((x > 0).mean() * 100))
        ar_lo, ar_hi = pe._bootstrap_ci(m_sub['return_pct'].values, lambda x: float(x.mean()))
        infer = pe._infer_tag(m['n'])
        L.append(f"- **子集事件数（进入回测）**: {n_sub}（其中触发跟随信号 **{n_valid_sub}** 笔，"
                 f"T+1 过滤掉 {n_filtered_sub} 笔，数据不足 {none_sub} 笔）")
        L.append(f"- **触发过滤率**: {n_filtered_sub / n_sub * 100:.1f}%（T+1 未涨停/量比不足）")
        L.append(f"- **显著性**: {infer}（样本 {m['n']} 笔，{'≥30 可作统计推断' if m['n']>=30 else '<30 仅参考'}）")
        L.append(f"- **胜率**: **{m['win_rate']:.1f}%**  [95% CI {wr_lo:.1f}%, {wr_hi:.1f}%]")
        L.append(f"- **平均收益**: **{pct(m['avg_ret'])}**  [95% CI {ar_lo*100:.1f}%, {ar_hi*100:.1f}%]")
        L.append(f"- **期望收益**: {pct(m['exp_ret'])}")
        L.append(f"- **年化收益**: {pct(m['ann_ret'])}")
        L.append(f"- **夏普比率**: {m['sharpe']:.2f}")
        L.append(f"- **索提诺比率**: {m['sortino']:.2f}")
        L.append(f"- **最大回撤**: {pct(m['max_dd'])}")
        L.append(f"- **Calmar**: {m['calmar']:.2f}")
        L.append(f"- **盈亏比**: {m['pf']:.2f}")
        L.append(f"- **出场分布**: 止盈 {m['n_tp']} / 止损 {m['n_sl']} / 时间止损 {m['n_time']}")

    # ---- Top 30 明细 ----
    L.append("\n## 🎯 Top 30 明细（按跟随收益）\n")
    if n_valid_sub > 0:
        top = m_sub.nlargest(30, 'return_pct')[
            ['code', 'signal_date', 't1_pct', 't1_vol_ratio', 'entry_price',
             'return_pct', 'exit_reason', 'hold']]
        L.append("| 排名 | 代码 | 信号日 T | T+1涨幅 | T+1量比 | 入场价 | 收益 | 出场 | 持有 |")
        L.append("|---:|---|---|---:|---:|---:|---:|---|---:|")
        for i, (_, r) in enumerate(top.iterrows(), 1):
            L.append(f"| {i} | {r['code']} | {r['signal_date']} | {r['t1_pct']*100:.1f}% | "
                     f"{r['t1_vol_ratio']:.2f} | {r['entry_price']:.2f} | "
                     f"**{pct(r['return_pct'])}** | {r['exit_reason']} | {r['hold']} |")
    else:
        L.append("- 无有效信号。")

    # ---- 统计分布 ----
    L.append("\n## 📈 统计分布\n")
    if n_valid_sub > 0:
        dist = _binned(m_sub, PARAMS['sl'], PARAMS['tp'])
        L.append("**跟随收益分桶**\n")
        L.append("| 收益区间 | 笔数 | 占比 |")
        L.append("|---|---:|---:|")
        for k, v in dist.items():
            L.append(f"| {k} | {int(v)} | {v/n_valid_sub*100:.1f}% |")
        L.append("")

    # ---- 对比（含全样本） ----
    if all_ev is not None:
        m_all, daily_all, n_all, none_all = run_events(all_ev, fee, slippage, bj_slippage)
        n_valid_all = len(m_all)
        n_filtered_all = n_all - n_valid_all - none_all
        if n_valid_sub > 0 and n_valid_all > 0:
            ma = pe.compute_metrics(m_all, daily_all)
            L.append("**子集 vs 全样本（1898 事件）对比**\n")
            L.append("| 指标 | 子集(" + pattern + ") | 全样本(1898) |")
            L.append("|---|---:|---:|")
            L.append(f"| 进入回测事件 | {n_sub} | {n_all} |")
            L.append(f"| 触发信号(笔) | {n_valid_sub} | {n_valid_all} |")
            L.append(f"| 触发过滤率 | {n_filtered_sub/n_sub*100:.1f}% | {n_filtered_all/n_all*100:.1f}% |")
            L.append(f"| **胜率** | **{m['win_rate']:.1f}%** | **{ma['win_rate']:.1f}%** |")
            L.append(f"| **平均收益** | **{pct(m['avg_ret'])}** | **{pct(ma['avg_ret'])}** |")
            L.append(f"| 年化收益 | {pct(m['ann_ret'])} | {pct(ma['ann_ret'])} |")
            L.append(f"| 夏普 | {m['sharpe']:.2f} | {ma['sharpe']:.2f} |")
            L.append(f"| 最大回撤 | {pct(m['max_dd'])} | {pct(ma['max_dd'])} |")
            L.append(f"| 盈亏比 | {m['pf']:.2f} | {ma['pf']:.2f} |")
            L.append(f"| 显著性 | {pe._infer_tag(n_valid_sub)} | {pe._infer_tag(n_valid_all)} |")
            L.append("")
            L.append("> 解读：若子集胜率/平均收益明显高于全样本，且触发过滤率更低（更易出现 T+1 涨停接力），"
                     "则说明'横盘蓄势+近端放量'是方案 E 更高胜率的前置形态；反之则形态筛选增益有限。\n")

    # ---- 风险提示 ----
    L.append("## ⚠️ 风险提示\n")
    L.append("1. 历史回测不代表未来收益，翻倍妖股策略暴露于极端波动与流动性风险。")
    L.append("2. 模型基于 2021–2026 数据，2026 年新模式可能失效。")
    L.append("3. 翻倍策略属激进策略，建议小资金启动。")
    L.append("4. 严格止损（sl=-10%）防止黑天鹅。")
    L.append("5. 北交所（920XXX）流动性较低，滑点可能放大；本回测已计入基础滑点，北交所额外滑点可用 --bj-slippage 压力测试。")
    L.append(f"6. 子集样本仅 {n_valid_sub} 笔，统计显著性有限（"
             f"{'≥30 可作推断' if n_valid_sub >= 30 else '<30 仅参考'}），bootstrap CI 已给出但区间仍宽，结论需谨慎。\n")

    # ---- 下一步建议 ----
    L.append("## 🔧 下一步建议\n")
    L.append("1. 对胜率更高的形态子集（如'横盘蓄势+放量'）单独做 walk-forward / 按年分层，验证 OOS 稳健性。")
    L.append("2. 用 --bj-slippage 0.003 对子集做北交所小盘压力测试。")
    L.append("3. 将聚类自动归类（N字底/平台突破/次新连板）后的各簇分别回测，定位最优前置形态。")
    L.append("4. 实盘小资金验证：信号日 T+1 满足条件后 T+2 开盘买入，严格 TP/SL/时间止损。\n")

    L.append("---")
    L.append(f"生成时间：{now.strftime('%Y-%m-%d %H:%M:%S')}")
    L.append("生成者：scripts/plan_e_subset_backtest.py")
    L.append("模型版本：v2（复用 plan_e_backtest）")
    L.append(f"数据日期：{now.strftime('%Y-%m-%d')}")
    return "\n".join(L), m_sub, daily_sub, n_sub, n_valid_sub, none_sub


def main() -> None:
    ap = argparse.ArgumentParser(description='方案 E 子集跟随回测')
    ap.add_argument('--pattern', default='横盘蓄势+放量', help='pre_pattern 形态标签')
    ap.add_argument('--fee', type=float, default=0.0003)
    ap.add_argument('--slippage', type=float, default=0.001)
    ap.add_argument('--bj-slippage', type=float, default=0.0)
    ap.add_argument('--no-cost', action='store_true')
    ap.add_argument('--compare-all', action='store_true', help='同时回测全样本 1898 作对照')
    ap.add_argument('--cluster', default=None,
                    help='改用 cluster_pre90_patterns 的 cluster_label 筛选（如 平台突破/N字底/上涨中继）')
    args = ap.parse_args()

    fee = 0.0 if args.no_cost else args.fee
    slippage = 0.0 if args.no_cost else args.slippage
    bj = 0.0 if args.no_cost else args.bj_slippage

    OUT.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')

    ev = pd.read_csv(EVENTS)
    fe = pd.read_csv(FEATURES)
    merge = ev.merge(fe[['code', 'start_date', 'pre_pattern', 'data_gap', 'new_listing']],
                     on=['code', 'start_date'], how='left')

    if args.cluster:
        # 读取最新聚类结果，按 cluster_label 筛选
        cl_files = sorted(OUT.parent.glob('data/double_up/double_30d_clustered_*.csv'))
        if not cl_files:
            cl_files = sorted((ROOT / 'data' / 'double_up').glob('double_30d_clustered_*.csv'))
        cl = pd.read_csv(cl_files[-1])[['code', 'start_date', 'cluster_label']]
        merge = merge.merge(cl, on=['code', 'start_date'], how='left')
        sel = args.cluster
        col = 'cluster_label'
    else:
        sel = args.pattern
        col = 'pre_pattern'
    safe = sel.replace('+', '_').replace(' ', '')

    subset = merge[merge[col] == sel]
    subset_clean = subset[~subset['new_listing'].fillna(False)
                          & ~subset['data_gap'].fillna(False)]
    print(f"[1/3] 筛选={col}={sel}: 事件 {len(subset)} → 剔除次新/缺口后 {len(subset_clean)}")

    all_ev = None
    if args.compare_all:
        all_clean = merge[~merge['new_listing'].fillna(False)
                          & ~merge['data_gap'].fillna(False)]
        all_ev = all_clean
        print(f"      全样本对照: {len(all_clean)} 事件")

    print("[2/3] 回测子集...")
    md, m_sub, _, n_sub, n_valid_sub, none_sub = build_report(
        sel, subset_clean, all_ev, fee, slippage, bj)

    fp_md = OUT / f'plan_e_subset_{safe}_{ts}.md'
    fp_md.write_text(md, encoding='utf-8')
    print(f"  ✓ MD: {fp_md.name}")

    # 信号明细 CSV（子集有效信号）
    if n_valid_sub > 0:
        fp_csv = OUT / f'plan_e_subset_{safe}_signals_{ts}.csv'
        out = m_sub.drop(columns=['daily'], errors='ignore')
        out.to_csv(fp_csv, index=False, encoding='utf-8-sig')
        print(f"  ✓ CSV(信号): {fp_csv.name} ({n_valid_sub} 笔)")

    print(f"[3/3] 完成。子集触发信号 {n_valid_sub}/{n_sub} 笔"
          f"（过滤 {n_sub - n_valid_sub - none_sub}，数据不足 {none_sub}）")


if __name__ == '__main__':
    main()
