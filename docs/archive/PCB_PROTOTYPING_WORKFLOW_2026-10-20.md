# 打板（PCB 打样 / 工程设计）端到端工作流规范（MCP + 技能 + 程序）

（2026-10-20 10:40）

> 适用范围：中小批量 PCB **打样**（从需求澄清到交付验收）的单板工程链路，含 2 层/4 层小板、含 SMT 打样、含首件检验。不含大批量量产爬坡与 SMT 治具量产工装设计。
> 参考实现栈：KiCad 8/9（`kicad-cli` + pcbnew Python API + IPC-2581/Gerber X2）为主线；Altium/立创 EDA 场景按同等能力做接口映射。

---

## 📊 核心指标

| # | 指标 | 数值 | 说明 |
|---|---|---|---|
| 1 | 流程阶段数 | **11**（S0 需求 → S10 归档/ECO） | 每阶段独立门禁，禁止跨阶段跳跃 |
| 2 | MCP 服务器 | **7**（eda / library / mfg / plm / doc / vcs / qa） | 写操作与只读分离，副作用集中在 mfg 与 vcs |
| 3 | 技能模块（Skill） | **14** | kebab-case，落在 `.codebuddy/skills/pcb-*/SKILL.md` |
| 4 | 可执行程序（Script） | **18** | 落在 `scripts/pcb/`，单文件 < 30 KB |
| 5 | 质量门禁 | **9**（G0–G8） | 每道门禁有明确的"通过判据 + 阻断动作" |
| 6 | 关键数据对象 | **9** | 契约化（YAML/JSON），是所有环节的交接协议 |
| 7 | 端到端目标周期 | 首次打样 **5–10 个工作日**（不含器件采购长交期） | 交期由厂商工艺与元器件库存决定，流程压缩空间主要在 S1–S6 |
| 8 | 首打成功率目标 | **≥ 70%**（无需 ECO 重投即可点亮上电） | 低于此值说明 DFM/DFA 门禁形同虚设 |

---

## 🎯 Top 10 关键决策点明细

| # | 决策点 | 发生在 | 默认判据 | 决策依据来源 | 责任人 |
|---|---|---|---|---|---|
| 1 | 叠层与阻抗方案定档 | S1 | 2 层/4 层，HDI 需专项评审 | `spec/impedance_target.yaml` | 硬件 + 版图 |
| 2 | 器件选型定档（是否用停产/长交期料） | S1 | 库存 ≥ 3 个月用量、交期 ≤ 4 周 | `parts_avail` 报告 | 硬件 + 采购 |
| 3 | 封装密度等级（IPC-7351） | S3 | 板级默认 Level B；BGA≤0.5mm 升 Level C | `library_audit` 报告 | 版图 |
| 4 | 原理图评审放行 | S2 | ERC 零错误 + 12 项人工 checklist 全过 | `erc_report` + 评审记录 | 硬件负责人 |
| 5 | 原理图↔PCB 一致性 | S4 | 网表比对 0 差异 | `diff_design` 输出 | 版图 |
| 6 | DRC 放行（量产级 0 违规） | S5 | 阻断类 0 / 警告类 ≤ 5 且逐条豁免 | `drc_report` | 版图 |
| 7 | DFM/DFA 阻断项放行 | S6 | 阻断类 0；换线/拼版不违规 | `dfm_report` | DFM 工程师 |
| 8 | 制程参数与拼版方案 | S6 | 阻抗/线宽线距/孔径落在工艺表内 | 厂商工艺能力表 | DFM + 采购 |
| 9 | 下单放行（付款前最后闸门） | S7 | manifest 指纹锁定 + 物料齐套 + 审核人签字 | `order_payload` + manifest | PM/负责人 |
| 10 | 验收判定（收料 vs 返工） | S9 | AOI/ICT/功能测试全项达标 | `acceptance_report` | QA |

---

## 1. 全流程阶段总览（11 阶段 / 9 门禁）

```
S0 需求接收
   └ G0 需求澄清通过
S1 方案与可制造性预评估  ──────────── G1 方案定档
S2 原理图设计与评审      ──────────── G2 原理图放行
S3 封装/符号/库版本 + BOM 准备 ───── G3 库与物料就绪
S4 PCB 布局布线            ──────────── G4 网表一致性
S5 设计规则检查（DRC/ERC/自检）───── G5 DRC 放行
S6 制造包输出 + 投板前审核 ───────── G6 制造包冻结（manifest 指纹）
S7 打样下单与制程跟踪    ──────────── G7 下单成功（锁单）
S8 到货检验 / ICT / 功能测试 ─────── G8 首件通过
S9 交付验收与归档
S10 ECO 改版（回流 S1）
```

### 阶段责任矩阵（RACI）

R = 执行　A = 唯一签批　C = 需咨询　I = 需知会

| 阶段 | 需求方 | 硬件 EE | 版图工程师 | DFM 工程师 | 采购/PM | QA/测试 | 生产(PCBA) |
|---|---|---|---|---|---|---|---|
| S0 需求接收 | A/R | C | I | I | C | I | – |
| S1 方案与可制造性 | C | R | R | A | C | I | I |
| S2 原理图设计与评审 | I | A/R | C | C | I | I | – |
| S3 库与 BOM 准备 | I | A | R | C | R | I | I |
| S4 布局布线 | I | C | A/R | C | I | I | – |
| S5 DRC/自检 | I | C | A/R | C | I | I | – |
| S6 制造包 + 投板审核 | I | C | R | A | R | I | C |
| S7 下单与制程跟踪 | I | I | C | C | A/R | I | C |
| S8 到货检验与测试 | I | C | I | I | C | A/R | R |
| S9 交付验收与归档 | A | R | R | C | C | R | C |
| S10 ECO 改版 | A | R | R | C | C | C | I |

**签批原则**：每个门禁只允许 **1 个 A**；没有 A 的门禁等于没有门禁（实务中「大家都可以放行」＝无人放行）。

---

## 2. 分阶段详解：输入 / 操作 / 输出 / 责任人 / 门禁

### S0 需求接收与澄清

| 项 | 内容 |
|---|---|
| **输入** | 客户/上级口头需求、参考板照片、竞品拆解、既有原理图片段；`docs/` 下的历史项目归档 |
| **操作** | ① 用 `pcb-requirement-intake` 技能把模糊需求结构化；② 强制澄清 **12 项必答**（见下）；③ 生成 `requirement.yaml` + `spec/electrical_spec.md` |
| **输出** | `projects/<pid>/01_req/requirement.yaml`（机器可读）、`spec/electrical_spec.md`（人读）、`spec/question_log.md`（澄清记录） |
| **责任人** | A/R：需求方；C：硬件 EE；I：其余 |
| **门禁 G0** | 12 项必答全部有明确值（不允许"待定"）；`spec/interface_matrix.csv` 中每条接口有驱动电平/速率/供电/方向/连接器；未过 → 退回澄清，不进 S1 |

**必答 12 项**：① 板子用途与功能边界 ② 尺寸/外形/安装孔 ③ 层数与最大速率 ④ 供电树（输入/各路电压/电流/纹波）⑤ 通信接口清单（速率/电平/差分对/连接器型号）⑥ 尺寸公差与叠层 ⑦ 阻抗要求（单端/差分）⑧ 器件限制（指定料/禁用料/国产化率）⑨ 环境（温度/湿度/振动/ESD/EMC）⑩ 认证要求（CE/CCC/UL）⑪ 数量（首件几片 + 是否留余量）⑫ 交期与预算。

> **反模式**：把「先画出来再说」当流程。S0 未澄清直接进 S2，是 90% ECO 返工的根因。

---

### S1 方案与可制造性预评估（DFM 前置）

| 项 | 内容 |
|---|---|
| **输入** | `requirement.yaml`、接口矩阵、目标层数/尺寸、候选器件清单 |
| **操作** | ① `pcb-stackup-advisor` 产出叠层表 + 阻抗计算（含 Dk/Df 取值来源）；② `pcb-parts-sourcing` 查库存/交期/生命周期/国产替代；③ `pcb-cost-estimate` 出成本区间 + 敏感性（层数/线宽/孔径/板材/表面处理对成本与交期的杠杆）；④ 器件布局可行性初判（BGA 扇出、散热、连接器方向、模拟/数字分区） |
| **输出** | `spec/stackup.yaml`（层序 + 介质 + 铜厚 + 阻抗目标 + 计算依据）、`spec/impedance_target.yaml`、`02_prep/parts_availability.csv`、`02_prep/cost_estimate.md`、`02_prep/risk_register.md` |
| **责任人** | A：DFM 工程师；R：硬件 EE + 版图；C：采购 |
| **门禁 G1** | ① 叠层可下单（厂商工艺表内）；② 阻抗目标与叠层自洽（`stackup_check` 通过）；③ 所有关键器件有货或已批准长交期/替代；④ 成本未超预算上限；⑤ 板外形/孔位可与机构件装配 |

