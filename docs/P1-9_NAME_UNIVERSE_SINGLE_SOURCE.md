# P1-9 名字/池单源方案（2026-11-02）

> 任务来源：整改计划 `docs/archive/REMEDIATION_PLAN_2026-11-01.md` 的 **P1-9「名字/池单源」**
> （KI-SINGLE-001/002/003，R-仓库，计划窗口 11-09 → 11-15）。
> 上游依赖 **P1-8 步骤 4 已于本会话完成**（见 `docs/P1-8_DEPLOY_PATH_UNIFICATION.md` §5.1）。

---

## 1. 目标与范围

把「股票代码怎么规范化」「ST 怎么判」「全 A 名单怎么读」三件事收敛到**每个语义一份实现**，
并用 AST 守卫把"实现数 = 1"钉死——不是靠人记得改，而是靠 CI 红。

**范围内**：

1. `norm_code`（3 份实现）→ 收敛到权威实现 + 两个语义明确的入口。
2. `load_universe`（8 份实现，且**返回类型还不统一**：dict 与 list 混用）→ 收敛到 `tdxfeed/names.py`。
3. `is_st`（计划描述「3 种语义」）→ **核查后确认已是单源**，本项转为「把既有事实用守卫钉死」。

**不在范围内**：涨停判定单源（原 P1-8，顺延 **P1-12**，与本项同属 KI-SINGLE，可合并一轮审计）；
数据表路径单源（已有守卫 `test_no_hardcoded_data_paths.py` / `test_universe_path_single_source.py` 覆盖）。

---

## 2. 现状盘点（2026-11-02 全仓 grep 实测，非计划书转述）

### 2.1 `norm_code` —— 3 份实现，实为**两种契约**

| # | 位置 | 口径 | 是否委托 |
|---|---|---|---|
| 1 | `scripts/_common.py:46` | **规范化**：lower + 去 sh/sz/bj 前缀 + zfill(6)，给 CLI 用户输入 | ✅ 已委托 `tdxfeed.execution.limits.normalize_code` |
| 2 | `scripts/factor_engine.py:57` | **剥前缀**：`re.sub(r"^(sh\|sz\|bj)", "")`，无 lower/zfill，给内部文件名/裸代码 | ❌ 本地实现 |
| 3 | `quant/multifactor/factors.py:31` | 同上（注释自述"与 factor_engine 口径一致"） | ❌ 内联最小实现 |

⚠️ **两份代码注释里已写明"契约不同, 勿盲目统一"**（2026-10-18 审计 Round2 注记）。
因此本项**不是**"删掉两份留一份"，而是：**一份前缀剥离实现 + 两个命名明确的入口**
（`normalize_code` = 规范化；`strip_exchange_prefix` = 仅剥前缀），消除"复制粘贴"而非消除"两种语义"。

### 2.2 `load_universe` —— 8 份实现，返回类型分裂

> **批次 A 后状态**：主仓已全部收编到 `tdxfeed/names.py`，下表的 8 份实现**只剩第 1 行**
> （其余 7 份删除，调用点改走统一入口）；另有 **20 处「直接读名单文件」**的散点
> （`json.loads(universe_path().read_text())` 一类）一并收编 → 详见 §5。

| # | 位置 | 返回 | 备注 |
|---|---|---|---|
| 1 | `tdxfeed/names.py:11` | `dict[str, str]` | **权威**：带 `_CACHE`，文件缺失返回 `{}` 由调用方决定是否致命 |
| 2 | `tdxfeed/daban/scanner.py:182` | `dict[str, str]` | 带 `path` 参数（默认 `UNIVERSE_PATH`） |
| 3 | `scripts/hengpan_t1_eval.py:192` | `dict[str, str]` | 带 `path` 参数 |
| 4 | `scripts/daban_formula1_screen.py:56` | `dict[str, str]` | 自拼路径 |
| 5 | `scripts/daban_formula1_validate.py:57` | `dict[str, str]` | 自拼路径 |
| 6 | `mcp/servers/backtest.py:686` | `dict[str, str]` | 名为 `_load_universe` |
| 7 | `scripts/predict.py:554` | `list` | **返回类型不同**（列表） |
| 8 | `scripts/plan_e_sim_live.py:89` | `list[str]` | 带 `limit` 参数，**返回类型不同** |

