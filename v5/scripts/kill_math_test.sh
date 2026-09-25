PID=$(pgrep -f math_sympy_http)
echo "Step 1: kill PID=$PID"
sudo kill -9 $PID
sleep 5
if pgrep -f math_sympy_http > /dev/null; then
  echo "Step 2: still alive"
else
  echo "Step 2: dead"
fi
echo "Step 3: wait 130s"
sleep 130
echo "Step 4: check"
curl -s 'http://127.0.0.1:9090/api/v1/query?query=up' | python3 -c "
import json,sys
d=json.loads(sys.stdin.read())
for r in d['data']['result']:
    if r['metric']['job']=='math-sympy-http':
        print('  up =', r['value'][1])
"
curl -s http://127.0.0.1:9090/api/v1/alerts | python3 -c "
import json,sys
d=json.loads(sys.stdin.read())
alerts=d['data']['alerts']
print('  prom alerts:', len(alerts))
for a in alerts:
    print('  ', a['labels'].get('alertname','?'), 'state=', a['state'])
"
curl -s http://127.0.0.1:9093/api/v2/alerts | python3 -c "
import json,sys
d=json.loads(sys.stdin.read())
print('  am alerts:', len(d))
for a in d:
    print('  ', a['labels'].get('alertname','?'), 'state=', a['status']['state'])
"
journalctl -u pushplus_bridge.service --since "3 minute ago" --no-pager | grep -E "POST|pushplus" | tail -5
echo "Step 5: restart"
cd /home/jiuben/tdx-data-feed
nohup ./venv/bin/python v5/mcp_servers/math_sympy_http.py --host 127.0.0.1 --port 8002 --workers 1 > /tmp/math-sympy-http.log 2>&1 &
NEW_PID=$!
echo "  new PID=$NEW_PID"
sleep 5
curl -s http://127.0.0.1:8002/health | head -c 80
echo
echo "Step 6: wait 60s"
sleep 60
journalctl -u pushplus_bridge.service --since "5 minute ago" --no-pager | grep -E "POST|pushplus" | tail -10
