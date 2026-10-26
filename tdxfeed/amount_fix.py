"""成交额缺口修复：Sina 日 K 无 amount 时用 close × volume(手) × 100 估算。"""
from __future__ import annotations

import os
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd


def fill_missing_amount(df: pd.DataFrame,
                        start_date: int | str | None = None) -> tuple[pd.DataFrame, int]:
    """amount<=0 且 volume>0 时用 close × volume(手) × 100 估算。

    只补缺口，不覆盖已有的真实成交额；volume=0（停牌）保持 0。
    start_date 给定时仅补该日及之后的行（YYYYMMDD）。
    返回 (新 DataFrame, 填补行数)。
    """
    out = df.copy()
    if not {"amount", "volume", "close"}.issubset(out.columns):
        return out, 0
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
    return out, n


def _backfill_one(path: Path, dry_run: bool, update_day: bool,
                  out_root: Path | None,
                  start_date: int | None,
                  backup_dir: Path | None = None) -> tuple[int, str | None]:
    try:
        df = pd.read_parquet(path)
    except Exception as e:
        return 0, f"{path.name}: {e!r}"
    filled, n = fill_missing_amount(df, start_date=start_date)
    if n == 0:
        return 0, None
    if dry_run:
        return n, None

    from tdxfeed.writers import Writers  # 延迟导入，避免 writers→amount_fix 循环

    if backup_dir is not None:
        # 写前备份：原文件不可逆覆盖，先留一份可回滚副本
        backup_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, backup_dir / f"{path.stem}.amountfix.bak.parquet")

    tmp = path.with_suffix(".amountfix.tmp.parquet")
    Writers._write_parquet_date32(str(tmp), filled)
    os.replace(tmp, path)
    if update_day and out_root is not None:
        Writers(str(out_root)).write_day_safe(path.stem, filled.values.tolist())
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
