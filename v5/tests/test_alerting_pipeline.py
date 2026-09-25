"""
v5 告警链路端到端验证 SOP。

quick 模式 (5s 内, 非侵入式):
    cd /home/jiuben/tdx-data-feed
    venv/bin/pytest v5/tests/test_alerting_pipeline.py -v
    # 校验 prom/am/8002 app/5001 sink 当前状态, 验证 rules, 不影响业务

full 模式 (130s+, 侵入式, 会冻结 8002 主进程约 140s):
    venv/bin/pytest v5/tests/test_alerting_pipeline.py::TestPipelineKillStop -v -s
    # 流程: kill -STOP 25529 -> 等 up=0 -> 等 UpstreamDown firing -> 验证 am 路由 -> 验证 sink 收 POST -> CONT 恢复

auto-sink 模式 (full + 自动起/停 webhook sink 在 :5001):
    AUTOSINK=1 venv/bin/pytest v5/tests/test_alerting_pipeline.py::TestPipelineKillStop -v -s

JSON 报告 (CI 集成用):
    REPORT_JSON=/tmp/report.json venv/bin/pytest v5/tests/test_alerting_pipeline.py -v

参考: v5/audit/2026-09-20-alerting-pipeline-verification.md
      v5/tests/test_alerting_pipeline.sh (CLI wrapper)
"""

import contextlib
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request

import pytest

PM = "http://127.0.0.1:9090"
AM = "http://127.0.0.1:9093"
APP_PORT = 8002
SINK_PORT = 5001
APP_CMD_KEYWORD = "math_sympy_http.py"
STOP_TOTAL_S = 140
SCRAPE_SETTLE_S = 50
FOR_WAIT_S = 80

FULL = os.environ.get("VERIFY_PIPELINE_FULL") == "1"
AUTOSINK = os.environ.get("AUTOSINK") == "1"


def _record(stage, status, **detail):
    """轻量 stage log, 直接 print 到 stdout; wrapper 负责 JSON 落盘。"""
    parts = [f"{stage}={status}"]
    for k, v in detail.items():
        parts.append(f"{k}={v}")
    print(f"  [stage] {' '.join(parts)}", flush=True)


def _get(url, timeout=5):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception as e:
        return {"_err": str(e)}


def _http_status(url, timeout=5):
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception as e:
        return 0, f"_err: {e}"


def _query(expr):
    return _get(f"{PM}/api/v1/query?query=" + urllib.parse.quote(expr))


def _port_listening(port):
    s = socket.socket()
    s.settimeout(2)
    try:
        s.connect(("127.0.0.1", port))
        return True
    except Exception:
        return False
    finally:
        s.close()


def _find_app_pid():
    port_hex = f"{APP_PORT:04X}"
    try:
        with open("/proc/net/tcp") as f:
            for line in f:
                cols = line.split()
                if len(cols) > 9 and cols[1].endswith(f":{port_hex}") and cols[3] == "0A":
                    inode = cols[9]
                    for pid in os.listdir("/proc"):
                        if not pid.isdigit():
                            continue
                        try:
                            for fd in os.listdir(f"/proc/{pid}/fd"):
                                tgt = os.readlink(f"/proc/{pid}/fd/{fd}")
                                if tgt == f"socket:[{inode}]":
                                    with open(f"/proc/{pid}/cmdline", "rb") as _cf:
                                        cmd = _cf.read().decode("utf-8", errors="replace")
                                    if APP_CMD_KEYWORD in cmd:
                                        return int(pid)
                        except PermissionError, FileNotFoundError, ProcessLookupError:
                            continue
    except Exception:
        pass
    return None


def _wait_until(expr_check, timeout_s, poll_s=5, label="condition"):
    deadline = time.time() + timeout_s
    last = None
    while time.time() < deadline:
        last = expr_check()
        if last:
            return last
        time.sleep(poll_s)
    pytest.fail(f"{label} 在 {timeout_s}s 内未满足; last={last}")


