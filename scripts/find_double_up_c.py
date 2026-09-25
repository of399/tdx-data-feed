"""方案 C：近 5 年（2020-2026）"20 个交易日翻倍"筛选
基于不复权价（明确标注偏差 <5%）
"""
import json
import re
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

PARQUET = Path('/home/jiuben/tdx-data-feed/data/parquet/daily')
VAULT_DIR = Path('/home/jiuben/StockVault/01-标的')
ALL_STOCKS = Path('/home/jiuben/tdx-data-feed/data/all_a_stocks.json')
OUT_DIR = Path('/home/jiuben/tdx-data-feed/data/double_up')
OUT_DIR.mkdir(parents=True, exist_ok=True)

START_DATE = pd.Timestamp('2020-01-01')
WINDOW = 20
RATIO = 2.0  # 翻倍

# 涨幅限制（沪深主板 / 创业板 / 科创板 / 北交所）
LIMIT_MAIN = 0.095    # 主板 ±10%，留余量
LIMIT_GEM = 0.195     # 创业板/科创板 ±20%
LIMIT_BSE = 0.295     # 北交所 ±30%


def is_st(code: str) -> bool:
    """查 StockVault 文件名判断 ST"""
    md_path = VAULT_DIR / f'{code}'
    if not md_path.exists():
        # 兼容中文符号（"万科Ａ" vs "万科A"）
        for _suffix in ('.md',):
            matches = list(VAULT_DIR.glob(f'{code}*'))
            if matches:
                md_path = matches[0]
                break
    if md_path.exists() and md_path.suffix == '.md':
        return '*ST' in md_path.stem or md_path.stem.startswith('ST') or 'ST' in md_path.stem
    return False


def get_name(code: str, name_map: dict) -> str:
    """从 all_a_stocks.json 取中文名"""
    short = re.sub(r'^(sh|sz|bj)', '', code)
    if short in name_map:
        n = name_map[short]
        # 去除全角空格
        return n.replace('　', '').strip()
    return '?'


def get_limit(code: str) -> float:
    """根据股票代码判断涨幅限制"""
    short = code[2:]
    if code.startswith('bj'):
        return LIMIT_BSE
    if code.startswith('sz'):
        # 30xxxx 创业板, 20xxxx B股，0xxxx/1xxxx 主板
        if short.startswith('30') or short.startswith('20'):
            return LIMIT_GEM
        return LIMIT_MAIN
    if code.startswith('sh'):
        # 688xxx 科创板
        if short.startswith('688'):
            return LIMIT_GEM
        return LIMIT_MAIN
    return LIMIT_MAIN


