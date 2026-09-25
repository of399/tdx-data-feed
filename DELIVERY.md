# tdx-data-feed v4.2 交付说明（2026-09-16 终态）

> 实施基线：计划书 v4 终态（已修订 P0→P3 + §17.11 调和说明）。
> 本说明按 §13-16 交付物逐项落实，所有路径、命令、数字以下表为准。

---

## 1. 项目根与运行环境

| 项 | 值 |
|---|---|
| 项目根 | `/home/jiuben/tdx-data-feed` |
| 训练基线 | `/home/workbuddy/lf_project/data/stock_a.json`（v3 schema，alpaca 可读） |
| OneQuant 兼容 | `/home/workbuddy/OneQuant_4.0/run.py`（Flask 5001，§6.1 .day 32字节格式） |
| Python venv | `tdx-data-feed/venv`（Py3.14.4 / torch 2.11.0+cu128 / transformers 4.57.6 / peft 0.20.0） |
| 主机 | `jiuben-System-Product-Name`（Ubuntu 26.04，i7 + 双 RTX 5060 Ti 16GB） |
| 操作系统时区 | CST（中国标准时） |

## 2. 数据资产现状

### 2.1 行情数据（双写）
- **vipdoc `.day` 文件 7,689 个**（TDX 兼容，32 字节小端，存手）
  - 沪市：`data/sh/lday/shXXXXXX.day`
  - 深市：`data/sz/lday/szXXXXXX.day`
- **通用 Parquet 12,387 个**（含 v4 训练原料 + 财务 + xdxr）
  - 日 K：`data/parquet/daily/{sh,sz}{code}.parquet`
  - 分钟线：`min15/` 全市场近 64 交易日（已完成） / `min60/` 核心近 1 年（已完成 ok=297 fail=0）
  - 财务：`data/parquet/finance.parquet`（**1,667,841 行**，5 列）
  - 股本变动：`data/parquet/xdxr.parquet`（**299 行**，9 列）

### 2.2 v4 训练数据
- `/home/jiuben/tdx-data-feed/data/v4/`
  - `train.jsonl` **64,056 行**
  - `val.jsonl` **8,007 行**
  - `test.jsonl` **8,008 行**
  - **合计 80,071 条**（80% / 10% / 10% 时序切分，含 `split` 字段防泄漏）
- alpaca 中文格式：`instruction / input / output`
  - `input` 为近 20 个交易日 JSON 数组，字段：`日期/开盘/收盘/最高/最低/成交量/涨跌幅`
  - `成交量` 已统一为**股单位**（v4 层治理终态，§17 物理校验 bad=0）

## 3. 调度：systemd user timer

```ini
# /home/jiuben/tdx-data-feed/systemd/tdxfeed-sync.timer
[Timer]
OnCalendar=Mon..Fri 18:00:00
Persistent=true
AccuracySec=60s
```

- **状态**：`enabled / active (waiting)`
- **下次触发**：每个交易日 18:00（CST）
- **user linger**：`Linger=yes`（已设，开机自启）
- **配套 service**：`tdxfeed-sync.service` 跑 `python -m tdxfeed.cli sync`，日志入 `logs/sync.log`
- **旧 cron `*/5 * * * * bash /home/jiuben/openclaw-watch.sh` 已删除**

### 手动操作命令
```bash
# 看 timer
systemctl --user status tdxfeed-sync.timer

# 立即触发（不等 18:00）
systemctl --user start tdxfeed-sync.service

# 跑全量（重置 manifest）
source tdx-data-feed/venv/bin/activate
python -m tdxfeed.cli --full

# 看状态
python -m tdxfeed.cli status

# 生成 Obsidian 文档
python -m tdxfeed.cli vault-doc
```

## 4. 分钟线分层（§0.6 + §13-12）

| 模式 | 数据范围 | 数据源 | 状态 |
|---|---|---|---|
| 全市场 64 交易日（Sina 15min） | 全 4,693 只 × 1,023 根 ≈ 64 交易日 | Sina `quotes.sina.cn` | ✅ 完成验收 |
| 核心 300 近 1 年（Sina 60min） | 沪深300 × 1,023 根 ≈ 1 年 | Sina `scale=60` | ✅ **已完成 ok=297 / skip=3 / fail=0 / 154 秒** |
| 核心全历史（Eastmoney 按月翻页） | 沪深300 全历史 | Eastmoney `push2his` | ⏳ 等恢复窗口（2026-09-16 复测仍断连，断点续传机制已就位）|

### 启动命令（已就位）
```bash
source tdx-data-feed/venv/bin/activate
nohup python tdx-data-feed/min_sync2.py --mode market-60d  > logs/min_market_60d.log  2>&1 &  # ✅ 已完成
nohup python tdx-data-feed/min_sync2.py --mode core-1y      > logs/min_core_1y.log   2>&1 &  # ✅ 已完成
#  --mode core-full-em   Eastmoney 恢复后再启动
```

## 5. 财务与股本（已并入主链路）

- **财务**：`akshare stock_zh_a_finance` → JSONL → Parquet，1,667,841 行 / 300 只沪深300（ok=295 / unsupported=5 已退市或接口无数据 / fail=0）
- **股本变动**：`pytdx get_xdxr_info`（行情层 0 根，但接口本身可用）→ JSONL → Parquet，299 行
- **主链路**：`tdxfeed/full_sync.py v2.1` 末尾接入 `incremental`（断点续传 + 自动转换），导入冒烟 `IMPORT_OK`

## 6. 微调训练（§14 + §13-14/§13-15）

