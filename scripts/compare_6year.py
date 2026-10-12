"""scripts/compare_6year.py — 5 模型 × 6 年(2020-2025)完整窗口对比

运行 sim_live × 5 次(每个模型 1 次),然后解析每模型的 per-year 表,
合并成综合报告 + 比较图。

输出:
  - data/plan_e/compare_6year_<timestamp>.csv  全量 5 模型 × 6 年矩阵
  - data/plan_e/compare_6year_<timestamp>.md  报告
  - data/plan_e/compare_6year_<timestamp>.png  图表

用法:
    venv/bin/python scripts/compare_6year.py
    venv/bin/python scripts/compare_6year.py --quick  # 减少 limit-codes=1000
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent.parent
MODELS_DIR = ROOT / "data" / "double_up"
OUT_DIR = ROOT / "data" / "plan_e"

MODELS: list[tuple[str, str, str]] = [
    # (display_name, model_filename, type)
    ("v8 baseline", "d_signal_5d_model.txt", "classifier (18)"),
    ("v9 (26 维)", "d_signal_5d_model_v9.txt", "classifier (26)"),
    ("v10 DART", "d_signal_5d_model_v10.txt", "classifier (26) + 正则+Dropout"),
    ("greedy 20", "d_signal_5d_model_greedy.txt", "classifier (20, dist+amt 增益)"),
    ("PnL proxy", "d_signal_5d_model_vpnl.txt", "regressor (20, PnL 代理)"),
]

START = "2020-01-01"
END = "2025-12-31"
YEARS = list(range(2020, 2026))


def run_sim_live(model_path: Path, limit_codes: int) -> tuple[Path, float]:
    """运行 sim_live,返回 .md 输出路径与耗时(秒)。"""
    cmd = [
        sys.executable,
        str(ROOT / "scripts" / "plan_e_sim_live.py"),
        "--model-file", str(model_path),
        "--samples", "quarterly",
        "--start", START,
        "--end", END,
        "--limit-codes", str(limit_codes),
        "--no-freshness-check",
    ]
    t0 = time.time()
    proc = subprocess.run(cmd, cwd=str(ROOT / "scripts"), capture_output=True, text=True)
    dt = time.time() - t0
    if proc.returncode != 0:
        print(f"  ❌ sim_live failed:", proc.stderr[-500:])
        return None, dt
    # 输出文件名形如 plan_e_sim_live_YYYYMMDD_HHMMSS.md
    md_files = sorted(OUT_DIR.glob("plan_e_sim_live_*.md"), key=lambda p: p.stat().st_mtime)
    return md_files[-1] if md_files else None, dt


def parse_sim_live(md_path: Path) -> dict:
    """解析 sim_live 的 markdown,返回 {year: {trades, win_rate, avg_pnl}} + 总指标。"""
    text = md_path.read_text(encoding="utf-8")
    out: dict = {"per_year": {}, "overall": {}}

    # 总体
    m_total = re.search(r"\|\s*总样本\s*\|\s*(\d+)\s*\|", text)
    m_win = re.search(r"\|\s*胜率\s*\|\s*([\d.]+)%\s*\|", text)
    m_pnl = re.search(r"\|\s*平均收益\s*\|\s*([-\d.]+)%\s*\|", text)
    m_cum = re.search(r"\|\s*累计收益\s*\|\s*([-\d.]+)%\s*\|", text)
    if m_total:
        out["overall"]["trades"] = int(m_total.group(1))
    if m_win:
        out["overall"]["win_rate"] = float(m_win.group(1))
    if m_pnl:
        out["overall"]["avg_pnl"] = float(m_pnl.group(1))
    if m_cum:
        out["overall"]["cum_pnl"] = float(m_cum.group(1))

    # 分年度
    m_block = re.search(r"### 分年度\s*\n\s*\|[^|]*\|[^|]*\|[^|]*\|[^|]*\|[^|]*\s*\|(.+?)(?=---|\\Z)", text, re.DOTALL)
    if m_block:
        for line in m_block.group(1).strip().split("\n"):
            parts = [p.strip() for p in line.split("|") if p.strip()]
            if len(parts) >= 4 and parts[0].isdigit():
                year = int(parts[0])
                out["per_year"][year] = {
                    "trades": int(parts[1]),
                    "win_rate": float(parts[2].rstrip("%")),
                    "avg_pnl": float(parts[3].rstrip("%")),
                }
    return out


def make_table(results: dict) -> str:
    """生成 markdown 汇总表(5 模型 × 6 年)。"""
    lines = []
    lines.append("# 5 模型 × 6 年完整窗口对比报告\n")
    lines.append(f"> 生成: {datetime.now().isoformat(timespec='seconds')}\n")
    lines.append(f"> 数据范围: {START} ~ {END}\n\n")

    # 1. 分年度胜率矩阵
    lines.append("## 1. 分年度胜率(%)矩阵\n\n")
    header = "| 模型 | 类型 |"
    sep = "|---|---|"
    for y in YEARS:
        header += f" {y} |"
        sep += "---:|"
    lines.append(header)
    lines.append(sep)
    for name, _fp, type_str in MODELS:
        if name not in results:
            continue
        row = f"| **{name}** | {type_str} |"
        for y in YEARS:
            r = results[name]["per_year"].get(y)
            row += f" {r['win_rate']:.1f} |" if r else " — |"
        lines.append(row)
    lines.append("")

    # 2. 分年度平均收益矩阵
    lines.append("## 2. 分年度平均收益(%)矩阵\n\n")
    header = "| 模型 | 类型 |"
    sep = "|---|---|"
    for y in YEARS:
        header += f" {y} |"
        sep += "---:|"
    lines.append(header)
    lines.append(sep)
    for name, _fp, type_str in MODELS:
        if name not in results:
            continue
        row = f"| **{name}** | {type_str} |"
        for y in YEARS:
            r = results[name]["per_year"].get(y)
            row += f" {r['avg_pnl']:+.1f} |" if r else " — |"
        lines.append(row)
    lines.append("")

    # 3. 6 年聚合指标
    lines.append("## 3. 6 年聚合指标\n\n")
    lines.append("| 模型 | 总笔数 | 平均胜率 | 胜率σ | 平均收益 | 累计收益 | 收益σ | 最大回撤(估) |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|")
    for name, _fp, type_str in MODELS:
        if name not in results:
            continue
        yrs = [results[name]["per_year"].get(y, {}) for y in YEARS]
        yrs = [r for r in yrs if r]
        if not yrs:
            continue
        win_rates = [r["win_rate"] for r in yrs]
        avg_pnls = [r["avg_pnl"] for r in yrs]
        total_trades = sum(r["trades"] for r in yrs)
        # 估算最大回撤:累计收益序列最低点
        cum_pnl = 0.0
        max_dd = 0.0
        peak = 0.0
        for r in yrs:
            cum_pnl += r["avg_pnl"] * r["trades"] / 100  # 折算比例
            if cum_pnl > peak:
                peak = cum_pnl
            dd = peak - cum_pnl
            if dd > max_dd:
                max_dd = dd
        lines.append(
            f"| **{name}** | {total_trades} | "
            f"{sum(win_rates)/len(win_rates):.1f}% | "
            f"{(sum((w-sum(win_rates)/len(win_rates))**2 for w in win_rates)/len(win_rates))**0.5:.1f} | "
            f"{sum(avg_pnls)/len(avg_pnls):+.2f}% | "
            f"{results[name]['overall'].get('cum_pnl', sum(avg_pnls)/len(avg_pnls)*total_trades/100):+.1f}% | "
            f"{(sum((p-sum(avg_pnls)/len(avg_pnls))**2 for p in avg_pnls)/len(avg_pnls))**0.5:.2f} | "
            f"{max_dd*100:+.1f}% |"
        )
    lines.append("")

    # 4. 排序
    lines.append("## 4. 排名(按 6 年平均胜率)\n\n")
    ranked = sorted(
        [n for n, _, _ in MODELS if n in results],
        key=lambda n: -results[n]["per_year"][YEARS[-1]]["win_rate"] if results[n]["per_year"] else 0,
    )
    for rank, n in enumerate(ranked, 1):
        if results[n]["per_year"]:
            last_win = results[n]["per_year"][YEARS[-1]]["win_rate"]
            last_pnl = results[n]["per_year"][YEARS[-1]]["avg_pnl"]
            lines.append(f"{rank}. {n} — 2025 胜率 {last_win:.1f}%, 收益 {last_pnl:+.1f}%")
    lines.append("")

    # 5. 解读
    lines.append("## 5. 解读\n\n")
    lines.append("- **6 年完整窗口**比单年(2024)更稳健,反映模型真实能力")
    lines.append("- **胜率 σ 越小** → 模型越稳定(年份间波动小)")
    lines.append("- **平均收益** 直接对应真实交易期望,远比 AUC 直观")
    lines.append("- **PnL proxy(regressor)** 与其他(classifier)模型走完全不同的训练目标,Top N 选股语义不同")
    lines.append("- 2025 年(最近一年)最能反映当前模型实际表现")
    lines.append("")
    return "\n".join(lines)


def make_chart(results: dict, out_png: Path):
    """生成 2 张子图:胜率折线、平均收益折线,5 模型 × 6 年。"""
    import matplotlib.pyplot as plt

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 5))
    colors = ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd"]
    for i, (name, _fp, _type) in enumerate(MODELS):
        if name not in results:
            continue
        yrs = [y for y in YEARS if y in results[name]["per_year"]]
        wins = [results[name]["per_year"][y]["win_rate"] for y in yrs]
        pnls = [results[name]["per_year"][y]["avg_pnl"] for y in yrs]
        ax1.plot(yrs, wins, "o-", label=name, color=colors[i], linewidth=1.5, markersize=5)
        ax2.plot(yrs, pnls, "o-", label=name, color=colors[i], linewidth=1.5, markersize=5)

    ax1.set_xlabel("Year")
    ax1.set_ylabel("Win Rate (%)")
    ax1.set_title("5 Models · 6-Year Win Rate (sim_live quarterly)")
    ax1.legend(loc="best", fontsize=9)
    ax1.grid(True, alpha=0.3)
    ax1.set_xticks(YEARS)
    ax1.axhline(30, color="gray", linestyle="--", alpha=0.5, label="_n=30? 阈值")

    ax2.set_xlabel("Year")
    ax2.set_ylabel("Avg PnL (%)")
    ax2.set_title("5 Models · 6-Year Avg PnL (sim_live quarterly)")
    ax2.legend(loc="best", fontsize=9)
    ax2.grid(True, alpha=0.3)
    ax2.set_xticks(YEARS)
    ax2.axhline(0, color="gray", linestyle="--", alpha=0.5)

    plt.tight_layout()
    plt.savefig(out_png, dpi=120)
    plt.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--quick", action="store_true", help="减少 limit-codes=1000 (默认 2000)")
    args = parser.parse_args()
    limit = 1000 if args.quick else 2000

    print("=== 5 模型 × 6 年完整窗口对比 ===\n")
    print(f"窗口: {START} ~ {END}")
    print(f"模型: {len(MODELS)} 个,股票子集: {limit} 只\n")

    results: dict = {}
    timing: dict = {}
    for name, fname, type_str in MODELS:
        fp = MODELS_DIR / fname
        if not fp.exists():
            print(f"⚠ 跳过 {name}: {fp} 不存在")
            continue
        print(f"[{name}] ({type_str})")
        print(f"  模型: {fp.name}")
        t0 = time.time()
        md, dt = run_sim_live(fp, limit)
        timing[name] = dt
        if md is None:
            print(f"  ❌ 失败\n")
            continue
        print(f"  sim_live 耗时: {dt:.0f}s")
        data = parse_sim_live(md)
        results[name] = data
        if "overall" in data and data["overall"]:
            o = data["overall"]
            print(f"  6 年: {o.get('trades', '?')} 笔, 胜率 {o.get('win_rate', 0):.1f}%, "
                  f"平均收益 {o.get('avg_pnl', 0):+.2f}%, 累计 {o.get('cum_pnl', 0):+.1f}%")
        print(f"  分年度数据: {len(data['per_year'])}/{len(YEARS)} 年")
        print()

    if not results:
        sys.exit("❌ 所有模型 sim_live 均失败")

    # 保存
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = OUT_DIR / f"compare_6year_{timestamp}.csv"
    md_path = OUT_DIR / f"compare_6year_{timestamp}.md"
    png_path = OUT_DIR / f"compare_6year_{timestamp}.png"

    # CSV
    with csv_path.open("w", encoding="utf-8") as f:
        f.write("model,year,trades,win_rate,avg_pnl\n")
        for n in [n for n, _, _ in MODELS if n in results]:
            for y in YEARS:
                r = results[n]["per_year"].get(y)
                if r:
                    f.write(f"{n},{y},{r['trades']},{r['win_rate']},{r['avg_pnl']}\n")
    print(f"CSV: {csv_path}")

    # MD
    table_md = make_table(results)
    md_path.write_text(table_md, encoding="utf-8")
    print(f"MD : {md_path}")

    # PNG
    make_chart(results, png_path)
    print(f"PNG: {png_path}")

    print("\n=== 完成 ===")
    print(f"耗时统计:")
    for n, t in timing.items():
        print(f"  {n:20s} {t:.0f}s")


if __name__ == "__main__":
    main()
