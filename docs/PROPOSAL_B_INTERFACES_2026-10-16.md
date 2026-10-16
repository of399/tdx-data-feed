# 方案 B 接口精确设计（2026-10-16 决策）

> 本文件配套 `docs/ROADMAP_2026-10-16.md` §方案 B。
> 用途：把 4 项 T0 改造的 CLI 契约、行为、文件格式、失败处理锁死，避免 GO 后反复来回。
> 状态：用户已答复 4 项接口；第 4 项过滤规则采用默认建议（Top 50 + 阈值 0.7）。

---

## 1. `run_v4_eval` sanity 集成（watchdog 自动）

### 用户答复
- 触发：watchdog 自动
- 阈值：acc 33% / Sp 33% / 三分类各 33%
- 失败动作：停训

### 精确实现

**不动 `run_v4_eval.py` 主逻辑**，仅扩展 `scripts/v9_train_watchdog.sh`：

- 检测新 `checkpoint-XXX` 落地（每 60s 轮询 output_dir）
- 自动调用：
  ```bash
  venv/bin/python training/eval/run_v4_eval.py \
      --ckpt <新ckpt> \
      --limit 200 \
      --out-prefix logs/v4fwd_sanity/ckpt-XXX
  ```
- 解析 stdout 末行（`direction_acc=… spearman=… cls_acc=[up,down,flat]=…`）
- 判定 5 指标：
  - `direction_acc ≥ 0.33`
  - `spearman ≥ 0.33`
  - `cls_acc_up ≥ 0.33`
  - `cls_acc_down ≥ 0.33`
  - `cls_acc_flat ≥ 0.33`
- 任一不满足 →
  - `kill -TERM <训练pid>`（已设的 `overwrite_output_dir: false` 安全停训）
  - 写 `logs/v4fwd_sanity/ckpt-XXX.FAILED`
  - console 报警（`pushplus-bridge` 当前退役，暂仅 console）

### run_v4_eval.py 已支持参数
`--ckpt / --base / --limit / --batch-size / --threshold / --out-prefix`，**无需新增 CLI**。

### 输出契约
- `logs/v4fwd_sanity/ckpt-XXX/summary.json`：sanity 结果（pass/fail + 5 指标值）
- `logs/v4fwd_sanity/ckpt-XXX.FAILED`：sanity 失败标记（仅当 fail）
- `logs/v9_train_watchdog.log`：watchdog 主日志

---

## 2. `predict.py --emit-top` 改造（叠加模式）

### 用户答复
- 与已有 `--output` 叠加
- 路径：`data/signals/predict_*.csv`
- Top N + 阈值 + 字段三维度并行

### 精确实现

**4 个新参数**：

```python
parser.add_argument('--emit-top', action='store_true',
                    help='额外写一份 Top N 信号到 data/signals/(与默认输出叠加,默认关闭)')
parser.add_argument('--emit-top-n', type=int, default=50,
                    help='--emit-top 输出 Top N 默认 50')
parser.add_argument('--emit-threshold', type=float, default=0.7,
                    help='--emit-top 阈值过滤 默认 0.7')
parser.add_argument('--emit-fields', default='code,score,ts,top_k',
                    help='--emit-top 输出字段 逗号分隔,默认核心 4 字段')
```

### 行为

- 不加 `--emit-top` → 行为完全不变（默认输出 `data/predictions/predict_*.csv` 自动生成）
- 加 `--emit-top` → 在默认输出基础上，**额外**写：
  ```
  data/signals/predict_YYYYMMDD_HHMMSS_top{N}_thr{0.7}.csv
  ```
- 三维度并行合法，例如：
  ```bash
  scripts/predict.py --emit-top --top 100 --threshold 0.6 \
                     --emit-top-n 30 --emit-threshold 0.75
  ```
  → 默认 top 100 输出到 `data/predictions/` + emit-top 30 额外输出到 `data/signals/`

### 新增依赖
- `pathlib` 已有
- `data/signals/` 目录自动 `mkdir(parents=True, exist_ok=True)`

### 字段说明
- `code`：股票代码（6 位）
- `score`：预测得分
- `ts`：预测时间戳
- `top_k`：在 Top N 内的排名（1-indexed）
- 用户可通过 `--emit-fields` 自定义，例如 `--emit-fields code,score,volatility,turnover`

---

## 3. `plan_e_backtest.py --codes-file` 改造（叠加输入源）

### 用户答复
- 与 `--codes` 单只参数叠加（但当前**没有 `--codes` 单只参数**——本项目为新增输入源）
- 文件格式：含 header + comment
- 错误：**不容错**（立即退出非零）

### 精确实现

**2 个新参数**：

```python
parser.add_argument('--codes-file', type=str, default=None,
                    help='外部股票代码列表文件路径(与现有批量回测叠加)')
parser.add_argument('--codes-file-comment-prefix', default='#',
                    help='comment 行前缀 默认 #')
```

### 文件格式契约

