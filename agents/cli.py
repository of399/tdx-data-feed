#!/usr/bin/env python3
"""
agents/cli.py — 多 Agent 系统命令行入口
========================================

用法：
    # 单次查询
    python -m agents.cli "sh600000 怎么看？"
    python -m agents.cli "sh600519 营收近 4 季度？"

    # 交互式 REPL（多轮对话）
    python -m agents.cli --interactive

    # 指定 LoRA checkpoint（启用 LLM 推理）
    python -m agents.cli --model training/output/qwen3-14b-tdx/checkpoint-final "sh600000"

    # 只看意图
    python -m agents.cli --intent-only "sh600000 财务"
"""

import argparse
import json
from pathlib import Path

from agents.orchestrator import Orchestrator


# === 格式化输出 =============================================================
def fmt_result(r: dict, compact: bool = False) -> str:
    """把 orchestrator 结果格式化成可读文本"""
    if not r.get("ok"):
        return f"❌ {r.get('error', '未知错误')}"

    intent = r["intent"]
    code = r.get("code", "—")
    elapsed = r["elapsed_ms"]
    res = r.get("result", {})

    lines = []
    lines.append(f"🎯 意图: {intent}  |  代码: {code}  |  耗时: {elapsed}ms")
    lines.append(f"🔧 工具链: {', '.join(res.get('tools_used', [])) or 'N/A'}")

    # 按 skill 类型格式化
    if intent == "indicator-match":
        lines.append(f"📊 打分: {res.get('score', '—')}  →  {res.get('label', '—')}")
        lines.append(f"📝 触发: {res.get('reason', '—')}")
        direction = res.get("direction", 0)
        arrows = {1: "📈 看多", -1: "📉 看空", 0: "➖ 中性"}
        lines.append(f"🎯 方向: {arrows.get(direction, '—')}")
    elif intent == "adj-factor":
        lines.append(f"🔢 前复权因子: {res.get('fwd_factor', '—')}")
        lines.append(f"🔢 后复权因子: {res.get('bwd_factor', '—')}")
        lines.append(f"📅 涉及除权除息事件: {res.get('n_events', 0)}")
    elif intent == "kline-analysis":
        llm_out = res.get("llm_output", "")
        if llm_out:
            lines.append("🤖 模型输出（前 400 字）:")
            lines.append("  " + llm_out[:400].replace("\n", "\n  "))
        if "parsed" in res:
            lines.append(f"📦 解析结果: {json.dumps(res['parsed'], ensure_ascii=False)}")
        else:
            # fallback indicator-match
            lines.append(f"📊 打分: {res.get('score', '—')}  →  {res.get('label', '—')}")
            lines.append(f"📝 触发: {res.get('reason', '—')}")
    elif intent == "financial-summary":
        lines.append(f"💰 指标: {res.get('metric', '—')}")
        if "values" in res:
            for v in res["values"][:4]:
                lines.append(f"   {v.get('period_end', '')}: {v.get('value', '—')} (yoy {v.get('yoy_pct', '—')}%)")
        if "summary" in res:
            lines.append(f"📝 总结: {res['summary']}")
    elif intent == "news-summary":
        items = res.get("items", [])
        if items:
            lines.append(f"📰 近期新闻（{len(items)} 条）:")
            for it in items[:5]:
                lines.append(f"   • [{it.get('date', '')}] {it.get('title', '')[:50]} ({it.get('impact', '')})")
        else:
            lines.append("📰 未检索到近期新闻")
        lines.append(f"🎭 整体情绪: {res.get('overall_sentiment', '—')}")

    return "\n".join(lines)


# === REPL ==================================================================
def repl(orch: Orchestrator) -> None:
    """交互式 REPL"""
    print("=" * 70)
    print("多 Agent 股票分析 REPL（输入 :quit 退出，:help 看帮助）")
    print("=" * 70)
    session_id = "repl"
    while True:
        try:
            query = input("\n>>> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见")
            break
        if not query:
            continue
        if query in (":quit", ":q", ":exit"):
            break
        if query == ":help":
            print("""可用命令：
    :help              显示帮助
    :quit / :q         退出
    :session           查看当前会话历史
    :clear             清空当前会话
    :skills            列出已加载的 skill
    <查询>             普通查询（如 "sh600000 怎么看？"）""")
            continue
        if query == ":session":
            sess = orch.get_session(session_id)
            for i, m in enumerate(sess[-10:]):
                print(f"  [{i}] {m['role']}: {str(m['content'])[:80]}")
            continue
        if query == ":clear":
            orch.clear_session(session_id)
            print("会话已清空")
            continue
        if query == ":skills":
            print("已加载 skill:", orch.runtime.list_skills())
            continue
        try:
            r = orch.handle(query, session_id=session_id)
            print(fmt_result(r))
        except Exception as e:
            print(f"❌ 出错：{e}")


# === Main ==================================================================
def main():
    ap = argparse.ArgumentParser(description="多 Agent 股票分析 CLI")
    ap.add_argument("query", nargs="?", help="单次查询")
    ap.add_argument("--interactive", "-i", action="store_true", help="REPL 模式")
    ap.add_argument("--model", type=str, default=None,
                    help="LoRA checkpoint 路径（启用 LLM 推理）")
    ap.add_argument("--base-model", type=str, default="Qwen/Qwen3-14B",
                    help="HF base model id 或本地路径")
    ap.add_argument("--skills-dir", type=str, default="skills",
                    help="skill yaml 目录")
    ap.add_argument("--mcp-dir", type=str, default="mcp/servers",
                    help="MCP server 脚本目录")
    ap.add_argument("--intent-only", action="store_true",
                    help="只显示意图识别结果")
    ap.add_argument("--json", action="store_true",
                    help="以 JSON 输出结果")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args()

    # 默认从项目根目录起算
    proj_root = Path(__file__).parent.parent
    import os
    os.chdir(proj_root)

    orch = Orchestrator(
        skills_dir=Path(args.skills_dir),
        mcp_dir=Path(args.mcp_dir),
        model_path=Path(args.model) if args.model else None,
        base_model=args.base_model,
        verbose=args.verbose,
    )

    try:
        if args.interactive:
            repl(orch)
        elif args.query:
            if args.intent_only:
                print(f"query='{args.query}' intent={orch.route(args.query)}")
            else:
                r = orch.handle(args.query)
                if args.json:
                    print(json.dumps(r, ensure_ascii=False, indent=2, default=str))
                else:
                    print(fmt_result(r))
        else:
            ap.print_help()
    finally:
        orch.shutdown()


if __name__ == "__main__":
    main()
