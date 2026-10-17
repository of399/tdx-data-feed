#!/usr/bin/env python3
"""
训练实时监控服务（零依赖，Python 标准库实现）

功能：
  1. 实时展示训练进度：epoch / step / 完成百分比 / 已用时间 / 预计剩余时间 / loss
  2. 实时日志推送：按时间顺序追加，区分 INFO / WARN / ERROR 级别
  3. 前端支持：自动滚动（可暂停）、按级别 + 关键字筛选、状态横幅（运行中/已停止/挂起/异常）
  4. 响应式界面：移动端与桌面端自适应

数据源（自动探测，无需配置）：
  - <OUT>/trainer_log.jsonl        实时 JSON 进度（含 current_steps/percentage/elapsed_time/remaining_time）
  - <OUT>/train.log                训练 stdout（含 [INFO|...] 行 + tqdm 进度）
  - <OUT>/checkpoint-*/trainer_state.json  最新 checkpoint 状态（fallback）
  - <LOGS>/train_health.log        健康检查日志（每 5 分钟心跳）
  - 进程 `llamafactory-cli train`   存活检测

运行：
  python3 train_monitor.py            # 默认端口 7860
  PORT=9000 python3 train_monitor.py  # 自定义端口
"""
import json
import os
import re
import subprocess
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

# ====================== 配置 ======================
BASE = "/home/jiuben/tdx-data-feed"
OUT = os.path.join(BASE, "train", "output", "qwen3-14b-tdx-fast")
TRAIN_LOG = os.path.join(OUT, "trainer_log.jsonl")      # 实时 JSON 进度
TRAIN_STDOUT = os.path.join(OUT, "train.log")           # 训练 stdout
HEALTH_LOG = os.path.join(BASE, "logs", "train_health.log")
PORT = int(os.environ.get("PORT", "7860"))
POLL_INTERVAL = 2.0   # SSE 推送间隔（秒）
STALL_TIMEOUT = 300   # 日志超过此秒数未更新视为挂起
PROC_PATTERN = "llamafactory-cli train"

# 全局偏移：SSE 增量推送用（第一次连接时初始化为当前文件末尾，跳过历史）
_log_offsets = {}
_stream_ready = False
# 错误上下文缓存
_error_cache = {"ts": 0.0, "ctx": None}

# ====================== 工具函数 ======================
def ensure_stream_offset():
    """SSE 首次连接时，把偏移设为当前文件末尾，避免推送历史日志（历史由 /api/logs 回填）。"""
    global _stream_ready
    if not _stream_ready:
        for p in (TRAIN_STDOUT, HEALTH_LOG):
            if os.path.exists(p):
                try:
                    _log_offsets[p] = os.path.getsize(p)
                except OSError:
                    _log_offsets[p] = 0
        _stream_ready = True


def read_new_lines(path):
    """读取文件自上次偏移之后的新增内容（SSE 增量用）。"""
    if not os.path.exists(path):
        return []
    try:
        size = os.path.getsize(path)
    except OSError:
        return []
    offset = _log_offsets.get(path, 0)
    if offset > size:           # 文件被轮转/截断
        offset = 0
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            f.seek(offset)
            data = f.read()
            _log_offsets[path] = f.tell()
        return [l for l in data.splitlines() if l.strip()]
    except OSError:
        return []


def read_tail(path, n=2000):
    """读取文件末尾 n 行（初始化回填用，不更新全局偏移）。"""
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            lines = f.read().splitlines()
        return [l for l in lines if l.strip()][-n:]
    except OSError:
        return []


def get_file_mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def check_process():
    """训练进程是否存活。"""
    try:
        out = subprocess.run(["pgrep", "-f", PROC_PATTERN],
                             capture_output=True, text=True, timeout=5)
        return bool(out.stdout.strip())
    except Exception:
        return False


def parse_timestamp(line):
    """从日志行提取时间戳 [2026-09-18 08:05:27]。"""
    m = re.search(r"\[(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})\]", line)
    if m:
        return m.group(1)
    return time.strftime("%Y-%m-%d %H:%M:%S")


