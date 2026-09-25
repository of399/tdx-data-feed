#!/usr/bin/env bash
# install_monitoring.sh · M3 W6 + M5 安装脚本
# 复制 systemd unit + 启用 fallback 日报 + audit rotate + M5 scheduler + evolution 日报定时器
#
# 支持两种安装模式：
#   1. 系统模式（需 sudo）：/etc/systemd/system/ - 开机自启
#   2. 用户模式（无需 sudo）：~/.config/systemd/user/ - 仅当前用户会话
#
# 默认尝试系统模式，失败则降级到用户模式

set -euo pipefail

cd "$(dirname "$(dirname "$0")")"
V5_BASE=$(pwd)
ROOT_DIR="$(dirname "$V5_BASE")"
V5_DIR=$(pwd)
V5_SUB="$V5_DIR/v5"
USER_MODE=false
SUDO="sudo -n"

echo "===== M3 W6 + M5 监控灰度/演化 安装 ====="
echo "v5 目录: $V5_DIR"
echo

# 1. 复制 systemd unit
echo "[1/5] 复制 systemd unit 文件..."

SERVICE_SRC="$V5_DIR/systemd/daily-fallback-report.service"
TIMER_SRC="$V5_DIR/systemd/daily-fallback-report.timer"
ROTATE_SERVICE_SRC="$V5_DIR/systemd/audit-log-rotate.service"
ROTATE_TIMER_SRC="$V5_DIR/systemd/audit-log-rotate.timer"
TDX_MONITOR_SERVICE="$V5_DIR/systemd/tdx-monitor.service"
TDX_MONITOR_TIMER="$V5_DIR/systemd/tdx-monitor.timer"
TDX_VERIFY_SERVICE="$V5_DIR/systemd/tdx-verification.service"
TDX_VERIFY_TIMER="$V5_DIR/systemd/tdx-verification.timer"
SCHEDULER_SERVICE="$V5_DIR/systemd/scheduler_agent.service"
SCHEDULER_TIMER="$V5_DIR/systemd/scheduler_agent.timer"
EVO_REPORT_SERVICE="$V5_DIR/systemd/evolution_daily_report.service"
EVO_REPORT_TIMER="$V5_DIR/systemd/evolution_daily_report.timer"
SUGGESTION_SERVICE="$V5_DIR/systemd/suggestion_agent.service"
SUGGESTION_TIMER="$V5_DIR/systemd/suggestion_agent.timer"
CANARY_SERVICE="$V5_DIR/systemd/canary_automation.service"
CANARY_TIMER="$V5_DIR/systemd/canary_automation.timer"
TDX_AUTONOMY_SERVICE="$V5_DIR/systemd/tdx-autonomy.service"
TDX_EVOLUTION_SERVICE="$V5_DIR/systemd/tdx-evolution.service"

if [ ! -f "$SERVICE_SRC" ] || [ ! -f "$TIMER_SRC" ]; then
    echo "❌ 找不到 systemd unit 源文件"
    echo "   期望: $SERVICE_SRC"
    echo "   期望: $TIMER_SRC"
    exit 1
fi

# audit-log-rotate unit 是可选的（fallback to 日报）
ROTATE_EXISTS=true
if [ ! -f "$ROTATE_SERVICE_SRC" ] || [ ! -f "$ROTATE_TIMER_SRC" ]; then
    echo "  ⚠️ audit-log-rotate unit 缺失（仅启用日报 timer）"
    ROTATE_EXISTS=false
fi

