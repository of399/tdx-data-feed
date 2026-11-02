# P1-12 批次执行方案：SEM002 清理 + ST 收编 + 台账 detect 复查 + 涨停判定单源（2026-11-02 15:10）

> 本批次把四件事放在同一批推进，但**风险差异很大**：其中两件零候选池影响、两件直接改判定口径。
> 方案的核心是**先定口径、再动代码**，避免候选池在口径未确认前被意外改动。
> 依据：2026-11-02 的只读审计（逐处读了实现，不依赖计划书转述）。

## 📊 核心指标

| 项 | 目标 | 现状（实测） | 规模 | 风险 | 动候选池？ |
|---|---|---|---|---|---|
| 1 | 清 5 处 SEM002，解锁 3 项红 | 5 处 / 4 文件：SEM002 = 5（语义基线 0） | 4 文件 | **低** | 否 |
| 2 | 内联 ST 判定收编统一 | 内联 16 处（登记可见）；本批处理 3 处 | 1 + 2 文件 | 低 / **高** | 否 / **是** |
| 3 | 台账 detect 复查 | 40 条 detect，已知 3 条过窄 | 1 文件 | **低** | 否 |
| 4 | P1-12 涨停判定单源 | `limit - 0.001` 共 **9 处**；`LIMIT_MAP` 独立硬编码 | 5 文件 | **高** | **是**（判定变严） |

## 🎯 Top N 明细

### 项 1 · 清理 5 处 SEM002（低风险，行为零变化）

> **✅ 已完成（2026-11-02，`d9d6951`）**：5 处计数补齐 + mark_sync_done argparse，
> 配对测试 `tests/test_sem002_last5.py`（RED 5/5 → GREEN），SEM002/001/003 全 0、
> 基线不抬、全量 1784 passed / 5 failed（5 项均既有红，targeted 4 红全消）。

**改动内容**（四处都是「控制流不变、只补计数」，不改任何判定结果）：

| 文件 | 行 | 现状 | 改法 |
|---|---|---|---|
| `scripts/check_sync_sentinel.py` | 43 | `except Exception: return False` | 计数键按**真实函数名**（`test_error_counters` 要求键对应函数）→ 仍 `return False` |
| `scripts/check_sync_sentinel.py` | 57 | 同上 | 同上 |
| `scripts/mark_sync_done.py` | 33 | `except Exception: last_date = None` | 计数 → 仍 `last_date = None` |
| `mcp/schema_validation.py` | 28 | `except Exception: jsonschema = None`（依赖缺失退化） | 新增 `_ERROR_COUNT` + 计数 → 退化语义保留 |
| `mcp/servers/parquet_reader.py` | 51 | `except Exception:` 文件加载回退 | 同上 |

**影响范围**：4 文件；`mcp/schema_validation.py` 与 `mcp/servers/parquet_reader.py` 目前**没有** `_ERROR_COUNT`，
需新增 `Counter` 定义（沿用 `mcp/servers/news.py` 既有写法：模块级 `_ERROR_COUNT: Counter[str] = Counter()`，
由 `tdxfeed/errors.py::flush()` 自动收集，`canary` 第 9 维 `swallowed_errors` 消费）。

**依赖**：无前置项；**不依赖**任何口径确认。

**风险**：低。唯一副作用是吞错计数从 0 变正——这正是门禁要的效果（语义基线仍是 0，不需要抬基线：
「基线只降不升」）。

---

### 项 2 · 内联 ST 判定收编（分两档：低风险可做 / 高风险须先定口径）

#### 2a. `scripts/find_double_up_hfq.py:181`（低风险，可直接做）

- 现状：`is_st="ST" in name.upper()` → `screen_one(..., is_st=...)` → **只写入输出记录字段**
  （`"is_st": bool(is_st)`，第 118 行）；该脚本的涨停档位走 `get_limit(code)`（**不传 name**），
  `is_st` **不参与命中/剔除判定** ✓
- 改法：改调 `tdxfeed.names.is_st_name`
- **口径差异**：权威实现会先去掉名称里的空格再判 ST ⇒ 极少数「名称含空格且含 ST」的记录，
  `is_st` 字段从 0 变 1 ⇒ **只改输出统计字段，不改命中结果**