注意 #7 / #8 的 list 版**可能带过滤逻辑**（如剔除指数/ETF）——收编前必须逐个核对语义，
不能想当然改成 `sorted(load_universe())`。

### 2.3 `is_st` —— 计划描述已过时，**现状是单源**

| 位置 | 关系 |
|---|---|
| `tdxfeed/names.py::is_st_name` | **权威实现**（`"ST" in name.replace(" ","").upper()`） |
| `scripts/predict.py:207 is_st()` | 委托 `is_st_name` ✅ |
| `tdxfeed/daban/limit_price.py:81 is_st()` | `from tdxfeed.names import is_st_name as _is_st_name` 后转发 ✅ |

现有守卫 `tests/test_names_single_source.py` 只覆盖 `predict` → `names` 的委托（源码级断言），
**不覆盖** `limit_price`，也不覆盖"全仓不得出现第二份 ST 字符串判定"。

### 2.4 现有守卫覆盖面

- `tests/test_names_single_source.py`：4 条用例，全部围绕 `predict.is_st`。
- `tests/test_universe_path_single_source.py`：**只管路径来源**（禁止自拼 `all_a_stocks.json`），
  **不管"实现数"**。

→ P1-9 的核心交付是**把"实现数 = 1"变成可执行断言**。

---

## 3. Canon 选定

| 语义 | 权威实现 | 入口设计 |
|---|---|---|
| 名单加载 | `tdxfeed/names.py::load_universe()` | 保留一个**带 `path` 的可选参数**重载（覆盖 #2 / #3 的"可指定文件"能力），默认走 `paths.universe_path()` |
| 名单取代码列表 | 同上 + 新增 `universe_codes()` | 供需要 list 的调用点（#7 / #8）使用，**语义差异（过滤）逐点核对后再收编** |
| ST 判定 | `tdxfeed/names.py::is_st_name` | `predict.is_st` / `limit_price.is_st` 保持公开别名（调用点不动），实现唯一 |
| 代码规范化 | `tdxfeed/execution/limits.py::normalize_code` | 保持不变；`scripts/_common.norm_code` 已是薄封装 |
| 仅剥前缀 | **新增** `tdxfeed/paths.py::strip_exchange_prefix()` | `factor_engine` / `quant.multifactor.factors` 改为委托 |

---

## 4. 迁移步骤（分批，每批独立可回滚）

| 批次 | 范围 | 风险 | 验收 | 状态 |
|---|---|---|---|---|
| **A** | `tdxfeed/names.py` 加 `load_universe(path=...)` 重载 + `universe_codes()`；`tdxfeed/daban/scanner.py`、`scripts/hengpan_t1_eval.py` 收编 | 低 | 对应模块单测绿；`def load_universe` 计数 8 → 6 | ✅ **已完成（2026-11-02）**，并把 B/C/E 一并提前落地（见 §5） |
| **B** | `scripts/daban_formula1_screen.py`、`scripts/daban_formula1_validate.py`、`mcp/servers/backtest.py` 收编 | 低（研究脚本 + MCP 工具） | `make test` 无新增红；MCP 工具返回与改前一致（抽样比对） | ✅ 随 A 完成 |
| **C** | `scripts/predict.py`、`scripts/plan_e_sim_live.py` 的 list 版收编 | **中**：这两处带过滤逻辑，误改会改变生产候选池 | 收编前后候选池逐票 diff = 0（用同一份 universe 快照比对）；`predict` Top5 输出一致 | ✅ 随 A 完成（候选池逐票 diff = 0） |
| **D** | `norm_code` 收敛：`limits.strip_exchange_prefix()` + `factor_engine` / `factors.py` 改委托 | 低 | `def norm_code` 计数 ≤ 2 且必须转发；两处调用点输出逐条一致（回归用例） | ✅ **已完成**（权威层实为 `tdxfeed/execution/limits.py`，见 §5.6） |
| **E** | 守卫升级：`tests/test_names_single_source.py` 扩为 AST 全仓断言；`tests/test_universe_path_single_source.py` 升到"实现数 = 1" | 低 | **反例必红**：造第二份 `load_universe` / ST 判定 / `norm_code` 必须让守卫失败 | ✅ **已完成**：universe 维度（实现数 = 1）+ ST/归一维度（4 → 16 条，含端到端自检与例外表防腐化） |
| **F** | 文档与台账：更新 `AGENTS.md`「数据路径与表口径」小节、`config/known_issues.yaml`（KI-SINGLE-001/002/003 状态） | 低 | 守卫绿 + 台账与代码一致 | ✅ **已完成**：三条 FIXED + `fix_note` + AGENTS.md 两条约定 |