# 尝试系统模式（需 sudo -n 非交互）
if command -v sudo >/dev/null 2>&1; then
    if sudo -n true 2>/dev/null; then
        echo "  → 系统模式（/etc/systemd/system/）"
        sudo cp "$SERVICE_SRC" /etc/systemd/system/
        sudo cp "$TIMER_SRC" /etc/systemd/system/
        sudo chmod 644 /etc/systemd/system/daily-fallback-report.{service,timer}
        if [ "$ROTATE_EXISTS" = true ]; then
            sudo cp "$ROTATE_SERVICE_SRC" /etc/systemd/system/
            sudo cp "$ROTATE_TIMER_SRC" /etc/systemd/system/
            sudo chmod 644 /etc/systemd/system/audit-log-rotate.{service,timer}
        fi
        # tdx-monitor + tdx-verification timer（+ 4 个 tdx service）
        if [ -f "$TDX_MONITOR_SERVICE" ]; then
            sudo cp "$TDX_MONITOR_SERVICE" /etc/systemd/system/
            [ -f "$TDX_MONITOR_TIMER" ] && sudo cp "$TDX_MONITOR_TIMER" /etc/systemd/system/
            sudo chmod 644 /etc/systemd/system/tdx-monitor.{service,timer}
        fi
        if [ -f "$TDX_VERIFY_SERVICE" ]; then
            sudo cp "$TDX_VERIFY_SERVICE" /etc/systemd/system/
            [ -f "$TDX_VERIFY_TIMER" ] && sudo cp "$TDX_VERIFY_TIMER" /etc/systemd/system/
            sudo chmod 644 /etc/systemd/system/tdx-verification.{service,timer}
        fi
        if [ -f "$TDX_AUTONOMY_SERVICE" ]; then
            sudo cp "$TDX_AUTONOMY_SERVICE" /etc/systemd/system/
            sudo chmod 644 /etc/systemd/system/tdx-autonomy.service
        fi
        if [ -f "$TDX_EVOLUTION_SERVICE" ]; then
            sudo cp "$TDX_EVOLUTION_SERVICE" /etc/systemd/system/
            sudo chmod 644 /etc/systemd/system/tdx-evolution.service
        fi
        # M5 scheduler_agent + evolution_daily_report
        if [ -f "$SCHEDULER_SERVICE" ]; then
            sudo cp "$SCHEDULER_SERVICE" /etc/systemd/system/
            [ -f "$SCHEDULER_TIMER" ] && sudo cp "$SCHEDULER_TIMER" /etc/systemd/system/
            sudo chmod 644 /etc/systemd/system/scheduler_agent.{service,timer}
        fi
        if [ -f "$EVO_REPORT_SERVICE" ]; then
            sudo cp "$EVO_REPORT_SERVICE" /etc/systemd/system/
            [ -f "$EVO_REPORT_TIMER" ] && sudo cp "$EVO_REPORT_TIMER" /etc/systemd/system/
            sudo chmod 644 /etc/systemd/system/evolution_daily_report.{service,timer}
        fi
        if [ -f "$SUGGESTION_SERVICE" ]; then
            sudo cp "$SUGGESTION_SERVICE" /etc/systemd/system/
            [ -f "$SUGGESTION_TIMER" ] && sudo cp "$SUGGESTION_TIMER" /etc/systemd/system/
            sudo chmod 644 /etc/systemd/system/suggestion_agent.{service,timer}
        fi
        if [ -f "$CANARY_SERVICE" ]; then
            sudo cp "$CANARY_SERVICE" /etc/systemd/system/
            [ -f "$CANARY_TIMER" ] && sudo cp "$CANARY_TIMER" /etc/systemd/system/
            sudo chmod 644 /etc/systemd/system/canary_automation.{service,timer}
        fi
        sudo systemctl daemon-reload
        sudo systemctl enable daily-fallback-report.timer
        sudo systemctl start daily-fallback-report.timer
        if [ "$ROTATE_EXISTS" = true ]; then
            sudo systemctl enable audit-log-rotate.timer
            sudo systemctl start audit-log-rotate.timer
        fi
        if [ -f "$TDX_MONITOR_TIMER" ]; then
            sudo systemctl enable tdx-monitor.timer
            sudo systemctl start tdx-monitor.timer
        fi
        if [ -f "$TDX_VERIFY_TIMER" ]; then
            sudo systemctl enable tdx-verification.timer
            sudo systemctl start tdx-verification.timer
        fi
        if [ -f "$SCHEDULER_TIMER" ]; then
            sudo systemctl enable scheduler_agent.timer
            sudo systemctl start scheduler_agent.timer
        fi
        if [ -f "$EVO_REPORT_TIMER" ]; then
            sudo systemctl enable evolution_daily_report.timer
            sudo systemctl start evolution_daily_report.timer
        fi
        if [ -f "$SUGGESTION_TIMER" ]; then
            sudo systemctl enable suggestion_agent.timer
            sudo systemctl start suggestion_agent.timer
        fi
        if [ -f "$CANARY_TIMER" ]; then
            sudo systemctl enable canary_automation.timer
            sudo systemctl start canary_automation.timer
        fi
        INSTALL_MODE="system"
    else
        echo "  ⚠️ sudo 需要 tty 认证，降级到用户模式"
        INSTALL_MODE="user"
    fi
