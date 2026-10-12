#!/usr/bin/env python3
"""方案A 实证：用「未来 N 日实际收益符号」重打 v4 三分类标签。

设计：
  - 读取现有 data/v4/{train,val,test}.jsonl（含回望叙述 output），
    从 instruction/input 解析 (code, as_of)，用 parquet 计算 as_of 之后 N 交易日
    的前瞻收益 ret_N = close[t+N]/close[t] - 1。
  - 三分类：ret_N > +θ → 涨；ret_N < -θ → 跌；否则 震荡。
  - 用结构化「方向：涨/震荡/跌」替换 output（与 llm_signal_plan_e 的部署 prompt 对齐）。
  - 泄漏防护：仅保留「as_of + N 交易日」仍在数据范围内的样本（丢弃数据尾部无法标注者）。
  - 原始 train/val/test 的时序切分（按窗口末日排序）保持不变，仅就地丢弃不可标注样本。

用法：
  venv/bin/python scripts/build_v4_forward_labels.py --n 5 --theta 0.02 --out-dir data/v4fwd
  venv/bin/python scripts/build_v4_forward_labels.py --survey   # 仅统计多阈值分布，不写文件
"""
from __future__ import annotations

import argparse
import glob
import json
import re
from pathlib import Path

import pandas as pd

ROOT = Path("/home/jiuben/tdx-data-feed")
PK = ROOT / "data" / "parquet" / "daily"
SRC = ROOT / "data" / "v4"
CODE_RE = re.compile(r"对股票\s+(\d{6})")
DATE_RE = re.compile(r"截至\s*(\d{4}-\d{2}-\d{2})")


def load_parquet(code_clean: str) -> pd.DataFrame | None:
    for prefix in ("sh", "sz", "bj"):
        p = PK / f"{prefix}{code_clean}.parquet"
        if p.exists():
            try:
                df = pd.read_parquet(p)
            except Exception:
                return None
            # schema 漂移：date 为 int64 YYYYMMDD
            if str(df["date"].dtype).startswith("int"):
                df["date"] = pd.to_datetime(df["date"].astype(str), format="%Y%m%d")
            else:
                df["date"] = pd.to_datetime(df["date"])
            return df.sort_values("date").reset_index(drop=True)
    return None


def parse_sample(s: dict) -> tuple[str | None, str | None]:
    """返回 (code6, as_of_str 'YYYY-MM-DD')。"""
    instr = s.get("instruction", "")
    m_code = CODE_RE.search(instr)
    m_date = DATE_RE.search(instr)
    code = m_code.group(1) if m_code else None
    as_of = m_date.group(1) if m_date else None
    return code, as_of


def forward_label(df: pd.DataFrame, as_of: str, n: int, theta: float) -> str | None:
    """计算 as_of 之后 n 交易日的前瞻收益并映射三分类；不可标注返回 None。"""
    target = pd.Timestamp(as_of)
    exact = df[df["date"] == target]
    if exact.empty:
        cand = df[df["date"] <= target].tail(5)
        if cand.empty:
            return None
        idx = cand.index[-1]
    else:
        idx = exact.index[-1]
    if idx + n >= len(df):
        return None  # 前瞻窗口超出数据范围 → 丢弃（防泄漏）
    c0 = float(df.iloc[idx]["close"])
    c1 = float(df.iloc[idx + n]["close"])
    if c0 <= 0 or c1 <= 0:
        return None
    ret = c1 / c0 - 1.0
    if ret > theta:
        return "涨"
    if ret < -theta:
        return "跌"
    return "震荡"


def relabel_file(path: Path, n: int, theta: float) -> tuple[list[dict], dict]:
    out = []
    dist = {"涨": 0, "震荡": 0, "跌": 0, "drop": 0}
    cache: dict[str, pd.DataFrame] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        s = json.loads(line)
        code, as_of = parse_sample(s)
        if code is None or as_of is None:
            dist["drop"] += 1
            continue
        df = cache.get(code)
        if df is None:
            df = load_parquet(code)
            cache[code] = df
        if df is None:
            dist["drop"] += 1
            continue
        lab = forward_label(df, as_of, n, theta)
        if lab is None:
            dist["drop"] += 1
            continue
        s["output"] = f"方向：{lab}"
        out.append(s)
        dist[lab] += 1
    return out, dist


def survey(n: int):
    print(f"# 前瞻标签多阈值分布调查（N={n} 交易日）")
    for theta in (0.01, 0.02, 0.03):
        print(f"\n--- θ = {theta} ---")
        tot = {"涨": 0, "震荡": 0, "跌": 0, "drop": 0}
        for split in ("train", "val", "test"):
            p = SRC / f"{split}.jsonl"
            _, d = relabel_file(p, n, theta)
            for k in tot:
                tot[k] += d[k]
            print(f"  {split}: 涨{d['涨']} 震荡{d['震荡']} 跌{d['跌']} drop{d['drop']}")
        kept = tot["涨"] + tot["震荡"] + tot["跌"]
        print(f"  汇总(kept={kept}): 涨{tot['涨']}({tot['涨']/kept*100:.1f}%) "
              f"震荡{tot['震荡']}({tot['震荡']/kept*100:.1f}%) "
              f"跌{tot['跌']}({tot['跌']/kept*100:.1f}%) drop{tot['drop']}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=5, help="前瞻交易日数（与部署 prompt 对齐=5）")
    ap.add_argument("--theta", type=float, default=0.02, help="震荡阈值（绝对值）")
    ap.add_argument("--out-dir", default="data/v4fwd")
    ap.add_argument("--survey", action="store_true", help="仅统计多阈值分布")
    args = ap.parse_args()

    if args.survey:
        survey(args.n)
        return

    out_dir = ROOT / args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"# 重打标签 N={args.n} θ={args.theta} -> {out_dir}")
    for split in ("train", "val", "test"):
        p = SRC / f"{split}.jsonl"
        out, d = relabel_file(p, args.n, args.theta)
        kept = d["涨"] + d["震荡"] + d["跌"]
        (out_dir / f"{split}.jsonl").write_text(
            "\n".join(json.dumps(x, ensure_ascii=False) for x in out) + "\n",
            encoding="utf-8")
        print(f"  {split}: kept={kept} 涨{d['涨']}({d['涨']/kept*100:.1f}%) "
              f"震荡{d['震荡']}({d['震荡']/kept*100:.1f}%) 跌{d['跌']}({d['跌']/kept*100:.1f}%) "
              f"drop{d['drop']}")
    print("V4FWD-LABELS-DONE ->", out_dir)


if __name__ == "__main__":
    main()