- 编码：UTF-8
- **header 必须存在**（第 1 行），首列名 `code`（含 sh/sz 前缀的 6/7 位代码）
- comment 行：以 `--codes-file-comment-prefix` 开头（默认 `#`）→ 跳过
- 空行 → 跳过
- 重复 code → 报错退出（**不容错**）
- 格式错（列名不符 / 缺列）→ 立即 `sys.exit(1)` + 详细错误

### 与现有 batch 关系

- 不加 `--codes-file` → 行为完全不变（全市场批量回测）
- 加 `--codes-file` → **限定**回测范围为文件中代码子集（其他参数 `--hold/--tp/--sl/--mf/...` 全部适用）

### 错误信息示例

```
ERROR: --codes-file=foo.csv 格式错
  - 第 3 行缺 'code' 列
  - 期望列: ['code']，实际列: ['stock', 'weight']
  参考格式: 参见 docs/PLAN_E_LIVE.md §"codes-file 格式"
```

---

## 4. `plan_e_journal.py --from-predict-list` 改造（自动信号→入库）

### 用户答复
- 取最新文件
- 过滤规则：默认建议 Top 50 + 阈值 0.7
- 双轨分流：实盘→`journal.csv`（source=real）、模拟→`journal_sim.csv`（source=sim），通过 `--mode` 切换

### 精确实现

**1 个新参数**：

```python
parser.add_argument('--from-predict-list', action='store_true',
                    help='从 data/predictions/predict_*.csv 取最新文件作为买入信号源(默认 Top 50 + 阈值 0.7 过滤)')
```

### 行为

- 不加 `--from-predict-list` → 行为完全不变（手动 `--code/--price/--qty` 模式）
- 加 `--from-predict-list --mode real` →
  1. 取 `data/predictions/predict_*.csv` 最新文件（按 mtime 倒序）
  2. 过滤：Top 50 + score ≥ 0.7
  3. 对每只入选股**模拟逐笔买入**（按当前价 × 固定资金分配）
  4. 写入 `journal.csv`（source=real，**注意是 paper-trading 而非真金白银**）
- 加 `--from-predict-list --mode simulate` → 同上但写入 `journal_sim.csv`

### 默认值（用户可后续调整）

| 维度 | 默认 | 来源 |
|---|---|---|
| Top N | 50 | 与 `predict.py --top 20` 默认略放大（journal 想更多信号） |
| 阈值 | 0.7 | 与 `predict.py --threshold 0.7` 默认一致 |
| 文件发现 | `data/predictions/predict_*.csv` 按 mtime 倒序第 1 个 | 自动 |
| 资金分配 | 模拟：等额（总资金 / N），实盘：同 | 简化 |

### 失败处理

- `data/predictions/` 无文件 → 立即 `sys.exit(1)` + 错误信息
- 最新 predict 文件格式错（缺 code/score 列）→ 立即 `sys.exit(1)` + 错误信息
- 过滤后空集（Top 50 + 0.7 阈值无符合）→ 写警告日志，正常退出（不写 journal）

---

## 实施顺序（T0-GO 之后）

1. **并行**启动 4 项改造（互不依赖）：
   - `run_v4_eval` watchdog 集成
   - `predict.py --emit-top` 加 4 个参数
   - `plan_e_backtest.py --codes-file` 加 2 个参数 + 文件解析函数
   - `plan_e_journal.py --from-predict-list` 加 1 个参数 + 文件发现/过滤函数
2. **每个改造完成后**单独 ruff check（保持 0 错误）
3. **4 项完成**后做端到端联调测试（用历史 `data/predictions/predict_*.csv` 跑 backtest + journal）
4. **联调通过**后写 `docs/PLAN_E_LIVE.md` 更新章节

---

## 阶段验收（T0-T5，与 ROADMAP §方案 B 一致）

- [ ] T0：4 项代码改造完成 + ruff 0 错误 + 单测
- [ ] T1：端到端联调测试通过（用历史 predict 输出回测 + 入库全链路）
- [ ] T2：v4fwd checkpoint-200 sanity 通过（5 指标 ≥ 0.33，方向判别非 94% 偏涨）
- [ ] T3：PnL Huber 模型训练完成
- [ ] T4：4 模型 OOS Sp 数值表 + 对比图
- [ ] T5：v9 部署 + predict 默认切换 + v8 降级 fallback 30 天

---

## 相关文件

- 上游规划：`docs/ROADMAP_2026-10-16.md` §方案 B
- 上游审查：`docs/WORKFLOW_REVIEW_2026-10-16-v2.md` §12
- 待改脚本：
  - `training/eval/run_v4_eval.py`（仅 watchdog 集成，主逻辑不动）
  - `scripts/v9_train_watchdog.sh`（watchdog 主脚本，需扩展）
  - `scripts/predict.py`（+4 参数）
  - `scripts/plan_e_backtest.py`（+2 参数 + 文件解析）
  - `scripts/plan_e_journal.py`（+1 参数 + 文件发现/过滤）
- 需新建文档：`docs/PLAN_E_LIVE.md`（联调章节，T1 阶段写）