**每批都要跑**：`venv/bin/python -m pytest tests/ -q` + `make gate`。
**每批单独提交**，显式路径（共享工作树，禁止 `-A`）。

---

## 5. 落地记录（2026-11-02）

### 5.1 统一入口（`tdxfeed/names.py`）

```python
load_universe(path=None) -> dict[str, str]      # 唯一的加载实现：{代码: 名称}
universe_codes(path=None, *, prefixes=None, fallback=False) -> list[str]   # 唯一的代码枚举实现
```

- `load_universe(path=...)`：**按可配置路径加载**（`None` = `paths.universe_path()`），兼容三种历史结构
  （`{code: name}` / `{"codes"|"stocks": [...]}` / `[code, ...]`），文件缺失返回 `{}`（是否致命由调用方决定）。
- `universe_codes()`：代码零填充到 6 位；`prefixes` 过滤 2 位前缀；`fallback=True` 时名单缺失/为空
  回退扫日线 parquet 目录（predict 与 MCP 回测的降级路径同源）。
- **刻意不缓存**：名单每日由 `download_universe.py` 更新，而 MCP server 是长驻进程——缓存会让名称映射
  在进程存活期内一直是旧的（ST 改名当天穿帮）。143 KB 的 JSON 解析约 1 ms，每次重读成本可忽略。

### 5.2 语义口径的选择（为什么是"原样"）

`load_universe` 返回**文件原样**的 `{代码: 名称}`：不 zfill、不去空格、不改键。

- 实测名单 5221 条：键全是 6 位数字 ⇒ `zfill`/`normalize_code` 实为 no-op；名称有 **36 条带排版空格**
  （`万  科Ａ`）。
- 旧 8 份实现里 **6 份返回原样**，只有 `tdxfeed/daban/scanner.py` 额外去空格 → 统一取"原样"，
  **行为差异最小**：8 份里 6 份逐字节不变，scanner 侧的 6 个调用方（研究脚本）显示口径由
  `万科Ａ` 变回 `万  科Ａ`（36 只）。
- 反过来取"去空格"会改动 **MCP 对外 API** 返回的名称（news / finance_deep），风险更高，故不取。

### 5.3 收编范围（远超原定 2 个文件）

守卫要断言"实现数 = 1"且**必须绿**，所以把全库读池点一次收干净：

| 类别 | 数量 | 处理 |
|---|---|---|
| `def load_universe` 类实现 | 8 | 删 7 份，调用点改走 `tdxfeed.names` |
| 直接读名单文件（`json.loads(universe_path().read_text())` 一类） | **20 处 / 19 文件** | 全部改走 `load_universe()` / `universe_codes()` |
| `predict.load_names`（ST 实时兜底） | 1 | 改为 `load_universe()` + 原有 try/except（吞错计数键改为 `run_prediction`） |
| `mcp/servers/backtest.py::_load_universe` / `news.py::_universe` | 2 | 改名 `_pool()`（入口名唯一性要求），内部改为委托统一入口 |
| `plan_e_sim_live` 的 `limit` 抽样 | 1 | 抽成 `_sample_codes()`（抽样是**策略**，不是加载） |
| 顺带清理 | — | 各文件残留的 `universe_path` 导入、`ALL_STOCKS`/`UNIVERSE_PATH` 等常量、未用的 `json` 导入 |

**例外（显式登记）**：v5 按 `AGENTS.md` 是**零耦合孪生**（不 import tdxfeed），它自己那一份读取留在
`v5/scripts/scan_double.py`；守卫同时断言 v5 域内也只有这一处——即"每个依赖域各一份实现"。

### 5.4 守卫升级（路径层 + 实现层双层强制）

`tests/test_universe_path_single_source.py` 从 4 条扩到 **10 条**：

