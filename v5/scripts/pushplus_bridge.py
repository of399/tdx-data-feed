#!/usr/bin/env python3
"""
pushplus_bridge.py · M3 W6 告警通道

alertmanager → 本桥（127.0.0.1:5002）→ PushPlus（微信推送）

工作流程：
1. alertmanager webhook_configs 触发 POST 到 http://127.0.0.1:5002/pushplus
2. 本桥接收 alertmanager 标准 payload（versionedAlerts 列表）
3. 转 PushPlus 期望格式（{token, title, content, template}）
4. POST 到 https://www.pushplus.plus/send/

用法：
    PUSHPLUS_TOKEN=xxx python pushplus_bridge.py [--port 5002]
"""

from __future__ import annotations

import argparse
import os
import sys
import time

import httpx
from fastapi import FastAPI, HTTPException, Request

# ---------- 配置 ----------
PUSHPLUS_TOKEN = os.environ.get("PUSHPLUS_TOKEN", "")
PUSHPLUS_URL = "https://www.pushplus.plus/send/"

if not PUSHPLUS_TOKEN:
    print("⚠️  PUSHPLUS_TOKEN 未设置（export PUSHPLUS_TOKEN=xxx）", file=sys.stderr)
    sys.exit(1)

BRIDGE_PORT = int(os.environ.get("BRIDGE_PORT", "5002"))
BRIDGE_HOST = os.environ.get("BRIDGE_HOST", "127.0.0.1")

app = FastAPI(title="PushPlus Bridge for Alertmanager", version="0.1.0")
START_TIME = time.time()


# ---------- Alertmanager payload → PushPlus 转换 ----------
def _format_alert(a: dict) -> str:
    """把单个 alert 格式化为 HTML（PushPlus 推荐）"""
    labels = a.get("labels", {})
    annotations = a.get("annotations", {})
    status = a.get("status", "firing")
    starts_at = a.get("startsAt", "")

    # severity 决定 emoji
    severity = labels.get("severity", "info")
    sev_emoji = {
        "critical": "🔴",
        "warning": "🟡",
        "info": "ℹ️",
    }.get(severity, "⚪")

    html = f"""
{sev_emoji} <b>{labels.get("alertname", "?")}</b> ({status})
  • Service: <code>{labels.get("service", "?")}</code>
  • Severity: <code>{severity}</code>
  • Time: <code>{starts_at}</code>
  • Summary: {annotations.get("summary", "-")}
  • Description: {annotations.get("description", "-")}
"""
    return html.strip()


def _build_pushplus_payload(req_body: dict) -> dict:
    """alertmanager → pushplus"""
    # alertmanager 0.27+ 用 status=firing/resolved + alerts[]
    alerts = req_body.get("alerts", [])
    title_alerts = req_body.get("commonLabels", {})

    if not alerts:
        raise ValueError("no alerts in payload")

    # title
    firing_count = sum(1 for a in alerts if a.get("status") == "firing")
    resolved_count = sum(1 for a in alerts if a.get("status") == "resolved")
    alert_name = title_alerts.get("alertname", "Alert")

    if resolved_count and not firing_count:
        title = f"✅ RESOLVED: {alert_name}"
    elif firing_count:
        title = f"🚨 FIRING ({firing_count}): {alert_name}"
    else:
        title = f"📊 {alert_name}"

    # content（HTML）
    content_lines = ["<h3>TDX 告警</h3>"]
    for a in alerts:
        content_lines.append(_format_alert(a))
        content_lines.append("<hr>")

    return {
        "token": PUSHPLUS_TOKEN,
        "title": title,
        "content": "\n".join(content_lines),
        "template": "html",
    }


# ---------- 端点 ----------
@app.post("/pushplus")
async def pushplus_endpoint(request: Request):
    """alertmanager webhook_configs 推送入口"""
    try:
        body = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"invalid JSON: {e}") from e

    try:
        payload = _build_pushplus_payload(body)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"payload build failed: {e}") from e

    async with httpx.AsyncClient(timeout=10) as client:
        try:
            r = await client.post(PUSHPLUS_URL, json=payload)
            r.raise_for_status()
            result = r.json()
        except httpx.HTTPStatusError as e:
            raise HTTPException(
                status_code=502,
                detail=f"pushplus HTTP {e.response.status_code}: {e.response.text[:200]}",
            ) from e
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"pushplus failed: {e}") from e

    return {
        "ok": result.get("code") == 200,
        "pushplus_code": result.get("code"),
        "pushplus_msg": result.get("msg"),
        "alerts_count": len(payload.get("content", [])),
    }


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "uptime_s": round(time.time() - START_TIME, 1),
        "pushplus_token_configured": bool(PUSHPLUS_TOKEN),
        "pushplus_token_preview": PUSHPLUS_TOKEN[:8] + "..." if PUSHPLUS_TOKEN else None,
    }


@app.get("/")
async def root():
    return {
        "service": "PushPlus Bridge for Alertmanager",
        "version": "0.1.0",
        "endpoint": "POST /pushplus (alertmanager webhook_configs target)",
        "docs": "https://www.pushplus.plus/doc/",
    }


# ---------- Main ----------
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=BRIDGE_HOST)
    parser.add_argument("--port", type=int, default=BRIDGE_PORT)
    args = parser.parse_args()

    import uvicorn

    print(f"🚀 pushplus_bridge starting on http://{args.host}:{args.port}", file=sys.stderr)
    print(f"   PUSHPLUS_TOKEN: {PUSHPLUS_TOKEN[:8]}...", file=sys.stderr)
    print(f"   Endpoint: POST http://{args.host}:{args.port}/pushplus", file=sys.stderr)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
