# 2026-09-25 v6 alpaca 集成 trigger 真值

## 集成流程

```
v6_train.json (5238 alpaca)
    ↓ parse instruction (code + end_date)
    ↓ parse input (20 日 OHLCV)
    ↓ calc_features_from_input → 24 features
    ↓ 加 industry/days_listed/log_amp20/market_dummy (6 features)
    ↓ 跑 v2 model → trigger_proba (0~1)
    ↓ 跑 multiclass model → 5 档 proba
    ↓ 查 triggers 区间 → 加 [历史触发] 标注
    ↓ 输出 v6_train_enriched.json (5238 条)
    ↓ 转 jsonl → v6_train_enriched_llmf.jsonl (供 llamafactory)
```

## 输出文件

| 文件 | 大小 | 内容 |
|---|---|---|
| `v6_train_enriched.json` | 22MB | 5238 enriched alpaca |
| `v6_train_enriched_llmf.jsonl` | 22MB | 同上转 llamafactory jsonl |
| `v6_enriched_train.yaml` | 1KB | llamafactory 训练配置 |
| `dataset_info.json` | 215B | 数据集注册 |
| `enrich_v6_with_models.py` | — | 集成脚本 |

## 数据增强样例

### Instruction (新增)
```
... 原始 v6 instruction ...
模型参考: trigger概率=0.36, 涨幅分级=未触发(<1x) (96% 置信)
```

### Input (新增 model_prediction)
```json
[..., {"日期":"2026-09-21","开盘":7.26,"收盘":7.42,...},
 "model_prediction": {
   "trigger_proba": 0.057,
   "mc_class": 0,
   "mc_label": "未触发(<1x)",
   "mc_proba": [0.99, 0.0015, ...]
 }]
```

### Output (12 个含 trigger 真值)
```
... 原始 v6 output ...
[历史触发] 该股曾在 2026-08-28~2026-09-24 区间涨幅 105%
```

## 统计

- 总样本: 5238 (100%)
- 含 trigger 真值: 12 (0.2%)
- v2 proba 分布: median=0.04, max=0.76
- mc class 0 占绝大多数 (99%+)

## 训练用法

```bash
cd /home/jiuben/tdx-data-feed

# 0. 装 llamafactory (已装 0.9.5)
./venv/bin/pip install llamafactory

# 1. 启动训练 (单 GPU, 约 4h)
./venv/bin/llamafactory-cli train v5/audit/v6_enriched_train.yaml

# 2. 或 export 后用 vllm 推理
./venv/bin/llamafactory-cli export v5/audit/v6_enriched_export.yaml
```

## 评估方式

- val AUC (5-fold) on trigger_proba vs 真值
- 人工评分 50 条：LLM 输出是否引用 trigger 真值
- 与 v5 已有输出对比 → 触发识别率
