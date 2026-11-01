# GitHub 资源筛选与升级方案 (2026-10-10 18:20)

> 检索说明（更新于 2026-10-10）：本环境 GitHub MCP 令牌已校验**有效**（`HTTP 200`，账号 `of399`）。docker 版 `github` 与 npx 版 `GitHub` 两个服务器现已用同一有效 PAT 对齐（此前 npx 版长期为空占位符、docker 版未验证，故曾误判 401）。**生效须知**：改令牌类配置后须**完整重启 IDE**（关窗口再开）方能在 MCP 直连检索中生效——仅"重启 MCP"只重连、不重读磁盘。

## 📊 核心指标（现状基线 → 改造目标）

**现状基线**
- 模块：数据采集（`full_sync`/`incremental`/`min_sync`）+ 方案D 预测 + 方案E 回测 + LoRA 训练（v7r）+ 实盘 journal + systemd 调度 + 容器化
- 已用关键库：`akshare`（新浪后端）、Sina/Tencent/pytdx 源、`pandas`/`pyarrow`、LightGBM、LLaMA-Factory/peft/transformers、Ollama
- 已知短板：① `pytdx` 公共服务器实测返回 0 根（主通道失效，全靠 Sina 兜底）；② 特征为 `predict.py` 手写 18 维，缺因子引擎；③ 方案E 自写 pandas 回测，绩效统计手算；④ 模型 v7r 续训≈随机、v8r 退化；⑤ 实盘仅 journal 观测，无执行层；⑥ 推理走 Ollama，吞吐受限

**改造目标（量化）**
1. 数据采集可靠性：替换失效的 pytdx 主通道 → 目标日线/指数同步零失败
2. 特征工程：引入向量化/因子引擎 → `predict` 特征计算耗时降低 ≥50%，可扩展新因子
3. 模型质量：引入 `optuna`+`unsloth` → 方案D 验证准确率突破当前 ≈48~49% 基线（目标 55%+ 或 AUC 显著提升）
4. 回测标准化：`backtrader`/`vectorbt` + `quantstats` → 方案E 产出专业 tearsheet，向量化场景回测提速 ≥10×
5. 实盘闭环：`vnpy` 执行层 → 从 journal 观测走向可下单/风控
6. 推理服务：`vLLM` → 每日预测批量推理吞吐提升 ≥3×

## 🎯 Top 资源明细（按用途分组，星标为 2026-10-10 实时值）

### A. 数据采集层
| 仓库 | Star | 语言 | 对本工作空间的用途与价值 |
|---|---:|:---|:---|
| `akfamily/akshare` | **22,878** | Python | 已用主数据源；社区极活跃（2026-10 仍每日更新），覆盖股票/基金/期货/期权/宏观。保留并作为统一数据接口层，注意本环境东财后端被墙、须走新浪后端。 |
| `mootdx/mootdx` | **2,412** | Python | 活跃的通达信数据读取封装，**替代已归档（2020 停更）的 `rainx/pytdx`（1,552★）**。替换 `full_sync` 的 pytdx 主通道，提升本地/指数数据可靠性与期货、期权覆盖。 |
| `akfamily/akquant` | **2,401** | Python（Rust 内核） | akshare 作者出的高性能量化框架：内置 Polars 因子表达式引擎、TA-Lib 双后端（103 指标）、多进程网格搜索。**直接补齐工作空间缺失的因子工程与参数优化**，可重构 `predict.py` 的 18 维特征计算。 |

### B. 回测 / 交易引擎层
| 仓库 | Star | 语言 | 对本工作空间的用途与价值 |
|---|---:|:---|:---|
| `mementum/backtrader` | **23,443** | Python | 经典事件驱动回测（2024 停更但有大量 fork）。作为方案E 自写 pandas 回测的**标准化底座**，完善佣金/滑点/仓位模型与可维护性。 |
| `polakowo/vectorbt` | **9,319** | Python | 向量化回测引擎。适合方案E 全市场 7,000+ universe 与 27 组敏感性网格（`--sensitivity`）的大规模扫描，速度远超事件驱动。 |
| `vnpy/vnpy` | **45,779** | Python | 开源量化交易平台，提供 CTP 等实盘接口。作为 **predict→实盘的"执行层"升级**，承接 plan-e journal 的实盘观测走向真实下单与风控；本环境已验证 py3.14 兼容。 |
| `nautechsystems/nautilus_trader` | **29,778** | Rust | 生产级低延迟事件驱动引擎。远期高吞吐实盘选项，**集成成本较高（Rust 栈）**，列为候选而非近期落地。 |

