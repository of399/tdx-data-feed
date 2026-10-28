"""全市场日线新鲜度校验：确认每只标的末日期均不早于阈值。

用法:
  venv/bin/python scripts/verify_daily_freshness.py
    [--threshold 20260912] [--root /home/jiuben/tdx-data-feed/data]

输出: 控制台摘要 + logs/verify_daily_since_YYYYMMDD.md 完整报告。
按代码前缀(tdxfeed.symbols.group)分 A股/ETF/债券/北交所/其他 统计达标与不达标。
"""
import argparse
import datetime as _dt
import glob
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tdxfeed.paths import DATA_ROOT, daily_dir  # noqa: E402

try:
    from tdxfeed.symbols import INDICES, group
except Exception:  # pragma: no cover
    def group(code: str) -> str:
        return "other"
    INDICES = {}


def to_int(d) -> int:
    """任意日期表示 → int YYYYMMDD（统一比较键）。"""
    if isinstance(d, (_dt.datetime, _dt.date, pd.Timestamp)):
        return int(d.strftime("%Y%m%d"))
    s = str(d).replace("-", "").replace(" ", "").replace(":", "")
    try:
        return int(s[:8])
    except Exception:
        return 0


def source_failure_block(root: str) -> list[str]:
    """读取 logs/sync_summary.json，生成“源失败率（同步质量）”指标段落。

    源失败率 = (个股失败 + 指数失败) / 全部同步尝试，目标 = 0%（同步零失败）。
    仅在 full_sync 已运行并写出 sync_summary.json 后才有数据。
    """
    import json

    path = os.path.join(root, "..", "logs", "sync_summary.json")
    if not os.path.exists(path):
        return [
            "## 源失败率（同步质量）",
            "",
            "> ⚠️ 未发现 `logs/sync_summary.json`。请先运行 "
            "`venv/bin/python -m tdxfeed.full_sync` 生成同步质量汇总后再评估源失败率。",
            "",
        ]
    try:
        with open(path, encoding="utf-8") as fh:
            s = json.load(fh)
    except Exception as e:
        return ["## 源失败率（同步质量）", "", f"> ⚠️ 读取汇总失败: {e!s}", ""]

    rate = s.get("source_failure_rate_pct", 0.0)
    zero = s.get("zero_failure", False)
    ind = s.get("individual", {})
    idx = s.get("index", {})
    stamp = s.get("timestamp", "?")
    flag = "✅ 零失败" if zero else f"⚠️ 失败率 {rate}%"
    return [
        "## 源失败率（同步质量）",
        "",
        f"- 汇总时间戳: {stamp}",
        f"- **源失败率: {rate}%** ｜ {flag}",
        f"- 个股通道: 成功 {ind.get('ok', 0)} ｜ 跳过 {ind.get('skip', 0)} ｜ 失败 {ind.get('fail', 0)}",
        f"- 指数通道: 成功 {idx.get('ok', 0)}（mootdx主 {idx.get('mootdx', 0)} / "
        f"Sina兜底 {idx.get('sina', 0)} / 回退 {idx.get('fallback', 0)}）｜ 失败 {idx.get('fail', 0)}",
        "",
        "> 目标：同步零失败（源失败率=0%）。TDX 公共服务器不可达时由 Sina 兜底，指数通道不计失败。",
        "",
    ]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--threshold", type=int, default=20260912)
    ap.add_argument(
        "--root",
        default=str(DATA_ROOT),
    )
    args = ap.parse_args()

    pq_dir = daily_dir(args.root)
    files = sorted(f for f in glob.glob(os.path.join(pq_dir, "*.parquet")) if ".bak" not in f)
    total = len(files)
    buckets: dict[str, dict] = {}
    last_dates: list[int] = []

    for f in files:
        code = os.path.basename(f).replace(".parquet", "")
        try:
            df = pd.read_parquet(f, columns=["date"])
            last = int(df["date"].map(to_int).max()) if not df.empty else 0
        except Exception:
            last = -1
        last_dates.append(last)
        core = code[2:] if code[:2] in ("sh", "sz") else code
        g = "index" if core in INDICES else group(core)
        b = buckets.setdefault(g, {"ok": 0, "stale": 0, "stale_list": []})
        if last >= args.threshold:
            b["ok"] += 1
        else:
            b["stale"] += 1
            b["stale_list"].append((code, last))

    all_ok = sum(b["ok"] for b in buckets.values())
    all_stale = sum(b["stale"] for b in buckets.values())
    overall_min = min(last_dates) if last_dates else 0
    overall_max = max(last_dates) if last_dates else 0

    L: list[str] = []
    L.append(f"# 日线新鲜度校验报告 (末日期 ≥ {args.threshold})")
    L.append("")
    L.append(f"- 校验时间: {pd.Timestamp.now()}")
    L.append(f"- 校验范围: `{pq_dir}`")
    L.append(f"- 标的文件总数: **{total}**")
    L.append(f"- 达标 (末日期 ≥ {args.threshold}): **{all_ok}**")
    L.append(f"- 不达标 (< {args.threshold}): **{all_stale}**")
    L.append(f"- 全市场最早日: {overall_min} ｜ 最新日: {overall_max}")
    L.append("")

    # 【阶段 0 加固 · 2026-10-10】源失败率（同步质量）指标段落
    L.extend(source_failure_block(args.root))

    L.append("## 按类别分布")
    L.append("")
    L.append("| 类别 | 达标 | 不达标 |")
    L.append("|---|---:|---:|")
    for g in sorted(buckets):
        L.append(f"| {g} | {buckets[g]['ok']} | {buckets[g]['stale']} |")
    L.append("")

    if all_stale:
        L.append("## 不达标标的明细")
        L.append("")
        L.append("| 代码 | 末日期 | 类别 |")
        L.append("|---|---:|---|")
        for g in sorted(buckets):
            for code, last in sorted(buckets[g]["stale_list"]):
                L.append(f"| {code} | {last} | {g} |")
        L.append("")
        L.append(f"共 **{all_stale}** 只不达标（末日期早于 {args.threshold}，或文件为空/读取失败）。")
    else:
        L.append("## 结论")
        L.append(
            f"✅ 全市场 **{total}** 只标的末日期均不早于 {args.threshold}，"
            "9-12 之后新数据已全部对齐入库。"
        )
    L.append("")
    report = "\n".join(L)
    print(report)

    out_dir = os.path.join(args.root, "..", "logs")
    os.makedirs(out_dir, exist_ok=True)
    ts = _dt.datetime.now().strftime("%Y%m%d")
    out_path = os.path.join(out_dir, f"verify_daily_since_{ts}.md")
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(report)
    print(f"\n报告已写入: {out_path}")


if __name__ == "__main__":
    main()