- ~~需确认：接受这一处统计口径差异（影响翻倍样本 CSV 的 `is_st` 列）~~
  → **✅ 已实测（2026-11-02）：差异是空集**。宇宙 5221 只 + hfq 样本 17794 行逐条比对，
  `is_st` 翻转 **0**（带排版空格的 36 个名称里没有「S T」型），无需裁决、CSV 不需重生成。

#### 2b. 含「退」的两处（~~高风险：直接改涨停档位与候选池~~ → 方案 A 行为等价，实测逐票 diff = 0）

| 文件:行 | 现状 | 影响面 |
|---|---|---|
| `scripts/daban_min60_features.py:140` | `is_st = int("ST" in str(name).upper() or "退" in str(name))` → `ratio_val = 0.05 if (is_st and main) else (0.10 if main else 0.20)` | **直接决定涨停档位**（ST 主板 5% vs 普通 10%）→ 改口径会改命中判定 |
| `tdxfeed/daban/scanner.py:372` | `if not self.allow_st and ("ST" in name.upper() or "退" in name): continue` | **直接剔除打板候选** → 改口径会改候选池 |

- **口径分歧**：这两处判的是「ST **或退市整理**」，而权威 `is_st_name` 只判「名称含 ST」⇒
  **「退」是另一个契约，不能塞进 `is_st_name`**（塞进去会把所有含「退」的非 ST 股也按 ST 处理）
- **需先行确认（二选一，必须先定）**：
  - **方案 A（推荐）**：在权威层新增一个显式入口（如 `is_st_or_delisting(name)`，语义＝「含 ST 或含退」），
    两处改调它 ⇒ 行为等价（判定结果逐票不变）+ 实现唯一 + 口径显式化
  - **方案 B**：两处保持内联、继续登记在守卫例外表 ⇒ 候选池零风险，但口径分叉留存
- **✅ Phase 0① 结论（2026-11-02 用户批准）：方案 A**。等价性有一个边界要逐票验证：
  权威 `is_st_name` 先剥名称空格、原内联不剥（AGENTS 记录 36 个名称带排版空格），
  收编时对当前宇宙逐票 diff，出现「S T」型翻转须显式裁决。另登记一个**不在本批动**的
  独立议题：按交易所规则退市整理并非 ST 档（主板 10%），`daban_min60_features` 把「退」
  归 5% 档的金融正确性存疑——改它属历史特征口径变更（重训议题）；scanner 已剔除退票，
  实盘入口零影响。
- ⚠️ 在确认前**不得**动这两处，否则可能让「含退市整理」的票改按 ST 档位计算（或不再被剔除）⇒ 候选池变化

---

### 项 3 · 台账 detect 复查（低风险，纯台账，不碰代码）

> **✅ 已完成（2026-11-02）**：40 条逐条反向验证。3 条错代理修复（COMPETE-001
> Requires= 代理→双闸门、OPS-001 字面量→USER_PATHS 变量、OPS-002 grep --user→absent --scope）；
> 8 条晋升 FIXED（含 5 条 RESOLVED 转正 + 3 条错代理修复后转正），守卫均含条目 ID；
> MCP-002 扩到 5 个未接线 server、SKILL-002 收窄到残留死字段 external_api_timeout_sec、
> IMPORT-001 扩到 train_daban_model 族、DOC-001 排除 docs/archive 历史快照、
> SINGLE-004 注明 docstring 命中（代码实为 8 处）。复扫收敛 STILL_PRESENT 16 · RESOLVED 0 ·
> FIXED_OK 24 · GUARD_MISSING 0；M-5 绑定检查通过。

**改动内容**：40 条 detect 规格逐条反向验证——「这条 pattern 能不能抓住它 title 描述的问题形态」。
已知过窄的三条（本批实测）：

| 条目 | 现 pattern | 漏掉什么 |
|---|---|---|
| `KI-SINGLE-001` | `def\s+_?norm_code` | 漏 `normalize_code`（同一概念的不同命名）——已在本批修正为 `def\s+_?(norm\|normalize)_code` |
| `KI-SINGLE-002` | `def\s+_?is_st` | 只覆盖**函数定义**，漏 16 处**内联**判定 |
| `KI-SINGLE-003` | `def\s+load_universe` | 只覆盖**函数定义**，漏 20 处**直接读池点** |

**方法**：对每条 detect 做「同概念不同命名/形态」的扩测（用 `make ledger-probe PAT=... PATHS=...` 补查），
把发现的口径写回 pattern 或 detect 规格；**不改代码**。

