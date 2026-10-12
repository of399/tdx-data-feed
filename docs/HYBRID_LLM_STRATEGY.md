# 本地 + 云端混合大模型使用技术方案

> **适用范围**：tdx-data-feed 量化研究平台的日常运行、研发与运维
> **最后更新**：2026-09-28 · **维护者**：jiuben
> **核心原则**：**生产本地化、研发云端化、敏感数据不出域**

---

## 1. 方案总览

```
┌──────────────────────────────────────────────────────────────┐
│                      混合大模型架构                            │
└──────────────────────────────────────────────────────────────┘

  ┌───────────────────── 生产面（100% 本地）────────────────────┐
  │  数据获取 → 预测生成 → 策略回测 → 实盘执行                   │
  │  baostock/akshare/Sina/Tencent → parquet（落盘本地）         │
  │  方案 D：LightGBM（无 LLM）                                  │
  │  方案 E：回测本地 + 分析用 qwen3-14b-tdx-v7（本地 Ollama）    │
  │  调度：systemd *.timer（sync/predict/journal/weekly/canary） │
  │  ✅ 行情/持仓/策略/财务数据 全程不出本机                       │
  └─────────────────────────────────────────────────────────────┘

  ┌───────────────────── 研发面（云端模型）─────────────────────┐
  │  IDE 内由云端大模型完成：                                     │
  │  代码编写 · 架构设计 · 跨文件重构 · 复杂因子推演 · 技术文档分析 │
  │  ⚠️ 禁止上传：原始行情、持仓明细、私有策略逻辑、财务数据      │
  └─────────────────────────────────────────────────────────────┘
```

**三条铁律**
1. 任何含持仓、策略、个股财务的输入，**绝不**发送到云端模型。
2. 生产 pipeline 的每一步都可在断网环境独立运行（仅依赖公开数据源下载）。
3. 本地 LLM 仅用于"分析/解释/对话"，不参与确定性预测（预测由 LightGBM 负责）。

---

## 2. 生产 Pipeline 完整本地化部署

目标：数据获取、预测生成、策略回测、实盘执行四个环节**全部本地运行**，数据全程不出本地环境。

### 2.1 数据获取（本地）

所有数据源均为公开行情接口，下载后落盘为本地 parquet，**不经过任何云端**：

```bash
cd /home/jiuben/tdx-data-feed
source venv/bin/activate

# 主力链路（Sina → akshare → Tencent 兜底，写 v2 schema date32）
python -m tdxfeed.full_sync
# 增量更新（Tencent 主源）
python -m tdxfeed.incremental --limit 100
# 复权因子 / 后复权 / ETF·可转债 / 北交所
python scripts/download_adj_factor_b.py
python scripts/download_hfq_factor.py
python scripts/download_etf_bond.py
python scripts/download_bj_daily.py
# 翻倍样本
python scripts/find_double_up_c_v2.py
```

### 2.2 预测生成（本地 · 方案 D · 无 LLM）

每日全市场打分由 **LightGBM**（18 维特征，joblib 序列化）完成，**不调用任何大模型**，毫秒级、确定性、零推理成本：

```bash
# 全市场 Top 30（~1 分钟，7620 只）
python scripts/predict.py --top 30 --threshold 0.7
# 指定标的
python scripts/predict.py --codes 600519,000001,300750 --top 3
```

输出：`data/predictions/predict_YYYYMMDD_HHMMSS.csv` + `.md`（概率 + 18 维特征，全部本地落盘）。

### 2.3 策略回测（本地 · 方案 E）

回测完全本地执行，不产生任何外发请求：

```bash
# v1 vs v2 对比
python scripts/plan_e_backtest.py --compare
# 参数扫描（持有天数 × 止盈）
for hold in 5 10 15; do
  for tp in 0.20 0.30 0.50; do
    python scripts/plan_e_backtest.py --hold $hold --tp $tp --sl -0.10 \
      --output data/plan_e/grid_h${hold}_tp${tp}.csv
  done
done
```