### C. 绩效 / 风险分析层
| 仓库 | Star | 语言 | 对本工作空间的用途与价值 |
|---|---:|:---|:---|
| `ranaroussi/quantstats` | **7,688** | Python | 组合绩效/风险分析（夏普/索提诺/回撤/收益归因），可一键生成 tearsheet。**替代/增强 `plan_e_backtest.py` 手算统计**，直接产出专业报告，满足合规与可读性要求。 |

### D. 机器学习 / LLM 层
| 仓库 | Star | 语言 | 对本工作空间的用途与价值 |
|---|---:|:---|:---|
| `lightgbm-org/LightGBM` | **18,852** | C++ | 方案D 预测模型（已用）；保持并评估 GPU/分布式训练提速（现 `d_signal_5d_model.txt`，89 棵树）。 |
| `optuna/optuna` | **14,906** | Python | 贝叶斯超参优化。自动调参方案D 模型与方案E 参数（`--retune`），并可用于特征选择，缓解"人工/网格调参"低效。 |
| `hiyouga/LlamaFactory` | **75,389** | Python | 已用于 LoRA 微调（v7r）；保持并跟踪更新，本环境已验证 py3.14 + 4bit QLoRA 可用。 |
| `unslothai/unsloth` | **77,656** | Python | 加速 LoRA 训练、更省显存，支持 Qwen3/GGUF/MLX。在 2×16GB 上**缓解 v8r 退化/续训未提升**，可作 LLaMA-Factory 的补充训练栈。 |
| `vllm-project/vllm` | **93,474** | Python | 高吞吐 LLM 推理服务。替代 Ollama 做**每日预测批量推理与并发实盘查询**，量化（GPTQ/AWQ）更优。 |

### E. 时间序列预测扩展
| 仓库 | Star | 语言 | 对本工作空间的用途与价值 |
|---|---:|:---|:---|
| `Nixtla/nixtla` | **4,020** | Jupyter | 时间序列基础模型（TimeGPT），预测/异常检测。作为方案D 树模型的**对照或融合特征**（需严格验证金融预测有效性，勿直接当信号）。 |

### F. 学习 / 模板 / 最佳实践
| 仓库 | Star | 语言 | 对本工作空间的用途与价值 |
|---|---:|:---|:---|
| `0voice/Awesome-QuantDev-Learn` | **1,143** | — | 量化开发系统学习路线（数据→策略→回测→实盘）。团队/个人补齐量化工程知识与最佳实践的参考模板。 |

## 📈 统计分布
- **覆盖维度**：数据采集 3 项、回测/执行 4 项、绩效分析 1 项、ML/LLM 5 项、时间序列 1 项、学习模板 1 项，共 **15 个候选项目**
- **已用并保留**：`akshare`、`LightGBM`、`LlamaFactory`（3 项）
- **直接替代/补缺**：`mootdx`（替 pytdx）、`akquant`（补因子引擎）、`quantstats`（补绩效报告）、`optuna`（补调参）、`unsloth`（补训练效率）、`vllm`（替 Ollama）
- **标准化升级**：`backtrader`/`vectorbt`（替自写回测）、`vnpy`（补实盘执行）
- **活跃度风险项**：`rainx/pytdx`（已归档 2020）、`mementum/backtrader`（2024 停更）——优先用 `mootdx`、`vectorbt`/`vnpy` 等活跃项目

