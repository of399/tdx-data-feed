# 第三轮代码审计（Round3）

- 审计日期：2026-10-19
- 前置：Round1 全量审计（`CODE_AUDIT_2026-10-17.md`，commit `ddf142e`）、Round2 复核（`CODE_AUDIT_ROUND2_2026-10-18.md`，commit `26b26df`）
- 本轮定位：**增量审计** —— 验证前两轮修复未回退 + 审查 10-18/19 新增代码 + 找新问题
- 修复 commit：`479368e`

---

## 0. 结论摘要

| 维度 | 评分 | 核心结论 |
|---|---|---|
| 模块分层 | 🟢 | `tdxfeed → quant → scripts` 单向依赖仍成立 |
| 重复代码 | 🟡 | **新发现**：涨停判定逻辑在 **5 个文件**重复（可提取 3 个公共函数）|
| 代码质量 | 🟡 | 新脚本 docstring 覆盖率 **17%~33%**（低）|
| 错误处理 | 🟢 | **回退已修复**：吞错 4→6→**4** |
| 性能 | 🟢 | predict 全市场 **40 秒**（P1 删除缓存后显著改善）|
| 依赖/配置 | 🟢 | `requirements.lock` 在位；0-import 依赖 **全为 0** |
| 测试 | 🟢 | **96 passed**（Round1 时 82）|

**本轮实际落地 1 个 commit**（`479368e`），修复 1 项回退 + 3 项新脚本质量问题。剩余为 P1/P2 建议。

---

## 1. 代码结构与模块划分

### 1.1 本次变更范围（10-18 ~ 10-19）

| 文件 | 变更 |
|---|---|
| `scripts/predict.py` | 改：`MODEL_REGISTRY` 加 `daban` + 2 处吞错留痕 |
| `scripts/train_daban_model.py` | 改：`--exclude-open-limit` 开关 |
| `scripts/verify_daban_hit.py` | **新增**（133 行）：命中率核对 |
| `scripts/compare_daban_vs_stage1.py` | **新增**（233 行）：双模型同口径对比 |
| `scripts/diagnose_sample_design.py` | **新增**（191 行）：样本设计诊断 |
| `scripts/factor_engine.py`、`quant/multifactor/factors.py` | 改：`norm_code` 契约注释 |
| `tdxfeed/features_5d.py`、`tdxfeed/factors_polars.py` | 改：涨停/连板特征 |

### 1.2 分层依赖

- `tdxfeed`（数据/特征）← `quant`（多因子）← `scripts`（任务脚本）：**单向，无循环** ✅
- 新增 3 个脚本均通过 `from train_daban_model import ...` / `from _common import ...` 复用，**未引入新的跨层依赖** ✅
- `v5/` 仍是平行宇宙（sys.path 硬编码 + 独立 systemd），Round2 已判定"README 边界声明、不包化"，本轮维持。

---

## 2. 代码质量

### 2.1 新增脚本 docstring 覆盖率（偏低）

| 脚本 | 行数 | 函数数 | 有 docstring | 覆盖率 |
|---|---:|---:|---:|---:|
| `verify_daban_hit.py` | 133 | 3 | 1 | **33%** |
| `compare_daban_vs_stage1.py` | 233 | 4 | 3 | 75% |
| `diagnose_sample_design.py` | 191 | 6 | 1 | **17%** |
| `train_daban_model.py` | 203 | 6 | 1 | **17%** |

模块级 docstring 均完整（说明用途/用法），但**函数级注释不足**，内部辅助函数（`_auc`、`diag_one`、`process_stock` 等）缺少职责说明。

### 2.2 命名与可读性

- 命名整体规范（`is_limit_up_today`、`consecutive_boards`、`board_position` 语义清晰）✅
- 可读性主要扣分项：`diagnose_sample_design.py` 的 `diag_one` 单函数承担「标签计算 + 可交易判定 + 一字板统计」三职，约 60 行，职责略重。

---

