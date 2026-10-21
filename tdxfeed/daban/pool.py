"""候选池加载：量化初筛（predict.py）与人工板块池（计划书 §D.5）。

三类池：

- ``predict``：``predict.py --emit-top`` 产出的 TopN（``data/predictions/predict_top*.csv``
  或 ``data/signals/predict_*_top50_thr0.7.csv``）→ 量化初筛层
- ``file``：任意 CSV，第一列为 6 位代码（可含 sh/sz/bj 前缀）
- ``block``：通达信板块导出的 ``.blk``（每行一个代码）

把池子接进扫描器 = "量化初筛 + 盘口确认"闭环：
先由 predict 缩小到 TopN，再用扫描器的封单/板块/状态因子做确认。
"""
from __future__ import annotations

import csv
import re
from pathlib import Path

__all__ = ["load_pool", "resolve_pool_path"]

_ROOT = Path(__file__).resolve().parents[2]
PRED_DIR = _ROOT / "data" / "predictions"
SIGNAL_DIR = _ROOT / "data" / "signals"

_CODE_RE = re.compile(r"^(?:sh|sz|bj)?(\d{6})$")


def _norm(code: str) -> str:
    m = _CODE_RE.match(str(code).strip().lower())
    return m.group(1) if m else ""


def _sort_key(path: Path) -> tuple[str, float]:
    """按文件名内嵌的时间戳排序（predict_top5_20261020_094301.csv → 20261020094301）。"""
    stem = path.stem
    digits = ""
    for part in reversed(stem.split("_")):
        if (part.isdigit() and len(part) in (6, 8, 14)) or (part.isdigit() and len(part) == 8):
            digits = part + digits
    if not digits:
        return (stem, path.stat().st_mtime)
    return (digits, path.stat().st_mtime)


def resolve_pool_path(spec: str) -> Path | None:
    """解析 predict 池的默认文件（取最新一个）。

    优先级：``data/signals/predict_*_top*.csv`` → ``data/predictions/predict_top*.csv``
    → ``data/predictions/predict_*.csv``

    ⚠ 不能用字典序排序：``predict_top5_20261020`` 与 ``predict_top6_20261010``
    按字典序会选错（'6' > '5'），必须按文件名内嵌时间戳。
    """
    if spec and spec not in ("predict", "auto"):
        p = Path(spec)
        return p if p.exists() else None
    for pattern in ("predict_*_top*.csv", "predict_top*.csv", "predict_*.csv"):
        files = sorted(SIGNAL_DIR.glob(pattern), key=_sort_key) or sorted(
            PRED_DIR.glob(pattern), key=_sort_key
        )
        if files:
            return files[-1]
    return None


def load_pool(spec: str, top: int = 0) -> list[str]:
    """加载候选池代码列表。

    参数:
        spec: ``predict`` / ``predict:<path>`` / ``file:<path>`` / ``block:<path>`` / 直接给路径
        top:  只取前 N 个（0 = 全部）
    """
    spec = (spec or "").strip()
    if not spec:
        return []
    codes: list[str] = []

    if spec.startswith("file:"):
        path = Path(spec[5:])
        if not path.exists():
            return []
        with path.open(encoding="utf-8-sig") as fh:
            for row in csv.reader(fh):
                if not row:
                    continue
                c = _norm(row[0])
                if c:
                    codes.append(c)
    elif spec.startswith("block:"):
        path = Path(spec[6:])
        if not path.exists():
            return []
        for line in path.read_text(encoding="utf-8").splitlines():
            c = _norm(line)
            if c:
                codes.append(c)
    else:
        path = resolve_pool_path(spec)
        if path is None:
            return []
        with path.open(encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh)
            fieldnames = [f.strip() for f in (reader.fieldnames or [])]
            code_col = next(
                (f for f in fieldnames if f.lower() in ("code", "代码", "symbol")), None
            )
            if code_col:
                for row in reader:
                    c = _norm(row.get(code_col, ""))
                    if c:
                        codes.append(c)
            else:  # 无表头：取第一列
                fh.seek(0)
                for row in csv.reader(fh):
                    if row:
                        c = _norm(row[0])
                        if c:
                            codes.append(c)

    # 去重保序
    seen: set[str] = set()
    out: list[str] = []
    for c in codes:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out[:top] if top > 0 else out
