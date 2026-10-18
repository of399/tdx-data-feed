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


def norm_code(code: str) -> str:
    """剥离 sh/sz/bj 前缀，返回裸 6 位代码。

    data/parquet/daily 文件名实为裸代码（600825）+ 北交所 bj 前缀（bj920000），
    而非 sh/sz 前缀；统一归一后 name_map / ST 集合 / 翻倍样本匹配才一致。

    审计 Round2 复核注记(2026-10-18): 与 scripts/_common.norm_code 语义不同——
    本版仅"剥前缀"(无 lower/zfill), 输入是内部生成的文件名/裸代码;
    _common 版是"规范化"(lower+zfill6)给 CLI 用户输入用。契约不同, 勿盲目统一。
    """
    return re.sub(r"^(sh|sz|bj)", "", code)


def resolve_parquet(code: str) -> Path:
    """code 为裸 6 位；北交所文件为 bj 前缀，主板为裸名。返回实际路径。"""
    bare = norm_code(code)
    for cand in (bare, f"bj{bare}"):
        p = PARQUET / f"{cand}.parquet"
        if p.exists():
            return p
    return PARQUET / f"{bare}.parquet"


def get_limit(code: str) -> float:
    short = norm_code(code)
    if short.startswith("920") or short.startswith("8"):
        return 0.295                      # 北交所
    if short.startswith("688") or short.startswith("300") or short.startswith("301") or short.startswith("200"):
        return 0.195                      # 科创板 / 创业板 / 中小板
    return 0.095                          # 沪深主板


def get_board(code: str) -> str:
    short = norm_code(code)
    if short.startswith("920") or short.startswith("8"):
        return "bj"
    if short.startswith("688"):
        return "sh688"
    if short.startswith("300") or short.startswith("301"):
        return "sz300"
    if short.startswith("60") or short.startswith("90"):
        return "sh_main"
    if short.startswith("00") or short.startswith("20") or short.startswith("30"):
        return "sz_main"
    return "other"


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


def cross_section_mode(start: str, end: str, limit: int = 0):
    """--cross-section：在调仓日（周五）构建全市场截面因子快照（long 格式落盘）。

    复用 quant/multifactor.factors 的五维因子与 z-score，默认事件驱动逻辑不变。
    落盘 data/factors/cross_section_YYYYMMDD.parquet（date,code,factor,z），
    并输出关键因子截面覆盖率中位数（<0.6 的因子在合成中将被剔除）。
    """
    sys.path.insert(0, str(ROOT))
    from quant.multifactor.factors import (
        build_factor_panel,
        compute_stock_raw,
        load_lhb_trailing,
        parse_daily,
        zscore_panel,
    )
    from quant.multifactor.valuation_data import ValuationConfig, ValuationProvider

    files = sorted(PARQUET.glob("*.parquet"))
    if limit:
        files = files[:limit]
    cache: dict = {}
    for fp in files:
        code = fp.stem
        df = parse_daily(fp)
        if df is None or len(df) < 190:
            continue
        cache[code] = compute_stock_raw(df, code)
    print(f"[{datetime.now():%H:%M:%S}] 载入 {len(cache)} 只因子序列")

    lhb = load_lhb_trailing()
    valp = ValuationProvider(ValuationConfig(mode="auto"))
    rbdates = pd.date_range(start, end, freq="W-FRI")
    rows, coverages = [], []
    t0 = time.time()
    for i, d in enumerate(rbdates):
        panel = build_factor_panel(cache, d, lhb, valp)
        if valp.mode != "proxy":
            panel = panel.drop(columns=["val_proxy"], errors="ignore")
        keep = panel.dropna(subset=["mom_60d", "zt_density_20d", "fundflow_divergence_20d"])
        if valp.mode == "proxy":
            keep = keep.dropna(subset=["val_proxy"])
        else:
            keep = keep.dropna(subset=["pe_ttm", "pb_mrq"], how="all")
        if keep.empty:
            continue
        coverages.append(keep.notna().mean())
        z = zscore_panel(keep)
        for code, row in z.iterrows():
            for fac, v in row.items():
                if pd.notna(v):
                    rows.append((str(d)[:10], code, fac, float(v)))
        if i % 20 == 0:
            print(f"  [{i}/{len(rbdates)}] {d:%Y-%m-%d} 覆盖 {len(keep)} 只")
    out = pd.DataFrame(rows, columns=["date", "code", "factor", "z"])
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    fp_out = OUT_DIR / f"cross_section_{pd.Timestamp.now():%Y%m%d}.parquet"
    out.to_parquet(fp_out)
    print(f"[{datetime.now():%H:%M:%S}] 快照落盘: {fp_out}  ({len(out):,} 行)")
    if coverages:
        cov = pd.DataFrame(coverages).median().sort_values()
        print("  关键因子截面覆盖率中位数（<0.6 将剔除）:")
        for k, v in cov.items():
            flag = "✗" if v < 0.6 else "✓"
            print(f"    {flag} {k}: {v:.2f}")
    print(f"  耗时 {time.time() - t0:.0f}s")
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--limit', type=int, default=0, help='仅处理前 N 只股票（验证用）')
    parser.add_argument('--start-date', default='2001-01-01', help='训练期起始日')
    parser.add_argument('--neg-ratio', type=int, default=1, help='负样本/正样本比')
    parser.add_argument('--signal-offset', type=int, default=0,
                        help='提前 N 日提取因子（避免 leakage，默认 0）')
    parser.add_argument('--only-pos', action='store_true', help='只算正样本')
    parser.add_argument('--cross-section', action='store_true',
                        help='截面快照模式：调仓日（周五）构建全市场五维因子 long 快照落盘')
    parser.add_argument('--cs-start', default='2020-01-01', help='--cross-section 起始日')
    parser.add_argument('--cs-end', default='2026-10-14', help='--cross-section 结束日')
    args = parser.parse_args()

    if args.cross_section:
        cross_section_mode(args.cs_start, args.cs_end, args.limit)
        return

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    t0 = time.time()

    # 加载股票清单
    name_map = json.load(open(ALL_STOCKS))

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

    files = sorted(p for p in PARQUET.glob('*.parquet') if '.bak' not in p.name)
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
        except Exception:
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
            feat['name'] = name_map.get(norm_code(code), '?')
            feat['signal_date'] = row['start_date']
            feat['label'] = 1
            feat['is_st'] = norm_code(code) in st_set
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
                    fp = resolve_parquet(code)
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
                feat['name'] = name_map.get(norm_code(code), '?')
                feat['signal_date'] = pos_date
                feat['label'] = 0
                feat['is_st'] = norm_code(code) in st_set
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
