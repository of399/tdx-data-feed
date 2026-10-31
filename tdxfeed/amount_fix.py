"""成交额缺口修复：Sina 日 K 无 amount 时用 close × volume(手) × 100 估算。

估算值会落进 amount 列，和真实成交额长得一样。为了让下游能区分，估算过的
(symbol, date) 另记一份旁路清单（不改 daily 表 schema）：
    data/.state/amount_estimated.json  {"sh600000": [20260105, ...]}
清单每次写盘整段替换该 symbol，真实 amount 回来后条目自动消失。

清单是进程内缓冲、退出时落盘（`flush_estimated` / atexit）：全市场同步一次
要记 5000 只，逐只重写整个 JSON 是 O(n²)。缓冲期间 `estimated_dates` 也能读到；
进程被 kill -9 时丢失的条目会在下次写入时按合并结果重算，属可自愈的标注数据。
"""
from __future__ import annotations

import atexit
import json
import os
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd

_LEDGER_LOCK = threading.RLock()
_LEDGER_CACHE: dict[Path, dict[str, list[int]]] = {}
_LEDGER_TOUCHED: dict[Path, set[str]] = {}
_LEDGER_ATEXIT_ARMED = False


def fill_missing_amount(df: pd.DataFrame,
                        start_date: int | str | None = None) -> tuple[pd.DataFrame, int]:
    """amount<=0 且 volume>0 时用 close × volume(手) × 100 估算。

    只补缺口，不覆盖已有的真实成交额；volume=0（停牌）保持 0。
    start_date 给定时仅补该日及之后的行（YYYYMMDD）。
    返回 (新 DataFrame, 填补行数)。
    """
    out, n, _ = fill_missing_amount_marked(df, start_date=start_date)
    return out, n


def fill_missing_amount_marked(df: pd.DataFrame,
                               start_date: int | str | None = None
                               ) -> tuple[pd.DataFrame, int, pd.Series]:
    """同 fill_missing_amount，额外返回与返回 DataFrame 索引对齐的估算 mask。"""
    out = df.copy()
    if not {"amount", "volume", "close"}.issubset(out.columns):
        return out, 0, pd.Series(False, index=out.index, dtype=bool)
    amount = pd.to_numeric(out["amount"], errors="coerce").fillna(0.0)
    volume = pd.to_numeric(out["volume"], errors="coerce").fillna(0.0)
    close = pd.to_numeric(out["close"], errors="coerce").fillna(0.0)
    mask = (amount <= 0) & (volume > 0) & (close > 0)
    if start_date is not None:
        if pd.api.types.is_integer_dtype(out["date"]):
            dates = pd.to_datetime(out["date"].astype(str), format="%Y%m%d",
                                   errors="coerce")
        else:
            dates = pd.to_datetime(out["date"], errors="coerce")
        cutoff = pd.to_datetime(str(start_date), format="%Y%m%d", errors="coerce")
        mask &= dates >= cutoff
    n = int(mask.sum())
    if n:
        out.loc[mask, "amount"] = close[mask] * volume[mask] * 100.0
    return out, n, mask


def ledger_path_for(root: str | Path) -> Path:
    """某个数据根下的估算清单位置（writers 用 out_root，脚本用 DATA_ROOT）。"""
    return Path(root) / ".state" / "amount_estimated.json"


def _resolve_path(path: str | Path | None) -> Path:
    if path is not None:
        return Path(path)
    from tdxfeed.paths import amount_ledger_path

    return amount_ledger_path()


def _read_ledger_file(p: Path) -> dict[str, list[int]]:
    """读盘上的清单；坏文件当空清单（清单只用于标注，不参与控制流）。"""
    if not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, list[int]] = {}
    for k, v in raw.items():
        if isinstance(v, list):
            try:
                out[str(k)] = sorted({int(x) for x in v})
            except (TypeError, ValueError):
                continue
    return out


def _ledger_view(p: Path) -> dict[str, list[int]]:
    """进程内视图（含尚未落盘的条目）。调用方须持有 _LEDGER_LOCK。"""
    if p not in _LEDGER_CACHE:
        _LEDGER_CACHE[p] = _read_ledger_file(p)
    return _LEDGER_CACHE[p]


def load_estimated(path: str | Path | None = None) -> dict[str, list[int]]:
    p = _resolve_path(path)
    with _LEDGER_LOCK:
        return {k: list(v) for k, v in _ledger_view(p).items()}


def record_estimated(symbol: str, dates, path: str | Path | None = None) -> None:
    """整段替换该 symbol 的估算日期（写后快照，自愈）；空集合即删除条目。

    只改进程内缓冲，落盘走 flush_estimated（或进程退出时的 atexit）。
    """
    global _LEDGER_ATEXIT_ARMED

    p = _resolve_path(path)
    days = sorted({int(d) for d in dates})
    with _LEDGER_LOCK:
        data = _ledger_view(p)
        if days:
            data[str(symbol)] = days
        else:
            data.pop(str(symbol), None)
        _LEDGER_TOUCHED.setdefault(p, set()).add(str(symbol))
        if not _LEDGER_ATEXIT_ARMED:
            atexit.register(_flush_at_exit)
            _LEDGER_ATEXIT_ARMED = True


