# P0-1 打板专用模型 · 落地蓝图 (2026-10-18)

> 目标：训练"次日涨停概率"二分类模型，替代当前"数日波段 PnL"模型的打板错配。
> 状态：蓝图待实现（v9r 训练 13h 后台进行中，可随时实施，纯 CPU 不冲突 GPU）。

## 一、问题回顾

| 现状 | 问题 |
|---|---|
| 实盘模型 `vpnl_huber_stage1` = Huber 回归，预测"数日预期 PnL" | 目标错配（打板要"次日涨停概率"）|
| `find_d_signal_5d.py` 标签 = "翻倍前 5 日" | 不是次日涨停 |
| `train_pnl_proxy.py` 标签 = PnL 代理 | 波段收益，非涨停事件 |

**结论**：需要新标注 + 新训练，复用特征提取与 LightGBM 训练框架。

## 二、标注方案（新脚本 `scripts/build_daban_labels.py`）

**标签定义**：`y = 1` 当且仅当 `close[t+1] / close[t] - 1 >= limit`（T+1 收盘涨停，含一字板）。

```python
# 伪代码
for code in universe:
    df = load_parquet(code)          # 含 date/open/high/low/close/volume/amount
    limit = get_limit(code)
    close = df["close"]
    ret_next = close.shift(-1) / close - 1   # T+1 收益
    y = (ret_next >= limit - 0.001).astype(int)
    # 每个交易日的特征用 compute_window_features_extended(29维, 含新涨停特征)
    # 剔除 y 无法计算的最后一天
```

**样本量预估**：~5000 股 × ~1000 交易日 ≈ 数百万行，正样本（次日涨停）约占 1-3%（涨停是稀有事件）→ **需正负采样**（正样本全取 + 负样本按 1:5~1:10 下采样，或 LightGBM `scale_pos_weight`）。

## 三、训练方案（新脚本 `scripts/train_daban_model.py`）

- **复用**：`find_d_signal_5d.py` 的 `load_one`/特征提取/LightGBM 训练框架
- **特征**：29 维（26 旧 + 3 新涨停特征 `is_limit_up_today`/`consecutive_boards`/`board_position`）
- **模型**：LightGBM 二分类（objective=binary，`scale_pos_weight` 处理不平衡）
- **输出**：`data/double_up/d_signal_5d_model_daban.txt`
- **注册**：`scripts/predict.py` 的 `MODEL_REGISTRY` 加 `"daban": (..., "cls")`

## 四、回测对比（验证 P0-1 价值）

| 对比项 | vpnl_huber_stage1（现状）| daban（新）|
|---|---|---|
| 预测目标 | 数日 PnL | 次日涨停概率 |
| 评价指标 | OOS Sp 0.3475 | 次日涨停命中率 + OOS AUC |
| 打板契合度 | 低（目标错配）| 高（直接预测涨停）|

**验收标准**：`daban` 模型 OOS AUC > 0.60 或次日涨停 Top-N 命中率显著优于随机，才算 P0-1 成功；否则保留 `vpnl_huber_stage1`。

## 五、工作量与风险

| 项 | 估时 | 风险 |
|---|---|---|
| 标注脚本 | 30 min | 低（复用 load_one）|
| 训练脚本 | 30 min | 低（复用框架）|
| 训练 | 10 min | 低（LightGBM CPU）|
| 回测对比 | 30-40 min | 中（需设计验证集防泄漏）|
| **合计** | **~2h** | 中等 |

## 六、实施顺序建议

1. 本蓝图先 commit（供后续会话续接）
2. v9r 训练期间（13h）任一时段实施，纯 CPU 不冲突
3. 标注 → 训练 → 回测 → 达标才切换实盘，**不达标保留现状**

## 七、P1-1（情绪数据）简述

涨停池数据源调研（akshare `stock_zt_pool_em` 等接口），计算每日 `涨停家数`/`炸板率`/`连板晋级率` 作为市场级情绪特征，注入打板模型。**排在 P0-1 之后**。
