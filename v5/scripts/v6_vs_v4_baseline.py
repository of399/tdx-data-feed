"""v6 vs v4 (fast) baseline 对比
- 公平对比：都用简单 prompt（不展示 v6 训练时的 5 项硬要求）
- v6 优势对比：用详细 prompt（5 项硬要求）
"""

import json
import re
import time
import urllib.request
import warnings
from datetime import datetime
from pathlib import Path

warnings.filterwarnings("ignore")

import pandas as pd  # noqa: E402
import torch  # noqa: E402
from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig  # noqa: E402

PARQUET_DIR = Path("/home/jiuben/tdx-data-feed/data/parquet/daily")
MERGED = "/home/jiuben/models/Qwen3-14B-tdx-v6-merged"
OLLAMA_URL = "http://127.0.0.1:11434"
V4_MODEL = "qwen3-14b-tdx-fast:14b"
REPORT = Path(
    f"/home/jiuben/tdx-data-feed/v5/reports/v6_vs_v4-{datetime.now().strftime('%Y%m%d-%H%M')}.md"
)
REPORT.parent.mkdir(parents=True, exist_ok=True)

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

# 简单 prompt（公平对比）
def PROMPT_SIMPLE(name, code, last_date, n, input_str):
    return (
    f"### Instruction:\n"
    f"你是专业的A股量化分析师。请基于以下截至{last_date}的近{n}个交易日行情数据，"
    f"对股票 {name}（{code}）做技术面分析并给出操作建议。\n\n"
    f"### Input:\n{input_str}\n\n### Response:\n"
)

# v6 优势 prompt（5 项硬要求）
def PROMPT_DETAILED(name, code, last_date, n, input_str):
    return (
    f"### Instruction:\n"
    f"你是专业的A股量化分析师，已通过 v5 微调。\n"
    f"请基于以下截至{last_date}的近{n}个交易日行情数据，"
    f"对 {name}（{code}）做技术面分析并给出操作建议。\n"
    f"输出要求（请严格遵守）：\n"
    f"1. 趋势判断：明确（上涨/下跌/横盘/震荡）\n"
    f"2. 技术指标：列出 MA5/MA20、MACD 状态、RSI 数值\n"
    f"3. 支撑压力：给出具体价位\n"
    f"4. 操作建议：明确（关注低吸/持股观察/逢高减仓/观望）\n"
    f"5. 风险提示：1-2 句话\n"
    f"格式：200 字以内中文段落，不要分点。\n\n"
    f"### Input:\n{input_str}\n\n### Response:\n"
)


def parse_date(d):
    if isinstance(d, pd.Timestamp):
        return d.strftime("%Y-%m-%d")
    if isinstance(d, (int, float)):
        s = str(int(d))
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}" if len(s) == 8 else s
    return str(d)[:10]


def build_input(code, window=20):
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


def call_ollama(model, prompt):
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
                time.sleep(2)
                continue
            return f"[ERROR {e}]", 0.0, 0


def extract_advice(text):
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


def field_score(text):
    return {
        "趋势": 1 if re.search(r"(上涨|下跌|横盘|震荡)", text) else 0,
        "技术指标": 1 if re.search(r"(MA\d+|MACD|RSI)", text) else 0,
        "支撑压力": 1 if re.search(r"支撑|压力", text) else 0,
        "操作建议": 1 if re.search(r"建议|关注|减仓|持股|观望", text) else 0,
        "风险提示": 1 if re.search(r"风险", text) else 0,
    }


def similarity(a, b):
    aw = set(re.findall(r"[\u4e00-\u9fff]{2,}", a))
    bw = set(re.findall(r"[\u4e00-\u9fff]{2,}", b))
    if not aw and not bw:
        return 0.0
    return len(aw & bw) / len(aw | bw) * 100


def detect_repetition(text):
    """检测重复模式：连续相同的 4 字以上词组 ≥3 次"""
    tokens = re.findall(r"[\u4e00-\u9fff]{4,}", text)
    if len(tokens) < 6:
        return 0
    cnt = {}
    for t in tokens:
        cnt[t] = cnt.get(t, 0) + 1
    max_repeat = max(cnt.values())
    return max_repeat


