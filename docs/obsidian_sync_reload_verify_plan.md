# systemd 配置重载验证操作方案 (2026-10-12 18:40)

> 目的：验证 `obsidian-sync.path` / `obsidian-sync.service` 的修复配置（`0b4c5c8`：监听改为 `PathChanged` 且仅监听人工源文档、解耦 `data/plan_e` 与 `training/eval`；`.service` 放宽 `StartLimitIntervalSec=60`/`StartLimitBurst=10`）在 systemd 运行时**真正生效**。
> 适用范围：用户级（user）单元 `obsidian-sync.path` + `obsidian-sync.service`。`daemon-reload` 会重载全部 user 单元，但本方案只变更上述两个单元，其余 timer 不受影响。

---

> ## ⚠️ 部署铁律（凡改 `systemd/` 必做）
> **systemd 从 `~/.config/systemd/user/` 加载单元，不从仓库 `systemd/` 加载。** 因此：
> 1. 凡修改 `systemd/` 目录下任何 `*.service` / `*.timer` / `*.path` 文件并提交后，**必须执行 `make timer-install`**（复制到 `~/.config/systemd/user/`，即实际加载目录）；
> 2. 随后 `systemctl --user daemon-reload` 让管理器重读；
> 3. 对 `*.path` 单元还需 `restart <单元>.path` 重建 inotify 监视（`daemon-reload` 不会自动重挂）；
> 4. 对处于 `start-limit-hit` 的单元还需 `reset-failed`。
>
> 若只改仓库文件而不部署，系统跑的仍是旧配置——这就是本次修复一度"未生效"的根因。
> **漂移检测已固化**：`make status` / `make canary` 会对比仓库与部署文件（V0），不一致即告警并提示 `make timer-install`；`make verify-obsidian` 可一键复跑本方案 V0–V8 全部判定。

---

## 0. 关键认知（决定方案成败，必读）

1. **systemd 从 `~/.config/systemd/user/` 加载单元，不从仓库 `systemd/` 加载。**
   已提交到 git 的修复只是"源文件"，必须先 `make timer-install` 部署到 `~/.config/systemd/user/`，再 `daemon-reload`，否则重载的是旧文件。
   > 实测：`FragmentPath=/home/jiuben/.config/systemd/user/obsidian-sync.*`，且已装文件仍为 `PathModified` + 含 `data/plan_e`/`training/eval` → 证明修复**尚未部署**。

2. **`daemon-reload` 不会重新挂载 `.path` 单元的 inotify 监视。**
   即使重载成功，旧 `.path` 仍在用旧监视列表，必须 `systemctl --user restart obsidian-sync.path` 才会用新 `PathChanged` 重建监视。

3. **`start-limit-hit` 状态必须用 `reset-failed` 清除。**
   单元处于 `failed`/`start-limit-hit` 时，新启动请求仍可能被旧计数阻塞，重跑前先 `reset-failed`。

4. **`PathChanged` 仅在文件"内容"变化时触发；`touch`（只改 mtime）不会触发。**
   触发测试必须**写内容**（如 `>>` 追加 / 新建带内容的文件），不能只用 `touch`。

---

## 1. 前置条件

- 与部署时同一用户会话执行（`systemctl --user` 需要 `XDG_RUNTIME_DIR` 与用户 bus）。
- 仓库位于修复提交之后（`git log --oneline -1` 应 ≥ `0b4c5c8`）。
- `~/.config/systemd/user/` 可写，且 `make` 可用。
- 若远程/无图形会话出现 `Failed to connect to bus`：`export XDG_RUNTIME_DIR=/run/user/$(id -u)` 后重试。

---

## 2. 重载前状态检查（基线采集，只读）

> 以下为**当前实测基线**（重载前应能复现）。凡不符即说明环境已漂移，先排查再继续。