> **关键认知**：**阻抗不达标是 4 层板打样最常见的"白花钱"原因**，且往往到 S8 验收才暴露（因为没有 coupon 或 coupon 测错）。必须在 S1 就把「阻抗 + coupon 位置 + 厂商测阻抗方式」三件事一起定档。

---

### S2 原理图设计与评审

| 项 | 内容 |
|---|---|
| **输入** | `spec/`（电气规格、叠层、接口矩阵）、厂商/官方符号库、经批准的器件选型 |
| **操作** | ① 符号自建优先，**禁止**从不明来源库直接取用（`pcb-library-guard` 校验引脚号/引脚名/电气类型/DNP）；② 设计：电源树（去耦、软启动、限流）、上电时序、接口保护（ESD/TVS/共模）、复位与看门狗、测试点（TP）、可制造性标记；③ 跑 ERC；④ `pcb-schematic-review` 走 **12 项人工 checklist**（下方列出）；⑤ 生成原理图 PDF + 网表 |
| **输出** | `03_schematic/<proj>.kicad_sch`（或 .SchDoc）、`03_schematic/erc_report.json`、网表 `03_schematic/netlist.json`（规范化）、`03_schematic/schematic_review.md`（含 checklist 勾选与遗留问题） |
| **责任人** | A/R：硬件 EE 负责人 |
| **门禁 G2** | ERC **零错误**；12 项 checklist 全部通过或有明确豁免记录；网表中无 `no_connect` 悬空（除显式标记）；每条电源轨有测试点；关键接口有 ESD 保护或有书面豁免 |

**原理图 12 项人工 checklist**：
1. 电源完整性：每路有测压点/限流/去耦（≥ 100 nF 每个引脚 + Bulk）
2. 上电时序：IC 上电顺序、电源斜率、EN 拉起逻辑无竞争
3. 复位/看门狗：复位源、阈值、上电释放延时
4. 接口电平：VIOL/VIL/OH/OL 与对端匹配，方向标注完整
5. 保护：ESD/反接/过压/浪涌，靠近入口放置
6. 晶振/时钟：负载电容、去耦、远离噪声源
7. 模拟区：参考地单点连接、模拟电源过滤
8. 差分对：阻抗一致、AC 耦合电容对称、长度容差
9. 调试与烧录：SWD/JTAG/UART 针脚定义、Boot 模式跳线、测试点可及
10. 机械/连接器：编号与位置约束（与机构图一致）、防反插
11. DNP 与替代：关键料有替代料（footprint/电气兼容）标注
12. 版本与变更：图框版本、变更记录、审核签字

---

### S3 封装 / 符号 / 库版本与 BOM 准备

| 项 | 内容 |
|---|---|
| **输入** | 评审通过的原理图、器件选型表、IPC-7351 密度等级策略 |
| **操作** | ① `pcb-library-guard` 逐封装核对：焊盘数/编号与符号引脚一一对应、1 脚方向、焊盘尺寸 vs 引脚、阻焊/锡膏层定义、3D 模型 STEP 存在且朝向正确；② 密���等级评估（Level A/B/C）；③ `pcb-bom-normalize` 生成归一 BOM（MPN 规范化、去重合并、标"缺封装/缺 3D/停产/无货"）；④ `pcb-parts-availability` 拉库存交期；⑤ 库版本快照（`libs/manifest.json`） |
| **输出** | `04_lib/library_audit.json` + `library_audit.md`、`04_lib/bom.csv`（归一 BOM）、`04_lib/parts_availability.csv`、`04_lib/libs_manifest.json` |
| **责任人** | A：硬件 EE；R：版图 + 采购 |
| **门禁 G3** | ① 符号-封装引脚 100% 对应，0 缺失；② 所有 IC/连接器有 3D 模型（或显式登记"无 3D"并接受装配风险）；③ BOM 中无「缺封装」条目；④ 关键料有货或已批替代；⑤ 库快照已提交版本库（`git tag libs/<date>`） |

---

### S4 PCB 布局布线

| 项 | 内容 |
|---|---|
| **输入** | 原理图 + 网表、叠层表、机构图（板框/安装孔/连接器位置/禁布区）、DFM 规则集 |
| **操作** | ① 板框与安装孔落位（先机械后电气）；② 电源分区与去耦布局（去耦电容紧邻引脚，按手册顺序）；③ 关键网络手工布线（电源主干、大电流、差分、时钟、模拟采样）；④ 高速：长度匹配、参考平面连续、过孔对称、避免跨分割；⑤ 散热：地铜皮、散热过孔、热焊盘、爬电距离；⑥ 丝印：丝印不压焊盘、极性标识清晰；⑦ `pcb-netlist-compare` 比对原理图↔PCB 网表 |
| **输出** | `05_pcb/<proj>.kicad_pcb`、布线报告 `05_pcb/layout_report.md`（含关键网络长度、层叠、隔离间距记录）、网表比对 `05_pcb/netlist_diff.json` |
| **责任人** | A/R：版图工程师；C：硬件 EE（关键网络确认）；C：DFM（工艺可行性） |
| **门禁 G4** | 网表比对 **0 差异**（无遗漏、无多余网络、无引脚错位）；机械孔位与机构图 100% 一致；关键网络长度/间距满足规格；大电流路径截面与过孔数满足温升估算 |

**布局布线 checklist（16 项速查）**：板框 → 固定孔 → 连接器 → 变压器/电感 → 发热源 → 模拟区 → 时钟 → 差分 → 电源 → 去耦 → 安全间距 → 散热 → 丝印 → 测试点可及 → 拼版工艺边 → 禁布区。

---

### S5 设计规则检查（DRC / ERC / 设计自检）

| 项 | 内容 |
|---|---|
| **输入** | PCB 文件、规则文件（`.kicad_dru` / DRC 规则集）、厂商工艺能力表 |
| **操作** | ① 跑 DRC（`kicad-cli pcb drc --severity-error --severity-warning --format json`）；② `pcb-drc-triage` 将违规按 **6 类**归类（见附录 B）并区分「阻断 / 警告 / 需豁免」；③ 人工复核每条豁免（写明理由 + 批准人 + 复审时间）；④ 回跑 ERC 兜底；⑤ 专项复核：地平面连续性、参考层切换、阻抗线长度、丝印压盘、阻焊桥、散热过孔、拼版工艺边与邮票孔/V-cut 标记 |
| **输出** | `06_check/drc_report.json` + `drc_report.md`（含违规分类统计 + 豁免台账）、`06_check/drc_exemptions.csv`、`06_check/erc_rerun.json` |
| **责任人** | A/R：版图工程师；C：DFM（工艺类豁免） |
| **门禁 G5** | 阻断类违规 = **0**；警告类 ≤ 5 条且逐条登记豁免；豁免台账版本化；专项复核 8 项全部有结论 |

**DRC 违规 6 类（阻断判定表）**：

| 类别 | 典型违规 | 阻断? |
|---|---|---|
| C1 短路类 | 走线重叠、焊盘重叠、丝印压焊盘 | ✅ 阻断 |
| C2 间距类 | 铜间距 < 板厂能力、铜-板边距不足 | ✅ 阻断（除非工艺放宽已书面确认） |
| C3 孔与盘类 | 孔径 < 板厂最小、焊盘未扩孔、孔未连接/未接网络 | ✅ 阻断 |
| C4 极性与层类 | 阻焊开窗缺失、层叠/内层未定义、镜像错误 | ✅ 阻断 |
| C5 电气规则类 | 网络未连接、单项短路、差分阻抗失配 | ✅ 阻断 |
| C6 工艺美学类 | 丝印压盘、阻焊桥、孤岛铜、尖角 | ⚠️ 警告（丝印压关键焊盘需人工确认） |

---

### S6 制造包输出与投板前审核