def _write_ledger_file(p: Path, data: dict[str, list[int]]) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f"{p.name}.{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, sort_keys=True, indent=2)
    os.replace(tmp, p)


def flush_estimated(path: str | Path | None = None) -> None:
    """把缓冲写盘。写前重读磁盘，只覆盖本进程动过的 symbol，不吞并别进程的条目。"""
    with _LEDGER_LOCK:
        targets = [_resolve_path(path)] if path is not None else list(_LEDGER_TOUCHED)
        for p in targets:
            touched = _LEDGER_TOUCHED.get(p)
            if not touched:
                continue
            disk = _read_ledger_file(p)
            cached = _ledger_view(p)
            for sym in touched:
                if sym in cached:
                    disk[sym] = list(cached[sym])
                else:
                    disk.pop(sym, None)
            _write_ledger_file(p, disk)
            _LEDGER_CACHE[p] = disk
            _LEDGER_TOUCHED[p] = set()


def _flush_at_exit() -> None:
    for p in list(_LEDGER_TOUCHED):
        # 数据根还在就补建 .state；根已被删（测试的 tmp 目录）就别把它造回来
        if not p.parent.parent.is_dir():
            continue
        try:
            flush_estimated(p)
        except Exception:
            pass


def estimated_dates(symbol: str, path: str | Path | None = None) -> set[int]:
    """该 symbol 当前 mark 为估算的交易日集合（YYYYMMDD int，含未落盘条目）。"""
    p = _resolve_path(path)
    with _LEDGER_LOCK:
        return set(_ledger_view(p).get(str(symbol), []))


def merge_estimated(symbol: str, added=(), removed=(),
                    path: str | Path | None = None) -> None:
    """在既有条目上增删：合并写只把「本次补的」记进来，只有拿到真实成交额的
    日期才撤销标记——估算值本身留在表里，不重算就没有别的线索。"""
    p = _resolve_path(path)
    with _LEDGER_LOCK:
        cur = set(_ledger_view(p).get(str(symbol), []))
    record_estimated(symbol, (cur | {int(d) for d in added}) - {int(d) for d in removed}, p)


def _backfill_one(path: Path, dry_run: bool, update_day: bool,
                  out_root: Path | None,
                  start_date: int | None,
                  backup_dir: Path | None = None) -> tuple[int, str | None]:
    try:
        df = pd.read_parquet(path)
    except Exception as e:
        return 0, f"{path.name}: {e!r}"
    filled, n, est = fill_missing_amount_marked(df, start_date=start_date)
    if n == 0:
        return 0, None
    if dry_run:
        return n, None

    from tdxfeed.writers import Writers  # 延迟导入，避免 writers→amount_fix 循环

    if backup_dir is not None:
        # 写前备份：原文件不可逆覆盖，先留一份可回滚副本
        backup_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, backup_dir / f"{path.stem}.amountfix.bak.parquet")

    # 不以 .parquet 结尾：中断残留不能被 daily 目录的 *.parquet 列举当成股票
    tmp = path.with_suffix(".amountfix.tmp")
    Writers._write_parquet_date32(str(tmp), filled)
    os.replace(tmp, path)
    if update_day and out_root is not None:
        Writers(str(out_root)).write_day_safe(path.stem, filled.values.tolist())
    merge_estimated(path.stem,
                    {Writers._date_int(d) for d in filled.loc[est, "date"]},
                    path=ledger_path_for(out_root) if out_root else None)
    return n, None


def backfill_daily_dir(daily_dir: str | Path, *, dry_run: bool = False,
                       update_day: bool = True, workers: int = 8,
                       out_root: str | Path | None = None,
                       start_date: int | None = None,
                       backup_dir: str | Path | None = None) -> dict:
    """批量修复 daily parquet 的 amount 缺口，可选重建同名 .day 文件。

    backup_dir 给定时，落盘前把每个被修改的原文件复制到该目录（可回滚）。
    """
    daily = Path(daily_dir)
    if out_root is None:
        out_root = daily.parent.parent
    files = [p for p in sorted(daily.glob("*.parquet")) if ".bak" not in p.stem]
    filled_rows = 0
    updated = 0
    errors: list[str] = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = [pool.submit(_backfill_one, p, dry_run, update_day,
                               Path(out_root), start_date,
                               Path(backup_dir) if backup_dir else None)
                   for p in files]
        for fut in futures:
            n, err = fut.result()
            if n:
                updated += 1
                filled_rows += n
            if err:
                errors.append(err)
    return {
        "stocks": len(files),
        "stocks_updated": updated,
        "filled_rows": filled_rows,
        "dry_run": dry_run,
        "backup_dir": str(backup_dir) if backup_dir else None,
        "errors": errors[:20],
        "n_errors": len(errors),
    }