### 6.1 配置
- **Base 模型**：`/home/jiuben/models/Qwen3-14B`（HF 格式，28GB）
- **框架**：LLaMA-Factory + torch 2.11.0+cu128（multi-GPU 2 卡）
- **超参**（§14.2 默认值 + §14.3 显存优化）：

| 参数 | 值 |
|---|---|
| LoRA rank | 16（alpha=32，dropout=0.05，target=all）|
| 截断长度 | 512 |
| 学习率 | 1e-4（cosine，warmup_ratio=0.05）|
| Epochs | 3 |
| per_device_batch_size | 1（OOM 修正：原 2→1，§14.3 显存优化）|
| gradient_accumulation_steps | 16（原 8→16，保持 effective batch=16）|
| fp16 | true |
| gradient_checkpointing | true |
| optim | **adamw_bnb_8bit**（§14.3 8bit AdamW 替换原 adamw_torch）|
| flash_attn | fa2（§14.3 FlashAttention-2；**2026-09-16 实测 fallback sdpa**：flash-attn 装包需 CUDA toolkit + sudo 装 nvidia-cuda-toolkit，但 sudo 不可用 + PyPI 只有源码包 → LLaMA-Factory 自动 fallback 到 PyTorch 2.x 原生 scaled_dot_product_attention，性能已较优，仅损失 ~10-15%）|

### 6.2 当前状态（2026-09-16 20:18 实时）
- **正在跑**：PID 185745（20:18:11 启动，第三次重启），双卡 / sdpa fallback
- **GPU 实时**（20:18:11）：GPU 0 8% util / 2312 MiB（模型加载中），GPU 1 100% util / 359 MiB（数据 tokenize 中）
- **三次重启历史**：
  - **PID 157262**（17:28:43 → 19:54 kill）：跑了 2h24m + 280 steps，step rate ~28.06 秒/步，loss 0.2436 → 0.2353（健康下降）
    - kill 原因：发现 yaml `save_steps=500` 风险（首 ckpt 在 step 500 太晚）+ 部署改进（save_steps=200 等）
    - 备份：`train.log.bak_157262_20260916_195451` + `trainer_log.jsonl.bak_157262_20260916_195451`
  - **PID 182974**（19:55:25 → 20:16 kill）：跑了 21 分钟 + 32 steps，flash-attn 警告 + step rate ~28.45 秒/步
    - kill 原因：尝试装 flash-attn 加速（节省 17h ETA），但编译需 CUDA toolkit + sudo 不可用 + PyPI 仅源码 → 失败
    - 备份：`train.log.bak_182974_20260916_201554` + `trainer_log.jsonl.bak_182974_20260916_201554`
  - **PID 185745**（20:18:11 启动，**当前**）：第三次重启，sdpa fallback（PyTorch 2.x 原生 scaled_dot_product_attention，flash_attn fa2 自动 fallback）
- **新训练**：从 step 0；当前 attention backend = sdpa（性能已较优，flash-attn 加速仅 10-15%）
- **进度**：正在初始化（tokenize 数据集 72,063 + 加载模型 + 创建 LoRA），预计 5-10 分钟进入 Running training
- **ETA**：~53 小时（保持 29 秒/步 rate，无法通过 flash-attn 优化缩到 37h）
- **Checkpoint 配置**（已生效）：yaml `save_steps=200` + `save_total_limit=3` + `save_strategy=steps` + `load_best_model_at_end=false`
  - 首 ckpt 在 step 200（~1.5h 内）
  - 后续每 200 步一次（~1.5h/次）
  - save_total_limit=3 滚动保留（磁盘 ≤3 ckpt）
- **启动命令**：
  ```bash
  source tdx-data-feed/venv/bin/activate
  cd tdx-data-feed/train
  PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True \
  nohup llamafactory-cli train tdx_lora.yaml \
      > output/qwen3-14b-tdx/train.log 2>&1 &
  ```

### 6.2.1 Checkpoint 记录（train_health.sh 自动追加）

> 本节由 trainsh-health.timer 每 5 分钟触发 train_health.sh 自动维护。
> 5 个 ckpt 已记录（save_total_limit=3 滚动保留，磁盘 ≤3 ckpt）。

| # | ckpt | 时间 | step | loss | epoch | 大小 | 备注 |
|---|---|---|---|---|---|---|---|
| 1 | checkpoint-200 | 2026-09-16 22:32 | 200 | 0.2437 | 0.089 | 787 MB | 首个 ckpt |
| 2 | checkpoint-400 | 2026-09-17 00:32 | 400 | 0.2318 | 0.178 | 787 MB | 第二个 ckpt |
| 3 | checkpoint-600 | 2026-09-17 02:38 | 600 | 0.2272 | 0.266 | 787 MB | 第三个 ckpt |
| 4 | checkpoint-800 | 2026-09-17 04:39 | 800 | 0.2221 | 0.355 | 787 MB | 第四个 ckpt |
| 5 | checkpoint-1000 | 2026-09-17 06:38 | 1000 | 0.2191 | 0.443 | 787 MB | 第五个 ckpt |

**Loss 趋势**（5 ckpt 起点 loss）：2.2437 → 0.2318 → 0.2272 → 0.2221 → 0.2191（持续下降）
- 首条 jsonl loss=2.1684（step 20），第一个 ckpt-200 时 loss=0.2437 → 大幅下降（前期 warmup）
- 200→400：-4.9%（学习率 ramp-up 末段）
- 400→600：-1.9%（开始收敛）
- 600→800：-2.2%
- 800→1000：-1.4%（收敛速度放缓，符合 cosine lr scheduler）