## ⚠️ 风险提示
1. 历史回测不代表未来收益；模型基于 2020-2026 数据训练，2026 年新模式可能失效
2. 翻倍策略属于激进策略，建议小资金启动；严格止损（-10%）防止黑天鹅
3. **依赖维护风险**：`pytdx` 已归档、`backtrader` 停更；新引入库须评估活跃度与许可证（如 vLLM Apache-2.0、vnpy MIT）
4. **技术栈集成成本**：`nautilus_trader`（Rust）、`akquant`（Rust 内核）、`vLLM`（CUDA 生态）引入会增加构建与运维复杂度，应分阶段试点
5. **Ollama→vLLM 迁移风险**：API 形态与量化格式不同，需改造 `predict.py` 推理调用并重新验证输出一致性
6. **金融预测有效性**：`Nixtla` 等基础模型在 A 股价量预测上的有效性未经证实，仅作对照/特征，禁止直接当作买卖信号
7. **合规**：实盘执行（vnpy/CTP）涉及交易接口与资金，须用户自行评估资质与风控，本方案仅作技术准备

## 🔧 下一步建议 / 实施步骤

**Phase 0 — 数据层加固（高优先，1~2 天）**
- 用 `mootdx` 替换 `pytdx_source.py` 的 pytdx 主通道，保留 Sina 兜底；在 `verify_daily_freshness.py` 加"源失败率"指标，目标同步零失败
- 将 `full_sync` 指数通道的 Sina 兜底与 `mootdx` 对齐，统一 volume ÷100 处理（复用现有修复）
- 回填 GitHub MCP 令牌，启用 MCP 直连检索（便于后续持续跟踪上游更新） — ✅ 已完成（2026-10-10：令牌校验有效 HTTP 200 / 账号 of399，docker 版与 npx 版已对齐；须完整重启 IDE 生效）

**Phase 1 — 特征/因子工程（1 周）**
- 试点 `akquant` 的 Polars 因子引擎，将 `predict.py` 手写 18 维特征迁移为因子表达式，验证数值一致后切换；目标特征计算耗时 ↓≥50%
- 新增 5~10 个候选因子（动量/波动率/量价背离），经 `optuna` 做特征选择后再入模

**Phase 2 — 模型升级（2~3 周）**
- 引入 `unsloth` 重训 Qwen3-14B LoRA（更大 rank/更长步数），对照 v7r；引入 `optuna` 调 LightGBM 超参
- 落地 MEMORY 已规划的"结构化标签/回归头/排序学习/Focal Loss"，目标方案D 验证准确率突破 ≈48~49% 基线
- 用 `Nixtla` 生成对照预测特征，做消融实验（仅作特征，不当信号）

**Phase 3 — 回测标准化（1~2 周）**
- 将方案E 核心逻辑移植到 `vectorbt`（向量化，覆盖 7,000+ universe + 27 组网格），保留 `plan_e_backtest.py` 作为对照
- 引入 `quantstats` 生成专业 tearsheet，替换手算夏普/索提诺/Calmar；保持 n≥30 显著性闸口与现有 `--real`/`--sensitivity` 开关

**Phase 4 — 实盘执行层（2~4 周，需用户资质）**
- 引入 `vnpy` 作为执行/风控层，消费 plan-e journal 的实盘信号做下单与仓位/止损管理；先模拟盘验证
- （远期候选）评估 `nautilus_trader` 低延迟实盘

**Phase 5 — 推理服务升级（1 周）**
- 部署 `vLLM` 承载合并模型（`qwen3-14b-tdx-v7r` 等），改造 `predict.py` 推理调用；对比 Ollama 吞吐，目标 ↑≥3×；保留 Ollama 作兜底

**Phase 6 — 工程化收口（持续）**
- 数据质量门禁（源覆盖率/连续性/除权跳空伪翻倍复核）、模型版本 registry、回测↔实盘一致性校验
- 更新 `docs/` 设计文档与 README，沉淀引入库的适配说明与回滚方案

---
生成时间：2026-10-10 18:20:00
生成者：GitHub 资源调研 + 升级规划（Agent）
模型版本：当前部署 qwen3-14b-tdx-v7r（参考）；本报告为工程规划，非模型推理
数据日期：2026-10-10（资源元数据来自 GitHub 公开 API 实时核验）