- **路径层（R1–R3）**：沿用原规则（不自拼 `all_a_stocks.json` 路径）。
- **实现层（R4·行为）**：除权威层外**任何代码都不得读取名单文件**——AST 识别「`universe_path()` 直接读」
  与「存进变量再 `open()/read_text()`」两种形态，**改名躲不过去**。
- **实现层（R5·入口）**：`load_universe` / `universe_codes` / `load_names` / `universe` 这类入口的
  **函数定义**只允许在权威层（历史上 `load_universe` 有 8 份）。
- **反例必红**：直接读 / 别名读 / 改名藏起来的入口三类反例样本（`tmp_path`）各自必须被报出；
  再加一条**扫描器端到端自检**（把第二实现放进临时扫描域 → 扫描器必须报出来），
  防止"探测器认得出、扫描器没算进去"的哑守卫。

### 5.5 等价性验证（怎么证明没改行为）

改造前后对 **17 个实现点**各取一次返回值落 JSON，**逐点 diff**：

- **16 项逐字节一致**（names / scanner 键 / hengpan / predict 两处 / plan_e 两处 / formula1 两处 /
  MCP backtest / MCP news / finance_deep / 原始 JSON 三个视图）。
- **1 项有差异且已声明**：`scanner.load_universe` 的 36 个名称由"去空格"回到"原样"（§5.2）。

此外：`make lint` 阻断集全绿；`ci_semantic_guard` 的 SEM001/SEM003 仍为 0、SEM002 仍是既有 5 条
（未因本次改造增加）；`pytest tests/` = **1755 passed / 10 failed**，10 项全为基线既有红
（与改造前逐项对照确认）。

> 附带效果：`make lint` 的 F821 当场抓到一处**真实破坏**
> （`daban_alpha_sources.py` 收编时丢了 `universe` 变量）——这正是"删实现、换调用"最典型的漏网方式。

### 5.6 批次 D / E / F 落地记录（2026-11-02）

#### D · 代码归一：两种契约、一份实现

权威 = `tdxfeed/execution/limits.py`，两个契约入口**共用同一条底层**：

```python
strip_exchange_prefix(code)   # 契约 B：只剥 sh/sz/bj 前缀，其余原样（大小写、长度都不动）
normalize_code(code)          # 契约 A：lower + 剥前缀 + 零填充 6 位 → 内部调用 strip_exchange_prefix
```

| 入口 | 调用场景 | 输入 | 输出 |
|---|---|---|---|
| `normalize_code`（契约 A） | **CLI 用户输入** | `600519` / `sh600519` / `" 600519 "` | 6 位裸码（`"1"` → `"000001"`） |
| `strip_exchange_prefix`（契约 B） | **内部生成的标识** | 日线文件名 `sh600000`、带前缀裸码 | 只去前缀（`"1"` 仍是 `"1"`） |

三份副本的处置：`scripts/_common.py::norm_code` → 改为权威实现的**别名**（大量脚本在用，不再包一层）；
`scripts/factor_engine.py::norm_code`（5 处调用）、`quant/multifactor/factors.py::norm_code`（1 处调用）
→ 删除定义、改调 `strip_exchange_prefix`（**契约 B**，因为它们喂的是文件名/裸代码）。

**"勿盲目统一"的意图保留**：两个入口的 docstring 都写明契约边界与差异后果
（审计 Round2 2026-10-18 注记："契约不同, 勿盲目统一"）——合并会静默改口径。

**行为不变的证据**：用 20 组输入（含 `SH600000` / `" 600519 "` / `"1"` / `sz000001extra` / 空串）
逐一对照**三份旧实现**（limits 版、`factor_engine` 严格版、`factors` 宽松版）→ 全部逐字节一致；
调用点抽查（`get_board` / `is_stock_code` / `_common.norm_code`）取值与改造前相同。

#### E · ST 判定：确认单源，只固化不重构

核查结论（**比"3 种语义"更精确**）：
- **函数层确已单源**：权威 = `tdxfeed/names.py::is_st_name`；`predict.is_st`（int 契约）与
  `limit_price.is_st`（bool 契约）都是转发；`find_double_up_c_v2.is_st(code, st_set)` 判的是
  「代码在不在 ST 名单里」，属**另一契约**（不是名称判定的副本）。