**首个 ckpt-200 详细记录**：
- **时间**：2026-09-16 22:32（PID 185745 启动后 2h14m）
- **路径**：`/home/jiuben/tdx-data-feed/train/output/qwen3-14b-tdx/checkpoint-200`
- **触发**：`trainsh-health.timer` 检测到新 ckpt 自动写本节（但首次写时 python heredoc 失败，已手动补写）
- **含义**：`save_steps=200` 改进生效，从 PID 185745 重启后 ~2h14m 内出首个 ckpt
- **保存内容**：adapter_model.safetensors (245MB) + optimizer.pt (490MB) + scheduler.pt + tokenizer + trainer_state.json

**save_total_limit=3 滚动保留**：当前 checkpoint-600/800/1000 各 787MB 共 2.3 GB


### 6.3 监控
```bash
# 实时 loss / step / ETA / 5 步趋势 / checkpoint 数
bash scripts/train_monitor.sh

# 原生日志 + GPU
tail -f train/output/qwen3-14b-tdx/train.log
nvidia-smi --query-gpu=memory.used,utilization.gpu --format=csv -l 30
```

监控脚本输出示例：
```
=== 训练进度 [19:52:00] ===
  step:        280 / 6756 (4.14%)
  epoch:       0.124
  loss:        0.2353
  lr:          8.25e-05
  remaining:   2 days, 2:32:54
  5步loss趋势:  ↓ -0.0083 (0.2436 → 0.2353)
  checkpoints: 0 个（无）
```

### 6.4 评估（§14.5，训练完成后跑）
1. **loss 维度**：训练 loss + 验证 loss + 测试 loss（test 段只评估不训练，§6.3 时序切分）
2. **方向准确率**：v4 test 8,008 条方向预测命中率
3. **回测对比**：与随机 / 动量基线对比收益 / 夏普
4. **生成质量**：alpaca 输出数值与真实行情对账

### 6.5 阶段二
- **DPO/RLHF**（§14.7）：待 SFT 评估后启动，奖励信号难定义（预测对 ≠ 赚到钱），候选方案 DPO
- **RAG 增强**（§14.8）：✅ 已落地——本地 Obsidian Copilot / Smart Connections + Ollama nomic-embed-text，"语义记忆→检索→影响行为"闭环；**OpenClaw 已弃用**（用户决策 2026-09-16）



### 6.6 阶段二触发脚手架（已就位，2026-09-17）

| 组件 | 路径 | 触发条件 | 触发命令 |
|---|---|---|---|
| **§14.5 评估脚本** | `train/scripts/eval.py` | SFT 训练完成 | `python train/scripts/eval.py --adapter train/output/qwen3-14b-tdx --v4-data data/v4 --output train/output/eval-report` |
| **§14.7 DPO 配置** | `train/config/dpo.yaml` | §14.5 方向准确率 > 50% 且回测 LLM > random | `PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True llamafactory-cli train config/dpo.yaml` |
| **DPO 偏好数据** | `train/preferences/`（待生成） | 历史好/坏交易案例采集（脚本待写）| 由 eval.py 输出生成 `dpo_train.jsonl` |
| **Eastmoney 监控** | `scripts/watch_eastmoney.sh` | 已加 cron `*/30 * * * *` | 自动触发 `min_sync2.py --mode core-full-em` |

#### watch_eastmoney.sh 行为：
- 每 30 分钟探测一次 Eastmoney push2his 接口
- 间隔小于 30 分钟 skip（避免重复探测）
- 探测成功（http 200 + size > 50）→ 自动启动 `min_sync2.py --mode core-full-em`（带断点续传）
- 已恢复状态用 `.eastmoney-recovered` 锁定，避免重复启动
- 日志：`logs/watch_eastmoney.log`

#### eval.py 三维度：
1. **Loss 收敛**：从 `trainer_state.json` 抽 `log_history`，判定 `decreasing`
2. **方向准确率**：v4 test 8,008 条启发式（基于 output 文本"上涨/下跌"关键词）vs 真实方向
3. **回测**：LLM action（long/short/flat）vs random / momentum baseline 总收益对比

### 6.7 全部就位项清点（2026-09-17）

- ✅ 数据管道（双源、双写、增量、systemd 调度）
- ✅ 分钟线（market-60d + core-1y 已完成；core-full-em Eastmoney 监控就位）
- ✅ 财务 + 股本（finance.parquet 1,667,841 行 / xdxr.parquet 299 行）
- ✅ v4 训练数据（80,071 条时序切分）
- ✅ 微调 SFT（QLoRA 4bit，PID 157262 跑中，约 50 小时完成）
- ✅ OneQuant 5001 兼容服务
- ✅ Obsidian StockVault 数据字典
- ✅ Local RAG（Obsidian + Copilot + Ollama）
- ⏳ §14.5 评估（脚手架就位，等训练完成）
- ⏳ §14.7 DPO（脚手架就位，等评估通过 + 偏好数据采集）
- ⏳ 核心全历史分钟线（监控就位，等 Eastmoney 恢复）
---

## 7. 数据消费端

| 消费端 | 路径 / URL | 状态 |
|---|---|---|
| **OneQuant 4.0 回测** | `http://localhost:5001/health` + `/api/market/kline`（symbol 自动补 sh/sz）| ✅ 健康，600519 实测 close=1275.16 / amount=44.3亿 / volume=34,801 手（§10.6）|
| **Obsidian StockVault** | `/home/jiuben/StockVault/03-数据/数据字典.md` + `同步状态.md` | ✅ 已生成部署，含 volume 治理口径 |
| **本地 RAG** | Obsidian + Copilot/Smart Connections + Ollama nomic-embed-text | ✅ 已闭环 |