**影响范围**：`config/known_issues.yaml` 一个文件（+ 必要的 detect 规格澄清）。

**依赖**：无；但**建议在项 1 之后做**——项 1 会改动 `check_sync_sentinel.py` / `mark_sync_done.py`，
台账里若有针对这两个文件的 detect（`KI-COMPETE-001` 等）需同步复核。

---

### 项 4 · P1-12 涨停判定单源化（~~高风险：判定阈值会变严~~ → **零口径变化去重**，Phase 0② 已翻正）

**现状（原口径实测 9 处 + 1 处常量；Phase 3 放宽变量形态后为 18 处 = 代码 17 + docstring 1）**：

| 形态 | 位置 |
|---|---|
| `>= limit - 0.001` | `tdxfeed/features_5d.py` ×4、`scripts/v3_premarket_top.py` ×2、`scripts/find_double_up_hfq.py` ×1、`scripts/top10_deep_dive.py` ×1、`scripts/find_double_up_c_v2.py` ×1 |
| 独立硬编码阈值 | `tdxfeed/features_5d.py:50` `LIMIT_MAP = {"main": 0.095, "gem": 0.195, "bse": 0.295}` |

**⚠️ 核心口径分歧（本项必须先确认的事）**：

- 权威判定 `limit_threshold` = 涨停比例 **− 0.5% 缓冲**（主板 0.095 / 创业板 0.195 / 北交所 0.295）
- 现状判定 `>= limit - 0.001`，而这些调用点的 `limit` 取的就是**阈值**（`get_limit(code)` = 0.095）
  ⇒ 实际判定门槛是 **0.094**，比权威口径**宽 0.1 个百分点**
- 即：现在等于「缓冲 0.5% + 再放宽 0.1%」。收编到权威（去掉多余的 0.001）会让判定**变严** ⇒
  「是否涨停」的结果变少 ⇒ 翻倍样本与候选池随之变化
- **需先行确认**：这 0.001 是「浮点误差容忍」（可由权威阈值自身的缓冲吸收 ⇒ 收编）还是
  「有意放宽」（⇒ 权威层需显式提供带容差的判定入口，而不是各处内联 −0.001）
- **✅ Phase 0② 结论（2026-11-02 用户批准）：浮点容忍，且权威层已有带容差入口——
  上面「收编会变严 0.1pp」的前提不成立，本项实为零口径变化的去重。**证据：
  `limits.py:31` `RET_TOLERANCE = 0.001`、`is_limit_up_ret(ret, limit) = ret >= limit - 0.001`；
  `features_5d.py:78` 注释明示「容差 0.001：规避浮点误差（如 0.0999999 vs 0.10）」，
  起源 2026-10-19 审计 P1 公共化、`48dcc6a` 收进 limits.py。9 处的 `limit` 逐处核实
  均为 `get_limit` = 阈值（0.095/0.195/0.295，已含 0.5% 缓冲）、分数单位一致 ⇒
  有效门槛 0.094 本就是权威口径。收编目标 = `is_limit_up_ret`（保留 0.001），
  **diff 必须为空、候选池零变化**；`features_5d.is_limit_up_ret` 自身是 9 处之一
  （limits 版的手抄第二份），改委托。Phase 3 风险等级由「高/改口径」降为
  「零口径变化去重」，与 Phase 1 同类；四件套（尤其逐票 diff）照跑。

**改动内容**（确认后）：
1. `features_5d.LIMIT_MAP` 改为从 `tdxfeed/execution/limits.py` 派生（阈值口径本就一致，只是没派生）
2. 9 处 `limit - 0.001` 改为走权威判定（`get_limit` / `is_limit_up_ret`）
3. 升级 `tests/test_limit_single_source.py`：断言「全仓 `limit\s*-\s*0\.001` = 0 处」+ `LIMIT_MAP` 已派生

**依赖**：**依赖口径确认（见上）**；且应在项 2 定稿之后做——ST 口径与涨停档位耦合（含「退」→ 5%/10% 档），
先定 ST 口径才不会在收窄阈值时把 ST 档位一起搅动。

## 📈 统计分布

### 依赖关系