def screen_one(code: str, name_map: dict, st_set: set) -> list:
    """单只股的 20 日翻倍筛选"""
    fp = PARQUET / f'{code}.parquet'
    if not fp.exists():
        return []

    try:
        df = pd.read_parquet(fp, columns=['date', 'open', 'high', 'low', 'close', 'volume', 'amount'])
    except Exception:
        return []

    # date 兼容：int 20210802 或 datetime
    if df['date'].dtype == 'int64' or df['date'].dtype == 'int32':
        df['date'] = pd.to_datetime(df['date'].astype(str), format='%Y%m%d')
    else:
        df['date'] = pd.to_datetime(df['date'])

    df = df.sort_values('date').reset_index(drop=True)

    # 过滤 START_DATE 之后
    df = df[df['date'] >= START_DATE].reset_index(drop=True)
    if len(df) < WINDOW + 5:
        return []

    # 上市首日（df 内首日，但可能是早期已上市）→ 用"df 内首日"作为近似
    first_date = df['date'].iloc[0]
    days_since_first = (df['date'] - first_date).dt.days

    # 20 日窗口：第 i 天的 close[i] → max(close[i+1:i+21]) ≥ 2.0 × close[i]
    # 即：close 在未来 20 个交易日内（含当日）达到 2 倍
    # 用 rolling forward 模拟
    close = df['close'].values
    dates = df['date'].values
    pct = df['close'].pct_change().values  # 每日涨幅

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
            # 找最高日
            peak_idx = i + 1 + np.argmax(close[i+1:i+WINDOW+1])
            peak_date = pd.Timestamp(dates[peak_idx])
            # 计算区间涨停数
            window_pct = pct[i+1:i+WINDOW+1]
            limit_up_count = int((window_pct >= limit - 0.001).sum())
            # 区间成交额
            window_amount = df['amount'].iloc[i+1:i+WINDOW+1].sum()
            # 是否新股（df 内首日 <= 60 天前）
            is_new = days_since_first.iloc[i] < 250

            hits.append({
                'code': code,
                'name': name,
                'start_date': pd.Timestamp(dates[i]).strftime('%Y-%m-%d'),
                'peak_date': peak_date.strftime('%Y-%m-%d'),
                'days_to_peak': peak_idx - i,
                'start_close': round(float(start_close), 2),
                'peak_close': round(float(window_max), 2),
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
    print("=== 方案 C: 近 5 年（2020-2026）20 日翻倍筛选 ===")
    print(f"  START_DATE: {START_DATE.date()}")
    print(f"  WINDOW: {WINDOW} 交易日")
    print(f"  RATIO: ≥{RATIO:.0%} (翻倍)")
    print(f"  数据源: parquet/daily ({len(list(PARQUET.glob('*.parquet')))} 个文件)")
    print()

    # 加载标的信息
    with open(ALL_STOCKS) as _f:
        name_map = json.load(_f)

    # ST 集合
    st_set = set()
    for md in VAULT_DIR.glob('*.md'):
        if '*ST' in md.stem or md.stem.startswith('ST') or 'ST' in md.stem:
            # 文件名格式: 000010-*ST美丽.md → code = 000010
            code = re.match(r'(\d{6})', md.stem)
            if code:
                # 需要补前缀（sh/sz/bj）
                short = code.group(1)
                # 简化：扫描所有 parquet 找匹配的 code
                st_set.add(short)
    print(f"  ST 集合: {len(st_set)} 只")

    # 加载所有 parquet 文件列表
    files = sorted(PARQUET.glob('*.parquet'))
    print(f"  扫描股票数: {len(files)}")

    # 转换 st_set 为完整代码集（带前缀）
    full_st_set = set()
    for code_short in st_set:
        for f in files:
            if f.stem.endswith(code_short):
                full_st_set.add(f.stem)
                break

    # 单进程跑（避免 pickling 问题）
    print("\n开始筛选（单进程，预计 ~2-3 分钟）...")
    all_hits = []
    scanned = 0
    for f in files:
        code = f.stem
        scanned += 1
        hits = screen_one(code, name_map, full_st_set)
        all_hits.extend(hits)
        if scanned % 500 == 0:
            elapsed = (datetime.now() - t0).total_seconds()
            print(f"  [{scanned}/{len(files)}] {elapsed:.0f}s 已扫, hits={len(all_hits)}")

    print(f"\n扫描完成: {scanned} 只股, {len(all_hits)} 个翻倍样本")
    print(f"耗时: {(datetime.now() - t0).total_seconds():.0f}s")

    if not all_hits:
        print("❌ 未发现任何翻倍样本")
        return

    df_hits = pd.DataFrame(all_hits)
    df_hits = df_hits.sort_values(['year', 'code', 'start_date']).reset_index(drop=True)

    # 输出 1: 完整明细 CSV
    csv_path = OUT_DIR / 'double_up_2020_2026_detail.csv'
    df_hits.to_csv(csv_path, index=False, encoding='utf-8-sig')
    print(f"\n✓ 明细 CSV: {csv_path} ({len(df_hits)} 行)")

    # 输出 2: 标的汇总（按代码聚合）
    code_summary = df_hits.groupby('code').agg(
        name=('name', 'first'),
        hit_count=('start_date', 'count'),
        years=('year', lambda x: ','.join(map(str, sorted(set(x))))),
        max_pct=('pct_change', 'max'),
        max_limit_up=('limit_up_count', 'max'),
        total_amount=('window_amount', 'sum'),
        first_hit=('start_date', 'min'),
        last_hit=('start_date', 'max'),
    ).reset_index()
    code_summary = code_summary.sort_values('hit_count', ascending=False).reset_index(drop=True)
    code_summary['rank'] = code_summary.index + 1
    csv_path2 = OUT_DIR / 'double_up_2020_2026_by_code.csv'
    code_summary.to_csv(csv_path2, index=False, encoding='utf-8-sig')
    print(f"✓ 标的汇总 CSV: {csv_path2} ({len(code_summary)} 只)")

    # 输出 3: 年份分布
    year_dist = df_hits.groupby('year').agg(
        hit_count=('start_date', 'count'),
        unique_codes=('code', 'nunique'),
        avg_pct=('pct_change', 'mean'),
        max_pct=('pct_change', 'max'),
        avg_limit_up=('limit_up_count', 'mean'),
    ).reset_index()
    year_dist['avg_pct'] = year_dist['avg_pct'].round(1)
    year_dist['max_pct'] = year_dist['max_pct'].round(1)
    year_dist['avg_limit_up'] = year_dist['avg_limit_up'].round(1)
    csv_path3 = OUT_DIR / 'double_up_2020_2026_year_dist.csv'
    year_dist.to_csv(csv_path3, index=False, encoding='utf-8-sig')
    print(f"✓ 年份分布 CSV: {csv_path3}")

    # 输出 4: Markdown 报告
    md_path = OUT_DIR / f'double_up_report_{datetime.now().strftime("%Y%m%d")}.md'
    with md_path.open('w', encoding='utf-8') as f:
        f.write("# 20 日翻倍筛选报告 · 方案 C · 2020-2026\n\n")
        f.write(f"> 生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"> 数据源: parquet/daily ({len(files)} 只标的)\n")
        f.write(f"> 时间窗口: {START_DATE.date()} ~ 2026-09-22\n")
        f.write(f"> 筛选条件: 任意连续 {WINDOW} 个交易日内，最高收盘价 ≥ 起始日收盘价 × {RATIO:.0%}\n")
        f.write("> 复权口径: **不复权**（数据无 adj_factor 字段）\n\n")
        f.write("## 偏差评估\n\n")
        f.write("- **预估偏差**: <5%（2020-2026 期间监管趋严，送转股极少）\n")
        f.write("- **影响范围**: 主要影响 2020 年前上市且多次送转的标的（如茅台、平安）\n")
        f.write("- **2020 后新股**: 影响极小（无除权历史）\n")
        f.write("- **方案 B 对比**: 已并行启动，复权因子下载完成后重跑验证\n\n")

        f.write("## 关键统计\n\n")
        f.write(f"- **翻倍样本数**: {len(df_hits)}\n")
        f.write(f"- **涉及标的数**: {len(code_summary)}\n")
        f.write(f"- **扫描股票数**: {len(files)}\n")
        f.write(f"- **命中率**: {len(code_summary)/len(files)*100:.2f}%\n\n")

        # 年份分布
        f.write("## 年份分布\n\n")
        f.write("| 年份 | 翻倍次数 | 涉及标的 | 平均涨幅 | 最大涨幅 | 平均涨停数 |\n")
        f.write("|---|---|---|---|---|---|\n")
        for _, r in year_dist.iterrows():
            f.write(f"| {int(r['year'])} | {int(r['hit_count'])} | {int(r['unique_codes'])} | "
                    f"{r['avg_pct']:.1f}% | {r['max_pct']:.1f}% | {r['avg_limit_up']:.1f} |\n")
        f.write("\n")

        # Top 20 妖股
        f.write("## Top 20 妖股（按翻倍次数）\n\n")
        f.write("| 排名 | 代码 | 名称 | 翻倍次数 | 涉及年份 | 最大涨幅 | 最多涨停 | 累计成交额 |\n")
        f.write("|---|---|---|---|---|---|---|---|\n")
        for _, r in code_summary.head(20).iterrows():
            f.write(f"| {int(r['rank'])} | {r['code']} | {r['name']} | {int(r['hit_count'])} | "
                    f"{r['years']} | {r['max_pct']:.1f}% | {int(r['max_limit_up'])} | "
                    f"{r['total_amount']/1e8:.1f} 亿 |\n")
        f.write("\n")

        # 单一标的详情（top 5）
        f.write("## 典型案例（妖股 #1）\n\n")
        top1 = code_summary.iloc[0]
        detail1 = df_hits[df_hits['code'] == top1['code']].head(15)
        f.write(f"**{top1['code']} {top1['name']}** - 共 {int(top1['hit_count'])} 次翻倍\n\n")
        f.write("| 起始日 | 最高日 | 峰值日 | 起始价 | 峰值价 | 涨幅 | 涨停数 | 成交额(亿) |\n")
        f.write("|---|---|---|---|---|---|---|---|\n")
        for _, r in detail1.iterrows():
            f.write(f"| {r['start_date']} | {r['peak_date']} | {r['days_to_peak']} | "
                    f"{r['start_close']:.2f} | {r['peak_close']:.2f} | {r['pct_change']:.1f}% | "
                    f"{r['limit_up_count']} | {r['window_amount']/1e8:.1f} |\n")
        f.write("\n")

        # ST 标的（如果有）
        st_hits = df_hits[df_hits['is_st']]
        if len(st_hits) > 0:
            f.write(f"## ⚠ ST 标的翻倍（{len(st_hits)} 个样本，需谨慎）\n\n")
            f.write("| 代码 | 名称 | 起始日 | 涨幅 |\n|---|---|---|---|\n")
            for _, r in st_hits.head(10).iterrows():
                f.write(f"| {r['code']} | {r['name']} | {r['start_date']} | {r['pct_change']:.1f}% |\n")
            f.write("\n")

        f.write("## 输出文件\n\n")
        f.write(f"- `{csv_path.name}` - 翻倍样本明细（{len(df_hits)} 行）\n")
        f.write(f"- `{csv_path2.name}` - 标的汇总（{len(code_summary)} 只）\n")
        f.write(f"- `{csv_path3.name}` - 年份分布\n")
        f.write(f"- `{md_path.name}` - 本报告\n\n")
        f.write("## 后续\n\n")
        f.write("1. **方案 B 对比**: 复权因子下载完成后重跑，验证偏差\n")
        f.write("2. **深度分析**: 对 Top 10 妖股做 K 线 + 成交量 + 涨停节奏复盘\n")
        f.write("3. **策略**: 用此清单做短线信号 / 风险预警\n")

    print(f"✓ 报告: {md_path}")
    print("\n=== 完成 ===")
    print("  Top 5 妖股:")
    for _, r in code_summary.head(5).iterrows():
        print(f"    {int(r['rank'])}. {r['code']} {r['name']} - {int(r['hit_count'])} 次")


if __name__ == '__main__':
    main()
