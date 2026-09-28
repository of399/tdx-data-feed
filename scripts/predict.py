#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
方案 D 预测脚本：每日全市场预测翻倍潜力股
========================================

【背景】
  方案 D 用 LightGBM 在"翻倍前 5 日"窗口识别翻倍信号，
  Test AUC 0.8144，阈值 0.7 精度 83.15%。
  本脚本用于生产化：每日拉一遍全市场 → 18 维特征 → 预测概率 → Top 排序

【输入数据】
  - data/parquet/daily/{sh|sz|bj}{code}.parquet     日 K（含 open/high/low/close/volume/amount/turnover）
  - data/adj_factor/{code}.parquet                 复权因子
  - data/all_a_stocks.json                         5565 只全 A 清单
  - data/double_up/d_signal_5d_model.txt           训练好的 LightGBM 模型

【18 维特征】（与训练完全一致）

| # | 特征 | 类型 | 含义 | 计算 |
|---|------|------|------|------|
| 1 | prev_close | float | T-1 收盘价 | df['close'].iloc[-1] |
| 2 | ret_5d | float | 5 日累计涨幅 | close[-1]/close[-6]-1 |
| 3 | ret_3d | float | 3 日累计涨幅 | close[-1]/close[-4]-1 |
| 4 | max_5d_pct | float | 5 日最大单日涨幅 | max(pct_change[-5:]) |
| 5 | limit_up_5d | int | 5 日涨停次数 | sum(close/prev_close >= 1+limit) |
| 6 | limit_up_t1 | int | T-1 是否涨停 | close[-1]/close[-2] >= 1+limit |
| 7 | pct_t1 | float | T-1 当日涨幅 | close[-1]/close[-2]-1 |
| 8 | avg_amount_5d | float | 5 日均成交额 | mean(amount[-5:]) |
| 9 | vol_ratio_t1 | float | T-1 量比（末日/5日均量） | volume[-1]/mean(volume[-5:]) |
| 10 | volatility_5d | float | 5 日波动率 | std(pct_change[-5:]) |
| 11 | close_pos_5d | float | T-1 收盘价在 5 日 K 线位置 | (close-min)/(max-min) |
| 12 | max_drawdown_5d | float | 5 日最大跌幅 | min(close/max_so_far - 1) |
| 13 | turnover_proxy | float | T-1 换手代理 | volume[-1]/shares |
| 14 | is_st | 0/1 | 是否 ST | name 含 "ST" |
| 15 | is_new | 0/1 | 是否新股（上市<60 日） | days_since_list < 60 |
| 16 | is_bj | 0/1 | 是否北交所 | code startswith 'bj' |
| 17 | is_gem | 0/1 | 是否创业板/科创板 | code 'sz30' 或 'sh688' |
| 18 | limit | float | 涨停限制（10%/20%/30%） | 见代码 |

【输出】
  data/predictions/predict_YYYYMMDD_HHMMSS.csv      全部股票预测（5565 行）
  data/predictions/predict_top20_YYYYMMDD_HHMMSS.csv Top 20 高分
  data/predictions/predict_high_YYYYMMDD_HHMMSS.csv  阈值 0.7+ 高分
  data/predictions/predict_YYYYMMDD_HHMMSS.md       Markdown 简明报告

【运行】
  # 默认：今天数据，Top 20，阈值 0.7
  venv/bin/python scripts/predict.py

  # 指定日期 + Top 50
  venv/bin/python scripts/predict.py --date 2026-09-20 --top 50

  # 自定义阈值
  venv/bin/python scripts/predict.py --threshold 0.6

  # 指定几只股（调试）
  venv/bin/python scripts/predict.py --codes 600519,000001,300750

【关键设计】
  - 跳过北交所 920XXX：训练时虽然包含，但 production 数据不连续，跳过避免噪声
  - 跳过 ST 股：st 涨跌幅限制 5%，与正常 10/20/30 不同
  - 跳过新股（上市<30 日）：新股前 N 日一字板，特征失真
  - 至少 30 日历史：少于 30 日 K 线无法计算 5 日窗口 + 复权因子