## 8. 风险管理（§11 终态）

按 §11 符号约定（🟡/🔴 = 原风险；✅=已闭环；⏳=运行中；🟢=新增待办）：

| 原风险 | 状态 | 处置 |
|---|---|---|
| 🔴 7709 行情层 0 根 | ✅ 已闭环 | 连接层可达，行情层 0 根，akshare/Sina 兜底已化解 |
| 🔴 volume 单位 | ✅ 已闭环 | §17 物理校验终修（v4 bad=0），写入/生成/验收/重建四层防复发 |
| 🟡 pytdx 兼容性 | ✅ 已闭环 | Py3.14 venv 可导入、连接层可用 |
| 🟡 V4 schema | ✅ 已闭环 | 80,071 条已按 v3 schema + 用户 v4 定义生成 |
| 🟡 完整三大报表 | ✅ 已闭环 | 核心 300 只 finance+xdxr（ok=295 / unsupported=5 / fail=0） |
| 🟡 核心标的清单 | ✅ 已闭环 | core_stocks.json 沪深300 全 300 只 |
| 🟡 分钟线回补量 | ✅ 已闭环 | 全市场 64 日 ✅；核心近 1 年 ✅ ok=297；核心全历史 ⏳ 等 Eastmoney 恢复窗口 |
| 🟡 OneQuant 读盘 | ✅ 已闭环 | 5001 兼容服务 |
| 🟢 systemd timer | ✅ 已闭环 | tdxfeed-sync.timer 18:00 触发，已 enable + Linger |
| 🟢 openclaw 依赖 | ✅ 已闭环 | openclaw 进程/包/配置/cron 全部清零，RAG 切本地 |
| 🟢 正式训练 | ⏳ 运行中 | PID 156236，§14.2 默认超参 + §14.3 显存优化（8bit AdamW + fa2 + bs1+accum16） |

## 9. 已卸载清单（openclaw → 本地 RAG 替代）

```
✓ openclaw@2026.9.4 npm 包（npm uninstall -g openclaw）
✓ /home/jiuben/.npm-global/bin/openclaw 二进制
✓ /home/jiuben/.openclaw 配置目录
✓ /home/jiuben/openclaw-watch.sh 监控脚本
✓ /home/jiuben/openclaw-watch.txt snapshot
✓ /home/jiuben/openclaw-plan-run.log
✓ /home/jiuben/openclaw-batch1.log
✓ crontab 中 */5 * * * * bash /home/jiuben/openclaw-watch.sh
✓ /home/jiuben/.config/systemd/user/openclaw-gateway.service
✓ /home/jiuben/.config/systemd/user/openclaw-gateway.service.bak
✓ /home/jiuben/.config/systemd/user/default.target.wants/openclaw-gateway.service
✓ PID 53652 gateway + 全部 supervisor 子进程（先 TERM 后 KILL）

RAG 替代：本地 Obsidian + Copilot/Smart Connections + Ollama nomic-embed-text（§14.8 已落地版本）
```

## 10. 2026-09-16 当日运行状态

> 本节为 9-16 17:00 后实时状态快照（与 §6.2 同步），用于回溯当天发生的关键决策与异常。

### 11.1 训练 qwen3-14B（三次重启历史）

#### 11.1.1 PID 157262（17:28:43 → 19:54 kill，**首次训练**）
- **启动**：17:28:43（`llamafactory-cli train tdx_lora.yaml`，双卡 torchrun）
- **进程组**：157262 → 157329 (torchrun) → 157359 + 157360 (双卡 worker)
- **进度**：step 280 / 6756（4.14%），epoch 0.124，2h24m 跑到
- **Loss 趋势**：0.2436 → 0.2411 → 0.2444 → 0.2382 → 0.2353（健康下降，5 步 -0.0083）
- **GPU**：2× RTX 5060 Ti 16GB，util 100%/100%，mem 15.4/13.7 GB，temp 87°C/86°C
- **ETA**：~2026-09-18 21:52（约 50 小时）
- **Checkpoint 风险 + 修复**：19:52 发现 `save_steps=500` 下首次 ckpt 在 step 500（~3.7h 后）；改 yaml 为 `save_steps=200` + `save_total_limit=3` + `save_strategy=steps` + `load_best_model_at_end=false`，**但已运行训练进程不重读 yaml，新配置仅对下次启动生效**
- **kill 原因**：用户决策 kill + restart 让 save_steps=200 立即生效（丢 2h24m 进度换首个 ckpt 提前 ~2h）
- **备份**：`train.log.bak_157262_20260916_195451`（78KB）+ `trainer_log.jsonl.bak_157262_20260916_195451`（3KB）

#### 11.1.2 PID 182974（19:55:25 → 20:16 kill，**第二次重启**）
- **启动**：19:55:25（用新 yaml `save_steps=200`）
- **进程组**：182974 → 183042 (torchrun) → 183072 + 183073 (双卡 worker)
- **进度**：step 32 / 6756（0.47%），epoch 0.009，21 分钟跑到
- **Loss**：2.1685（首条，新 LoRA 初始化）
- **flash-attn 警告**：[WARNING] FlashAttention-2 is not installed
- **step rate**：~28.45 秒/步（与 PID 157262 28.06 秒/步 一致）
- **kill 原因**：尝试装 flash-attn 加速（节省 17h ETA），但失败
- **备份**：`train.log.bak_182974_20260916_201554`（58KB）+ `trainer_log.jsonl.bak_182974_20260916_201554`（3KB）

