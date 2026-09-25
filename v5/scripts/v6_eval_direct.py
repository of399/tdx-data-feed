"""v6 直接推理评估（避开 Ollama GGUF 问题）
用 transformers 直接加载 Qwen3-14B + v6 LoRA merged 模型。
GPU 1 已空闲（v5 训练在 GPU 0）。
"""

import json
import re
import time
import warnings
from datetime import datetime
from pathlib import Path

warnings.filterwarnings("ignore")

import pandas as pd  # noqa: E402
import torch  # noqa: E402
from transformers import AutoModelForCausalLM, AutoTokenizer  # noqa: E402

PARQUET_DIR = Path("/home/jiuben/tdx-data-feed/data/parquet/daily")
MERGED = "/home/jiuben/models/Qwen3-14B-tdx-v6-merged"
REPORT = Path(
    f"/home/jiuben/tdx-data-feed/v5/reports/v6-eval-direct-{datetime.now().strftime('%Y%m%d-%H%M')}.md"
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


def parse_date(d) -> str:
    if isinstance(d, pd.Timestamp):
        return d.strftime("%Y-%m-%d")
    if isinstance(d, (int, float)):
        s = str(int(d))
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}" if len(s) == 8 else s
    return str(d)[:10]


def build_input(code: str, window: int = 20):
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
    return {
        "趋势": 1 if re.search(r"(上涨|下跌|横盘|震荡)", text) else 0,
        "技术指标": 1 if re.search(r"(MA\d+|MACD|RSI)", text) else 0,
        "支撑压力": 1 if re.search(r"支撑|压力", text) else 0,
        "操作建议": 1 if re.search(r"建议|关注|减仓|持股|观望", text) else 0,
        "风险提示": 1 if re.search(r"风险", text) else 0,
    }


def main():
    print("=== v6 直接推理评估 ===")
    print(f"  模型: {MERGED}")
    print("\n加载模型（~30-60s）...")

    t0 = time.time()
    tokenizer = AutoTokenizer.from_pretrained(MERGED, trust_remote_code=True)

    from transformers import BitsAndBytesConfig

    bnb = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_quant_type="nf4",
        bnb_4bit_compute_dtype=torch.bfloat16,
        bnb_4bit_use_double_quant=True,
    )
    print("  加载模型: int4 量化（4bit nf4）→ 7GB 显存可塞下")
    model = AutoModelForCausalLM.from_pretrained(
        MERGED,
        quantization_config=bnb,
        device_map="cuda:1",
        trust_remote_code=True,
    )
    model.eval()
    print(f"  ✓ 加载完成 ({time.time() - t0:.1f}s)")
    print(f"  device: {next(model.parameters()).device}")

    results = []
    t_total = time.time()
    for i, (code, name) in enumerate(CORE_STOCKS):
        last_date, input_str, n = build_input(code)
        if not input_str:
            print(f"  ⚠ {code} {name}: no data")
            continue

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

        inputs = tokenizer(prompt, return_tensors="pt").to("cuda:1")
        t1 = time.time()
        with torch.no_grad():
            outputs = model.generate(
                **inputs,
                max_new_tokens=350,
                temperature=0.3,
                top_p=0.95,
                top_k=20,
                do_sample=True,
                pad_token_id=tokenizer.eos_token_id,
            )
        dur = time.time() - t1
        response = tokenizer.decode(
            outputs[0][inputs.input_ids.shape[1] :], skip_special_tokens=True
        ).strip()

        results.append(
            {
                "code": code,
                "name": name,
                "last_date": last_date,
                "advice": extract_advice(response),
                "fields": field_score(response),
                "response": response,
                "tokens": outputs.shape[1] - inputs.input_ids.shape[1],
                "duration": round(dur, 2),
            }
        )

        f_sum = sum(results[-1]["fields"].values())
        print(
            f"  [{i + 1:2d}/30] {code} {name[:6]:6} 建议={results[-1]['advice']:<6} 字段={f_sum}/5 {dur:.1f}s"
        )

    elapsed = time.time() - t_total
    print("\n=== 完成 ===")
    print(f"  30 股耗时: {elapsed:.1f}s ({elapsed / 30:.1f}s/股)")

    advice_dist = pd.Series([r["advice"] for r in results]).value_counts()
    field_avg = {
        k: sum(r["fields"][k] for r in results) / len(results) * 100
        for k in ["趋势", "技术指标", "支撑压力", "操作建议", "风险提示"]
    }

    with REPORT.open("w", encoding="utf-8") as f:
        f.write("# v6 直接推理评估（transformers + merged safetensors）\n\n")
        f.write(f"> 生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write("> 模型: Qwen3-14B + v6 LoRA merged → safetensors\n")
        f.write("> 推理设备: GPU 1，dtype: bfloat16\n")
        f.write("> 评估范围: 30 只核心股\n\n")

        f.write("## 一、推理性能\n\n")
        f.write(f"- 平均延迟: **{elapsed / 30:.2f}s/股**\n")
        f.write(f"- 平均 token 数: {sum(r['tokens'] for r in results) / len(results):.0f}\n\n")

        f.write("## 二、建议分布\n\n")
        f.write("| 建议类型 | 数量 | 占比 |\n|---|---|---|\n")
        for adv, n in advice_dist.items():
            f.write(f"| {adv} | {n} | {n / len(results) * 100:.1f}% |\n")
        f.write("\n")

        f.write("## 三、字段完整度（v6 训练硬要求）\n\n")
        f.write("| 字段 | 命中率 |\n|---|---|\n")
        for k, v in field_avg.items():
            f.write(f"| {k} | {v:.1f}% |\n")
        f.write("\n")

        f.write("## 四、详细结果\n\n")
        f.write("| 标的 | 建议 | 字段 | tokens | 耗时 |\n|---|---|---|---|---|\n")
        for r in results:
            f_sum = sum(r["fields"].values())
            f.write(
                f"| {r['code']} {r['name'][:6]} | {r['advice']} | {f_sum}/5 | {r['tokens']} | {r['duration']:.1f}s |\n"
            )

        f.write("\n## 五、典型输出（茅台 600519）\n\n")
        m = next((r for r in results if r["code"] == "600519"), None)
        if m:
            f.write(f"**建议**: {m['advice']}\n**字段**: {sum(m['fields'].values())}/5\n\n")
            f.write(f"```\n{m['response']}\n```\n\n")

        f.write("## 六、结论\n\n")
        avg_fields = sum(sum(r["fields"].values()) for r in results) / len(results)
        f.write("- v6 模型加载成功，merged safetensors 推理正常\n")
        f.write(f"- 平均延迟 {elapsed / 30:.2f}s/股\n")
        f.write(f"- 字段完整度平均 {avg_fields:.1f}/5\n")
        if avg_fields >= 4.0:
            f.write("- ✅ **v6 5 项训练硬要求被学会**\n")
        else:
            f.write("- ⚠️ **部分字段未稳定学会**\n")

    print(f"✓ 报告: {REPORT}")


if __name__ == "__main__":
    main()
