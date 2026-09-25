"""v6 vs v4 sequential baseline（避开 Ollama OOM）
- sequential：先 v4 → 释放 → v6
- int4 量化：每个 7GB，单卡 15.5GB 够用
"""

import gc
import json
import re
import time
import warnings
from datetime import datetime
from pathlib import Path

warnings.filterwarnings("ignore")

import pandas as pd  # noqa: E402
import torch  # noqa: E402
from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig  # noqa: E402

PARQUET_DIR = Path("/home/jiuben/tdx-data-feed/data/parquet/daily")
V4_MERGED = "/home/jiuben/models/Qwen3-14B-tdx-fast-merged-safetensors"
V6_MERGED = "/home/jiuben/models/Qwen3-14B-tdx-v6-merged"
REPORT = Path(
    f"/home/jiuben/tdx-data-feed/v5/reports/v6_vs_v4-seq-{datetime.now().strftime('%Y%m%d-%H%M')}.md"
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

def PROMPT_SIMPLE(name, code, last_date, n, input_str):
    return (
    f"### Instruction:\n"
    f"你是专业的A股量化分析师。请基于以下截至{last_date}的近{n}个交易日行情数据，"
    f"对股票 {name}（{code}）做技术面分析并给出操作建议。\n\n"
    f"### Input:\n{input_str}\n\n### Response:\n"
)

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
    tokens = re.findall(r"[\u4e00-\u9fff]{4,}", text)
    if len(tokens) < 6:
        return 0
    cnt = {}
    for t in tokens:
        cnt[t] = cnt.get(t, 0) + 1
    return max(cnt.values())


def load_int4(path):
    print(f"\n  加载 {path.split('/')[-1]}...")
    t0 = time.time()
    tokenizer = AutoTokenizer.from_pretrained(path, trust_remote_code=True)
    bnb = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_quant_type="nf4",
        bnb_4bit_compute_dtype=torch.bfloat16,
        bnb_4bit_use_double_quant=True,
    )
    model = AutoModelForCausalLM.from_pretrained(
        path, quantization_config=bnb, device_map="cuda:1", trust_remote_code=True
    )
    model.eval()
    print(f"    ✓ {time.time() - t0:.1f}s")
    return tokenizer, model


def free_model(model):
    del model
    gc.collect()
    torch.cuda.empty_cache()


def run_model(name, model, tokenizer, prompt_kind):
    """name: 'v4' or 'v6', prompt_kind: 'simple' or 'detailed'"""
    results = []
    print(f"\n--- {name} / {prompt_kind} ---")
    t0 = time.time()
    for i, (code, sname) in enumerate(CORE_STOCKS):
        last_date, input_str, n = build_input(code)
        if not input_str:
            continue
        if prompt_kind == "simple":
            prompt = PROMPT_SIMPLE(sname, code, last_date, n, input_str)
        else:
            prompt = PROMPT_DETAILED(sname, code, last_date, n, input_str)

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
        resp = tokenizer.decode(
            out[0][inputs.input_ids.shape[1] :], skip_special_tokens=True
        ).strip()

        results.append(
            {
                "code": code,
                "name": sname,
                "resp": resp,
                "advice": extract_advice(resp),
                "fields": field_score(resp),
                "rep": detect_repetition(resp),
            }
        )
        f_sum = sum(results[-1]["fields"].values())
        print(
            f"  [{i + 1:2d}/30] {code} {sname[:6]:6} 建议={results[-1]['advice']:<6} 字段={f_sum}/5 rep={results[-1]['rep']}"
        )

    print(f"  耗时: {(time.time() - t0) / 60:.1f} 分钟")
    return results