```
                ┌──────────── Phase 0（只确认口径，不改代码）────────────┐
                │ ① 「退」字判定归属：方案 A（新增 is_st_or_delisting）还是方案 B（继续登记例外）
                │ ② 0.001 容差性质：浮点容忍（可收编）还是有意放宽（权威层显式提供带容差入口）
                └───────────────┬───────────────────────────┬───────────┘
                                │                           │
        Phase 1（零候选池影响）  │                           │
          项 1 SEM002 清理 ─────┤                           │
          项 3 detect 复查 ────┘                           │
                                │                           │
        Phase 2（ST 收编）      │                           │
          项 2a find_double_up_hfq（低风险，只需确认统计字段差异）
          项 2b 含「退」两处 ─────┤ 依赖 ①
                                │
        Phase 3（改判定口径）    │
          项 4 P1-12 单源化 ─────┴─ 依赖 ②，且排在项 2 定稿之后
```

### 推进顺序与理由

| 阶段 | 内容 | 为什么在这个位置 | 候选池影响 |
|---|---|---|---|
| **Phase 0** | 两项口径确认（只读讨论 + 查证，**不改代码**） | 口径分歧必须在动代码**之前**定：① 「退」若并入 ST 判定会改档位；② 0.001 容差若被移除会改判定门槛。定错要返工，而返工时可能已经改过候选池 | **零**（只读） |
| **Phase 1** | 项 1 SEM002 清理 → 项 3 detect 复查 | 两件都不碰判定口径 ⇒ 可**立刻**拿到 3 项红解锁的收益；且先把台账 detect 修准，Phase 2/3 的复扫才有可信基准 | **零** |
| **Phase 2** | 项 2a → 项 2b | 2a 只改输出统计字段（已核实不参与判定）⇒ 先做掉低风险部分；2b 依赖 Phase 0①，**口径定了才动**，避免「含退市整理」的票改按 ST 档位算或不再被剔除 | 2a 零；2b 按 Phase 0① 结论，可能为零（方案 A 保持等价） |
| **Phase 3** | 项 4 P1-12 | 改的是「是否涨停」的判定门槛，影响面最大 ⇒ 放最后；且排在项 2 之后——ST 口径（5%/10% 档）与阈值收窄会叠加，先把 ST 定稿才不会两处一起动、难以归因 | ~~**是**（判定变严，样本/候选池变少）~~ → **零**（Phase 0② 确认 0.001 是浮点容忍、权威入口已带它；实测逐点 diff = 0） |

**总原则**：**候选池只允许在 Phase 2b 之后、且仅在口径确认的前提下变动**；Phase 1 与 Phase 3 之间的
任何一步都不得让「涨停判定」或「ST 判定」发生变化。

### 一致的验收标准（四件套，每项都跑同一套）

| # | 验收项 | 做法 |
|---|---|---|
| 1 | **守卫绿** | 本项对应守卫全绿（项 1 → `test_ci_semantic_guard` / `test_error_counters` / `test_sem002_convergence`；项 2 → `test_names_single_source`；项 3 → `audit_ledger.py scan`；项 4 → `test_limit_single_source`） |
| 2 | **行为等价 diff** | 改前/改后对**同一输入集**逐点比对取值（项 1：4 个函数返回值；项 2a：翻倍样本输出的 `is_st` 列；项 2b：候选池逐票；项 4：涨停判定逐票逐日） |
| 3 | **基线对照** | 在改动前的 commit 建 `git worktree` 复跑同一批用例，确认**没有新增红** |
| 4 | **台账一致** | 条目状态 / `fix_note` / detect 规格与代码一致；`FIXED` 条目绑定**真实存在**的守卫文件，且该测试文件里出现条目 ID（M5-R2 要求） |

### 三项红「解锁」的判定（项 1 专属，但用同一套口径）

| 判据 | 命令 / 位置 | 期望 |
|---|---|---|
| 语义违规归零 | `venv/bin/python scripts/ci_semantic_guard.py` | `SEM002 吞错必计数 0 条`（SEM001/SEM003 保持 0） |
| 基线不抬 | `config/semantic_baseline.json` | `total` 仍为 **0**（「基线只降不升」，不靠抬基线解锁） |
| 三项红转绿 | `venv/bin/python -m pytest tests/test_ci_semantic_guard.py tests/test_error_counters.py tests/test_sem002_convergence.py -q` | **3/3 绿** |
| 计数可观测 | canary 第 9 维 `swallowed_errors` | 24h 吞错计数从 4,080 降下来（观察项，不作判据） |

### ST 收编与单源化的达成判定

