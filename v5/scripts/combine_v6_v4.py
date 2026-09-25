"""合并 v4/v6 JSONL 出对比报告"""

import json
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path

V4_JSONL = Path("/tmp/v4_results.jsonl")
V6_JSONL = Path("/tmp/v6_results.jsonl")
REPORT = Path(
    f"/home/jiuben/tdx-data-feed/v5/reports/v6_vs_v4-final-{datetime.now().strftime('%Y%m%d-%H%M')}.md"
)
REPORT.parent.mkdir(parents=True, exist_ok=True)


def load(p):
    if not p.exists():
        return []
    rows = []
    with p.open() as f:
        for l in f:
            l = l.strip()
            if l:
                rows.append(json.loads(l))
    return rows


def field_avg(rs):
    return {
        k: sum(r["fields"][k] for r in rs) / len(rs) * 100
        for k in ["趋势", "技术指标", "支撑压力", "操作建议", "风险提示"]
    }


def similarity(a, b):
    aw = set(re.findall(r"[\u4e00-\u9fff]{2,}", a))
    bw = set(re.findall(r"[\u4e00-\u9fff]{2,}", b))
    if not aw and not bw:
        return 0.0
    return len(aw & bw) / len(aw | bw) * 100


def main():
    print("=== 合并 v4/v6 报告 ===")
    v4 = load(V4_JSONL)
    v6 = load(V6_JSONL)
    print(f"  v4: {len(v4)} 行")
    print(f"  v6: {len(v6)} 行")

    if not v4 or not v6:
        print("❌ 数据缺失")
        return

    v4_by = defaultdict(dict)
    v6_by = defaultdict(dict)
    for r in v4:
        v4_by[r["prompt"]][r["code"]] = r
    for r in v6:
        v6_by[r["prompt"]][r["code"]] = r

    with REPORT.open("w", encoding="utf-8") as f:
        f.write("# v6 vs v4 (fast) baseline 对比（独立进程，int4 量化）\n\n")
        f.write(f"> 生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write("> v4: Qwen3-14B + v4 fast LoRA merged (int4 nf4, GPU 1)\n")
        f.write("> v6: Qwen3-14B + v6 LoRA merged (int4 nf4, GPU 1)\n")
        f.write("> 设备: GPU 1, sequential (v4 → release → v6)\n")
        f.write("> 评估范围: 30 只核心股 × 2 prompt (simple + detailed)\n\n")

        for prompt_kind in ["simple", "detailed"]:
            v4_p = list(v4_by[prompt_kind].values())
            v6_p = list(v6_by[prompt_kind].values())

            title_num = "一" if prompt_kind == "simple" else "二"
            prompt_label = "简单" if prompt_kind == "simple" else "详细"
            f.write(f"## {title_num}、{prompt_label} prompt（{prompt_kind}）\n\n")

            same_count = sum(1 for a, b in zip(v6_p, v4_p, strict=False) if a["advice"] == b["advice"])
            f.write("### 建议一致率\n\n")
            f.write(f"- v6 == v4: **{same_count}/30** ({same_count / 30 * 100:.1f}%)\n\n")

            v6_fa = field_avg(v6_p)
            v4_fa = field_avg(v4_p)
            f.write("### 字段完整度\n\n")
            f.write("| 字段 | v6 | v4 | v6 - v4 |\n|---|---|---|---|\n")
            for k in ["趋势", "技术指标", "支撑压力", "操作建议", "风险提示"]:
                d = v6_fa[k] - v4_fa[k]
                arrow = "↑" if d > 0 else ("↓" if d < 0 else "→")
                f.write(f"| {k} | {v6_fa[k]:.1f}% | {v4_fa[k]:.1f}% | {arrow} {d:+.1f}% |\n")
            f.write("\n")

            v6_rep = sum(1 for r in v6_p if r["rep"] >= 3)
            v4_rep = sum(1 for r in v4_p if r["rep"] >= 3)
            f.write("### 复读检测（≥3 次）\n\n")
            f.write(f"- v6 复读: **{v6_rep}/30** ({v6_rep / 30 * 100:.1f}%)\n")
            f.write(f"- v4 复读: **{v4_rep}/30** ({v4_rep / 30 * 100:.1f}%)\n\n")

            f.write("### 详细对比\n\n")
            f.write(
                "| 标的 | v6 建议 | v4 建议 | v6 字段 | v4 字段 | 相似度 | v6 复读 | v4 复读 |\n|---|---|---|---|---|---|---|---|\n"
            )
            for v6r, v4r in zip(v6_p, v4_p, strict=False):
                v6f, v4f = sum(v6r["fields"].values()), sum(v4r["fields"].values())
                sim = similarity(v6r["response"], v4r["response"])
                same = "✅" if v6r["advice"] == v4r["advice"] else "⚠️"
                f.write(
                    f"| {same} {v6r['code']} {v6r['name'][:6]} | {v6r['advice']} | {v4r['advice']} | "
                    f"{v6f}/5 | {v4f}/5 | {sim:.0f}% | {v6r['rep']} | {v4r['rep']} |\n"
                )

            m6 = next((r for r in v6_p if r["code"] == "600519"), None)
            m4 = next((r for r in v4_p if r["code"] == "600519"), None)
            if m6 and m4:
                f.write("\n### 典型输出对比（茅台 600519）\n\n")
                f.write(
                    f"**v6 输出**（建议={m6['advice']}, 字段={sum(m6['fields'].values())}/5, 复读={m6['rep']}）：\n\n"
                )
                f.write(f"```\n{m6['response'][:600]}\n```\n\n")
                f.write(
                    f"**v4 输出**（建议={m4['advice']}, 字段={sum(m4['fields'].values())}/5, 复读={m4['rep']}）：\n\n"
                )
                f.write(f"```\n{m4['response'][:600]}\n```\n\n")

        f.write("## 三、结论\n\n")
        v6_simple = list(v6_by["simple"].values())
        v4_simple = list(v4_by["simple"].values())
        v6_detailed = list(v6_by["detailed"].values())
        v4_detailed = list(v4_by["detailed"].values())

        same_simple = sum(1 for a, b in zip(v6_simple, v4_simple, strict=False) if a["advice"] == b["advice"])
        same_det = sum(1 for a, b in zip(v6_detailed, v4_detailed, strict=False) if a["advice"] == b["advice"])

        v6s_fa = field_avg(v6_simple)
        v4s_fa = field_avg(v4_simple)
        v6d_fa = field_avg(v6_detailed)
        v4d_fa = field_avg(v4_detailed)

        f.write("### 关键发现\n\n")
        f.write("| 维度 | 简单 prompt | 详细 prompt |\n|---|---|---|\n")
        f.write(f"| 建议一致 | {same_simple}/30 | {same_det}/30 |\n")
        for k in ["趋势", "技术指标", "支撑压力", "操作建议", "风险提示"]:
            d_s = v6s_fa[k] - v4s_fa[k]
            d_d = v6d_fa[k] - v4d_fa[k]
            f.write(f"| {k} 提升 | {d_s:+.1f}% | {d_d:+.1f}% |\n")
        f.write("\n")

        v6s_rep = sum(1 for r in v6_simple if r["rep"] >= 3)
        v4s_rep = sum(1 for r in v4_simple if r["rep"] >= 3)
        v6d_rep = sum(1 for r in v6_detailed if r["rep"] >= 3)
        v4d_rep = sum(1 for r in v4_detailed if r["rep"] >= 3)
        f.write(f"| v6 复读率 | {v6s_rep}/30 | {v6d_rep}/30 |\n")
        f.write(f"| v4 复读率 | {v4s_rep}/30 | {v4d_rep}/30 |\n\n")

        if v6d_fa["风险提示"] >= 80:
            f.write(f"- ✅ 详细 prompt 下 v6 风险提示 {v6d_fa['风险提示']:.1f}% 达标\n")
        else:
            f.write(f"- ⚠️ 详细 prompt 下 v6 风险提示 {v6d_fa['风险提示']:.1f}% 未达标\n")

        if v6s_rep > 20:
            f.write(f"- ⚠️ v6 复读严重（{v6s_rep}/30）→ 需 repetition_penalty=1.3+\n")
        elif v6s_rep < 5:
            f.write("- ✅ v6 复读控制良好\n")

        v6_avg = sum(v6d_fa.values()) / 5
        v4_avg = sum(v4d_fa.values()) / 5
        f.write("\n### 综合建议\n\n")
        if v6_avg > v4_avg:
            f.write(f"- ✅ v6 综合字段覆盖 **{v6_avg:.1f}%** > v4 {v4_avg:.1f}%\n")
            f.write("- 推荐：v6 可作为推荐模型，但需修复复读问题\n")
        else:
            f.write("- ⚠️ v6 综合字段覆盖未超过 v4\n")
            f.write("- 需要 v7 训练：加防复读数据 + 风险提示样本\n")

    print(f"\n✓ 报告: {REPORT}")


if __name__ == "__main__":
    main()
