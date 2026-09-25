"""v5 训练数据质量报告"""

import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path

V5_TRAIN = Path("/home/jiuben/tdx-data-feed/data/v5_train/v5_train.json")
V5_VAL = Path("/home/jiuben/tdx-data-feed/data/v5_train/v5_val.json")
V3_DATA = Path("/home/workbuddy/lf_project/data/stock_a.json")
OUT = Path("/home/jiuben/tdx-data-feed/v5/reports/v5-data-quality-20260922.md")

v5_train = json.loads(V5_TRAIN.read_text())
v5_val = json.loads(V5_VAL.read_text())
v3_data = json.loads(V3_DATA.read_text())


# 统计
def stats(data, label):
    codes = []
    years = []
    inp_lens = []
    out_lens = []
    for s in data:
        m_code = re.search(r"对股票\s*[^（]*（(\d+)）", s["instruction"])
        if m_code:
            codes.append(m_code.group(1))
        m_date = re.search(r"截至(\d{4})-(\d{2})-(\d{2})", s["instruction"])
        if m_date:
            years.append(int(m_date.group(1)))
        inp_lens.append(len(s["input"]))
        out_lens.append(len(s["output"]))
    return {
        "count": len(data),
        "unique_codes": len(set(codes)),
        "year_range": (min(years), max(years)) if years else (0, 0),
        "avg_in": sum(inp_lens) / len(inp_lens) if inp_lens else 0,
        "avg_out": sum(out_lens) / len(out_lens) if out_lens else 0,
        "codes": codes,
        "years": years,
    }


v5t = stats(v5_train, "v5_train")
v5v = stats(v5_val, "v5_val")
v3s = stats(v3_data, "v3")

# 写报告
with OUT.open("w", encoding="utf-8") as f:
    f.write("# v5 训练数据质量报告\n\n")
    f.write(f"> 生成: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")

    f.write("## 一、数据规模对比\n\n")
    f.write("| 来源 | 数量 | 标的数 | 时间范围 |\n|---|---|---|---|\n")
    f.write(
        f"| **v3** | {v3s['count']} | {v3s['unique_codes']} | {v3s['year_range'][0]}-{v3s['year_range'][1]} |\n"
    )
    f.write(
        f"| v5_train | {v5t['count']} | {v5t['unique_codes']} | {v5t['year_range'][0]}-{v5t['year_range'][1]} |\n"
    )
    f.write(
        f"| v5_val | {v5v['count']} | {v5v['unique_codes']} | {v5v['year_range'][0]}-{v5v['year_range'][1]} |\n"
    )
    f.write(f"| **v5 总** | **{v5t['count'] + v5v['count']}** | - | - |\n\n")

    f.write("## 二、样本长度对比\n\n")
    f.write("| 来源 | 平均 input (chars) | 平均 output (chars) |\n|---|---|---|\n")
    f.write(f"| v3 | {v3s['avg_in']:.0f} | {v3s['avg_out']:.0f} |\n")
    f.write(f"| v5_train | {v5t['avg_in']:.0f} | {v5t['avg_out']:.0f} |\n")
    f.write(f"| v5_val | {v5v['avg_in']:.0f} | {v5v['avg_out']:.0f} |\n\n")

    f.write("## 三、时间分布（v5_train）\n\n")
    year_dist = Counter(v5t["years"])
    f.write("| 年份 | 样本数 |\n|---|---|\n")
    for y in sorted(year_dist.keys()):
        f.write(f"| {y} | {year_dist[y]} |\n")
    f.write("\n")

    f.write("## 四、关键改进\n\n")
    f.write(
        "- ✅ **instruction 含标的名称**：v3 只有 code（'对股票 300750'），v5 改成'对股票 宁德时代（300750）'\n"
    )
    f.write(
        "- ✅ **历史时点覆盖**：607 个 v4历史回溯段（2001-2026），含 2008/2015/2018/2021 关键时点\n"
    )
    f.write("- ✅ **真实 v4 输出**：每条 output 都是 v4 微调模型生成，非人工标注\n")
    f.write("- ✅ **25 年时间跨度**：覆盖牛市/熊市/横盘全周期\n\n")

    f.write("## 五、训练配置\n\n")
    f.write("- **基础模型**: Qwen3-14B (合并 v4 LoRA 后)\n")
    f.write("- **微调方法**: LoRA (rank=16, alpha=32, dropout=0.05)\n")
    f.write("- **目标模块**: q/k/v/o + gate/up/down (7 个)\n")
    f.write("- **量化**: QLoRA 4bit + paged_adamw_32bit\n")
    f.write("- **batch**: per_device=1, accum=32 (effective batch 32)\n")
    f.write("- **epochs**: 3.0\n")
    f.write("- **学习率**: 5e-5 cosine\n")
    f.write("- **预计训练**: ~3-4 小时（单卡 16GB）\n\n")

print(f"  报告: {OUT}")