| 项 | 内容 |
|---|---|
| **输入** | 通过 G5 的 PCB、归一 BOM、坐标文件需求、厂商工艺表与报价 |
| **操作** | ① 导出 Gerber（X2）+ 钻孔（Excellon）+ 装配图 + 坐标 + 钢网；② `pcb-gerber-audit` 审核（层齐全/极性/单位/镜像/最小线宽/焊盘/钻孔对/板边/丝印溢出/钻孔与焊盘对位）；③ 叠层与阻抗 coupon 一并输出并指定测试方式；④ 拼版（邮票孔 / 半孔 / V-cut / 工艺边）并检查拼版内 DRC；⑤ 导入厂商 DFM 报告并 `pcb-dfm-report` 归一阻断项；⑥ `pcb-manifest` 生成**设计指纹**（全部制造文件 sha256 + 库版本 + 工具版本 + 时间戳）并冻结；⑦ 组装制造包（Gerber+钻孔+BOM+坐标+装配图+钢网+工艺说明+版本说明+验收标准） |
| **输出** | `07_release/<ver>/gerber/`、`drill/`、`bom.csv`、`pos.csv`、`gbrjob`、装配图、`assembly_notes.md`、DFM 报告、**`release_manifest.json`（指纹）** |
| **责任人** | A：DFM 工程师；R：版图 + 采购；C：硬件 EE |
| **门禁 G6** | ① `gerber_audit` 0 阻断；② 厂商 DFM 阻断项 0；③ 拼版内 DRC 通过 + 工艺边 ≥ 板厂要求；④ 阻抗 coupon 已随板；⑤ manifest 已生成并入库；⑥ 制造包文件清单与版本号一致 |

> **强制规则**：**manifest 冻结后，Gerber/钻孔/BOM 任一文件改动都必须重生成 manifest**（`pcb-manifest --verify` 校验），否则下单时物理板与电子文档脱节——这是"打样回来和图纸不一样"的第一大来源。

---

### S7 打样下单与制程跟踪

| 项 | 内容 |
|---|---|
| **输入** | 冻结的制造包 + manifest、厂商报价单、工艺选择、付款信息、收货地址 |
| **操作** | ① 选板厂与工艺（层数/板材/铜厚/阻抗/表面处理/最小线宽线距/最小孔径/拼版数/交期/价格）；② 物料齐套检查（`parts_availability` → 齐套率 100%，否则先备料再投产）；③ `pcb-order-payload` 生成下单 payload（含 manifest 指纹作幂等键）；④ 人工复核价格与交期 → 放行；⑤ 下单并回填订单号；⑥ 定时 `pcb-order-status` 拉状态（生产中/已发货/异常），异常自动推送 |
| **输出** | `08_order/order.json`（订单号/工艺/金额/交期/状态历史）、`08_order/order_payload.json`、`08_order/status_history.jsonl`、发票/付款凭证 |
| **责任人** | A/R：采购/PM；R：DFM（工艺参数）；C：硬件 EE |
| **门禁 G7** | ① 工艺参数与 S1 定档一致；② 齐套率 100%；③ manifest 校验通过；④ 价格与交期与审批一致；⑤ 下单回执与 payload 逐字段一致 |

**下单幂等设计**：幂等键 = `sha256(manifest_id + 厂商 + 工艺参数 + 数量)`；重复提交返回原订单号而非新建。

---

### S8 到货检验 / ICT / 功能测试

| 项 | 内容 |
|---|---|
| **输入** | 到货实物、随板文件（Gerber/BOM/坐标/装配图）、`spec/acceptance_criteria.md`、测试程序 |
| **操作** | ① 外观与尺寸（板厚、孔径、板框、丝印、V-cut/邮票孔、镀层、阻焊颜色）；② AOI 报告查看 + 人工目检（重点：焊点空洞、连锡、偏移、缺件、多件、极性反）；③ X-ray（BGA/QFN 空洞率、虚焊）；④ ICT/飞针（网络连通性/短路）；⑤ 上电首检（限流、首次上电电流、稳态电流、各路电压纹波）；⑥ 功能测试（关键接口、通信、Flash 烧录、传感器、显示）；⑦ 不合格板：拍照 → 归因（设计/工艺/来料/焊接/测试）→ 决定返工或改版 |
| **输出** | `09_accept/inspection/<serial>.md`（含照片索引）、`09_accept/ict_result.csv`、`09_accept/power_on_report.md`、`09_accept/ncr_*.md`（不合格报告） |
| **责任人** | A/R：QA/测试；R：生产(PCBA)；C：硬件 EE（定位）、版图（设计归因） |
| **门禁 G8** | ① 尺寸/外观全项合格；② AOI 无致命缺陷；③ ICT 全网络通过；④ 首次上电无异常电流/冒烟/过热；⑤ 功能测试用例通过率 = 100%（关键项）；⑥ 不合格板已出具归因结论 |

---

### S9 交付验收与归档

| 项 | 内容 |
|---|---|
| **输入** | 全部检验记录、功能测试报告、不合格项处理结论 |
| **操作** | ① 汇总 `acceptance_report`（一次通过率、返工率、平均上电成功率、缺陷分类统计）；② 编写交付物清单与版本说明（板卡版本 ↔ 制造包 ↔ manifest ↔ BOM 版本 ↔ 实物批次）；③ 归档（原理图、PCB、制造包、测试记录、问题单、成品照片、3D/STEP 装配文件）；④ 复盘（Top N 缺陷与根因），回写设计规范与 FMEA |
| **输出** | `09_accept/acceptance_report.md`、`deliverables/manifest.md`（交付物清单 + 指纹）、`docs/postmortem_<pid>.md`、归档包（zip + sha256） |
| **责任人** | A：需求方；R：硬件 EE + QA；C：全体 |
| **门禁** | 交付物清单逐项签收；归档包 sha256 记录；复盘结论已转成「设计规范更新项」并有责任人与期限 |

---

### S10 ECO 改版（变更控制，回流 S1）

| 项 | 内容 |
|---|---|
| **触发** | 验收发现设计缺陷 / 客户需求变更 / 器件停产 / 成本优化 |
| **操作** | ① `pcb-eco-analyze` 做影响面分析：差异器件、差异网络、受影响板层、**需重新打样的原因**、**可复用的部分**（如只改丝印/只换连接器不必重投）、库存呆滞与呆料金额影响；② 变更单（ECO 编号、原因、影响、验证方式、是否需客户确认）；③ 回到 S1 走简化流程（若影响叠层/阻抗则必须全流程） |
| **输出** | `10_eco/eco_<n>.md`、`eco_diff.json`（设计差异）、变更后 manifest（版本 +1） |
| **责任人** | A：需求方或硬件负责人；R：执行变更的工程师 |
| **关键纪律** | ECO 必须**版本化**（不得覆盖原制造包）；旧板与新板**必须物理区分**（丝印版本号/文件名后缀），否则贴错板是现场最贵的错误之一 |

---

## 3. MCP 集成架构

### 3.1 为什么需要 MCP：把"会说话"接到"会干活"

Agent 的短板不在推理，而在**不能可靠地读写 EDA 文件、不能查真实库存、不能把订单写进系统**。MCP（Model Context Protocol）把外部能力标准化为「工具（tool）+ 资源（resource）+ 提示模板（prompt）」，使 AI 能以**可审计、可授权、可重放**的方式操作设计工具与业务系统。

在本方案中，MCP 承担三件事：
1. **动作执行**（把设计动作落到 KiCad/厂商系统/PLM 上）
2. **数据取回**（元件库存、报价、DRC 报告、订单状态、历史项目经验）
3. **受控副作用**（下单、改单、提交审批）—— 必须走「预览 → 人工确认 → 执行」三段式

### 3.2 七类 MCP 服务器

| MCP | 形态 | 核心能力 | 典型工具（tool） | 输入 → 输出 | 副作用 |
|---|---|---|---|---|---|
| `pcb-eda` | stdio 本地进程 | KiCad 8/9：原理图/PCB 读写、ERC/DRC、Gerber/坐标/3D 导出、网表导出 | `open_project` / `read_schematic` / `read_pcb` / `run_erc` / `run_drc` / `export_netlist` / `export_gerber` / `export_pos` / `render_3d` / `place_component` / `set_rule` | 项目路径 + 操作参数 → 结构化 JSON（违规/网络/器件） | 写操作改文件（须工作区隔离 + 版本控制） |
| `pcb-library` | HTTP 远程 | 元件库/商城：MPN 校验、库存、交期、生命周期、国产替代、符号/封装/3D 检索与下载 | `search_part` / `get_stock` / `get_lead_time` / `suggest_alternatives` / `download_symbol` | MPN / 关键字 → 元数据 + 文件下载链接 | 仅下载/缓存（写缓存目录） |
| `pcb-mfg` | HTTP 远程 | 板厂：工艺能力、报价、拼版、下单、改单、状态、物流 | `get_capability` / `quote` / `create_order` / `change_order` / `get_order_status` | 制造包引用 + 工艺参数 → 订单对象 | ⚠️ **写**：下单/改单/取消/付款 |
| `pcb-plm` | HTTP 远程 | 需求与工单：需求单、评审任务、变更单（ECO）、审批流 | `create_requirement` / `submit_review` / `create_eco` / `get_task_status` | 表单 → 工单/审批节点 | ⚠️ **写**：建单/流转审批 |
| `pcb-doc` | HTTP 远程 | 规范与知识：设计规范手册、历史项目、FMEA、供应商工艺文档 | `search_spec` / `get_rule_set` / `lookup_history` | 关键词 → 条目 + 出处 | 只读 |
| `pcb-vcs` | stdio/HTTP | 版本与制品：commit/tag/分支、文件指纹、制品归档 | `commit` / `tag` / `diff` / `verify_manifest` / `archive` | 路径 + 参数 → commit/指纹 | ⚠️ **写**：提交/打标 |
| `pcb-qa` | HTTP 远程 | 检验与制造执行：ICT/飞针/AOI/X-ray/功能测试、烧录工位 | `get_ict_result` / `get_aoi_defects` / `run_fct` / `get_fai_report` | 批次/序列号 → 测试结果 | 只读为主 |