_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")
_TQDM_RE = re.compile(r"^\s*\d+%\|.*\|.*/\d+\s+\[")


def clean_line(line):
    """清理 ANSI 转义码与多余空白。"""
    return _ANSI_RE.sub("", line).strip()


def is_tqdm(line):
    """是否为 tqdm 进度条刷屏行（进度卡片已展示百分比，无需进日志）。"""
    return "it/s]" in line or bool(_TQDM_RE.match(line))


def line_to_entry(l, src):
    """将一行原始日志转为结构化 entry；tqdm/空行返回 None。"""
    cl = clean_line(l)
    if not cl or is_tqdm(cl):
        return None
    level = classify_level(cl)
    # health 源自身的 Traceback/异常（如 train_health.sh 的 KeyError）属于监控脚本噪声，
    # 并非训练任务错误，降级为 WARN 避免误导用户以为训练失败。
    if src == "health" and level == "ERROR":
        level = "WARN"
    return {"ts": parse_timestamp(cl), "level": level,
            "source": src, "msg": cl}


def classify_level(line):
    """判断日志级别 INFO / WARN / ERROR。"""
    low = line.lower()
    if "[error" in low or "[critical" in low:
        return "ERROR"
    if "traceback" in low or ("error" in low and "no error" not in low):
        return "ERROR"
    if "fail" in low or "异常" in low or "exception" in low:
        return "ERROR"
    if "[warning" in low or "[warn" in low or "warn" in low:
        return "WARN"
    return "INFO"


def get_last_jsonl():
    """读取 trainer_log.jsonl 最后一条合法 JSON。"""
    if not os.path.exists(TRAIN_LOG):
        return None
    try:
        with open(TRAIN_LOG, encoding="utf-8", errors="ignore") as f:
            lines = [l for l in f.read().splitlines() if l.strip()]
        for l in reversed(lines):
            try:
                return json.loads(l)
            except Exception:
                continue
    except OSError:
        return None
    return None


def detect_error():
    """检测 train.log 末尾是否出现错误/异常，返回上下文（带缓存，避免每 2 秒重扫）。"""
    now = time.time()
    if now - _error_cache["ts"] < 10 and _error_cache["ctx"] is not None:
        return _error_cache["ctx"]
    ctx = None
    try:
        lines = read_tail(TRAIN_STDOUT, 200)
        for i, l in enumerate(lines):
            if ("Traceback" in l or ("Error" in l and "no error" not in l.lower())
                    or "RuntimeError" in l or ("CUDA" in l and "out of memory" in l.lower())):
                ctx = "\n".join(lines[max(0, i - 3): i + 12])
                break
    except OSError:
        pass
    _error_cache["ts"] = now
    _error_cache["ctx"] = ctx
    return ctx


