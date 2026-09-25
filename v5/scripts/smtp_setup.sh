#!/bin/bash
# smtp_setup.sh · SMTP 邮件推送配置（等用户提供 Gmail App Password）
#
# 用法：
#   bash smtp_setup.sh '<Gmail_App_Password>' '<receiver@gmail.com>'
#
# 步骤：
#   1. 获取 Gmail App Password：
#      a. https://myaccount.google.com/security → 启用 2-Step Verification
#      b. https://myaccount.google.com/apppasswords → 创建 "Mail / Other (tdx-alertmanager)"
#      c. 拿到 16 字符 password（如 "abcd efgh ijkl mnop"）
#
#   2. 跑本脚本：
#      bash smtp_setup.sh 'abcd efgh ijkl mnop' 'your@gmail.com'
#
#   3. 验证：
#      sudo systemctl stop math-sympy-http    # 触发 UpstreamDown
#      # 几分钟后应收到 email
#      sudo systemctl start math-sympy-http

set -euo pipefail

PASSWORD="${1:-}"
RECEIVER="${2:-}"
ALERTMANAGER_YML="/home/jiuben/tdx-data-feed/v5/monitoring/alertmanager.yml"
SYSTEM_ALERTMANAGER_YML="/etc/alertmanager/alertmanager.yml"

if [ -z "$PASSWORD" ] || [ -z "$RECEIVER" ]; then
    echo "用法: bash smtp_setup.sh '<Gmail_App_Password>' '<receiver@gmail.com>'"
    echo ""
    echo "获取 Gmail App Password:"
    echo "  1. https://myaccount.google.com/security → 启用 2-Step Verification"
    echo "  2. https://myaccount.google.com/apppasswords → 创建 'Mail' 用途"
    echo "  3. 拿到 16 字符 password 后跑本脚本"
    exit 1
fi

GMAIL_USER="tdx-alerts@gmail.com"

echo "=== SMTP 配置 ==="
echo "  from: $GMAIL_USER"
echo "  to:   $RECEIVER"
echo "  host: smtp.gmail.com:587"

# 备份
cp "$ALERTMANAGER_YML" "${ALERTMANAGER_YML}.bak.$(date +%Y%m%d-%H%M%S)"
echo "  ✓ 备份原 alertmanager.yml"

# 改 SMTP 配置（用 python sed 避免 sed 转义问题）
python3 <<PYEOF
import re
p = "$ALERTMANAGER_YML"
text = open(p).read()

# 替换 SMTP 字段
text = re.sub(r"smtp_from:\s*'[^']*'",
              f"smtp_from: '$GMAIL_USER'", text)
text = re.sub(r"smtp_auth_username:\s*'[^']*'",
              f"smtp_auth_username: '$GMAIL_USER'", text)
text = re.sub(r"smtp_auth_password:\s*'[^']*'",
              f"smtp_auth_password: '{repr(\"$PASSWORD\")[1:-1]}'", text)

# 替换 critical-webhook receiver 的 email to
text = re.sub(r"(to:\s*)'[^']*'",
              f"\\1'$RECEIVER'", text, count=1)

open(p, 'w').write(text)
print('  ✓ alertmanager.yml SMTP 字段已更新')
PYEOF

echo
echo "=== 改完后关键字段 ==="
grep -E "smtp_from|smtp_auth_username|smtp_auth_password|to:" "$ALERTMANAGER_YML"

echo
echo "=== 部署到系统模式 ==="
if [ -d "/etc/alertmanager" ]; then
    sudo cp "$ALERTMANAGER_YML" "$SYSTEM_ALERTMANAGER_YML"
    sudo systemctl restart alertmanager
    sleep 2
    echo "  ✓ alertmanager 重启"
else
    echo "  ⚠ /etc/alertmanager 不存在，请先跑 install_monitoring.sh"
fi

echo
echo "=== 验证 SMTP 配置 ==="
curl -s http://127.0.0.1:9093/-/ready && echo "  ✓ alertmanager ready"

echo
echo "=== 触发 UpstreamDown 验证邮件 ==="
echo "  1. 停 math-sympy-http:"
echo "       sudo systemctl stop math-sympy-http"
echo "  2. 等 1~3 分钟，应该在 $RECEIVER 收到邮件"
echo "  3. 恢复服务:"
echo "       sudo systemctl start math-sympy-http"