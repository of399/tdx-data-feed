# P1-8 部署脚本路径统一方案（2026-11-02）

> 任务来源：用户指定的 P1-8（统一部署脚本路径）。
> ⚠️ **编号冲突说明**：整改计划 `docs/REMEDIATION_PLAN_2026-11-01.md` 中的 **P1-8 原是「涨停判定单源」(KI-SINGLE-004)**，与「部署脚本路径统一」并非同一项。本文件按用户指令把 P1-8 重新定义为「部署脚本路径统一」，原「涨停判定单源」顺延为 **P1-12**（与 P1-9 的「名字/池单源」同属 KI-SINGLE 系列，合并一轮审计）。计划台账需相应调整：P1-8 改目标、新增 P1-12。

## 1. 背景与目标

全仓 `grep /home/jiuben/tdx-data-feed` 命中 **669+ 处**，但绝大多数是文档（`README` / `SECURITY` / `v5/audit` 历史快照）。真正影响运行的是部署/运行路径的三处硬编码：

1. `systemd/`（user + system）单元：`WorkingDirectory` / `ExecStart` / `Environment` PATH / `StandardOutput` 共 **62 处**。
2. `scripts/*.sh` 安装/同步脚本：`SRC` / `REPO` / `VAULT` 硬编码（`sync-to-obsidian` / `git_init_vaults` / `hooks/pre-commit` 等）。
3. `tdxfeed/` 入口默认值：`cli.py` / `full_sync.py` / `fin_parquet.py` 写死仓库根。

**目标**：所有部署相关路径来自单一源，仓库根变更（迁移机器 / 改目录名）只需改一处，而非扫 669 处。

## 2. 目标目录结构（单一路径源）

```
仓库根/
├─ tdxfeed/paths.py        # Python 单一源：ROOT / DATA_ROOT（已有）
├─ scripts/lib/tdx-env.sh  # Shell 单一源（本回合新增）：TDX_HOME / TDX_VENV / TDX_DATA_DIR / TDX_CONFIG / TDX_VAULT_PROJ / TDX_VAULT_STOCK
├─ systemd/                # 单元用 ${TDX_HOME} + Environment=TDX_HOME=（迁移步骤 4）
└─ scripts/*.sh            # 全部 source scripts/lib/tdx-env.sh
```

**约定**：
- **Python**：一律 `from tdxfeed.paths import ROOT / DATA_ROOT`，禁止字面量仓库根。
- **Shell**：一律 `source scripts/lib/tdx-env.sh`，仓库根取 `TDX_HOME`（未设则按脚本位置推导），禁止字面量。外部 vault（StockVault）不在仓库内，保留主机默认但可被 `TDX_VAULT_STOCK` 覆盖。
- **systemd**：`Environment=TDX_HOME=/home/jiuben/tdx-data-feed` + 全量 `${TDX_HOME}`；`Documentation=` 行（systemd 不做环境变量展开）保留字面或改相对，不纳入守卫。

## 3. 现有路径引用点梳理（审计）

| 载体 | 文件 | 引用点 | 当前写法 | 统一后 |
|---|---|---|---|---|
| Python 入口 | `tdxfeed/cli.py` | CONFIG_PATH / output_root 默认 / obsidian_vault 默认 | 仓库根字面量（StockVault 外部，保留） | `ROOT / 'config.yaml'` / `str(ROOT/'data')` |
| Python 入口 | `tdxfeed/full_sync.py:37` | OUT_ROOT | `"/home/jiuben/tdx-data-feed/data"` | `str(DATA_ROOT)` |
| Python 入口 | `tdxfeed/fin_parquet.py:26` | CORE_FILE | 仓库根字面量 | `_ROOT` 推导（已存在变量） |
| Shell | `scripts/sync-to-obsidian.sh:9-10` | SRC / VAULT | 字面量 | `$TDX_HOME` / `$TDX_VAULT_PROJ` |
| Shell | `scripts/git_init_vaults.sh:14-19` | REPO / VAULT_STOCK / VAULT_PROJ | 字面量 | `$TDX_HOME` / `$TDX_VAULT_STOCK` / `$TDX_VAULT_PROJ` |
| Shell | `scripts/hooks/pre-commit:11-13` | REPO / VAULT_DEFAULT_* | 字面量 | 同上 |
| systemd user | `systemd/*.service` ~30 文件 | WorkingDirectory/ExecStart/Environment/StandardOutput | 字面量 | `${TDX_HOME}` + Environment |
| systemd system | `systemd/system/*.service` ~20 文件 | 同上（+ 字节镜像守卫） | 字面量 | `${TDX_HOME}` + Environment |