def get_progress():
    """聚合进度与状态。"""
    running = check_process()
    last = get_last_jsonl()
    prog = {
        "running": running,
        "step": None, "total_steps": 6756, "percentage": 0.0,
        "epoch": None, "elapsed": "—", "remaining": "—",
        "loss": None, "eval_loss": None, "lr": None,
        "status": "running", "message": None,
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    if last:
        prog.update({
            "step": last.get("current_steps"),
            "total_steps": last.get("total_steps", 6756),
            "percentage": round(last.get("percentage", 0), 2),
            "epoch": last.get("epoch"),
            "elapsed": last.get("elapsed_time"),
            "remaining": last.get("remaining_time"),
            "loss": last.get("loss"),
            "eval_loss": last.get("eval_loss"),
            "lr": last.get("lr"),
        })
    # 状态优先级：error > stopped > stalled > running
    err = detect_error()
    if err:
        prog["status"] = "error"
        prog["message"] = err
    elif not running:
        prog["status"] = "stopped"
        prog["message"] = f"训练进程已退出（未在进程列表中找到 '{PROC_PATTERN}'）。"
    else:
        mtime = max(get_file_mtime(TRAIN_STDOUT), get_file_mtime(TRAIN_LOG))
        if time.time() - mtime > STALL_TIMEOUT:
            prog["status"] = "stalled"
            prog["message"] = f"日志超过 {STALL_TIMEOUT} 秒未更新，训练可能挂起或磁盘写入阻塞。"
    return prog


def collect_logs_delta():
    """SSE 增量日志：仅返回自上次偏移后的新行（带级别/时间/源）。"""
    entries = []
    for path, src in ((TRAIN_STDOUT, "train"), (HEALTH_LOG, "health")):
        for l in read_new_lines(path):
            e = line_to_entry(l, src)
            if e:
                entries.append(e)
    entries.sort(key=lambda x: x["ts"])
    return entries


def collect_logs_tail(limit=2000, level="ALL", keyword=""):
    """初始化回填：读文件末尾，支持级别/关键字过滤。"""
    entries = []
    for path, src in ((TRAIN_STDOUT, "train"), (HEALTH_LOG, "health")):
        for l in read_tail(path, limit):
            e = line_to_entry(l, src)
            if e:
                entries.append(e)
    entries.sort(key=lambda x: x["ts"])
    if level != "ALL":
        entries = [e for e in entries if e["level"] == level]
    if keyword:
        kw = keyword.lower()
        entries = [e for e in entries if kw in e["msg"].lower()]
    return entries[-800:]


# ====================== 前端页面 ======================
HTML_PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=5.0">
<title>训练实时监控 · qwen3-14b-tdx</title>
<style>
  :root{
    --bg:#0d1117; --panel:#161b22; --border:#30363d; --text:#e6edf3;
    --muted:#8b949e; --accent:#58a6ff; --green:#3fb950; --amber:#d29922;
    --red:#f85149; --info:#8b949e; --shadow:0 4px 14px rgba(0,0,0,.4);
  }
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"PingFang SC","Microsoft YaHei",sans-serif;
    background:var(--bg);color:var(--text);line-height:1.5;padding:16px;max-width:1200px;margin:0 auto}
  h1{font-size:1.25rem;font-weight:600;display:flex;align-items:center;gap:10px;flex-wrap:wrap}
  h1 .sub{font-size:.8rem;color:var(--muted);font-weight:400}
  /* 状态横幅 */
  #statusBar{margin:14px 0;padding:10px 14px;border-radius:8px;font-size:.9rem;
    border:1px solid var(--border);display:flex;align-items:center;gap:10px;flex-wrap:wrap}
  #statusBar .dot{width:10px;height:10px;border-radius:50%;flex:none}
  .st-running{background:rgba(63,185,80,.12);border-color:var(--green)}
  .st-running .dot{background:var(--green);box-shadow:0 0 8px var(--green)}
  .st-stopped{background:rgba(248,81,73,.12);border-color:var(--red)}
  .st-stopped .dot{background:var(--red);box-shadow:0 0 8px var(--red)}
  .st-stalled{background:rgba(210,153,34,.12);border-color:var(--amber)}
  .st-stalled .dot{background:var(--amber);box-shadow:0 0 8px var(--amber)}
  .st-error{background:rgba(248,81,73,.15);border-color:var(--red)}
  .st-error .dot{background:var(--red);animation:pulse 1s infinite}
  @keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
  /* 进度条 */
  .progress-wrap{margin:14px 0}
  .progress-meta{display:flex;justify-content:space-between;font-size:.85rem;color:var(--muted);margin-bottom:6px}
  .progress-track{height:14px;background:#21262d;border-radius:8px;overflow:hidden;border:1px solid var(--border)}
  .progress-fill{height:100%;width:0;background:linear-gradient(90deg,#1f6feb,#58a6ff);
    transition:width .6s ease;border-radius:8px}
  /* 指标卡片 */
  .cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;margin:14px 0}
  .card{background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:12px 14px}
  .card .label{font-size:.72rem;color:var(--muted);text-transform:uppercase;letter-spacing:.04em}
  .card .value{font-size:1.15rem;font-weight:600;margin-top:4px;word-break:break-all}
  .card .value small{font-size:.7rem;color:var(--muted);font-weight:400}
  /* 日志面板 */
  .log-panel{background:var(--panel);border:1px solid var(--border);border-radius:10px;margin-top:16px;overflow:hidden}
  .log-toolbar{display:flex;gap:8px;flex-wrap:wrap;align-items:center;padding:10px 12px;
    border-bottom:1px solid var(--border);background:#0f141a}
  .log-toolbar select,.log-toolbar input{background:#21262d;color:var(--text);border:1px solid var(--border);
    border-radius:6px;padding:6px 8px;font-size:.82rem;outline:none}
  .log-toolbar input{flex:1;min-width:120px}
  .btn{background:#21262d;color:var(--text);border:1px solid var(--border);border-radius:6px;
    padding:6px 12px;font-size:.82rem;cursor:pointer;user-select:none;transition:.15s}
  .btn:hover{border-color:var(--accent)}
  .btn.active{background:var(--accent);color:#0d1117;border-color:var(--accent);font-weight:600}
  .spacer{flex:1}
  .log-count{font-size:.75rem;color:var(--muted)}
  #logBox{height:46vh;min-height:240px;overflow-y:auto;padding:8px 12px;font-family:"SF Mono",Menlo,Consolas,monospace;
    font-size:.78rem;line-height:1.55;background:#0a0d12}
  .log-line{display:flex;gap:8px;padding:1px 0;border-bottom:1px solid rgba(255,255,255,.02);white-space:pre-wrap;word-break:break-word}
  .log-line .ts{color:#6e7681;flex:none}
  .log-line .lvl{flex:none;width:48px;font-weight:700;text-align:center;border-radius:4px;padding:0 4px;font-size:.7rem}
  .log-line .src{color:#6e7681;flex:none}
  .log-line .msg{flex:1}
  .lvl-INFO{color:#8b949e;background:rgba(139,148,158,.12)}
  .lvl-WARN{color:#d29922;background:rgba(210,153,34,.15)}
  .lvl-ERROR{color:#f85149;background:rgba(248,81,73,.15)}
  .log-line.ERROR{background:rgba(248,81,73,.06)}
  .err-box{margin:10px 0;padding:10px 12px;background:rgba(248,81,73,.1);border:1px solid var(--red);
    border-radius:8px;font-family:monospace;font-size:.78rem;white-space:pre-wrap;color:#ffb3ae;max-height:200px;overflow:auto}
  .empty{color:var(--muted);text-align:center;padding:30px 0;font-size:.85rem}
  /* 移动端 */
  @media (max-width:640px){
    body{padding:10px}
    h1{font-size:1.05rem}
    .cards{grid-template-columns:repeat(2,1fr);gap:8px}
    .card .value{font-size:1rem}
    .log-toolbar{gap:6px}
    .log-toolbar input{min-width:90px}
    #logBox{height:52vh;font-size:.72rem}
  }
  /* 滚动条 */
  ::-webkit-scrollbar{width:8px;height:8px}
  ::-webkit-scrollbar-thumb{background:#30363d;border-radius:4px}
  ::-webkit-scrollbar-track{background:transparent}
</style>
</head>
<body>
  <h1>📊 训练实时监控 <span class="sub" id="modelName">qwen3-14b-tdx-fast</span></h1>

  <div id="statusBar" class="st-running">
    <span class="dot"></span>
    <span id="statusText">连接中…</span>
    <span id="statusMsg" style="color:var(--muted);font-size:.8rem"></span>
  </div>

  <div id="errBox" class="err-box" style="display:none"></div>

  <div class="progress-wrap">
    <div class="progress-meta">
      <span id="progLabel">进度 —</span>
      <span id="progPct">0%</span>
    </div>
    <div class="progress-track"><div class="progress-fill" id="progFill"></div></div>
  </div>

  <div class="cards" id="cards">
    <div class="card"><div class="label">Epoch</div><div class="value" id="cEpoch">—</div></div>
    <div class="card"><div class="label">Step</div><div class="value" id="cStep">—</div></div>
    <div class="card"><div class="label">完成度</div><div class="value" id="cPct">—</div></div>
    <div class="card"><div class="label">已用时间</div><div class="value" id="cElapsed">—</div></div>
    <div class="card"><div class="label">预计剩余</div><div class="value" id="cRemaining">—</div></div>
    <div class="card"><div class="label">Train Loss</div><div class="value" id="cLoss">—</div></div>
    <div class="card"><div class="label">Eval Loss</div><div class="value" id="cEval">—</div></div>
    <div class="card"><div class="label">Learning Rate</div><div class="value" id="cLr">—</div></div>
  </div>

  <div class="log-panel">
    <div class="log-toolbar">
      <button class="btn active" id="btnAuto">🔄 自动滚动</button>
      <button class="btn active" id="btnSmooth">🌊 平滑滚动</button>
      <select id="selLevel">
        <option value="ALL">全部级别</option>
        <option value="INFO">INFO</option>
        <option value="WARN">WARN</option>
        <option value="ERROR">ERROR</option>
      </select>
      <input id="inpKeyword" type="text" placeholder="按关键字筛选…" />
      <button class="btn" id="btnClear">清空</button>
      <span class="spacer"></span>
      <span class="log-count" id="logCount">0 条</span>
    </div>
    <div id="logBox"><div class="empty">等待日志…</div></div>
  </div>

<script>
(function(){
  var allLogs = [];          // 全量日志（上限 4000）
  var MAX = 4000;
  var autoScroll = true;     // 自动滚动开关
  var scrollSmooth = true;   // 平滑滚动开关（可配置）
  var filterLevel = "ALL";
  var keyword = "";
  var es = null;
  var seen = Object.create(null);  // 日志指纹去重（SSE 与轮询兜底共用）
  var seenArr = [];
  var lastSSE = 0;           // SSE 最近收到消息的时间戳
  var polling = false;       // 是否处于轮询兜底模式
  var pollTimer = null;

  var $ = function(id){ return document.getElementById(id); };

  function fmtNum(v, d){ if(v===null||v===undefined) return "—"; return Number(v).toFixed(d===undefined?4:d); }
  function fmtTime(v){ if(!v) return "—"; if(typeof v==="number") v=String(v); return v; }
  // 日志唯一指纹，用于增量推送/轮询去重，避免重复行
  function fp(e){ return e.source + "|" + e.ts + "|" + e.msg; }
  function markSeen(e){ var k=fp(e); if(!seen[k]){ seen[k]=1; seenArr.push(k); if(seenArr.length>MAX*3){ delete seen[seenArr.shift()]; } } }
  function isSeen(e){ return !!seen[fp(e)]; }

  // ---- 进度渲染 ----
  function renderProgress(p){
    $("cEpoch").innerHTML = p.epoch!=null ? fmtNum(p.epoch,3)+' <small>/ 3.0</small>' : "—";
    $("cStep").innerHTML = (p.step!=null ? p.step : "—") + ' <small>/ '+(p.total_steps||6756)+'</small>';
    $("cPct").textContent = p.percentage!=null ? p.percentage.toFixed(2)+"%" : "—";
    $("cElapsed").textContent = fmtTime(p.elapsed);
    $("cRemaining").textContent = fmtTime(p.remaining);
    $("cLoss").textContent = p.loss!=null ? fmtNum(p.loss,4) : "—";
    $("cEval").textContent = p.eval_loss!=null ? fmtNum(p.eval_loss,4) : "—";
    $("cLr").textContent = p.lr!=null ? p.lr.toExponential(2) : "—";
    var pct = p.percentage||0;
    $("progFill").style.width = pct+"%";
    $("progLabel").textContent = "进度 " + (p.step!=null?p.step:"?") + " / " + (p.total_steps||6756);
    $("progPct").textContent = pct.toFixed(1)+"%";

    // 状态横幅
    var bar = $("statusBar");
    bar.className = "st-"+p.status;
    var txt = {running:"🟢 训练中", stopped:"🔴 训练已停止", stalled:"🟠 训练挂起", error:"🔴 训练异常"}[p.status] || p.status;
    $("statusText").textContent = txt;
    $("statusMsg").textContent = p.message || ("更新于 "+p.updated_at);

    // 错误框
    var eb = $("errBox");
    if(p.status==="error" && p.message){
      eb.style.display="block"; eb.textContent = p.message;
    } else { eb.style.display="none"; }
  }

  // ---- 日志渲染 ----
  function matchesFilter(e){
    if(filterLevel!=="ALL" && e.level!==filterLevel) return false;
    if(keyword && e.msg.toLowerCase().indexOf(keyword.toLowerCase())<0) return false;
    return true;
  }
  function esc(s){ return String(s).replace(/[&<>]/g,function(c){return {"&":"&amp;","<":"&lt;",">":"&gt;"}[c];}); }
  function buildLine(e){
    var div = document.createElement("div");
    div.className = "log-line" + (e.level==="ERROR"?" ERROR":"");
    div.innerHTML = '<span class="ts">'+esc(e.ts)+'</span>'+
                    '<span class="lvl lvl-'+e.level+'">'+e.level+'</span>'+
                    '<span class="src">['+e.source+']</span>'+
                    '<span class="msg">'+esc(e.msg)+'</span>';
    return div;
  }
  function scrollToBottom(smooth){
    var box = $("logBox");
    if(smooth && scrollSmooth){ box.scrollTo({top: box.scrollHeight, behavior:"smooth"}); }
    else { box.scrollTop = box.scrollHeight; }
  }
  function renderLogs(){
    var box = $("logBox");
    var frag = document.createDocumentFragment();
    var shown = 0;
    for(var i=0;i<allLogs.length;i++){
      if(matchesFilter(allLogs[i])){ frag.appendChild(buildLine(allLogs[i])); shown++; }
    }
    box.innerHTML = "";
    if(shown===0){ box.innerHTML = '<div class="empty">无匹配日志</div>'; }
    else { box.appendChild(frag); }
    $("logCount").textContent = shown + " / " + allLogs.length + " 条";
    if(autoScroll){ scrollToBottom(false); }  // 重建后直接定位，避免抖动
  }
  function appendLogs(list){
    var added = 0;
    for(var i=0;i<list.length;i++){
      var e = list[i];
      if(isSeen(e)) continue;          // 去重：SSE 与轮询兜底不重复追加
      markSeen(e);
      allLogs.push(e);
      if(allLogs.length>MAX) allLogs.shift();
      added++;
    }
    if(added>0){
      renderLogs();
      if(autoScroll){ scrollToBottom(true); }  // 新内容平滑滚动到底部
    }
  }

  // ---- 初始化回填 ----
  function initLogs(){
    fetch("/api/logs?limit=2000").then(function(r){return r.json();}).then(function(d){
      allLogs = []; seenArr = []; for(var k in seen) delete seen[k];
      var arr = (d.logs||[]).slice(-MAX);
      for(var i=0;i<arr.length;i++){ markSeen(arr[i]); allLogs.push(arr[i]); }
      renderLogs();
    }).catch(function(e){ console.warn("init logs failed", e); });
  }

  // ---- SSE（主通道） ----
  function connect(){
    if(typeof EventSource === "undefined"){ startPolling(); return; }
    try{
      if(es) try{ es.close(); }catch(e){}
      es = new EventSource("/stream");
      es.onmessage = function(ev){
        lastSSE = Date.now();
        if(polling){ stopPolling(); }   // SSE 恢复，退出轮询兜底
        try{
          var data = JSON.parse(ev.data);
          if(data.progress) renderProgress(data.progress);
          if(data.logs && data.logs.length) appendLogs(data.logs);
        }catch(e){ console.warn(e); }
      };
      es.onerror = function(){
        // SSE 断开不立即重连：交给心跳检测，8s 无消息则启用轮询兜底
        if(!polling) $("statusText").textContent = "⚠️ SSE 中断，等待恢复/兜底…";
      };
    }catch(e){ startPolling(); }
  }

  // ---- 轮询兜底（SSE 不可用时保证页面自动更新） ----
  function pollOnce(){
    fetch("/api/progress").then(function(r){return r.json();}).then(renderProgress).catch(function(){});
    fetch("/api/logs?limit=120").then(function(r){return r.json();}).then(function(d){
      if(d.logs && d.logs.length) appendLogs(d.logs);
    }).catch(function(){});
  }
  function startPolling(){
    if(polling) return;
    polling = true;
    $("statusText").textContent = "🟡 实时（轮询兜底 · SSE 不可用）";
    pollOnce();
    pollTimer = setInterval(pollOnce, 2000);
  }
  function stopPolling(){
    if(!polling) return;
    polling = false;
    if(pollTimer){ clearInterval(pollTimer); pollTimer = null; }
  }
  // 心跳：SSE 超过 8 秒无消息则自动切换到轮询兜底
  setInterval(function(){
    if(es && !polling && Date.now() - lastSSE > 8000) startPolling();
  }, 2500);

  // ---- 控件 ----
  $("btnAuto").onclick = function(){
    autoScroll = !autoScroll;
    this.classList.toggle("active", autoScroll);
    if(autoScroll){ scrollToBottom(true); }
  };
  $("btnSmooth").onclick = function(){
    scrollSmooth = !scrollSmooth;
    this.classList.toggle("active", scrollSmooth);
  };
  $("selLevel").onchange = function(){ filterLevel = this.value; renderLogs(); };
  $("inpKeyword").oninput = function(){ keyword = this.value.trim(); renderLogs(); };
  $("btnClear").onclick = function(){ allLogs = []; renderLogs(); };
  $("logBox").onscroll = function(){
    var box = this;
    var atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 30;
    if(!atBottom && autoScroll){
      autoScroll = false; $("btnAuto").classList.remove("active");
    } else if(atBottom && !autoScroll){
      autoScroll = true; $("btnAuto").classList.add("active");
      if(scrollSmooth){ scrollToBottom(true); }
    }
  };

  // 启动
  initLogs();
  connect();
})();
</script>
</body>
</html>"""


# ====================== HTTP 服务 ======================
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)
        if path in ("/", "/index.html"):
            self._send(200, HTML_PAGE, "text/html; charset=utf-8")
        elif path == "/api/progress":
            self._send(200, json.dumps(get_progress(), ensure_ascii=False))
        elif path == "/api/logs":
            limit = int(qs.get("limit", ["2000"])[0])
            level = qs.get("level", ["ALL"])[0]
            keyword = qs.get("keyword", [""])[0]
            logs = collect_logs_tail(limit=limit, level=level, keyword=keyword)
            self._send(200, json.dumps({"logs": logs}, ensure_ascii=False))
        elif path == "/stream":
            self.serve_stream()
        else:
            self._send(404, json.dumps({"error": "not found"}))

    def serve_stream(self):
        # 关键修复：使用 HTTP/1.0 + Connection: close。
        # 浏览器/代理对 HTTP/1.1 且“无 Content-Length”的响应常做整响应缓冲，
        # 导致 SSE 消息必须等连接关闭才一次性交付（表现即“需手动刷新才更新”）。
        # HTTP/1.0 下客户端按 EOF 逐块流式读取，SSE 可实时交付。
        self.protocol_version = "HTTP/1.0"
        ensure_stream_offset()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "close")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            while True:
                try:
                    payload = {"progress": get_progress(), "logs": collect_logs_delta()}
                    self.wfile.write(("data: " + json.dumps(payload, ensure_ascii=False) + "\n\n").encode("utf-8"))
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    break
                time.sleep(POLL_INTERVAL)
        except Exception:
            pass

    def log_message(self, *args):
        pass  # 静默


def main():
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"训练监控已启动: http://0.0.0.0:{PORT}  (Ctrl+C 退出)")
    print("  数据源 OUT =", OUT)
    print("  健康检查日志 =", HEALTH_LOG)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在关闭…")
        server.shutdown()


if __name__ == "__main__":
    main()
