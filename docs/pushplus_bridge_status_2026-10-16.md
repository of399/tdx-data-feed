# pushplus-bridge 退役记录

> 状态：**已停用（disabled + stopped，可逆）**
> 生效时间：2026-10-16 16:08
> 退役类型：**可逆停用**（保留所有文件，仅关闭运行时）
> 审批人：项目所有者（jiuben）

---

## 1. 停用原因

| # | 原因 | 证据 |
|---|---|---|
| 1 | **零告警流量** | 7 天内 bridge 日志仅 1 条 `GET /health`，无 `POST /alert` |
| 2 | **无生产调用方** | `scripts/canary_health_check.py` / `scripts/monitor_plan_e_live.py` / `scripts/` 其它近期脚本均 0 处引用 pushplus/5002 |
| 3 | **告警链是"写日志"不是"推消息"** | canary → `logs/canary.log`；plan-e-monitor → `logs/plan-e-monitor.out`，人巡检/手动 `make canary` |
| 4 | **空转资源** | 1 进程 / ~30MB 内存 / 1 端口监听，无任何产出 |
| 5 | **第三方面依赖未启用** | PushPlus token 已配但从未被使用；保留但 = dead token |

---

## 2. 已执行的动作

```bash
# 停用 + 关闭自启 + 停止服务（保留文件可回滚）
systemctl --user disable --now pushplus-bridge.service
# 结果：
#   - 服务状态 active → inactive
#   - 端口 127.0.0.1:5002 关闭
#   - 移除 default.target.wants/pushplus-bridge.service 软链
#   - 3 个文件原样保留
```

**保留文件**（供回滚）：
| 文件 | 大小 | 用途 |
|---|---|---|
| `systemd/pushplus-bridge.service` | 791B | systemd 单元定义 |
| `v5/scripts/pushplus_bridge.py` | 5.4KB | HTTP→PushPlus 桥脚本 |
| `v5/scripts/.pushplus.env` | 48B | PushPlus token（**敏感**，权限 600） |

**未删除** `systemd/pushplus-bridge.service` / `v5/scripts/pushplus_bridge.py`，因为它们是 git tracked 文件，删除会污染工作树。

---

## 3. 影响范围

### 3.1 不受影响（继续正常运行）
- 全部其它 timer / path / service 单元（除 pushplus-bridge 自身）
- 全部数据采集 / 量化 / 训练 / 部署 / 监控链路
- `canary-health` 5min 巡检 → 仍写 `logs/canary.log`
- `plan-e-monitor` 跟踪 → 仍写 `logs/plan-e-monitor.out`
- Ollama 推理 / v7r 端点 / 预测 / 回测

### 3.2 受影响（已停止）
- **告警推送链路**：PushPlus 微信推送已停。FAIL/偏差不再自动推送手机，需主动巡检日志。

### 3.3 数据 / 配置 / 状态
- **无数据丢失**（bridge 不持久化任何业务数据）
- **配置完整保留**（token / 单元 / 脚本 / systemd 软链可恢复）
- **外部状态**（PushPlus 服务端）：无影响（我们仅是消费者，token 仍有效）

---

## 4. 替代方案（停用期间的告警获取方式）

| # | 方案 | 实施成本 | 适用场景 |
|---|---|---|---|
| **A1** | 定期手动 `make canary` / `tail -F logs/canary.log` | 0 | 单人/低频巡检 |
| **A2** | 写个本地轮询脚本：FAIL 时桌面通知（`notify-send`）/ 弹窗 | 1-2 小时 | 工作时段不离机 |
| **A3** | 邮件告警：`v5/scripts/smtp_setup.sh` 已存在，改造 canary FAIL 分支走 SMTP | 2-3 小时 | 不依赖第三方推送服务 |
| **A4** | Webhook 推钉钉/Slack/飞书机器人 | 2-3 小时 | 团队/多端接收 |
| **A5** | 完全无推送，靠 `journalctl --user -u canary-health` 查 | 0 | 极简模式 |

> 当前默认沿用 **A1**（最小变更、最贴合现有日志流）。若 FAIL 频率变高再升级到 A3/A4。

---

## 5. 重新启用条件与审批流程

### 5.1 启用条件（满足任一即可）

- **条件 1（推荐）**：实现 canary / plan-e-monitor → pushplus HTTP 推送的端到端链路
  - 任务：在 `scripts/canary_health_check.py` 异常分支与 `scripts/monitor_plan_e_live.py` 偏差告警分支各加 2-5 行 `requests.post("http://127.0.0.1:5002/alert", json={...})`
  - 验收：手动制造一次 canary FAIL（如断网 mock），手机微信收到推送
- **条件 2**：找到 / 实现其它明确调用方（不能是"以备不时之需"）
- **条件 3**：将告警链路整体迁移到其它通道（邮件/钉钉）后，bridge 作为冗余通道

### 5.2 审批流程

1. **提交方案**：在 issue/PR 或对话中说明启用理由 + 满足哪个条件 + 预计改动文件
2. **代码改动**：按方案修改并自测通过（本地 curl POST → 微信收到）
3. **审批**：项目所有者（jiuben）明确批准
4. **启用**：
   ```bash
   systemctl --user daemon-reload
   systemctl --user enable --now pushplus-bridge.service
   systemctl --user is-active pushplus-bridge.service   # 应为 active
   ss -ltn | grep :5002                                  # 应有 LISTEN
   curl -X POST http://127.0.0.1:5002/health            # 应返回 OK
   ```
5. **回写文档**：更新本文件第 1 节（状态从"已停用"改为"已启用"），记录启用日期与触发方

### 5.3 预计恢复时间

- **纯启用**（文件已保留、token 仍有效、无代码改动）：**5 分钟**
  - `daemon-reload` + `enable --now` + 健康检查
- **含代码改动**（条件 1，端到端联动）：**30-60 分钟**
  - canary/monitor 改造 15-30 分钟 + 自测 + 部署 + 验证

---

## 6. 时间线

| 时间 | 事件 |
|---|---|
| 2026-09-25 | pushplus_bridge.py 落地（v5 工作区） |
| 2026-09-26 | token `.pushplus.env` 配置（48B） |
| 2026-10-13 | systemd 单元 `pushplus-bridge.service` 创建并 enable |
| 2026-10-16 14:30 | v1 审查未发现此服务 |
| 2026-10-16 16:00 | v2 审查发现此服务但 0 调用方 |
| 2026-10-16 16:08 | **本记录生效**（disable + stop；文件保留） |

---

## 7. 相关文件

- 本记录：`docs/pushplus_bridge_status_2026-10-16.md`
- systemd 单元：`systemd/pushplus-bridge.service`
- 桥脚本：`v5/scripts/pushplus_bridge.py`
- token：`v5/scripts/.pushplus.env`（600 权限）
- 审查底稿：`docs/WORKFLOW_REVIEW_2026-10-16-v2.md` §5.1（已同步停用标注）
- WORKFLOW.md §7.5（已加停用标注）

---

> 本记录定位：**退役登记**（decommissioning record），用于回滚追溯与团队对齐。如需升级到 A2-A4 替代方案，请新建 ADR（架构决策记录）或修改本文件第 4 节。