## 4. 统一后的路径约定

- `TDX_HOME`：仓库根；Shell 未设时按 `scripts/lib` 上两级推导；systemd 由 `Environment=` 显式设。
- `TDX_VENV`：默认 `$TDX_HOME/venv`（系统级 `onfailure-alert-system` 的 PATH 同步改 `${TDX_HOME}/venv/bin`）。
- `TDX_DATA_DIR`：`${TDX_OUT_ROOT:-$TDX_HOME/data}`（与 Python `DATA_ROOT` 同源）。
- `TDX_CONFIG`：`$TDX_HOME/config.yaml`。
- `TDX_VAULT_PROJ`：`$TDX_HOME/../ObsidianVaults/tdx-data-feed`。
- `TDX_VAULT_STOCK`：外部 vault，默认 `/home/jiuben/StockVault`，可被 `TDX_VAULT_STOCK` 覆盖（不在仓库内，不可推导）。

## 5. 迁移步骤

- **步骤 1（已完成）· Python 入口**：`cli.py` / `full_sync.py` / `fin_parquet.py` 改用 `tdxfeed.paths.ROOT` / `DATA_ROOT` / `_ROOT` 推导，删除仓库根字面量。
- **步骤 2（已完成）· Shell 单一源**：新增 `scripts/lib/tdx-env.sh`；`sync-to-obsidian.sh` / `git_init_vaults.sh` / `hooks/pre-commit` 改为 source 它，删除字面量。
- **步骤 3（已完成）· 守卫**：新增 `tests/test_deploy_paths_unified.py`，断言上述文件无仓库根字面量且引用单一源。
- **步骤 4（已完成 2026-11-02）· systemd 单元**：形态与原计划**不同**，见 §5.1。

### 5.1 步骤 4：原方案为什么不能用，实测结论与替代形态

原计划是「单元里字面量换成 `${TDX_HOME}` + 每个 service 增 `Environment=TDX_HOME=...`」。
**这条路在本机 systemd 259 上不成立**——2026-11-02 用探针单元逐条实测：

| 指令 | `${TDX_HOME}` 是否展开 | 实测结果 |
|---|---|---|
| `ExecStart=` / `ExecStartPre=` / `ExecStartPost=` | ✅ 生效（未加引号的词） | `test -f ${TDX_HOME}/config.yaml` 退出码 0 |
| `WorkingDirectory=` | ❌ | **单元直接加载失败**：`Unit ... has a bad unit file setting` |
| `StandardOutput=append:` / `StandardError=append:` | ❌ | **静默降级**：`StandardOutput= path is not absolute` → 回落 journal，日志文件不再产生 |
| `Environment="PATH=${TDX_HOME}/venv/bin:..."` | ❌ | 原样保留 `${TDX_HOME}/...`，不展开 |
| `Documentation=` | ❌ | systemd 不做任何变量展开 |

即：四类指令里**只有 ExecStart 一类**能用变量，而工作目录、日志落盘、环境变量这三类
必须拿到**真实绝对路径**。照原方案改，`WorkingDirectory` 会让单元装不上、`StandardOutput`
会静默把日志吞进 journal——**改完线上会红，且红在"看起来装上了"的地方**。

**替代形态（已落地）：模板 + 渲染。**

```
仓库单元（模板）                渲染器（单一路径源）              部署物（真实绝对路径）
systemd/*.service         ──▶   scripts/render_systemd_units.py  ──▶   ~/.config/systemd/user/*
systemd/system/*.service        TDX_HOME ← scripts/lib/tdx-env.sh       /etc/systemd/system/*
```

- 模板里指向仓库的路径一律写 `@TDX_HOME@`（systemd 里 `%` 是 specifier、`@` 只用于模板实例名，
  不冲突）；渲染是纯字符串替换，产物与改造前**逐字节相同**（已用改造前快照全量比对验证：
  49 个单元中 48 个字节一致，唯一差异是 `plan-e-monitor.service` 的 `%h/tdx-data-feed`
  改成显式绝对路径——语义等价，且不再依赖 `%h`）。
- 渲染产物**不入库**（`build/` 已在 gitignore），部署物直接从渲染结果复制；
  源↔部署的零漂移由守卫直接比"渲染结果 vs 已部署文件"。
- `make timer-install` 先渲染再安装；新增 `make systemd-render` 只渲染不安装。

