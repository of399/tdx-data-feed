#!/usr/bin/env bash
# install_grafana.sh · Grafana 一键安装脚本（v5 W6）
#
# Ubuntu 26.04 默认 apt 源没有 grafana，需要从官方源安装：
#   https://packages.grafana.com/oss/
#
# 本脚本会：
#   1. 添加 Grafana 官方 apt 源（含 GPG key）
#   2. apt update + install grafana
#   3. 启动 grafana-server + 开机自启
#   4. 复制 dashboard JSON 到 /var/lib/grafana/dashboards/ 自动 provision
#   5. 复制 datasources/provisioning.yml 自动配 Prometheus datasource
#
# 用法：
#   sudo bash install_grafana.sh
#
# 验证：
#   systemctl status grafana-server
#   curl -s http://127.0.0.1:3000/-/healthy
#   浏览器打开 http://127.0.0.1:3000  (admin/admin)

set -euo pipefail

if [ "$EUID" -ne 0 ]; then
    echo "⚠️  需要 sudo，请用：sudo bash $0"
    exit 1
fi

V5_DIR=/home/jiuben/tdx-data-feed/v5
GRAFANA_DASHBOARD_DIR=/var/lib/grafana/dashboards
GRAFANA_PROVISIONING_DIR=/etc/grafana/provisioning

echo "===== Grafana 一键安装脚本 ====="
echo "v5 目录: $V5_DIR"
echo

# 1. 添加 Grafana 官方 apt 源
echo "[1/5] 添加 Grafana 官方 apt 源..."
if [ ! -f /etc/apt/keyrings/grafana.gpg ] && [ ! -f /etc/apt/trusted.gpg.d/grafana.gpg ]; then
    mkdir -p /etc/apt/keyrings
    curl -fsSL https://apt.grafana.com/gpg.key | gpg --dearmor -o /etc/apt/keyrings/grafana.gpg
    echo "deb [signed-by=/etc/apt/keyrings/grafana.gpg] https://apt.grafana.com stable main" > /etc/apt/sources.list.d/grafana.list
else
    echo "  ✓ Grafana apt source 已存在"
fi
apt-get update -qq
echo

# 2. 安装 grafana
echo "[2/5] apt install grafana..."
if ! command -v grafana-server >/dev/null 2>&1; then
    DEBIAN_FRONTEND=noninteractive apt-get install -y grafana
else
    echo "  ✓ grafana 已装（$(grafana-server -v 2>&1 | head -1)）"
fi
echo

# 3. 配置 Prometheus datasource 自动 provision
echo "[3/5] 配置 Prometheus 自动 provision..."
mkdir -p "$GRAFANA_PROVISIONING_DIR/datasources" "$GRAFANA_PROVISIONING_DIR/dashboards" "$GRAFANA_DASHBOARD_DIR"

cat > "$GRAFANA_PROVISIONING_DIR/datasources/prometheus.yml" <<'EOF'
apiVersion: 1

datasources:
  - name: Prometheus
    type: prometheus
    access: proxy
    url: http://127.0.0.1:9090
    isDefault: true
    editable: true
    jsonData:
      timeInterval: "15s"
      httpMethod: POST
EOF

cat > "$GRAFANA_PROVISIONING_DIR/dashboards/tdx.yml" <<'EOF'
apiVersion: 1

providers:
  - name: 'tdx-dashboards'
    orgId: 1
    folder: 'TDX'
    type: file
    disableDeletion: false
    updateIntervalSeconds: 30
    allowUiUpdates: true
    options:
      path: /var/lib/grafana/dashboards
EOF

echo "  ✓ datasource: Prometheus (http://127.0.0.1:9090)"
echo "  ✓ dashboard path: $GRAFANA_DASHBOARD_DIR"

# 4. 复制 tdx_dashboard.json
echo "[4/5] 复制 dashboard JSON..."
if [ -f "$V5_DIR/monitoring/tdx_dashboard.json" ]; then
    cp "$V5_DIR/monitoring/tdx_dashboard.json" "$GRAFANA_DASHBOARD_DIR/tdx_overview.json"
    echo "  ✓ /var/lib/grafana/dashboards/tdx_overview.json"
else
    echo "  ⚠️  $V5_DIR/monitoring/tdx_dashboard.json 不存在，跳过"
fi

# 5. 重启 grafana
echo "[5/5] 重启 grafana-server..."
systemctl daemon-reload
systemctl enable --now grafana-server
sleep 3

echo
echo "===== 安装完成 ====="
echo "✓ Grafana 状态："
systemctl status grafana-server --no-pager | head -5
echo
echo "✓ 健康检查："
curl -sf http://127.0.0.1:3000/api/health 2>&1 | head -1
echo
echo "===== 访问 ====="
echo "  URL:      http://127.0.0.1:3000"
echo "  用户名:   admin"
echo "  初始密码: admin（首次登录强制修改）"
echo
echo "===== 验证 ====="
echo "  curl -s http://127.0.0.1:3000/api/health"
echo "  curl -s -u admin:admin http://127.0.0.1:3000/api/dashboards/home  # 看 dashboard 列表"