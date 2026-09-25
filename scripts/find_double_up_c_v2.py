"""方案 C v2：用前复权价重跑 20 日翻倍 + 与 v1 对比

数据源：
  - parquet/daily/{code}.parquet: 收盘价（不复权）
  - parquet/adj_factor/{code_short}.parquet: adj_factor, qfq_close
       qfq_close = raw_close × adj_factor

对比维度：
  1) 翻倍样本数 (v1 不复权 vs v2 前复权)
  2) 涉及标的数变化
  3) 涨幅差异 (v2_pct - v1_pct)
  4) v1 在 v2 视角下的"假翻倍"样本 (v1 是但 v2 不是)
  5) v2 在 v1 视角下的"漏判"样本 (v2 是但 v1 不是)

输出：
  - data/double_up/double_up_v2_2020_2026_detail.csv
  - data/double_up/double_up_v2_compare.csv (v1 vs v2 diff)
  - data/double_up/double_up_v2_report_YYYYMMDD.md
"""
import json
import re
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

PARQUET = Path('/home/jiuben/tdx-data-feed/data/parquet/daily')
ADJ = Path('/home/jiuben/tdx-data-feed/data/adj_factor')
VAULT_DIR = Path('/home/jiuben/StockVault/01-标的')
ALL_STOCKS = Path('/home/jiuben/tdx-data-feed/data/all_a_stocks.json')
V1_DETAIL = Path('/home/jiuben/tdx-data-feed/data/double_up/double_up_2020_2026_detail.csv')
OUT_DIR = Path('/home/jiuben/tdx-data-feed/data/double_up')

START_DATE = pd.Timestamp('2020-01-01')
WINDOW = 20
RATIO = 2.0

LIMIT_MAIN = 0.095
LIMIT_GEM = 0.195
LIMIT_BSE = 0.295


def is_st(code: str, st_set: set) -> bool:
    return code in st_set


def get_name(code: str, name_map: dict) -> str:
    short = re.sub(r'^(sh|sz|bj)', '', code)
    n = name_map.get(short, '?')
    return n.replace('　', '').strip()


def get_limit(code: str) -> float:
    short = code[2:]
    if code.startswith('bj'):
        return LIMIT_BSE
    if code.startswith('sz'):
        if short.startswith('30') or short.startswith('20'):
            return LIMIT_GEM
        return LIMIT_MAIN
    if code.startswith('sh'):
        if short.startswith('688'):
            return LIMIT_GEM
        return LIMIT_MAIN
    return LIMIT_MAIN


def screen_one_qfq(code: str, name_map: dict, st_set: set) -> list:
    """用前复权价 (raw_close × adj_factor) 跑翻倍"""
    fp = PARQUET / f'{code}.parquet'
    if not fp.exists():
        return []

    short = re.sub(r'^(sh|sz|bj)', '', code)
    adj_fp = ADJ / f'{short}.parquet'
    if not adj_fp.exists():
        return []

    try:
        df = pd.read_parquet(fp, columns=['date', 'open', 'high', 'low', 'close', 'volume', 'amount'])
        adj = pd.read_parquet(adj_fp, columns=['date', 'adj_factor'])
    except Exception:
        return []

    if df['date'].dtype in ('int64', 'int32'):
        df['date'] = pd.to_datetime(df['date'].astype(str), format='%Y%m%d')
    else:
        df['date'] = pd.to_datetime(df['date'])

    if adj['date'].dtype in ('int64', 'int32'):
        adj['date'] = pd.to_datetime(adj['date'].astype(str), format='%Y%m%d')
    else:
        adj['date'] = pd.to_datetime(adj['date'])

    df = df.merge(adj, on='date', how='left')
    df['adj_factor'] = df['adj_factor'].fillna(1.0)
    df['qfq_close'] = df['close'] * df['adj_factor']

    df = df.sort_values('date').reset_index(drop=True)
    df = df[df['date'] >= START_DATE].reset_index(drop=True)
    if len(df) < WINDOW + 5:
        return []

    first_date = df['date'].iloc[0]
    days_since_first = (df['date'] - first_date).dt.days

    close = df['qfq_close'].values
    dates = df['date'].values
    raw_pct = df['close'].pct_change().values  # 用不复权涨幅算涨停（一致）

    limit = get_limit(code)
    is_st_flag = code in st_set
    name = get_name(code, name_map)

    hits = []
    n = len(close)
    for i in range(n - WINDOW):
        start_close = close[i]
        if start_close <= 0 or np.isnan(start_close):
            continue
        window_max = close[i+1:i+WINDOW+1].max()
        ratio = window_max / start_close
        if ratio >= RATIO:
            peak_idx = i + 1 + np.argmax(close[i+1:i+WINDOW+1])
            peak_date = pd.Timestamp(dates[peak_idx])
            # 用不复权涨幅判涨停（更准，因为涨停是按交易所规则对不复权价算的）
            window_pct = raw_pct[i+1:i+WINDOW+1]
            limit_up_count = int((window_pct >= limit - 0.001).sum())
            window_amount = df['amount'].iloc[i+1:i+WINDOW+1].sum()
            is_new = days_since_first.iloc[i] < 250

            hits.append({
                'code': code,
                'name': name,
                'start_date': pd.Timestamp(dates[i]).strftime('%Y-%m-%d'),
                'peak_date': peak_date.strftime('%Y-%m-%d'),
                'days_to_peak': peak_idx - i,
                'start_close_qfq': round(float(start_close), 4),
                'peak_close_qfq': round(float(window_max), 4),
                'pct_change': round((ratio - 1) * 100, 1),
                'limit_up_count': limit_up_count,
                'window_amount': float(window_amount),
                'year': peak_date.year,
                'is_st': is_st_flag,
                'is_new': bool(is_new),
                'limit': round(limit * 100, 0),
            })

    return hits