**因此本步骤不需要 root 会话**：渲染对已部署内容字节无变化，`/etc/systemd/system/` 下
21 个镜像单元与渲染结果逐字节一致（守卫 `test_mirror_matches_deployed_bytes` 绿）。
原先"必须 root 重新部署"的前提是"单元内容会变"，而渲染恰好把内容变化消掉了。

## 6. 每一项改动的文件范围与验收标准

| 改动 | 文件范围 | 验收标准 | 状态 |
|---|---|---|---|
| Python 入口推导 | `tdxfeed/cli.py` `full_sync.py` `fin_parquet.py` | `grep -r "/home/jiuben/tdx-data-feed" tdxfeed/` 仅剩 StockVault 外部默认（非仓库根）；`make test` 相关用例绿 | ✅ |
| Shell 单一源 | 新增 `scripts/lib/tdx-env.sh`；改 `sync-to-obsidian.sh` `git_init_vaults.sh` `hooks/pre-commit` | 上述 .sh 无仓库根字面量；source 后 `TDX_HOME` 指向仓库根 | ✅ |
| 守卫（步骤 1–3） | `tests/test_deploy_paths_unified.py` | 反例（字面量回潮）必红 | ✅ |
| 渲染器（步骤 4） | 新增 `scripts/render_systemd_units.py` | `--check` 绿；渲染输出与改造前快照字节一致 | ✅ |
| 单元模板化（步骤 4） | `systemd/*.service\|timer\|path` 18 个 + `systemd/system/*` 15 个（共 33 个含仓库路径） | 模板无仓库根字面量、无 `%h/tdx-data-feed`；`@TDX_HOME@` 覆盖全部仓库路径 | ✅ |
| 安装链改造 | `Makefile`（`timer-install` 依赖新目标 `systemd-render`） | `make timer-install` 后 `~/.config/systemd/user` 与渲染结果字节一致 | ✅ |
| 守卫同步（步骤 4） | `tests/test_deploy_paths_unified.py`（新增 systemd 三条）、`tests/test_systemd_units.py`、`tests/test_systemd_system_units_mirror.py`、`tests/test_sync_post_jobs.py`、`tests/test_daban_ticket_timer_shape.py`、`scripts/verify_obsidian_systemd.sh` | 全部按"渲染口径"读单元；改动模板不改守卫必红 | ✅ |
| 系统级镜像 | `/etc/systemd/system/`（**未改动**） | 渲染结果 == 已部署文件，镜像守卫绿；因内容字节不变，本次**无需 root 会话** | ✅ |

## 7. 风险与缓解

- **systemd 守卫耦合**：`test_mirrored_services_use_the_repo_venv` 原硬断言字面量 → 已改为
  「渲染后路径 == 本机仓库根的 venv」，CI（checkout 在别处）同样成立。
- **模板不是合法单元文件**：仓库里的 `systemd/*.service` 带 `@TDX_HOME@`，不能直接
  `systemctl link` 或丢进 `/etc/systemd/system/`——必须走 `make timer-install` / 渲染器。
  守卫 `test_installed_user_units_match_rendered_templates` 把"改了模板忘了安装"变成红灯。
- **obsidian 漂移检查误报**：`scripts/verify_obsidian_systemd.sh` 的 V0 原先直接 diff
  仓库文件与部署文件，模板化后会永远报漂移 → 已改为先渲染仓库侧（本次修掉的真实回归，
  canary 的 `obsidian_sync` 维度曾由绿转红）。
- **外部 vault 不可推导**：StockVault 保留主机默认 + 可覆盖，不强行统一。
- **既有漂移被新守卫暴露**：`make timer-install` 补上了 P0-1 之后漏装的 6 个用户级单元
  （`predict-daily` 此前缺同步哨兵与 `TimeoutStartSec=1800`）——这是**改造前就存在**的
  部署漂移，本次一并修复。

## 8. 当前状态与下一步（P1-9）

- P1-8 步骤 1–4 **已全部落地**：Python 入口 / Shell 单一源 / 守卫 / systemd 模板+渲染。
- 剩余遗留：`systemd/system/` 下的单元仍是模板，若将来需要**改内容**（而非路径），
  改动后必须走渲染 + （系统级）root 部署；改路径则只改 `scripts/lib/tdx-env.sh` 一处。
- **P1-9（名字/池单源）**的范围、依赖与成功判据见
  `docs/P1-9_NAME_UNIVERSE_SINGLE_SOURCE.md`（含"计划描述与现状的两处偏差"）。

---
生成时间：2026-11-02 11:40
生成者：P1-8 实施会话（步骤 4）
模型版本：不适用（工程改造）
数据日期：不适用
