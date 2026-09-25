#!/usr/bin/env python3
"""
集成: v2 binary + multiclass → v6_train alpaca 加 trigger 真值

对每条 v6_train:
  1. 解析 instruction (提取代码+截至日期)
  2. 解析 input (20日 OHLCV)
  3. 用 input 算 features, 跑 v2 model (proba) + multiclass (5档 proba)
  4. 把模型输出加到 input 末尾 (JSON 格式)
  5. instruction 加一行: 模型预测 {proba:.2f} (涨幅分级 X)
  6. output 末尾追加 trigger 真值 (从 double_triggers_enriched.csv 查)

输出: v5/audit/v6_train_enriched.json (供 llamafactory 微调用)
"""
import pandas as pd
import numpy as np
import json, re, time
from pathlib import Path
import lightgbm as lgb

# === 路径 ===
V6_TRAIN = 'data/v6_train/v6_train.json'
TRIGGERS = 'v5/audit/double_triggers_enriched.csv'
BASIC = '/tmp/baostock_basic.parquet'
INDUSTRY = '/tmp/baostock_industry.parquet'
V2_MODEL = 'v5/audit/baseline_v2_model.txt'
MC_MODEL = 'v5/audit/baseline_multiclass_model.txt'
OUT = 'v5/audit/v6_train_enriched.json'


def calc_features_from_input(rows):
    """input rows 是 20 日 OHLCV list; 返回 (24 features, extra features)"""
    df = pd.DataFrame(rows)
    df.columns = ['date', 'open', 'close', 'high', 'low', 'volume', 'pct']
    df['date'] = pd.to_datetime(df['date'])

    f = pd.DataFrame(index=[0])
    c, h, l, v = df['close'], df['high'], df['low'], df['volume']

    for w in [5, 10, 20]:
        f[f'ma{w}'] = c.rolling(w).mean().iloc[-1:].values
        f[f'mom{w}'] = c.pct_change(w).iloc[-1:].values
    delta = c.diff()
    gain = delta.clip(lower=0).rolling(14).mean().iloc[-1]
    loss = (-delta.clip(upper=0)).rolling(14).mean().iloc[-1]
    rs = gain / (loss + 1e-9)
    f['rsi14'] = 100 - 100 / (1 + rs)
    ma20 = c.rolling(20).mean().iloc[-1]
    std20 = c.rolling(20).std().iloc[-1]
    f['bb_upper'] = ma20 + 2 * std20
    f['bb_lower'] = ma20 - 2 * std20
    f['bb_width'] = (f['bb_upper'] - f['bb_lower']) / (ma20 + 1e-9)
    f['bb_pos'] = (c.iloc[-1] - f['bb_lower'].iloc[0]) / (f['bb_upper'].iloc[0] - f['bb_lower'].iloc[0] + 1e-9)
    tr = pd.concat([h-l, (h-c.shift(1)).abs(), (l-c.shift(1)).abs()], axis=1).max(axis=1)
    f['atr14'] = tr.rolling(14).mean().iloc[-1]
    f['atr_ratio'] = f['atr14'].iloc[0] / (c.iloc[-1] + 1e-9)
    for w in [5, 10, 20]:
        f[f'vol_ma{w}'] = v.rolling(w).mean().iloc[-1:].values
    f['vol_ratio'] = v.iloc[-1] / (f['vol_ma20'].iloc[0] + 1e-9)
    f['vp_corr_10'] = v.pct_change().rolling(10).corr(c.pct_change())
    for w in [5, 10, 20]:
        f[f'amp{w}'] = (h.rolling(w).max().iloc[-1] - l.rolling(w).min().iloc[-1]) / (c.rolling(w).mean().iloc[-1] + 1e-9)
    f['hl_ratio'] = h.iloc[-1] / (l.iloc[-1] + 1e-9)
    f['gap'] = (c.iloc[-1] - c.iloc[-2]) / (c.iloc[-2] + 1e-9) if len(c) >= 2 else 0
    f['close_pct_20'] = (c.iloc[-1] - c.rolling(20).min().iloc[-1]) / (c.rolling(20).max().iloc[-1] - c.rolling(20).min().iloc[-1] + 1e-9)
    return f


def parse_meta(instruction):
    """从 instruction 提取 (code, date)"""
    # "对 浙江建投（002761）做" → code='002761'
    m = re.search(r'（(\d{6})）', instruction)
    code = m.group(1) if m else None
    # "截至2026-09-21" → date='2026-09-21'
    m = re.search(r'截至(\d{4}-\d{2}-\d{2})', instruction)
    date = m.group(1) if m else None
    return code, date


