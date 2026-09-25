"""v6 评估脚本 - 与 v4 对比 30 只核心股
输入: v6 Ollama 模型 + v4 Ollama 模型
输出: 评估报告 + 评分
"""

import json
import re
import time
import urllib.request
from datetime import datetime
from pathlib import Path

import pandas as pd

VAULT_DIR = Path("/home/jiuben/StockVault/01-标的")
PARQUET_DIR = Path("/home/jiuben/tdx-data-feed/data/parquet/daily")
OLLAMA_URL = "http://127.0.0.1:11434"
REPORT = Path("/home/jiuben/tdx-data-feed/v5/reports/v6-eval-20260924.md")

# 30 只核心股（与历史回溯一致）
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

    # 重试 3 次处理 HTTPError 500（ollama 偶发 OOM）
    last_err = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                d = json.loads(r.read())
            return d.get("response", ""), d.get("eval_duration", 0) / 1e9, d.get("eval_count", 0)
        except (urllib.error.HTTPError, urllib.error.URLError) as e:
            last_err = e
            wait = 5 * (attempt + 1)
            print(f"    [retry] HTTPError {e.code}, sleep {wait}s", flush=True)
            time.sleep(wait)
        except Exception as e:
            last_err = e
            wait = 5 * (attempt + 1)
            print(f"    [retry] {type(e).__name__}, sleep {wait}s", flush=True)
            time.sleep(wait)

    raise last_err if last_err else RuntimeError("call_ollama failed")


def build_input(code: str, window: int = 20) -> tuple[str, str, str]:
    """返回 (instruction, input_json, last_date_str)"""
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


def main():
    REPORT.parent.mkdir(parents=True, exist_ok=True)

    print("=== v6 评估 vs v4 (30 只核心股) ===")
    results = []

    for code, name in CORE_STOCKS:
        last_date, input_str, n = build_input(code)
        if not input_str:
            print(f"  ⚠ {code} {name}: no data")
            continue

        instruction = (
            f"你是专业的A股量化分析师。请基于以下截至{last_date}的近{n}个交易日行情数据，"
            f"对股票 {name}（{code}）做技术面分析并给出操作建议。"
        )
        prompt = f"### Instruction:\n{instruction}\n\n### Input:\n{input_str}\n\n### Response:\n"

        # v4
        v4_resp, v4_dur, v4_tok = call_ollama("qwen3-14b-tdx-fast:14b", prompt)
        v4_advice = extract_advice(v4_resp)
        # v6
        v6_resp, v6_dur, v6_tok = call_ollama("qwen3-14b-tdx-v6", prompt)
        v6_advice = extract_advice(v6_resp)

        # 比较输出差异（关键字）
        v4_words = set(re.findall(r"[\u4e00-\u9fff]{2,}", v4_resp))
        v6_words = set(re.findall(r"[\u4e00-\u9fff]{2,}", v6_resp))
        overlap = len(v4_words & v6_words)
        similarity = overlap / max(len(v4_words | v6_words), 1) * 100

        results.append(
            {
                "code": code,
                "name": name,
                "last_date": last_date,
                "v4_advice": v4_advice,
                "v4_tokens": v4_tok,
                "v4_dur": round(v4_dur, 2),
                "v6_advice": v6_advice,
                "v6_tokens": v6_tok,
                "v6_dur": round(v6_dur, 2),
                "similarity": round(similarity, 1),
                "v4_resp": v4_resp[:200],
                "v6_resp": v6_resp[:200],
            }
        )

        same = "✅" if v4_advice == v6_advice else "⚠️"
        print(
            f"  [{len(results):2d}/30] {same} {code} {name[:6]:6} v4={v4_advice} v6={v6_advice} 相似度={similarity:.0f}%"
        )

    # 写报告
    with REPORT.open("w", encoding="utf-8") as f:
        f.write("# v6 评估 vs v4 · 2026-09-24\n\n")
        f.write(f"> 评估时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write("> 评估范围: 30 只核心股\n")
        f.write("> 模型对比: qwen3-14b-tdx-fast:14b (v4) vs qwen3-14b-tdx-v6\n\n")

        f.write("## 一、建议一致性\n\n")
        same_count = sum(1 for r in results if r["v4_advice"] == r["v6_advice"])
        f.write(
            f"- **一致**: {same_count}/{len(results)} ({same_count / len(results) * 100:.1f}%)\n"
        )
        f.write(f"- **不一致**: {len(results) - same_count}\n\n")

        f.write("## 二、平均相似度\n\n")
        avg_sim = sum(r["similarity"] for r in results) / len(results) if results else 0
        f.write(f"- 输出文字相似度: **{avg_sim:.1f}%**\n\n")

        f.write("## 三、详细对比\n\n")
        f.write(
            "| 标的 | v4 建议 | v6 建议 | 相似度 | v4 tok | v6 tok |\n|---|---|---|---|---|---|\n"
        )
        for r in results:
            same = "✅" if r["v4_advice"] == r["v6_advice"] else "⚠️"
            f.write(
                f"| {same} {r['code']} {r['name'][:6]} | {r['v4_advice']} | {r['v6_advice']} | {r['similarity']:.0f}% | {r['v4_tokens']} | {r['v6_tokens']} |\n"
            )

        f.write("\n## 四、典型输出对比（茅台 600519）\n\n")
        m = next((r for r in results if r["code"] == "600519"), None)
        if m:
            f.write(f"**v4 输出**:\n> {m['v4_resp']}\n\n")
            f.write(f"**v6 输出**:\n> {m['v6_resp']}\n\n")

        f.write("\n## 五、结论\n\n")
        if same_count >= 20:
            f.write("- ✅ **v6 与 v4 高度一致**：v6 训练未破坏原能力\n")
        else:
            f.write(f"- ⚠️ **v6 偏离 v4**：{len(results) - same_count} 只建议不同\n")
        if avg_sim >= 60:
            f.write("- ✅ **输出风格保持**：平均相似度较高\n")
        else:
            f.write(f"- ⚠️ **输出风格变化**：相似度 {avg_sim:.0f}%\n")
        f.write(
            f"- **v6 平均生成**: {sum(r['v6_tokens'] for r in results) / len(results):.0f} tokens\n"
        )
        f.write(
            f"- **v4 平均生成**: {sum(r['v4_tokens'] for r in results) / len(results):.0f} tokens\n"
        )

    print("\n=== 评估完成 ===")
    print(f"  报告: {REPORT}")
    print(f"  一致: {same_count}/30")
    print(f"  平均相似度: {avg_sim:.1f}%")


if __name__ == "__main__":
    main()
