#!/bin/bash
# pushplus_e2e.sh - 用 systemctl stop 替代 kill（避免自动重启）
echo "Step 1: sudo systemctl stop math-sympy-http.service"
sudo systemctl stop math-sympy-http.service
sleep 5
echo "  status: $(systemctl is-active math-sympy-http.service)"
echo
echo "Step 2: 等待 130 秒（for:1m + 4 scrape + 路由）"
sleep 130
echo
echo "Step 3: 检查状态"
echo "  up 值:"
curl -s 'http://127.0.0.1:9090/api/v1/query?query=up' | python3 -c "
import json,sys
d=json.loads(sys.stdin.read())
for r in d['data']['result']:
    if r['metric']['job']=='math-sympy-http':
        print('   ', r['metric']['job'], '=', r['value'][1])
"
echo "  prometheus alerts:"
curl -s http://127.0.0.1:9090/api/v1/alerts | python3 -c "
import json,sys
d=json.loads(sys.stdin.read())
a=d['data']['alerts']
print('   total:', len(a))
for x in a:
    print('   ', x['labels'].get('alertname','?'), 'state=', x['state'])
"
echo "  alertmanager alerts:"
curl -s http://127.0.0.1:9093/api/v2/alerts | python3 -c "
import json,sys
d=json.loads(sys.stdin.read())
print('   total:', len(d))
for x in d:
    print('   ', x['labels'].get('alertname','?'), 'state=', x['status']['state'])
"
echo "  pushplus_bridge POST:"
journalctl -u pushplus_bridge.service --since "3 minute ago" --no-pager | grep -E "POST|pushplus" | tail -5
echo
echo "Step 4: 恢复"
sudo systemctl start math-sympy-http.service
echo "  status: $(systemctl is-active math-sympy-http.service)"
sleep 5
curl -s http://127.0.0.1:8002/health | head -c 80
echo
echo
echo "Step 5: 等待 60 秒 alert resolved"
sleep 60
echo "  最终 pushplus_bridge 日志:"
journalctl -u pushplus_bridge.service --since "5 minute ago" --no-pager | grep -E "POST|pushplus" | tail -10