| 目标 | 判据 |
|---|---|
| **ST 收编达成** | ① `tests/test_names_single_source.py` 绿（内联例外表按结论缩减：方案 A → 减 2 条、方案 B → 保持）；② 例外表**无腐化**（该守卫自带）；③ 收编前后逐票 ST 判定 diff = 0（方案 A），或仅统计字段差异已书面确认（2a） |
| **单源化达成** | ① 全仓「阈值 − 字面量 0.001」命中 **0 处**（口径放宽到 `(limit[A-Za-z_]*\|lim\|["']limit["']])\s*-\s*0\.001`，原规格只认 `limit` 会漏 9 处；台账 `KI-SINGLE-004` 转 **FIXED**）；② `features_5d.LIMIT_MAP` 已从 `limits.py` 派生（不再硬编码 0.095/0.195/0.295）；③ `tests/test_limit_single_source.py` 升级后绿（含**反作弊**断言：站点必须真的引用权威容差，删掉容差冒充去重必红）；④ **判定结果 diff 已评审**：Phase 0② 确认为浮点容忍 ⇒ 按方案要求 diff 为空，实测逐点等价（见执行结果补记） |

## ⚠️ 风险提示

1. ~~**项 2b（含「退」两处）与项 4（阈值收窄）会改候选池/样本**~~——**Phase 0 两条结论已把这项风险消掉**：
   2b 走方案 A（行为等价，逐票 diff = 0）、项 4 是去重（逐点 diff = 0）。顺序仍按 Phase 0 → 2 → 3 执行，
   以便任何一处出现非零 diff 时能单独归因。
2. ~~**口径确认缺失的代价最高**~~——确认已完成（见 Phase 0 结论补记）：0.001 = 浮点容忍，
   权威层 `is_limit_up_ret` 本就带它，所以不存在「收编即收窄」的返工路径。
3. **项 1 的计数键必须对应真实函数名**（`test_error_counters` 守卫）⇒ 键名按函数分桶，
   不能随手写已不存在的名字（本轮已踩过一次：`load_names` 删除后计数键失效）。
4. **台账 detect 复查别把 kind 与方向写反**：`absent` 是「缺失即存在」、`grep` 是「命中即存在」，
   写反会误报 ⇒ 改 pattern 时同步确认 kind 与方向。
5. **跨环境的测试数字不可直接比**：基线 worktree 无 `data/` ⇒ skipped 数不同，
   对照只取**环境等价**的静态守卫用例。
6. ~~**收窄阈值会让历史产物漂移**（翻倍样本 CSV、特征表的 `limit_up_*` 列）⇒ 项 4 完成后要重跑并
   比对受影响产物~~——**没有收窄**：262 只 × 多 sig_idx 的 1044 个特征向量 / 30276 个取值、
   839913 个逐日涨停判定点全部零差异 ⇒ 历史产物**不需要重跑**（重跑只会引入数据日更带来的无关差异）。

## 🔧 下一步建议

1. **Phase 0 立刻可做（只读）**：把两个确认项写成可回答的问题——
   ① 含「退」的判定是否要独立入口（方案 A 推荐，行为等价）？
   ② `limit - 0.001` 的 0.001 是浮点容忍还是有意放宽？
   确认结论落进本方案与 `docs/P1-9_NAME_UNIVERSE_SINGLE_SOURCE.md` 的对应小节。
2. **Phase 1 无需等待确认**：项 1（4 文件 5 处计数）→ 立刻拿 3 项红解锁；随即做项 3（40 条 detect
   逐条反向验证，重点 KI-SINGLE-002/003/004 这类「只覆盖定义、漏掉散点」的规格）。
3. **Phase 2**：项 2a 先做（低风险，只需确认 `is_st` 输出字段的统计差异）；项 2b 等 Phase 0① 结论。
4. **Phase 3**：项 4 在项 2 定稿后做，改完必须重跑并**逐票比对**涨停判定与翻倍样本产物。
5. **全程纪律**：每阶段单独提交（显式路径，不用 `-A`），提交前跑「守卫 + 行为 diff + 基线对照 + 台账一致」四件套。

---

## Phase 0 结论补记（2026-11-02，独立核查会话取证、用户批准）

