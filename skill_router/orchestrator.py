#!/usr/bin/env python3
"""
skill_router/orchestrator.py — L4 编排器（意图路由 + 多轮状态 + 串接多 Skill）
=========================================================================

职责：
    1. 接收用户自然语言查询
    2. 意图路由（基于规则 + 可选 LLM）
    3. 多轮对话状态管理
    4. 串接多个 Skill（复杂查询可能需多步）
    5. 把结果组装成最终回答

用法：
    from skill_router.orchestrator import Orchestrator
    orch = Orchestrator()
    print(orch.handle("sh600000 怎么看？"))

依赖：
    skill_router/runtime.py
"""

import re
import time
from pathlib import Path
from typing import Any

from skill_router.runtime import SkillRuntime

# === 意图路由规则 ===========================================================
INTENT_PATTERNS = [
    # 优先级从高到低
    (re.compile(r'财务|营收|净利润|ROE|毛利|EPS|pe|pb|市盈率|市净率|报表|资产负债|现金流'), "financial-summary"),
    (re.compile(r'复权|除权除息|前复权|后复权|送转|分红'), "adj-factor"),
    (re.compile(r'新闻|公告|政策|舆情|消息|利好|利空'), "news-summary"),
    (re.compile(r'打板|连板|首板|次日涨停|涨停候选|涨停股|接力板'), "daban"),
    (re.compile(r'指标|形态|金叉|死叉|锤子|十字星|超买|超卖|均线|MACD|KDJ|RSI'), "indicator-match"),
    # 默认：含代码 → 主分析
    (re.compile(r'((sh|sz|bj)\d{6})'), "kline-analysis"),
]

# === 理论讲义（财经七书）路由 ================================================
# 只在「查询里没有股票代码」且命中概念性提问词时才走 theory-reference，
# 否则「600519 的护城河是什么」这类个股问题会被讲义抢走。
CODE_RE = re.compile(r'(?<!\d)(?:sh|sz|bj)?\d{6}(?!\d)', re.IGNORECASE)

THEORY_PATTERN = re.compile(
    r'什么是|定义|原理|公式|推导|假设|证明|含义|概念|名词解释|知识点|考点|复习|'
    r'怎么理解|如何理解|有何区别|的区别|'
    r'讲义|教材|课本|第[一二三四五六七八九十百\d]+章|'
    r'投资学|公司理财|宏观经济学|金融市场|货币金融学|计量经济学|会计学|例题精解|'
    r'异方差|自相关|多重共线性|协整|资本资产定价|套利定价|有效市场|MM定理|'
    r'菲利普斯曲线|IS-LM|索洛|久期|凸性|期权定价|资本预算|加权平均资本成本'
)

# 顺序敏感：长名在前，避免「金融市场」抢先匹配「金融市场与金融机构」
BOOK_ALIASES = [
    ("金融市场与金融机构", "金融市场与金融机构"),
    ("金融市场", "金融市场与金融机构"),
    ("货币金融学", "货币金融学"),
    ("计量经济学", "计量经济学"),
    ("宏观经济学", "宏观经济学"),
    ("公司理财", "公司理财"),
    ("投资学", "投资学"),
    ("会计学", "会计学"),
    ("例题精解", "例题精解"),
    ("附录", "附录"),
]