- **但内联写法散落 16 处**，且口径各异：含退市整理「退」（2 处）、判 vault 文件名词干（2 处）、
  仅前缀匹配（1 处，与权威的"任意位置含 ST"不同）、与权威等价待收编（1 处）等。

因此**不重构**，改为用 AST 守卫（`tests/test_names_single_source.py`，4 → 16 条）固化三条校验目标：

1. **实现唯一**：权威层外不得再定义 ST 判定函数；登记为「转发」的例外**必须真的转发**
   （函数体里调用 `is_st_name`），否则名不副实。
2. **内联可见**：内联 ST 字符串判定必须**逐条登记理由**（例外表），新增未登记的必红；
   并加「例外表不得腐化」——登记了却已无命中 = 代码变了，该重新评估这条例外。
3. **归一单源**（与 D 同文件）：两个入口只在权威层、`norm_code` 这个第三种命名不得再出现、
   两入口必须共用底层（断言 `normalize_code` 调用 `strip_exchange_prefix`，且前缀正则只出现一次）。

**检测器的坑（实测）**：用「常量含 ST 子串」判定会误报 9 处（`"tests/"`、`"state"`、`"step"`、
`"OBSIDIAN_SYSTEMD:"`、`"inst_net"`…）→ 改为**精确 marker 匹配**（`"ST"` / `"*ST"`）。

#### F · 台账与文档

- 台账三条 KI-SINGLE 全部转 **FIXED**（KI-SINGLE-001 代码归一 / 002 ST 判定 / 003 股票池），
  各自绑定真实存在的守卫文件 + `fix_note` 写清处置与遗留；
  **detect 规格一并修正**：原 `def\s+_?norm_code` 漏掉 `normalize_code`——正是它让 5 处实现
  一直没被台账看见（守卫改口径后一次挖出 8 处）。
- `AGENTS.md` 同步两条约定（代码归一的两个契约 + ST 单源与守卫的三条校验目标），
  并与批次 A 写入的「股票池唯一入口」条目**逐条核对无冲突**。
- M-5（修复必附测试）要求 FIXED 条目绑定的测试里出现条目 ID → 两个守卫文件补了
  `KI-SINGLE-001/002/003` marker。

## 6. 依赖关系

```
P1-8 步骤 4（systemd 模板+渲染）──► P1-9（✅ 本会话已完成上游）
P1-9（名字/池单源）──► P1-12（原 P1-8 涨停判定单源，KI-SINGLE 同族，合并一轮审计）
P1-11（plan_e 拆库）──⚠─► 批次 C 触碰 scripts/plan_e_sim_live.py
                            → 两者不要同批；建议 P1-11 先抽公共部分，P1-9 再收编
D2 决策（执行研究线是否归档）──⚠─► 归档会让 6 份副本中的研究脚本直接消失
                            → 决策截止 11-15；在 D2 定案前，批次 B 里的
                              daban_formula1_* 收编可能白做（但改动本身无害）
```

- **与 P1-8 的衔接**：P1-8 步骤 4 已把"部署路径"单一源化（`scripts/lib/tdx-env.sh` → 渲染器 → 单元）；
  P1-9 把"数据/名字"单一源化（`tdxfeed/names.py` / `paths.py` → 调用方）。两处共用同一条原则：
  **权威实现放 `tdxfeed/`，脚本与工具只做薄封装，守卫用 AST 钉住"只有一份"**。
- **可并行性**：整改计划标注 P1-8/9/10 互相独立；但共享工作树下**必须分提交、错峰**。

---

## 7. 成功判据（可执行）

| # | 判据 | 验证方式 |
|---|---|---|
| 1 | `load_universe` 实现数 = 1 | `grep -rn "def load_universe" --include="*.py" scripts tdxfeed mcp quant` → 仅 `tdxfeed/names.py` |
| 2 | ST 判定实现数 = 1 | AST 守卫：除 `tdxfeed/names.py` 外无第二处 `"ST" in` 判定；`def is_st` 仅允许委托实现 |
| 3 | 代码规范化无复制 | `def norm_code` ≤ 2 处且必须转发；`strip_exchange_prefix` 实现唯一 |
| 4 | 返回类型不分裂 | 名单加载权威返回 `dict`；需要 list 的调用点走 `universe_codes()` |
| 5 | 反例必红 | 守卫自检：往临时目录塞一份第二实现 → 守卫必须失败（`tmp_path` 造样例） |
| 6 | 无新增红 | `venv/bin/python -m pytest tests/ -q` 与改动前同批红（当前基线：10 项既有红，见 §7） |
| 7 | 生产口径不变 | `predict` 候选池与 Top5 输出在收编前后逐票一致；MCP `backtest` 工具返回抽样一致 |