### 3.3 与设计工具的集成要点

- **优先 CLI 而非 GUI 自动化**：`kicad-cli` 提供 `sch export netlist`、`sch erc`、`pcb drc`、`pcb render`、`pcb gerber`、`pcb drill`、`pcb export pos` 等命令，输出均为可解析格式。GUI 自动化（pyautogui/xdotool）**不作为主链路**，只在必须触发"带人工确认的交互式操作"时兜底。
- **Python 插件层**：`pcbnew` 的 Python API 用于 pcbnew 不暴露的精细操作（拼版、pad/zone 改写、DRC 豁免注释写回）。要求与 KiCad 版本严格一致，**在隔离 venv 中固定版本**（对应本项目 `requirements.lock` 的锁定思路）。
- **文件格式优先级**：优先 **IPC-2581**（含层叠/BOM/坐标/工艺全信息，单文件可验证）→ Gerber X2 + Excellon（广泛兼容）→ 厂商在线 3D/网表导入。**能用 IPC-2581 就不要用多文件散包**，可消除层错配与版本错配。
- **只读优先原则**：EDA MCP 默认以只读工具暴露；写操作必须显式开启并写入版本控制分支。
- **长任务转异步**：Gerber/DXF/3D 渲染可能耗时数十秒到数分钟，应以「返回 job_id + 进度查询」方式提供，避免 MCP 调用超时（与本项目 `llm_signal_plan_e.py --resume` 断点续跑思路一致）。

### 3.4 数据流转集成

```
requirement.yaml ──spec_from_requirement.py──▶ electrical_spec.md
        │                                            │
        ├──stackup_advisor──▶ stackup.yaml ─────────┤
        ├──parts_sourcing──▶ parts_availability.csv │
        │                                            ▼
        ▼                                     S2 原理图 (.kicad_sch)
   [pcb-eda MCP]                                     │ export_netlist
        │  ERC                                         ▼
        ▼                                     netlist.json
   erc_report.json                                 │
        │                                            ▼
        ▼                                     S4 PCB (.kicad_pcb)
   [pcb-eda MCP]  ── run_drc ──▶ drc_report.json ──▶ triage ──▶ 分级豁免台账
        │  export_gerber/drill/pos                     │
        ▼                                             ▼
   ┌────────── release/ (Gerber/Drill/BOM/POS/钢网) ──────────┐
   │        ↓ gerber_audit / dfm_report / stackup_check      │
   └────────── release_manifest.json (sha256 指纹) ──────────┘
        │                                   │
        │ 下单 payload (幂等键=manifest)      │ 归档
        ▼                                   ▼
   [pcb-mfg MCP] → order.json → 状态轮询 → 实物
                                              │
                                    [pcb-qa MCP] → ICT/AOI/FCT
                                              │
                                              ▼
                                   acceptance_report.md
                                              │
                                     ECO? ──▶ S10 回到 S1
```

**三类数据流必须分清**：
1. **设计数据**（原理图/PCB/库）—— 版本控制，变更走 commit/ECO
2. **制造数据**（Gerber/钻孔/BOM/坐标/manifest）—— **冻结不可变**，只能整包新版本
3. **业务数据**（需求/工单/订单/测试记录）—— 通过 PLM / 厂商 API / QA 系统落库

> 常见事故：设计改了但制造包没重新导出（1 类），或制造包重新导出但 manifest 没更新（2 类）→ 物理板与文档脱节。`pcb-manifest --verify` 专门防这个。

### 3.5 权限、审计与安全

| 原则 | 落地 |
|---|---|
| 读写分离 | `pcb-eda` 默认只读工具集；写工具单独命名（`*_apply`）并要求 `--confirm` |
| 人工闸门 | 下单、改单、付款、提交客户确认 = **强制人工确认**，Agent 只生成 payload 与差异对照 |
| 最小权限 | 供应链 token 只读 + 单次下单额度；财务/付款独立凭证 |
| 全审计 | 所有写操作记 `logs/pcb_audit.jsonl`：时间、操作者、tool、参数、结果、指纹 |
| 隔离工作区 | EDA 写操作在项目副本工作区进行，通过 diff 审核后合入主干 |
| 数据脱敏 | 客户料号/价格/原理图不外发给第三方 MCP（仅传必要字段） |
| 沙箱 | 脚本执行限制在 `projects/<pid>/` 内，禁止写 `data/`、`models/`（沿用本项目"不删不改数据目录"约定） |

---

## 4. 技能模块（Skill）设计

> 落地位置：`.codebuddy/skills/pcb-<role>/SKILL.md`，frontmatter 含 `name` / `description`（description 写清触发条件，便于按需加载）。

| # | 技能 | 触发条件 | 主要步骤 | 输入 | 输出 | 依赖 MCP / 程序 |
|---|---|---|---|---|---|---|
| S-01 | `pcb-requirement-intake` | 收到新板子需求 | 12 项必答清单 → 追问 → 生成结构化需求 | 口述需求 | `requirement.yaml` + `electrical_spec.md` | `spec_from_requirement.py` |
| S-02 | `pcb-stackup-advisor` | 定层数/阻抗 | 层序设计 → 介质与铜厚 → 阻抗计算 → 工艺表比对 | 电气规格/尺寸/阻抗目标 | `stackup.yaml` + 阻抗计算依据 | `stackup_check.py`、`pcb-doc` |
| S-03 | `pcb-parts-sourcing` | 选型/替代 | MPN 校验 → 库存交期 → 生命周期 → 替代建议 → 齐套率 | 器件清单 | `parts_availability.csv` + 风险清单 | `pcb-library` MCP、`parts_avail.py` |
| S-04 | `pcb-library-guard` | 建库/用第三方库 | 符号↔封装引脚比对 → 焊盘尺寸 → 1 脚方向 → 3D 齐备 → 密度等级 | 符号/封装/3D | `library_audit.json/.md` | `library_audit.py`、`pcb-eda` |
| S-05 | `pcb-schematic-review` | 原理图评审 | ERC → 12 项 checklist → 逐条记录 | `.kicad_sch` | `schematic_review.md` | `pcb-eda`、`erc_report.py` |
| S-06 | `pcb-layout-review` | 布局布线评审 | 分区 → 关键网络 → 长度/间距 → 热 → 丝印 → 拼版边 | `.kicad_pcb` | `layout_report.md` | `pcb-eda` |
| S-07 | `pcb-drc-triage` | DRC 有违规 | 解析 → 6 类归类 → 阻断/警告分级 → 修复建议 | `drc_report.json` | 分级报告 + 豁免台账 | `drc_report.py` |
| S-08 | `pcb-dfm-check` | 投板前 | 厂商 DFM 报告导入 → 阻断项判定 → 工艺参数比对 | 厂商 DFM PDF/JSON | `dfm_findings.json` | `dfm_report.py`、`pcb-mfg` |
| S-09 | `pcb-cam-audit` | 制造包冻结前 | 层齐全/极性/单位/镜像/线宽/焊盘/钻孔对/板边/丝印 | Gerber/Drill | `gerber_audit.md` | `gerber_audit.py` |
| S-10 | `pcb-cost-estimate` | 报价/降本 | 层数/线宽/孔径/表面/板材/拼版 → 单价区间 + 敏感性 | 叠层+工艺+数量 | `cost_estimate.md` | `cost_estimate.py`、`pcb-mfg` |
| S-11 | `pcb-order-placement` | 准备下单 | 齐套检查 → 工艺选型 → payload → 差异复核 → 下单 → 跟踪 | 冻结制造包 | `order.json` + 状态历史 | `order_payload.py`、`order_status.py`、`pcb-mfg` |
| S-12 | `pcb-acceptance-test` | 到货 | 尺寸 → AOI → X-ray → ICT → 上电 → 功能 → 归因 | 实物 + 验收标准 | `acceptance_report.md` + NCR | `pcb-qa`、`acceptance_report.py` |
| S-13 | `pcb-delivery-doc` | 交付 | 清单生成 → 版本对齐 → 归档 → 复盘 | 全部记录 | 交付清单 + 归档包 + 复盘 | `manifest.py`、`pcb-vcs` |
| S-14 | `pcb-eco-change` | 变更 | 影响面分析 → 差异 → 变更单 → 决策（复用/重投） | 两版设计 | `eco_<n>.md` | `diff_design.py`、`pcb-plm` |

