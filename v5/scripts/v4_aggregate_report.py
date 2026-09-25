"""v4 历史回溯聚合分析报告
输入: 30 只 vault 笔记的 ## v4历史回溯 段
输出: v4-aggregate-2026-09-22.md 报告
内容:
  1. v4 操作建议分布（每只建议类型）
  2. 关键时点判断准确度（2007 牛顶/2008 暴跌/2015 牛顶/2018 暴跌/2021 牛顶）
  3. 每只"最佳买点"提炼
  4. v4 判断 vs 事后实际收益
"""

import re
from datetime import datetime
from pathlib import Path

import pandas as pd

VAULT_DIR = Path("/home/jiuben/StockVault/01-标的")
PARQUET_DIR = Path("/home/jiuben/tdx-data-feed/data/parquet/daily")
REPORT_DIR = Path("/home/jiuben/tdx-data-feed/v5/reports")

# 关键时点（用于验证 v4 判断准确度）
KEY_DATES = {
    "20071228": ("2007 大牛市顶", "茅台 230 → 2008 108 -53%"),
    "20081231": ("2008 金融危机底", "茅台 108 → 2009 169 +56%"),
    "20151231": ("2015 杠杆牛顶", "茅台 207 → 2016 334 +61%"),
    "20181228": ("2018 中美贸易战底", "茅台 590 → 2019 1183 +100%"),
    "20211231": ("2021 消费顶", "茅台 2050 → 2022 1727 -16%"),
    "20221230": ("2022 政策底", "茅台 1727 → 2024 1524 -12%"),
}


def parse_date(d) -> str:
    if isinstance(d, pd.Timestamp):
        return d.strftime("%Y-%m-%d")
    if isinstance(d, (int, float)):
        s = str(int(d))
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}" if len(s) == 8 else s
    return str(d)[:10]


def get_current_price(code: str) -> tuple[float, str]:
    """返回 (现价, 现价日期)"""
    for prefix in ("sh", "sz", "bj"):
        p = PARQUET_DIR / f"{prefix}{code}.parquet"
        if p.exists():
            df = pd.read_parquet(p, columns=["date", "close"])
            df = df.sort_values("date").reset_index(drop=True)
            return float(df.iloc[-1]["close"]), parse_date(df.iloc[-1]["date"])
    return 0.0, "?"


def get_price_at(code: str, date_int: int) -> float:
    """返回指定日期收盘价"""
    for prefix in ("sh", "sz", "bj"):
        p = PARQUET_DIR / f"{prefix}{code}.parquet"
        if p.exists():
            df = pd.read_parquet(p, columns=["date", "close"])
            df = df[df["date"] <= date_int].reset_index(drop=True)
            if len(df) > 0:
                return float(df.iloc[-1]["close"])
    return 0.0


def parse_history_table(md_text: str) -> list[dict]:
    """解析 ## v4历史回溯 (YYYY-YYYY) 表格
    表格格式: | 时点 | 耗时 | 当时技术面分析 |"""
    # 找历史回溯段
    m = re.search(r"## v4历史回溯 \(\d{4}-\d{4}\)\n(.*?)(?=\n## |\Z)", md_text, re.DOTALL)
    if not m:
        return []
    section = m.group(1)

    # 提取表格行
    rows = []
    for line in section.split("\n"):
        if not line.startswith("|") or line.startswith("|------") or "时点 | 耗时" in line:
            continue
        parts = [p.strip() for p in line.split("|")[1:-1]]
        if len(parts) < 3:
            continue
        date_str, elapsed, analysis = parts[0], parts[1], parts[2]
        if len(date_str) != 8 or not date_str.isdigit():
            continue

        # 提取关键信息
        row = {
            "date": date_str,
            "date_fmt": f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}",
            "elapsed": elapsed,
            "trend": "",
            "price": 0.0,
            "advice": "",
            "raw": analysis,
        }
        # 趋势
        if "上涨态势" in analysis:
            row["trend"] = "上涨"
        elif "下跌态势" in analysis:
            row["trend"] = "下跌"
        elif "横盘态势" in analysis:
            row["trend"] = "横盘"
        elif "震荡" in analysis:
            row["trend"] = "震荡"

        # 价格
        pm = re.search(r"股价(?:为)?(?:约)?(\d+\.?\d*)", analysis)
        if pm:
            row["price"] = float(pm.group(1))

        # 操作建议
        am = re.search(r"建议([\u4e00-\u9fff]+?)(?:[。,.]|$)", analysis)
        if am:
            advice = am.group(1)
            if "低吸" in advice or "关注" in advice or "回调" in advice:
                row["advice"] = "关注低吸"
            elif "减仓" in advice or "高抛" in advice:
                row["advice"] = "逢高减仓"
            elif "持股" in advice:
                row["advice"] = "持股观察"
            elif "观望" in advice or "谨慎" in advice:
                row["advice"] = "观望谨慎"
            else:
                row["advice"] = advice[:8]
        rows.append(row)
    return rows