- **① 退字归属 = 方案 A**（`is_st_or_delisting`，行为等价；空格边界逐票验证；退市整理 5% 档的金融正确性另立议题，本批不动）。
- **② 0.001 = 浮点容忍**（`RET_TOLERANCE`，权威入口 `is_limit_up_ret` 已存在）⇒ 项 4 是去重不是收窄，「判定变严/候选池变少」前提作废，Phase 3 降级为零口径变化。
- 项 1 已落地（`d9d6951`）；项 3（detect 复查）与 Phase 2/3 待排期。
- 取证锚点：`tdxfeed/execution/limits.py:31,132`、`tdxfeed/features_5d.py:68-78`、`git log -S RET_TOLERANCE`（`48dcc6a`）。

---

## 执行结果补记（2026-11-02，Phase 2a → 2b → 3 链）

### Phase 2a · `find_double_up_hfq` 的 `is_st` 收编

- 改动：`scripts/find_double_up_hfq.py` 调用点 `is_st="ST" in name.upper()` → `is_st_name(name)`（新增导入）。
- 守卫：新增 `tests/test_find_double_up_hfq_st_source.py`——**端到端跑 `main()`**（合成翻倍帧 +
  替身 `frame`/`factor_jumps`/`daily_files`/`get_limit`，名单与产物都落 tmp），验输出 CSV 的 `is_st` 列；
  另加源码委托断言。`ST_INLINE_EXCEPTIONS` 去掉该文件条目。
- RED 证据：`"*S T康佳A"` 旧写法判 False（漏判）、权威判 True ⇒ 行为测试真失败；改后 4/4 绿。
- **行为 diff = 0**：宇宙 5221 只、hfq 样本 17794 行逐条比对，`is_st` 翻转 0
  ⇒ 原方案「需确认极少数空格名称 0→1 差异」的前提实测为空集，无需额外确认。

### Phase 2b · 含「退」两处收编（方案 A）

- 改动：新增 `tdxfeed/names.py::is_st_or_delisting(name) = is_st_name(name) or "退" in str(name)`；
  `scripts/daban_min60_features.py` 的 5% 档判定与 `tdxfeed/daban/scanner.py::symbols` 的候选过滤改走它；
  `ST_INLINE_EXCEPTIONS` 再去 2 条（剩 3 条，均为口径不同的登记例外）。