### 技能协作关系（三层）

```
        ┌─────────── 编排层（人主导） ───────────┐
        │ S-01 需求  →  S-02 叠层  →  S-03 选型     │
        └───────┬───────────────────────────┬────┘
                │ 规格/叠层/物料            │ 需求回流
                ▼                           │
        ┌──── 设计与验证层 ────┐             │
        │ S-05 原理图评审      │             │
        │ S-04 库守卫(S-03后)  │             │
        │ S-06 布局评审        │             │
        │ S-07 DRC 分诊        │             │
        │ S-08 DFM  +  S-09 CAM│             │
        └───────┬──────────────┘             │
                │ 冻结制造包 + manifest        │
                ▼                              │
        ┌──── 制造与交付层 ────┐              │
        │ S-10 成本            │              │
        │ S-11 下单与跟踪      │              │
        │ S-12 验收            │              │
        │ S-13 交付归档        │              │
        │ S-14 ECO ────────────┼──────────────┘
        └──────────────────────┘
```

- **数据契约是协作的粘合剂**：技能之间不直接传自由文本，只传契约化文件（如 `stackup.yaml` → `cost_estimate.py` 的入参）。
- **技能与 MCP 的边界**：MCP 提供"能力"，技能提供"决策流程与判据"。**没有技能，Agent 只会乱调工具；有技能，Agent 才知道什么时候必须停下来问人**。
- **与本项目既有 skill 的关系**：`tdx-quant-workflow` 是「数据→训练→预测→回测→实盘」的端到端编排；本方案是「需求→设计→制造→验收」的端到端编排。二者结构同构（编排层 / 执行层 / 产物层 / 门禁），可复用同一套目录约定与门禁风格。

---

## 5. 程序（Script）清单与功能定位

> 落地位置 `scripts/pcb/`；遵循本项目规范：Python 3.14+、4 空格、核心函数类型注解、Google 风格 docstring、具体异常捕获、`logging` 而非裸 `print`、`pathlib.Path`、单文件 < 30 KB、输入输出均为契约化文件（stdout 保持干净）。

| # | 程序 | 功能定位 | 输入 | 输出 | 被谁调用 |
|---|---|---|---|---|---|
| P-01 | `spec_from_requirement.py` | 问卷/纪要 → 结构化需求 | 回答 JSON/YAML | `requirement.yaml` | S-01 |
| P-02 | `stackup_check.py` | 叠层↔阻抗↔工艺表三方一致性校验 | `stackup.yaml` + 工艺能力表 | `stackup_check.json` | S-02 / G1 |
| P-03 | `parts_avail.py` | 库存/交期/价格拉取 + 缓存 + 降级 | BOM/MPN 列表 | `parts_availability.csv` | S-03 / G3 |
| P-04 | `bom_normalize.py` | BOM 归一（MPN 规范化/去重/合并/异常标记） | 原始 BOM | `bom.csv` + 异常清单 | S-03 |
| P-05 | `library_audit.py` | 符号↔封装↔3D 引脚与尺寸比对 | 库目录 | `library_audit.json/.md` | S-04 / G3 |
| P-06 | `erc_report.py` | 解析 ERC 输出 → 结构化分类 | ERC 报告 | `erc_report.json` | S-05 / G2 |
| P-07 | `netlist_compare.py` | 原理图↔PCB 网表比对（遗漏/多余/引脚错位） | 两份网表 | `netlist_diff.json` | G4 |
| P-08 | `drc_report.py` | 解析 DRC → 6 类归类 + 分级 + 豁免台账 | DRC JSON | `drc_report.json/.md` + `drc_exemptions.csv` | S-07 / G5 |
| P-09 | `dfm_report.py` | 厂商 DFM 报告解析 + 阻断判定 | 厂商 PDF/JSON | `dfm_findings.json` | S-08 / G6 |
| P-10 | `gerber_audit.py` | 层齐全/极性/单位/镜像/线宽/孔径/钻孔对/丝印 | Gerber+Drill | `gerber_audit.md` | S-09 / G6 |
| P-11 | `cost_estimate.py` | 成本区间 + 敏感性（层数/线宽/孔径/工艺） | 叠层+工艺+数量 | `cost_estimate.md` | S-10 |
| P-12 | `pick_place_check.py` | 坐标 vs BOM/板边/旋转/极性 一致性 | POS + BOM + 板框 | `pp_check.json` | G6 |
| P-13 | `manifest.py` | 设计指纹生成/校验（sha256 + 库版本 + 工具版本） | 制造包目录 | `release_manifest.json` | G6 / G7 |
| P-14 | `order_payload.py` | 生成下单 payload + 幂等键 + 字段校验 | manifest + 工艺参数 | `order_payload.json` | S-11 / G7 |
| P-15 | `order_status.py` | 订单状态轮询 + 状态机 + 异常推送 | 订单号 | `status_history.jsonl` | S-11 |
| P-16 | `acceptance_report.py` | 检验/测试结果判定 + 缺陷分类统计 | ICT/AOI/FCT 结果 | `acceptance_report.md` | S-12 / G8 |
| P-17 | `diff_design.py` | 两版设计差异 → ECO 影响面 | 两版 PCB/网表/BOM | `eco_diff.json` | S-14 |
| P-18 | `trace_log.py` | 统一审计日志（所有 MCP 写操作 + 脚本运行） | 事件 | `logs/pcb_audit.jsonl` | 全局 |

### 5.1 程序间协作（关键链路示例：S4→S6→S7）

```bash
# 1) 导出与比对
kicad-cli sch export netlist --format kicadsexpr 03_schematic/x.kicad_sch > 03_schematic/netlist.net
venv/bin/python scripts/pcb/netlist_compare.py \
    --sch-netlist 03_schematic/netlist.net \
    --pcb-netlist  05_pcb/pcb.net \
    --out          05_pcb/netlist_diff.json          # G4

# 2) DRC 与分诊
kicad-cli pcb drc --format json --severity-error --severity-warning \
    --output 06_check/drc.json 05_pcb/x.kicad_pcb
venv/bin/python scripts/pcb/drc_report.py \
    --drc 06_check/drc.json \
    --out-json 06_check/drc_report.json \
    --out-md   06_check/drc_report.md \
    --exemptions 06_check/drc_exemptions.csv          # G5

# 3) 制造包 + 审计 + 指纹
kicad-cli pcb export gerbers --output 07_release/v1/gerber/ 05_pcb/x.kicad_pcb
kicad-cli pcb export drill --output 07_release/v1/drill/ 05_pcb/x.kicad_pcb
kicad-cli pcb export pos --format csv --units mm --output 07_release/v1/pos.csv 05_pcb/x.kicad_pcb
venv/bin/python scripts/pcb/gerber_audit.py --release 07_release/v1/ --out 07_release/v1/gerber_audit.md
venv/bin/python scripts/pcb/manifest.py --release 07_release/v1/ --out 07_release/v1/release_manifest.json

# 4) 下单（先校验指纹，再出 payload）
venv/bin/python scripts/pcb/manifest.py --verify 07_release/v1/release_manifest.json
venv/bin/python scripts/pcb/order_payload.py \
    --manifest 07_release/v1/release_manifest.json \
    --vendor <vendor> --qty 5 --process <process_profile> \
    --out 08_order/order_payload.json
# 人工复核 diff → 确认 → 调用 pcb-mfg MCP create_order
```

### 5.2 关键数据契约（9 个对象）

