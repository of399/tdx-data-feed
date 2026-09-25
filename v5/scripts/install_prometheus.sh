#!/usr/bin/env bash
# install_prometheus.sh · W4 Prometheus + Alertmanager 一键部署
#
# Ubuntu 默认 apt 仓库没有 alertmanager 包，需从 GitHub release 下载二进制
#
# 用法（需 sudo 单独执行）：
#   sudo apt update && sudo apt install -y prometheus prometheus-node-exporter
#   sudo bash v5/scripts/install_prometheus.sh
#
# 步骤：
#   1. apt install prometheus prometheus-node-exporter（apt）
#   2. 从 GitHub release 下载 alertmanager-0.34.1.linux-amd64.tar.gz
#   3. 解压到 /usr/local/bin/alertmanager
#   4. 创建 alertmanager 用户 + /var/lib/alertmanager 目录
#   5. 复制 alertmanager.service + alertmanager.default 到 /etc/
#   6. 复制 prometheus 配置 + alertmanager 配置
#   7. 创建 textfile collector 目录
#   8. daemon-reload + restart 所有服务
#   9. 验证 web UI + scrape

set -euo pipefail

cd "$(dirname "$(dirname "$0")")"
V5_BASE=$(pwd)
ROOT_DIR="$(dirname "$V5_BASE")"
V5_DIR=$(pwd)
SYSTEMD_DIR="$V5_DIR/systemd"
MONITOR_DIR="$V5_DIR/monitoring"

ALERTMANAGER_VERSION="0.34.1"
# GitHub 官方地址（清华镜像备用）
ALERTMANAGER_GH_URL="https://github.com/prometheus/alertmanager/releases/download/v${ALERTMANAGER_VERSION}/alertmanager-${ALERTMANAGER_VERSION}.linux-amd64.tar.gz"
ALERTMANAGER_TSINGHUA_URL="https://mirrors.tuna.tsinghua.edu.cn/github-release/prometheus/alertmanager/v${ALERTMANAGER_VERSION}/alertmanager-${ALERTMANAGER_VERSION}.linux-amd64.tar.gz"

echo "===== W4 Prometheus + Alertmanager 一键部署 ====="
echo "v5 目录: $V5_DIR"
echo "alertmanager version: $ALERTMANAGER_VERSION"
echo

# 0. 检查 sudo 是否可用
echo "[0/5] 检查环境..."
if ! sudo -n true 2>/dev/null; then
    echo "  ❌ sudo 不可用或需密码，请用 sudo 执行本脚本"
    exit 1
fi

# 1. apt 包检查
echo "[1/5] 检查 apt 包..."
for pkg in prometheus prometheus-node-exporter; do
    if ! dpkg -l "$pkg" 2>/dev/null | grep -q "^ii"; then
        echo "  ❌ $pkg 未装，请先执行："
        echo "     sudo apt install -y $pkg"
        exit 1
    fi
    echo "  ✓ $pkg 已装"
done

# 检查 alertmanager 二进制
if [ -x "/usr/local/bin/alertmanager" ]; then
    AM_VER=$(/usr/local/bin/alertmanager --version 2>&1 | head -1 | awk '{print $NF}')
    echo "  ✓ alertmanager 二进制已装（v${AM_VER}）"