def main():
    # 加载
    print(f"[1] 加载数据")
    with open(V6_TRAIN) as f:
        v6 = json.load(f)
    triggers = pd.read_csv(TRIGGERS)
    triggers['start_dt'] = pd.to_datetime(triggers['start_date'])
    triggers['end_dt'] = pd.to_datetime(triggers['end_date'])
    basic = pd.read_parquet(BASIC)
    basic['symbol'] = basic['code'].str.replace('.', '')
    basic['ipoDate'] = pd.to_datetime(basic['ipoDate'], errors='coerce')
    industry = pd.read_parquet(INDUSTRY)
    industry['symbol'] = industry['code'].str.replace('.', '')
    industry_map = dict(zip(industry['symbol'], industry['industry'].fillna('UNKNOWN')))
    ipo_map = dict(zip(basic['symbol'], basic['ipoDate']))

    v2 = lgb.Booster(model_file=V2_MODEL)
    mc = lgb.Booster(model_file=MC_MODEL)
    print(f"  v2 features: {v2.num_feature()}")
    print(f"  mc num_class: {mc.num_model_per_iteration() if hasattr(mc, 'num_model_per_iteration') else '?'}")

    enriched = []
    t0 = time.time()
    skipped = 0
    for i, sample in enumerate(v6):
        try:
            instr = sample['instruction']
            inp_str = sample['input']
            code, end_date_str = parse_meta(instr)
            if not code or not end_date_str:
                skipped += 1
                enriched.append(sample)  # 保留原样
                continue
            inp_rows = json.loads(inp_str)
            last_day = inp_rows[-1]
            # symbol (code → sh600000 / sz000001)
            sym_prefix = 'sh' if code.startswith('6') else ('sz' if code.startswith(('0', '3')) else 'bj')
            symbol = f'{sym_prefix}{code}'
            end_dt = pd.Timestamp(end_date_str)

            # 算 features
            feats = calc_features_from_input(inp_rows)

            # 加 industry / days_listed / market_dummy
            industry_code = industry_map.get(symbol, 'UNKNOWN')
            ipo_dt = ipo_map.get(symbol)
            days_listed = (end_dt - ipo_dt).days if pd.notna(ipo_dt) else 0
            feats['industry_code_id'] = pd.Series([industry_code]).astype('category').cat.codes.iloc[0]
            feats['days_listed'] = max(0, days_listed)
            feats['log_days_listed'] = np.log1p(max(0, days_listed))
            feats['log_amp20'] = np.log1p(feats['amp20'].iloc[0])
            feats['market_dummy'] = {'sh': 0, 'sz': 1, 'bj': 2}.get(sym_prefix, 3)
            # industry mean (取整体均值作 fallback)
            feats['industry_mean_trigger'] = 0.16  # baseline pos rate

            # 排序对到训练顺序
            feat_cols = v2.feature_name()
            X_v2 = feats[feat_cols].astype(float)
            proba = float(v2.predict(X_v2)[0])

            # multiclass 用原始 24 features
            mc_base_cols = [c for c in feats.columns if c not in (
                'industry_code_id', 'days_listed', 'log_days_listed',
                'log_amp20', 'market_dummy', 'industry_mean_trigger')]
            X_mc = feats[mc_base_cols].astype(float)
            mc_proba = mc.predict(X_mc)[0]
            mc_class = int(mc_proba.argmax())
            class_names = ['未触发(<1x)', '1-1.5x', '1.5-2x', '2-3x', '3-5x', '5x+']

            # 查 trigger 真值
            sym_trigs = triggers[triggers['symbol'] == symbol]
            triggered = False
            trigger_info = ''
            # 检查 end_date_str 是否在某 trigger 区间内
            for _, t in sym_trigs.iterrows():
                if t['start_dt'] <= end_dt <= t['end_dt']:
                    triggered = True
                    trigger_info = f" [历史触发] 该股曾在 {t['start_date']}~{t['end_date']} 区间涨幅 {t['range_pct']*100:.0f}%"
                    break

            # 改 instruction: 加模型预测
            new_instr = instr + f"\n\n模型参考: trigger概率={proba:.2f}, 涨幅分级={class_names[mc_class]} ({mc_proba[mc_class]*100:.0f}% 置信)"

            # 改 input: 加 model 预测块
            model_pred_str = f", \"model_prediction\": {{\"trigger_proba\": {proba:.3f}, \"mc_class\": {mc_class}, \"mc_label\": \"{class_names[mc_class]}\", \"mc_proba\": {mc_proba.tolist()}}}"
            new_inp_str = inp_str[:-1] + model_pred_str + ']'

            # 改 output: 加 trigger 真值参考
            new_out = sample['output']
            if triggered:
                new_out = new_out + trigger_info

            new_sample = {
                'instruction': new_instr,
                'input': new_inp_str,
                'output': new_out,
            }
            enriched.append(new_sample)
        except Exception as e:
            if i < 3:
                import traceback
                traceback.print_exc()
            skipped += 1
            enriched.append(sample)
        if (i + 1) % 500 == 0:
            print(f"  [{i+1}/{len(v6)}] {time.time()-t0:.0f}s, skipped={skipped}")

    # 输出
    with open(OUT, 'w') as f:
        json.dump(enriched, f, ensure_ascii=False, indent=2)
    print(f"\n✓ {len(enriched)} samples → {OUT}")
    print(f"  skipped: {skipped}")
    print(f"  triggered_in_history: {sum(1 for s in enriched if '历史触发' in s.get('output', ''))}")

    # 样本预览
    print(f"\n=== 样本预览 (前 1 条) ===")
    s = enriched[0]
    print(f"instruction (last 100):\n  ...{s['instruction'][-200:]}")
    print(f"\ninput (last 200):\n  ...{s['input'][-300:]}")
    print(f"\noutput: {s['output'][:300]}")


if __name__ == "__main__":
    main()