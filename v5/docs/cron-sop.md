# Cron 接入 SOP

> 用途：把定时任务（数据采集 / 调度 / 验证）接入系统 cron 的标准操作流程
> 维护者：jiuben
> 创建：2026-09-25
> 相关：v5/scripts/scheduler_agent.py, scripts/watch_eastmoney.sh, v5/tests/test_alerting_pipeline.sh

## 当前 cron 任务（基线）

```bash
$ crontab -l
*/30 * * * * bash /home/jiuben/tdx-data-feed/scripts/watch_eastmoney.sh
*/5  * * * * cd /home/jiuben/tdx-data-feed && venv/bin/python v5/scripts/scheduler_agent.py --once
```

| 任务 | 频率 | 脚本 | 作用 |
|---|---|---|---|
| `watch_eastmoney` | 30 分钟 | `scripts/watch_eastmoney.sh` | 监控东方财富数据变化（市场数据采集）|
| `scheduler_agent` | 5 分钟 | `v5/scripts/scheduler_agent.py --once` | tdx-data-feed 内部调度（audit / suggestion / canary）|

## 添加新 cron 任务的 SOP

### 步骤 1：写 wrapper script

把核心逻辑写到 `v5/scripts/<name>.sh`，**不要**直接在 crontab 里写复杂命令。

```bash
#!/usr/bin/env bash
# v5/scripts/my_task.sh - 我的新任务
# 用途: ...
# 日志: /var/log/tdx-data-feed/my_task.log (或 /tmp/<name>.log)
# 频率: 5min/30min/hourly/...
set -uo pipefail

PROJ=/home/jiuben/tdx-data-feed
VENV=$PROJ/venv
LOG=/tmp/my_task.log
TS=$(date '+%Y-%m-%d %H:%M:%S')

echo "=== [$TS] start ===" >> "$LOG"
cd "$PROJ"
$VENV/bin/python v5/scripts/my_task.py >> "$LOG" 2>&1
RC=$?
echo "=== [$TS] end rc=$RC ===" >> "$LOG"
exit "$RC"
```

要点：
- `set -uo pipefail`（不要 `-e`，单个 fail 不应该停整个 cron）
- **绝对路径**（cron 跑时无 $PATH，依赖手动指定）
- 写日志（cron 默认 stdout 是邮件给 root，**邮件发不出去反而坑**）
- 写时间戳 + RC（便于事后查问题）

### 步骤 2：chmod +x + 手动跑一次验证

```bash
chmod +x v5/scripts/my_task.sh
bash v5/scripts/my_task.sh        # 手动跑一次, 看 log
cat /tmp/my_task.log | tail -20   # 看输出
```

### 步骤 3：crontab -e 加任务

```bash
crontab -e
```

加一行（按需替换频率）：

```cron
*/5 * * * * bash /home/jiuben/tdx-data-feed/v5/scripts/my_task.sh
```

或者每周一跑链路验证 SOP：

```cron
0 9 * * 1 bash /home/jiuben/tdx-data-feed/v5/scripts/sop-weekly.sh
```

（配套脚本见 `v5/scripts/sop-weekly.sh`）

### 步骤 4：等下一次触发后查日志

```bash
sleep 300  # 等一个周期
tail -30 /tmp/my_task.log
```

### 步骤 5：撤掉任务

```bash
crontab -e
# 在对应行前加 # 注释, 或删除整行
```

或者更稳的：

```bash
crontab -l | grep -v my_task > /tmp/cron.new
crontab /tmp/cron.new
rm /tmp/cron.new
```

## 权限要求

- `crontab -e` 不需要 sudo（用户级别 crontab）
- 但**用户 crontab**只能跑用户权限，**不能访问 root-only 文件**
- 如果要 root cron：`sudo crontab -e`

**关键约束**：
- 涉及 `/etc/systemd/`, `/etc/alertmanager/`, `/etc/prometheus/` 等 root 文件的脚本，**用户 cron 跑会失败**
- 这类任务用 **systemd timer**（不是 cron）：见 `/etc/systemd/system/*.timer`

## 日志管理

### 默认日志路径

| 脚本 | 日志 |
|---|---|
| `watch_eastmoney.sh` | 自管（脚本内）|
| `scheduler_agent.py` | 自管 |
| `sop-weekly.sh` | `/tmp/sop-weekly.log` |

### 防止日志无限增长

```bash
# /etc/logrotate.d/tdx-data-feed （需要 sudo）
/tmp/sop-weekly.log /tmp/my_task.log {
    daily
    rotate 7
    compress
    missingok
    notifempty
}
```

或者在 wrapper script 里用 `truncate` 保留最后 N 行：

```bash
# 保留最后 1000 行
tail -1000 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
```

## 故障排查

| 症状 | 排查 |
|---|---|
| cron 不跑 | `grep CRON /var/log/syslog`（需要 sudo）|
| 任务跑但无输出 | `tail -50 /tmp/my_task.log`，看是否真的写了 |
| 任务跑但失败 | 脚本里加 `RC=$?` 记录 exit code |
| 时区错乱 | `crontab` 用系统时区；`date` 输出确认 |
| 路径不对 | 脚本里**绝对路径**一切，别依赖 $PATH |

## 推荐标准（写入所有 wrapper）

```bash
#!/usr/bin/env bash
set -uo pipefail     # 不带 -e
PROJ=/home/jiuben/tdx-data-feed
VENV=$PROJ/venv
LOG=/tmp/$(basename "$0" .sh).log
TS=$(date '+%Y-%m-%d %H:%M:%S')

echo "=== [$TS] start ===" >> "$LOG"
cd "$PROJ"
"$VENV/bin/python" "$PROJ/v5/scripts/my_task.py" >> "$LOG" 2>&1
RC=$?
echo "=== [$TS] end rc=$RC ===" >> "$LOG"
exit "$RC"
```

## 接入新任务的 Checklist

- [ ] wrapper script 写到 `v5/scripts/`
- [ ] chmod +x
- [ ] 手动跑一次，验证输出
- [ ] crontab -e 加任务
- [ ] 等 1 个周期后看日志
- [ ] 在 `v5/audit/` 或 README 里登记这个任务

## 范例：每周告警链路验证

`v5/scripts/sop-weekly.sh`（已实现）：

```bash
#!/usr/bin/env bash
# 每周一 09:00 跑告警链路 quick 验证, 失败发邮件 (TODO)
set -uo pipefail
PROJ=/home/jiuben/tdx-data-feed
LOG=/tmp/sop-weekly.log
TS=$(date '+%Y-%m-%d %H:%M:%S')

echo "=== [$TS] sop-weekly start ===" >> "$LOG"
bash "$PROJ/v5/tests/test_alerting_pipeline.sh" --report-json /tmp/sop-weekly-report.json >> "$LOG" 2>&1
RC=$?
echo "=== [$TS] sop-weekly end rc=$RC ===" >> "$LOG"
exit "$RC"
```

crontab：

```cron
0 9 * * 1 bash /home/jiuben/tdx-data-feed/v5/scripts/sop-weekly.sh
```