def main():
    print("=== v6 vs v4 sequential baseline（int4 量化）===")
    print(f"  v4 merged: {V4_MERGED}")
    print(f"  v6 merged: {V6_MERGED}")

    # ===== v4 推理 =====
    tok4, m4 = load_int4(V4_MERGED)
    v4_simple = run_model("v4", m4, tok4, "simple")
    v4_detailed = run_model("v4", m4, tok4, "detailed")
    free_model(m4)
    del tok4

    # ===== v6 推理 =====
    tok6, m6 = load_int4(V6_MERGED)
    v6_simple = run_model("v6", m6, tok6, "simple")
    v6_detailed = run_model("v6", m6, tok6, "detailed")
    free_model(m6)
    del tok6

    # ===== 写报告 =====
    def field_avg(rs):
        return {
            k: sum(r["fields"][k] for r in rs) / len(rs) * 100
            for k in ["趋势", "技术指标", "支撑压力", "操作建议", "风险提示"]
        }

    v6s_fa = field_avg(v6_simple)
    v4s_fa = field_avg(v4_simple)
    v6d_fa = field_avg(v6_detailed)
    v4d_fa = field_avg(v4_detailed)

    same_simple = sum(1 for a, b in zip(v6_simple, v4_simple, strict=False) if a["advice"] == b["advice"])
    same_det = sum(1 for a, b in zip(v6_detailed, v4_detailed, strict=False) if a["advice"] == b["advice"])

    v6s_rep = sum(1 for r in v6_simple if r["rep"] >= 3)
    v4s_rep = sum(1 for r in v4_simple if r["rep"] >= 3)
    v6d_rep = sum(1 for r in v6_detailed if r["rep"] >= 3)
    v4d_rep = sum(1 for r in v4_detailed if r["rep"] >= 3)

    with REPORT.open("w", encoding="utf-8") as f:
        f.write("# v6 vs v4 (fast) sequential baseline 对比\n\n")
        f.write(f"> 生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write("> v6: Qwen3-14B + v6 LoRA merged (int4 nf4)\n")
        f.write("> v4: Qwen3-14B + v4 fast LoRA merged (int4 nf4)\n")
        f.write("> 设备: GPU 1, sequential (v4 → release → v6)\n")
        f.write("> 评估范围: 30 只核心股 × 2 prompt (simple/detailed)\n\n")

        f.write("## 一、简单 prompt（公平对比）\n\n")
        f.write("### 建议一致率\n\n")
        f.write(f"- v6 == v4: **{same_simple}/30** ({same_simple / 30 * 100:.1f}%)\n\n")

        f.write("### 字段完整度\n\n")
        f.write("| 字段 | v6 | v4 | v6 - v4 |\n|---|---|---|---|\n")
        for k in ["趋势", "技术指标", "支撑压力", "操作建议", "风险提示"]:
            d = v6s_fa[k] - v4s_fa[k]
            arrow = "↑" if d > 0 else ("↓" if d < 0 else "→")
            f.write(f"| {k} | {v6s_fa[k]:.1f}% | {v4s_fa[k]:.1f}% | {arrow} {d:+.1f}% |\n")
        f.write("\n")

        f.write("### 复读检测（≥3 次）\n\n")
        f.write(f"- v6 复读: **{v6s_rep}/30** ({v6s_rep / 30 * 100:.1f}%)\n")
        f.write(f"- v4 复读: **{v4s_rep}/30** ({v4s_rep / 30 * 100:.1f}%)\n\n")

        f.write("## 二、详细 prompt（v6 优势 prompt）\n\n")
        f.write("### 建议一致率\n\n")
        f.write(f"- v6 == v4: **{same_det}/30** ({same_det / 30 * 100:.1f}%)\n\n")

        f.write("### 字段完整度\n\n")
        f.write("| 字段 | v6 | v4 | v6 - v4 |\n|---|---|---|---|\n")
        for k in ["趋势", "技术指标", "支撑压力", "操作建议", "风险提示"]:
            d = v6d_fa[k] - v4d_fa[k]
            arrow = "↑" if d > 0 else ("↓" if d < 0 else "→")
            f.write(f"| {k} | {v6d_fa[k]:.1f}% | {v4d_fa[k]:.1f}% | {arrow} {d:+.1f}% |\n")
        f.write("\n")

        f.write("### 复读检测\n\n")
        f.write(f"- v6 复读: **{v6d_rep}/30** ({v6d_rep / 30 * 100:.1f}%)\n")
        f.write(f"- v4 复读: **{v4d_rep}/30** ({v4d_rep / 30 * 100:.1f}%)\n\n")

        # 详细对比（简单 prompt）
        f.write("## 三、详细对比（简单 prompt）\n\n")
        f.write(
            "| 标的 | v6 建议 | v4 建议 | v6 字段 | v4 字段 | v6 复读 | v4 复读 |\n|---|---|---|---|---|---|---|\n"
        )
        for a, b in zip(v6_simple, v4_simple, strict=False):
            v6f, v4f = sum(a["fields"].values()), sum(b["fields"].values())
            same = "✅" if a["advice"] == b["advice"] else "⚠️"
            f.write(
                f"| {same} {a['code']} {a['name'][:6]} | {a['advice']} | {b['advice']} | {v6f}/5 | {v4f}/5 | {a['rep']} | {b['rep']} |\n"
            )

        # 典型输出
        f.write("\n## 四、典型输出对比（茅台 600519，详细 prompt）\n\n")
        m6 = next((r for r in v6_detailed if r["code"] == "600519"), None)
        m4 = next((r for r in v4_detailed if r["code"] == "600519"), None)
        if m6 and m4:
            f.write(
                f"**v6 输出**（建议={m6['advice']}, 字段={sum(m6['fields'].values())}/5, 复读={m6['rep']}）：\n\n"
            )
            f.write(f"```\n{m6['resp'][:800]}\n```\n\n")
            f.write(
                f"**v4 输出**（建议={m4['advice']}, 字段={sum(m4['fields'].values())}/5, 复读={m4['rep']}）：\n\n"
            )
            f.write(f"```\n{m4['resp'][:800]}\n```\n\n")

        # 结论
        f.write("## 五、结论\n\n")
        f.write("### 关键发现\n\n")
        if v6s_fa["风险提示"] > v4s_fa["风险提示"]:
            f.write(
                f"- ✅ **v6 风险提示能力提升**：简单 prompt 下 {v6s_fa['风险提示']:.1f}% vs v4 {v4s_fa['风险提示']:.1f}%\n"
            )
        else:
            f.write(
                f"- ⚠️ **v6 风险提示能力未提升**：{v6s_fa['风险提示']:.1f}% vs v4 {v4s_fa['风险提示']:.1f}%\n"
            )

        if v6s_rep > v4s_rep:
            f.write(f"- ⚠️ **v6 复读问题比 v4 严重**：{v6s_rep} vs {v4s_rep}\n")
        else:
            f.write(f"- ✅ v6 复读控制良好：{v6s_rep} vs {v4s_rep}\n")

        if same_simple >= 20:
            f.write(f"- ✅ v6 与 v4 建议高度一致（{same_simple}/30）\n")
        else:
            f.write("- ⚠️ v6 与 v4 建议分歧较大\n")

        # 总字段平均
        v6_avg_fields = sum(v6d_fa.values()) / 5
        v4_avg_fields = sum(v4d_fa.values()) / 5
        f.write("\n### 字段平均分（详细 prompt）\n\n")
        f.write(f"- v6: **{v6_avg_fields:.1f}%**\n")
        f.write(f"- v4: **{v4_avg_fields:.1f}%**\n")
        if v6_avg_fields > v4_avg_fields:
            f.write(f"- ✅ v6 整体字段覆盖优于 v4（{v6_avg_fields - v4_avg_fields:+.1f}%）\n")
        else:
            f.write("- ⚠️ v6 整体字段覆盖未超过 v4\n")

    print("\n=== 完成 ===")
    print(f"  报告: {REPORT}")
    print(f"  简单 prompt 一致: {same_simple}/30")
    print(f"  详细 prompt 一致: {same_det}/30")


if __name__ == "__main__":
    main()