| 对象 | 路径 | 关键字段 | 产出者 |
|---|---|---|---|
| 需求 | `01_req/requirement.yaml` | `functions/board_size/layers/power_tree/interfaces/quantities/delivery` | P-01 |
| 叠层 | `spec/stackup.yaml` | `layers[]/dielectric/copper_thickness/target_impedance` | S-02 |
| 电气规格 | `spec/electrical_spec.md` | 表格化接口/电源/环境 | S-01 |
| 归一 BOM | `04_lib/bom.csv` | `mpn/qty/footprint/value/availability/alt_mpn` | P-04 |
| DRC 报告 | `06_check/drc_report.json` | `violations[].{class,severity,location,exemption}` | P-08 |
| DFM 结论 | `06_check/dfm_findings.json` | `findings[].{rule,blocking,resolution}` | P-09 |
| 制造指纹 | `07_release/v1/release_manifest.json` | `files[].{path,sha256}/lib_version/tool_version` | P-13 |
| 订单 | `08_order/order.json` | `order_id/idempotency_key/process/price/qty/eta/status` | P-14 |
| 验收 | `09_accept/acceptance_report.md` | `first_pass_yield/defect_taxonomy/verdict` | P-16 |

---

## 6. 典型场景示例

### 场景 A：2 层 MCU 小板首次打样（最简全链路）

**触发**：客户要一块带 WiFi 的控制板，50×35 mm，5 块样，2 周内要。

| 步 | 动作 | 责任 | 工具/产物 |
|---|---|---|---|
| 1 | `pcb-requirement-intake` 追问 12 项，澄清供电为 5V/3.3V/1.8V，接口仅 UART+USB-C（充电，无 PD） | 需求方 | `requirement.yaml` |
| 2 | `pcb-stackup-advisor` 判 2 层 1.6mm FR-4 即可，USB 2.0 走 90Ω 差分（非 100Ω 差分） | 硬件+DFM | `stackup.yaml` |
| 3 | `pcb-parts-sourcing`：MCU/模组/USB-C 均有货，1 个连接器缺料 → 选国产替代 pin-to-pin | 硬件+采购 | `parts_availability.csv` |
| 4 | 原理图设计 → ERC 0 错 → 12 项 checklist 通过 | 硬件 | `erc_report.json` |
| 5 | `pcb-library-guard`：2 个第三方连接器封装 1 脚方向反了 → 修正 | 版图 | `library_audit.md` |
| 6 | 布局布线（USB 差分 90Ω，长度差 < 0.5 mm） | 版图 | `.kicad_pcb` |
| 7 | 网表比对 0 差异；DRC 阻断 0（2 条丝印压盘警告已豁免） | 版图 | `netlist_diff.json` / `drc_report.md` |
| 8 | CAM 审计通过；厂商 DFM 阻断 0；生成 manifest 冻结 | DFM | `release_manifest.json` |
| 9 | 下单 5 片（含 1 片 coupon 测阻抗），跟单至"已发货" | 采购 | `order.json` |
| 10 | 到货：尺寸/外观合格，ICT 通过，首次上电 120 mA 正常，功能测试 8/8 通过 | QA | `acceptance_report.md` |

**结果**：一次点亮，无 ECO。

---

### 场景 B：4 层 USB3 / 高速板（阻抗与 DFM 是主要风险）

**关键差异**：4 层叠层（信号-地-电源-信号）、差分 100Ω、层间偏移要求、厂商需做阻抗 coupon 并提供测试报告。

| 步 | 关键动作 | 风险与控制 |
|---|---|---|
| 1 | 需求澄清追加：总长、插入损耗上限、EMI 等级 | 高速指标不明确 → 阻抗与材料全靠猜 |
| 2 | 叠层：L1 信号 / L2 GND / L3 PWR / L4 信号，1oz 铜，指定板材 Dk | 不同板材 Dk 差异 → 阻抗偏移 |
| 3 | `stackup_check` 校验阻抗计算与厂商工艺表 | 交期/成本 |
| 4 | 差分对：AC 耦合电容对称放置、参考平面连续、避免跨 L1↔L4 换层 | 阻抗不连续 → 眼图失败 |
| 5 | DRC + 专项复核：过孔对称、回流路径、层切换 | 高速板专项项，DRC 查不出来 |
| 6 | DFM：孔径 ≥ 0.2 mm、线宽线距 ≥ 3/3 mil、阻抗 coupon 位置 | 阻断项 = 改板 = 重投 |
| 7 | 下单时明确要求：**阻抗测试报告随货**、附 TDR 切片位置 | 不写进工艺单 = 板厂不测 |
| 8 | 验收：先量阻抗（coupon TDR），不合格直接进 ECO，**不要先焊** | 阻抗不合格板焊上去是纯浪费 |

> **门禁心得**：高速板必须把「阻抗 coupon + 随货阻抗报告」写进 S1 定档与 S7 下单工艺单，否则问题一定在 S8 才暴露。

---

### 场景 C：打样回来发现电源短路（归因 → ECO → 再打样）

**现象**：板子到手，上电瞬间限流保护动作，3.3V 轨短路。

| 步 | 动作 | 工具 | 产出 |
|---|---|---|---|
| 1 | **禁止反复上电**（可能烧毁 MCU/电源 IC） | — | 安全纪律 |
| 2 | 目检 + 显微镜找短路点（助焊剂/锡珠/连锡） | AOI + 人工 | 照片 |
| 3 | X-ray 查 BGA/QFN 内部连锡或空洞 | X-ray | 报告 |
| 4 | 必要时割线定位（切线法：逐网断开二分） | 治具 | 定位结论 |
| 5 | 判归因：① 焊接工艺（连锡）→ 返工/降功率重焊；② 设计缺陷（如 3.3V 与 1.8V 走线间距过近、散热焊盘跨网络）→ ECO | `diff_design` + DRC 复核 | 归因结论 |
| 6 | 若设计缺陷 → `pcb-eco-change` 出影响面：是否需全板重投？能否局部改（换料/改封装/加跳线） | P-17 | `eco_diff.json` |
| 7 | ECO 决策会：局部改（加 0Ω 隔离/改封装） vs 全板重投 vs 手工飞线补救 | 需求方 A | ECO 单 |
| 8 | 重投时**升版**（v2），丝印版本号同步修改，v1 制造包保留不覆盖 | `manifest.py` | v2 manifest |
| 9 | 复盘写进设计规范：例如"电源轨间距最小值"、"散热焊盘跨网络禁止项" | S-13 | 规范更新项 + 责任人 |

---

### 场景 D：量产前 ECO 改版（器件停产 + 客户加功能）

| 步 | 动作 | 工具 |
|---|---|---|
| 1 | 触发：主控 MCU 停产（厂商 EOL 通知） + 客户要求加一路 CAN | PLM |
| 2 | 影响面分析：替代料是否 pin-to-pin？封装是否变？是否需改层？CAN 收发器新增会影响布局空间吗？ | P-17 `diff_design` |
| 3 | 库存影响：旧料库存 X 片，能否覆盖新版本生产？是否需先消化库存 | `parts_avail` |
| 4 | 变更分级：**A 类**（丝印/文档）→ 直接改，不重投；**B 类**（局部改版，不动叠层）→ 简化流程重投；**C 类**（改层/改阻抗/改板框）→ **全流程走 S1 起** | 规则表 |
| 5 | 生成 ECO 单 + 客户确认（若影响外观/接口） | `pcb-plm` |
| 6 | 重投：新版本号 + 丝印版本 + 新 manifest + 新订单 | P-13/P-14 |
| 7 | 归档：v1/v2 制造包、订单、验收报告全部关联到同一 ECO 链 | `pcb-vcs` + S-13 |

---

## 7. 常见问题处理（Troubleshooting）

