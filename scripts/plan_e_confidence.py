#!/usr/bin/env python3
"""方案E (B) 方案：基于模型置信度对有效交易候选项排序。

置信度定义：模型在 temperature=0 贪心解码下，对决策位「方向词」
(涨/震荡/跌) 输出的对数概率 logprob 经 softmax 转换得到的概率
P = exp(logprob)，单位 0–1（亦以百分比标注）。

排序对象：方案E 基线(无LLM过滤、非北交所) 有效交易的去重候选 code，
按模型对其方向判断的置信度从高到低排序。
"""
import json
import math
import re
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, "/home/jiuben/tdx-data-feed/scripts")
sys.path.insert(0, "/home/jiuben/tdx-data-feed/training/eval")
from llm_signal_plan_e import build_window  # noqa: E402
from plan_e_backtest import backtest, load_double_up  # noqa: E402

ROOT = Path("/home/jiuben/tdx-data-feed")
OLLAMA_URL = "http://127.0.0.1:11434"
DIRECTION_TOKENS = {"涨", "上涨", "震荡", "横盘", "跌", "下跌"}


def _norm_code(c):
    c = str(c).lower()
    c = re.sub(r"^(sh|sz|bj)", "", c)
    return c.zfill(6)


def call_conf(model: str, instruction: str, inp: str, timeout: int = 600):
    content = f"{instruction}\n{inp}\n<no_think>"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": content}],
        "stream": False,
        "options": {"temperature": 0.0, "num_predict": 16, "num_ctx": 2048},
        "logprobs": True,
    }
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/chat", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read())
    lps = d.get("logprobs") or []
    chosen, conf = None, None
    for e in lps:
        t = e.get("token", "").strip()
        if t in DIRECTION_TOKENS:
            chosen, conf = t, e.get("logprob")
            break
    if chosen is None and lps:
        chosen = lps[0].get("token", "").strip()
        conf = lps[0].get("logprob")
    p = math.exp(conf) if isinstance(conf, (int, float)) else float("nan")
    return chosen, p


def main():
    model = sys.argv[1] if len(sys.argv) > 1 else "qwen3-14b-tdx-v4:latest"
    out_md = sys.argv[2] if len(sys.argv) > 2 else str(
        ROOT / "data" / "plan_e" / "confidence_rank_v4.md")

    bt_valid, _ = backtest(load_double_up())
    def _is_bj(c):
        c = _norm_code(c)
        return c.startswith("920") or (c and c[0] == "8")
    bt_valid = bt_valid[~bt_valid["code"].apply(_is_bj)].reset_index(drop=True)
    bt_valid["code_n"] = bt_valid["code"].map(_norm_code)
    cand = (bt_valid[["code_n", "signal_date"]]
            .drop_duplicates("code_n").reset_index(drop=True))
    print(f"候选有效交易(去重 code): {len(cand)}", flush=True)

    rows = []
    t0 = time.time()
    for i, (_, r) in enumerate(cand.iterrows()):
        code, as_of = r["code_n"], r["signal_date"]
        built = build_window(code, as_of)
        if built is None:
            rows.append((code, as_of, None, float("nan")))  # 数据不足
            continue
        _, inp, _ = built
        ci = (f"你是A股量化分析师。基于以上行情，对股票 {code} 未来5个交易日方向做判断，"
              f"只输出一个词：涨、震荡 或 跌。不要任何其他文字。")
        try:
            chosen, p = call_conf(model, ci, inp)
        except Exception:
            rows.append((code, as_of, None, float("nan")))
            continue
        rows.append((code, as_of, chosen, p))
        if (i + 1) % 20 == 0:
            print(f"  {i+1}/{len(cand)} done ({time.time()-t0:.0f}s)", flush=True)

    valid = [x for x in rows if x[3] == x[3]]  # 非 NaN
    missing = [x for x in rows if x[3] != x[3]]
    rows_sorted = sorted(valid, key=lambda x: -x[3]) + sorted(missing, key=lambda x: x[0])

    # 并列检测（保留 6 位小数）
    from collections import defaultdict
    grp = defaultdict(list)
    for code, as_of, chosen, p in valid:
        grp[round(p, 6)].append(code)
    ties = {k: v for k, v in grp.items() if len(v) > 1}

    lines = []
    lines.append(f"# 方案E 有效交易候选 — 模型置信度排序（模型: {model}）\n")
    lines.append("排序依据：置信度 = 模型在 temperature=0 贪心解码下，对决策位方向词"
                 "(涨/震荡/跌) 输出 token 的 softmax 概率 P=exp(logprob)，单位 0–1 / %。"
                 "数值越高代表模型对该方向判断越确定。\n")
    rank = 0
    for code, as_of, chosen, p in rows_sorted:
        rank += 1
        if p != p:
            lines.append(f"第{rank}名 | {code} (事件日 {as_of}) | 置信度: 数据缺失 | "
                         f"理由: 窗口不足/模型调用失败")
        else:
            lines.append(f"第{rank}名 | {code} (事件日 {as_of}) | 置信度: {p*100:.2f}% "
                         f"(P={p:.4f}) | 理由: 模型判{chosen}")
    lines.append("")
    if ties:
        tl = "；".join(f"{k*100:.2f}% 并列 {len(v)} 项({','.join(v)})" for k, v in ties.items())
        lines.append(f"并列说明：检测到以下完全相等置信度（按 code 升序稳定排列）—— {tl}")
    else:
        lines.append("并列说明：未出现完全相等的置信度；如有并列，按 code 升序稳定排列。")
    if missing:
        lines.append(f"数据不足：{len(missing)} 个候选因窗口不足或模型调用失败缺失置信度，"
                     f"已排至末尾；缺失项无法参与有效置信度比较，会削弱 top-K 选股完整性。")

    out = "\n".join(lines) + "\n"
    Path(out_md).parent.mkdir(parents=True, exist_ok=True)
    Path(out_md).write_text(out, encoding="utf-8")
    print(out)


if __name__ == "__main__":
    main()
