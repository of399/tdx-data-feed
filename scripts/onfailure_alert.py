#!/usr/bin/env python3
"""systemd `OnFailure=` 告警投递：把失败的单元转成 alertmanager 形状推给本地 pushplus 桥。

为什么需要它：`tdxfeed-sync` 挂了只留在 `logs/sync.err`，`predict-daily` 靠 `After=` 排在
它后面照样起跑；没人半夜翻 journal，所以「有日志」不等于「有告警」。

载荷用 alertmanager 的形状而不是自己定一套，是为了复用已经在跑的桥
（`v5/scripts/pushplus_bridge.py`，127.0.0.1:5002）——token 只在桥那边配一份，
这里不碰密钥，也不新增常驻进程。字段契约由 tests/test_onfailure_alert.py 拿桥自己的
转换函数验证。

冷却（`--cooldown`）是防刷屏：canary 每 5 分钟一次，同一单元不加节流会变成一小时 12 条推送。

用法：
    venv/bin/python scripts/onfailure_alert.py tdxfeed-sync.service --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_URL = os.environ.get("TDX_ALERT_URL", "http://127.0.0.1:5002/pushplus")
DEFAULT_STATE_DIR = ROOT / "logs" / ".onfailure"
ALERT_NAME = "TdxUnitFailed"
SHOW_PROPS = ("Result", "ActiveState", "SubState", "ExecMainCode", "ExecMainStatus")
_SHOW_KEYS = {
    "Result": "result",
    "ActiveState": "active_state",
    "SubState": "sub_state",
    "ExecMainCode": "exec_code",
    "ExecMainStatus": "status",
}


def parse_show(text: str) -> dict:
    """`systemctl show` 的 Key=Value → 小写键；认不出的键丢掉，别把无关状态带进推送。"""
    info: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if not sep:
            continue
        mapped = _SHOW_KEYS.get(key.strip())
        if mapped is not None:
            info[mapped] = value.strip()
    return info


def journal_tail_cmd(unit: str, lines: int = 8) -> list[str]:
    return ["journalctl", "--user", "-u", unit, "-n", str(lines), "--no-pager",
            "-o", "short-iso"]


def _run(argv: list[str]) -> str:
    """取状态是锦上添花，命令本身失败不能让告警发不出去。"""
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=10).stdout
    except (subprocess.SubprocessError, OSError):
        return ""


def collect_state(unit: str, lines: int = 8) -> tuple[dict, list[str]]:
    show = ["systemctl", "--user", "show", unit] + [f"-p{p}" for p in SHOW_PROPS]
    info = parse_show(_run(show))
    tail = [ln.strip() for ln in _run(journal_tail_cmd(unit, lines)).splitlines() if ln.strip()]
    return info, tail


def build_payload(unit: str, info: dict, log_tail: list[str], when: str) -> dict:
    """alertmanager 载荷：桥只读 labels/annotations/status/startsAt，多给的字段它忽略。"""
    summary = (f"{unit} 失败（result={info.get('result', '?')}, "
               f"status={info.get('status', '?')}）")
    description = [summary]
    if info.get("sub_state"):
        description.append(f"SubState={info['sub_state']} / ActiveState={info.get('active_state', '?')}")
    if log_tail:
        description.append("最近日志：")
        description.extend(log_tail)
    alert = {
        "status": "firing",
        "labels": {
            "alertname": ALERT_NAME,
            "service": unit,
            "severity": "critical",
        },
        "annotations": {
            "summary": summary,
            "description": "<br>".join(description),
        },
        "startsAt": when,
    }
    return {
        "version": "4",
        "status": "firing",
        "receiver": "pushplus",
        "commonLabels": dict(alert["labels"]),
        "alerts": [alert],
    }


def post_alert(payload: dict, url: str = DEFAULT_URL, timeout: int = 5) -> tuple[bool, str]:
    """投递失败只返回 (False, 原因)，不抛栈——OnFailure 单元自己崩在栈里更难查。"""
    request = urllib.request.Request(
        url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return True, resp.read().decode("utf-8", "replace")[:200]
    except urllib.error.HTTPError as exc:      # 桥在，但它拒了
        body = exc.read().decode("utf-8", "replace")[:200] if hasattr(exc, "read") else ""
        return False, f"HTTP {exc.code}: {body}"
    except Exception as exc:                   # URLError / 超时 / 桥没起
        return False, f"{type(exc).__name__}: {exc}"


def marker_path(state_dir: Path, unit: str) -> Path:
    return Path(state_dir) / f"{unit}.last"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="投递 systemd 单元失败告警到 pushplus 桥")
    ap.add_argument("unit", help="失败的 systemd 单元名（OnFailure 模板里的 %%i）")
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--lines", type=int, default=8, help="附带的日志行数")
    ap.add_argument("--when", default=None, help="告警起始时间，默认取当前本地时间")
    ap.add_argument("--cooldown", type=int, default=3600,
                    help="同一单元两次投递的最小间隔（秒），0 表示不节流")
    ap.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    ap.add_argument("--dry-run", action="store_true", help="只打印载荷，一个字节都不发")
    args = ap.parse_args(argv)

    info, tail = collect_state(args.unit, lines=args.lines)
    when = args.when or datetime.now().astimezone().isoformat(timespec="seconds")
    payload = build_payload(args.unit, info, tail, when=when)

    if args.dry_run:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    marker = marker_path(args.state_dir, args.unit)
    now = datetime.now().timestamp()
    if args.cooldown > 0 and marker.exists():
        gap = now - marker.stat().st_mtime
        if gap < args.cooldown:
            print(f"跳过重复告警 {args.unit}：距上次 {gap / 60:.0f} 分钟 < 冷却 "
                  f"{args.cooldown / 60:.0f} 分钟")
            return 0

    ok, detail = post_alert(payload, url=args.url)
    if ok:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()
    print(f"{'✓ 已投递' if ok else '✗ 投递失败'} {args.unit}: {detail[:160]}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
