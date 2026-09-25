#!/usr/bin/env bash
# test_pushplus_e2e.sh · PushPlus 端到端测试脚本
# 验证：kill math-sympy-http → prometheus up{}==0 → alertmanager → pushplus_bridge → 微信

set +e

echo "═══════════════════════════════════════════════════════════"
echo "PushPlus 端到端测试"
echo "═══════════════════════════════════════════════════════════"

# 1. 找到并杀进程
PID=$(ps -ef | grep math_sympy_http | grep -v grep | awk '{print $2}')
echo "Step 1: 杀掉 math-sympy-http PID=$PID"
sudo kill -9 "$PID"

# 2. 验证停了
sleep 3
echo
echo "Step 2: 验证停了"
if curl -sf -m 2 http://127.0.0.1:8002/health > /dev/null; then
    echo "  ✗ 还在响应（kill 失败？）"
else
    echo "  ✓ 已停（curl failed）"
fi

# 3. 等待 120 秒
echo
echo "Step 3: 等待 120 秒（prometheus 4 次 scrape + for:1m + 路由 + pushplus 推送）"
sleep 120

# 4. 看 prometheus up 指标
echo
echo "Step 4: prometheus up 指标"
curl -s 'http://127.0.0.1:9090/api/v1/query?query=up{job="math-sympy-http"}' \
  | python3 -c "import json,sys;d=json.loads(sys.stdin.read());print('  up =', d['data']['result'][0]['value'][1] if d['data']['result'] else 'NO DATA')"

# 5. 看 alertmanager active alerts
echo
echo "Step 5: alertmanager active alerts"
curl -s http://127.0.0.1:9093/api/v2/alerts \
  | python3 -c "
import json,sys
d=json.loads(sys.stdin.read())
print(f'  active alerts: {len(d)}')
for a in d[:5]:
    sev = a['labels'].get('severity', '?')
    name = a['labels']['alertname']
    state = a['status']['state']
    print(f'    [{sev}] {name} state={state}')"

# 6. 看 pushplus_bridge journal
echo
echo "Step 6: pushplus_bridge POST 记录"
journalctl -u pushplus_bridge.service --since "3 minute ago" --no-pager \
  | grep -E "POST|pushplus" | tail -5

# 7. 恢复
echo
echo "Step 7: 恢复 math-sympy-http"
cd /home/jiuben/tdx-data-feed
nohup ./venv/bin/python v5/mcp_servers/math_sympy_http.py --host 127.0.0.1 --port 8002 --workers 1 > /tmp/math-sympy-http.log 2>&1 &
NEW_PID=$!
echo "  ✓ 新 PID=$NEW_PID"
sleep 5
curl -s http://127.0.0.1:8002/health | head -c 80
echo

# 8. 等待 60 秒（alert resolved）
echo
echo "Step 8: 等待 60 秒（alert resolved）"
sleep 60

# 9. 最终日志
echo
echo "Step 9: 最终 pushplus_bridge 日志"
journalctl -u pushplus_bridge.service --since "5 minute ago" --no-pager \
  | grep -E "POST|pushplus|FIRING|RESOLVED" | tail -15

echo
echo "═══════════════════════════════════════════════════════════"
echo "请查看你的微信，应该收到 2 条推送："
echo "  1. 🚨 FIRING: UpstreamDown"
echo "  2. ✅ RESOLVED: UpstreamDown"
echo "═══════════════════════════════════════════════════════════"