else
    echo "  → 下载 alertmanager v${ALERTMANAGER_VERSION} 二进制（GitHub release ~37MB）..."

    TMPDIR=$(mktemp -d)
    cd "$TMPDIR"
    DOWNLOAD_OK=false

    # 优先 GitHub，3 分钟超时（GitHub 慢但可达）
    if curl -fSL --max-time 180 --retry 3 --retry-delay 5 -o am.tar.gz "$ALERTMANAGER_GH_URL" 2>/dev/null && [ -s am.tar.gz ]; then
        SIZE=$(stat -c '%s' am.tar.gz)
        if [ "$SIZE" -gt 30000000 ]; then
            echo "    ✓ 从 GitHub 下载成功（${SIZE} bytes）"
            DOWNLOAD_OK=true
        fi
    fi

    # Fallback 1: 清华镜像
    if [ "$DOWNLOAD_OK" = false ]; then
        rm -f am.tar.gz
        if curl -fSL --max-time 60 -o am.tar.gz "$ALERTMANAGER_TSINGHUA_URL" 2>/dev/null && [ -s am.tar.gz ]; then
            SIZE=$(stat -c '%s' am.tar.gz)
            if [ "$SIZE" -gt 30000000 ]; then
                echo "    ✓ 从清华镜像下载成功（${SIZE} bytes）"
                DOWNLOAD_OK=true
            fi
        fi
    fi

    if [ "$DOWNLOAD_OK" = true ]; then
        tar xzf am.tar.gz
        sudo mv "alertmanager-${ALERTMANAGER_VERSION}.linux-amd64/alertmanager" /usr/local/bin/
        sudo mv "alertmanager-${ALERTMANAGER_VERSION}.linux-amd64/amtool" /usr/local/bin/ 2>/dev/null || true
        sudo chmod +x /usr/local/bin/alertmanager
        AM_VER=$(/usr/local/bin/alertmanager --version 2>&1 | head -1 | awk '{print $NF}')
        echo "  ✓ alertmanager 安装完成（v${AM_VER}）"
    else
        echo "  ⚠️  alertmanager 下载失败（GitHub 慢或镜像 404）"
        echo "    继续部署 prometheus（不依赖 alertmanager）"
        echo "    手动重试: curl -fSL --max-time 600 -o /tmp/am.tar.gz $ALERTMANAGER_GH_URL"
        echo "    解压: tar xzf /tmp/am.tar.gz && sudo mv alertmanager-*/alertmanager /usr/local/bin/"
        echo "    然后重跑本脚本第 5 步: sudo systemctl daemon-reload && sudo systemctl enable --now alertmanager"
    fi
    cd "$V5_DIR"
    rm -rf "$TMPDIR"
fi
echo

# 2. 创建 alertmanager 用户 + 目录
echo "[2/5] 创建 alertmanager 用户..."
if ! id -u alertmanager >/dev/null 2>&1; then
    sudo useradd -r -s /bin/false -d /var/lib/alertmanager alertmanager
    echo "  ✓ alertmanager 用户已创建"
else
    echo "  ✓ alertmanager 用户已存在"
fi
sudo mkdir -p /var/lib/alertmanager /etc/alertmanager
sudo chown alertmanager:alertmanager /var/lib/alertmanager
echo

# 3. 复制 systemd unit + 配置
echo "[3/5] 复制 systemd unit + 监控配置..."
sudo cp "$SYSTEMD_DIR/alertmanager.service" /etc/systemd/system/alertmanager.service
sudo cp "$SYSTEMD_DIR/alertmanager.default" /etc/default/alertmanager
sudo cp "$MONITOR_DIR/prometheus.yml" /etc/prometheus/prometheus.yml
sudo cp "$MONITOR_DIR/prometheus-alerts.yml" /etc/prometheus/prometheus-alerts.yml
sudo cp "$MONITOR_DIR/alertmanager.yml" /etc/alertmanager/alertmanager.yml
sudo chmod 644 /etc/systemd/system/alertmanager.service /etc/default/alertmanager
sudo chmod 644 /etc/prometheus/prometheus.yml /etc/prometheus/prometheus-alerts.yml
sudo chmod 644 /etc/alertmanager/alertmanager.yml
sudo chown alertmanager:alertmanager /etc/alertmanager/alertmanager.yml
echo "  ✓ /etc/systemd/system/alertmanager.service"
echo "  ✓ /etc/default/alertmanager"
echo "  ✓ /etc/prometheus/prometheus.yml"
echo "  ✓ /etc/prometheus/prometheus-alerts.yml"
echo "  ✓ /etc/alertmanager/alertmanager.yml"
echo "  ⚠️  提醒：在 /etc/alertmanager/alertmanager.yml 第 58 行替换 Slack webhook URL"
echo