| # | 症状 | 根因 | 定位手段 | 处置动作 | 预防门禁 |
|---|---|---|---|---|---|
| 1 | DRC 报大量短路/重叠 | 封装引脚数与符号不符 / 走线压焊盘 | `drc_report.py` 分类统计，找同源批量项 | 修正封装或重排，不逐条豁免 | G3 库守卫（引脚 100% 对应）+ G5 豁免台账 |
| 2 | 供应商称"封装和您的不一样" | 第三方库封装非标准 / 1 脚方向反 | 3D 模型比对 + 首件 X-ray | 修正封装 + 手工改板或重投 | G3 强制 3D 齐备 |
| 3 | BOM 有 MPN 但买不到 | 停产 / 停产公告未拉取 / 区域限制 | `parts_avail.py` + 生命周期字段 | 选替代（pin-to-pin）并更新符号 | G1/G3 库存与生命周期检查 |
| 4 | 齐套率不足，板子回来少件 | 未做齐套检查就投产 | 齐套率报表 | 备料后投产，或改"部分贴片 + 手工补焊" | G7 齐套率 100% 硬门禁 |
| 5 | Gerber 报错/板厂反馈层不对 | 导出漏层、层名映射错、镜像错 | `gerber_audit.py` + 板厂回执 | 重新导出 + 复审 manifest | G6 CAM 审计 0 阻断 |
| 6 | 阻抗不达标（高速板必查） | 叠层/介质/线宽与厂商实际工艺偏差 | coupon TDR + 板厂随货报告 | 改线宽或改叠层后重投；局部可接受则改主板铜箔 | S1 叠层定档 + G6 阻抗 coupon + S7 写明随货测阻抗 |
| 7 | 到货短路 | 助焊剂残留/锡珠/连锡/设计缺陷 | 显微镜 → X-ray → 割线二分 | 工艺问题返工；设计问题走 ECO | G5/G6 + S8 上电前必检 |
| 8 | 上电即冒烟/限流 | 电源极性反、IC 装反、阻焊/焊盘错 | 首次上电限流 + 红外测温 + 逐轨断开 | 立即断电，定位后 ECO | G2 checklist 11 + G5 DRC C4 极性类 |
| 9 | 板子尺寸超差/毛刺 | 板框未按板厂补偿、V-cut 位置错 | 卡尺 + 首件量测 | 返工（一般不重投） | S4 板框补偿 + G4 机构图核对 |
| 10 | 焊点虚焊/空洞率高 | 钢网开孔与焊盘不匹配、贴装压力/温度曲线 | AOI + X-ray + 切片 | 调整钢网/回流曲线；必要时手工补焊 | S6 钢网随包 + 焊盘与钢网匹配检查 |
| 11 | 功能测试过不了但硬件无故障 | 测试点未引出、程序版本错、供电时序 | 对照 `spec/acceptance_criteria.md` 逐项 | 补测点 → ECO v2；程序问题重测 | S2 checklist 9（测试点）+ S9 用例冻结 |
| 12 | 贴错板版本 | 多版本无物理区分 | 丝印版本号 + 板边打标 | 隔离召回，仓库重标 | S10 ECO 纪律（版本化 + 物理区分） |
| 13 | BOM 数量与实际贴装不符 | 坐标与 BOM 不同步 | `pick_place_check.py` | 停止上电，核对坐标/BOM 版本 | G6 POS/BOM 一致性 + manifest |
| 14 | 板厂反馈"阻抗/阻焊/颜色做不了" | 工艺参数超能力表 | `dfm_report.py` + `get_capability` | 改工艺或改设计 | G6 DFM 阻断 0 |
| 15 | 每次打样都要"临时改一下" | 需求未澄清/变更未受控 | 变更记录缺失 | 补 S0 澄清模板 + ECO 流程 | G0 + S10 |

**处理通则**：
1. **先复现/先隔离，后归因**——先确定是"所有板都这样"还是"个别板"，决定是设计问题还是工艺问题。
2. **归因必须落到三类之一**：设计（ECO）/ 工艺（供应商沟通 + 工艺单修正）/ 来料与焊接（换料、调整制程）。
3. **每个问题都要回写门禁**（"预防门禁"列）——否则同类问题会重复发生，这是"打样一次比一次贵"的根治办法。

---

## 8. 质量门禁清单（G0–G8，可直接贴墙）

| 门禁 | 判据（全部满足才过） | 阻断动作 | 责任人 |
|---|---|---|---|
| **G0** 需求澄清 | 12 项必答无"待定"；接口矩阵完整 | 退回需求方 | 需求方 |
| **G1** 方案定档 | 叠层在工艺表内；阻抗自洽；关键料有货/已批替代；成本不超预算 | 重新选型 | DFM |
| **G2** 原理图放行 | ERC 0 错；12 项 checklist 全过或书面豁免 | 打回改图 | 硬件负责人 |
| **G3** 库与物料就绪 | 符号-封装引脚 100% 对应；3D 齐备；BOM 无缺封装；库快照入库 | 补库/换料 | 硬件 + 版图 |
| **G4** 网表一致性 | 原理图↔PCB 0 差异；机械孔位与机构图一致 | 重新布局 | 版图 |
| **G5** DRC 放行 | 阻断类 0；警告 ≤5 且逐条豁免；8 项专项复核有结论 | 回改板 | 版图 |
| **G6** 制造包冻结 | CAM 审计 0 阻断；DFM 阻断 0；拼版 DRC 通过；含阻抗 coupon；manifest 已生成 | 重导出重审 | DFM |
| **G7** 下单 | 工艺与定档一致；齐套 100%；manifest 校验通过；价格交期与审批一致 | 不放行 | 采购/PM |
| **G8** 首件通过 | 尺寸外观合格；AOI 无致命；ICT 全通过；首电无异常；功能 100% | 返工或 ECO | QA |

---

## 📈 统计分布（流程风险与工时占用，按典型 2 层打样估算）

**工时占比**（设计 60% / 制造准备 20% / 验收与返工 20%）：

| 阶段 | 工时占比 | 周期占比 | 主要不确定性来源 |
|---|---|---|---|
| S0 需求 | 6% | 4% | 客户表述模糊度 |
| S1 方案 | 10% | 6% | 阻抗/工艺窗口 |
| S2 原理图 | 16% | 10% | 电路复杂度 |
| S3 库与 BOM | 8% | 5% | 第三方库质量、缺料 |
| S4 布局布线 | 26% | 16% | 密度/高速/散热 |
| S5 DRC/自检 | 10% | 6% | 规则完备性 |
| S6 制造包 | 6% | 4% | DFM 阻断项数量 |
| S7 下单跟踪 | 4% | 12% | **板厂产能与交期** |
| S8 验收 | 10% | 25% | **到货与返工** |
| S9 交付归档 | 4% | 2% | 文档完整性 |

**缺陷来源分布**（历史经验量级，业界通用）：

| 缺陷类别 | 占比 | 主要发生在 | 拦截门禁 |
|---|---|---|---|
| 原理图/逻辑缺陷 | ~30% | S2 | G2 |
| 封装/库错误 | ~15% | S3 | G3 |
| 布局布线缺陷 | ~20% | S4–S5 | G4/G5 |
| 工艺与参数错 | ~15% | S6–S7 | G6/G7 |
| 焊接与来料 | ~15% | S8 | G8 |
| 文档/版本混乱 | ~5% | S9–S10 | manifest + ECO |

> 结论：**把拦截点前移到 G0–G3（占缺陷约 45% 且都在低成本阶段）收益最大**；S8 之后的返工成本是 S1 的 10–50 倍。

---

## ⚠️ 风险提示

1. **历史经验不代表未来**：本文的工时占比、缺陷分布、门禁设置基于通用 PCB 工程经验与本项目对"可审计流程"的要求推导，**未在具体板厂/具体产品线上实测校准**；首次落地必须用 2–3 个真实项目回测校准参数（尤其缺陷分布与齐套率口径）。
2. **AI 不能替代工程签批**：MCP + 技能只负责"把正确的事做得更快、留下证据"，**所有 G0–G8 门禁的签批责任仍属人**；AI 生成的 DRC 豁免、阻抗计算、BOM 替代建议均需人工复核。
3. **下游 MCP 能力假设**：本方案中 `pcb-mfg`（板厂下单 API）、`pcb-qa`（ICT/AOI 报告 API）、`pcb-library`（商城库存接口）均为**假设存在可编程接口**；实际多数中小板厂与国产贴片厂**不提供开放 API**，需降级为"人工录入 + 文件交换"模式（程序需设计 `--offline` 降级路径）。落地前必须先验证接口可得性。
4. **数据安全与客户保密**：原理图、BOM 含客户专有设计与价格，须配置 MCP 脱敏白名单、禁止外发到第三方模型；本方案的"脱敏"条目在实现前需法务/IT 确认。
5. **改版纪律是最高杠杆项**：多数打样浪费来自"口头改一下"绕过版本控制；本方案要求 ECO 版本化 + 丝印物理区分，**初期执行成本高，但收益最大**，不建议因嫌麻烦而取消。
6. **速度与成本权衡**：G1–G6 门禁会显著增加前期工时（预估 +30~50%），换取返工率下降；若项目是"一次性验证性打样"且失败成本可承受，可按 S-14 规则降级为 C 类精简流程，但**不可跳过 G6（制造包冻结）**。
7. **工具版本漂移**：KiCad / pcbnew Python API / 厂商解析器版本差异会改变导出结果（尤其实例坐标与 Gerber 属性），必须锁定版本并写入 manifest。

---

## 🔧 下一步建议