def main():
    print("=== v6 vs v4 baseline 对比 ===")
    print(f"  v6: {MERGED} (int4 量化, GPU 1)")
    print(f"  v4: {V4_MODEL} (Ollama API, GPU 1)")
    print("\n加载 v6 模型...")

    t0 = time.time()
    tokenizer = AutoTokenizer.from_pretrained(MERGED, trust_remote_code=True)
    bnb = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_quant_type="nf4",
        bnb_4bit_compute_dtype=torch.bfloat16,
        bnb_4bit_use_double_quant=True,
    )
    model = AutoModelForCausalLM.from_pretrained(
        MERGED, quantization_config=bnb, device_map="cuda:1", trust_remote_code=True
    )
    model.eval()
    print(f"  ✓ v6 加载完成 ({time.time() - t0:.1f}s)")

    results_simple = []
    results_detailed = []

    print("\n--- 测试 1：简单 prompt（公平对比）---")
    t1 = time.time()
    for i, (code, name) in enumerate(CORE_STOCKS):
        last_date, input_str, n = build_input(code)
        if not input_str:
            continue

        prompt = PROMPT_SIMPLE(name, code, last_date, n, input_str)

        # v6 推理
        inputs = tokenizer(prompt, return_tensors="pt").to("cuda:1")
        with torch.no_grad():
            out = model.generate(
                **inputs,
                max_new_tokens=350,
                temperature=0.3,
                top_p=0.95,
                top_k=20,
                do_sample=True,
                pad_token_id=tokenizer.eos_token_id,
                repetition_penalty=1.1,
            )
        v6_resp = tokenizer.decode(
            out[0][inputs.input_ids.shape[1] :], skip_special_tokens=True
        ).strip()

        # v4 ollama 推理
        v4_resp, _v4_dur, _v4_tok = call_ollama(V4_MODEL, prompt)

        results_simple.append(
            {
                "code": code,
                "name": name,
                "last_date": last_date,
                "v6_resp": v6_resp,
                "v6_advice": extract_advice(v6_resp),
                "v6_fields": field_score(v6_resp),
                "v6_rep": detect_repetition(v6_resp),
                "v4_resp": v4_resp,
                "v4_advice": extract_advice(v4_resp),
                "v4_fields": field_score(v4_resp),
                "v4_rep": detect_repetition(v4_resp),
                "similarity": round(similarity(v6_resp, v4_resp), 1),
            }
        )
        print(
            f"  [{i + 1:2d}/30] {code} {name[:6]:6} v6={results_simple[-1]['v6_advice']:<6} v4={results_simple[-1]['v4_advice']:<6} sim={results_simple[-1]['similarity']:.0f}%"
        )

    print(f"  简单 prompt 总耗时: {(time.time() - t1) / 60:.1f} 分钟")

    print("\n--- 测试 2：v6 优势 prompt（详细 5 项硬要求）---")
    t2 = time.time()
    for i, (code, name) in enumerate(CORE_STOCKS):
        last_date, input_str, n = build_input(code)
        if not input_str:
            continue

        prompt = PROMPT_DETAILED(name, code, last_date, n, input_str)

        # v6 详细 prompt
        inputs = tokenizer(prompt, return_tensors="pt").to("cuda:1")
        with torch.no_grad():
            out = model.generate(
                **inputs,
                max_new_tokens=350,
                temperature=0.3,
                top_p=0.95,
                top_k=20,
                do_sample=True,
                pad_token_id=tokenizer.eos_token_id,
                repetition_penalty=1.1,
            )
        v6d_resp = tokenizer.decode(
            out[0][inputs.input_ids.shape[1] :], skip_special_tokens=True
        ).strip()

        # v4 也用详细 prompt（看 v4 学不会）
        v4d_resp, _, _ = call_ollama(V4_MODEL, prompt)

        results_detailed.append(
            {
                "code": code,
                "name": name,
                "v6d_resp": v6d_resp,
                "v6d_advice": extract_advice(v6d_resp),
                "v6d_fields": field_score(v6d_resp),
                "v6d_rep": detect_repetition(v6d_resp),
                "v4d_resp": v4d_resp,
                "v4d_advice": extract_advice(v4d_resp),
                "v4d_fields": field_score(v4d_resp),
                "v4d_rep": detect_repetition(v4d_resp),
            }
        )
        print(
            f"  [{i + 1:2d}/30] {code} {name[:6]:6} v6d={results_detailed[-1]['v6d_advice']:<6} v4d={results_detailed[-1]['v4d_advice']:<6} v6d_字段={sum(results_detailed[-1]['v6d_fields'].values())}/5"
        )

    print(f"  详细 prompt 总耗时: {(time.time() - t2) / 60:.1f} 分钟")

    # 释放 GPU
    del model
    torch.cuda.empty_cache()

    # ===== 写报告 =====
    def field_avg(rs, key):
        return {
            k: sum(r[key][k] for r in rs) / len(rs) * 100
            for k in ["趋势", "技术指标", "支撑压力", "操作建议", "风险提示"]
        }

    v6_simple_fa = field_avg(results_simple, "v6_fields")
    v4_simple_fa = field_avg(results_simple, "v4_fields")
    v6_det_fa = field_avg(results_detailed, "v6d_fields")
    v4_det_fa = field_avg(results_detailed, "v4d_fields")

    same_simple = sum(1 for r in results_simple if r["v6_advice"] == r["v4_advice"])
    same_det = sum(1 for r in results_detailed if r["v6d_advice"] == r["v4d_advice"])

    v6_simple_rep = sum(1 for r in results_simple if r["v6_rep"] >= 3)
    v4_simple_rep = sum(1 for r in results_simple if r["v4_rep"] >= 3)
    v6_det_rep = sum(1 for r in results_detailed if r["v6d_rep"] >= 3)
    v4_det_rep = sum(1 for r in results_detailed if r["v4d_rep"] >= 3)

    with REPORT.open("w", encoding="utf-8") as f:
        f.write("# v6 vs v4 (fast) baseline 对比\n\n")
        f.write(f"> 生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write("> v6: Qwen3-14B + v6 LoRA merged（int4 nf4，GPU 1）\n")
        f.write("> v4: Qwen3-14B + v4 fast LoRA merged（Ollama，已部署）\n")
        f.write("> 评估范围: 30 只核心股\n\n")

        f.write("## 一、测试 1：简单 prompt（公平对比）\n\n")
        f.write("### 建议一致率\n\n")
        f.write(
            f"- v6 == v4: **{same_simple}/{len(results_simple)}** ({same_simple / len(results_simple) * 100:.1f}%)\n\n"
        )
        f.write("### 字段完整度\n\n")
        f.write("| 字段 | v6 | v4 | v6 - v4 |\n|---|---|---|---|\n")
        for k in ["趋势", "技术指标", "支撑压力", "操作建议", "风险提示"]:
            delta = v6_simple_fa[k] - v4_simple_fa[k]
            arrow = "↑" if delta > 0 else ("↓" if delta < 0 else "→")
            f.write(
                f"| {k} | {v6_simple_fa[k]:.1f}% | {v4_simple_fa[k]:.1f}% | {arrow} {delta:+.1f}% |\n"
            )
        f.write("\n")

        f.write("### 复读检测（连续 4 字以上词组 ≥3 次）\n\n")
        f.write(
            f"- v6 复读: **{v6_simple_rep}/{len(results_simple)}** ({v6_simple_rep / len(results_simple) * 100:.1f}%)\n"
        )
        f.write(
            f"- v4 复读: **{v4_simple_rep}/{len(results_simple)}** ({v4_simple_rep / len(results_simple) * 100:.1f}%)\n\n"
        )

        f.write("## 二、测试 2：v6 优势 prompt（5 项硬要求）\n\n")
        f.write("### 建议一致率\n\n")
        f.write(
            f"- v6 == v4: **{same_det}/{len(results_detailed)}** ({same_det / len(results_detailed) * 100:.1f}%)\n\n"
        )
        f.write("### 字段完整度\n\n")
        f.write("| 字段 | v6 | v4 | v6 - v4 |\n|---|---|---|---|\n")
        for k in ["趋势", "技术指标", "支撑压力", "操作建议", "风险提示"]:
            delta = v6_det_fa[k] - v4_det_fa[k]
            arrow = "↑" if delta > 0 else ("↓" if delta < 0 else "→")
            f.write(
                f"| {k} | {v6_det_fa[k]:.1f}% | {v4_det_fa[k]:.1f}% | {arrow} {delta:+.1f}% |\n"
            )
        f.write("\n")

        f.write("### 复读检测\n\n")
        f.write(
            f"- v6 复读: **{v6_det_rep}/{len(results_detailed)}** ({v6_det_rep / len(results_detailed) * 100:.1f}%)\n"
        )
        f.write(
            f"- v4 复读: **{v4_det_rep}/{len(results_detailed)}** ({v4_det_rep / len(results_detailed) * 100:.1f}%)\n\n"
        )

        # 详细表
        f.write("## 三、详细对比（简单 prompt）\n\n")
        f.write(
            "| 标的 | v6 建议 | v4 建议 | v6 字段 | v4 字段 | 相似度 | v6 复读 | v4 复读 |\n|---|---|---|---|---|---|---|---|\n"
        )
        for r in results_simple:
            v6f = sum(r["v6_fields"].values())
            v4f = sum(r["v4_fields"].values())
            same = "✅" if r["v6_advice"] == r["v4_advice"] else "⚠️"
            f.write(
                f"| {same} {r['code']} {r['name'][:6]} | {r['v6_advice']} | {r['v4_advice']} | "
                f"{v6f}/5 | {v4f}/5 | {r['similarity']:.0f}% | {r['v6_rep']} | {r['v4_rep']} |\n"
            )

        # 典型输出
        f.write("\n## 四、典型输出对比（茅台 600519，详细 prompt）\n\n")
        m = next((r for r in results_detailed if r["code"] == "600519"), None)
        if m:
            f.write(
                f"**v6 输出**（建议={m['v6d_advice']}，字段={sum(m['v6d_fields'].values())}/5）：\n\n"
            )
            f.write(f"```\n{m['v6d_resp'][:800]}\n```\n\n")
            f.write(
                f"**v4 输出**（建议={m['v4d_advice']}，字段={sum(m['v4d_fields'].values())}/5）：\n\n"
            )
            f.write(f"```\n{m['v4d_resp'][:800]}\n```\n\n")

        # 结论
        f.write("## 五、结论\n\n")
        f.write("### v6 相对 v4 的提升\n\n")
        f.write("| 维度 | 提升方向 | 说明 |\n|---|---|---|\n")
        for k in ["趋势", "技术指标", "支撑压力", "操作建议", "风险提示"]:
            d_simple = v6_simple_fa[k] - v4_simple_fa[k]
            d_det = v6_det_fa[k] - v4_det_fa[k]
            f.write(
                f"| {k} | {('↑' if d_det > 0 else '↓')} | 简单 {d_simple:+.1f}%, 详细 {d_det:+.1f}% |\n"
            )
        f.write("\n")

        if v6_simple_fa["风险提示"] > v4_simple_fa["风险提示"]:
            f.write("- ✅ v6 **风险提示**能力提升\n")
        else:
            f.write("- ⚠️ v6 **风险提示**能力未提升（训练数据不足）\n")

        if v6_simple_rep > v4_simple_rep:
            f.write(f"- ⚠️ v6 **复读问题比 v4 严重**（{v6_simple_rep} vs {v4_simple_rep}）\n")
            f.write("  → 训练数据可能存在重复模式，建议 v7 加 repetition_penalty 或 num_beams\n")
        else:
            f.write("- v6 复读问题与 v4 相近\n")

        # 总结
        if same_simple >= 20:
            f.write(f"- ✅ v6 与 v4 建议高度一致（{same_simple}/30）→ v6 未破坏 v4 能力\n")
        else:
            f.write(f"- ⚠️ v6 与 v4 建议分歧较大（{30 - same_simple} 不同）\n")

    print("\n=== 完成 ===")
    print(f"  报告: {REPORT}")
    print(f"  简单 prompt 一致: {same_simple}/30")
    print(f"  详细 prompt 一致: {same_det}/30")


if __name__ == "__main__":
    main()
