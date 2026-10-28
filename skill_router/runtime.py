#!/usr/bin/env python3
"""
skill_router/runtime.py — Skill Runtime 与 MCP 子进程管理器
======================================================

职责：
    1. 加载 skills/ 目录下所有 yaml skill
    2. 按需启动 MCP server 子进程（JSON-RPC over stdio）
    3. 提供统一的 execute_skill(name, inputs) 接口
    4. LLM 调用（如果 skill 配置了 llm）：通过 transformers + peft 加载 14B LoRA
    5. 无 LLM 时降级到纯工具/规则执行（如 indicator-match skill）

依赖：
    - pyyaml           (pip install pyyaml)
    - transformers + peft + accelerate   (项目 venv 已有)
    - pandas + numpy                    (项目 venv 已有)

不依赖任何 LLM 框架（LangChain / LlamaIndex），保持轻量。

用法：
    from skill_router.runtime import SkillRuntime
    rt = SkillRuntime(skills_dir=Path("skills"), mcp_dir=Path("mcp/servers"))
    rt.load_all_skills()
    result = rt.execute_skill("kline-analysis", {"code": "sh600000"})
    # 或纯工具调用（无 LLM）：
    result = rt.execute_skill("indicator-match", {"code": "sh600000"})
"""

import contextlib
import json
import logging
import re
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import yaml

# === 路径与日志 =============================================================
LOG_FILE = Path("/home/jiuben/tdx-data-feed/logs/skill_router.log")
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    filename=str(LOG_FILE), level=logging.INFO,
    format="%(asctime)s [agents:runtime] %(message)s",
)
log = logging.getLogger("skill_router.runtime")


# === MCP server 名称 → 脚本映射 =============================================
# 与 skills/*.yaml 中 mcp:tdx-xxx 引用对应
SERVER_SCRIPT_MAP = {
    "tdx-parquet-reader": "parquet_reader.py",
    "tdx-calc": "calc.py",
    "tdx-vault": "vault.py",
    "tdx-news": "news.py",
    "tdx-backtest": "backtest.py",
    "tdx-finance-deep": "finance_deep.py",
}

# 财务指标别名 → finance.parquet 中的精确 metric
FINANCE_METRIC_MAP = {
    "revenue": "营业总收入",
    "net_profit": "归母净利润",
    "roe": "净资产收益率(ROE)",
    "gross_margin": "毛利率",
    "eps": "基本每股收益",
    "pe": "市盈率",
    "pb": "市净率",
}
VALUATION_METRICS = {"pe", "pb"}

# 财经七书讲义在 vault 中的一级目录（theory-reference 只读它，其余技能检索时排除它）
THEORY_CATEGORY = "09-理论讲义"


