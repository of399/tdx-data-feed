#!/home/jiuben/tdx-data-feed/venv/bin/python3
"""plot_loss.py — 从 trainer_log.jsonl 画 loss 曲线 PNG

用法:
  plot_loss.py                          # 默认: $OUT_BASE/qwen3-14b-tdx
  plot_loss.py <OUT_DIR>                # 指定训练目录
  plot_loss.py --all                    # 给所有 v* 训练各画一张

输出:
  - <OUT_DIR>/loss_curve.png
  - <OUT_DIR>/loss_curve_data.csv
"""
import json
import sys

import matplotlib

matplotlib.use("Agg")
from pathlib import Path

import matplotlib.pyplot as plt

BASE = Path("/home/jiuben/tdx-data-feed")
OUT_BASE = BASE / "train" / "output"


def load_jsonl(jsonl: Path):
    if not jsonl.exists():
        return None
    rows = []
    skipped = 0
    with open(jsonl) as f:
        for l in f:
            l = l.strip()
            if not l:
                continue
            try:
                r = json.loads(l)
            except Exception:
                skipped += 1
                continue
            if 'loss' not in r or ('current_steps' not in r and 'step' not in r):
                skipped += 1
                continue
            rows.append(r)
    if skipped:
        print(f"  (跳过 {skipped} 行非 metric)")
    return rows


def normalize(rows):
    """统一 current_steps 字段"""
    out = []
    for r in rows:
        step = r.get('current_steps', r.get('step'))
        out.append({
            'step': step,
            'loss': r['loss'],
            'epoch': r.get('epoch', 0.0),
            'lr': r.get('lr', 0.0),
        })
    return out


def plot_one(out_dir: Path):
    jsonl = out_dir / "trainer_log.jsonl"
    out_png = out_dir / "loss_curve.png"
    out_csv = out_dir / "loss_curve_data.csv"

    rows = load_jsonl(jsonl)
    if not rows:
        print(f"✗ {jsonl} 无数据")
        return False
    rows = normalize(rows)

    steps = [r["step"] for r in rows]
    losses = [r["loss"] for r in rows]
    epochs = [r["epoch"] for r in rows]
    lrs = [r["lr"] for r in rows]

    # 写 CSV
    with open(out_csv, "w") as f:
        f.write("step,epoch,loss,lr\n")
        for s, e, l, lr in zip(steps, epochs, losses, lrs, strict=False):
            f.write(f"{s},{e:.4f},{l:.6f},{lr:.4e}\n")

    # 画 PNG
    fig, ax1 = plt.subplots(figsize=(12, 6))
    ax1.plot(steps, losses, color="#58a6ff", linewidth=1.2, label="loss")
    ax1.set_xlabel("step")
    ax1.set_ylabel("loss", color="#58a6ff")
    ax1.tick_params(axis="y", labelcolor="#58a6ff")
    ax1.grid(True, alpha=0.3)

    ax2 = ax1.twinx()
    ax2.plot(steps, lrs, color="#d29922", linewidth=0.8, alpha=0.6, label="lr")
    ax2.set_ylabel("lr", color="#d29922")
    ax2.tick_params(axis="y", labelcolor="#d29922")

    title = out_dir.name
    ax1.set_title(f"{title} — loss / lr curve ({len(rows)} steps)")
    fig.tight_layout()
    fig.savefig(out_png, dpi=100, bbox_inches="tight")
    plt.close(fig)
    print(f"✓ {out_png.name} ({len(rows)} steps, last loss={losses[-1]:.4f})")
    return True


def main():
    args = sys.argv[1:]

    if "--all" in args:
        targets = sorted([t for t in OUT_BASE.glob("qwen3-14b-tdx-v*") if not (t / ".no_monitor").exists()])
        if not targets:
            print(f"✗ {OUT_BASE} 下未发现 qwen3-14b-tdx-v* 目录")
            sys.exit(1)
        print(f"扫描 {len(targets)} 个训练: {[t.name for t in targets]}")
        ok = 0
        for t in targets:
            if plot_one(t):
                ok += 1
        print(f"\n汇总: {ok}/{len(targets)} 成功")
        sys.exit(0 if ok > 0 else 1)

    # 单个目录
    target = Path(args[0]) if args else OUT_BASE / "qwen3-14b-tdx"

    if not target.exists():
        print(f"✗ {target} 不存在")
        sys.exit(1)
    sys.exit(0 if plot_one(target) else 1)


if __name__ == "__main__":
    main()