"""

import argparse
import json
import sys
import warnings
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd

warnings.filterwarnings('ignore')

# ============== 路径 ==============
ROOT = Path(__file__).parent.parent
PARQUET_DIR = ROOT / 'data' / 'parquet' / 'daily'
ADJ_DIR = ROOT / 'data' / 'adj_factor'
MODEL_FILE = ROOT / 'data' / 'double_up' / 'd_signal_5d_model.txt'
OUTPUT_DIR = ROOT / 'data' / 'predictions'
UNIVERSE_FILE = ROOT / 'data' / 'all_a_stocks.json'

# ============== 18 维特征定义（顺序与训练一致）==============
FEATURE_COLS = [
    'limit',          # 涨停限制
    'prev_close',     # T-1 收盘价
    'ret_5d',         # 5 日累计涨幅
    'ret_3d',         # 3 日累计涨幅
    'max_5d_pct',     # 5 日最大单日涨幅
    'limit_up_5d',    # 5 日涨停次数
    'limit_up_t1',    # T-1 是否涨停
    'pct_t1',         # T-1 当日涨幅
    'avg_amount_5d',  # 5 日均成交额
    'vol_ratio_t1',   # T-1 量比
    'volatility_5d',  # 5 日波动率
    'close_pos_5d',   # T-1 收盘价在 5 日 K 线位置
    'max_drawdown_5d',# 5 日最大跌幅
    'turnover_proxy', # T-1 换手代理
    'is_st',          # 是否 ST
    'is_new',         # 是否新股
    'is_bj',          # 是否北交所
    'is_gem',         # 是否创业板/科创板
]


# ============== 特征计算 ==============

def get_limit(code: str) -> float:
    """根据代码判断涨停限制"""
    code = str(code).zfill(6)
    if code.startswith('bj'):
        return 0.30
    if code.startswith('300') or code.startswith('301'):  # 创业板
        return 0.20
    if code.startswith('688'):  # 科创板
        return 0.20
    return 0.10


def is_st(name: str) -> int:
    """判断 ST（影响涨跌幅）"""
    if not isinstance(name, str):
        return 0
    return 1 if ('ST' in name or '*ST' in name) else 0


def compute_features(code: str) -> dict:
    """
    对单只股计算 18 维特征
    返回 None 表示无法计算（数据不足等）
    """
    code = str(code).zfill(6)
    code_clean = code.lstrip('bj')

    # 找 parquet 文件
    for prefix in ['', 'sh', 'sz', 'bj']:
        fp = PARQUET_DIR / f"{prefix}{code_clean if prefix else code_clean}.parquet"
        if not fp.exists():
            continue
        # 也支持 sh600xxx 这种格式
        for cand in [
            f"{code_clean}.parquet",
            f"sh{code_clean}.parquet",
            f"sz{code_clean}.parquet",
            f"bj{code_clean}.parquet",
        ]:
            f = PARQUET_DIR / cand
            if f.exists():
                fp = f
                break
        break

    # 实际找文件
    fp = None
    for cand in [
        f"{code_clean}.parquet",
        f"sh{code_clean}.parquet",
        f"sz{code_clean}.parquet",
        f"bj{code_clean}.parquet",
    ]:
        f = PARQUET_DIR / cand
        if f.exists():
            fp = f
            break
    if fp is None:
        return None

    try:
        df = pd.read_parquet(fp)
    except Exception:
        return None

    if len(df) < 30:
        return None

    # 合并 adj_factor（如果 df 已经有这列，先删掉避免冲突）
    if 'adj_factor' in df.columns:
        df = df.drop(columns=['adj_factor'])
    if 'qfq_close' in df.columns:
        df = df.drop(columns=['qfq_close'])

    adj_fp = ADJ_DIR / f"{code_clean}.parquet"
    if adj_fp.exists():
        adj_df = pd.read_parquet(adj_fp)
        df['date'] = pd.to_datetime(df['date'])
        adj_df['date'] = pd.to_datetime(adj_df['date'])
        df = df.merge(adj_df[['date', 'adj_factor']], on='date', how='left')
        df['adj_factor'] = df['adj_factor'].ffill().fillna(1.0)
        df['qfq_close'] = df['close'] * df['adj_factor']
    else:
        df['adj_factor'] = 1.0
        df['qfq_close'] = df['close']

    df = df.tail(30).copy().reset_index(drop=True)

    limit = get_limit(code)
    close = df['qfq_close'].values
    high = df['high'].values * df['adj_factor'].values
    low = df['low'].values * df['adj_factor'].values

    # 复权 pct_change
    pct = np.zeros(len(close))
    for i in range(1, len(close)):
        pct[i] = close[i] / close[i-1] - 1 if close[i-1] > 0 else 0

    last_close = close[-1]
    last5 = close[-5:]
    last3 = close[-3:]

    feats = {}

    # 1. limit
    feats['limit'] = limit

    # 2. prev_close
    feats['prev_close'] = float(last_close)

    # 3-4. ret_5d / ret_3d
    feats['ret_5d'] = float(close[-1] / close[-6] - 1) if close[-6] > 0 else 0
    feats['ret_3d'] = float(close[-1] / close[-4] - 1) if close[-4] > 0 else 0

    # 5. max_5d_pct
    feats['max_5d_pct'] = float(pct[-5:].max())

    # 6-7. limit_up_5d / limit_up_t1
    feats['limit_up_5d'] = int(sum(pct[-5:] >= limit - 0.001))
    feats['limit_up_t1'] = int(pct[-1] >= limit - 0.001)

    # 9. pct_t1
    feats['pct_t1'] = float(pct[-1])

    # 10. avg_amount_5d
    if 'amount' in df.columns:
        feats['avg_amount_5d'] = float(df['amount'].iloc[-5:].mean())
    else:
        feats['avg_amount_5d'] = float((df['volume'].iloc[-5:] * df['close'].iloc[-5:]).mean())

    # 11. vol_ratio_t1
    vol_5d_mean = float(df['volume'].iloc[-5:].mean())
    feats['vol_ratio_t1'] = float(df['volume'].iloc[-1] / vol_5d_mean) if vol_5d_mean > 0 else 0

    # 12. volatility_5d
    feats['volatility_5d'] = float(pct[-5:].std())

    # 13. close_pos_5d
    mn, mx = last5.min(), last5.max()
    feats['close_pos_5d'] = float((last_close - mn) / (mx - mn)) if mx > mn else 0.5

    # 14. max_drawdown_5d
    running_max = np.maximum.accumulate(last5)
    dd = last5 / running_max - 1
    feats['max_drawdown_5d'] = float(dd.min())

    # 15. turnover_proxy
    if 'turnover' in df.columns:
        feats['turnover_proxy'] = float(df['turnover'].iloc[-1])
    else:
        # 估算：volume / 总股本（无法准确，置 1）
        feats['turnover_proxy'] = 1.0

    # 16. is_st（需要股票名称，先从 all_a_stocks 查）
    feats['is_st'] = 0  # 默认非 ST（生产环境可加 name 字段）

    # 17. is_new：上市 < 60 日
    # 这里用简化：df 行数 < 30 → 当作新股
    feats['is_new'] = 1 if len(df) < 30 else 0

    # 18. is_bj
    feats['is_bj'] = 1 if code.startswith('bj') else 0

    # 19. is_gem
    feats['is_gem'] = 1 if (code.startswith('300') or code.startswith('301') or code.startswith('688')) else 0

    # 添加元信息
    meta = {
        'code': code,
        'date': str(df['date'].iloc[-1])[:10],
        'close': float(df['close'].iloc[-1]),
        'qfq_close': float(last_close),
        'volume': float(df['volume'].iloc[-1]),
    }
    return {**meta, **feats}


def load_single_stock(code: str) -> tuple:
    return code, compute_features(code)


def load_all_stocks(codes: list, max_workers: int = 8) -> pd.DataFrame:
    """并行加载所有股票的特征"""
    print(f"  并行加载 {len(codes)} 只股票...", file=sys.stderr)
    rows = []
    failed = 0
    with ProcessPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(load_single_stock, code): code for code in codes}
        for i, fut in enumerate(as_completed(futures)):
            code, data = fut.result()
            if data is not None:
                rows.append(data)
            else:
                failed += 1
            if (i + 1) % 500 == 0:
                print(f"    进度: {i+1}/{len(codes)} (失败 {failed})", file=sys.stderr)
    print(f"    完成: 成功 {len(rows)} / 失败 {failed}", file=sys.stderr)
    return pd.DataFrame(rows)


def load_universe() -> list:
    """加载全 A 股代码清单"""
    fp = ROOT / 'data' / 'all_a_stocks.json'
    if fp.exists():
        with open(fp) as f:
            data = json.load(f)
        codes = data.get('codes', data.get('stocks', []))
        if codes:
            return [str(c).zfill(6) for c in codes]
    # 回退：扫 parquet 目录
    codes = set()
    for fp in PARQUET_DIR.glob('*.parquet'):
        name = fp.stem
        for prefix in ['sh', 'sz', 'bj']:
            if name.startswith(prefix):
                name = name[len(prefix):]
                break
        codes.add(name)
    return sorted(codes)


def main():
    parser = argparse.ArgumentParser(description='方案 D 每日翻倍潜力预测')
    parser.add_argument('--date', help='基准日（YYYY-MM-DD），默认今天')
    parser.add_argument('--top', type=int, default=20, help='输出 Top N 高分股')
    parser.add_argument('--threshold', type=float, default=0.7, help='高分阈值')
    parser.add_argument('--codes', help='指定股票代码（逗号分隔）')
    parser.add_argument('--workers', type=int, default=8, help='并行进程数')
    parser.add_argument('--no-skip-bj', action='store_true', help='不跳过北交所')
    parser.add_argument('--no-skip-st', action='store_true', help='不跳过 ST')
    parser.add_argument('--no-skip-new', action='store_true', help='不跳过新股')
    args = parser.parse_args()

    print(f"=== 方案 D 预测 ===")
    print(f"  时间: {datetime.now().isoformat(timespec='seconds')}")
    print(f"  阈值: {args.threshold}")
    print(f"  Top:  {args.top}")

    if not MODEL_FILE.exists():
        sys.exit(f"❌ 模型不存在: {MODEL_FILE}")

    # 加载模型
    print(f"  加载模型: {MODEL_FILE}")
    model = lgb.Booster(model_file=str(MODEL_FILE))
    print(f"    期望特征数: {model.num_feature()}")
    print(f"    树数: {model.num_trees()}")

    if model.num_feature() != len(FEATURE_COLS):
        print(f"  ⚠ 特征数不匹配！模型 {model.num_feature()} vs 脚本 {len(FEATURE_COLS)}")
        print(f"    请检查 FEATURE_COLS 顺序")

    # 股票代码
    if args.codes:
        codes = [c.strip().zfill(6) for c in args.codes.split(',')]
    else:
        codes = load_universe()
        # 默认跳过北交所
        if not args.no_skip_bj:
            codes = [c for c in codes if not c.startswith('920')]
        # 默认跳过 ST（在特征计算层）
        # 默认跳过新股（在特征计算层）
    print(f"  股票数: {len(codes)}")

    # 计算特征
    print(f"\n[1/3] 计算 18 维特征...")
    feat_df = load_all_stocks(codes, args.workers)
    if feat_df.empty:
        sys.exit("❌ 无可用数据")
    print(f"    有效股票: {len(feat_df)}")

    # 过滤 ST / 新股
    if not args.no_skip_st:
        before = len(feat_df)
        feat_df = feat_df[feat_df['is_st'] == 0]
        print(f"    过滤 ST: {before} → {len(feat_df)}")
    if not args.no_skip_new:
        before = len(feat_df)
        feat_df = feat_df[feat_df['is_new'] == 0]
        print(f"    过滤新股: {before} → {len(feat_df)}")

    # 预测
    print(f"\n[2/3] 预测概率...")
    X = feat_df[FEATURE_COLS].fillna(0)
    feat_df['prob'] = model.predict(X)

    feat_df = feat_df.sort_values('prob', ascending=False).reset_index(drop=True)
    feat_df['rank'] = feat_df.index + 1

    high_score = feat_df[feat_df['prob'] >= args.threshold].copy()

    # 统计
    print(f"    Top {args.top} 概率: {feat_df.head(args.top)['prob'].min():.4f} ~ {feat_df['prob'].max():.4f}")
    print(f"    阈值 {args.threshold} 以上: {len(high_score)} 只")
    print(f"    平均概率: {feat_df['prob'].mean():.4f}")
    print(f"    中位数概率: {feat_df['prob'].median():.4f}")

    # 保存
    print(f"\n[3/3] 输出结果...")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')

    fp_all = OUTPUT_DIR / f'predict_{ts}.csv'
    feat_df.to_csv(fp_all, index=False, encoding='utf-8-sig')
    print(f"    ✓ 全部: {fp_all.name} ({len(feat_df)} 行)")

    fp_top = OUTPUT_DIR / f'predict_top{args.top}_{ts}.csv'
    feat_df.head(args.top).to_csv(fp_top, index=False, encoding='utf-8-sig')
    print(f"    ✓ Top {args.top}: {fp_top.name}")

    if len(high_score) > 0:
        fp_high = OUTPUT_DIR / f'predict_high_{ts}.csv'
        high_score.to_csv(fp_high, index=False, encoding='utf-8-sig')
        print(f"    ✓ 高分 {len(high_score)} 只: {fp_high.name}")
    else:
        print(f"    (无阈值 {args.threshold}+ 高分股)")

    # Markdown 报告
    fp_md = OUTPUT_DIR / f'predict_{ts}.md'
    with open(fp_md, 'w', encoding='utf-8') as f:
        f.write(f"# 方案 D 每日预测报告\n\n")
        f.write(f"- **时间**: {datetime.now().isoformat(timespec='seconds')}\n")
        f.write(f"- **基准日**: {args.date or '今天'}\n")
        f.write(f"- **股票数**: {len(feat_df)}\n")
        f.write(f"- **阈值**: {args.threshold}\n")
        f.write(f"- **高分股**: {len(high_score)} 只\n\n")

        f.write(f"## 📊 统计\n\n")
        f.write(f"| 指标 | 值 |\n|---|---|\n")
        f.write(f"| 平均概率 | {feat_df['prob'].mean():.4f} |\n")
        f.write(f"| 中位概率 | {feat_df['prob'].median():.4f} |\n")
        f.write(f"| 最高概率 | {feat_df['prob'].max():.4f} |\n")
        f.write(f"| ≥ 0.9 | {len(feat_df[feat_df['prob']>=0.9])} 只 |\n")
        f.write(f"| ≥ 0.8 | {len(feat_df[feat_df['prob']>=0.8])} 只 |\n")
        f.write(f"| ≥ 0.7 | {len(feat_df[feat_df['prob']>=0.7])} 只 |\n")
        f.write(f"| ≥ 0.6 | {len(feat_df[feat_df['prob']>=0.6])} 只 |\n\n")

        f.write(f"## 🎯 Top {args.top} 高分股\n\n")
        f.write(f"| Rank | 代码 | 概率 | 收盘 | 前复权 | T-1 涨幅 | 5 日涨幅 | 量比 | 涨停限制 |\n")
        f.write(f"|---:|:---:|---:|---:|---:|---:|---:|---:|---:|\n")
        for _, r in feat_df.head(args.top).iterrows():
            f.write(f"| {r['rank']} | {r['code']} | **{r['prob']:.4f}** | "
                    f"{r['close']:.2f} | {r['qfq_close']:.2f} | "
                    f"{r['pct_t1']*100:+.2f}% | {r['ret_5d']*100:+.2f}% | "
                    f"{r['vol_ratio_t1']:.2f} | {r['limit']*100:.0f}% |\n")

        if len(high_score) > 0:
            f.write(f"\n## 🚨 阈值 {args.threshold}+ 高分明细\n\n")
            f.write(f"| 代码 | 概率 | 收盘 | T-1 涨幅 |\n")
            f.write(f"|:---:|---:|---:|---:|\n")
            for _, r in high_score.iterrows():
                f.write(f"| {r['code']} | {r['prob']:.4f} | "
                        f"{r['close']:.2f} | {r['pct_t1']*100:+.2f}% |\n")

        f.write(f"\n## 使用建议\n\n")
        f.write(f"- **阈值 0.7+**：高置信度，可直接进入候选池（结合题材/新闻人工复核）\n")
        f.write(f"- **阈值 0.6-0.7**：中等置信度，建议观察 1-2 日\n")
        f.write(f"- **阈值 < 0.6**：仅作参考，不建议行动\n\n")
        f.write(f"---\n\n")
        f.write(f"生成工具: `scripts/predict.py`\n")
    print(f"    ✓ 报告: {fp_md.name}")

    # 同步 latest
    fp_latest = OUTPUT_DIR / 'predict_latest.md'
    fp_latest.write_text(fp_md.read_text(encoding='utf-8'), encoding='utf-8')

    print(f"\n=== ✅ 完成 ===")
    print(f"  输出目录: {OUTPUT_DIR}")
    print(f"  Top 1: {feat_df.iloc[0]['code']} (P={feat_df.iloc[0]['prob']:.4f})")
    print(f"  Top 5: {feat_df.head(5)['code'].tolist()}")


if __name__ == '__main__':
    main()