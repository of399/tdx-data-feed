"""v5 vs v6 同 prompt 对比脚本（轻量）

输入: 30 只核心股
输出: markdown 报告 v5_vs_v6-{date}.md

对比维度:
  - 建议一致率（v5 建议 == v6 建议）
  - 文字相似度（中文词集 Jaccard）
  - 字段完整度（趋势/技术指标/支撑压力/操作建议/风险提示 5 项）
  - 长度（tokens）
"""

import json
import re
import time
import urllib.request
from datetime import datetime
from pathlib import Path

import pandas as pd

PARQUET_DIR = Path("/home/jiuben/tdx-data-feed/data/parquet/daily")
OLLAMA_URL = "http://127.0.0.1:11434"

V5_MODEL = "qwen3-14b-tdx-v5"
V6_MODEL = "qwen3-14b-tdx-v6"

REPORT = Path(
    f"/home/jiuben/tdx-data-feed/v5/reports/v5_vs_v6-{datetime.now().strftime('%Y%m%d')}.md"
)

CORE_STOCKS = [
    ("600519", "贵州茅台"),
    ("000858", "五粮液"),
    ("000333", "美的集团"),
    ("000651", "格力电器"),
    ("600036", "招商银行"),
    ("601398", "工商银行"),
    ("601318", "中国平安"),
    ("002594", "比亚迪"),
    ("300750", "宁德时代"),
    ("601012", "隆基绿能"),
    ("600276", "恒瑞医药"),
    ("000538", "云南白药"),
    ("600887", "伊利股份"),
    ("601888", "中国中免"),
    ("600028", "中国石化"),
    ("601857", "中国石油"),
    ("600585", "海螺水泥"),
    ("000002", "万科A"),
    ("600048", "保利发展"),
    ("300059", "东方财富"),
    ("300124", "汇川技术"),
    ("002415", "海康威视"),
    ("600900", "长江电力"),
    ("601138", "工业富联"),
    ("002475", "立讯精密"),
    ("688981", "中芯国际"),
    ("600009", "上海机场"),
    ("300015", "爱尔眼科"),
    ("600030", "中信证券"),
    ("601628", "中国人寿"),
]


def parse_date(d) -> str:
    if isinstance(d, pd.Timestamp):
        return d.strftime("%Y-%m-%d")
    if isinstance(d, (int, float)):
        s = str(int(d))
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}" if len(s) == 8 else s
    return str(d)[:10]


def call_ollama(model: str, prompt: str) -> tuple[str, float, int]:
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {
            "temperature": 0.3,
            "top_p": 0.95,
            "top_k": 20,
            "num_predict": 350,
            "num_ctx": 4096,
        },
    }
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/generate",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                d = json.loads(r.read())
            return d.get("response", ""), d.get("eval_duration", 0) / 1e9, d.get("eval_count", 0)
        except Exception as e:
            if attempt < 2:
                time.sleep(2**attempt)
                continue
            return f"[ERROR {e}]", 0.0, 0


def build_input(code: str, window: int = 20) -> tuple[str, str, str]:
    for prefix in ("sh", "sz", "bj"):
        p = PARQUET_DIR / f"{prefix}{code}.parquet"
        if p.exists():
            df = pd.read_parquet(p, columns=["date", "open", "high", "low", "close", "volume"])
            df = df.sort_values("date").reset_index(drop=True)
            df_tail = df.tail(window).reset_index(drop=True)
            last_date = parse_date(df_tail.iloc[-1]["date"])
            break
    else:
        return None, None, None

    rows = []
    prev_close = None
    for _, row in df_tail.iterrows():
        d_str = parse_date(row["date"])
        close = float(row["close"])
        pct = 0.0 if prev_close is None else round((close - prev_close) / prev_close * 100, 2)
        rows.append(
            {
                "日期": d_str,
                "开盘": round(float(row["open"]), 2),
                "收盘": round(close, 2),
                "最高": round(float(row["high"]), 2),
                "最低": round(float(row["low"]), 2),
                "成交量": round(float(row["volume"]) * 100, 0),
                "涨跌幅": pct,
            }
        )
        prev_close = close
    return last_date, json.dumps(rows, ensure_ascii=False), str(len(df_tail))


def extract_advice(text: str) -> str:
    m = re.search(r"建议([\u4e00-\u9fff]+?)(?:[。,.]|$)", text)
    if not m:
        return "其他"
    a = m.group(1)
    if "低吸" in a or "关注" in a or "回调" in a:
        return "关注低吸"
    if "减仓" in a or "高抛" in a:
        return "逢高减仓"
    if "持股" in a:
        return "持股观察"
    return "其他"