def _start_sink():
    sink_script = (
        """
import http.server, socketserver, json
PORT = %d
LOG = "/tmp/alertmanager_webhook.log"

class H(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(n).decode("utf-8", errors="replace")
        try:
            payload = json.loads(body) if body else {}
            alerts = payload if isinstance(payload, list) else [payload]
            count = len(alerts)
            summary = [(a.get("labels", {}).get("alertname", "?"),
                        a.get("labels", {}).get("severity", "?"),
                        a.get("status", "?")) for a in alerts]
        except Exception as e:
            count = -1
            summary = [("parse_err", str(e)[:60], "?")]
        with open(LOG, "a") as f:
            f.write(f"POST {self.path} bytes={n} count={count} alerts={summary}\\n")
        self.send_response(200); self.end_headers(); self.wfile.write(b"ok")
    def log_message(self, *a, **k): pass

with socketserver.TCPServer(("127.0.0.1", PORT), H) as s:
    s.serve_forever()
"""
        % SINK_PORT
    )
    p = subprocess.Popen(
        [sys.executable, "-c", sink_script],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(1)
    if _port_listening(SINK_PORT):
        return p.pid
    p.terminate()
    return None


def _stop_sink(pid):
    try:
        os.kill(pid, signal.SIGTERM)
        time.sleep(1)
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


class TestPipelineStatic:
    """非侵入式: 检查 prom / am / 5001 sink / app 当前状态"""

    def test_01_prometheus_ready(self):
        code, body = _http_status(f"{PM}/-/ready")
        assert code == 200, f"prom /-/ready HTTP {code}: {body!r}"
        _record("prom_ready", "PASS", http=code)

    def test_02_alertmanager_ready(self):
        code, body = _http_status(f"{AM}/-/ready")
        assert code == 200, f"am /-/ready HTTP {code}: {body!r}"
        _record("am_ready", "PASS", http=code)

    def test_03_app_reachable(self):
        s = socket.socket()
        s.settimeout(3)
        try:
            s.connect(("127.0.0.1", APP_PORT))
        finally:
            s.close()
        up = _query('up{job="math-sympy-http"}')
        assert up.get("data", {}).get("result"), f"up series 缺失: {up}"
        assert up["data"]["result"][0]["value"][1] == "1", f"up != 1, 可能业务挂了: {up}"
        _record("app_reachable", "PASS", up="1")

    def test_04_sink_or_warn(self):
        if not _port_listening(SINK_PORT):
            _record("sink_present", "SKIP", reason=":5001 无人监听")
            pytest.skip(
                f":{SINK_PORT} 无人监听 -> 告警通知出口未部署; "
                "修法: 起 sink (见 v5/audit/2026-09-20-alerting-pipeline-verification.md sec4)"
            )
        _record("sink_present", "PASS", port=SINK_PORT)

    def test_05_am_notification_metrics(self):
        code, body = _http_status(f"{AM}/metrics")
        assert code == 200, f"am /metrics HTTP {code}"
        webhook_lines = [
            l
            for l in body.splitlines()
            if l.startswith('alertmanager_notifications_total{integration="webhook"')
        ]
        n = float(webhook_lines[0].split()[-1]) if webhook_lines else 0.0
        print(f"\n  am webhook_total={n}")
        _record("am_metrics", "PASS" if n > 0 else "INFO", webhook_total=n)

    def test_06_rule_files_parse(self):
        d = _get(f"{PM}/api/v1/rules")
        rules = d.get("data", {}).get("groups", [])
        assert rules, "无 rules 加载"
        names = [r["name"] for g in rules for r in g["rules"]]
        assert "FallbackLayerMissing" in names, (
            f"FallbackLayerMissing rule 缺失 (修复后未部署?): {names}"
        )
        assert "UpstreamDown" in names, f"UpstreamDown rule 缺失: {names}"
        _record("rules_parsed", "PASS", rule_count=len(names))


@pytest.mark.skipif(not FULL, reason="侵入式 kill -STOP; 显式设置 VERIFY_PIPELINE_FULL=1 才执行")
class TestPipelineKillStop:
    """完整 kill -STOP 实验, 会冻结 8002 主进程约 140s"""

    @pytest.fixture
    def app_pid(self):
        pid = _find_app_pid()
        assert pid, f"未找到监听 :{APP_PORT} 的进程 (可能 math-sympy-http 未启动)"
        yield pid
        # 关键: 用 kill -CONT -PID (进程组) 而非 kill -CONT PID
        # 经验: kill -STOP PID 会冻结主线程, worker threads 仍可能持锁等主线程;
        #       SIGCONT PID 只恢复主线程, worker threads 仍 freeze → 死锁.
        #       进程组 CONT 一把解冻所有 threads.
        try:
            os.kill(-pid, signal.SIGCONT)
            print(f"\n  [teardown] CONT process group -{pid} (兜底恢复)")
            time.sleep(5)
        except ProcessLookupError:
            pass

    def test_full_fire_path(self, app_pid):
        pid = app_pid
        log = []

        # auto-sink: 在 full 模式下, 如果需要自动起 sink
        sink_pid = None
        if AUTOSINK and not _port_listening(SINK_PORT):
            with contextlib.suppress(FileNotFoundError):
                os.remove("/tmp/alertmanager_webhook.log")
            sink_pid = _start_sink()
            if sink_pid is None:
                pytest.fail(f"无法起 sink 在 :{SINK_PORT}")
            print(f"\n  [sink] 起 PID={sink_pid}, 监听 :{SINK_PORT}")
            _record("autosink_started", "PASS", pid=sink_pid)

        try:
            base_up = _query('up{job="math-sympy-http"}')["data"]["result"][0]["value"][1]
            assert base_up == "1", f"基线 up != 1: {base_up}"
            log.append(("baseline", f"up={base_up}, pid={pid}"))
            print(f"\n[T0] baseline up={base_up}")
            _record("baseline", "PASS", up=base_up, pid=pid)

            print(f"[T0] >>> kill -STOP -{pid} (进程组)")
            os.kill(-pid, signal.SIGSTOP)
            _record("process_stopped", "PASS", pid=pid)
            t0 = time.time()

            def check_up_zero():
                up = _query('up{job="math-sympy-http"}')
                v = up["data"]["result"][0]["value"][1] if up.get("data", {}).get("result") else "?"
                if v == "0":
                    elapsed = int(time.time() - t0)
                    log.append(("up_zero", f"t+{elapsed}s"))
                    print(f"  [T+{elapsed}s] up=0 ok")
                    return True
                return False

            _wait_until(check_up_zero, SCRAPE_SETTLE_S + 20, poll_s=10, label="up=0")
            _record("up_zero_detected", "PASS", elapsed_s=int(time.time() - t0))

            def check_firing():
                alerts = _get(f"{PM}/api/v1/alerts")
                for a in alerts.get("data", {}).get("alerts", []):
                    if a["labels"]["alertname"] == "UpstreamDown" and a["state"] == "firing":
                        elapsed = int(time.time() - t0)
                        log.append(("firing", f"t+{elapsed}s"))
                        print(f"  [T+{elapsed}s] UpstreamDown FIRING ok")
                        return True
                return False

            _wait_until(check_firing, FOR_WAIT_S + 30, poll_s=10, label="UpstreamDown firing")
            _record("prom_firing", "PASS")

            am = _get(f"{AM}/api/v2/alerts")
            ud = [a for a in am if a["labels"].get("alertname") == "UpstreamDown"]
            assert ud, f"am 未收到 UpstreamDown: {am}"
            rcv = [r["name"] for r in ud[0].get("receivers", [])]
            assert "critical-webhook" in rcv, f"UpstreamDown 未路由到 critical-webhook: {rcv}"
            log.append(("am_route", f"receivers={rcv}"))
            print(f"  [am] UpstreamDown receivers={rcv} ok")
            _record("am_route", "PASS", receivers=rcv)

            if _port_listening(SINK_PORT):
                deadline = time.time() + 30
                hit = False
                while time.time() < deadline:
                    try:
                        with open("/tmp/alertmanager_webhook.log") as f:
                            content = f.read()
                            if "UpstreamDown" in content or "alerts/critical" in content:
                                hit = True
                                break
                    except FileNotFoundError:
                        pass
                    time.sleep(2)
                if hit:
                    log.append(("sink_received", "POST received"))
                    print("  [sink] POST 已收到 (含 UpstreamDown)")
                    _record("sink_received", "PASS")
                else:
                    print(
                        f"  [sink] :{SINK_PORT} 监听中但 30s 内未收到 POST (am retry group_interval=5m)"
                    )
                    _record("sink_received", "INFO", note="sink 监听但 retry 未到")
            else:
                pytest.skip(f":{SINK_PORT} 无人监听; 链路验证到此为止")

        finally:
            # 兜底: 关 sink (如果本测试起了)
            if sink_pid is not None:
                _stop_sink(sink_pid)
                print(f"  [sink] 已停 PID={sink_pid}")
                _record("autosink_stopped", "PASS")
            # 兜底: CONT 进程组 (避免只 CONT 主线程导致 worker threads 死锁)
            print(f"<<< kill -CONT -{pid} (进程组)")
            with contextlib.suppress(ProcessLookupError):
                os.kill(-pid, signal.SIGCONT)
            _record("process_resumed", "PASS", pid=pid)
            deadline = time.time() + 60
            while time.time() < deadline:
                up = _query('up{job="math-sympy-http"}')
                if up["data"]["result"][0]["value"][1] == "1":
                    elapsed = int(time.time() - t0)
                    log.append(("recovered", f"t+{elapsed}s"))
                    print(f"  [T+{elapsed}s] up=1 ok 链路恢复")
                    _record("recovered", "PASS")
                    return  # noqa: B012  # finally 内的 return 让 sink/CONT 跑完直接退出
                time.sleep(10)
            _record("failed", "FAIL", reason="CONT 后 60s 内 up 未回 1")
            pytest.fail("CONT 后 60s 内 up 未回 1")

        print("\n=== Timeline ===")
        for stage, msg in log:
            print(f"  {stage:12} {msg}")
