# 告警链路验证 SOP

端到端验证 `math-sympy-http` → `prometheus` → `alertmanager` → `webhook sink` 链路是否通畅。

参考：
- `audit/2026-09-20-alerting-pipeline-verification.md`（昨日手工验证记录）
- `audit/2026-09-21-vllm-quantize-exploration.md`（vLLM 探索止损记录，B 方案）

## 快速开始

```bash
cd /home/jiuben/tdx-data-feed
bash v5/tests/test_alerting_pipeline.sh                          # quick (5s)
bash v5/tests/test_alerting_pipeline.sh --mode full              # full (140s, 侵入式)
bash v5/tests/test_alerting_pipeline.sh --mode full --auto-sink  # full + 自动起/停 webhook sink
```

## 模式说明

| 模式 | 用时 | 业务影响 | 验证范围 |
|---|---|---|---|
| `quick` | < 5s | 零 | prom/am ready, 8002 app up=1, rules 已加载, sink 状态 |
| `full` | 130-160s | 冻结 math-sympy-http 进程 140s | + kill -STOP 触发 UpstreamDown firing → am 路由 → sink 收 POST → CONT 恢复 |
| `full --auto-sink` | 130-160s | 同上 + 自动起/停 :5001 sink | 同上但 sink 缺失也能验证 |

## 退出码

| Code | 含义 |
|---|---|
| 0 | 全部 PASS |
| 1 | 至少 1 FAIL |
| 2 | 启动错误（前置条件不满足，如 8002 app 没启） |
| 130 | Ctrl-C 中断 |

CI 集成：解析 `$?` 决定失败 / 通过。

## JSON 报告

每次跑会写一个 JSON 报告（含时间戳、mode、result、exit_code）。

默认路径：`/tmp/alerting-pipeline-report.json`

可自定义：`--report-json /path/to/report.json`

```json
{
  "started_at":  "2026-09-21T15:23:12+0800",
  "finished_at": "2026-09-21T15:23:12+0800",
  "elapsed_s":   0,
  "mode":        "quick",
  "auto_sink":   0,
  "exit_code":   0,
  "result":      "PASS"
}
```

## 各 stage 含义

quick 模式跑 6 项：

| Stage | 含义 | 通过条件 |
|---|---|---|
| `prom_ready` | Prometheus up | `/-/ready` HTTP 200 |
| `am_ready` | Alertmanager up | `/-/ready` HTTP 200 |
| `app_reachable` | 8002 应用健康 | TCP 通 + prom `up{job="math-sympy-http"} == 1` |
| `sink_present` | webhook 出口 | `:5001` 有人监听（缺失时 SKIP，不 FAIL）|
| `am_metrics` | am 累计通知 | `alertmanager_notifications_total{integration="webhook"} >= 0`（首次可 0）|
| `rules_parsed` | rules 加载 | prom /api/v1/rules 含 `FallbackLayerMissing` + `UpstreamDown` |

full 模式跑全套 quick + 链路验证：

| Stage | 含义 | 通过条件 |
|---|---|---|
| `baseline` | STOP 前基线 | prom `up == 1` |
| `process_stopped` | kill -STOP 已发送 | OS 接受信号 |
| `up_zero_detected` | prom 反映服务宕 | T+30s 内 `up == 0` |
| `prom_firing` | 规则升 firing | T+90s 内 `UpstreamDown` 状态为 `firing` |
| `am_route` | am 收到并路由 | am /api/v2/alerts 含 UpstreamDown 且 receivers 含 `critical-webhook` |
| `sink_received` | webhook 真送达 | 30s 内 sink 日志出现 POST（含 UpstreamDown）|
| `process_resumed` | kill -CONT 已发送 | OS 接受信号 |
| `recovered` | 链路恢复 | CONT 后 60s 内 prom `up == 1` |
| `autosink_started/stopped` | auto-sink 生命周期 | sink PID 起来 + 兜底关掉 |

## 关键时序（full 模式）

```
T+0s     baseline (up=1)
T+0s     kill -STOP PID
T+30s    scrape 反映 up=0
T+90s    UpstreamDown 升 firing（for=60s 后）
T+95s    am 收到 firing，路由到 critical-webhook
T+95s    sink 收到 POST /alerts/critical
T+130s   实验结束，kill -CONT
T+130-190s  up 回到 1
```

## 已知陷阱

| 现象 | 原因 |
|---|---|
| `sink_received` 一直是 INFO | am `group_interval=5m`，STOP 期间触发的 firing 在 CONT 后才会重试。可加 `repeat_interval: 30s` 加快（不在默认 SOP 范围）。 |
| `auto-sink` 起不来 | :5001 已被占。看 `lsof -i:5001` |
| `failed: CONT 后 60s 内 up 未回 1` | math-sympy-http 真挂了，去 systemd 看 |

## 何时跑

- 改 alert rule 后：`bash v5/tests/test_alerting_pipeline.sh --mode full --auto-sink`
- 改 alertmanager.yml 后：同上
- 日常巡检：`bash v5/tests/test_alerting_pipeline.sh`（quick）
- CI 集成：见 GitHub Actions / GitLab CI 模板（待补）

## 历史

- 2026-09-20：手工验证链路，写 audit 报告（10KB）
- 2026-09-21：脚本化 + 加 auto-sink fixture + JSON 报告 + CLI wrapper