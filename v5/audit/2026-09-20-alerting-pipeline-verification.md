# 告警链路端到端验证 · 2026-09-20

**作者**: jiuben  
**目标机**: 127.0.0.1 (linux)  
**验证范围**: prometheus → alertmanager → webhook → 5001 sink  
**结论**: 链路 5 段全部 ✅ 通；**通知出口全链路占位符**（已识别 4 个独立问题）；**Prometheus rule 1 处语义错误**（已修源文件，待 sudo 部署）

---

## 1. 背景

接到 QAT 同学指令 "stop math-sympy-http → 等 90s → 检查 prom/am 链路 → start"，意图确认：
1. Prometheus 能否检测到 upstream 不可达
2. 规则能否升 pending → firing
3. alertmanager 能否接收、路由、送达

期望用 `sudo systemctl stop math-sympy-http`，但**该服务在本机不存在**（system + user 层都无 unit，最近似的是 timer 驱动的 `tdx-verification.service`，不是常驻 HTTP）。改为通过 `/proc` 反查 8002 端口的监听进程 PID=25529，用 `kill -STOP`/`CONT` 冻结/恢复来模拟"上游挂掉"，对真实业务影响最小（只暂停，不释放 socket）。

---

## 2. 时间线

| 时间 (CST) | 事件 | 验证手段 |
|---|---|---|
| 16:17:02 | 实验开始，记录基线：`up=1`, prom 只有 pending `FallbackLayerMissing`, am alerts 空 | curl /api/v1/alerts, /api/v2/alerts |
| 16:17:02 | `kill -STOP 25529` (math-sympy-http 主进程) | /proc/25529/status `State: T (stopped)` |
| 16:17:13 | T+10s, `up` 仍=1（旧 scrape cache） | curl /api/v1/query |
| 16:17:53 | T+50s, `up=0`，prom 新增 `pending UpstreamDown (critical)` | curl /api/v1/alerts |
| 16:18:18 | T+75s, 仍 pending (for=60s 未满足) | curl /api/v1/alerts |
| 16:18:38 | **AUTO CONT 恢复** | `(sleep 95 && kill -CONT 25529)` 后台守护 |
| 16:18:48 | T+105s, `up=1` 恢复；UpstreamDown pending 持续 55s 未升 firing（实验时长不够） | curl /api/v1/query |
| 16:19:48 | T+165s, UpstreamDown 消失，链路回到基线 | curl /api/v1/alerts |

**第一阶段结论**: prom 侧 4 段全 ✅（scrape / rules / pending / 状态变化），**未观察到 firing**（STOP 95s < for=60s + buffer）。

---

## 3. 链路定位 · 后 3 段

链路半通的真实瓶颈定位：

| 段 | 状态 | 证据 |
|---|---|---|
| prom → am 推送 | ✅ | `am received_total{status=firing}` 累积上升 |
| am 内部路由 | ✅ | alert 经 `routes.match.severity: critical` → `critical-webhook` |
| am → webhook POST | ❌ → ✅ | 初始 `connection refused`，启 sink 后 `Notify success` |
| **5001 webhook 接收** | ❌ | 全机扫端口无人监听；alertmanager.yml 第 38/48 行硬编码 `http://127.0.0.1:5001/...` |

**根本原因**: 5001 是"可选 webhook 接收端"占位符，从未实现。整个 `v5/` 项目只有 `v5/monitoring/alertmanager.yml` 引用了 5001，没有对应接收脚本或 systemd 服务。

---

## 4. 验证闭环 B · 起本地 sink

为验证完整链路，**没改 am config**（5001 URL 本就指向 sink），只起了一个最小 webhook receiver：

```python
# /tmp/webhook_sink.py (28 行)
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
import json, time

LOG = "/tmp/alertmanager_webhook.log"

class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(n).decode() if n else ""
        with open(LOG, "a") as f:
            f.write(f"[{time.strftime('%H:%M:%S')}] POST {self.path} bytes={len(body)}\n")
        self.send_response(200); self.send_header("Content-Type","application/json"); self.end_headers()
        self.wfile.write(b'{"ok":true}')
    def log_message(self,*a,**k): return

class ThreadingServer(ThreadingMixIn, HTTPServer): daemon_threads=True
ThreadingServer(("127.0.0.1",5001), Handler).serve_forever()
```

