"""告警路由不许有「收不到」的落点。

2026-10-28 之前两份静默失败：
- `default` receiver 一个 integration 都没有：没打 severity 标签的告警路由过去就被丢掉；
- `slack-warnings` 配的是未注册的 Slack 占位 URL，每次投递 404。

这些都是「规则写了等于没写」，而 alertmanager 自己只在日志里报投递失败。这里用纯配置
检查锁住：每条 route 的 receiver 必须存在，每个 receiver 必须至少有一个真实通道，
且通道 URL 不能是占位域名。
"""
from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "v5" / "monitoring" / "alertmanager.yml"

PLACEHOLDER_MARKERS = ("example.com", "hooks.slack.com/services/T1234", "<secret>")


def _cfg() -> dict:
    return yaml.safe_load(CONFIG.read_text(encoding="utf-8"))


def _routes(route: dict):
    yield route
    for child in route.get("routes", []) or []:
        yield from _routes(child)


def _channels(receiver: dict) -> list[dict]:
    out = []
    for key, value in receiver.items():
        if key.endswith("_configs") and value:
            out.extend(value)
    return out


def test_every_route_points_at_a_defined_receiver():
    cfg = _cfg()
    names = {r["name"] for r in cfg["receivers"]}
    for route in _routes(cfg["route"]):
        assert route["receiver"] in names, f"路由到未定义的 receiver: {route['receiver']}"


def test_every_receiver_has_a_real_channel():
    empty = [r["name"] for r in _cfg()["receivers"] if not _channels(r)]
    assert not empty, f"这些 receiver 没有任何 integration，告警路由过去等于丢弃: {empty}"


def test_no_placeholder_endpoints():
    bad = []
    for r in _cfg()["receivers"]:
        for ch in _channels(r):
            target = str(ch.get("url") or ch.get("api_url") or "")
            if any(m in target for m in PLACEHOLDER_MARKERS):
                bad.append(f"{r['name']} → {target}")
    assert not bad, f"占位地址永远投不出去（自称告警的静默失败）: {bad}"
