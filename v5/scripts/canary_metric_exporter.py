#!/usr/bin/env python3
"""canary_metric_exporter.py - Prometheus exporter for canary metrics"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

STATE_FILE = Path("/home/jiuben/tdx-data-feed/v5/audit/canary_state.json")
AUDIT_DIR = Path("/home/jiuben/tdx-data-feed/v5/audit")
TODAY_FILE = AUDIT_DIR / "fallback-chain.jsonl"


def _read_state() -> dict:
    if not STATE_FILE.exists():
        return {"ratio": 0.0, "history": []}
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {"ratio": 0.0, "history": []}


def _read_today_calls() -> dict:
    if not TODAY_FILE.exists():
        return {}
    out = {
        "control": 0,
        "canary": 0,
        "control_ok": 0,
        "canary_ok": 0,
        "control_total_s": 0.0,
        "canary_total_s": 0.0,
    }
    try:
        for line in TODAY_FILE.read_text().splitlines():
            if not line.strip():
                continue
            e = json.loads(line)
            group = e.get("group", "control")
            ok = e.get("ok", False)
            elapsed = float(e.get("total_elapsed_s", 0))
            out[group] = out.get(group, 0) + 1
            if ok:
                out[f"{group}_ok"] = out.get(f"{group}_ok", 0) + 1
            out[f"{group}_total_s"] = out.get(f"{group}_total_s", 0.0) + elapsed
    except Exception as ex:
        print(f"warn: parse error: {ex}", file=sys.stderr)
    return out


def render_metrics() -> str:
    state = _read_state()
    calls = _read_today_calls()
    lines = []
    lines.append("# HELP canary_ratio Current canary traffic ratio (0-1)")
    lines.append("# TYPE canary_ratio gauge")
    lines.append(f"canary_ratio {state.get('ratio', 0.0):.4f}")
    lines.append("# HELP canary_decisions_total Total canary ratio decisions")
    lines.append("# TYPE canary_decisions_total counter")
    lines.append(f"canary_decisions_total {len(state.get('history', []))}")
    lines.append("# HELP canary_calls_total Total math-sympy-http calls by canary group")
    lines.append("# TYPE canary_calls_total counter")
    lines.append(f'canary_calls_total{{group="control"}} {calls.get("control", 0)}')
    lines.append(f'canary_calls_total{{group="canary"}} {calls.get("canary", 0)}')
    lines.append("# HELP canary_calls_success_total Successful calls by group")
    lines.append("# TYPE canary_calls_success_total counter")
    lines.append(f'canary_calls_success_total{{group="control"}} {calls.get("control_ok", 0)}')
    lines.append(f'canary_calls_success_total{{group="canary"}} {calls.get("canary_ok", 0)}')
    lines.append("# HELP canary_success_rate Success rate by group (0-1)")
    lines.append("# TYPE canary_success_rate gauge")
    for g in ("control", "canary"):
        total = calls.get(g, 0)
        ok = calls.get(f"{g}_ok", 0)
        rate = (ok / total) if total > 0 else 1.0
        lines.append(f'canary_success_rate{{group="{g}"}} {rate:.4f}')
    lines.append("# HELP canary_avg_duration_seconds Average call duration by group")
    lines.append("# TYPE canary_avg_duration_seconds gauge")
    for g in ("control", "canary"):
        total = calls.get(g, 0)
        total_s = calls.get(f"{g}_total_s", 0.0)
        avg = (total_s / total) if total > 0 else 0.0
        lines.append(f'canary_avg_duration_seconds{{group="{g}"}} {avg:.4f}')
    return "\n".join(lines) + "\n"


class MetricsHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/metrics":
            body = render_metrics().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args):
        pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=9101)
    args = parser.parse_args()
    print(f"=== Canary Metrics Exporter :{args.port}/metrics ===")
    httpd = HTTPServer(("127.0.0.1", args.port), MetricsHandler)
    print(f"  state: {STATE_FILE}")
    print(f"  audit: {TODAY_FILE}")
    print(f"  listening on 127.0.0.1:{args.port}")
    with contextlib.suppress(KeyboardInterrupt):
        httpd.serve_forever()


if __name__ == "__main__":
    main()