elif [ -w /etc/systemd/system/ ] 2>/dev/null; then
    echo "  → 系统模式（已是 root）"
    cp "$SERVICE_SRC" /etc/systemd/system/
    cp "$TIMER_SRC" /etc/systemd/system/
    chmod 644 /etc/systemd/system/daily-fallback-report.{service,timer}
    if [ "$ROTATE_EXISTS" = true ]; then
        cp "$ROTATE_SERVICE_SRC" /etc/systemd/system/
        cp "$ROTATE_TIMER_SRC" /etc/systemd/system/
        chmod 644 /etc/systemd/system/audit-log-rotate.{service,timer}
    fi
    if [ -f "$SCHEDULER_SERVICE" ]; then
        cp "$SCHEDULER_SERVICE" /etc/systemd/system/
        [ -f "$SCHEDULER_TIMER" ] && cp "$SCHEDULER_TIMER" /etc/systemd/system/
        chmod 644 /etc/systemd/system/scheduler_agent.{service,timer}
    fi
    if [ -f "$EVO_REPORT_SERVICE" ]; then
        cp "$EVO_REPORT_SERVICE" /etc/systemd/system/
        [ -f "$EVO_REPORT_TIMER" ] && cp "$EVO_REPORT_TIMER" /etc/systemd/system/
        chmod 644 /etc/systemd/system/evolution_daily_report.{service,timer}
    fi
    if [ -f "$SUGGESTION_SERVICE" ]; then
        cp "$SUGGESTION_SERVICE" /etc/systemd/system/
        [ -f "$SUGGESTION_TIMER" ] && cp "$SUGGESTION_TIMER" /etc/systemd/system/
        chmod 644 /etc/systemd/system/suggestion_agent.{service,timer}
    fi
    if [ -f "$CANARY_SERVICE" ]; then
        cp "$CANARY_SERVICE" /etc/systemd/system/
        [ -f "$CANARY_TIMER" ] && cp "$CANARY_TIMER" /etc/systemd/system/
        chmod 644 /etc/systemd/system/canary_automation.{service,timer}
    fi
    systemctl daemon-reload
    systemctl enable daily-fallback-report.timer
    systemctl start daily-fallback-report.timer
    if [ "$ROTATE_EXISTS" = true ]; then
        systemctl enable audit-log-rotate.timer
        systemctl start audit-log-rotate.timer
    fi
    if [ -f "$SCHEDULER_TIMER" ]; then
        systemctl enable scheduler_agent.timer
        systemctl start scheduler_agent.timer
    fi
    if [ -f "$EVO_REPORT_TIMER" ]; then
        systemctl enable evolution_daily_report.timer
        systemctl start evolution_daily_report.timer
    fi
    if [ -f "$SUGGESTION_TIMER" ]; then
        systemctl enable suggestion_agent.timer
        systemctl start suggestion_agent.timer
    fi
    INSTALL_MODE="system"
else
    INSTALL_MODE="user"
fi