---

## 8. 风险与缓解

| 风险 | 说明 | 缓解 |
|---|---|---|
| **误改生产候选池** | 批次 C 收编 list 版时若把过滤逻辑抹掉，Top5 会变 | 收编前后用同一份 universe 快照做逐票 diff；不一致即回滚该批 |
| **守卫误伤** | AST 扫全仓时把文档字符串/注释里的 `"ST" in` 判成违规 | 沿用仓库既有做法：只扫 AST 字符串字面量，注释与 docstring 不算（见 `tests/test_no_hardcoded_data_paths.py` 的 `_code_strings` 思路） |
| **D2 归档抢先** | 决定归档执行研究线后，批次 B 的目标文件会消失 | 批次 B 可延后到 D2 定案；先做 A/D/E（与归档无关） |
| **P1-11 撞车** | 批次 C 与 plan_e 拆库都动 `plan_e_sim_live.py` | 拆库先行，批次 C 后置；或两批合并做（不推荐，回归面太大） |
| **共享工作树并发** | 与其他代理会话共用同一 checkout | 分批 + 显式路径提交；`git add` 禁用 `-A`/`.` |

---

## 9. 当前基线（2026-11-02，供批次验收对表）

改动前 `venv/bin/python -m pytest tests/ -q` 的**既有红（10 项，与本项无关）**：

- `test_ci_semantic_guard`（5 处 SEM002：P0-1 的 `check_sync_sentinel.py` / `mark_sync_done.py`、
  P1-5 的 `mcp/schema_validation.py` / `mcp/servers/parquet_reader.py`）
- `test_ci_typecheck_gate`、`test_ci_violation_gate`（基线额度）
- `test_daban_mcp::test_skill_yaml_wires_mcp_tool`、`test_mcp_contracts`
- `test_docs_classification`、`test_theory_reference`（依赖 Obsidian vault 状态）
- `test_error_counters`、`test_sem002_convergence`（同上 SEM002 根因）
- `test_production_entry_smoke[mark_sync_done.py]`

> 本会话另修掉 1 项既有红：`test_ci_data_skip`——上一轮 P1-8 新增的
> `tests/test_deploy_paths_unified.py` 缺 `NEEDS_DATA = False`，被 conftest 误判成数据依赖模块。

---

## 10. 下一步（批次 A / D / E / F 已完成）

1. **收编可收编的内联 ST 判定**（批次 E 留下的尾巴）：16 处例外里，
   `scripts/find_double_up_hfq.py` 那处与权威等价 → 可直接改调 `names.is_st_name`；
   含「退」的两处（`daban_min60_features.py`、`tdxfeed/daban/scanner.py`）是**另一契约**
   （权威 `is_st_name` 不含「退」），要么给权威加 `is_st_or_delisting` 入口、要么继续登记为例外
   ——**收编前必须先确认口径**（动了会改变涨停候选池）。
2. **P1-12（原 P1-8「涨停判定单源」KI-SINGLE-004）**：与本项同属 KI-SINGLE，可合并一轮审计，
   但**分提交**。
3. **台账 detect 规格复查**：本批发现 `def\s+_?norm_code` 这类过窄模式会让实现长期不被看见
   （5 处归一实现因此漏网）→ 其余 38 条 detect 也值得按此例过一遍。
4. `scripts/find_double_30d.py` 仍有字面量数据路径（`Path('/home/jiuben/tdx-data-feed/data/double_up')`）
   → 交给 P1-8 后续 / P2-2「裸写残留清理」。

---
生成时间：2026-11-02 14:40（批次 A / D / E / F 落地后更新）
生成者：P1-9 批次 A/D/E/F 会话
模型版本：不适用（工程改造）
数据日期：不适用