def field_score(text: str) -> dict:
    """v6 训练时加了 5 项硬要求：趋势/技术指标/支撑压力/操作建议/风险提示"""
    return {
        "趋势": 1 if re.search(r"(上涨|下跌|横盘|震荡)", text) else 0,
        "技术指标": 1 if re.search(r"(MA\d+|MACD|RSI)", text) else 0,
        "支撑压力": 1 if re.search(r"支撑|压力", text) else 0,
        "操作建议": 1 if re.search(r"建议|关注|减仓|持股|观望", text) else 0,
        "风险提示": 1 if re.search(r"风险", text) else 0,
    }


def similarity(a: str, b: str) -> float:
    aw = set(re.findall(r"[\u4e00-\u9fff]{2,}", a))
    bw = set(re.findall(r"[\u4e00-\u9fff]{2,}", b))
    if not aw and not bw:
        return 0.0
    return len(aw & bw) / len(aw | bw) * 100


def process_one(code: str, name: str):
    last_date, input_str, n = build_input(code)
    if not input_str:
        return {"code": code, "name": name, "error": "no data"}

    instruction = (
        f"你是专业的A股量化分析师，已通过 v5 微调。\n"
        f"请基于以下截至{last_date}的近{n}个交易日行情数据，"
        f"对 {name}（{code}）做技术面分析并给出操作建议。\n"
        f"输出要求（请严格遵守）：\n"
        f"1. 趋势判断：明确（上涨/下跌/横盘/震荡）\n"
        f"2. 技术指标：列出 MA5/MA20、MACD 状态、RSI 数值\n"
        f"3. 支撑压力：给出具体价位\n"
        f"4. 操作建议：明确（关注低吸/持股观察/逢高减仓/观望）\n"
        f"5. 风险提示：1-2 句话\n"
        f"格式：200 字以内中文段落，不要分点。"
    )
    prompt = f"### Instruction:\n{instruction}\n\n### Input:\n{input_str}\n\n### Response:\n"

    v5_resp, v5_dur, v5_tok = call_ollama(V5_MODEL, prompt)
    v6_resp, v6_dur, v6_tok = call_ollama(V6_MODEL, prompt)

    return {
        "code": code,
        "name": name,
        "last_date": last_date,
        "v5_resp": v5_resp,
        "v5_tok": v5_tok,
        "v5_dur": round(v5_dur, 2),
        "v5_advice": extract_advice(v5_resp),
        "v5_fields": field_score(v5_resp),
        "v6_resp": v6_resp,
        "v6_tok": v6_tok,
        "v6_dur": round(v6_dur, 2),
        "v6_advice": extract_advice(v6_resp),
        "v6_fields": field_score(v6_resp),
        "similarity": round(similarity(v5_resp, v6_resp), 1),
    }