if [ "$INSTALL_MODE" = "user" ]; then
    echo "  → 用户模式（~/.config/systemd/user/）"
    USER_SYSTEMD_DIR="$HOME/.config/systemd/user"
    mkdir -p "$USER_SYSTEMD_DIR"
    cp "$SERVICE_SRC" "$USER_SYSTEMD_DIR/" || { echo "❌ 复制失败"; exit 1; }
    cp "$TIMER_SRC" "$USER_SYSTEMD_DIR/" || { echo "❌ 复制失败"; exit 1; }
    chmod 644 "$USER_SYSTEMD_DIR/daily-fallback-report.service"
    chmod 644 "$USER_SYSTEMD_DIR/daily-fallback-report.timer"
    cp "$ROTATE_SERVICE_SRC" "$USER_SYSTEMD_DIR/" 2>/dev/null
    cp "$ROTATE_TIMER_SRC" "$USER_SYSTEMD_DIR/" 2>/dev/null
    cp "$TDX_MONITOR_SERVICE" "$USER_SYSTEMD_DIR/" 2>/dev/null
    cp "$TDX_MONITOR_TIMER" "$USER_SYSTEMD_DIR/" 2>/dev/null
    cp "$TDX_VERIFY_SERVICE" "$USER_SYSTEMD_DIR/" 2>/dev/null
    cp "$TDX_VERIFY_TIMER" "$USER_SYSTEMD_DIR/" 2>/dev/null
    cp "$TDX_AUTONOMY_SERVICE" "$USER_SYSTEMD_DIR/" 2>/dev/null
    cp "$TDX_EVOLUTION_SERVICE" "$USER_SYSTEMD_DIR/" 2>/dev/null
    cp "$SCHEDULER_SERVICE" "$USER_SYSTEMD_DIR/" 2>/dev/null
    cp "$SCHEDULER_TIMER" "$USER_SYSTEMD_DIR/" 2>/dev/null
    cp "$EVO_REPORT_SERVICE" "$USER_SYSTEMD_DIR/" 2>/dev/null
    cp "$EVO_REPORT_TIMER" "$USER_SYSTEMD_DIR/" 2>/dev/null
    cp "$SUGGESTION_SERVICE" "$USER_SYSTEMD_DIR/" 2>/dev/null
    cp "$SUGGESTION_TIMER" "$USER_SYSTEMD_DIR/" 2>/dev/null
    cp "$CANARY_SERVICE" "$USER_SYSTEMD_DIR/" 2>/dev/null
    cp "$CANARY_TIMER" "$USER_SYSTEMD_DIR/" 2>/dev/null
    if [ -f "$USER_SYSTEMD_DIR/audit-log-rotate.timer" ]; then
        chmod 644 "$USER_SYSTEMD_DIR/audit-log-rotate.service"
        chmod 644 "$USER_SYSTEMD_DIR/audit-log-rotate.timer"
    chmod 644 "$USER_SYSTEMD_DIR"/tdx-monitor.service "$USER_SYSTEMD_DIR"/tdx-monitor.timer 2>/dev/null
    chmod 644 "$USER_SYSTEMD_DIR"/tdx-verification.service "$USER_SYSTEMD_DIR"/tdx-verification.timer 2>/dev/null
    chmod 644 "$USER_SYSTEMD_DIR"/tdx-autonomy.service 2>/dev/null
    chmod 644 "$USER_SYSTEMD_DIR"/tdx-evolution.service 2>/dev/null
    chmod 644 "$USER_SYSTEMD_DIR"/scheduler_agent.service "$USER_SYSTEMD_DIR"/scheduler_agent.timer 2>/dev/null
    chmod 644 "$USER_SYSTEMD_DIR"/evolution_daily_report.service "$USER_SYSTEMD_DIR"/evolution_daily_report.timer 2>/dev/null
    chmod 644 "$USER_SYSTEMD_DIR"/suggestion_agent.service "$USER_SYSTEMD_DIR"/suggestion_agent.timer 2>/dev/null
    chmod 644 "$USER_SYSTEMD_DIR"/canary_automation.service "$USER_SYSTEMD_DIR"/canary_automation.timer 2>/dev/null
    fi

    # 启用 user mode（需要 XDG_RUNTIME_DIR 设置过）
    if [ -n "${XDG_RUNTIME_DIR:-}" ]; then
        if systemctl --user daemon-reload 2>/dev/null && \
           systemctl --user enable --now daily-fallback-report.timer 2>/dev/null; then
           systemctl --user enable --now audit-log-rotate.timer
            echo "  ✅ user mode timer 已启用（重启后会失效）"
        else
            echo "  ⚠️ user mode 启用失败"
            echo "     文件已复制到 $USER_SYSTEMD_DIR/"
            echo "     启用方法：loginctl enable-linger $USER"
        fi
    else
        echo "  ⚠️ XDG_RUNTIME_DIR 未设置，user timer 暂未启用"
        echo "     文件已复制到 $USER_SYSTEMD_DIR/"
        echo "     启用方法：loginctl enable-linger $USER && systemctl --user daemon-reload && systemctl --user enable --now daily-fallback-report.timer"
           systemctl --user enable --now audit-log-rotate.timer
    fi
