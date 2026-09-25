#!/usr/bin/env python3
"""
agents/runtime.py — Skill Runtime 与 MCP 子进程管理器
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
    from agents.runtime import SkillRuntime
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
LOG_FILE = Path("/home/jiuben/tdx-data-feed/logs/agents.log")
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    filename=str(LOG_FILE), level=logging.INFO,
    format="%(asctime)s [agents:runtime] %(message)s",
)
log = logging.getLogger("agents.runtime")


# === MCP server 名称 → 脚本映射 =============================================
# 与 skills/*.yaml 中 mcp:tdx-xxx 引用对应
SERVER_SCRIPT_MAP = {
    "tdx-parquet-reader": "parquet_reader.py",
    "tdx-calc": "calc.py",
    "tdx-vault": "vault.py",
}


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
        except Exception:
            pass

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
            except Exception:
                pass

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
    # LLM 加载（懒加载）
    # -----------------------------------------------------------------
    def _ensure_llm(self) -> bool:
        """加载 base + LoRA（仅在第一次需要 LLM 时调用）"""
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
                # MACD 金叉
                if macd["golden_cross"] and macd["golden_cross"][-1] == 1:
                    score += weights["macd_golden_cross"]
                    reasons.append("MACD金叉")
                if macd["golden_cross"] and len(macd["golden_cross"]) >= 2 and \
                   macd["golden_cross"][-2] == 1 and macd["golden_cross"][-1] == 0:
                    score += weights["macd_death_cross"]
                    reasons.append("MACD死叉")
                # KDJ
                if kdj["j"][-1] < 20:
                    score += weights["kdj_j_oversold"]
                    reasons.append("KDJ超卖")
                if kdj["j"][-1] > 80:
                    score += weights["kdj_j_overbought"]
                    reasons.append("KDJ超买")
                # RSI
                if rsi[-1] < 30:
                    score += weights["rsi_oversold"]
                    reasons.append("RSI超卖")
                if rsi[-1] > 70:
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
                metric_keyword = inputs.get("metric", "")
                # 中文指标名映射 → 实际 finance.parquet 中的 metric 字段
                metric_map = {
                    "revenue": "营业收入",
                    "net_profit": "归母净利润",
                    "roe": "净资产收益率",
                    "gross_margin": "毛利率",
                    "eps": "每股收益",
                    "pe": "市盈率",
                    "pb": "市净率",
                }
                m = metric_map.get(metric_keyword, metric_keyword or "归母净利润")
                resp = self.call_mcp(
                    "tdx-parquet-reader", "read_finance",
                    {"code": code, "metric": m, "periods": 8},
                )
                rows = resp.get("data", [])
                if not rows:
                    return {"ok": False, "error": f"{code} 无 '{m}' 财务数据"}
                # 按报告日期降序，整理成 values 列表
                rows = sorted(rows, key=lambda x: x.get("report_date", ""), reverse=True)[:8]
                values = [{
                    "period_end": r["report_date"],
                    "value": float(r.get("value", 0)),
                } for r in rows]
                # 算同比（period_end 同比去年同季度，简化：与 N 期前对比）
                if len(values) >= 2:
                    latest = values[0]["value"]
                    prev = values[min(4, len(values) - 1)]["value"]
                    yoy = (latest / prev - 1) * 100 if prev > 0 else 0
                else:
                    yoy = 0
                cur = values[0]["value"] if values else 0
                direction_word = "增长" if yoy > 5 else ("下降" if yoy < -5 else "持平")
                out.update({
                    "code": code,
                    "metric": m,
                    "metric_key": metric_keyword,
                    "values": values,
                    "summary": f"{code} {m}：最新 {cur:.4g}，同比 {yoy:+.1f}%，趋势 {direction_word}",
                    "note": "无 LLM，纯数据展示（LLM 上线后可生成自然语言摘要）",
                    "tools_used": ["tdx-parquet-reader.read_finance"],
                })
                return out

            # news-summary（无 LLM 降级：返回空 + 提示）
            if name == "news-summary":
                out["ok"] = True
                out["items"] = []
                out["overall_sentiment"] = "neutral"
                out["summary"] = f"{code} 新闻摘要：当前无外部新闻 API 接入（待 akshare 财新/同花顺接入或手动维护）"
                out["note"] = "无 LLM + 无外部新闻 API，需 LLM 上线或接入新闻源后可用"
                out["tools_used"] = []
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
        import torch
        # 兜底默认值（防止 prompt template KeyError）
        inputs.setdefault("lookback", 20)
        inputs.setdefault("query", f"分析 {inputs.get('code', '该股')}")
        code = inputs.get("code")
        out = {"ok": True, "skill": skill["name"], "code": code, "tools_used": []}

        # 1. 准备 input：调所需 MCP 工具
        context_parts = []
        # 预读 K 线（所有非 wrapper skill 都要）
        if code:
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
                input="\n".join(context_parts),
                lookback=inputs.get("lookback", 20),
                kline_json=context_parts[0] if context_parts else "",
                code=code or "",
                metric=inputs.get("metric", ""),
                periods=inputs.get("periods", 8),
                finance_data=context_parts[0] if context_parts else "",
                news_items="\n".join(context_parts),
                query=inputs.get("query", ""),
            )
        except KeyError as e:
            # 还有未覆盖的变量，用通用兜底
            log.warning(f"prompt_template 变量缺失：{e}，用通用兜底")
            prompt = f"### 指令:\n{inputs.get('query', f'分析 {code}')}\n\n### 输入:\n" + "\n".join(context_parts) + "\n\n### 回答:\n"

        # 3. 调 LLM 推理
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
            out["llm_output"] = decoded
            # 解析 alpaca JSON
            m = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', decoded, re.S)
            if m:
                with contextlib.suppress(Exception):
                    out["parsed"] = json.loads(m.group(1))
            return out
        except Exception as e:
            out["ok"] = False
            out["error"] = f"LLM 推理失败：{e}"
            # fallback 到 indicator-match
            log.warning(f"LLM 失败，降级到 indicator-match: {e}")
            fallback = skill.get("fallback_skill") or "indicator-match"
            return self._execute_pure_tools(self.skills[fallback], inputs)

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