| 步骤 | 命令（只读） | 预期输出（当前实测） | 判定标准 |
|---|---|---|---|
| B1 | `systemctl --user show obsidian-sync.service -p FragmentPath -p StartLimitIntervalSec -p StartLimitBurst` | `FragmentPath=/home/jiuben/.config/systemd/user/obsidian-sync.service`、`StartLimitIntervalSec=10000`、`StartLimitBurst=5` | 确认加载的是 `~/.config` 下文件，且仍是**旧默认值**（10s/5） |
| B2 | `systemctl --user show obsidian-sync.path -p FragmentPath -p Paths` | `FragmentPath=.../obsidian-sync.path`；`Paths=` 含 `data/plan_e (PathModified)`、`training/eval (PathModified)` 及 `docs/README.md/DELIVERY.md/CHANGELOG.md/Makefile (PathModified)` | 确认旧监听列表（含两个数据目录、且是 `PathModified`） |
| B3 | `systemctl --user show obsidian-sync.service obsidian-sync.path -p ActiveState -p SubState -p Result` | 两者 `ActiveState=failed`、`Result=start-limit-hit`（path 为 `unit-start-limit-hit`） | 确认两单元当前处于失败/限流态 |
| B4 | `systemctl --user status obsidian-sync.path obsidian-sync.service --no-pager` | path/service 均 `failed`，service `Result: start-limit-hit`，`TriggeredBy: obsidian-sync.path` | 人工复核状态文字与 B3 一致 |
| B5（可选） | `make status` / `make canary` | 记录其余 timer（tdxfeed-sync/predict-daily/plan-e-*/canary-health）状态与 canary 通过项数（基线） | 作为回归对照，重载后不应劣化 |

---

## 3. 重载命令的准确执行（变更步骤）

按顺序执行，**任一步失败即停止排查**，不要继续。

| 步骤 | 命令 | 预期输出 / 退出码 | 判定标准 |
|---|---|---|---|
| 1 部署 | `make timer-install` | 打印 `▶ 复制 timer/service/path 到 user systemd`，无报错，`echo $?`=0 | 已将仓库 `systemd/obsidian-sync.{path,service}` 复制到 `~/.config/systemd/user/`（`--remove-destination` 覆盖旧文件） |
| 1' 校验部署 | `diff systemd/obsidian-sync.path ~/.config/systemd/user/obsidian-sync.path && diff systemd/obsidian-sync.service ~/.config/systemd/user/obsidian-sync.service` | 无差异输出，`echo $?`=0 | 确认部署文件与仓库修复版**逐字节一致** |
| 2 重载 | `systemctl --user daemon-reload` | 无输出，`echo $?`=0 | 管理器已重读全部单元文件（含新的 StartLimit 与 PathChanged） |
| 3 清限流 | `systemctl --user reset-failed obsidian-sync.service obsidian-sync.path` | 无输出，`echo $?`=0 | 清除 `start-limit-hit` 计数，允许后续启动 |
| 4 重挂监视 | `systemctl --user restart obsidian-sync.path` | 无输出，`echo $?`=0 | 重建 `.path` 的 inotify 监视（用新 `PathChanged` 列表）；path 单元重新 `active` |

> 说明：`make timer-enable` = `timer-install`+`daemon-reload`+`enable`（会顺带 enable 全部 timer）。为保持范围最小、避免改动其他单元，本方案用分步 1→4；如需一键可改用 `make timer-enable` 后补 `reset-failed`+`restart obsidian-sync.path`。

---

## 4. 重载后验证方法（核心判定）