## 3. 重复代码 / 冗余逻辑 / 可合并模块

### 3.1 核心发现：涨停判定逻辑 5 处重复

| 模式 | 出现文件 |
|---|---|
| `ret >= (limit_val - 0.001)` 收盘涨停判定 | `train_daban_model.py`、`diagnose_sample_design.py`、`compare_daban_vs_stage1.py`、`verify_daban_hit.py`、`tdxfeed/factors_polars.py`（**5 处**）|
| `open/prev_close - 1 >= limit - 0.001` 开盘涨停（不可交易）判定 | `train_daban_model.py`、`compare_daban_vs_stage1.py`（**2 处**）|
| `open == high == low == close` 一字板判定 | `diagnose_sample_design.py`、`compare_daban_vs_stage1.py`（2 处）|

**风险**：这些判定是标签与过滤的核心口径，**任何一处微调不同步就会造成口径漂移**。例如本次改造 `--exclude-open-limit` 就需同步改 2 处。

**建议**：提取 3 个纯函数到 `tdxfeed/features_5d.py`（已有 `get_limit`，是最自然的归属）：

| 建议函数 | 语义 |
|---|---|
| `is_limit_up(close, prev_close, limit)` | 收盘涨停判定 |
| `is_open_limit_up(open, prev_close, limit)` | 开盘即涨停（不可买入）|
| `is_yizi(open, high, low, close)` | 一字板（四价合一）|

**预期收益**：5 处 → 1 处，消除口径漂移风险；新增口径只需改一处。
**潜在风险**：低（纯函数、无副作用）。但需注意 `factors_polars.py` 为向量化实现（polars 表达式），与标量版需保持**语义一致**，应补 parity 测试。

### 3.2 其他重复（维持 Round1 结论，未恶化）

- `plan_e_*` 13 文件、`_norm_code` 已统一（`is_st` 4 种语义留 P2）——本轮无恶化。
- `_PARQUET_CACHE` 已删（P1），predict.py 中仅剩注释引用，无功能残留 ✅

---

## 4. 性能瓶颈与资源使用

| 项 | 本轮实测 | 评价 |
|---|---|---|
| `predict.py` 全市场（6882 只）| **40 秒** | 🟢 显著改善（Round1 记录 12 min 是环境争抢异常值；P1 删除 `_PARQUET_CACHE` 后真实水位约 40 秒）|
| `train_daban_model.py` 全量 | 约 17-19 min | 🟡 特征计算为 O(n²)（`compute_window_features_extended` 逐点调用），已在对比脚本中以「抽样窗口」规避 |
| `verify_daban_hit.py` | 秒级（逐票读 parquet）| 🟢 |
| 内存 | `_PARQUET_CACHE` 已删，无常驻大缓存 | 🟢 |

**结论**：无新增性能问题。已知的 O(n²) 特征计算是历史设计，本轮未恶化，且已有规避手段（`--limit` + `--recent` 窗口）。

---

## 5. 错误处理与边界条件

### 5.1 本轮发现回退（已修复）

P0 已把 37 处吞错降到 4 处，但 **P1 改造新增 2 处**未留痕：

| 位置 | 问题 | 严重性 |
|---|---|---|
| `predict.py`（freshness 防御）| `except Exception: pass` | 中 |
| `predict.py`（`load_names`）| `except Exception: pass` → 返回空 dict | **高**：会致**实时 ST 校验静默失效**，ST 股重新漏网（正是 P1-5 要防的漏洞）|

**已修复**：均改为 `print(f"[WARN] ...{e!r}")`，控制流不变。复测：吞错 **6 → 4**（回到预期水位，剩余 4 处均为 `v5/tests` 故意异常路径）。

### 5.2 新脚本 except 无留痕（已修复）

`compare_daban_vs_stage1.py` 与 `diagnose_sample_design.py` 的 `read_parquet` 失败时 `return None` 但无日志 —— 样本会**无声丢失**，导致统计口径与预期不符时无法诊断。已补 `[WARN]` 打印。

