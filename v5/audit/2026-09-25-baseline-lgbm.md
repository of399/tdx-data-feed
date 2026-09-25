# 2026-09-25 baseline ML 模型训练报告

> 任务: 基于候选清单训练 baseline 预测"20 日涨幅 ≥ 100%"概率
> 算法: LightGBM 二分类 (28 个技术特征)

## 数据

| 项 | 值 |
|---|---|
| 正样本 (trigger start_date) | 32,835 |
| 负样本 (随机未触发起点) | 199,883 |
| 正负比 | 1:6.1 |
| 切分方式 | 按股票 split (val 看到未见过的股票, 模拟真实预测) |
| 股票数 | 3,727 (候选清单全部) |

## 特征工程 (28 个 → 24 个用上)

| 类 | 特征 |
|---|---|
| 价 | ma5/10/20, mom5/10/20, rsi14 |
| BBands | upper/lower/width/pos |
| 波动 | atr14/atr_ratio |
| 量 | vol_ma5/10/20, vol_ratio, vp_corr_10 |
| 形态 | amp5/10/20, hl_ratio, gap |
| 估值 | close_pct_20 |

## 模型

```
LightGBM (binary, auc)
- learning_rate=0.05, num_leaves=63, max_depth=7
- min_child_samples=50, feature_fraction=0.8, bagging_fraction=0.8
- early_stopping=50, best_iter=183
```

## 评估

```
ROC-AUC: 0.806 (val 20% stocks)
  pos count: 32835 / neg count: 199883
```

## Top 5 特征重要性 (gain)

| 特征 | gain | 说明 |
|---|---|---|
| gap | 139,724 | 跳空 (今日 vs 昨收) |
| hl_ratio | 51,733 | 当日高低比 |
| bb_lower | 25,995 | BBands 下轨 |
| mom20 | 15,338 | 20 日动量 |
| vol_ma20 | 13,466 | 20 日均量 |

## 关键修复

⚠️ 第一次跑 AUC=1.0 (data leakage)：特征用了 `end_date` 当窗口末
- 修正: 改用 `start_date` (触发起点)，未来 20 日才是预测目标
- 第二次 AUC=NaN (val 全负样本)：按日期切分导致 val 期触发稀疏
- 修正: 按股票 stratified split → val 看到未见过的股票

## 最新日打分 (5544 A 股)

```
分位数: min=0.007, 25%=0.028, 50%=0.045, 75%=0.082, max=0.901
top 5 proba > 0.75: sh688496 0.90 / sh600825 0.85 / sh601218 0.78 / sh600802 0.77 / sh600792 0.76
```

## 输出文件

| 文件 | 说明 |
|---|---|
| `baseline_model.txt` | LightGBM 模型 (24 features) |
| `baseline_features.parquet` | 训练数据集 (232,718 行) |
| `baseline_metrics.json` | AUC + 训练参数 |
| `baseline_importance.csv` | 特征重要性 |
| `baseline_score_latest.csv` | 5,544 A 股最新一日打分 |

## 重跑

```bash
cd /home/jiuben/tdx-data-feed
./venv/bin/python v5/scripts/train_baseline_lgbm.py --out-prefix v5/audit/baseline
./venv/bin/python -c "
import pandas as pd, lightgbm as lgb, glob
from pathlib import Path
# 用 model 对所有股打分 (见 v5/audit/2026-09-25-baseline-lgbm.md)
"
```

## 已知局限

1. 标签定义: 触发 = 起点后 20 日内 max(high)/min(low) ≥ 2.0 (含跳空和涨停)
2. 复权: raw close, 未做复权 (2020 后 adj_factor 可补)
3. 采样: 负样本随机采同时间窗起点, 没考虑"近期已大涨"的样本 (可能有偏)
4. 行业/SW 分类未用 (baostock 已有证监会分类, 后续可加)
5. 文本特征 (新闻/公告) 未用
6. AUC=0.806 baseline, 优化空间大 (调参 + 加特征 + 加时序模型)
