"""reason 工具的超时预算要有功能保护（2026-10-25）。

`LLM_REASON_TIMEOUT_S=8` 是 mcp-llm-ollama 单元里两条真正生效的变量之一，而仓库里唯一
覆盖 reason 路径的用例默认被 `SKIP_REASON_TEST=1` 跳过（那条要真 GPU，一次 60~120 秒），
所以「预算到了就降级、不要干等」这个设计从来没人测过。数学服务那边就吃过冷启动顶穿
预算的亏。

这里用一个假 Ollama 把它测死：慢模型的回答远晚于预算，`reason_impl` 必须在预算处放手、
降级到 chat 模型，而不是等满服务端耗时。
"""
from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "v5" / "mcp_servers"))

import llm_ollama  # noqa: E402

SLOW_MODEL = "slow-reason-model"
FAST_MODEL = "fast-chat-model"
REASON_BUDGET_S = 1.0
SLOW_SERVER_DELAY_S = 4.0  # 远大于预算：降级必须是预算逼出来的，不是模型返回快
CHAT_BUDGET_S = 10.0  # 远大于 REASON 预算、小于服务端耗时，用于反证


class _StubHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        model = body["model"]
        self.server.seen.append(model)
        if model == SLOW_MODEL:
            time.sleep(SLOW_SERVER_DELAY_S)
        payload = json.dumps({"message": {"content": f"{model} answers"}}).encode()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass  # 客户端已到点撤了 —— 这正是本用例想看到的

    def log_message(self, *args):
        pass


def _serve():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _StubHandler)
    srv.daemon_threads = True
    srv.seen = []
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_reason_gives_up_at_its_own_budget_and_falls_back(monkeypatch):
    srv = _serve()
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}"
        monkeypatch.setattr(llm_ollama, "OLLAMA_URL", url)
        monkeypatch.setattr(llm_ollama, "REASON_TIMEOUT_S", REASON_BUDGET_S)
        monkeypatch.setattr(llm_ollama, "CHAT_TIMEOUT_S", CHAT_BUDGET_S)
        monkeypatch.setattr(llm_ollama, "_audit", lambda rec: None)

        t0 = time.time()
        result = asyncio.run(
            llm_ollama.reason_impl("x + 3 = 7，一行回答", model=SLOW_MODEL, fallback_model=FAST_MODEL)
        )
        elapsed = time.time() - t0
        seen = list(srv.seen)
    finally:
        srv.shutdown()
        srv.server_close()

    assert result["ok"] is True
    assert result.get("fallback") is True, f"慢模型没有被降级：{result}"
    assert result["model"] == FAST_MODEL
    # 若 reason 误用了 chat 预算，这里会等到 4s 拿到慢模型的答复、根本不走降级
    assert elapsed < SLOW_SERVER_DELAY_S, (
        f"用了 {elapsed:.1f}s，比服务端耗时 {SLOW_SERVER_DELAY_S}s 还长 —— "
        f"REASON 预算 ({REASON_BUDGET_S}s) 没起作用"
    )
    assert seen == [SLOW_MODEL, FAST_MODEL], f"请求顺序不对：{seen}"