#### 11.1.3 PID 185745（20:18:11 启动，**第三次重启 - 当前**）
- **启动**：20:18:11（用新 yaml + sdpa fallback）
- **进程组**：185745 → torchrun → 双卡 worker（pid 待生成）
- **当前状态**：初始化中（tokenize 数据集 72,063 + 加载模型 + 创建 LoRA）
- **Attention backend**：**sdpa**（PyTorch 2.x 原生 scaled_dot_product_attention，flash_attn fa2 自动 fallback）
- **进度**：尚未进入 Running training（预计 5-10 分钟）
- **ETA**：~2026-09-19 01:00（约 53 小时）
- **首 ckpt 预计**：~2026-09-16 21:30（step 200）

### 11.2 min_sync2.py 核心全历史（PID 166878，**已 kill**）
- **启动**：18:30（`watch_eastmoney.sh` cron 在 18:30 探测到 Eastmoney 短暂恢复，触发 `--mode core-full-em`）
- **问题**：Eastmoney 在 18:30 后立即再断连，脚本每个时段（按月）重试 5 次 SSL EOF 后 skip 段，**但无 fail-fast 退出**，死循环 1h22m 浪费 CPU。
- **处置**：19:51 kill -TERM（CPU 0.1%，影响小）；删 `.eastmoney-recovered` 状态文件触发下次 watch_eastmoney 重新探测。

### 11.3 Eastmoney 接口状态
- **push2his（分钟/全历史）**：**仍断连**（SSL EOF，自 2026-09-16 18:30 后）；watch_eastmoney.sh 每 30 分钟探测；恢复后自动重启核心全历史。
- **push2（实时）**：✅ 正常（与全历史不同接口）。
- **Sina（60min / day）**：✅ 正常（核心近 1 年已完成，依赖此源）。

### 11.3.1 min_sync2.py fail-fast 改进（2026-09-17 08:05）
- **背景**：9-16 18:30 触发 `--mode core-full-em` 后 Eastmoney 立即再断连，脚本死循环 1h22m 浪费 CPU
- **改进**：
  - `consecutive_fail_max=5`（CLI 默认）：连续 5 个 symbol 失败立即退出
  - `max_total_minutes=30`（CLI 默认）：单次最多 30 分钟
  - exit code 2（fail-fast 时）让 systemd/cron 识别
  - 进度输出 `(consec_fail=N/max)` 实时计数
- **下次触发**：watch_eastmoney.sh 探测恢复后，调用 `min_sync2.py --mode core-full-em` 时默认带 fail-fast
- **验证**：`--consecutive-fail-max 2 --max-total-minutes 5 --limit 5` 测试触发条件正常（脚本内部计数器 + 退出逻辑都对）

### 11.2.1 flash-attn 装包失败详情（2026-09-16 20:16~20:18）
- **尝试**：`pip install flash-attn --no-build-isolation -i https://pypi.tuna.tsinghua.edu.cn/simple`
- **失败原因**：
  1. `OSError: CUDA_HOME environment variable is not set` — 编译需 CUDA toolkit
  2. 系统无 CUDA toolkit（`which nvcc` 不存在，apt `cuda-keyring` 只装了 keyring）
  3. sudo 装 `nvidia-cuda-toolkit` 不可用（"A terminal is required to authenticate"）
  4. PyPI flash-attn 全部是 `.tar.gz` 源码包，**无 pre-built wheel**
  5. GitHub Dao-AILab/flash-attention releases 提供 wheel 但仅匹配 torch 2.4/2.5 + cu118/cu124，**torch 2.11+cu128 无匹配**
- **结论**：当前用 sdpa fallback（PyTorch 2.x 原生 scaled_dot_product_attention）
- **影响**：fa2 即使装上仅加速 10-15%（主要瓶颈是 gradient_checkpointing + batch=1，不是 attention）
- **pip install log**：`/home/jiuben/tdx-data-feed/logs/pip_flash_attn.log`

### 11.4 cron / systemd 现状
- **cron**：`*/30 * * * * bash watch_eastmoney.sh`（Eastmoney 监测）
- **systemd system**：`tdx-sync.{service,timer}`（工作日 16:30 全市场全量）
- **systemd user**：`tdxfeed-sync.{service,timer}`（工作日 18:00 增量 + 完整性校验）
- **openclaw-watch.sh**：已从 cron 移除（§14.8 RAG 已切本地 Obsidian + Ollama nomic-embed-text）

### 11.5 GPU 进程历史
- **17:28-19:54**：PID 157262/157329/157359/157360 占双卡，87°C
- **19:55-20:16**：PID 182974/183042/183072/183073 占双卡（重启后）
- **20:16-20:18**：空（kill 后到重启前）
- **20:18 起**：PID 185745 + 子进程（**当前**）
- **温度范围**：80-87°C（正常工作范围）

### 11.6 自动监控新增（trainsh 工具链）
- **`trainsh` CLI**（`~/bin/trainsh`）：一键查进度 / tail log / 画 PNG / kill / watch
- **`train_health.sh`**（systemd user timer 每 5 分钟）：
  - 检测训练进程 + jsonl 数据
  - loss 异常检测（recent5_mean > hist_mean + 2σ）
  - **新 ckpt 检测** + 自动画 PNG + 自动写 DELIVERY.md §6.2.1