### 5.3 边界条件覆盖（评价）

- `verify_daban_hit.py`：已覆盖「parquet 缺失」「数据不足 30 行」「前收为 0」「无前收」「未同步」等 ✅
- `train_daban_model.py`：`close <= 0` 跳过、`len(df) < 30` 跳过 ✅
- 仍缺失：一字板判定未考虑「ST 股不同涨跌幅限制」（`get_limit` 已按板块，ST 未特殊处理）

---

## 6. 项目配置 / 构建流程 / 依赖管理

| 项 | 状态 |
|---|---|
| `requirements.lock` | ✅ 在位 |
| 0-import 依赖 | ✅ plotly/schedule/seaborn/ollama/deepspeed/trl **全部 0**（P0 清理保持）|
| 关键包 pin | ✅ `torch<3` / `transformers<5` / `llamafactory<0.10` / `polars<3` |
| `Makefile` | 88 targets；`sync-lock` 已加（P0）|
| `pip check` | ✅ 已加在 `install` 末尾（软警告版）|
| tests | ✅ **96 passed**（Round1 时 82，新增 14 个）|
| systemd | 18 service + 12 timer（双轨仍在，Round2 已判定留 P2）|

**本轮未发现新增依赖冲突。**

---

## 7. 优化建议（按优先级）

### P0 —— 已完成（本轮）

| # | 建议 | 状态 |
|---|---|---|
| 1 | 修复 2 处吞错回退（含 `load_names` 安全相关）| ✅ `479368e` |
| 2 | 新脚本 ruff 清理（import 排序 + 3 个 F841）| ✅ `479368e` |
| 3 | 新脚本 except 补 WARN 留痕 | ✅ `479368e` |

### P1 —— 建议近期做

| # | 建议 | 预期收益 | 潜在风险 |
|---|---|---|---|
| 4 | **提取 `is_limit_up` / `is_open_limit_up` / `is_yizi` 到 `features_5d.py`，替换 5 处重复** | 消除口径漂移风险；新口径只改一处 | 低；`factors_polars.py` 为向量化实现，需补 parity 测试确保语义一致 |
| 5 | 为新脚本补函数级 docstring（`diagnose`/`train_daban` 仅 17%）| 可维护性提升 | 零 |
| 6 | `train_daban_model.py` 的 O(n²) 特征计算改向量化批量 | 17 min → 预估 3-5 min | 中；需 golden 快照验证位级一致（可参照 `test_stage1_factors_parity.py`）|

### P2 —— 稳定期立项

| # | 建议 | 预期收益 | 潜在风险 |
|---|---|---|---|
| 7 | `plan_e_*` 13 文件整合 | 减少维护面 | 中；需逐个验证 |
| 8 | `v5/` 双轨去留决策 | 减少 sys.path 硬编码与双 systemd 复杂度 | 中；19 个 systemd 依赖，需谨慎 |
| 9 | `is_st` 4 种语义统一 | 消除 ST 判定口径分歧 | 中；int 特征版 vs bool 语义不同，需逐个切换 |
| 10 | 全仓既有风格问题清理（E702 28 / B905 18 / SIM115 17）| ruff 全绿 | 低；纯风格，但改动面大，宜分文件渐进 |

---

## 8. 方法论沉淀（Round3 新增）

1. **修复本身也会回退**：P0 修完的吞错，在 P1 改造中新增 2 处。说明"一次性修复"不够，需把**吞错检查纳入常规审计项**。
2. **安全相关的静默失败要单独识别**：`load_names` 的 `except: pass` 表面看是"读不到就用默认值"，但它的下游是**安全防线**（ST 校验）——静默失败等于防线失效。这类"防线本身的失败"应优先加留痕甚至直接报错。
3. **改造新增的代码同样要过审计**：本轮 3 个新脚本都是改造副产物，若不做 Round3，其质量问题（docstring 17%、无留痕 except）会长期沉淀。
