"""v6-only 推理（独立进程，避免和 v4 共享 GPU OOM）
输出 JSONL 到 /tmp/v6_results.jsonl
"""

import json
import re
import time
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

import pandas as pd  # noqa: E402
import torch  # noqa: E402
from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig  # noqa: E402

PARQUET_DIR = Path("/home/jiuben/tdx-data-feed/data/parquet/daily")
V6_MERGED = "/home/jiuben/models/Qwen3-14B-tdx-v6-merged"
OUT_JSONL = Path("/tmp/v6_results.jsonl")

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


def detect_repetition(text):
    tokens = re.findall(r"[\u4e00-\u9fff]{4,}", text)
    if len(tokens) < 6:
        return 0
    cnt = {}
    for t in tokens:
        cnt[t] = cnt.get(t, 0) + 1
    return max(cnt.values())


def main():
    print("=== v6-only 推理（独立进程）===")
    print(f"  模型: {V6_MERGED}")
    print(f"  输出: {OUT_JSONL}")

    OUT_JSONL.unlink(missing_ok=True)
    f_out = OUT_JSONL.open("w", encoding="utf-8")

    tokenizer = AutoTokenizer.from_pretrained(V6_MERGED, trust_remote_code=True)
    bnb = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_quant_type="nf4",
        bnb_4bit_compute_dtype=torch.bfloat16,
        bnb_4bit_use_double_quant=True,
    )
    print("  加载 v6 (int4 nf4)...")
    model = AutoModelForCausalLM.from_pretrained(
        V6_MERGED, quantization_config=bnb, device_map="cuda:1", trust_remote_code=True
    )
    model.eval()
    print("  ✓ 加载完成")

    for prompt_kind in ["simple", "detailed"]:
        print(f"\n--- v6 / {prompt_kind} ---")
        t0 = time.time()
        for i, (code, name) in enumerate(CORE_STOCKS):
            last_date, input_str, n = build_input(code)
            if not input_str:
                continue

            if prompt_kind == "simple":
                prompt = (
                    f"### Instruction:\n你是专业的A股量化分析师。请基于以下截至{last_date}的近{n}个交易日行情数据，"
                    f"对股票 {name}（{code}）做技术面分析并给出操作建议。\n\n### Input:\n{input_str}\n\n### Response:\n"
                )
            else:
                prompt = (
                    f"### Instruction:\n你是专业的A股量化分析师，已通过 v5 微调。\n"
                    f"请基于以下截至{last_date}的近{n}个交易日行情数据，"
                    f"对 {name}（{code}）做技术面分析并给出操作建议。\n"
                    f"输出要求（请严格遵守）：\n"
                    f"1. 趋势判断：明确（上涨/下跌/横盘/震荡）\n"
                    f"2. 技术指标：列出 MA5/MA20、MACD 状态、RSI 数值\n"
                    f"3. 支撑压力：给出具体价位\n"
                    f"4. 操作建议：明确（关注低吸/持股观察/逢高减仓/观望）\n"
                    f"5. 风险提示：1-2 句话\n"
                    f"格式：200 字以内中文段落，不要分点。\n\n### Input:\n{input_str}\n\n### Response:\n"
                )

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

            row = {
                "model": "v6",
                "prompt": prompt_kind,
                "code": code,
                "name": name,
                "advice": extract_advice(resp),
                "fields": field_score(resp),
                "rep": detect_repetition(resp),
                "response": resp,
            }
            f_out.write(json.dumps(row, ensure_ascii=False) + "\n")
            f_out.flush()
            f_sum = sum(row["fields"].values())
            print(
                f"  [{i + 1:2d}/30] {code} {name[:6]:6} 建议={row['advice']:<6} 字段={f_sum}/5 rep={row['rep']}"
            )

        print(f"  耗时: {(time.time() - t0) / 60:.1f} 分钟")

    f_out.close()
    print(f"\n✓ JSONL: {OUT_JSONL}")


if __name__ == "__main__":
    main()
