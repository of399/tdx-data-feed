#!/usr/bin/env python3
"""tdx_monitor.py · M4 系统监控（CPU/GPU/Disk/Services）

定期采集系统指标 + 关键服务健康状态，写到 /tmp/tdx-metrics.json + journal。

输出（每 5 分钟 systemd timer 触发）：
- CPU 使用率（5min 平均）
- GPU 占用（nvidia-smi）
- 磁盘使用率（v5/audit/ 目录）
- 关键服务健康：math-sympy-http / ollama / workbench

journal 日志格式：JSON（结构化），便于 Alertmanager 抓取

用法：
    venv/bin/python v5/scripts/tdx_monitor.py
    # systemd timer 调度（5min）
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

METRICS_FILE = Path(
    os.environ.get("TDX_METRICS_FILE", "/home/jiuben/tdx-data-feed/v5/reports/tdx-metrics.json")
)
MATH_HTTP_URL = os.environ.get("MATH_HTTP_URL", "http://127.0.0.1:8002")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
WORKBENCH_URL = os.environ.get("WORKBENCH_URL", "http://127.0.0.1:8010")
ALERT_LOG_DIR = Path("/home/jiuben/tdx-data-feed/v5/audit")
ALERT_LOG_FILE = ALERT_LOG_DIR / "system-health.jsonl"


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _read_loadavg() -> dict:
    """CPU load average (1min/5min/15min)"""
    try:
        with open("/proc/loadavg") as f:
            parts = f.read().split()
        return {
            "load_1min": float(parts[0]),
            "load_5min": float(parts[1]),
            "load_15min": float(parts[2]),
        }
    except OSError, IndexError, ValueError:
        return {}


def _read_disk_usage(path: str = "/") -> dict:
    """磁盘使用率"""
    try:
        u = shutil.disk_usage(path)
        return {
            "total_gb": round(u.total / 1024**3, 1),
            "used_gb": round(u.used / 1024**3, 1),
            "free_gb": round(u.free / 1024**3, 1),
            "used_pct": round(u.used / u.total * 100, 1),
        }
    except OSError as e:
        return {"error": str(e)}


def _read_gpu() -> dict:
    """GPU 状态（nvidia-smi）"""
    try:
        out = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=index,name,utilization.gpu,memory.used,memory.total,temperature.gpu",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if out.returncode != 0:
            return {"available": False, "error": out.stderr.strip()[:200]}
        gpus = []
        for line in out.stdout.strip().split("\n"):
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 6:
                gpus.append(
                    {
                        "index": int(parts[0]),
                        "name": parts[1],
                        "util_pct": float(parts[2]) if parts[2] else 0,
                        "mem_used_mb": float(parts[3]) if parts[3] else 0,
                        "mem_total_mb": float(parts[4]) if parts[4] else 0,
                        "temp_c": float(parts[5]) if parts[5] else 0,
                    }
                )
        return {"available": True, "gpus": gpus}
    except FileNotFoundError:
        return {"available": False, "error": "nvidia-smi not found"}
    except subprocess.TimeoutExpired:
        return {"available": False, "error": "nvidia-smi timeout"}


def _http_health(url: str, name: str, timeout: float = 2.0) -> dict:
    """HTTP 健康检查"""
    try:
        import httpx

        with httpx.Client(timeout=timeout) as client:
            t0 = time.time()
            r = client.get(url)
            elapsed_ms = (time.time() - t0) * 1000
            return {
                "name": name,
                "url": url,
                "status": "online" if r.status_code == 200 else f"http_{r.status_code}",
                "elapsed_ms": round(elapsed_ms, 1),
            }
    except Exception as e:
        return {"name": name, "url": url, "status": "offline", "error": str(e)[:100]}


def _check_audit_log_growth() -> dict:
    """audit log 增长率检查"""
    if not ALERT_LOG_DIR.exists():
        return {"exists": False}
    chain_log = ALERT_LOG_DIR / "fallback-chain.jsonl"
    if not chain_log.exists():
        return {"exists": False}
    size_mb = chain_log.stat().st_size / 1024 / 1024
    return {
        "exists": True,
        "size_mb": round(size_mb, 2),
        "warning": size_mb > 100,  # >100MB 警告
    }


def check_alerts(metrics: dict) -> list[str]:
    """根据 metrics 生成 alert 列表"""
    alerts = []

    # CPU load
    load5 = metrics.get("load", {}).get("load_5min", 0)
    if load5 > 8:
        alerts.append(f"🔴 CPU load 5min={load5:.1f} > 8（高负载）")

    # Disk usage
    disk_used_pct = metrics.get("disk", {}).get("used_pct", 0)
    if disk_used_pct > 90:
        alerts.append(f"🔴 磁盘使用率 {disk_used_pct}% > 90%")

    # Services offline
    for svc in metrics.get("services", []):
        if svc.get("status") == "offline":
            alerts.append(f"🟠 服务离线: {svc.get('name')}")

    # GPU 异常温度
    for gpu in metrics.get("gpu", {}).get("gpus", []):
        if gpu.get("temp_c", 0) > 80:
            alerts.append(f"🟠 GPU {gpu['index']} 温度 {gpu['temp_c']}°C > 80°C")

    # audit log 膨胀
    if metrics.get("audit_log", {}).get("warning"):
        alerts.append(f"🟡 audit log 大小 {metrics['audit_log']['size_mb']}MB > 100MB（需 rotate）")

    return alerts


def main() -> int:
    metrics = {
        "ts": _utc_now().isoformat(),
        "hostname": os.uname().nodename,
        "load": _read_loadavg(),
        "disk": _read_disk_usage("/"),
        "gpu": _read_gpu(),
        "services": [
            _http_health(f"{MATH_HTTP_URL}/health", "math-sympy-http"),
            _http_health(f"{OLLAMA_URL}/api/tags", "ollama"),
            _http_health(f"{WORKBENCH_URL}/health", "workbench"),
        ],
        "audit_log": _check_audit_log_growth(),
    }
    metrics["alerts"] = check_alerts(metrics)

    # 写 metrics 文件（供前端 / 其他工具读）
    METRICS_FILE.write_text(json.dumps(metrics, indent=2, ensure_ascii=False))

    # 追加到 audit log（结构化 JSONL）
    ALERT_LOG_DIR.mkdir(parents=True, exist_ok=True)
    with ALERT_LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(metrics, ensure_ascii=False) + "\n")

    # 写 Prometheus textfile collector（M3 W6 + M5）
    # node-exporter 默认读 /var/lib/prometheus/node-exporter/tdx_*.prom
    textfile_dir = Path(os.environ.get("TDX_TEXTFILE_DIR", "/var/lib/prometheus/node-exporter"))
    if textfile_dir.parent.exists() or os.access(textfile_dir.parent, os.W_OK):
        try:
            textfile_dir.mkdir(parents=True, exist_ok=True)
            textfile_path = textfile_dir / "tdx_monitor.prom"
            with textfile_path.open("w", encoding="utf-8") as f:
                f.write(_to_prometheus_textfile(metrics))
        except Exception as e:
            print(f"textfile collector write failed: {e}", file=sys.stderr)

    # 输出到 journal（结构化日志）
    summary = (
        f"ts={metrics['ts']} "
        f"load5={metrics['load'].get('load_5min', 0):.2f} "
        f"disk={metrics['disk'].get('used_pct', 0):.1f}% "
        f"gpu={len(metrics['gpu'].get('gpus', []))} "
        f"services={[s['status'] for s in metrics['services']]} "
        f"alerts={len(metrics['alerts'])}"
    )
    print(summary, file=sys.stderr)

    # 如果有 alert，打印到 stdout（journal 会捕获）
    if metrics["alerts"]:
        for a in metrics["alerts"]:
            print(f"ALERT: {a}", file=sys.stderr)

    return 0 if not metrics["alerts"] else 1  # exit 1 让 systemd 看到非零退出


def _to_prometheus_textfile(metrics: dict) -> str:
    """转 Prometheus textfile collector 格式

    输出示例（prom 格式）：
      # HELP tdx_load5 System load 5min average
      # TYPE tdx_load5 gauge
      tdx_load5 0.71
      # HELP tdx_disk_used_percent Disk used percent
      # TYPE tdx_disk_used_percent gauge
      tdx_disk_used_percent 11.7
      # HELP tdx_services_up Service health status (1=up, 0=down)
      # TYPE tdx_services_up gauge
      tdx_services_up{service="math-sympy-http"} 1
      ...
    """
    lines = []

    # load
    load5 = metrics["load"].get("load_5min", 0)
    lines.append("# HELP tdx_load5 System load 5min average")
    lines.append("# TYPE tdx_load5 gauge")
    lines.append(f"tdx_load5 {load5}")

    # disk
    disk_pct = metrics["disk"].get("used_pct", 0)
    lines.append("# HELP tdx_disk_used_percent Disk used percent")
    lines.append("# TYPE tdx_disk_used_percent gauge")
    lines.append(f"tdx_disk_used_percent {disk_pct}")

    # gpu
    gpu_count = len(metrics["gpu"].get("gpus", []))
    lines.append("# HELP tdx_gpu_count Number of GPUs detected")
    lines.append("# TYPE tdx_gpu_count gauge")
    lines.append(f"tdx_gpu_count {gpu_count}")

    # services (1=up, 0=down)
    lines.append("# HELP tdx_services_up Service health status (1=up, 0=down)")
    lines.append("# TYPE tdx_services_up gauge")
    for svc in metrics["services"]:
        up = 1 if svc["status"] == "online" else 0
        lines.append(f'tdx_services_up{{service="{svc["name"]}"}} {up}')

    # alerts count
    alert_count = len(metrics["alerts"])
    lines.append("# HELP tdx_alerts_count Number of active alerts")
    lines.append("# TYPE tdx_alerts_count gauge")
    lines.append(f"tdx_alerts_count {alert_count}")

    # audit_log lines (recent count in 24h)
    audit_total = metrics.get("audit_log", {}).get("total_lines", 0)
    lines.append("# HELP tdx_audit_log_lines Total audit log lines")
    lines.append("# TYPE tdx_audit_log_lines gauge")
    lines.append(f"tdx_audit_log_lines {audit_total}")

    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    sys.exit(main())