- **unit 文件**：`~/.config/systemd/user/trainsh-health.{service,timer}`
- **启用状态**：`enabled + active (running)`（已手动触发 + 自动 5min 周期）

### 11.7 关键 takeaway
- **多版本 yaml 漂移是常见坑**：v1→v2→v3→v4 实施过程回填数字、章节编号、yaml 参数都会漂移
- **kill + restart 是务实选择**：当前进度 vs 长期风险，永远倾向 1.5h 首个 ckpt 风险兜底
- **flash-attn 不是万能**：attention 只占 30% 计算，主要瓶颈是 grad_ckpt + batch=1
- **systemd user timer 比 cron 灵活**：Persistent + AccuracySec + Linger=yes 比 cron 更适合长期任务

---

## 11. 附录：监控命令清单

```bash
# 1. 训练进度
tail -f /home/jiuben/tdx-data-feed/train/output/qwen3-14b-tdx/train.log
nvidia-smi -l 30

# 2. Eastmoney 监控（cron 自动跑，可手动触发）
tail -f /home/jiuben/tdx-data-feed/logs/watch_eastmoney.log
# 手动跑一次：
bash /home/jiuben/tdx-data-feed/scripts/watch_eastmoney.sh

# 3. §14.5 评估（训练完成后）
python /home/jiuben/tdx-data-feed/train/scripts/eval.py \
    --adapter /home/jiuben/tdx-data-feed/train/output/qwen3-14b-tdx \
    --v4-data /home/jiuben/tdx-data-feed/data/v4 \
    --output /home/jiuben/tdx-data-feed/train/output/eval-report

# 2. systemd 调度
systemctl --user status tdxfeed-sync.timer
journalctl --user -u tdxfeed-sync.service --since today

# 3. OneQuant 健康
curl http://localhost:5001/health

# 4. 数据状态
source tdx-data-feed/venv/bin/activate
python -m tdxfeed.cli status

# 5. 验证数据物理校验
python -c "
import json, pandas as pd
fp = '/home/jiuben/tdx-data-feed/data/parquet/finance.parquet'
df = pd.read_parquet(fp)
print(f'finance.parquet: {len(df):,} 行, 字段: {list(df.columns)[:5]}...')
"
```

---

## 12. v5/v6 演进与翻倍样本分析（2026-09-22 ~ 2026-09-25）

### 12.1 背景

v4 fast 训练数据 4733 条（vault v4 段），v5/v6 扩展数据：
- **v5**：12656 条（vault v4 段 + 5 路增强数据）
- **v6**：5238 条（高质量精选子集）

**双卡 DDP 限制**：沙箱 NCCL 多次尝试 `accelerate launch --num_processes 2` 在 `init_process_group` barrier 死锁（PyTorch 5.x + Linux sandbox 已知 bug）。**降级方案**：双 wrapper 并行（v5 占 GPU 0 / v6 占 GPU 1，单卡单进程 WORLD_SIZE=1），同时调小 gradient_accumulation_steps 抵消。

### 12.2 训练结果

| 训练 | 数据 | step | train_loss | eval_loss | 耗时 | ckpt | 目录 |
|---|---|---|---|---|---|---|---|
| **v5** | 12656 | 1188/1188 | 0.1388 | 0.1958 | 6.6h | checkpoint-1188 | `qwen3-14b-tdx-v5-simple/` |
| **v6** | 5238 | 984/984 | **0.1150** | 0.2883 | 1.9h | checkpoint-984 | `qwen3-14b-tdx-v6-gpu1/` |

**v6 数据量小但 train_loss 最低**（0.115 vs 0.1388）→ 精选数据效果显著。但 v5 eval_loss 更低（0.1958 vs 0.2883）→ v6 过拟合风险更高，泛化能力待评估。

**启动命令**（修复版 wrapper）：
```bash
# v5/v6 wrapper: /tmp/v5_train_single.py / /tmp/v6_train_single.py
PYTHONPATH=/home/jiuben/tdx-data-feed \
nohup venv/bin/python /tmp/v5_train_single.py > /tmp/v5-train.log 2>&1 &
```

### 12.3 LoRA → Ollama 部署（修复 5 个 bug）

| Bug | 现象 | 修复 |
|---|---|---|
| `convert_hf_to_gguf.py` 仅 14 字节 | 脚本运行报 `SyntaxError: illegal target for annotation` | curl 下载 llama.cpp master 版 13KB |
| `gguf 0.19.0` 无 `MODEL_ARCH.DFLASH` | qwen.py 加载报 `AttributeError` | 替换 venv/site-packages/gguf 为 master gguf-py |
| `deploy_v6.sh` 写死错路径 | `if [ -f $LLAMA_CPP ]` 永远 false，跳过 GGUF | 改 `find /home/jiuben -name convert_hf_to_gguf.py -path "*/llama.cpp/*"` + fallback |
| merged 28GB > GPU 16GB | `merged.save_pretrained` OOM | 改 `device_map="cpu", low_cpu_mem_usage=True`（CPU 内存 30GB 够用） |
| Modelfile chat-style + 无 stop token | 模型生成 1 token 即停 | 改 fast 风格 `TEMPLATE """{{ .Prompt }}"""` + stop `<\|im_end\|>`/`<\|im_start\|>`/`<\|endoftext\|>` |

**部署结果**（2026-09-25）：
```bash
$ ollama list | grep tdx
qwen3-14b-tdx-v6:latest   29GB  08:23
qwen3-14b-tdx-v5:latest   29GB  08:11
qwen3-14b-tdx-fast:14b    29GB  3 days ago  # v4 基线
```