| 步骤 | 命令 | 预期输出（目标值） | 判定标准（PASS 条件） |
|---|---|---|---|
| V1 StartLimit 生效 | `systemctl --user show obsidian-sync.service -p StartLimitIntervalSec -p StartLimitBurst` | `StartLimitIntervalSec=60000`、`StartLimitBurst=10` | 两项均匹配 → **重载已生效（服务侧）** |
| V2 监听列表生效 | `systemctl --user show obsidian-sync.path -p Paths` | 仅 5 条 `PathChanged`：`docs`/`README.md`/`DELIVERY.md`/`CHANGELOG.md`/`Makefile`；**不含** `data/plan_e`、`training/eval` | 确认解耦 + `PathChanged` 语义 → **重载已生效（路径侧）** |
| V3 path 状态 | `systemctl --user status obsidian-sync.path --no-pager` | `Active: active (waiting)`，`Result` 为空，无 `failed` | 监视单元健康运行 |
| V4 service 状态 | `systemctl --user status obsidian-sync.service --no-pager` | `Active: inactive (dead)`（oneshot 未运行）、`Result` 为空（非 `start-limit-hit`）、`TriggeredBy: obsidian-sync.path` | 不再限流，待触发 |
| V5 正向触发测试 | 先 `printf '\n# reload-verify %s\n' "$(date +%s)" >> README.md`（写内容，非 touch）；`sleep 3`；`systemctl --user status obsidian-sync.service --no-pager` 与 `journalctl --user -u obsidian-sync.service -n 5 --no-pager` | service 近期出现 `Starting … Finished`，`logs/obsidian-sync.log` 时间戳刷新 | 同步被触发且**恰好运行一次**（无风暴） |
| V6 解耦反向测试 | `echo reload-verify > data/plan_e/_reload_verify_tmp && sleep 5; rm -f data/plan_e/_reload_verify_tmp` | `logs/obsidian-sync.log` 时间戳**不变**、`journalctl -u obsidian-sync.service` 无新条目 | 写入数据目录**不再触发**同步（解耦有效） |
| V7 冷却/锁防风暴 | 在 30s 内对 `README.md` 连续写内容 2 次；`journalctl --user -u obsidian-sync.service -n 20 --no-pager` 统计 | 该窗口内最多 1 次有效运行（flock 或 30s 冷却吸收并发） | 自激/叠加被抑制 |
| V8 回归 | `make canary`（对比 B5 基线）、`make status` | canary 通过项数与 B5 一致；其余 timer 仍 `active` | 整体健康未因重载劣化 |

> 反向测试临时文件务必清理（`rm -f data/plan_e/_reload_verify_tmp`），避免污染数据目录（即便不会触发同步）。

---

## 5. 异常情况与排查

| 异常现象 | 可能原因 | 排查命令 | 解决 |
|---|---|---|---|
| V1/V2 仍为旧值（10s/5、含 `data/plan_e`） | 未部署，旧文件仍在 `~/.config/systemd/user/` | `diff systemd/obsidian-sync.path ~/.config/systemd/user/obsidian-sync.path`；`systemctl --user show -p FragmentPath` | 重跑第 1 步 `make timer-install`，确认覆盖；再 `daemon-reload` |
| V4 仍 `start-limit-hit` | 未 `reset-failed` 或未 `restart obsidian-sync.path` | `systemctl --user show -p Result`；`journalctl -u obsidian-sync.service -n` | 执行第 3、4 步；检查 `logs/obsidian-sync.err` 是否有脚本报错 |
| `restart obsidian-sync.path` 后立刻 `failed`/`start-limit` | 触发风暴未平息或同步脚本本身失败 | `journalctl --user -u obsidian-sync.path -n`；`journalctl -u obsidian-sync.service -n`；`cat logs/obsidian-sync.err` | 确认新 `StartLimitBurst=10` 已生效（V1）；若脚本报错先修脚本 |
| V5 写内容后仍不触发 | `PathChanged` 语义 + path 未 `active` / 写错文件 | `systemctl --user show obsidian-sync.path -p ActiveState -p Paths`；`journalctl -u obsidian-sync.path -n` | 确认对 V2 列表中的文件**写内容**；确认 path `active (waiting)` |
| V6 写入 `data/plan_e` 仍触发 | 解耦未生效（仍是旧 `PathModified` 列表） | `systemctl --user show obsidian-sync.path -p Paths` | 重做部署 + `daemon-reload` + `restart obsidian-sync.path` |
| `Failed to connect to bus` | 无用户会话 / `XDG_RUNTIME_DIR` 未设 | `echo $XDG_RUNTIME_DIR` | `export XDG_RUNTIME_DIR=/run/user/$(id -u)` 后重试 |
| `cp` 写入 `~/.config/systemd/user` 权限拒绝 | 非同用户或目录不可写 | `ls -ld ~/.config/systemd/user`；`whoami` | 用正确用户执行，确保目录归属当前用户 |
| 触发后 service 退出非 0 | 同步脚本逻辑错误 | `cat logs/obsidian-sync.err`；`journalctl -u obsidian-sync.service -n` | 修复 `scripts/sync-to-obsidian.sh` 后重跑第 1 步部署 |

