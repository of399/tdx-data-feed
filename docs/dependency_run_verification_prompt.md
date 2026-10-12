# 依赖安装与跑通验证提示（tdx-data-feed · 2026-10-06）

> ⚠️ **已合并**：本文档已与 `training_stack_prompt.md` 合并为端到端指南 `docs/training_stack_e2e_guide.md`（含今日单卡 QLoRA 实测结论、细粒度报错分类、✅/⚠️/🔲 状态图例）。请以合并版为准。

> 用途：作为执行"确保 pip 依赖能成功跑通运行"任务的指令/提示。全程只描述**做什么、为何做、失败方向与解决思路**，不包含具体 shell 命令或代码块。

## 0. 任务目标
确保本项目的全部 pip 依赖（数据采集、训练、模型转换、量化研究、Web、数据库、监控等）在目标 Python 环境（3.14）下能被**成功解析、安装，并实际导入与运行核心功能**，而非仅"装上不报错"。最终产出一份**按风险分级的依赖跑通状态清单**与**失败项的解决方向**。

## 1. 项目环境（执行前务必先确认）
- **解释器**：Python 3.14。本机现有虚拟环境位于项目根目录 `venv/`；conda 存在于 `/opt/miniforge3/bin/conda` 但**不在 PATH**，且无系统级 `python3.11`。如无 3.14 专属 wheel，可考虑在 conda 下新建受支持的隔离环境（如 3.11/3.12）作为退路。
- **操作系统**：Linux（x86_64），具备 2×RTX 5060 Ti 16GB GPU 与 CUDA 运行时。
- **既有依赖**：akshare 1.18.94 / pandas 2.3.3 / pyarrow 25.0.1 等已就绪；训练相关包（torch、llamafactory、bitsandbytes、deepspeed 等）为重量级且对 3.14 敏感。
- **数据约束**：不得删除/修改 `data/`、`models/` 既有产物；大文件不入库；不为验证而升级破坏既有版本绑定。
- **网络现状**：Sina / Tencent / GitHub 可达；akshare 东方财富后端被阻断、新浪后端正常（仅影响取数，不影响依赖跑通）。

## 2. 依赖清单（按功能域，来源 `requirements.txt`）
- **数据采集**：baostock、akshare、pandas、numpy、pyarrow、fastparquet
- **训练栈**：torch、transformers、peft、accelerate、datasets、trl、bitsandbytes、llamafactory、deepspeed
- **模型转换**：gguf、sentencepiece
- **量化研究**：lightgbm、scikit-learn、scipy、statsmodels
- **接口/客户端**：ollama、httpx
- **Web**：fastapi、uvicorn[standard]、pydantic、jinja2、python-multipart
- **数据库/缓存**：psycopg2-binary、redis、sqlalchemy、alembic
- **监控**：prometheus-client、prometheus-fastapi-instrumentator
- **工具/图**：pyyaml、python-dotenv、rich、tqdm、schedule、matplotlib、seaborn
- **可选（不在 requirements，已单独验证）**：vn.py 4.0.0（py3.14 依赖解析通过）

## 3. 验证流程（逻辑阶段，非逐项命令）
1. **可解析性预检**：先以"仅解析、不安装"的方式确认整份清单在 3.14 下能被 pip 解析到对应 wheel（重点盯 torch、llamafactory、TA-Lib 等），避免全量安装后才发现不可解析、浪费时间。
2. **隔离分层安装**：在受控环境（现有 venv 或新建隔离环境）分层/分批安装——先装"轻量、确定性高"的数据与工具域，再装"重量级、风险高"的训练域，便于精确定位失败来源。
3. **导入烟测**：按域对每个包做导入级验证（import 不抛异常），区分"纯 Python 包"与"含原生扩展/CUDA 的包"。
4. **功能烟测**：对核心链路做最小功能验证（如 torch 能识别 CUDA、transformers 能加载 tokenizer、akshare 新浪后端能取数、`plan_e_backtest` 能跑通一行、fastapi 能启动），而不只是 import 成功。
5. **已知陷阱复核**：主动核对记忆中已登记的版本绑定与退化问题（见第 4 节），确认它们未被本次安装破坏或误修复。
6. **状态归档**：将结果分级（✅ 跑通 / ⚠️ 可装但需特殊处理 / ❌ 失败）并给出解决方向。

## 4. 已知高风险依赖与"报错 → 解决方向"映射
- **torch / CUDA 适配**：3.14 上若无对应 cp314 wheel，会尝试源码编译或报平台不支持；方向是确认官方 wheel 覆盖、必要时锁定可用版本，或退回受支持的 3.11/3.12 隔离环境。
- **llamafactory + omegaconf + antlr4**：llamafactory 的依赖链会把 `antlr4-python3-runtime` 强绑定到 4.9.*，若误升级到 4.11/4.13 会导致 llamafactory 崩溃；方向是固定 `antlr4-python3-runtime==4.9.3`，不要盲目升级（这条属于"既有的、不能破坏的版本绑定"）。
- **bitsandbytes / deepspeed**：强依赖 CUDA 工具链与原生编译，在无 nvcc 或版本不匹配时极易失败；方向是确认 CUDA 运行时版本、优先使用预编译 wheel，或在无 GPU 训练场景下将其设为可选/降级安装。
- **flash-attn**：本环境无 cp314 wheel 且无 nvcc；虽未列入 requirements，凡涉及需明确"回退到 SDPA 注意力实现"，不强行编译。
- **gguf（DFLASH）**：PyPI 发布的 0.19.0 缺少 DFLASH 支持，转换会报错；方向是用 llama.cpp 主线版本覆盖 site-packages 中的 gguf 模块，而非仅做 pip 升级。
- **akshare 东方财富后端**：被网络阻断（`RemoteDisconnected`），但属运行时取数问题而非依赖安装问题；方向是默认走新浪后端、禁用东财 `stock_zh_a_hist`，不在此任务内修复网络。
- **vn.py（可选）**：已验证 4.0.0 在 3.14 依赖解析通过（TA-Lib 有 cp314 wheel、PySide6 为 abi3），但完整安装含 PySide6 GUI（约 250MB+），无头回测只需核心；CTP 实盘仍需柜台授权 + 原生 `.so`，非本任务目标。

## 5. 验收标准
- 每个功能域至少有一个"导入 + 最小功能"均通过的样本，才算该域 ✅。
- 失败项必须给出"报错类别 → 解决方向"映射，不允许只报"装不上"。
- 不得改动既有数据/模型，不得为验证而升级破坏既有版本绑定（如 antlr4）。
- 产出物：一份风险分级状态清单（落 `docs/`），并同步关键结论到跨会话记忆。

## 6. 约束
- 不输出具体 shell 命令或代码块；以"做什么 / 为何 / 方向"描述。
- 不删除、不修改 `data/`、`models/` 既有内容。
- 升级依赖前先评估对既有版本绑定的影响（尤其 antlr4 ↔ omegaconf ↔ llamafactory）。
- 结论标注置信度；未实测的项标"待核实"。

---
生成时间：2026-10-06
生成者：依赖验证提示编写（AI 助手）
数据/环境基准：venv(py3.14) + requirements.txt(63 行) + 已登记陷阱