### 阶段一（第 1 周）——先立契约，不碰工具
1. 落地 9 个数据契约的 **JSON Schema**（`config/pcb/schemas/*.json`），并写 1 份 `spec/` 模板（电气规格 + 叠层 + 验收标准）。
2. 产出 `docs/PCB_DESIGN_RULES.md`：叠层选项表、DRC 规则集、DFM 规则集、12 项原理图 checklist、16 项布局 checklist（本文已给出骨架，补齐具体数值）。
3. 明确**首个小项目**（建议 2 层板，5 片）作为试点，先用**纯人工 + 脚本**跑通 S0–S9，验证流程本身，再接 MCP。

### 阶段二（第 2–3 周）——接两个最确定的 MCP
4. 先接 `pcb-eda`（KiCad CLI，本地可控、无外部依赖）与 `pcb-vcs`（Git + manifest 指纹）——这两者收益最高、风险最低。
5. 实现 P-07 `netlist_compare.py`、P-08 `drc_report.py`、P-13 `manifest.py` 三个程序（覆盖 G4/G5/G6 三个门禁）。
6. 写 3 个技能：`pcb-drc-triage`、`pcb-cam-audit`、`pcb-delivery-doc`（分别对应 S-07、S-09、S-13），把"判据"固化为可复用流程。

### 阶段三（第 4–6 周）——接供应链与验收
7. 验证 `pcb-mfg` / `pcb-library` 接口可得性；**不可得则实现离线降级**（CSV 导入 + 人工确认 + payload 导出），不要为了"接 MCP"而卡住流程。
8. 实现 P-14/P-15（订单 payload + 状态跟踪）与 P-16（验收报告）。
9. 上线 S-11/S-12 技能与 G7/G8 门禁。

### 阶段四（持续）——闭环与校准
10. 每完成 2 个项目，做一次**流程校准复盘**：统计各阶段工时占比、缺陷来源分布、返工次数、门禁漏拦次数，修订本文与门禁判据。
11. 把每次验收发现的问题**回写** `docs/PCB_DESIGN_RULES.md`（形成"错误一次，规则长一条"）。
12. 视需要引入 `pcb-plm`（需求/ECO 工单化）与 `pcb-doc`（规范检索），把 ECO 从文档升级为受控流程。

### 立即可做的 3 件事（今天就能开始）
- 用 `pcb-requirement-intake` 的 12 项清单，去澄清**当前手上正在打样的那块板**——检验清单好不好用，成本 30 分钟。
- 对**最近一次打样**的制造包跑一次手工版 CAM 审计（层齐全/极性/钻孔对/丝印越界），看能翻出多少历史问题。
- 建立 `projects/<pid>/` 目录骨架与 9 个契约文件的空模板，后续所有项目沿用同一结构。

---

## 附录 A：MCP 配置示例（CodeBuddy / 通用 MCP 客户端）

```json
{
  "mcpServers": {
    "pcb-eda": {
      "command": "python3",
      "args": ["-m", "mcp_pcb_eda", "--project-root", "${workspace}/projects"],
      "env": { "KICAD_VERSION": "9.0" }
    },
    "pcb-library": {
      "command": "npx",
      "args": ["-y", "@pcb/mcp-library"],
      "env": { "LCSC_TOKEN": "${LCSC_TOKEN}" }
    },
    "pcb-mfg": {
      "command": "npx",
      "args": ["-y", "@pcb/mcp-mfg"],
      "env": { "MFG_API_BASE": "${MFG_API_BASE}", "MFG_TOKEN": "${MFG_TOKEN}" }
    },
    "pcb-plm": {
      "command": "npx",
      "args": ["-y", "@pcb/mcp-plm"],
      "env": { "PLM_BASE": "${PLM_BASE}", "PLM_TOKEN": "${PLM_TOKEN}" }
    },
    "pcb-vcs": { "command": "git", "args": ["-C", "${workspace}"] },
    "pcb-doc": { "command": "npx", "args": ["-y", "@pcb/mcp-doc"] },
    "pcb-qa": { "command": "npx", "args": ["-y", "@pcb/mcp-qa"], "env": { "QA_BASE": "${QA_BASE}" } }
  }
}
```

**工具命名约定**（读/写分离，危险动作必须显式）：

| 类别 | 命名 | 语义 |
|---|---|---|
| 只读 | `read_*` / `get_*` / `list_*` / `search_*` | 无副作用，可并发 |
| 分析 | `analyze_*` / `check_*` / `audit_*` | 只计算不落盘（除显式 `--out`） |
| 写入 | `apply_*` / `write_*` / `export_*` | 改文件，必须在项目工作区 + 记录审计日志 |
| 高危 | `create_order` / `change_order` / `cancel_order` / `pay` | **必须人工确认**，Agent 只生成 payload 与差异 |

---

## 附录 B：DRC 违规分类字典（P-08 固化用）

| 类别码 | 名称 | 判定关键词 | 级别 | 责任技能 |
|---|---|---|---|---|
| C1 | 短路 | `short_circuit` / `items on same net` / `copper overlap` | 阻断 | S-07 |
| C2 | 间距 | `clearance` / `track_dangling`（近边） / `edge_clearance` | 阻断 | S-07 + S-08 |
| C3 | 孔与盘 | `hole_clearance` / `annular_ring` / `pad_to_hole` / `unconnected` | 阻断 | S-07 |
| C4 | 极性与层 | `silk_over_copper` / `footprint` / `layer` / `mirror` | 阻断 | S-07 + S-04 |
| C5 | 电气规则 | `net_conflict` / `diff_pair_gap` / `via_style` | 阻断 | S-07 + S-06 |
| C6 | 工艺美学 | `starved_thermal` / `courtyard_overlap` / `text_height` | 警告 | S-07 |

---

## 附录 C：术语与缩写

| 缩写 | 全称 | 说明 |
|---|---|---|
| DFM | Design for Manufacturability | 面向制造的设计（可制造性） |
| DFA | Design for Assembly | 面向装配的设计（可装配性：拼版、贴装、焊接） |
| DRC / ERC | Design / Electrical Rule Check | 设计规则 / 电气规则检查 |
| IPC-7351 | 通用零件性能描述标准 | 密度等级 A/B/C |
| IPC-6012 | 印制板通用性能规范 | 常规产品 Class 2 |
| IPC-2581 | 印制板制造数据交换标准 | 含层叠/BOM/坐标/工艺的单文件格式 |
| Gerber X2 | 扩展 Gerber | 带属性与层的 Gerber 格式 |
| Excellon | 钻孔文件格式 | 坐标 + 钻孔 |
| AOI | Automated Optical Inspection | 自动光学检测（贴装外观） |
| ICT / FAI | In-Circuit Test / First Article Inspection | 在路测试 / 首件检验 |
| FCT | Functional Circuit Test | 功能测试 |
| V-cut | 分板刀痕 | 铣槽分板 |
| 钢网 | Stencil | 锡膏印刷模板 |
| TP | Test Point | 测试点 |
| ECO | Engineering Change Order | 工程变更单 |
| 齐套率 | Kitting Ratio | 投产所需物料到位比例 |

---

## 附录 D：本文的事实边界与自评

- **工程常识类内容**（DRC/DFM 类别、验收项目、阻抗与 coupon 流程、变更控制方法）：来自通用 PCB 工程实践，**表述可靠**。
- **流程门禁、目录结构、程序清单、契约对象**：本方案**设计产出**，可按实际团队规模裁剪；未在真实项目上跑过端到端验证。
- **具体数值**（工时占比、缺陷分布、12 天周期、70% 首打成功率）：**量级估计**，需用 2–3 个真实项目校准后方可作为管理指标。
- **MCP 接口假设**：板厂/商城/QA 系统的可编程接口**未在本环境验证**，多数国产供应商很可能需离线降级（见风险提示 3）。
- **置信度**：流程结构与职责划分 **高**（约 0.85）；程序与契约设计 **中高**（约 0.7，因依赖实际 EDA 工具链与厂商工艺表）；数值类指标 **低**（约 0.45，必须实测校准）。
- **出处说明**：KiCad 8/9 CLI 子命令、IPC-7351/6012/2581、Gerber X2 与 Excellon 格式、AOI/ICT/FCT 工序、ECO 与 manifest 冻结机制，均为公开标准与通用工程实践；未引用任何未公开的内部文档。

---

生成时间：2026-10-20 10:40:00
生成者：CodeBuddy（PCB 打样工作流方案设计）
数据日期：不适用（本方案为流程规范，未依赖行情数据）
模型版本：不适用（规则与经验库设计文档）
