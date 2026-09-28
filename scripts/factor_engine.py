"""
因子计算引擎（事件驱动版）
==========================

设计：不在"全市场 × 每日"上算因子矩阵（会达 500MB+），
     而是在"事件日"提取因子值：
       正样本：(code, signal_date, label=1) 取自翻倍样本 start_date
       负样本：随机采样同期未翻倍股（label=0），按 1:1 配对

输入:
- parquet/daily/{code}.parquet: 行情
- adj_factor/{short}.parquet: 复权因子（可选）
- double_up/double_up_v2_2020_2026_detail.csv: 22,305 笔翻倍样本（标签）

输出:
- data/factors/event_factors.parquet: ~44k 行事件 × 30 列因子
- data/factors/factor_meta.json: 因子元信息（含义/计算口径/缺失率）

执行:
- 默认全市场 8035 只，跑完约 25-35 分钟
- 用 --limit 30 跑小样本验证

依赖:
- 仅使用 daily (8,035 只) + adj_factor (5,565 只)
- 资金/情绪因子因数据未覆盖（仅核心 300）暂未实现
- 北交所标的必须有 adj 才计算（保持与翻倍样本一致口径）
"""
import argparse
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path('/home/jiuben/tdx-data-feed')
PARQUET = ROOT / 'data/parquet/daily'
ADJ_DIR = ROOT / 'data/adj_factor'
ALL_STOCKS = ROOT / 'data/all_a_stocks.json'
DOUBLE_UP = ROOT / 'data/double_up/double_up_v2_2020_2026_detail.csv'
OUT_DIR = ROOT / 'data/factors'
ST_VAULT = Path('/home/jiuben/StockVault/01-标的')


def get_limit(code: str) -> float:
    short = code[2:]
    if code.startswith('bj'):
        return 0.295
    if code.startswith('sz'):
        if short.startswith('30') or short.startswith('20'):
            return 0.195
        return 0.095
    if code.startswith('sh'):
        if short.startswith('688'):
            return 0.195
        return 0.095
    return 0.095


def get_board(code: str) -> str:
    short = code[2:]
    if code.startswith('bj'):
        return 'bj'
    if code.startswith('sz') and short.startswith('300'):
        return 'sz300'
    if code.startswith('sh') and short.startswith('688'):
        return 'sh688'
    if code.startswith('sh') and (short.startswith('60') or short.startswith('90')):
        return 'sh_main'
    if code.startswith('sz') and short.startswith('00'):
        return 'sz_main'
    return 'other'


def parse_daily(fp: Path) -> pd.DataFrame | None:
    try:
        df = pd.read_parquet(fp, columns=['date', 'open', 'high', 'low', 'close', 'volume', 'amount'])
    except Exception:
        return None
    if df.empty or 'close' not in df.columns:
        return None
    if df['date'].dtype in ('int64', 'int32'):
        df['date'] = pd.to_datetime(df['date'].astype(str), format='%Y%m%d')
    else:
        df['date'] = pd.to_datetime(df['date'], errors='coerce')
    df = df.dropna(subset=['date', 'close']).sort_values('date').reset_index(drop=True)
    return df


def calc_window_factors(df: pd.DataFrame, code: str) -> pd.DataFrame:
    """对单只股票的日 K 一次性算全部因子（向量化）"""
    out = pd.DataFrame(index=df.index)
    out['close'] = df['close']
    out['high'] = df['high']
    out['low'] = df['low']
    out['volume'] = df['volume']
    out['amount'] = df['amount']

    pct = df['close'].pct_change()
    out['pct_chg'] = pct
    out['limit'] = get_limit(code)

    # ===== 量价因子 =====
    for w in (5, 10, 20):
        out[f'limit_up_{w}d'] = (pct >= out['limit'] - 0.001).rolling(w, min_periods=1).sum()
        out[f'pct_chg_{w}d'] = df['close'].pct_change(w)
        out[f'amount_{w}d_avg'] = df['amount'].rolling(w, min_periods=1).mean()
        out[f'volume_{w}d_avg'] = df['volume'].rolling(w, min_periods=1).mean()
        out[f'high_low_{w}d'] = (
            (df['high'].rolling(w, min_periods=1).max() - df['low'].rolling(w, min_periods=1).min())
            / df['close']
        )
        ma = df['close'].rolling(w, min_periods=1).mean()
        out[f'price_break_ma{w}'] = df['close'] / ma - 1

    # 量比 5/20
    out['volume_ratio_5d_20d'] = (
        df['volume'].rolling(5, min_periods=1).mean()
        / df['volume'].rolling(20, min_periods=1).mean()
    )

    # 连板数（修复版：按 limit 计算）
    is_up_limit = (pct >= out['limit'] - 0.001).fillna(False).values
    grp = np.cumsum(~is_up_limit)
    series = pd.Series(is_up_limit.astype(int))
    out['continuous_limit'] = series.groupby(grp).cumsum()

    # ===== 趋势因子 =====
    out['ma5_ma20_diff'] = (
        df['close'].rolling(5, min_periods=1).mean()
        / df['close'].rolling(20, min_periods=1).mean() - 1
    )
    out['close_pos_in_20d_range'] = (
        (df['close'] - df['low'].rolling(20, min_periods=1).min())
        / (
            df['high'].rolling(20, min_periods=1).max()
            - df['low'].rolling(20, min_periods=1).min()
        )
    )

    # ===== 波动率 =====
    out['volatility_20d'] = pct.rolling(20, min_periods=5).std()

    return out