输出：`data/plan_e/plan_e_compare_*.md` + 明细 CSV（v2 胜率 55.6% / 平均收益 12.22% / 盈亏比 3.00）。

### 2.4 实盘执行（本地 · 方案 E）

盘前打分 + 实盘跟踪全部在本地：

```bash
# 盘前实时打分（工作日 09:00 systemd 触发）
python scripts/v3_premarket_top.py --date 2026-09-28 --top-n 10
# 实盘跟踪（simulate/paper/live）
python scripts/plan_e_journal.py --mode daily
python scripts/plan_e_v3_live_track.py --mode premarket
```

持仓状态落盘：`~/.local/share/plan_e_journal/state.json` + `journal_YYYY-MM-DD.md`。

### 2.5 自动化调度（本地 · systemd）

```bash
cp systemd/*.{service,timer,path} ~/.config/systemd/user/
sudo loginctl enable-linger $USER
systemctl --user daemon-reload
for t in tdxfeed-sync predict-daily plan-e-journal plan-e-weekly canary-health; do
  systemctl --user enable --now $t.timer
done
systemctl --user enable --now obsidian-sync.path
systemctl --user list-timers --all
```

| Timer | 时间 | 动作 |
|---|---|---|
| `tdxfeed-sync.timer` | 18:00 daily | 增量同步 |
| `predict-daily.timer` | 18:05 daily | 全市场预测 |
| `plan-e-journal.timer` | 18:10 daily | 写实盘 journal |
| `plan-e-weekly.timer` | 周五 20:00 | 周报汇总 |
| `canary-health.timer` | 每 5 min | 健康检查 |

### 2.6 数据不出域保障

| 保障措施 | 说明 |
|---|---|
| 数据源均为公开接口 | baostock/akshare/Sina/Tencent，无私有 API |
| 落盘格式 parquet/CSV | 全部存于 `data/`，无上传通道 |
| LightGBM 预测无网络 | 推理是本地 CPU/GPU 计算 |
| Ollama 仅监听 localhost | `OLLAMA_HOST=localhost`，不暴露公网 |
| 代码仓库私有 | 仅内部使用，禁止推敏感数据到公开远端 |

---

## 3. 方案 D 与方案 E 的分工

### 3.1 方案 D：LightGBM，不依赖任何 LLM

| 项 | 内容 |
|---|---|
| 模型 | LightGBM（18 维特征，joblib 序列化）|
| LLM 依赖 | **无** —— 纯梯度提升树 |
| 输入 | 日 K + 复权因子 + 翻倍样本特征 |
| 输出 | 每只股票翻倍概率 + 特征贡献 |
| 调度 | `predict-daily.timer`（18:05）|

**为什么不依赖 LLM**：① 确定性（同输入同输出，可审计）；② 速度（7620 只 1 分钟）；③ 零边际成本（无需 GPU 推理）；④ 隐私（无文本外发）。方案 D 是"生产主力"，LLM 不参与。

### 3.2 方案 E：分析环节使用 qwen3-14b-tdx-v7

| 项 | 内容 |
|---|---|
| 回测 | 本地 `plan_e_backtest.py`（无 LLM）|
| 实盘跟踪 | 本地 `plan_e_journal.py`（无 LLM）|
| **分析 / 解释 / 对话** | **本地 Ollama：`qwen3-14b-tdx-v7`** |
| 模型特点 | Qwen3-14B + 翻倍样本 LoRA，eval_loss **0.1914**（最优）|

调用示例（本地，数据不离机）：

```bash
curl -s http://localhost:11434/api/generate -d '{
  "model": "qwen3-14b-tdx-v7",
  "prompt": "结合今日 Top30 候选，分析 600519 近 30 日资金面、技术面与翻倍特征",
  "stream": false,
  "options": {"stop": [], "repeat_penalty": 1.0}
}' | python3 -m json.tool
```