---

## 6. 回滚方案

若新配置引发异常，回退到修复前版本（保留 git 历史，不手动删仓库文件）：

```bash
# 取修复提交前的单元文件（0b4c5c8 的上一个版本）
git show 0b4c5c8^:systemd/obsidian-sync.path   > systemd/obsidian-sync.path
git show 0b4c5c8^:systemd/obsidian-sync.service > systemd/obsidian-sync.service
# 重新部署并生效
make timer-install
systemctl --user daemon-reload
systemctl --user reset-failed obsidian-sync.service obsidian-sync.path
systemctl --user restart obsidian-sync.path
```

回滚后旧文件内容为 `PathModified` + 含 `data/plan_e`/`training/eval`（即回到重启风暴前状态），按第 4 节方法复测确认。

---

## 7. 验证结论判定清单（最终确认）

全部满足即判定"重载成功生效"：

- [ ] B1–B5 基线采集完成，环境符合预期（旧配置、限流态）
- [ ] 第 1–4 步执行无报错，退出码均为 0
- [ ] **V1**：`StartLimitIntervalSec=60000` 且 `StartLimitBurst=10`
- [ ] **V2**：`Paths` 仅 5 条 `PathChanged`，不含 `data/plan_e`/`training/eval`
- [ ] **V3/V4**：path `active (waiting)`，service 不再 `start-limit-hit`
- [ ] **V5**：写内容后同步被触发且恰好运行一次
- [ ] **V6**：写 `data/plan_e` 不再触发同步
- [ ] **V7**：短时多次触发被冷却/锁抑制，无风暴
- [ ] **V8**：`make canary` / `make status` 与基线一致，其余 timer 正常

---

## ⚠️ 风险提示

1. 本方案只动 `obsidian-sync` 两个用户级单元；`daemon-reload` 会重读全部 user 单元，但配置未变者行为不变。
2. 反向测试向 `data/plan_e` 写入的临时文件**必须清理**，避免污染数据目录（虽然不会触发同步）。
3. `restart obsidian-sync.path` 会短暂移除再重建 inotify 监视，期间（秒级）的文档变更可能漏触发一次；建议在非高频写入时段执行。
4. 不要手动 `rm` 仓库中的修复文件来"回滚"——用第 6 节的 git 方式，保证可追踪。
5. 历史回测不代表未来收益（通用提示，与本运维无关，仅按报告规范保留）。

## 🔧 下一步建议

1. 将"obsidian-sync 是否 `start-limit-hit` / `Paths` 是否含数据目录"纳入 `make status` 或 `canary` 的常规检查，形成持续监控。
2. 在 `Makefile` 或部署文档中明确"改 `systemd/` 后必须 `make timer-install`"，防止仓库与部署漂移。
3. 验证通过后，把本方案 V1–V8 的判定固化成一个轻量脚本（如 `scripts/verify_obsidian_systemd.sh`），便于日后复跑。

---
生成时间：2026-10-12 18:40:00
生成者：tdx 运维验证方案（agent）
模型版本：—
数据日期：2026-10-12