class McpClient:
    """单个 MCP server 子进程的 JSON-RPC 客户端（线程安全）"""
    def __init__(self, server_name: str, script_path: Path, verbose: bool = False):
        self.server_name = server_name
        self.script_path = script_path
        self.verbose = verbose
        self.proc: subprocess.Popen | None = None
        self._id = 0
        self._lock = threading.Lock()
        self._stderr_thread: threading.Thread | None = None

    def start(self) -> None:
        """启动 server 子进程"""
        if self.proc is not None:
            return
        if not self.script_path.exists():
            raise FileNotFoundError(f"MCP server 脚本不存在：{self.script_path}")
        self.proc = subprocess.Popen(
            [sys.executable, "-u", str(self.script_path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        # 启 stderr 读取线程（避免阻塞）
        self._stderr_thread = threading.Thread(target=self._drain_stderr, daemon=True)
        self._stderr_thread.start()
        # initialize
        self._request({"method": "initialize", "params": {}})
        # initialized notification
        self._notify({"method": "notifications/initialized"})
        log.info(f"MCP server 启动：{self.server_name}")

    def _drain_stderr(self) -> None:
        try:
            for line in self.proc.stderr:
                line = line.rstrip()
                if line and self.verbose:
                    print(f"  [{self.server_name}] {line}", file=sys.stderr)
        except Exception as e:
            log.warning("读取 MCP stderr 失败: %s", e)

    def _request(self, payload: dict[str, Any], timeout: float = 30) -> dict[str, Any]:
        with self._lock:
            self._id += 1
            req = {"jsonrpc": "2.0", "id": self._id, **payload}
            try:
                self.proc.stdin.write(json.dumps(req, ensure_ascii=False) + "\n")
                self.proc.stdin.flush()
                line = self.proc.stdout.readline()
                if not line:
                    raise RuntimeError(f"MCP server {self.server_name} 已断开")
                return json.loads(line)
            except Exception as e:
                log.exception(f"MCP request 失败：{e}")
                raise

    def _notify(self, payload: dict[str, Any]) -> None:
        with self._lock:
            try:
                self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", **payload}) + "\n")
                self.proc.stdin.flush()
            except Exception as e:
                log.warning("写入 MCP stdin 失败: %s", e)

    def list_tools(self) -> list[dict[str, Any]]:
        resp = self._request({"method": "tools/list", "params": {}})
        return resp.get("result", {}).get("tools", [])

    def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        resp = self._request({
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        })
        if "error" in resp:
            raise RuntimeError(f"MCP error: {resp['error']}")
        content = resp.get("result", {}).get("content", [])
        if not content:
            return None
        text = content[0].get("text", "{}")
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"raw": text}

    def stop(self) -> None:
        if self.proc is not None:
            try:
                self.proc.stdin.close()
                self.proc.wait(timeout=2)
            except Exception:
                self.proc.kill()
            self.proc = None


class SkillRuntime:
    """skill 注册中心 + MCP 客户端池 + LLM 后端"""
    def __init__(self, skills_dir: Path, mcp_dir: Path,
                 model_path: Path | None = None,
                 base_model: str = "Qwen/Qwen3-14B",
                 verbose: bool = False):
        self.skills_dir = skills_dir
        self.mcp_dir = mcp_dir
        self.model_path = model_path
        self.base_model = base_model
        self.verbose = verbose

        self.skills: dict[str, dict[str, Any]] = {}
        self.mcp_clients: dict[str, McpClient] = {}
        self.llm = None
        self.tok = None

    # -----------------------------------------------------------------
    # Skill 加载
    # -----------------------------------------------------------------
    def load_all_skills(self) -> list[str]:
        """加载 skills_dir 下所有 *.yaml，返回加载的 skill 名"""
        loaded = []
        for path in sorted(self.skills_dir.glob("*.yaml")):
            try:
                skill = yaml.safe_load(path.read_text(encoding="utf-8"))
                name = skill.get("name")
                if not name:
                    continue
                self.skills[name] = skill
                loaded.append(name)
                if self.verbose:
                    print(f"[runtime] 加载 skill: {name}  ({path.name})")
            except Exception as e:
                log.warning(f"加载 {path} 失败：{e}")
        return loaded

    def list_skills(self) -> list[str]:
        return list(self.skills.keys())

    # -----------------------------------------------------------------
    # MCP 客户端管理
    # -----------------------------------------------------------------
    def _ensure_mcp(self, server_name: str) -> McpClient:
        if server_name in self.mcp_clients:
            return self.mcp_clients[server_name]
        script_name = SERVER_SCRIPT_MAP.get(server_name)
        if not script_name:
            raise ValueError(f"未知 MCP server：{server_name}")
        client = McpClient(server_name, self.mcp_dir / script_name, verbose=self.verbose)
        client.start()
        self.mcp_clients[server_name] = client
        return client

    def call_mcp(self, server: str, tool: str, arguments: dict[str, Any]) -> Any:
        client = self._ensure_mcp(server)
        return client.call_tool(tool, arguments)

    # -----------------------------------------------------------------
    # 财务取数（纯工具与 LLM 路径共用）
    # -----------------------------------------------------------------
    def _finance_rows(self, code: str, metric: str, periods: int) -> tuple[list[dict], str]:
        """读财务指标。本地 core 300 无数据时回退到 tdx-finance-deep 按需拉取。"""
        source = "tdx-parquet-reader.read_finance"
        resp = self.call_mcp("tdx-parquet-reader", "read_finance",
                             {"code": code, "metric": metric, "periods": periods})
        rows = resp.get("data", []) if isinstance(resp, dict) else []
        if not rows:
            try:
                deep = self.call_mcp("tdx-finance-deep", "get_metrics",
                                     {"code": code, "metrics": [metric],
                                      "periods": periods})
                deep_rows = []
                for item in (deep or {}).get("data", []):
                    for v in item.get("values", []):
                        if item.get("metric") == metric and v.get("value") is not None:
                            deep_rows.append({
                                "report_date": v.get("period_end"),
                                "option": item.get("option"),
                                "metric": item.get("metric"),
                                "value": v.get("value"),
                            })
                if deep_rows:
                    rows = deep_rows
                    source = "tdx-finance-deep.get_metrics"
            except Exception as e:
                log.warning("finance-deep 回退失败 %s/%s：%s", code, metric, e)
        return sorted(rows, key=lambda r: str(r.get("report_date", "")), reverse=True), source

    @staticmethod
    def _ttm_eps(rows: list[dict]) -> tuple[float | None, str | None]:
        """用累计 EPS 推导 TTM：上年年报 - 上年同期 + 本期。"""
        if not rows:
            return None, None
        latest = rows[0]
        rd = str(latest.get("report_date", ""))
        if len(rd) != 8 or latest.get("value") is None:
            return None, rd or None
        if rd.endswith("1231"):
            return float(latest["value"]), rd
        year, mmdd = int(rd[:4]), rd[4:]
        prev_annual = next(
            (r for r in rows if str(r.get("report_date")) == f"{year - 1}1231"), None)
        prev_same = next(
            (r for r in rows if str(r.get("report_date")) == f"{year - 1}{mmdd}"), None)
        if prev_annual is None or prev_same is None:
            return None, rd
        return (float(prev_annual["value"]) + float(latest["value"])
                - float(prev_same["value"])), rd

    def _build_finance_payload(self, code: str, metric_key: str,
                               periods: int = 8) -> dict[str, Any]:
        metric_key = metric_key or "revenue"
        metric = FINANCE_METRIC_MAP.get(metric_key, metric_key)

        if metric_key in VALUATION_METRICS:
            price_resp = self.call_mcp(
                "tdx-parquet-reader", "read_daily_kline", {"code": code, "limit": 1})
            kline = price_resp.get("data", []) if isinstance(price_resp, dict) else []
            if not kline:
                raise ValueError(f"{code} 无日 K 数据，无法计算 {metric_key.upper()}")
            price = float(kline[-1]["close"])
            price_date = str(kline[-1]["date"])
            if metric_key == "pe":
                rows, fin_src = self._finance_rows(code, "基本每股收益", periods)
                basis, report_date = self._ttm_eps(rows)
                if basis is None:
                    raise ValueError(f"{code} 缺少上年年报/上年同期 EPS，无法计算 TTM PE")
                value = price / basis if basis > 0 else None
                note = f"TTM EPS={basis:.4f}，价格={price:.2f}（{price_date}）"
                if value is None:
                    note += "；EPS≤0，PE 不适用"
            else:
                rows, fin_src = self._finance_rows(code, "每股净资产", 1)
                basis = float(rows[0]["value"]) if rows and rows[0].get("value") is not None else None
                report_date = str(rows[0]["report_date"]) if rows else None
                if basis is None or basis <= 0:
                    raise ValueError(f"{code} 缺少有效每股净资产，无法计算 PB")
                value = price / basis
                note = f"每股净资产={basis:.4f}（{report_date}），价格={price:.2f}（{price_date}）"
            values = [{
                "period_end": report_date,
                "value": round(value, 4) if value is not None else None,
                "yoy_pct": None,
                "price_date": price_date,
            }]
            return {
                "code": code, "metric": metric, "metric_key": metric_key,
                "values": values, "latest_value": values[0]["value"],
                "latest_period": report_date, "yoy_pct": None,
                "summary": (f"{code} {metric_key.upper()}："
                            f"{values[0]['value'] if value is not None else '不适用'}（{note}）"),
                "note": "PE/PB 由最新收盘价与财务指标计算，非本地财务表原始字段",
                "finance_source": fin_src,
                "tools_used": [fin_src, "tdx-parquet-reader.read_daily_kline"],
            }

        rows, fin_src = self._finance_rows(code, metric, periods)
        if not rows:
            raise ValueError(f"{code} 无 '{metric}' 财务数据")
        by_date = {str(r.get("report_date")): r for r in rows}
        values = []
        for r in rows:
            rd = str(r.get("report_date", ""))
            val = r.get("value")
            if val is None or len(rd) != 8:
                continue
            prev = by_date.get(f"{int(rd[:4]) - 1}{rd[4:]}")
            yoy = None
            if prev is not None and prev.get("value") not in (None, 0):
                prev_val = float(prev["value"])
                if prev_val > 0:
                    yoy = (float(val) / prev_val - 1) * 100
            values.append({
                "period_end": rd,
                "option": r.get("option"),
                "value": float(val),
                "yoy_pct": round(yoy, 2) if yoy is not None else None,
            })
        if not values:
            raise ValueError(f"{code} 的 '{metric}' 无有效数值")
        latest = values[0]
        yoy = latest["yoy_pct"]
        if yoy is None:
            trend = "无同期可比数据"
        elif yoy > 5:
            trend = "增长"
        elif yoy < -5:
            trend = "下降"
        else:
            trend = "持平"
        yoy_text = f"{yoy:+.1f}%" if yoy is not None else "N/A"
        return {
            "code": code, "metric": metric, "metric_key": metric_key,
            "values": values, "latest_value": latest["value"],
            "latest_period": latest["period_end"], "yoy_pct": yoy,
            "summary": (f"{code} {metric}：最新 {latest['value']:.4g}"
                        f"（{latest['period_end']}），同比 {yoy_text}，趋势 {trend}"),
            "note": "同一报告期只保留一个口径；同比按去年同月同日报告期计算",
            "finance_source": fin_src,
            "tools_used": [fin_src],
        }

    # -----------------------------------------------------------------
    # LLM 加载（懒加载）
    # -----------------------------------------------------------------
    def _ensure_llm(self) -> bool:
        """加载 base + LoRA（仅在第一次需要 LLM 时调用）"""
        import os
        # Ollama/HTTP 后端：复用外部已启动的 Ollama 兼容服务（如本机 11434）
        # 通过环境变量切换，不影响默认的本地 transformers+peft 路径
        if os.getenv("TDX_LLM_BACKEND", "local").lower() == "ollama":
            self.llm = "ollama"
            self.ollama_url = os.getenv("TDX_OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
            log.info(f"LLM 后端=ollama，复用 {self.ollama_url}（不本地加载权重）")
            return True
        if self.llm is not None:
            return True
        try:
            import torch
            from peft import PeftModel
            from transformers import AutoModelForCausalLM, AutoTokenizer
        except ImportError:
            log.warning("transformers / peft 未装，无法加载 LLM")
            return False

        if not self.model_path or not self.model_path.exists():
            log.warning(f"LoRA checkpoint 不存在：{self.model_path}，将走纯工具/规则路径")
            return False

        log.info(f"加载 LLM: base={self.base_model} adapter={self.model_path}")
        try:
            self.tok = AutoTokenizer.from_pretrained(self.base_model, trust_remote_code=True)
            if self.tok.pad_token is None:
                self.tok.pad_token = self.tok.eos_token
            base = AutoModelForCausalLM.from_pretrained(
                self.base_model,
                torch_dtype=torch.bfloat16,
                device_map="auto",
                trust_remote_code=True,
            )
            self.llm = PeftModel.from_pretrained(base, str(self.model_path), torch_dtype=torch.bfloat16)
            self.llm.eval()
            return True
        except Exception as e:
            log.exception(f"LLM 加载失败：{e}")
            return False

    # -----------------------------------------------------------------
    # Skill 执行主入口
    # -----------------------------------------------------------------
    def execute_skill(self, skill_name: str, inputs: dict[str, Any]) -> dict[str, Any]:
        """统一入口：执行 skill"""
        if skill_name not in self.skills:
            return {"ok": False, "error": f"未知 skill：{skill_name}"}
        skill = self.skills[skill_name]

        # 1. 优先：调 LLM
        if (
            skill.get("llm", {}).get("model_id")
            and skill["llm"]["model_id"] != "none"
            and self._ensure_llm()
        ):
            return self._execute_with_llm(skill, inputs)

        # 2. 降级：纯工具/规则
        return self._execute_pure_tools(skill, inputs)

    # -----------------------------------------------------------------
    # 纯工具/规则执行（无 LLM）
    # -----------------------------------------------------------------
    def _execute_pure_tools(self, skill: dict[str, Any], inputs: dict[str, Any]) -> dict[str, Any]:
        """纯 MCP 工具调用 + 规则计算（适用于 adj-factor / indicator-match 等）"""
        name = skill["name"]
        code = inputs.get("code")
        out = {"ok": True, "skill": name, "code": code, "tools_used": []}

        try:
            # adj-factor：纯计算（适配 xdxr.parquet 汇总格式）
            if name == "adj-factor":
                if not code:
                    return {"ok": False, "error": "adj-factor 需要 code"}
                xdxr_resp = self.call_mcp("tdx-parquet-reader", "read_xdxr", {"code": code})
                rows = xdxr_resp.get("data", [])
                if not rows:
                    out["ok"] = True
                    out["fwd_factor"] = 1.0
                    out["bwd_factor"] = 1.0
                    out["n_events"] = 0
                    out["summary"] = f"{code} 无 xdxr 汇总数据（可能新股或未分红）"
                    out["tools_used"] = ["tdx-parquet-reader.read_xdxr"]
                    return out
                row = rows[0]
                # xdxr 实际字段：累计股息、年均股息、分红次数、融资总额、融资次数
                cum_div = float(row.get("累计股息", 0) or 0)  # 元/股累计
                avg_div = float(row.get("年均股息", 0) or 0)
                div_count = int(row.get("分红次数", 0) or 0)
                float(row.get("融资总额", 0) or 0)
                # 取最新股价做"股息率"估算
                kline = self.call_mcp("tdx-parquet-reader", "read_daily_kline",
                                      {"code": code, "limit": 1})
                cur_price = kline["data"][-1]["close"] if kline.get("data") else 1.0
                # 估算"等效除权"：累计除息相当于 fwd_factor *= 1 - 累计股息/当前价
                # 注：实际还有送转股配股，但 xdxr 汇总表未含 → fwd_factor 仅作下限
                fwd_factor = max(1.0 - cum_div / max(cur_price, 0.01), 0.1)
                bwd_factor = 1.0 / fwd_factor if fwd_factor > 0 else 1.0
                out.update({
                    "fwd_factor": round(fwd_factor, 4),
                    "bwd_factor": round(bwd_factor, 4),
                    "n_events": div_count,
                    "cum_dividend_yuan": cum_div,
                    "avg_dividend_yuan": avg_div,
                    "dividend_count": div_count,
                    "current_price": cur_price,
                    "summary": (f"{code}：累计派息 {cum_div:.2f} 元/股，"
                                f"共 {div_count} 次，按当前价 {cur_price:.2f} 元估算 "
                                f"前复权因子 ≈ {fwd_factor:.4f}"),
                    "note": "基于 xdxr 汇总表估算（无逐次事件明细），仅作下限参考",
                    "tools_used": ["tdx-parquet-reader.read_xdxr", "tdx-parquet-reader.read_daily_kline"],
                })
                return out

            # indicator-match：纯规则 + 计算
            if name == "indicator-match":
                if not code:
                    return {"ok": False, "error": "indicator-match 需要 code"}
                lookback = inputs.get("lookback", 60)
                kline_resp = self.call_mcp(
                    "tdx-parquet-reader", "read_daily_kline",
                    {"code": code, "limit": lookback + 30},
                )
                rows = kline_resp.get("data", [])
                if not rows:
                    return {"ok": False, "error": f"代码 {code} 无日 K 数据"}
                closes = [r["close"] for r in rows]
                highs = [r["high"] for r in rows]
                lows = [r["low"] for r in rows]
                macd = self.call_mcp("tdx-calc", "calc_macd", {"prices": closes})
                kdj = self.call_mcp("tdx-calc", "calc_kdj",
                                    {"high": highs, "low": lows, "close": closes})
                rsi = self.call_mcp("tdx-calc", "calc_rsi", {"prices": closes})
                # 加权打分
                weights = skill["decision_logic"]["weight_table"]
                thresholds = skill["decision_logic"]["threshold"]
                score = 0.0
                reasons = []
                # MACD 金叉/死叉（事件：1/-1/0）
                cross = macd.get("golden_cross") or []
                if cross and cross[-1] == 1:
                    score += weights["macd_golden_cross"]
                    reasons.append("MACD金叉")
                elif cross and cross[-1] == -1:
                    score += weights["macd_death_cross"]
                    reasons.append("MACD死叉")
                # KDJ
                j = kdj["j"][-1] if kdj.get("j") else None
                if j is not None and j < 20:
                    score += weights["kdj_j_oversold"]
                    reasons.append("KDJ超卖")
                if j is not None and j > 80:
                    score += weights["kdj_j_overbought"]
                    reasons.append("KDJ超买")
                # RSI
                rsi_last = rsi[-1] if rsi else None
                if rsi_last is not None and rsi_last < 30:
                    score += weights["rsi_oversold"]
                    reasons.append("RSI超卖")
                if rsi_last is not None and rsi_last > 70:
                    score += weights["rsi_overbought"]
                    reasons.append("RSI超买")
                # 价格 vs MA20
                ma20_resp = self.call_mcp("tdx-calc", "calc_ma",
                                          {"prices": closes, "window": 20})
                if closes[-1] > (ma20_resp[-1] or closes[-1]):
                    score += weights["price_above_ma20"]
                    reasons.append("站上MA20")
                else:
                    score += weights["price_below_ma20"]
                    reasons.append("跌破MA20")
                # 方向判断
                if score >= thresholds["strong_buy"]:
                    direction, label = 1, "强烈买入"
                elif score >= thresholds["buy"]:
                    direction, label = 1, "买入"
                elif score <= thresholds["strong_sell"]:
                    direction, label = -1, "强烈卖出"
                elif score <= thresholds["sell"]:
                    direction, label = -1, "卖出"
                else:
                    direction, label = 0, "中性"
                out.update({
                    "score": round(score, 2),
                    "direction": direction,
                    "label": label,
                    "reason": " + ".join(reasons) if reasons else "无明显信号",
                    "tools_used": [
                        "tdx-parquet-reader.read_daily_kline",
                        "tdx-calc.calc_macd", "tdx-calc.calc_kdj",
                        "tdx-calc.calc_rsi", "tdx-calc.calc_ma",
                    ],
                })
                return out

            # financial-summary（无 LLM 降级：直接展示数据，不摘要）
            if name == "financial-summary":
                if not code:
                    return {"ok": False, "error": "financial-summary 需要 code"}
                payload = self._build_finance_payload(
                    code, inputs.get("metric", "revenue"), int(inputs.get("periods", 8)))
                tools = payload.pop("tools_used")
                out.update(payload)
                out["tools_used"] = tools
                return out

            # financial-statement（纯工具：三大报表）
            if name == "financial-statement":
                if not code:
                    return {"ok": False, "error": "financial-statement 需要 code"}
                resp = self.call_mcp("tdx-finance-deep", "get_statement", {
                    "code": code,
                    "statement": inputs.get("statement", "balance"),
                    "periods": int(inputs.get("periods", 8)),
                    **({"fields": inputs["fields"]} if inputs.get("fields") else {}),
                })
                out.update(resp or {})
                out["tools_used"] = ["tdx-finance-deep.get_statement"]
                return out

            # backtest（纯工具：策略回测 / 显式信号回测）
            if name == "backtest":
                if not code:
                    return {"ok": False, "error": "backtest 需要 code"}
                if inputs.get("signals"):
                    resp = self.call_mcp("tdx-backtest", "run_signal_backtest", {
                        "code": code,
                        "signals": inputs["signals"],
                        "start": inputs.get("start"),
                        "end": inputs.get("end"),
                        "hold_days": int(inputs.get("hold_days", 5)),
                        "take_profit": inputs.get("take_profit"),
                        "stop_loss": inputs.get("stop_loss"),
                        "include_equity_curve": bool(inputs.get("include_equity_curve", False)),
                    })
                    used = "tdx-backtest.run_signal_backtest"
                else:
                    resp = self.call_mcp("tdx-backtest", "run_backtest", {
                        "code": code,
                        "strategy": inputs.get("strategy", "ma_cross"),
                        "start": inputs.get("start"),
                        "end": inputs.get("end"),
                        "params": inputs.get("params") or {},
                        "include_equity_curve": bool(inputs.get("include_equity_curve", False)),
                    })
                    used = "tdx-backtest.run_backtest"
                out.update(resp or {})
                out["tools_used"] = [used]
                return out

            # daban（纯工具：全市场次日涨停概率 Top-N）
            if name == "daban":
                resp = self.call_mcp("tdx-backtest", "daban_top_picks", {
                    "date": inputs.get("date"),
                    "top": int(inputs.get("top", 5)),
                    "exclude_limit_today": bool(inputs.get("exclude_limit_today", False)),
                    "min_amount": float(inputs.get("min_amount", 0) or 0),
                    "include_st": bool(inputs.get("include_st", False)),
                })
                out.update(resp or {})
                out["tools_used"] = ["tdx-backtest.daban_top_picks"]
                return out

            # news-summary（无 LLM 降级：直接返回新闻/公告条目，不做情感推断）
            if name == "news-summary":
                if not code:
                    return {"ok": False, "error": "news-summary 需要 code"}
                days = int(inputs.get("lookback_days", 7))
                limit = int(inputs.get("limit", inputs.get("top_k", 10)))
                raw_items, sources, errors = [], [], []
                try:
                    news = self.call_mcp("tdx-news", "search_stock_news",
                                         {"code": code, "days": days, "limit": limit})
                    raw_items += (news or {}).get("items", [])
                    sources.append("tdx-news.search_stock_news")
                    if (news or {}).get("error") and not (news or {}).get("items"):
                        errors.append(news["error"])
                except Exception as e:
                    errors.append(f"news: {e}")
                if inputs.get("include_announcements", True):
                    try:
                        ann = self.call_mcp("tdx-news", "search_announcements",
                                            {"code": code, "days": max(days, 30),
                                             "limit": limit})
                        raw_items += (ann or {}).get("items", [])
                        sources.append("tdx-news.search_announcements")
                        if (ann or {}).get("error") and not (ann or {}).get("items"):
                            errors.append(ann["error"])
                    except Exception as e:
                        errors.append(f"announcements: {e}")
                # 去重（新闻与公告可能重复同一标题）
                seen, items = set(), []
                for it in raw_items:
                    title = it.get("title") or ""
                    if not title or title in seen:
                        continue
                    seen.add(title)
                    body = it.get("content") or it.get("text") or title
                    items.append({
                        "title": title,
                        "date": it.get("publish_time"),
                        "source": it.get("source") or "",
                        "summary": body[:160],
                        "impact": "信息不足",
                        "kind": it.get("kind", "news"),
                        "url": it.get("url") or "",
                    })
                out["items"] = items[:limit]
                out["overall_sentiment"] = "信息不足" if not items else "neutral"
                if items:
                    shown = len(out["items"])
                    out["summary"] = (f"{code} 近 {days} 天检索到 {len(items)} 条新闻/公告"
                                      + (f"（返回 {shown} 条）" if shown < len(items) else ""))
                else:
                    out["summary"] = f"信息不足：{code} 近 {days} 天未检索到新闻/公告"
                if errors:
                    out["error"] = "；".join(errors[:4])
                out["note"] = "纯工具模式：仅罗列检索结果，不做情感判断"
                out["tools_used"] = sources
                return out

            # theory-reference（纯检索：财经七书讲义，限定 category=09-理论讲义）
            if name == "theory-reference":
                query = inputs.get("query")
                if not query:
                    return {"ok": False, "error": "theory-reference 需要 query"}
                book = inputs.get("book") or "all"
                book_paths = skill.get("book_paths") or {}
                prefix = book_paths.get(book) or book_paths.get("all", THEORY_CATEGORY)
                resp = self.call_mcp("tdx-vault", "search", {
                    "query": query,
                    "top_k": int(inputs.get("top_k", 5)),
                    "category": THEORY_CATEGORY,
                    "path_prefix": prefix,
                }) or {}
                hits = [{
                    "title": (r.get("meta") or {}).get("title", ""),
                    "path": (r.get("meta") or {}).get("path", ""),
                    "score": r.get("score"),
                    "text": r.get("text", ""),
                    "related_skills": (r.get("meta") or {}).get("related_skills") or [],
                } for r in resp.get("results", [])]
                # 命中片段带出的技能去重汇总，供调用方把理论接到实盘数据上
                linked = list(dict.fromkeys(s for h in hits for s in h["related_skills"]))
                scope = "全部讲义" if book == "all" else f"《{book}》"
                out.update({
                    "query": query,
                    "book": book,
                    "hits": hits,
                    "related_skills": linked,
                    "n_matched": resp.get("n_matched", 0),
                    "mode": resp.get("mode"),
                    "summary": (f"「{query}」在{scope}命中 {len(hits)} 段讲义原文"
                                if hits else
                                f"信息不足：讲义中未检索到「{query}」（范围：{scope}）"),
                    "note": "纯工具模式：返回讲义原文片段与笔记路径，不做改写",
                    "tools_used": ["tdx-vault.search"],
                })
                if resp.get("hint"):
                    out["hint"] = resp["hint"]
                return out

            # 其他 skill：无 LLM 又无规则实现 → 报错
            return {"ok": False, "error": f"skill {name} 需要 LLM 但当前不可用，请用 indicator-match 替代"}

        except Exception as e:
            log.exception(f"执行 skill {name} 失败")
            out["ok"] = False
            out["error"] = str(e)
            return out

    # -----------------------------------------------------------------
    # LLM 执行（带工具调用）
    # -----------------------------------------------------------------
    def _execute_with_llm(self, skill: dict[str, Any], inputs: dict[str, Any]) -> dict[str, Any]:
        """调 LLM + 工具调用"""
        # 兜底默认值（防止 prompt template KeyError）
        inputs.setdefault("lookback", 20)
        inputs.setdefault("query", f"分析 {inputs.get('code', '该股')}")
        code = inputs.get("code")
        out = {"ok": True, "skill": skill["name"], "code": code, "tools_used": []}

        # 1. 准备 input：按 skill 语义调所需 MCP 工具
        #
        # 修复（2026-09-26）：此前无条件预读 K 线，并把 finance_data / news_items
        # 一并填成 K 线 JSON —— 导致 financial-summary 拿到行情数据、news-summary
        # 拿到 MA/MACD，两条意图的 prompt 上下文全是错的。现改为按 skill 分别取数。
        context_parts = []
        extra_vars: dict[str, Any] = {}
        name = skill["name"]

        if name == "financial-summary":
            try:
                payload = self._build_finance_payload(
                    code, inputs.get("metric", "revenue"), int(inputs.get("periods", 8)))
                extra_vars["finance_data"] = json.dumps(payload["values"], ensure_ascii=False)
                extra_vars["metric"] = payload["metric"]
                out["metric"] = payload["metric"]
                out["values"] = payload["values"]
                out["yoy_pct"] = payload["yoy_pct"]
                out["finance_note"] = payload["note"]
                out["tools_used"] += payload["tools_used"]
            except Exception as e:
                return {"ok": False, "skill": name, "code": code,
                        "error": f"财务数据不可用：{e}"}

        elif name == "news-summary":
            raw_items: list[dict] = []
            sources: list[str] = []
            errors: list[str] = []
            days = int(inputs.get("lookback_days", 7))
            top_k = int(inputs.get("limit", inputs.get("top_k", 8)))
            if code:
                try:
                    news = self.call_mcp("tdx-news", "search_stock_news",
                                         {"code": code, "days": days, "limit": top_k})
                    raw_items += (news or {}).get("items", [])
                    sources.append("tdx-news.search_stock_news")
                    if (news or {}).get("error") and not (news or {}).get("items"):
                        errors.append(news["error"])
                except Exception as e:
                    errors.append(f"news: {e}")
                if inputs.get("include_announcements", True):
                    try:
                        ann = self.call_mcp("tdx-news", "search_announcements",
                                            {"code": code, "days": max(days, 30),
                                             "limit": top_k})
                        raw_items += (ann or {}).get("items", [])
                        sources.append("tdx-news.search_announcements")
                        if (ann or {}).get("error") and not (ann or {}).get("items"):
                            errors.append(ann["error"])
                    except Exception as e:
                        errors.append(f"announcements: {e}")
            news_items = [{
                "text": it.get("text") or f"{it.get('title', '')}\n{it.get('content', '')}".strip(),
                "meta": {"source": it.get("source"), "date": it.get("publish_time"),
                         "url": it.get("url"), "kind": it.get("kind")},
            } for it in raw_items if it.get("title")]
            out["tools_used"] += sources
            if not news_items:
                # 外部新闻为空时才回退 StockVault，避免两路内容混杂
                try:
                    query = inputs.get("query") or f"{code} 最新 新闻 公告 政策"
                    hits = self.call_mcp("tdx-vault", "search",
                                         {"query": query, "top_k": top_k,
                                          "exclude_category": THEORY_CATEGORY})
                    docs = (hits or {}).get("results", []) if isinstance(hits, dict) else []
                    news_items = [{"text": d.get("text", ""), "meta": d.get("meta", {})}
                                  for d in docs]
                    out["tools_used"].append("tdx-vault.search")
                except Exception as e:
                    errors.append(f"vault: {e}")
            extra_vars["news_items"] = (
                json.dumps(news_items, ensure_ascii=False) if news_items else
                "（未检索到个股新闻/公告，StockVault 也无相关文档。请如实回复信息不足，不要编造新闻。）"
            )
            out["news_count"] = len(news_items)
            if errors:
                out["news_errors"] = errors[:4]

        elif code:
            # K 线类 skill（kline-analysis 等）：预读 K 线 + MA20 + MACD
            try:
                kline = self.call_mcp(
                    "tdx-parquet-reader", "read_daily_kline",
                    {"code": code, "limit": inputs.get("lookback", 20)},
                )
                context_parts.append(f"近 20 日 K 线：{json.dumps(kline['data'], ensure_ascii=False)}")
                out["tools_used"].append("tdx-parquet-reader.read_daily_kline")
                # 算 MA20 / MACD
                closes = [r["close"] for r in kline["data"]]
                ma20 = self.call_mcp("tdx-calc", "calc_ma",
                                     {"prices": closes, "window": 20})
                macd = self.call_mcp("tdx-calc", "calc_macd", {"prices": closes})
                context_parts.append(f"MA20 末值：{ma20[-1]:.2f}" if ma20[-1] else "MA20 末值：N/A")
                context_parts.append(f"MACD 最新：dif={macd['dif'][-1]:.3f} dea={macd['dea'][-1]:.3f}")
                out["tools_used"] += ["tdx-calc.calc_ma", "tdx-calc.calc_macd"]
            except Exception as e:
                context_parts.append(f"（数据读取失败：{e}）")

        # 2. 拼 prompt（覆盖所有 skill yaml 中可能的变量）
        tmpl = skill.get("prompt_template") or "### 指令:\n{instruction}\n\n### 输入:\n{input}\n\n### 回答:\n"
        try:
            prompt = tmpl.format(
                instruction=inputs.get("query", f"分析 {code}"),
                input="\n".join(context_parts) or json.dumps(extra_vars, ensure_ascii=False),
                lookback=inputs.get("lookback", 20),
                kline_json=context_parts[0] if context_parts else "",
                code=code or "",
                metric=extra_vars.get("metric", inputs.get("metric", "")),
                periods=inputs.get("periods", 8),
                finance_data=extra_vars.get(
                    "finance_data", context_parts[0] if context_parts else ""),
                news_items=extra_vars.get("news_items", "\n".join(context_parts)),
                query=inputs.get("query", ""),
            )
        except KeyError as e:
            # 还有未覆盖的变量，用通用兜底
            log.warning(f"prompt_template 变量缺失：{e}，用通用兜底")
            prompt = f"### 指令:\n{inputs.get('query', f'分析 {code}')}\n\n### 输入:\n" + "\n".join(context_parts) + "\n\n### 回答:\n"

        # 3. 调 LLM 推理
        if getattr(self, "llm", None) == "ollama":
            try:
                decoded = self._call_ollama(prompt, skill)
                decoded, flagged = self._guard_hallucination(name, decoded, extra_vars)
                out["llm_output"] = decoded
                if flagged:
                    out["hallucination_detected"] = True
                m = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', decoded, re.S)
                if m:
                    with contextlib.suppress(Exception):
                        out["parsed"] = json.loads(m.group(1))
                return out
            except Exception as e:
                return self._handle_llm_failure(skill, inputs, f"Ollama LLM 调用失败：{e}")
        import torch
        try:
            enc = self.tok(prompt, return_tensors="pt", truncation=True, max_length=2048).to(self.llm.device)
            with torch.no_grad():
                gen = self.llm.generate(
                    **enc,
                    max_new_tokens=skill["llm"].get("inference", {}).get("max_new_tokens", 256),
                    do_sample=False,
                    pad_token_id=self.tok.pad_token_id,
                )
            decoded = self.tok.decode(gen[0, enc["input_ids"].shape[1]:], skip_special_tokens=True)
            decoded, flagged = self._guard_hallucination(name, decoded, extra_vars)
            out["llm_output"] = decoded
            if flagged:
                out["hallucination_detected"] = True
            # 解析 alpaca JSON
            m = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', decoded, re.S)
            if m:
                with contextlib.suppress(Exception):
                    out["parsed"] = json.loads(m.group(1))
            return out
        except Exception as e:
            return self._handle_llm_failure(skill, inputs, f"LLM 推理失败：{e}")

    def _handle_llm_failure(self, skill: dict[str, Any], inputs: dict[str, Any],
                            error: str) -> dict[str, Any]:
        """只使用 skill 显式声明的 fallback；财务/新闻等 null fallback 必须报错。"""
        name = skill["name"]
        fb_name = skill.get("fallback_skill")
        log.warning("%s LLM 失败：%s（fallback=%s）", name, error, fb_name)
        if fb_name:
            fb = self.skills.get(fb_name)
            if not fb:
                return {"ok": False, "skill": name, "code": inputs.get("code"),
                        "error": f"{error}；声明的 fallback_skill={fb_name} 不存在"}
            result = self._execute_pure_tools(fb, inputs)
            result["fallback_from"] = name
            result["fallback_reason"] = error
            return result
        return {"ok": False, "skill": name, "code": inputs.get("code"), "error": error}

    def _guard_hallucination(self, name: str, text: str, extra_vars: dict) -> tuple[str, bool]:
        """financial-summary / news-summary 的幻觉兜底：
        若 LLM 输出包含行情/指标类字段，而输入数据本身不含这些字段，判定为编造，
        强制改写为规范的『信息不足』说明，避免不实内容外泄。"""
        if name not in ("financial-summary", "news-summary"):
            return text, False
        forbidden = [
            "最新价", "涨跌幅", "均线", "MACD", "KDJ", "RSI",
            "市盈率", "市净率", "总市值", "流通市值", "所属行业",
            "所属概念", "机构持仓", "成交量", "换手率", "振幅",
        ]
        ctx = json.dumps(extra_vars, ensure_ascii=False)
        hit = [w for w in forbidden if w in text and w not in ctx]
        if hit:
            kind = "财务" if name == "financial-summary" else "新闻"
            return (
                f"信息不足：输入中未提供任何{kind}相关字段"
                f"（检测到输出含未在输入出现的内容：{', '.join(hit)}），"
                f"不补充未提供的信息，无法生成{kind}摘要。",
                True,
            )
        return text, False

    def _resolve_ollama_model(self, skill: dict) -> str:
        """解析实际可用的 Ollama 模型名。

        优先级：TDX_OLLAMA_MODEL > skill.llm.ollama_model > 本机 tags 中优先 tdx/v7r。
        skill.llm.model_id 只是能力档位标注，不能直接当 Ollama tag 使用。
        """
        import os

        import requests
        requested = (os.getenv("TDX_OLLAMA_MODEL")
                     or skill.get("llm", {}).get("ollama_model")
                     or "").strip()
        try:
            resp = requests.get(f"{self.ollama_url}/api/tags", timeout=5)
            resp.raise_for_status()
            names = [m.get("name", "") for m in resp.json().get("models", [])
                     if m.get("name")]
        except Exception as e:
            log.warning("读取 Ollama 模型列表失败：%s", e)
            names = []
        if requested:
            if requested in names:
                return requested
            if f"{requested}:latest" in names:
                return f"{requested}:latest"
            log.warning("Ollama 模型 %s 不存在，改用本机可用模型", requested)
        preferred = [n for n in names if "tdx" in n.lower()]
        v7r = [n for n in preferred if "v7r" in n.lower() and "q8" in n.lower()]
        for group in (v7r, preferred, names):
            if group:
                return group[0]
        raise RuntimeError("Ollama 没有可用模型（/api/tags 为空）")

    def _call_ollama(self, prompt: str, skill: dict) -> str:
        """通过 Ollama 兼容 /api/generate 端点调用外部 LLM 服务（复用已启动的 11434 等）"""
        import requests
        max_tokens = int(skill.get("llm", {}).get("inference", {}).get("max_new_tokens", 256))
        model = self._resolve_ollama_model(skill)
        resp = requests.post(
            f"{self.ollama_url}/api/generate",
            json={
                "model": model,
                "prompt": prompt,
                "stream": False,
                "think": False,
                "options": {"num_predict": max_tokens, "temperature": 0},
            },
            timeout=180,
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"Ollama HTTP {resp.status_code}: {resp.text[:300]}")
        return resp.json().get("response", "")

    def shutdown(self) -> None:
        for c in self.mcp_clients.values():
            c.stop()
        self.mcp_clients.clear()
        log.info("SkillRuntime 已关闭")


# === 直接运行的调试入口 ====================================================
if __name__ == "__main__":
    rt = SkillRuntime(
        skills_dir=Path("skills"),
        mcp_dir=Path("mcp/servers"),
        verbose=True,
    )
    rt.load_all_skills()
    print(f"加载 {len(rt.list_skills())} 个 skill: {rt.list_skills()}")
    # 测试 indicator-match（无需 LLM）
    print("\n=== indicator-match 测试（sh600000）===")
    r = rt.execute_skill("indicator-match", {"code": "sh600000"})
    print(json.dumps(r, ensure_ascii=False, indent=2))
    # 测试 adj-factor
    print("\n=== adj-factor 测试（sh600519）===")
    r = rt.execute_skill("adj-factor", {"code": "sh600519"})
    print(json.dumps(r, ensure_ascii=False, indent=2))
    rt.shutdown()
