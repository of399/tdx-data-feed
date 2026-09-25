# Prometheus + Alertmanager 安装配置指南

## 1. 安装 Prometheus

```bash
# Ubuntu/Debian
sudo apt-get update
sudo apt-get install prometheus

# 或下载二进制
# https://prometheus.io/download/
sudo useradd -r -s /bin/false prometheus
sudo cp prometheus /usr/local/bin/
sudo cp promtool /usr/local/bin/
```

## 2. 复制配置

```bash
# 复制 alerts 配置到 prometheus 工作目录
PROMETHEUS_DIR=$(dirname $(readlink -f $(which prometheus)))/..
sudo cp /home/jiuben/tdx-data-feed/v5/monitoring/prometheus.yml $PROMETHEUS_DIR/prometheus.yml
sudo cp /home/jiuben/tdx-data-feed/v5/monitoring/prometheus-alerts.yml $PROMETHEUS_DIR/
sudo cp /home/jiuben/tdx-data-feed/v5/monitoring/alertmanager.yml /etc/alertmanager/alertmanager.yml

# 验证配置
promtool check config $PROMETHEUS_DIR/prometheus.yml
```

## 3. 启用 systemd

```bash
# prometheus 通常自带 systemd unit
sudo systemctl enable prometheus
sudo systemctl start prometheus
sudo systemctl status prometheus

# 验证 scrape
curl -s http://127.0.0.1:9090/api/v1/targets | python3 -m json.tool | head -20
```

## 4. 接入 Slack webhook

### 4.1 创建 Slack Incoming Webhook

1. 访问 https://api.slack.com/apps
2. 创建新 App → "From scratch" → 命名（如 "tdx-monitor"）→ 选 workspace
3. 左侧菜单 **"Incoming Webhooks"** → 开启
4. **"Add New Webhook to Workspace"** → 选 channel（如 `#alerts`）→ "Allow"
5. 复制 webhook URL（格式：`https://hooks.slack.com/services/T.../B.../XXX`）

### 4.2 填入 alertmanager.yml

```bash
# 编辑第 58 行（api_url）
sudo vi /etc/alertmanager/alertmanager.yml
```

找到 `slack_configs` 段：
```yaml
- name: 'slack-warnings'
  slack_configs:
    - api_url: 'https://hooks.slack.com/services/REPLACE_WITH_YOUR_TEAM/REPLACE_WITH_YOUR_CHANNEL/REPLACE_WITH_YOUR_TOKEN'
      channel: '#math-monitor-alerts'   # ← 改为你的 channel
```

替换 `REPLACE_WITH_YOUR_TEAM/CHANNEL/TOKEN` 为实际值。

### 4.3 重启 alertmanager

```bash
sudo systemctl restart alertmanager
sudo systemctl status alertmanager

# 验证配置
amtool check-config /etc/alertmanager/alertmanager.yml
```

### 4.4 测试告警

```bash
# 手动触发测试
curl -XPOST http://127.0.0.1:9093/api/v1/alerts \
  -H 'Content-Type: application/json' \
  -d '[{
    "labels": {"alertname":"TestAlert","severity":"warning"},
    "annotations": {
      "summary": "Test alert",
      "description": "测试告警是否到达 Slack"
    }
  }]'

# 检查 alertmanager 日志
sudo journalctl -u alertmanager -n 20
```

## 5. 安装 Grafana（可选，用于可视化）

```bash
sudo apt-get install grafana
sudo systemctl enable grafana-server
sudo systemctl start grafana-server
# 访问 http://localhost:3000 （默认 admin/admin）
```

Grafana 数据源：
- Type: Prometheus
- URL: http://localhost:9090

推荐 dashboard：
- 工具调用 rate / error rate
- Fallback 策略分布
- p50 / p95 latency
- cache hit ratio

## 6. 验证 scrape

```bash
# Prometheus target 状态
curl -s 'http://127.0.0.1:9090/api/v1/targets?state=active' | python3 -c "
import json,sys
d=json.load(sys.stdin)
for t in d['data']['activeTargets']:
    print(f\"{t['labels'].get('job','?'):<20}  {t['scrapeUrl']:<50}  health={t['health']}\")"

# Prometheus alerts
curl -s 'http://127.0.0.1:9090/api/v1/alerts' | python3 -m json.tool | head -30
```

## 7. 故障排查

**问题**：scrape 失败，显示 "context deadline exceeded"
**解决**：math-sympy-http 没运行或端口变了
```bash
curl -s http://127.0.0.1:8002/health  # 应返回 {"status":"ok"}
systemctl --user status math-sympy-http  # 或 sudo systemctl
```

**问题**：alertmanager 收不到 alert
**解决**：检查 `alerting.alertmanagers` 配置 + 网络可达
```bash
curl -s http://127.0.0.1:9093/-/healthy  # 应返回 "OK"
```

**问题**：Slack 没收到告警
**解决**：webhook URL 错误 / channel 不存在 / 权限
```bash
# 手工测试 webhook
curl -X POST 'YOUR_WEBHOOK_URL' \
  -H 'Content-Type: application/json' \
  -d '{"text":"测试告警 from tdx-monitor"}'
```