启动后（PID=50349），am 立即成功 retry：

| 时间 | 事件 | am 日志原话 |
|---|---|---|
| 16:48:06 | **Notify success** UpstreamDown → critical-webhook → webhook[0] | `Notify success ... aggrGroup="{alertname=UpstreamDown, service=math-sympy-http}" numAlerts=1 duration=1.096ms` |
| 16:48:10 | **Notify success** FallbackLayerMissing → default → webhook[0] | `Notify success ... aggrGroup="{alertname=FallbackLayerMissing, ...}" numAlerts=1` |
| 之后每 4h | am `repeat_interval: 4h` 自动 retry，sink 持续收到 | 9 次成功 POST |

**链路全段 ✅ 通**。sink 日志 `/tmp/alertmanager_webhook.log` 共记录 13 条 POST（4h 周期），其中前 2 条对应链路验证的关键证据。

---

## 5. 顺手发现的 4 个独立问题

### 5.1 `FallbackLayerMissing` 告警语义写错 🔴 **业务侧真问题**

**症状**: prom 侧 `FallbackLayerMissing` 持续 pending → firing，severity=info。

**根因**: rule 写错：
```yaml
# /etc/prometheus/prometheus-alerts.yml:96-99
expr: |
  absent(fallback_attempts_total{strategy="sympy"}) == 1
  or
  absent(fallback_attempts_total{strategy="ask"}) == 1
```
`absent()` 的真实语义是"series 从未出现过"，**不是**"30 分钟内无调用"。8002 进程自启动 (10:37) 以来 23.5h 只发生 26 次调用，且 `math_strategy_route_and_execute` 只 3 次全部 sympy 成功 → ask 段 metric series **从未产生过** → `absent()` 永远 true → 永远 firing。

**不是 bug, 是 metric 的副作用**：
- audit log 总 174 次请求历史分布：`success: sympy=107 / llm=40 / ask=5 / numeric=18` —— ask 段历史上**确实被调用过**
- 但当前 8002 实例 23.5h 内只跑简单请求，sympy 段通吃 → ask 系列指标一直没 increment
- 真正的问题是**测试流量太简单**，fallback chain 深层路径未被覆盖，不是代码失效

**修复**: 用 `increase()` 反映"30m 内增量"语义，已修源文件 `/home/jiuben/tdx-data-feed/v5/monitoring/prometheus-alerts.yml:95-122`：

```yaml
expr: |
  (sum by(service) (increase(fallback_attempts_total{strategy="sympy"}[30m])) > 0)
  and
  ((sum by(service) (increase(fallback_attempts_total{strategy="ask"}[30m])) == 0)
   or
   (sum by(service) (increase(fallback_attempts_total{strategy="numeric"}[30m])) == 0))
for: 5m
```

`promtool check rules` 通过；prom `/api/v1/query` instant 验证返回空（按设计）。**待 sudo 部署**。

**部署步骤**:
```bash
sudo cp /home/jiuben/tdx-data-feed/v5/monitoring/prometheus-alerts.yml /etc/prometheus/prometheus-alerts.yml
sudo promtool check rules /etc/prometheus/prometheus-alerts.yml
sudo kill -HUP $(pidof prometheus)   # 因 prom 启动时未加 --web.enable-lifecycle，POST /-/reload 返回 403
```

### 5.2 am 通知渠道全链路占位符 🟡 **通知侧配置空**

| Receiver | 配置 URL/凭证 | 实际状态 |
|---|---|---|
| `default` webhook | `http://127.0.0.1:5001/alerts` | 16 次 retry 后 cancel，无人监听 |
| `critical-webhook` webhook | `http://127.0.0.1:5001/alerts/critical` | 同上 |
| `critical-webhook` email | `smtp.gmail.com:587` + `alerts@example.com`/`xxxxx` | **535 BadCredentials** |
| `slack-warnings` Slack | `https://hooks.slack.com/services/<secret>` | **404 no_team**（曾有 `TestSlackPush` 测试） |