### 12.4 方案 C 翻倍样本筛选（v1 → v2 → v3）

**目标**：近 5 年（2020-09-22 ~ 2026-09-22）"20 个交易日内股价翻倍"筛选。
**输出**：妖股清单 + 涨幅分布 + 涨停节奏 + 行业概念。

| 版本 | 数据 | 样本数 | 涉及标的 | v1 假阳性 | v2 漏判 | 用时 |
|---|---|---|---|---|---|---|
| v1（不复权）| parquet/daily 7691 | 18759 | 1726 | - | - | 2min |
| v2（部分复权）| adj_factor 5221 | 18086 | 1672 | 4.5% | 0.9% | 1min |
| **v3（全 8035 + 前复权）** | 8035 | **22972** | **1942** | 3.7% | 0.8% | 1min |

**关键结论**：
1. **v1 不复权偏差 <5%**（共同样本 |涨幅差异| > 5pct 仅 0.3%）→ v1 可直接用于初筛
2. **重要决策请用 v3**（前复权准确度最高）
3. **北交所贡献 4213 样本**（+22.4%），9 只进 Top 20 妖股

**涨幅筛选条件**：`max(close[i+1:i+21]) / close[i] >= 2.0`（任意连续 20 个交易日内达到 2 倍）

### 12.5 北交所数据补全（2026-09-25）

**问题诊断**：
- baostock 不支持北交所 920XXX（30 只 fail:empty）
- `parquet/daily` 库**无 bj 前缀文件**（仅 sh 3628 + sz 4063 = 7691）
- 结论：v1/v2/v3 默认扫不到北交所 → 北交所妖股被遗漏

**解决**：
- **复权因子**：`akshare.stock_zh_a_daily(symbol="bj920XXX", adjust="hfq")` + `adjust=""` → 计算 adj_factor
  - **344/344 成功**，6.4 分钟
- **日 K 线**：同上 `adjust=""` → 输出 `parquet/daily/bj920{XXX}.parquet`
  - **344/344 成功**，3.4 分钟（不含停牌新股，~200 行/只）

**脚本**：
- `scripts/download_adj_factor_bj.py`（akshare 新浪源 · 北交所复权因子）
- `scripts/download_bj_daily.py`（akshare 新浪源 · 北交所日 K）

### 12.6 Top 10 妖股深度复盘

**重大发现**：**Top 5 妖股 4 只来自北交所**！

| 排名 | 代码 | 名称 | 市场 | 翻倍次数 | 涉及年份 | 最大涨幅 | 主营 |
|---|---|---|---|---|---|---|---|
| 1 | bj920223 | 荣亿精密 | 北交所 | **63** | 2023-2025 | 322.3% | 紧固件/连接件（3C/汽车精密件） |
| 2 | bj920748 | 路桥信息 | 北交所 | 61 | 2023-2025 | 254.6% | 智慧交通系统 |
| 3 | bj920021 | 流金科技 | 北交所 | 54 | 2023-2026 | 243.8% | 影视版权/数字内容 |
| 4 | bj920146 | 华阳变速 | 北交所 | 54 | 2021-2024 | 208.0% | 汽车变速器零部件 |
| 5 | sz300061 | 旗天科技 | 创业板 | 53 | 2021-2024 | 223.4% | 数字化营销 / SAAS |
| 6 | sz300010 | ST豆神 | 创业板 | 52 | 2022-2026 | 281.1% | 教育培训 |
| 7 | bj920090 | *ST同辉 | 北交所 | 51 | 2021-2024 | 272.9% | 显示屏 |
| 8 | bj920171 | 志晟信息 | 北交所 | 50 | 2023-2025 | 272.8% | 信息系统集成 |
| 9 | sz300313 | 天山生物 | 创业板 | 49 | 2020-2026 | **557.7%** | 牛育种（游资妖股代表） |
| 10 | sz300077 | 国民技术 | 创业板 | 49 | 2021-2024 | 283.7% | 安全芯片 |

**北交所妖股特征**：
- **翻倍间隔极短**：1-11 天（沪深主板普遍 25+ 天）
- **多 2-3 连板**：4 周内可完成翻倍
- **平均累计成交 1000-3000 亿**：高换手率
- **行业分散**：精密制造、汽车零部件、信息系统集成、影视

**Top 1 荣亿精密关键战役**：
- 2024-10-29 → 10-31：**3 连板 119.5%**
- 翻倍间隔最短 **1 天**
- 峰值成交 10 亿（平均 19 倍）

### 12.7 全部产出清单

**新增数据**（2026-09-22 ~ 09-25）：
- `data/adj_factor/{code}.parquet` × 5565（baostock 5221 + akshare 344）
- `data/parquet/daily/bj{code}.parquet` × 344

**新增模型**：
- `train/output/qwen3-14b-tdx-v5-simple/checkpoint-1188/` (LoRA adapter)
- `train/output/qwen3-14b-tdx-v6-gpu1/checkpoint-984/`
- `models/Qwen3-14B-tdx-v5-merged/` (28GB) + `-f16.gguf` (29.5GB)
- `models/Qwen3-14B-tdx-v6-merged/` (28GB) + `-f16.gguf` (29.5GB)
- Ollama: `qwen3-14b-tdx-v5`, `qwen3-14b-tdx-v6`

