"""教师模型坏了必须**响**：call() 不能重试三次后 return ""，main() 不能把空 output 写进语料还退出 0。

要抓的破坏（三处，任意一处被改坏就应当有测试变红）：
1. `call()` 的最终失败路径 return ""——调用方无法区分「教师死了」和「教师答了个空」。
2. `main()` 把 `output` 原样写进 jsonl 并 print("DISTILL_DONE") 后正常退出——于是 7,983 条
   规模的训练集可以整片变成空字符串，而退出码是 0，调度器与人都看不见。
3. 反向：正常答案仍要写进去、仍要退出 0——修「响」不能把好的运行一起打死。

背景（2026-10-24 核实）：`scripts/distill_outputs.py` 的失败路径就是 1+2；教师 tag 写死那段
已经由 `TDX_DISTILL_MODEL` 守卫堵掉最常见的一种触发方式，但「装了却坏了的模型」「超时」
「服务端中途重启」走的仍是同一条静默路径。

测试不打桩：起一个真的本地 HTTP 服务扮演 Ollama 的 /api/generate，让 urlopen 走真 socket。
输出文件一律落在 tmp_path，绝不碰 data/distill/full.jsonl（那是 systemd 定时器的生产产物）。
"""

import http.server
import json
import subprocess
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "distill_outputs.py"

ONE_ROW = [{"instruction": "给出涨停概率", "input": "600519", "output": ""}]


class _Handler(http.server.BaseHTTPRequestHandler):
    """按类属性决定这一路服务怎么应答：状态码 + 原样回写的 JSON 体。"""

    status = 500
    body = b"{}"
    hits = 0
    prompts = set()

    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler 的命名
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        type(self).hits += 1
        try:
            type(self).prompts.add(json.loads(raw).get("prompt"))
        except Exception:
            pass
        self.send_response(self.status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *args):  # 静音，别污染 pytest 输出
        pass


def _serve(handler):
    srv = http.server.HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _run(tmp_path, port, extra_env=None, rows=None, limit=0, conc=8):
    """argv 位置是 (skeleton, out, LIMIT, CONC) —— 少传一个就会静默改变行为。"""
    sk = tmp_path / "skeleton.jsonl"
    sk.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in (rows or ONE_ROW)) + "\n",
        encoding="utf-8",
    )
    out = tmp_path / "out.jsonl"
    env = {
        "PATH": "/usr/bin:/bin",
        "TDX_OLLAMA_URL": f"http://127.0.0.1:{port}",
        "TDX_DISTILL_MODEL": "ghost-teacher:latest",
        "NUM_PREDICT": "16",
    }
    env.update(extra_env or {})
    argv = [sys.executable, str(SCRIPT), str(sk), str(out), str(limit), str(conc)]
    proc = subprocess.run(
        argv, cwd=ROOT, env=env, capture_output=True, text=True, timeout=120,
    )
    return proc, out


def _blank_rows(out):
    """落盘里 output 为空/缺失的行——这些行就是训练时的静默数据损毁。"""
    if not out.exists():
        return []
    return [json.loads(l) for l in out.read_text(encoding="utf-8").splitlines() if l.strip()]


def test_teacher_refusing_requests_exits_nonzero_and_names_the_model(tmp_path):
    """连续 500：必须非零退出，且 stderr 点名是哪个教师模型——「重试三次后算了」不算错误处理。"""

    class Failing(_Handler):
        status = 500

    srv = _serve(Failing)
    try:
        proc, out = _run(tmp_path, srv.server_address[1])
    finally:
        srv.shutdown()

    assert proc.returncode != 0, (
        f"教师全程拒绝请求却退出 0，这正是静默写空数据集的形状；stderr={proc.stderr[-400:]}"
    )
    assert "ghost-teacher:latest" in proc.stderr, (
        f"失败信息没点名教师模型，运维无从判断是模型没装好还是网络断了：stderr={proc.stderr[-400:]}"
    )


def test_empty_answer_is_never_written_as_a_training_row(tmp_path):
    """200 但 response 为空：不能落一条空 output，也不能报成功。"""

    class Blank(_Handler):
        status = 200
        body = json.dumps({"response": ""}).encode()

    srv = _serve(Blank)
    try:
        proc, out = _run(tmp_path, srv.server_address[1])
    finally:
        srv.shutdown()

    rows = _blank_rows(out)
    assert not any(not r.get("output") for r in rows), f"空 output 被写进了训练集：{rows}"
    assert proc.returncode != 0, (
        f"教师答了个空却 DISTILL_DONE 退出 0；stdout={proc.stdout[-300:]} stderr={proc.stderr[-300:]}"
    )


def test_abort_stops_burning_the_remaining_prompts(tmp_path):
    """中止必须是中止：教师死了，还在排队的 prompt 不该被提交上去撞重试。

    钉的是结构而不是计时。20 条 ÷ chunksize 4 = 5 块，CONC=2 → 2 块在飞、3 块排队。
    取消排队 ⇒ 服务端最多见到 2 个 prompt；不取消 ⇒ shutdown 等所有 future 跑完，20 个全撞上去。
    阈值取 4：两边都留了余量，且只有「真的没停」才能越线。
    """
    twenty = [{"instruction": f"题 {i}", "input": str(i), "output": ""} for i in range(20)]

    class Failing(_Handler):
        status = 500
        hits = 0
        prompts = set()

    srv = _serve(Failing)
    try:
        proc, out = _run(tmp_path, srv.server_address[1], rows=twenty, limit=0, conc=2)
    finally:
        srv.shutdown()

    served = {p for p in Failing.prompts if p}
    assert len(served) <= 4, (
        f"教师已死却仍把 {len(served)}/20 条 prompt 提交了上去（hits={Failing.hits}）——排队的工作没取消"
    )
    assert proc.returncode != 0, "剩余 prompt 仍在跑，退出码也不会是 0"


def test_a_real_answer_still_lands_and_exits_zero(tmp_path):
    """反向守卫：把失败路径改响，不能顺手打死正常运行（这条在修复前后都该是绿的）。"""

    class Working(_Handler):
        status = 200
        body = json.dumps({"response": "  涨停概率约 62%  "}).encode()

    srv = _serve(Working)
    try:
        proc, out = _run(tmp_path, srv.server_address[1])
    finally:
        srv.shutdown()

    assert proc.returncode == 0, f"正常应答却被判失败：stderr={proc.stderr[-400:]}"
    rows = _blank_rows(out)
    assert len(rows) == 1, f"正常答案应当写出 1 行，实际 {len(rows)} 行"
    assert rows[0]["output"] == "涨停概率约 62%", f"答案没被 clean() 去掉首尾空白：{rows[0]['output']!r}"