# 4. 创建 textfile collector 目录 + 启用 node-exporter textfile collector
echo "[4/5] 创建 Prometheus textfile collector 目录 + 启用 node-exporter textfile collector..."
sudo mkdir -p /var/lib/prometheus/node-exporter
sudo chown prometheus:prometheus /var/lib/prometheus/node-exporter
# 让 jiuben 用户可写 textfile（tdx_monitor.service 用 jiuben 跑）
sudo chmod 775 /var/lib/prometheus/node-exporter
sudo usermod -aG prometheus jiuben 2>/dev/null || true
echo "  ✓ /var/lib/prometheus/node-exporter/ (prometheus:prometheus 775 + jiuben 在 group)"
echo "  ✓ jiuben 已加入 prometheus group（可写 textfile）"

# 启用 node-exporter textfile collector（关键，否则 tdx_monitor.prom 不会被 prometheus 抓到）
NE_DEFAULT=/etc/default/prometheus-node-exporter
if [ -f "$NE_DEFAULT" ]; then
    if ! grep -q "collector.textfile.directory" "$NE_DEFAULT"; then
        sudo sed -i 's|^ARGS=""|ARGS="--collector.textfile.directory=/var/lib/prometheus/node-exporter"|' "$NE_DEFAULT"
        echo "  ✓ node-exporter ARGS 加 textfile collector"
    else
        echo "  ✓ node-exporter ARGS 已含 textfile collector"
    fi
fi
echo

# 5. daemon-reload + restart + 验证
echo "[5/5] daemon-reload + restart + 验证..."
sudo systemctl daemon-reload

# prometheus + node-exporter 一定装上（apt），alertmanager 可能没装（二进制）
if [ -x "/usr/local/bin/alertmanager" ]; then
    sudo systemctl enable --now prometheus prometheus-node-exporter alertmanager
    AM_STATUS="alertmanager"
else
    sudo systemctl enable --now prometheus prometheus-node-exporter
    AM_STATUS="alertmanager (未装，二进制下载失败，请手动补)"
fi
sleep 3

echo "  服务状态:"
for svc in prometheus prometheus-node-exporter; do
    ACTIVE=$(sudo systemctl is-active $svc 2>&1)
    echo "    $svc: $ACTIVE"
done
echo "    $AM_STATUS"

echo
echo "===== 验证 ====="
echo "prometheus :9090 healthy:"
curl -fsS --max-time 5 http://127.0.0.1:9090/-/healthy && echo " ✓" || echo " ❌"
if [ -x "/usr/local/bin/alertmanager" ]; then
    echo "alertmanager :9093 healthy:"
    curl -fsS --max-time 5 http://127.0.0.1:9093/-/healthy && echo " ✓" || echo " ❌"
fi
echo "node-exporter :9100 metrics (前 3 行):"
curl -fsS --max-time 5 http://127.0.0.1:9100/metrics 2>/dev/null | head -3 || echo " ❌"

echo
echo "===== Web UI 入口 ====="
echo "  Prometheus   http://127.0.0.1:9090"
echo "  Alertmanager http://127.0.0.1:9093"

echo
echo "===== 下一步 ====="
echo "  1. 在 prometheus UI 点 Status → Targets 应看到 5 个 job"
echo "  2. tdx-monitor.timer 每 5min 写 textfile → tdx_* metrics 应出现"
echo "  3. 等用户贴 Slack webhook URL 后填 /etc/alertmanager/alertmanager.yml 第 58 行"
echo "  4. 重启 alertmanager: sudo systemctl restart alertmanager"
echo "  5. 触发告警测试: sudo systemctl stop math-sympy-http（UpstreamDown 应触发）"