def main():
    t0 = datetime.now()
    print("=== 方案 C v2: 前复权价翻倍筛选 ===")
    print(f"  START_DATE: {START_DATE.date()}")
    print("  数据源: parquet/daily × parquet/adj_factor (5221 只)")
    print()

    with open(ALL_STOCKS) as _f:
        name_map = json.load(_f)

    st_set = set()
    for md in VAULT_DIR.glob('*.md'):
        if '*ST' in md.stem or md.stem.startswith('ST') or 'ST' in md.stem:
            code = re.match(r'(\d{6})', md.stem)
            if code:
                st_set.add(code.group(1))
    print(f"  ST 集合: {len(st_set)} 只")

    files = sorted(PARQUET.glob('*.parquet'))
    print(f"  parquet/daily 总数: {len(files)}")
    print(f"  adj_factor 总数: {len(list(ADJ.glob('*.parquet')))}")

    full_st_set = set()
    for code_short in st_set:
        for f in files:
            if f.stem.endswith(code_short):
                full_st_set.add(f.stem)
                break

    print(f"\n开始筛选（仅扫有 adj_factor 的 {len(list(ADJ.glob('*.parquet')))} 只）...")
    all_hits = []
    scanned = 0
    no_adj = 0
    # 只扫有 adj_factor 的
    short_to_files = {}
    for f in files:
        short = re.sub(r'^(sh|sz|bj)', '', f.stem)
        short_to_files.setdefault(short, []).append(f.stem)

    for adj_fp in ADJ.glob('*.parquet'):
        short = adj_fp.stem
        codes = short_to_files.get(short, [])
        if not codes:
            no_adj += 1
            continue
        code = codes[0]
        scanned += 1
        hits = screen_one_qfq(code, name_map, full_st_set)
        all_hits.extend(hits)
        if scanned % 500 == 0:
            elapsed = (datetime.now() - t0).total_seconds()
            print(f"  [{scanned}] {elapsed:.0f}s 已扫, hits={len(all_hits)}")

    print(f"\nv2 扫描完成: {scanned} 只股, {len(all_hits)} 个翻倍样本")
    print(f"耗时: {(datetime.now() - t0).total_seconds():.0f}s")

    # 保存 v2 详细结果
    df_v2 = pd.DataFrame(all_hits)
    if len(df_v2) > 0:
        df_v2 = df_v2.sort_values(['year', 'code', 'start_date']).reset_index(drop=True)
    csv_v2 = OUT_DIR / 'double_up_v2_2020_2026_detail.csv'
    df_v2.to_csv(csv_v2, index=False, encoding='utf-8-sig')
    print(f"\n✓ v2 明细: {csv_v2} ({len(df_v2)} 行)")

    # ====== v1 vs v2 对比 ======
    print("\n=== v1 vs v2 对比 ===")
    if not V1_DETAIL.exists():
        print(f"❌ v1 结果不存在: {V1_DETAIL}")
        return

    df_v1 = pd.read_csv(V1_DETAIL, dtype={'code': str})
    print(f"  v1 总样本: {len(df_v1)}")
    print(f"  v2 总样本: {len(df_v2)}")
    print(f"  v1 涉及标的: {df_v1['code'].nunique()}")
    print(f"  v2 涉及标的: {df_v2['code'].nunique()}")

    # 找 key (code + start_date)
    df_v1['key'] = df_v1['code'] + '|' + df_v1['start_date']
    df_v2['key'] = df_v2['code'] + '|' + df_v2['start_date']

    v1_keys = set(df_v1['key'])
    v2_keys = set(df_v2['key'])
    common = v1_keys & v2_keys
    only_v1 = v1_keys - v2_keys  # v1 是但 v2 不是 → 不复权假翻倍
    only_v2 = v2_keys - v1_keys  # v2 是但 v1 不是 → 不复权漏判

    print(f"  v1∩v2 (共同样本): {len(common)}")
    print(f"  only v1 (不复权假阳性): {len(only_v1)}")
    print(f"  only v2 (不复权漏判): {len(only_v2)}")

    # 共同样本的涨幅差异
    df_common_v1 = df_v1[df_v1['key'].isin(common)].set_index('key')
    df_common_v2 = df_v2[df_v2['key'].isin(common)].set_index('key')
    df_common = df_common_v1[['code', 'name', 'start_date', 'pct_change']].rename(columns={'pct_change': 'pct_v1_unadj'})
    df_common['pct_v2_qfq'] = df_common_v2['pct_change']
    df_common['pct_diff'] = (df_common['pct_v2_qfq'] - df_common['pct_v1_unadj']).round(2)

    print("\n  共同样本涨幅差异:")
    print(f"    平均差异: {df_common['pct_diff'].mean():+.2f} pct")
    print(f"    中位数差异: {df_common['pct_diff'].median():+.2f} pct")
    print(f"    最大正差: {df_common['pct_diff'].max():+.2f} pct (v2 比 v1 大)")
    print(f"    最大负差: {df_common['pct_diff'].min():+.2f} pct (v2 比 v1 小)")
    print(f"    abs(pct_diff) > 5 的样本: {(df_common['pct_diff'].abs() > 5).sum()} ({(df_common['pct_diff'].abs() > 5).mean()*100:.1f}%)")

    # 假阳性分析 (only v1)
    if len(only_v1) > 0:
        df_only_v1 = df_v1[df_v1['key'].isin(only_v1)][['code', 'name', 'start_date', 'peak_date', 'pct_change']].copy()
        df_only_v1 = df_only_v1.rename(columns={'pct_change': 'v1_pct'})
        df_only_v1 = df_only_v1.sort_values('v1_pct', ascending=False)
        csv_only_v1 = OUT_DIR / 'double_up_only_v1_false_positive.csv'
        df_only_v1.to_csv(csv_only_v1, index=False, encoding='utf-8-sig')
        print(f"\n  only_v1 (前复权后非翻倍 = 不复权假阳性): {len(df_only_v1)} 个")
        print(f"  → {csv_only_v1}")

    # 漏判分析 (only v2)
    if len(only_v2) > 0:
        df_only_v2 = df_v2[df_v2['key'].isin(only_v2)][['code', 'name', 'start_date', 'peak_date', 'pct_change']].copy()
        df_only_v2 = df_only_v2.rename(columns={'pct_change': 'v2_pct'})
        df_only_v2 = df_only_v2.sort_values('v2_pct', ascending=False)
        csv_only_v2 = OUT_DIR / 'double_up_only_v2_missed.csv'
        df_only_v2.to_csv(csv_only_v2, index=False, encoding='utf-8-sig')
        print(f"\n  only_v2 (前复权后翻倍但不复权未识别 = 不复权漏判): {len(df_only_v2)} 个")
        print(f"  → {csv_only_v2}")

    # 共同样本涨幅差异
    cmp_csv = OUT_DIR / 'double_up_v2_compare.csv'
    df_common.reset_index().to_csv(cmp_csv, index=False, encoding='utf-8-sig')
    print(f"\n  v1 vs v2 涨幅差异表: {cmp_csv}")

    # ====== Markdown 报告 ======
    md_path = OUT_DIR / f'double_up_v2_report_{datetime.now().strftime("%Y%m%d_%H%M")}.md'
    with md_path.open('w', encoding='utf-8') as f:
        f.write("# 方案 C v2: 前复权价翻倍筛选 vs v1 对比\n\n")
        f.write(f"> 生成: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"> v1: 不复权价（{len(df_v1)} 样本, {df_v1['code'].nunique()} 只）\n")
        f.write(f"> v2: 前复权价（{len(df_v2)} 样本, {df_v2['code'].nunique()} 只）\n")
        f.write("> 时间窗口: 2020-2026-09-22\n\n")

        f.write("## 关键对比\n\n")
        f.write("| 维度 | v1 (不复权) | v2 (前复权) | 差异 |\n|---|---|---|---|\n")
        f.write(f"| 翻倍样本数 | {len(df_v1)} | {len(df_v2)} | {len(df_v2)-len(df_v1):+d} ({(len(df_v2)-len(df_v1))/len(df_v1)*100:+.1f}%) |\n")
        f.write(f"| 涉及标的数 | {df_v1['code'].nunique()} | {df_v2['code'].nunique()} | {df_v2['code'].nunique()-df_v1['code'].nunique():+d} |\n")
        f.write(f"| 共同样本 | {len(common)} | - | - |\n")
        f.write(f"| v1 假阳性 | {len(only_v1)} ({len(only_v1)/len(df_v1)*100:.1f}%) | - | 复权后不再翻倍 |\n")
        f.write(f"| v2 漏判 | {len(only_v2)} ({len(only_v2)/len(df_v2)*100:.1f}%) | - | 不复权价未达 2× |\n\n")

        f.write("## 共同样本涨幅差异 (v2 - v1)\n\n")
        f.write(f"- 平均差异: **{df_common['pct_diff'].mean():+.2f} pct**\n")
        f.write(f"- 中位数差异: {df_common['pct_diff'].median():+.2f} pct\n")
        f.write(f"- 最大正差 (v2 更大): {df_common['pct_diff'].max():+.2f} pct\n")
        f.write(f"- 最大负差 (v2 更小): {df_common['pct_diff'].min():+.2f} pct\n")
        f.write(f"- **|差异| > 5 pct**: {(df_common['pct_diff'].abs() > 5).sum()} 样本 ({(df_common['pct_diff'].abs() > 5).mean()*100:.1f}%)\n")
        f.write(f"- **|差异| > 10 pct**: {(df_common['pct_diff'].abs() > 10).sum()} 样本 ({(df_common['pct_diff'].abs() > 10).mean()*100:.1f}%)\n\n")

        f.write("## 偏差结论\n\n")
        delta_pct = len(only_v1) / len(df_v1) * 100 if len(df_v1) else 0
        if delta_pct < 5:
            f.write(f"✅ **方案 C v1 不复权偏差 <5%**：假阳性率 {delta_pct:.1f}% 极低\n")
        else:
            f.write(f"⚠️ **方案 C v1 不复权偏差 {delta_pct:.1f}%**：需要重看 v1 结果\n")
        f.write("- 2020 后送转股极少 → 不复权偏差小\n")
        f.write("- v1 仍可用，但**重要决策请用 v2（前复权）**\n\n")

        if len(only_v1) > 0 and len(df_only_v1) > 0:
            f.write("## v1 假阳性 Top 10（不复权显示翻倍但复权后没有）\n\n")
            f.write("| 代码 | 名称 | 起始日 | 峰值日 | v1 涨幅 |\n|---|---|---|---|---|\n")
            for _, r in df_only_v1.head(10).iterrows():
                f.write(f"| {r['code']} | {r['name']} | {r['start_date']} | {r['peak_date']} | {r['v1_pct']:.1f}% |\n")
            f.write("\n")

        if len(only_v2) > 0 and len(df_only_v2) > 0:
            f.write("## v2 漏判 Top 10（前复权翻倍但不复权未达 100%）\n\n")
            f.write("| 代码 | 名称 | 起始日 | 峰值日 | v2 涨幅 |\n|---|---|---|---|---|\n")
            for _, r in df_only_v2.head(10).iterrows():
                f.write(f"| {r['code']} | {r['name']} | {r['start_date']} | {r['peak_date']} | {r['v2_pct']:.1f}% |\n")
            f.write("\n")

        f.write("## 输出文件\n\n")
        f.write(f"- `{csv_v2.name}` - v2 明细（{len(df_v2)} 行）\n")
        f.write(f"- `{cmp_csv.name}` - v1 vs v2 涨幅差异\n")
        f.write("- `double_up_only_v1_false_positive.csv` - v1 假阳性\n")
        f.write("- `double_up_only_v2_missed.csv` - v2 漏判\n")
        f.write(f"- `{md_path.name}` - 本报告\n")

    print(f"\n✓ 报告: {md_path}")
    print("\n=== 完成 ===")


if __name__ == '__main__':
    main()
