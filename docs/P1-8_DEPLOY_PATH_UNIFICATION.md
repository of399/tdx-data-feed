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

- **步骤 1（本回合已完成）· Python 入口**：`cli.py` / `full_sync.py` / `fin_parquet.py` 改用 `tdxfeed.paths.ROOT` / `DATA_ROOT` / `_ROOT` 推导，删除仓库根字面量。
- **步骤 2（本回合已完成）· Shell 单一源**：新增 `scripts/lib/tdx-env.sh`；`sync-to-obsidian.sh` / `git_init_vaults.sh` / `hooks/pre-commit` 改为 source 它，删除字面量。
- **步骤 3（本回合已完成）· 守卫**：新增 `tests/test_deploy_paths_unified.py`，断言上述文件无仓库根字面量且引用单一源。
- **步骤 4（下一机械批次，R-部署）· systemd 单元**：
  1. 仓库 `systemd/` 全部 user+system 单元：字面量 `/home/jiuben/tdx-data-feed` → `${TDX_HOME}`（`Documentation=` 行除外）；每个 service 增 `Environment=TDX_HOME=/home/jiuben/tdx-data-feed`。
  2. 同步改写守卫 `tests/test_systemd_system_units_mirror.py::test_mirrored_services_use_the_repo_venv`：接受 `${TDX_HOME}/venv/bin/python` 形态（原硬性断言字面量会红）。
  3. **root 部署会话**：`cp systemd/system/* /etc/systemd/system/ + daemon-reload + restart`（与 P0-1/P0-2 同一条 root 路径），否则 `test_mirror_matches_deployed_bytes` 会红。
  4. 守卫 `tests/test_deploy_paths_unified.py` 扩展：断言 systemd 单元功能行（ExecStart/WorkingDirectory/Environment/StandardOutput/ExecStartPre/Post）无仓库根字面量、且含 `TDX_HOME`。

## 6. 每一项改动的文件范围与验收标准

| 改动 | 文件范围 | 验收标准 |
|---|---|---|
| Python 入口推导 | `tdxfeed/cli.py` `full_sync.py` `fin_parquet.py` | `grep -r "/home/jiuben/tdx-data-feed" tdxfeed/` 仅剩 StockVault 外部默认（非仓库根）；`make test` 相关用例绿 |
| Shell 单一源 | 新增 `scripts/lib/tdx-env.sh`；改 `sync-to-obsidian.sh` `git_init_vaults.sh` `hooks/pre-commit` | 上述 .sh 无仓库根字面量；source 后 `TDX_HOME` 指向仓库根；脚本手动 dry-run 跑通 |
| 守卫 | `tests/test_deploy_paths_unified.py` | 反例（字面量回潮）必红 |
| systemd（步骤4） | `systemd/*.service` `systemd/system/*.service` ~50 文件 + `tests/test_systemd_system_units_mirror.py` + `tests/test_deploy_paths_unified.py` | 仓库单元无字面量（Documentation 除外）；`test_mirrored_services_use_the_repo_venv` 接受 `${TDX_HOME}`；root 部署后字节镜像守卫绿 |

## 7. 风险与缓解

- **systemd 守卫耦合**：`test_mirrored_services_use_the_repo_venv` 硬断言字面量 → 步骤 4 必须同步改守卫，否则 CI 红。
- **root 依赖**：systemd 单元改完不过 `test_mirror_matches_deployed_bytes` 直到 root 部署；与 P0-1/P0-2 共用一次 root 会话最经济。
- **外部 vault 不可推导**：StockVault 保留主机默认 + 可覆盖，不强行统一。

## 8. 下一步（P1-9 启动建议）

P1-9「名字/池单源」：归一 `norm_code`（当前 3 份）、`is_st`（3 种语义）、`load_universe`（7 份）到单一实现。

- **启动第一步（先查后说）**：用 code-explorer 或 grep 全仓定位 `norm_code` / `is_st` / `load_universe` 的全部定义点与调用点（关注 `scripts/_common.py`、`tdxfeed/paths.py`、`tdxfeed/daban/limit_price.py`）。
- **定 canon**：选一份作为唯一实现（建议 `scripts/_common.norm_code` 委托 `tdxfeed/paths.exchange_prefix`/`normalize_code`，与现有单源守卫一致），其余标记删除。
- **建守卫**：扩展 `tests/test_names_single_source.py`（断言每种实现全局唯一）+ 升级 `tests/test_universe_path_single_source.py`。
- **迁移调用点**：逐文件改调用到 canon，删重复定义；每改一处跑 `make gate`。
- **与 P1-8 关系**：计划依赖图标注 P1-8/9/10 互相独立可并行；但共享工作树下建议分提交、错峰推进，避免 `git` 冲突。P1-9 与 P1-12（原涨停判定单源）同属 KI-SINGLE，可合并一轮审计。