> ⚠️ 必须带 `stop:[]` + `repeat_penalty:1.0`，否则只生成 1 token 即停。

### 3.3 敏感数据边界（关键）

| 数据类别 | 是否允许进入 LLM | 说明 |
|---|---|---|
| 公开行情（日 K / 复权因子）| ✅ 本地 LLM 可用 | 已脱敏、非持仓 |
| 翻倍样本统计特征 | ✅ 本地 LLM 可用 | 来自公开数据 |
| **当前持仓明细** | ❌ 禁止外发 | 仅本地处理 |
| **私有策略逻辑 / 参数** | ❌ 禁止外发 | 仅本地处理 |
| **个股财务原始数据** | ❌ 禁止上传云端 | 仅本地 LLM |
| 代码 / 架构 / 文档 | ✅ 云端模型可用 | 不含敏感数据时可 |

---

## 4. 研发与运维环节的云端模型使用场景

下列任务在 **IDE 内由云端大模型**完成（本会话即云端模型），提升研发效率：

| 任务类型 | 具体场景 | 示例 |
|---|---|---|
| **代码编写** | 新脚本/模块实现 | 写 `factor_engine.py`、新增 CLI 命令 |
| **架构设计** | 模块划分 / 数据流设计 | 设计 v3 增量训练流水线 |
| **跨文件重构** | 多文件协同改动 | 统一 v1→v2 schema 迁移脚本 |
| **复杂因子推演** | 因子逻辑 / 防泄漏设计 | 设计 offset 防泄漏、情绪因子扩展 |
| **技术文档分析** | 读 README/论文/API 文档 | 解析 LLaMA-Factory 配置、GGUF 格式 |

**禁止云端模型处理的场景**（必须本地或人工）：
- 上传原始行情、持仓明细、私有策略、财务原始数据
- 让云端模型"直接预测某只股票是否翻倍"（预测属于方案 D/LightGBM 职责）
- 把 `.env` 中的密码 / token 粘贴进云端对话框

**IDE 工作流**

```text
@tdx-predictor  跑今日全市场预测        ← 本地 agent 编排
@tdx-model-trainer 把 v7 部署到 Ollama  ← 本地 agent 编排
# 复杂研发问题直接问云端模型（本会话）
```

---

## 5. 本地 LLM 选型策略

### 5.1 日常 vs 强推理

| 场景 | 选用模型 | 理由 |
|---|---|---|
| **日常分析 / 对话 / 解释** | **`qwen3-14b-tdx-v7`** | 量化领域专精，eval_loss 最优，追求最高准确率 |
| **强推理 / 逻辑推导** | **`deepseek-r1:14b`** | 显式思维链，适合因子推导、异常归因 |
| 快速验证 | `qwen3-14b-tdx-fast` | baseline，9.3GB，轻量 |
| 更高质量（高显存机）| `qwen3:32b` | 需 24GB+ 显存 |
| 语义检索 | `nomic-embed-text` | 274MB 向量嵌入 |

### 5.2 选型决策表

```
任务需要"一步步推理/归因"？
  ├─ 是 → deepseek-r1:14b
  └─ 否 → 需要"A股领域知识/翻倍样本语境"？
              ├─ 是 → qwen3-14b-tdx-v7   ← 日常主用
              └─ 否 → qwen3:14b / fast（通用轻量）
```

### 5.3 切换命令

```bash
# 查看本地模型
ollama list
# 强推理任务
curl -s http://localhost:11434/api/generate -d '{
  "model":"deepseek-r1:14b","prompt":"推导该因子在样本外的过拟合风险",
  "stream":false}' | python3 -m json.tool
# 日常分析
curl -s http://localhost:11434/api/generate -d '{
  "model":"qwen3-14b-tdx-v7","prompt":"解释 Top30 候选的共性特征",
  "stream":false,"options":{"stop":[],"repeat_penalty":1.0}}'
```