fi

# 2. 创建 reports 目录
echo "[2/5] 创建报告目录..."
mkdir -p "$V5_DIR/reports"
chmod 755 "$V5_DIR/reports"
echo "  ✅ $V5_DIR/reports 已创建"

# 3. 测试日报（立即生成一次验证）
echo "[3/5] 测试日报生成..."
"$ROOT_DIR/venv/bin/python" "$V5_DIR/scripts/daily_fallback_report.py" 2>&1 | tail -5

# 4. 测试 audit log 增长估算
echo "[4/5] audit log 增长估算..."
"$ROOT_DIR/venv/bin/python" "$V5_DIR/scripts/audit_log_rotate.py" --estimate-only --audit-dir "$V5_DIR/audit" 2>&1 | tail -10

# 5. （可选）安装 Prometheus 配置
echo "[5/5] 检测 Prometheus / Alertmanager 安装..."
if command -v prometheus >/dev/null 2>&1; then
    PROMETHEUS_DIR=$(dirname $(readlink -f $(which prometheus)))/..
    if [ -d "$PROMETHEUS_DIR" ]; then
        if [ "$INSTALL_MODE" = "system" ]; then
            sudo cp "$V5_DIR/monitoring/prometheus-alerts.yml" "$PROMETHEUS_DIR/" 2>/dev/null || true
            sudo cp "$V5_DIR/monitoring/prometheus.yml" "$PROMETHEUS_DIR/prometheus.yml.new" 2>/dev/null || true
            echo "  ✅ prometheus-alerts.yml 已复制到 $PROMETHEUS_DIR"
            echo "     请合并到主 prometheus.yml 后重启 prometheus"
        else
            echo "  ⚠️ Prometheus 已装但当前为用户模式，请手动复制配置"
        fi
    fi
else
    echo "  ⚠️ prometheus 未安装（配置已就绪于 $V5_DIR/monitoring/）"
fi

echo
echo "===== 安装完成 ====="
echo "📊 监控端点:"
echo "   - math-sympy-http: http://127.0.0.1:8002/health"
echo "   - workbench API:   http://127.0.0.1:8010/api/monitor/*"
echo "   - 监控看板 UI:     http://127.0.0.1:5173/monitor"
echo ""
echo "⏰ 日报触发：每日 09:00 UTC"
echo "📄 报告位置: $V5_DIR/reports/fallback-YYYY-MM-DD.md"
echo ""
echo "🔍 查看 timer 状态:"
if [ "$INSTALL_MODE" = "system" ]; then
    echo "   systemctl list-timers daily-fallback-report*"
    echo "   systemctl status daily-fallback-report.timer"
else
    echo "   systemctl --user list-timers daily-fallback-report*"
    echo "   systemctl --user status daily-fallback-report.timer"
fi
echo ""
echo "🧪 手动触发日报:"
if [ "$INSTALL_MODE" = "system" ]; then
    echo "   sudo systemctl start daily-fallback-report.service"
else
    echo "   systemctl --user start daily-fallback-report.service"
fi
echo ""
echo "🔧 如需升级到系统模式（开机自启），请执行："
echo "   sudo bash $V5_DIR/scripts/install_monitoring.sh"
echo ""
echo "📦 Slack webhook 接入：编辑 $V5_DIR/monitoring/alertmanager.yml 第 47 行"