- 守卫：新增 `tests/test_st_or_delisting.py`——9 个名称形态（ST/*ST/空格拆开 ST/退市整理/普通/空/None）、
  **转发断言**（把 `is_st_name` 换成哨兵后合并入口必须跟着变，防第二份字符串判定）、
  `Scanner.symbols` 行为测试（轻量替身，`allow_st` 两态）。
- RED 证据：`allow_st=False` 时旧实现把 `"*S T康佳A"` 放进候选池（`['sz000016','sh600000']`），
  新实现剔除（`['sh600000']`）。
- **行为 diff = 0**：宇宙 5221 只 / hfq 样本 17794 行，「ST 或退」翻转 0（样本中含「退」名称 0 条）。
- **第三处「退」不收编**：`tdxfeed/daban/paper.py:299` 把退市整理判成 `status="risk_warning"`
  （同行 `is_st` 列另走权威转发），是**状态分类**不是候选过滤；合并会让 ST 名称也被打 risk_warning ⇒ 改口径。
- 遗留议题（本批不动）：退市整理期按 5% 档算涨停价的**金融正确性**另立议题（需重训议程）。

### Phase 3 · 涨停判定容差与阈值常量单源（零口径变化去重）

- **范围裁定（比原方案大）**：原 detect `limit\s*-\s*0\.001` 只认变量名 `limit`，
  漏掉同契约的 `limits` / `limit_val` / `lim` / `df['limit']` 等写法。放宽为
  `(limit[A-Za-z_]*|lim|\[["']limit["']\])\s*-\s*0\.001` 后命中 **18 行 = 代码 17 + docstring 1**，
  多出 9 处（predict 2、factor_engine 2、v3_premarket_top 1、features_5d 1、find_double_up_c 1、
  daban_label_diagnose 1、quant factors 1）**一并收编**——同契约、同常量、可证零差异，分批反而留尾巴。
- 改动：标量点走 `limits.is_limit_up_ret`（`features_5d.is_limit_up_ret` 改转发、`daban_label_diagnose` 直接调）；
  向量化点（Series/ndarray）减 `limits.RET_TOLERANCE`；`features_5d.LIMIT_MAP` / `LIMIT_ST_MAIN_OLD`
  改为从 `LIMIT_RATIO` / `LIMIT_RATIO_ST_MAIN_OLD` 减 `DEFAULT_THRESHOLD_BUFFER` 派生
  （`LIMIT_ST_MAIN_OLD` 按既有 R17 保留为规则名，无调用方）；docstring 里的字面量示例改写。
- **价格级 EPS 是另一契约，不收编**：`ztj - EPS` / `limit_price - _EPS`（formula1_screen/validate、
  hengpan_t1_eval、orderbook_log）比的是**元**，min60 的 `EPS` 还是 **0.005**（值都不同）⇒ 并入会改口径。
- 守卫升级（`tests/test_limit_single_source.py`，6 条新断言）：① 放宽口径全仓 0 命中；
  ② **反作弊**——10 个站点必须真的引用 `RET_TOLERANCE` / `is_limit_up_ret`（把容差整段删掉也能让 ① 变绿，
  但那是判定变严）；③ `RET_TOLERANCE == 0.001` 值钉住；④ 哨兵替换内核验真委托；
  ⑤ `LIMIT_MAP` 派生 + 源码无阈值字面量；⑥ 0.0001 步长网格 × 4 档阈值与旧表达式逐点等价。
- RED 证据：①②④⑤ 四条先失败（18 行命中 / 站点未引用 / 手抄实现不跟哨兵 / 源码含 0.095），
  ③⑥ 五条一开始就绿（值与行为本就如此）⇒ 收编后 42/42 绿。
- **等价证明（四件套 #2）**：`exec` 加载 `HEAD` 版 `features_5d`（只把两条相对导入改绝对，其余逐字不动）
  与工作树版同输入比对——262 只 × 多 `sig_idx` 共 **1044 个特征向量 / 30276 个取值零差异**；
  涨停掩码 **839913 个逐日点**，旧字面量 / 新常量 / 权威标量三者**零差异**；
  `LIMIT_MAP` 与 `LIMIT_ST_MAIN_OLD` 逐值相等。⇒ 历史产物不需要重跑（风险 #6 作废）。
- 台账：`KI-SINGLE-004` OPEN → **FIXED**，detect 换成放宽口径、`fix_note` 记范围裁定与等价证明；
  复扫 STILL_PRESENT 15 · RESOLVED 0 · FIXED_OK 25 · GUARD_MISSING 0，M-5 绑定检查通过。
- 附带翻正：`KI-SINGLE-002` 的「16 处」是**宽口径**计数（含 9 处已知误报：tests/、state、step、stage1、
  inst_net、industry_code、hist_bj、current_steps、OBSIDIAN_SYSTEMD:），守卫用的**精确 AST** 口径
  批次 E 当时是 6 处、Phase 2a/2b 后 **3 处**；两套口径都写进 `fix_note` 与 `AGENTS.md`，避免再被混用。

### 四件套执行情况

| # | 验收项 | Phase 2a | Phase 2b | Phase 3 |
|---|---|---|---|---|
| 1 | 守卫绿 | ✅ 新守卫 4/4 + `test_names_single_source` | ✅ 新守卫 13/13 + 同上 | ✅ `test_limit_single_source` + `test_features_5d` 42/42 |
| 2 | 行为等价 diff | ✅ 5221 只 / 17794 行翻转 0 | ✅ 同集翻转 0 | ✅ 30276 取值 + 839913 判定点零差异 |
| 3 | 基线对照 | ✅ 全量 1810 passed / 3 failed（均为既有本机基线红：mypy、ruff、daban_mcp） | ✅ 1805 passed / 同 3 红 | ✅ 收尾全量 **1833 passed / 3 failed / 6 skipped**——红的还是那三条既有基线红，passed 净增 23 = 三阶段新增守卫用例数；CI 同构副本 **1757 passed / 81 skipped / 4 failed**，4 条全部归因清楚（2 条副本免疫项 + 2 条因我用 tar 拷进副本的 **gitignore 未跟踪目录 `train/`** 才被扫到的语义违规，删掉 `train/` 复跑 333 passed 证明与本轮改动无关）；CI 通过数下限 1140 / 跳过上限 100 均满足 |
| 4 | 台账一致 | ✅ `KI-SINGLE-002` fix_note + 例外表缩减 | ✅ 同上 + `AGENTS.md` 约定 | ✅ `KI-SINGLE-004` FIXED + detect 放宽 + M-5 通过 |

---
生成时间：2026-11-02 15:25:00
生成者：P1-12 批次方案会话
模型版本：不适用（工程改造）
数据日期：不适用
