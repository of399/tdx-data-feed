# 模型切流：DEFAULT_MODEL 正式切换为 daban（小仓位场景启用）

- 切换日期：2026-10-19
- 改动文件：`scripts/predict.py`
- 切换依据：`docs/COMPARE_EXTENDED_2026-10-19.md`（历史累积验证）
- 状态：✅ 已切换，5 条路径验证通过

---

## 1. 切换内容

| 项 | 切换前 | 切换后 |
|---|---|---|
| `DEFAULT_MODEL` | `vpnl_huber_stage1` | **`daban`** |
| 默认 Top N | 20 | **5**（小仓位场景）|
| 仓位模式 | 无 | 新增 `--position-mode {small,full}` |
| 异常处理 | 模型缺失即 `sys.exit` | **自动回退 stage1 + 告警** |

### 切换依据（历史累积，非单日实盘）

| 指标 | daban | stage1 | 优势 |
|---|---:|---:|---|
| OOS AUC（334 交易日 / 199,283 样本）| **0.8090** | 0.7526 | +0.0563 |
| Top5 命中率 2025 | **24.52%** | 11.64% | 2.11× |
| Top5 命中率 2026 | **21.70%** | 11.70% | 1.85× |

两个独立年度均稳定领先，无过拟合迹象。

---

## 2. 小仓位启用条件

「小仓位」在本项目落地为**显式语义**，而非隐式约定：

| 仓位模式 | 模型 | 默认 Top N | 语义 |
|---|---|---:|---|
| **`small`（默认）** | `daban`（次日涨停二分类）| **5** | 少而精：专用模型 + 少标的 |
| `full` | `vpnl_huber_stage1`（波段 PnL 回归）| 20 | 稳健优先：通用模型 + 多标的 |

**daban 仅在 `small` 模式下作为默认模型启用**；`full` 模式仍走稳健的 stage1。

---

## 3. 模型解析优先级（可控性）

```
1. --model X            （最高优先，显式指定，用于回退/调试）
2. TDX_MODEL=X 环境变量 （不改代码即可临时覆盖）
3. --position-mode      （small→daban / full→stage1）
```

每次运行都会打印**来源标注**，例如：
```
加载模型[daban]: .../d_signal_5d_model_daban.txt（来源: --position-mode small）
```

---

## 4. 生效范围（边界）

| 范围 | 是否受影响 | 说明 |
|---|---|---|
| `predict.py` 主流程（CLI）| ✅ 受影响 | 默认模型 → daban |
| `predict.py --web`（Web UI）| ✅ 受影响 | 同样走 `resolve_model` |
| `plan_e_backtest.py` | ❌ **不受影响** | 纯规则策略，不加载任何模型 |
| `train_daban_model.py` | ❌ 不受影响 | 训练脚本，与推理无关 |
| 其他模型（v9/vpnl/...）| ❌ 不受影响 | `--model` 显式指定仍可用 |

---

## 5. 异常与回退处理

### 5.1 自动回退（运行时不中断）

| 异常 | 行为 |
|---|---|
| 目标模型**文件缺失** | `[WARN]` 告警 → 自动切到 `FALLBACK_MODEL`（stage1）→ 继续运行 |
| 目标模型**加载失败** | 同上 |
| 回退模型也不可用 | `sys.exit`（明确失败，不静默）|

### 5.2 手动回退（三种方式，任选其一）

```bash
# 方式 1：显式指定（单次）
venv/bin/python scripts/predict.py --model vpnl_huber_stage1

# 方式 2：环境变量（当前 shell 会话内生效，不改代码）
TDX_MODEL=vpnl_huber_stage1 venv/bin/python scripts/predict.py

# 方式 3：大仓位模式
venv/bin/python scripts/predict.py --position-mode full
```

---

## 6. 验证结果（行为一致性）

| # | 场景 | 命令 | 预期 | 实测 |
|---|---|---|---|---|
| 1 | 默认（小仓位）| `predict.py --codes 600000` | daban + Top 5 | ✅ |
| 2 | 大仓位模式 | `--position-mode full` | stage1 + Top 20 | ✅ |
| 3 | 显式指定优先 | `--model vpnl_huber_stage1` | stage1（覆盖 position-mode）| ✅ |
| 4 | 环境变量覆盖 | `TDX_MODEL=vpnl_huber_stage1` | stage1 | ✅ |
| 5 | 异常自动回退 | 临时移除 daban 模型文件 | 告警 + 回退 stage1 | ✅ |
| 6 | 显式 Top 优先 | `--top 3` | Top 3（覆盖模式默认）| ✅ |

---

## 7. 后续监控要求

| 监控项 | 频率 | 阈值 |
|---|---|---|
| 实盘 Top5 命中率 vs 回测基线（21.7~24.5%）| 每 2 周 | 若实盘持续低于回测 **10pp 以上** → 瓶颈在可执行性，需排查成交率/滑点 |
| daban 模型文件存在性 | 每日 canary | 缺失即触发自动回退（已有）|
| AUC 衰减 | 每季度 | 参考 2025→2026 衰减 -0.0167，必要时重训 |

---

## 8. 已知限制

1. **回测无法覆盖的可执行性风险**：排队买不进、滑点、隔夜跳空（详见 `COMPARE_EXTENDED_2026-10-19.md` 第 5 节）。
2. **2026 自然涨停率升至 1.73%**：模型在更热的市场中训练，未来市场转冷时命中率可能下降。
3. **`--position-mode full` 的 Top 20**：这是设计默认值，非实测最优；如需调整改 `main()` 中联动逻辑。