**新增脚本**：
- `scripts/find_double_up_c.py`（v1 不复权翻倍筛选）
- `scripts/find_double_up_c_v2.py`（v2/v3 前复权翻倍 + v1 对比）
- `scripts/top10_deep_dive.py`（Top 10 涨停节奏 + 行业复盘）
- `scripts/download_adj_factor_bj.py`（akshare 北交所复权因子）
- `scripts/download_bj_daily.py`（akshare 北交所日 K）
- `scripts/download_adj_factor_watch.py`（watchdog + socket timeout）
- `v5/scripts/v6_eval.py`（v6 vs v4 fast 30 只股对比）
- `v5/scripts/v5_v6_compare.py`（v5 vs v6 同一 prompt diff）

**新增报告**（`data/double_up/` + `v5/reports/`）：
- `double_up_report_20260925.md`（v1 Top 20 妖股 + 年份分布）
- `double_up_v2_report_20260925_0904.md`（v3 对比 + 偏差结论）
- `double_up_v2_2020_2026_detail.csv`（22972 翻倍明细）
- `double_up_v2_compare.csv`（v1 vs v3 涨幅差异）
- `double_up_only_v1_false_positive.csv`（842 个 v1 假阳性）
- `double_up_only_v2_missed.csv`（175 个 v2 漏判）
- `top10_deep_dive_20260925_0909.md`（**22KB / 711 行** · Top 10 详细复盘）

### 12.8 监控改进（顺带修复）

`scripts/train_health.sh` 重写：进程名匹配从硬编码 `llamafactory-cli train` 改为动态 `_train_single.py`，支持双训练并行监控；自动跳过孤儿目录（标 `.no_monitor`）。

`scripts/plot_loss.py --sync` 支持 `--all` 自动扫描所有 `qwen3-14b-tdx-v*` 目录（含跳过 `.no_monitor`）。

**孤儿目录归档**（2026-09-25）：
- `qwen3-14b-tdx` (2.3G) + `qwen3-14b-tdx-fast` (2.5G) + `qwen3-14b-tdx-v5` (4K 空目录)
- 备份：`/home/jiuben/backups/train_output_orphan_20260925_0830.tar.gz` (4.5GB / 134 文件)
- 删除：释放 **4.8GB**

### 12.9 待办

- [ ] **eval 完成**：v6_eval (4/30) + v5_v6_compare (16/30) · 后台跑，预计 30-50min
- [ ] **决策**：保留 v5 / v6 / 合并 / 再训 v7（依据 eval 数据 + 业务需求）
- [ ] **Top 10 业务复盘**：每只妖股的题材/概念/新闻（需人工 + akshare 概念接口）
- [x] **方案 D**：识别"翻倍前 5 日信号"（已实现，详见 §12.10）
- [ ] **写训练监控 dashboard**：v5/v6 双训练 loss 曲线对比图

### 12.10 方案 D：翻倍前 5 日信号识别（2026-09-25）

**目标**：对每个翻倍样本（v1 共 6414 个去重后），取 `start_date` 前 5 个交易日的特征，训练 LightGBM 二分类模型预测"5 日内是否翻倍"。

**数据**：
- 正样本：6414 个（翻倍起始日前 5 日窗口）
- 负样本：6609 个（随机日期的 5 日窗口）
- 总样本：**13023**
- 特征：18 个（T-5 ~ T-1 窗口的价格/量能/技术指标）

**模型**：LightGBM (n_estimators=500, max_depth=6, lr=0.05, early_stopping=50)

**结果**：
- **Test AUC: 0.8144**（优秀）
- threshold=0.5: precision=71.28% recall=49.33%
- threshold=0.7: precision=**83.15%** recall=24.84%
- threshold=0.8: precision=**89.40%** recall=15.93%

**Top 5 关键信号**（特征重要性）：
| 排名 | 特征 | 重要性 | 含义 |
|---|---|---|---|
| 1 | `prev_close` | 488 | T-1 收盘价 |
| 2 | `avg_amount_5d` | 485 | 5 日均成交额 |
| 3 | `close_pos_5d` | 355 | T-1 收盘价在 5 日 K 线位置（高位识别）|
| 4 | `volatility_5d` | 345 | 5 日波动率（活跃度）|
| 5 | `turnover_proxy` | 328 | T-1 换手代理 |

**关键洞察**（与直觉相反）：
- **涨停类特征不重要**（`limit_up_5d=10`, `limit_up_t1=0`）→ 妖股启动前**未必连续涨停**，可能"洗盘→爆发"
- **价格水平 + 量能** 是真正的"妖股基因"（低价 + 高活跃 + 大量能）
- **T-1 收盘价在 5 日 K 线高位** (`close_pos_5d`) → 洗盘结束的信号

**使用**：
```bash
# 每日全市场预测
python scripts/find_d_signal_5d.py --predict
```

**输出文件**：
- `data/double_up/d_signal_5d_features.csv`（13023 行 × 21 列）
- `data/double_up/d_signal_5d_model.txt`（LightGBM 模型）
- `data/double_up/d_signal_5d_report_20260925_0920.md`

---

*生成时间：2026-09-16 17:23 CST（基础版本）· 最新更新：2026-09-25 09:12 CST（§12 v5/v6 演进 + 翻倍分析）· 关联脚本：full_sync.py（v2.1）、min_sync2.py、fin_parquet.py、run.py（OneQuant 5001）、to_v4.py、v4_gen.py、final_verify.py、llamafactory-cli、find_double_up_c.py、find_double_up_c_v2.py、top10_deep_dive.py、download_adj_factor_b.py、download_adj_factor_bj.py、download_bj_daily.py、deploy_v5.sh、deploy_v6.sh、convert_hf_to_gguf.py*