def aggregate_report():
    """主报告生成"""
    # 找所有含 ## v4历史回溯 的笔记
    notes = []
    for f in VAULT_DIR.iterdir():
        if not f.name.endswith(".md") or "MOC" in f.name:
            continue
        text = f.read_text(encoding="utf-8")
        if "## v4历史回溯" not in text:
            continue
        # 解析 code/name
        m = re.match(r"(\d{6})-(.+)\.md", f.name)
        if m:
            code, name = m.group(1), m.group(2)
        else:
            code, name = "?", f.stem
        rows = parse_history_table(text)
        if rows:
            notes.append({"code": code, "name": name, "file": f, "rows": rows})

    # 排序按 code
    notes.sort(key=lambda x: x["code"])

    # 统计
    total_timepoints = sum(len(n["rows"]) for n in notes)
    advice_dist = {"关注低吸": 0, "持股观察": 0, "逢高减仓": 0, "观望谨慎": 0, "其他": 0}
    for n in notes:
        for r in n["rows"]:
            advice_dist[r["advice"] if r["advice"] in advice_dist else "其他"] += 1

    # 关键时点判断
    key_judgments = []
    for date_int, (label, actual) in KEY_DATES.items():
        judgments = []
        for n in notes:
            for r in n["rows"]:
                if r["date"] == date_int:
                    judgments.append(
                        {
                            "code": n["code"],
                            "name": n["name"],
                            "trend": r["trend"],
                            "advice": r["advice"],
                            "price_then": r["price"],
                        }
                    )
                    break
        key_judgments.append(
            {"date": date_int, "label": label, "actual": actual, "judgments": judgments}
        )

    # 每只最佳买点：v4 建议"关注低吸"且之后大涨
    best_buys = []
    for n in notes:
        _cur_price, cur_date = get_current_price(n["code"])
        for r in n["rows"]:
            if r["advice"] == "关注低吸" and r["price"] > 0:
                # 假设持有到现在
                cur_price_val, _cur_date_val = get_current_price(n["code"])
                # 或者持有 1 年
                year_after = int(r["date"]) + 10000
                get_price_at(n["code"], year_after) if r["date"][:4] < "2025" else 0
                ret_to_now = (
                    round((cur_price_val - r["price"]) / r["price"] * 100, 1)
                    if r["price"] > 0
                    else 0
                )
                actual_price = cur_price_val
                best_buys.append(
                    {
                        "code": n["code"],
                        "name": n["name"],
                        "date": r["date_fmt"],
                        "price_then": r["price"],
                        "cur_price": actual_price,
                        "cur_date": cur_date,
                        "ret_to_now": ret_to_now,
                        "advice": r["advice"],
                    }
                )
    best_buys.sort(key=lambda x: x["ret_to_now"], reverse=True)

    # 写报告
    out = REPORT_DIR / f"v4-aggregate-{datetime.now().strftime('%Y%m%d')}.md"
    with out.open("w", encoding="utf-8") as f:
        f.write("# v4 历史回溯聚合分析报告\n\n")
        f.write(f"> 生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"> 数据范围: {len(notes)} 只核心股, {total_timepoints} 个时点\n")
        f.write("> 模型: qwen3-14b-tdx-fast:14b\n\n")

        f.write("## 一、操作建议分布\n\n")
        f.write("| 建议类型 | 数量 | 占比 |\n|---|---|---|\n")
        for k, v in advice_dist.items():
            pct = round(v / total_timepoints * 100, 1)
            f.write(f"| {k} | {v} | {pct}% |\n")
        f.write(f"| **总计** | **{total_timepoints}** | **100%** |\n\n")

        f.write("## 二、关键时点 v4 判断准确度\n\n")
        for kj in key_judgments:
            f.write(f"### {kj['date'][:4]}-{kj['date'][4:6]}-{kj['date'][6:8]} · {kj['label']}\n\n")
            f.write(f"**事后实际**: {kj['actual']}\n\n")
            f.write("| 标的 | v4 趋势 | v4 建议 | 当时价 |\n|---|---|---|---|\n")
            for j in kj["judgments"][:10]:
                f.write(
                    f"| {j['code']} {j['name'][:6]} | {j['trend']} | {j['advice']} | {j['price_then']:.1f} |\n"
                )
            f.write("\n")

        f.write('## 三、v4 建议"关注低吸" → 实际收益（事后验证）\n\n')
        f.write('> 假设在 v4 建议"关注低吸"时点买入，持有到现在\n\n')
        f.write("| 排名 | 标的 | 时点 | 当时价 | 现价 | 收益 |\n|---|---|---|---|---|---|\n")
        for i, b in enumerate(best_buys[:30], 1):
            f.write(
                f"| {i} | {b['code']} {b['name'][:8]} | {b['date']} | {b['price_then']:.1f} | {b['cur_price']:.1f} | **{b['ret_to_now']:+.1f}%** |\n"
            )
        f.write("\n")

        # 总结
        if best_buys:
            top10_avg = sum(b["ret_to_now"] for b in best_buys[:10]) / min(10, len(best_buys))
            all_avg = sum(b["ret_to_now"] for b in best_buys) / len(best_buys)
            win_rate = sum(1 for b in best_buys if b["ret_to_now"] > 0) / len(best_buys) * 100
            f.write("### 验证结果\n\n")
            f.write(f"- **Top 10 平均收益**: {top10_avg:+.1f}%\n")
            f.write(f"- **全部平均收益**: {all_avg:+.1f}%\n")
            f.write(f"- **胜率**（收益>0）: {win_rate:.1f}%\n")
            f.write(f"- **样本数**: {len(best_buys)} 个'关注低吸'信号\n\n")

        f.write("## 四、每只核心股 25 年回溯概览\n\n")
        for n in notes:
            f.write(f"### {n['code']} {n['name']}\n\n")
            f.write("| 时点 | 价格 | 趋势 | 操作建议 |\n|---|---|---|---|\n")
            for r in n["rows"]:
                f.write(f"| {r['date_fmt']} | {r['price']:.1f} | {r['trend']} | {r['advice']} |\n")
            f.write("\n")

    return out, {
        "total_timepoints": total_timepoints,
        "advice_dist": advice_dist,
        "best_buys_count": len(best_buys),
        "notes_count": len(notes),
    }


if __name__ == "__main__":
    out, stats = aggregate_report()
    print("\n=== v4 聚合报告完成 ===")
    print(f"  报告: {out}")
    print(f"  30 只核心股: {stats['notes_count']} 个")
    print(f"  总时点: {stats['total_timepoints']} 个")
    print(f"  操作建议分布: {stats['advice_dist']}")
    print(f"  关注低吸信号: {stats['best_buys_count']} 个")
