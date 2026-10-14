"""scripts/top_n_sensitivity.py — Top-N 敏感性分析

目的:验证 sim_live 的 Top-30 + threshold=0.7 是否"压平"了模型间差异。
     在 threshold=0.5(宽松,允许更多股票通过)下,扫描 top-N ∈ {30,50,100,200},
     观察 3 模型(v8 / v9 / PnL proxy)在不同 top-N 下的胜率与收益差异。

输出:
  - data/plan_e/top_n_sensitivity_<ts>.csv  全部 sim_live 解析结果
  - data/plan_e/top_n_sensitivity_<ts>.md    综合报告
  - data/plan_e/top_n_sensitivity_<ts>.png  双图(win rate, avg pnl)

用法:
    venv/bin/python scripts/top_n_sensitivity.py
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
OUT_DIR = ROOT / "data" / "plan_e"
MODELS_DIR = ROOT / "data" / "double_up"

MODELS = [
    ("v8 baseline", "d_signal_5d_model.txt", "P(翻倍) 18 维"),
    ("v9 (26 维)", "d_signal_5d_model_v9.txt", "P(翻倍) 26 维"),
    ("PnL proxy", "d_signal_5d_model_vpnl.txt", "E[PnL] 20 维"),
]

TOP_LEVELS = [30, 50, 100, 200]
THRESHOLD = 0.5  # 宽松阈值,让 top-N 起作用


def run_sim_live(model_path: Path, top: int, threshold: float, start: str, end: str, limit_codes: int) -> tuple[Path, float]:
    cmd = [
        sys.executable,
        str(ROOT / "scripts" / "plan_e_sim_live.py"),
        "--model-file", str(model_path),
        "--samples", "quarterly",
        "--top", str(top),
        "--threshold", str(threshold),
        "--start", start,
        "--end", end,
        "--limit-codes", str(limit_codes),
        "--no-freshness-check",
    ]
    t0 = time.time()
    proc = subprocess.run(cmd, cwd=str(ROOT / "scripts"), capture_output=True, text=True)
    dt = time.time() - t0
    if proc.returncode != 0:
        return None, dt
    md_files = sorted(OUT_DIR.glob("plan_e_sim_live_*.md"), key=lambda p: p.stat().st_mtime)
    return md_files[-1] if md_files else None, dt


def parse_sim_live(md_path: Path) -> dict:
    text = md_path.read_text(encoding="utf-8")
    out = {"per_year": {}, "overall": {}}
    for label, key, cast in [
        (r"\|\s*总样本\s*\|\s*(\d+)\s*\|", "trades", int),
        (r"\|\s*胜率\s*\|\s*([\d.]+)%\s*\|", "win_rate", float),
        (r"\|\s*平均收益\s*\|\s*([+\-\d.]+)%\s*\|", "avg_pnl", float),
        (r"\|\s*累计收益\s*\|\s*([+\-\d.]+)%\s*\|", "cum_pnl", float),
    ]:
        m = re.search(label, text)
        if m:
            out["overall"][key] = cast(m.group(1))
    for line in text.split("\n"):
        m = re.match(r"\|\s*(\d{4})\s*\|\s*(\d+)\s*\|\s*([\d.]+)%\s*\|\s*([+\-\d.]+)%\s*\|", line)
        if m:
            year, trades, win, pnl = int(m.group(1)), int(m.group(2)), float(m.group(3)), float(m.group(4))
            out["per_year"][year] = {"trades": trades, "win_rate": win, "avg_pnl": pnl}
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2023-01-01", help="起始日期(默认 2023-01-01,3 年窗口)")
    parser.add_argument("--end", default="2025-12-31", help="结束日期")
    parser.add_argument("--limit-codes", type=int, default=2000)
    args = parser.parse_args()

    print(f"=== Top-N 敏感性分析 (threshold={THRESHOLD}) ===")
    print(f"窗口: {args.start} ~ {args.end}")
    print(f"3 模型 × {len(TOP_LEVELS)} top-N = {len(MODELS) * len(TOP_LEVELS)} sim_live\n")

    results: dict = {}
    timing: dict = {}
    for name, fname, type_str in MODELS:
        fp = MODELS_DIR / fname
        for top_n in TOP_LEVELS:
            key = f"{name}|top={top_n}"
            print(f"[{name} | top={top_n}]")
            md, dt = run_sim_live(fp, top_n, THRESHOLD, args.start, args.end, args.limit_codes)
            timing[key] = dt
            if md is None:
                print("  ❌ 失败\n")
                continue
            print(f"  耗时 {dt:.0f}s")
            o = parse_sim_live(md)
            results[key] = {
                "model": name, "type": type_str, "top": top_n,
                "trades": o["overall"].get("trades", 0),
                "win_rate": o["overall"].get("win_rate", 0),
                "avg_pnl": o["overall"].get("avg_pnl", 0),
                "cum_pnl": o["overall"].get("cum_pnl", 0),
                "per_year": o["per_year"],
            }
            print(f"  → {results[key]['trades']} 笔, 胜率 {results[key]['win_rate']:.1f}%, "
                  f"收益 {results[key]['avg_pnl']:+.2f}%\n")

    if not results:
        sys.exit("❌ 所有 sim_live 均失败")

    # 保存 + 报告
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = OUT_DIR / f"top_n_sensitivity_{timestamp}.csv"
    md_path = OUT_DIR / f"top_n_sensitivity_{timestamp}.md"
    png_path = OUT_DIR / f"top_n_sensitivity_{timestamp}.png"

    # CSV
    with csv_path.open("w", encoding="utf-8") as f:
        f.write("model,top_n,trades,win_rate,avg_pnl,cum_pnl\n")
        for k, r in results.items():
            f.write(f"{r['model']},{r['top']},{r['trades']},{r['win_rate']},{r['avg_pnl']},{r['cum_pnl']}\n")
    print(f"CSV: {csv_path}")

    # MD 报告
    lines = []
    lines.append("# Top-N 敏感性分析报告\n")
    lines.append(f"> 生成: {datetime.now().isoformat(timespec='seconds')}\n")
    lines.append(f"> threshold = **{THRESHOLD}** (宽松,让 top-N 起作用)\n")
    lines.append(f"> 窗口: {args.start} ~ {args.end}\n\n")

    lines.append("## 1. 各 (模型 × top-N) 实测结果\n\n")
    lines.append("| 模型 | top-N | 笔数 | 胜率 | 平均收益 | 累计收益 |")
    lines.append("|---|---:|---:|---:|---:|---:|")
    for k, r in results.items():
        lines.append(f"| **{r['model']}** | {r['top']} | {r['trades']} | "
                    f"{r['win_rate']:.1f}% | {r['avg_pnl']:+.2f}% | {r['cum_pnl']:+.1f}% |")
    lines.append("")

    # 横向:相同模型, 不同 top-N 的胜率
    lines.append("## 2. 同模型 Top-N 影响(横向)\n\n")
    lines.append("| 模型 | top=30 | top=50 | top=100 | top=200 | 极差 |")
    lines.append("|---|---:|---:|---:|---:|---:|")
    for name, _, _ in MODELS:
        wins = []
        pnls = []
        for tn in TOP_LEVELS:
            r = results.get(f"{name}|top={tn}")
            if r:
                wins.append(r["win_rate"])
                pnls.append(r["avg_pnl"])
        if wins:
            wrange = max(wins) - min(wins)
            prange = max(pnls) - min(pnls)
            lines.append(f"| **{name}** | {wins[0]:.1f}% | {wins[1]:.1f}% | "
                        f"{wins[2]:.1f}% | {wins[3]:.1f}% | 胜率 {wrange:.1f}pp, 收益 {prange:+.2f}pp |")
    lines.append("")

    # 纵向:相同 top-N, 不同模型
    lines.append("## 3. 同 Top-N 模型对比(纵向)\n\n")
    lines.append("| top-N | v8 胜率 | v9 胜率 | PnL 胜率 | 极差 | v8 收益 | v9 收益 | PnL 收益 |")
    lines.append("|---:|---:|---:|---:|---:|---:|---:|---:|")
    for tn in TOP_LEVELS:
        ws = []
        ps = []
        for name, _, _ in MODELS:
            r = results.get(f"{name}|top={tn}")
            if r:
                ws.append(r["win_rate"])
                ps.append(r["avg_pnl"])
        if ws:
            wrange = max(ws) - min(ws)
            lines.append(f"| {tn} | {ws[0]:.1f}% | {ws[1]:.1f}% | {ws[2]:.1f}% | {wrange:.1f}pp | "
                        f"{ps[0]:+.2f}% | {ps[1]:+.2f}% | {ps[2]:+.2f}% |")
    lines.append("")

    # 关键判断
    lines.append("## 4. 关键判断\n\n")
    # 计算每 top-N 下模型间的胜率极差
    model_separation = {}
    for tn in TOP_LEVELS:
        ws = [results.get(f"{n}|top={tn}", {}).get("win_rate", 0) for n, _, _ in MODELS]
        ps = [results.get(f"{n}|top={tn}", {}).get("avg_pnl", 0) for n, _, _ in MODELS]
        model_separation[tn] = (max(ws) - min(ws), max(ps) - min(ps))

    best_tn = max(model_separation, key=lambda k: model_separation[k][0])
    lines.append(f"- **模型区分度最高 top-N = {best_tn}** (胜率极差 {model_separation[best_tn][0]:.1f}pp)")
    lines.append(f"- 默认 top=30 时模型区分度: 胜率 {model_separation[30][0]:.1f}pp, 收益 {model_separation[30][1]:.2f}pp")
    lines.append(f"- **放宽 top-N 是否放大差异**: {('✅ 是' if model_separation[best_tn][0] > model_separation[30][0] + 1 else '❌ 否')}\n")

    # 各 top-N 累计收益
    lines.append("## 5. 累计收益对比(同 top-N 内最佳模型)\n\n")
    for tn in TOP_LEVELS:
        best = max(MODELS, key=lambda m: results.get(f"{m[0]}|top={tn}", {}).get("cum_pnl", -1e9))
        r = results.get(f"{best[0]}|top={tn}")
        if r:
            lines.append(f"- top={tn}: 最佳 **{best[0]}** 累计 {r['cum_pnl']:+.1f}%")
    lines.append("")

    lines.append("## 6. 结论\n\n")
    # 看哪个 top-N 最优(综合 win_rate + avg_pnl)
    lines.append("- 若 top-N 影响显著,说明模型间差异在宽松阈值下放大,真实选股更稳健")
    lines.append("- 若 top-N 无显著影响,说明模型已稳定,Top-30 已能代表最佳信号")
    lines.append("- 选股 trade-off: 大 top-N → 多笔交易 → 分散风险但可能混入噪声股\n")
    lines.append("")

    md_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"MD : {md_path}")

    # PNG
    import matplotlib.pyplot as plt
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 5))
    colors = ["#1f77b4", "#ff7f0e", "#2ca02c"]
    for i, (name, _, _) in enumerate(MODELS):
        xs = []
        ws = []
        ps = []
        for tn in TOP_LEVELS:
            r = results.get(f"{name}|top={tn}")
            if r:
                xs.append(tn)
                ws.append(r["win_rate"])
                ps.append(r["avg_pnl"])
        ax1.plot(xs, ws, "o-", color=colors[i], label=name, linewidth=1.5, markersize=7)
        ax2.plot(xs, ps, "o-", color=colors[i], label=name, linewidth=1.5, markersize=7)

    ax1.set_xscale("log")
    ax1.set_xlabel("Top-N (log)")
    ax1.set_ylabel("Win Rate (%)")
    ax1.set_title(f"Top-N 敏感性 · 胜率 (threshold={THRESHOLD})")
    ax1.legend(loc="best")
    ax1.grid(True, alpha=0.3)
    ax1.set_xticks(TOP_LEVELS)
    ax1.set_xticklabels([str(t) for t in TOP_LEVELS])
    ax1.axhline(30, color="gray", linestyle="--", alpha=0.4)

    ax2.set_xscale("log")
    ax2.set_xlabel("Top-N (log)")
    ax2.set_ylabel("Avg PnL (%)")
    ax2.set_title(f"Top-N 敏感性 · 平均收益 (threshold={THRESHOLD})")
    ax2.legend(loc="best")
    ax2.grid(True, alpha=0.3)
    ax2.set_xticks(TOP_LEVELS)
    ax2.set_xticklabels([str(t) for t in TOP_LEVELS])
    ax2.axhline(0, color="gray", linestyle="--", alpha=0.4)

    plt.tight_layout()
    plt.savefig(png_path, dpi=120)
    plt.close()
    print(f"PNG: {png_path}")

    print(f"\n=== 完成 === 总耗时 {(sum(timing.values())):.0f}s")


if __name__ == "__main__":
    main()