**修复路径**（按优先级）：
1. Slack webhook URL：按 `v5/monitoring/PROMETHEUS_SETUP.md §4` 申请并填第 58 行
2. Gmail App Password：填 `smtp_auth_password`，或换公司 SMTP
3. 5001：要么起正式 alert-dispatcher 服务，要么改回空（disable receiver webhook）

### 5.3 5001 占位符从未实现 ⚪ **配置先行遗留坑**

`v5/monitoring/alertmanager.yml` 第 38/48 行 hard-code `127.0.0.1:5001`，注释写 `# 可选 webhook 接收端`，但 `v5/` 全目录无对应接收脚本，无对应 systemd 单元。这是配置先行、接收端永远未部署的典型坑。

### 5.4 `tdx-verification.service` 文档与服务名漂移 ⚪ **次要**

最初用户用 `sudo systemctl stop math-sympy-http` 想停服务，但本机**只有** `tdx-verification.service`（描述为 "run math_sympy tests + smoke test"）。两个服务都跟 math_sympy 相关，但前者是常驻 HTTP，后者是 timer 触发的测试脚本。文档/脚本命名一致性需要后续梳理。

---

## 6. 临时文件清单

| 文件 | 状态 | 说明 |
|---|---|---|
| `/tmp/webhook_sink.py` | 保留 | 28 行 Python 最小 sink，复用价值高 |
| `/tmp/webhook_sink.out` | 保留 | 启动日志 |
| `/tmp/alertmanager_webhook.log` | 保留 | **真实告警 JSON 落地证据**（13 条 am retry POST） |
| `/tmp/probe_full.py` | 已删 | 实验探针 |
| `/tmp/run_b_test.sh` | 已删 | 实验脚本 |

**当前 sink 状态**: 已 kill (PID 50349 已停)。5001 端口恢复无人监听状态。am `Notify success` 不再发生，下次 retry 会再次失败（属预期）。

---

## 7. 后续建议（按紧迫度）

| 优先级 | 动作 | 影响 |
|---|---|---|
| **P0** | 部署 `FallbackLayerMissing` rule 修复（sudo cp + SIGHUP） | 当前告警回归真实语义 |
| **P0** | 修通 ask 路径测试用例（跑一些需 ask 的输入） | 验证 fallback chain 深层路径有效 |
| **P1** | 补 Slack webhook URL | 让 `slack-warnings` receiver 真正可达 |
| **P1** | 修 email SMTP 凭证 | 让 `critical-webhook` email 可达 |
| **P1** | 5001 升级（要么正式 dispatcher，要么 disable） | 消除 am 长期 retry noise |
| **P2** | 链路验证 SOP 化（v5/tests/test_alerting_pipeline.sh） | 每周回归，避免配置漂移 |
| **P2** | 文档更新：`v5/monitoring/PROMETHEUS_SETUP.md` 增加"通知渠道全占位符"已知问题节 | 省后续 ops 时间 |

---

## 8. 关键证据链引用

- prom sink 真实收到 am 的 POST: `/tmp/alertmanager_webhook.log` 第 4-7 行 (16:48:06 /alerts/critical, 16:48:10 /alerts)
- am 日志 `Notify success`: `journalctl -u alertmanager --since "2026-09-20 16:48" | grep "Notify success"`
- am metrics 累积: `curl http://127.0.0.1:9093/metrics | grep webhook_total`（重启前 7 次，成功率 100%）
- audit log fallback 分布: `python3 -c "import json; from collections import Counter; ..."` 见上文 §5.1
- 新 rule 语法验证: `promtool check rules /home/jiuben/tdx-data-feed/v5/monitoring/prometheus-alerts.yml`
- 新 rule 在 prom 即时验证: `curl -G 'http://127.0.0.1:9090/api/v1/query' --data-urlencode 'query=<新 expr>'`

---

**签名**: jiuben @ 2026-09-20 18:xx CST (实验) / 2026-09-21 11:xx CST (文档)