---

## 6. 路由决策总表

| 任务 | 路由 | 模型 / 工具 | 数据去向 |
|---|---|---|---|
| 每日全市场预测 | 本地 | LightGBM（`predict.py`）| 不出域 |
| 方案 E 回测 | 本地 | `plan_e_backtest.py` | 不出域 |
| 方案 E 实盘跟踪 | 本地 | `plan_e_journal.py` | 不出域 |
| 候选股深度分析/对话 | 本地 | `qwen3-14b-tdx-v7` | 不出域 |
| 因子逻辑/异常归因 | 本地 | `deepseek-r1:14b` | 不出域 |
| 代码编写 | 云端 | IDE 云端模型 | 仅代码 |
| 架构设计 | 云端 | IDE 云端模型 | 仅设计 |
| 跨文件重构 | 云端 | IDE 云端模型 | 仅代码 |
| 复杂因子推演（方法论）| 云端 | IDE 云端模型 | 不含敏感数据 |
| 技术文档分析 | 云端 | IDE 云端模型 | 仅文档 |

---

## 7. 部署命令速查（落地用）

```bash
# === 1. 环境确认 ===
source venv/bin/activate && python -c "import lightgbm, peft, ollama; print('deps OK')"
nvidia-smi && ollama list          # 确认双 16GB GPU + 8 模型

# === 2. 生产 pipeline（全本地）===
python -m tdxfeed.full_sync
python scripts/predict.py --top 30 --threshold 0.7
python scripts/plan_e_backtest.py --compare
python scripts/plan_e_journal.py --mode daily

# === 3. 启用调度 ===
cp systemd/*.{service,timer} ~/.config/systemd/user/
systemctl --user enable --now tdxfeed-sync.timer predict-daily.timer plan-e-journal.timer plan-e-weekly.timer canary-health.timer obsidian-sync.path

# === 4. 本地 LLM 调用 ===
curl -s http://localhost:11434/api/generate -d '{"model":"qwen3-14b-tdx-v7","prompt":"分析今日候选","stream":false,"options":{"stop":[],"repeat_penalty":1.0}}'
curl -s http://localhost:11434/api/generate -d '{"model":"deepseek-r1:14b","prompt":"推导过拟合风险","stream":false}'
```

---

## 8. 风险与防护

| 风险 | 等级 | 防护 |
|---|---|---|
| 敏感数据误传云端 | 高 | 铁律 1：持仓/策略/财务禁止外发；代码评审时人工核对 |
| `.env` 占位符未注入 | 中 | 部署前 `export POSTGRES_PASSWORD=...` 或 `envsubst` |
| GGUF 转换报 DFLASH | 中 | 用 llama.cpp master 覆盖 venv 内 gguf |
| Ollama 只生成 1 token | 中 | Modelfile + 调用均设 `stop:[]` + `repeat_penalty:1.0` |
| 单卡 16GB 跑不动 f16 | 中 | 双卡（本机）/ q4 量化 / 改 `qwen3:14b` |
| 训练合并 OOM | 中 | CPU 模式合并（`device_map="cpu"`）|
| RAM 仅 30GB（标 32GB）| 低 | 监控 `free -h`，避免多模型同载 |

---

## 9. 验证清单

- [ ] `ollama list` 显示 `qwen3-14b-tdx-v7` 与 `deepseek-r1:14b`
- [ ] `python scripts/predict.py --top 30` 本地出分，无网络请求
- [ ] `plan_e_backtest.py --compare` 本地出 v1/v2 对比
- [ ] `systemctl --user list-timers` 5 个 timer 已启用
- [ ] Ollama 仅监听 `localhost`（无公网暴露）
- [ ] 研发对话中未出现持仓/策略/财务原始数据

---

> **免责声明**：本方案用于量化研究，所有预测/回测结果不构成投资建议。实盘前须通过模拟验证与风控阈值检查。