def extract_factors_at_date(df_full: pd.DataFrame, factors: pd.DataFrame,
                            signal_date: pd.Timestamp, offset_days: int = 0) -> dict | None:
    """在指定日期提取一行因子值
    offset_days: 提前 N 日提取（默认 0 = 当日；如要"翻倍前 10 日"设 10）
    """
    target_date = signal_date - pd.Timedelta(days=offset_days)
    if target_date not in df_full['date'].values:
        idx = (df_full['date'] - target_date).abs().idxmin()
    else:
        idx = df_full.index[df_full['date'] == target_date][0]

    row = factors.loc[idx]
    if pd.isna(row['close']):
        return None

    days_since_first = (signal_date - df_full['date'].iloc[0]).days

    out = {
        'close': float(row['close']),
        'amount_5d_avg': float(row['amount_5d_avg']) if not pd.isna(row['amount_5d_avg']) else np.nan,
        'amount_20d_avg': float(row['amount_20d_avg']) if not pd.isna(row['amount_20d_avg']) else np.nan,
        'volume_ratio_5d_20d': float(row['volume_ratio_5d_20d']),
        'pct_chg_5d': float(row['pct_chg_5d']) if not pd.isna(row['pct_chg_5d']) else np.nan,
        'pct_chg_10d': float(row['pct_chg_10d']) if not pd.isna(row['pct_chg_10d']) else np.nan,
        'pct_chg_20d': float(row['pct_chg_20d']) if not pd.isna(row['pct_chg_20d']) else np.nan,
        'limit_up_5d': float(row['limit_up_5d']),
        'limit_up_10d': float(row['limit_up_10d']),
        'limit_up_20d': float(row['limit_up_20d']),
        'high_low_5d': float(row['high_low_5d']) if not pd.isna(row['high_low_5d']) else np.nan,
        'high_low_20d': float(row['high_low_20d']) if not pd.isna(row['high_low_20d']) else np.nan,
        'price_break_ma5': float(row['price_break_ma5']),
        'price_break_ma10': float(row['price_break_ma10']),
        'price_break_ma20': float(row['price_break_ma20']),
        'continuous_limit': float(row['continuous_limit']),
        'ma5_ma20_diff': float(row['ma5_ma20_diff']),
        'close_pos_in_20d_range': float(row['close_pos_in_20d_range']),
        'volatility_20d': float(row['volatility_20d']) if not pd.isna(row['volatility_20d']) else np.nan,
        'days_since_ipo': days_since_first,
    }
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--limit', type=int, default=0, help='仅处理前 N 只股票（验证用）')
    parser.add_argument('--start-date', default='2001-01-01', help='训练期起始日')
    parser.add_argument('--neg-ratio', type=int, default=1, help='负样本/正样本比')
    parser.add_argument('--signal-offset', type=int, default=0,
                        help='提前 N 日提取因子（避免 leakage，默认 0）')
    parser.add_argument('--only-pos', action='store_true', help='只算正样本')
    args = parser.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    t0 = time.time()

    # 加载股票清单
    name_map = json.load(open(ALL_STOCKS))
    short_to_codes = {}
    for fp in PARQUET.glob('*.parquet'):
        short_to_codes.setdefault(fp.stem[2:], []).append(fp.stem)

    # ST 集合
    st_set = set()
    for md in ST_VAULT.glob('*.md'):
        m = re.match(r'(\d{6})', md.stem)
        if m:
            short = m.group(1)
            if '*ST' in md.stem or md.stem.startswith('ST'):
                st_set.add(short)

    # 加载翻倍样本（正样本）
    print(f'[{datetime.now():%H:%M:%S}] 加载翻倍样本...')
    pos_df = pd.read_csv(DOUBLE_UP)
    pos_df['start_date'] = pd.to_datetime(pos_df['start_date'])
    pos_df = pos_df[pos_df['start_date'] >= pd.Timestamp(args.start_date)].reset_index(drop=True)
    print(f'  正样本（start_date >= {args.start_date}）: {len(pos_df):,} 笔')

    files = sorted(PARQUET.glob('*.parquet'))
    if args.limit > 0:
        files = files[:args.limit]
        codes_in_files = set(f.stem for f in files)
        pos_df = pos_df[pos_df['code'].isin(codes_in_files)].reset_index(drop=True)
        print(f'  限定至 {args.limit} 只后正样本: {len(pos_df):,} 笔')
    print(f'  待扫描股票: {len(files)}')

    records = []
    scanned = 0
    cache = {}  # code -> (df_full, factors)

    for fp in files:
        code = fp.stem
        scanned += 1
        if scanned % 200 == 0:
            elapsed = time.time() - t0
            print(f'  [{scanned}/{len(files)}] {code} | 累计事件 {len(records):,} | 耗时 {elapsed:.0f}s')

        df_full = parse_daily(fp)
        if df_full is None or len(df_full) < 60:
            continue
        try:
            factors = calc_window_factors(df_full, code)
        except Exception as e:
            continue
        cache[code] = (df_full, factors)

        # 提取该股所有翻倍 start_date 的因子
        own_pos = pos_df[pos_df['code'] == code]
        for _, row in own_pos.iterrows():
            feat = extract_factors_at_date(df_full, factors, row['start_date'],
                                            offset_days=args.signal_offset)
            if feat is None:
                continue
            feat['code'] = code
            feat['name'] = name_map.get(code[2:], '?')
            feat['signal_date'] = row['start_date']
            feat['label'] = 1
            feat['is_st'] = code[2:] in st_set
            feat['board'] = get_board(code)
            feat['limit'] = get_limit(code)
            feat['pct_change_20d_actual'] = float(row['pct_change'])
            records.append(feat)

    print(f'\n[{datetime.now():%H:%M:%S}] 正样本提取完成: {len(records):,} 行')

    # 负样本：每个正样本事件配对一个同期"未翻倍"股（1:1）
    if not args.only_pos:
        np.random.seed(42)
        neg_records = []
        all_codes = [f.stem for f in files]

        # 构建 code -> 翻倍日期集合（用于排除该股本身翻倍的日期）
        code_to_dates = pos_df.groupby('code')['start_date'].apply(set).to_dict()

        for _, pos_row in pos_df.iterrows():
            pos_code = pos_row['code']
            pos_date = pos_row['start_date']
            # 候选：该日期未翻倍的所有股票
            candidates = [
                c for c in all_codes
                if c != pos_code and pos_date not in code_to_dates.get(c, set())
            ]
            if not candidates:
                continue
            # 采 neg_ratio 个（默认 1 个 = 1:1 配对）
            sample_size = min(args.neg_ratio, len(candidates))
            sampled = np.random.choice(candidates, size=sample_size, replace=False)

            for code in sampled:
                if code not in cache:
                    fp = PARQUET / f'{code}.parquet'
                    df_full = parse_daily(fp)
                    if df_full is None or len(df_full) < 60:
                        continue
                    try:
                        factors = calc_window_factors(df_full, code)
                    except Exception:
                        continue
                    cache[code] = (df_full, factors)

                df_full, factors = cache[code]
                feat = extract_factors_at_date(df_full, factors, pos_date,
                                                offset_days=args.signal_offset)
                if feat is None:
                    continue
                feat['code'] = code
                feat['name'] = name_map.get(code[2:], '?')
                feat['signal_date'] = pos_date
                feat['label'] = 0
                feat['is_st'] = code[2:] in st_set
                feat['board'] = get_board(code)
                feat['limit'] = get_limit(code)
                feat['pct_change_20d_actual'] = np.nan
                neg_records.append(feat)

        records.extend(neg_records)
        print(f'[{datetime.now():%H:%M:%S}] 负样本采样完成: {len(neg_records):,} 行')

    # 写出
    df_out = pd.DataFrame(records)
    out_fp = OUT_DIR / 'event_factors.parquet'
    df_out.to_parquet(out_fp, index=False)
    print(f'\n输出: {out_fp}  ({len(df_out):,} 行 × {len(df_out.columns)} 列)')
    print(f'  正样本: {(df_out["label"]==1).sum():,}  负样本: {(df_out["label"]==0).sum():,}')

    # 因子元信息
    factor_cols = [c for c in df_out.columns if c not in (
        'code', 'name', 'signal_date', 'label', 'is_st', 'board', 'limit',
        'pct_change_20d_actual'
    )]
    meta = {
        'generated_at': datetime.now().isoformat(),
        'total_rows': len(df_out),
        'pos': int((df_out['label']==1).sum()),
        'neg': int((df_out['label']==0).sum()),
        'train_start': args.start_date,
        'factor_list': factor_cols,
        'missing_rate': {c: float(df_out[c].isna().mean()) for c in factor_cols},
    }
    with open(OUT_DIR / 'factor_meta.json', 'w') as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)
    print(f'  因子: {len(factor_cols)} 个')
    print(f'  缺失率 Top5: {sorted(meta["missing_rate"].items(), key=lambda x: -x[1])[:5]}')

    elapsed = time.time() - t0
    print(f'\n总耗时: {elapsed:.1f}s ({elapsed/60:.1f} min)')


if __name__ == '__main__':
    main()