def main():
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    print(f"=== {V5_MODEL} vs {V6_MODEL} 对比 · {len(CORE_STOCKS)} 只核心股 ===")
    print(f"  报告: {REPORT}")
    print("  顺序跑：每个股先 v5 再 v6（避免 OOM）")

    results = []
    for code, name in CORE_STOCKS:
        r = process_one(code, name)
        results.append(r)
        v5f = r.get("v5_fields", {})
        v6f = r.get("v6_fields", {})
        v5_sum = sum(v5f.values()) if v5f else 0
        v6_sum = sum(v6f.values()) if v6f else 0
        same = "✅" if r.get("v5_advice") == r.get("v6_advice") else "⚠️"
        print(
            f"  [{len(results):2d}/{len(CORE_STOCKS)}] {same} {code} {name[:6]:6} "
            f"v5={r.get('v5_advice')} v6={r.get('v6_advice')} "
            f"字段 v5={v5_sum}/5 v6={v6_sum}/5 "
            f"sim={r.get('similarity', 0):.0f}%"
        )

    valid = [r for r in results if "error" not in r]
    with REPORT.open("w", encoding="utf-8") as f:
        f.write(f"# {V5_MODEL} vs {V6_MODEL} 对比 · {datetime.now().strftime('%Y-%m-%d')}\n\n")
        f.write(f"> 评估时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"> 评估范围: {len(valid)} 只核心股（{len(results) - len(valid)} 跳过）\n")
        f.write(f"> 模型: {V5_MODEL} vs {V6_MODEL}\n\n")

        same_count = sum(1 for r in valid if r["v5_advice"] == r["v6_advice"])
        f.write("## 一、建议一致率\n\n")
        f.write(f"- **一致**: {same_count}/{len(valid)} ({same_count / len(valid) * 100:.1f}%)\n")
        f.write(f"- **不一致**: {len(valid) - same_count}\n\n")

        avg_sim = sum(r["similarity"] for r in valid) / len(valid) if valid else 0
        f.write("## 二、文字相似度\n\n")
        f.write(f"- 平均 Jaccard: **{avg_sim:.1f}%**\n")
        sim_buckets = {">=80%": 0, "60-80%": 0, "40-60%": 0, "<40%": 0}
        for r in valid:
            s = r["similarity"]
            if s >= 80:
                sim_buckets[">=80%"] += 1
            elif s >= 60:
                sim_buckets["60-80%"] += 1
            elif s >= 40:
                sim_buckets["40-60%"] += 1
            else:
                sim_buckets["<40%"] += 1
        f.write("- 分布: " + " | ".join(f"{k}={v}" for k, v in sim_buckets.items()) + "\n\n")

        f.write("## 三、字段完整度（v6 训练时加了 5 项硬要求）\n\n")
        f.write("| 字段 | v5 平均 | v6 平均 | 提升 |\n|---|---|---|---|\n")
        for field in ["趋势", "技术指标", "支撑压力", "操作建议", "风险提示"]:
            v5_avg = sum(r["v5_fields"][field] for r in valid) / len(valid) * 100
            v6_avg = sum(r["v6_fields"][field] for r in valid) / len(valid) * 100
            delta = v6_avg - v5_avg
            arrow = "↑" if delta > 0 else ("↓" if delta < 0 else "→")
            f.write(f"| {field} | {v5_avg:.0f}% | {v6_avg:.0f}% | {arrow} {delta:+.0f}% |\n")
        f.write("\n")

        v5_avg_tok = sum(r["v5_tok"] for r in valid) / len(valid) if valid else 0
        v6_avg_tok = sum(r["v6_tok"] for r in valid) / len(valid) if valid else 0
        f.write("## 四、生成长度\n\n")
        f.write(f"- v5 平均: {v5_avg_tok:.0f} tokens\n")
        f.write(f"- v6 平均: {v6_avg_tok:.0f} tokens\n\n")

        f.write("## 五、详细对比\n\n")
        f.write(
            "| 标的 | v5 建议 | v6 建议 | 相似度 | v5 字段 | v6 字段 | v5 tok | v6 tok |\n|---|---|---|---|---|---|---|---|\n"
        )
        for r in valid:
            v5f = sum(r["v5_fields"].values())
            v6f = sum(r["v6_fields"].values())
            same = "✅" if r["v5_advice"] == r["v6_advice"] else "⚠️"
            f.write(
                f"| {same} {r['code']} {r['name'][:6]} | {r['v5_advice']} | {r['v6_advice']} | "
                f"{r['similarity']:.0f}% | {v5f}/5 | {v6f}/5 | {r['v5_tok']} | {r['v6_tok']} |\n"
            )

        f.write("\n## 六、典型输出对比（茅台 600519）\n\n")
        m = next((r for r in valid if r["code"] == "600519"), None)
        if m:
            f.write(f"**v5 输出**（{m['v5_tok']} tokens）:\n> {m['v5_resp'][:500]}\n\n")
            f.write(f"**v6 输出**（{m['v6_tok']} tokens）:\n> {m['v6_resp'][:500]}\n\n")

        f.write("## 七、结论\n\n")
        if avg_sim >= 70:
            f.write(
                f"- ✅ **v5/v6 输出风格高度一致**（平均 {avg_sim:.0f}% 相似度），v6 训练未大幅破坏 v5 能力\n"
            )
        else:
            f.write(f"- ⚠️ **v5/v6 输出风格差异较大**（平均 {avg_sim:.0f}% 相似度），v6 风格变化\n")
        v6_avg_fields = sum(sum(r["v6_fields"].values()) for r in valid) / len(valid)
        if v6_avg_fields >= 4.0:
            f.write(
                f"- ✅ **v6 字段完整度高**（平均 {v6_avg_fields:.1f}/5）— 5 项硬要求被模型学会\n"
            )
        else:
            f.write(f"- ⚠️ **v6 字段完整度待提升**（平均 {v6_avg_fields:.1f}/5）\n")
        if v6_avg_tok > 0 and v5_avg_tok > 0:
            if v6_avg_tok < v5_avg_tok:
                f.write(
                    f"- ✅ **v6 更精简**（{v6_avg_tok:.0f} < v5 {v5_avg_tok:.0f}）— 输出效率高\n"
                )
            else:
                f.write(f"- ⚠️ **v6 更长**（{v6_avg_tok:.0f} > v5 {v5_avg_tok:.0f}）— 输出更详细\n")

    print("\n=== 对比完成 ===")
    print(f"  报告: {REPORT}")
    print(f"  建议一致: {same_count}/{len(valid)}")
    print(f"  平均相似: {avg_sim:.1f}%")


if __name__ == "__main__":
    main()