class Orchestrator:
    """多 Agent 编排器（核心入口）"""

    def __init__(self,
                 skills_dir: Path = Path("skills"),
                 mcp_dir: Path = Path("mcp/servers"),
                 model_path: Path | None = None,
                 base_model: str = "Qwen/Qwen3-14B",
                 verbose: bool = False):
        self.runtime = SkillRuntime(skills_dir, mcp_dir, model_path, base_model, verbose)
        loaded = self.runtime.load_all_skills()
        self.sessions: dict[str, list[dict[str, Any]]] = {}
        self.verbose = verbose
        if verbose:
            print(f"[orchestrator] 加载 skills: {loaded}")

    # -----------------------------------------------------------------
    # 意图识别
    # -----------------------------------------------------------------
    def route(self, query: str) -> str:
        """基于规则的意图路由"""
        if not CODE_RE.search(query) and THEORY_PATTERN.search(query):
            return "theory-reference"
        for pattern, skill in INTENT_PATTERNS:
            if pattern.search(query):
                return skill
        return "indicator-match"  # 兜底（纯计算，无需 LLM）

    def _err(self, msg: str, intent: str, t0: float) -> dict[str, Any]:
        """统一错误返回（含 elapsed_ms）"""
        return {
            "ok": False,
            "error": msg,
            "intent": intent,
            "code": None,
            "skill_used": None,
            "result": None,
            "elapsed_ms": int((time.time() - t0) * 1000),
            "session_id": "default",
        }

    def extract_code(self, query: str) -> str | None:
        """从查询提取股票代码"""
        m = re.search(r'((?:sh|sz|bj)\d{6})', query, re.IGNORECASE)
        return m.group(1).lower() if m else None

    def extract_metric(self, query: str) -> str | None:
        """从查询提取财务指标"""
        mapping = {
            "营收|收入|revenue": "revenue",
            "净利润|net_profit|盈利": "net_profit",
            "ROE|净资产收益": "roe",
            "毛利|gross_margin": "gross_margin",
            "EPS|eps|每股收益": "eps",
            "PE|pe|市盈率": "pe",
            "PB|pb|市净率": "pb",
        }
        for pat, m in mapping.items():
            if re.search(pat, query, re.IGNORECASE):
                return m
        return None

    def extract_book(self, query: str) -> str:
        """从查询提取讲义书名；命中 0 本或多本时返回 all（跨书比较不宜锁死单本）"""
        found = {book for alias, book in BOOK_ALIASES if alias in query}
        return found.pop() if len(found) == 1 else "all"

    # -----------------------------------------------------------------
    # 主入口
    # -----------------------------------------------------------------
    def handle(self, query: str, session_id: str = "default") -> dict[str, Any]:
        """处理一条用户查询，返回结构化结果"""
        t0 = time.time()
        intent = self.route(query)
        code = self.extract_code(query)

        if self.verbose:
            print(f"[orchestrator] query='{query}' intent={intent} code={code}")

        # 根据意图分发
        if intent == "financial-summary":
            metric = self.extract_metric(query) or "revenue"
            if not code:
                return self._err("财务查询需要股票代码（如 sh600000）", intent, t0)
            result = self.runtime.execute_skill("financial-summary", {
                "code": code, "metric": metric,
            })
        elif intent == "adj-factor":
            if not code:
                return self._err("复权查询需要股票代码", intent, t0)
            result = self.runtime.execute_skill("adj-factor", {"code": code})
        elif intent == "news-summary":
            if not code:
                return self._err("新闻查询需要股票代码", intent, t0)
            result = self.runtime.execute_skill("news-summary", {"code": code})
        elif intent == "daban":
            result = self.runtime.execute_skill("daban", {"top": 5})
        elif intent == "indicator-match":
            if not code:
                return self._err("指标分析需要股票代码", intent, t0)
            result = self.runtime.execute_skill("indicator-match", {"code": code})
        elif intent == "theory-reference":
            result = self.runtime.execute_skill("theory-reference", {
                "query": query, "book": self.extract_book(query),
            })
        elif intent == "kline-analysis":
            if not code:
                return self._err("K线分析需要股票代码", intent, t0)
            # 主分析：有 LLM 就用 LLM，没有就降级到 indicator-match
            result = self.runtime.execute_skill("kline-analysis", {
                "code": code, "query": query,
            })
            # 若 LLM 不可用且未返回，自动 fallback
            if not result.get("ok") or "llm_output" not in result:
                if self.verbose:
                    print("[orchestrator] LLM 不可用，自动降级到 indicator-match")
                result = self.runtime.execute_skill("indicator-match", {"code": code})
        else:
            result = {"ok": False, "error": f"未知意图：{intent}"}

        # 多轮状态
        self.sessions.setdefault(session_id, []).append({"role": "user", "content": query})
        self.sessions[session_id].append({"role": "assistant", "content": result})
        if len(self.sessions[session_id]) > 20:
            self.sessions[session_id] = self.sessions[session_id][-20:]

        # 组装最终返回
        return {
            "ok": result.get("ok", False),
            "intent": intent,
            "code": code,
            "skill_used": result.get("skill") or intent,
            "result": result,
            "elapsed_ms": int((time.time() - t0) * 1000),
            "session_id": session_id,
            "session_turns": len(self.sessions.get(session_id, [])) // 2,
        }

    # -----------------------------------------------------------------
    # 多轮状态
    # -----------------------------------------------------------------
    def get_session(self, session_id: str = "default") -> list[dict[str, Any]]:
        return self.sessions.get(session_id, [])

    def clear_session(self, session_id: str = "default") -> None:
        self.sessions[session_id] = []

    def shutdown(self) -> None:
        self.runtime.shutdown()


# === 直接运行的调试入口 ====================================================
if __name__ == "__main__":
    import json
    orch = Orchestrator(verbose=True)
    tests = [
        "sh600000 怎么看？",
        "sh600519 财务怎么样？营收近 4 季度趋势",
        "sh600000 的复权因子",
        "sz000858 有锤子形态吗？MACD 金叉吗？",
        "sh600000 最近有什么新闻？",
        "什么是异方差？对回归推断有什么影响",
        "投资学里的有效市场假说定义是什么",
        "sh600519 的护城河是什么？ROE 怎么样",
    ]
    for q in tests:
        print(f"\n>>> 用户: {q}")
        r = orch.handle(q)
        print(f"    意图: {r['intent']} | 耗时: {r['elapsed_ms']}ms")
        print(f"    结果: {json.dumps(r['result'], ensure_ascii=False, indent=2)[:600]}")
    orch.shutdown()
