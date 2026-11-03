"""成交额缺口修复：Sina 日 K 无 amount 时用 close × volume(手) × 100 估算。

估算值会落进 amount 列，和真实成交额长得一样。为了让下游能区分，估算过的
(symbol, date) 另记一份旁路清单（不改 daily 表 schema）：
    data/.state/amount_estimated.json  {"sh600000": [20260105, ...]}
清单每次写盘整段替换该 symbol，真实 amount 回来后条目自动消失。

量级守卫（只检测、不改写）记第二份同形状清单：
    data/.state/amount_deviation.json  {"sh600000": [20261026, ...]}
它补的是估算逻辑的盲区——amount>0 却比公式低一个量级的错值行（主源偶发脏数据）
绕过了 amount<=0 的触发条件，永远不会被标进估算清单。两份清单共用同一套进程内
缓冲与 atexit 落盘，只是文件名与默认路径不同。

判据只治理 `PROXY_CALIBER_START`（2026-09-16）之后的行：断点前是真实观测成交额，
close × volume 只是当日 VWAP 的近似，按同一阈值判会命中 948 万行、把清单彻底淹没
（常量处的全市场实测数就是这条边界的依据）。所以这份清单的语义是「代理期内应等于
公式却不相等的交易日」，不是「全历史成交额异常清单」。

清单是进程内缓冲、退出时落盘（`flush_estimated` / atexit）：全市场同步一次
要记 5000 只，逐只重写整个 JSON 是 O(n²)。缓冲期间 `estimated_dates` 也能读到；
进程被 kill -9 时丢失的条目会在下次写入时按合并结果重算，属可自愈的标注数据。
"""
from __future__ import annotations

import atexit
import json
import os
import re
import shutil
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

# 吞错计数（SEM002）：每处被吞掉的错误都计入统计，按函数名分桶，便于事后追溯失败率
_ERROR_COUNT: Counter[str] = Counter()

_LEDGER_LOCK = threading.RLock()
_LEDGER_CACHE: dict[Path, dict[str, list[int]]] = {}
_LEDGER_TOUCHED: dict[Path, set[str]] = {}
_LEDGER_ATEXIT_ARMED = False

_LEDGER_FILE = "amount_estimated.json"
_DEVIATION_FILE = "amount_deviation.json"

# 量级阈值依据：2026-09-16 起全市场 amount 精确等于 close × volume(手) × 100，
# 已知错值行偏离 6~15 倍（dev≈0.83~0.94），与阈值相隔两个量级。
DEVIATION_TOLERANCE = 0.005

# 判据只在代理期口径内成立：日线主源换到新浪后日 K 不再返回 amount，2026-09-16
# 起表内 amount 全市场精确等于公式（docs/OPS_SYNC_CHAIN_BUDGET_AND_FRESHNESS.md §6.1）。
# 断点之前是真实观测成交额，close × volume 只是当日 VWAP 的近似——2026-11-03 全市场
# 实测（5,236 个日线文件 / 16,786,827 可判定行）：dev>0.005 在断点后命中 5 行（正是
# KI-DATA-002 那 5 行），在断点前命中 9,479,112 行、涉及 5,207 只（dev 中位 1.05%、
# p90 2.55%）；历史期即使放宽到 dev>0.5 仍有 7,086 行，其中 7,062 行落在 1990-1993
# 的早期成交额口径。所以单一全局阈值会把清单灌成噪声、淹没真正那 5 行 ⇒ 按断点分档。
PROXY_CALIBER_START = 20260916

_DAILY_SYMBOL_RE = re.compile(r"^[a-z]{2}\d{6}$")

__all__ = [
    "DEVIATION_TOLERANCE",
    "PROXY_CALIBER_START",
    "backfill_daily_dir",
    "detect_deviation_dates",
    "deviation_dates",
    "deviation_ledger_path",
    "estimated_dates",
    "fill_missing_amount",
    "fill_missing_amount_marked",
    "flush_deviation",
    "flush_estimated",
    "ledger_path_for",
    "load_estimated",
    "merge_estimated",
    "record_deviation",
    "record_deviation_for_table",
    "record_estimated",
]


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
    return Path(root) / ".state" / _LEDGER_FILE


def deviation_ledger_path(root: str | Path | None = None) -> Path:
    """偏差清单位置：与估算清单同目录同形状，只是文件名不同。

    root 给定时按数据根拼（写日线时从表路径回推的根）；缺省取配置的数据根。
    """
    if root is not None:
        return Path(root) / ".state" / _DEVIATION_FILE
    return _resolve_path(None, filename=_DEVIATION_FILE)


def _resolve_path(path: str | Path | None,
                  filename: str = _LEDGER_FILE) -> Path:
    if path is not None:
        return Path(path)
    from tdxfeed.paths import state_dir

    return state_dir() / filename


def _read_ledger_file(p: Path) -> dict[str, list[int]]:
    """读盘上的清单；坏文件当空清单（清单只用于标注，不参与控制流）。"""
    if not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        _ERROR_COUNT['_read_ledger_file'] += 1
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, list[int]] = {}
    for k, v in raw.items():
        if isinstance(v, list):
            try:
                out[str(k)] = sorted({int(x) for x in v})
            except (TypeError, ValueError):
                _ERROR_COUNT['_read_ledger_file'] += 1
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
    _replace_entry(_resolve_path(path), symbol, dates)


def _replace_entry(p: Path, symbol: str, dates) -> None:
    """两份清单共用的缓冲写入：整段替换该 symbol，只改进程内视图。

    缓冲按清单文件（Path）分键，所以估算与偏差各记各的，互不覆盖。
    """
    global _LEDGER_ATEXIT_ARMED

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
            _flush_one(p)


def flush_deviation(path: str | Path | None = None) -> None:
    """把偏差清单的缓冲写盘；缺省时刷本进程动过的所有偏差文件。"""
    with _LEDGER_LOCK:
        targets = ([_resolve_path(path, filename=_DEVIATION_FILE)]
                   if path is not None else
                   [p for p in _LEDGER_TOUCHED if p.name == _DEVIATION_FILE])
        for p in targets:
            _flush_one(p)


def _flush_one(p: Path) -> None:
    """单个清单文件的落盘：重读磁盘 → 只替换本进程动过的 symbol → 原子换。"""
    with _LEDGER_LOCK:
        touched = _LEDGER_TOUCHED.get(p)
        if not touched:
            return
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
            _ERROR_COUNT['_flush_at_exit'] += 1
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


# ---------------------------------------------------------------------------
# 量级守卫：amount>0 却与公式差一个量级的错值行，只登记、不改写
# ---------------------------------------------------------------------------
def _f64(s: pd.Series) -> np.ndarray:
    """数值列 → float64 ndarray。float64 走零拷贝快路径（日线三列本就是它）。"""
    if s.dtype == np.float64:
        return s.to_numpy(copy=False)
    return pd.to_numeric(s, errors="coerce").to_numpy(dtype="float64")


def _yyyymmdd(s: pd.Series) -> pd.Series:
    """任意日线日期表示 → Int64 的 YYYYMMDD（清单键口径，与 Writers._date_int 一致）。"""
    if pd.api.types.is_integer_dtype(s):
        return s.astype("int64")
    if pd.api.types.is_float_dtype(s):
        return s.round().astype("Int64")
    return pd.to_datetime(s, errors="coerce").dt.strftime("%Y%m%d").astype("Int64")


def detect_deviation_dates(df: pd.DataFrame,
                           tolerance: float = DEVIATION_TOLERANCE,
                           proxy_start: int = PROXY_CALIBER_START) -> set[int]:
    """代理期内的「amount>0 但与 close × volume(手) × 100 偏离超阈值」的日期集合。

    只读 df、只回日期：守卫不改写表内数值——改值要源侧证据，静默改写会把错值
    伪装成真实成交额，还会让估算清单的 amount<=0 触发条件失真。
    缺失/非有限的三列都不判定（宁可漏记，不可把判据外推行）。
    `proxy_start` 之前的行不判定：那段是真实观测成交额，公式只是 VWAP 近似，
    按 0.5% 判会把近万行/票的正常历史偏离灌进清单（模块常量处有全市场实测数）。
    """
    if not {"amount", "volume", "close", "date"}.issubset(df.columns):
        return set()
    amount = _f64(df["amount"])
    volume = _f64(df["volume"])
    close = _f64(df["close"])
    est = close * volume * 100.0
    # NA 日期 → nan → 比较为 False → 不判定（与「判不了就不记」同口径）
    day = _yyyymmdd(df["date"]).astype("float64").to_numpy()
    ok = ((amount > 0) & (volume > 0) & (close > 0)
          & np.isfinite(amount) & np.isfinite(est) & (day >= proxy_start))
    with np.errstate(invalid="ignore", divide="ignore"):
        dev = np.abs(amount - est) / est
    positions = np.flatnonzero(ok & (dev > tolerance))
    if positions.size == 0:
        return set()
    return {int(v) for v in day[positions]}


def deviation_dates(symbol: str, path: str | Path | None = None) -> set[int]:
    """该 symbol 当前标为量级异常的交易日集合（YYYYMMDD int，含未落盘条目）。"""
    p = _resolve_path(path, filename=_DEVIATION_FILE)
    with _LEDGER_LOCK:
        return set(_ledger_view(p).get(str(symbol), []))


def record_deviation(symbol: str, df: pd.DataFrame,
                     path: str | Path | None = None) -> set[int]:
    """按整表评估并整段替换该 symbol 的偏差日期（口径同 record_estimated）。"""
    days = detect_deviation_dates(df)
    _replace_entry(_resolve_path(path, filename=_DEVIATION_FILE), symbol, days)
    return days


def _daily_symbol_from_name(name: str) -> str | None:
    """日线规范名及其原子写临时名 → symbol；旁支表（_indicators、.v1.bak）返回 None。"""
    stem, _, rest = name.partition(".")
    if not _DAILY_SYMBOL_RE.match(stem):
        return None
    if rest == "parquet":
        return stem
    if rest.endswith("tmp") and "bak" not in rest:
        return stem
    return None


def _data_root_from_daily_table(table_path: str | Path) -> Path | None:
    """<root>/parquet/daily/<symbol>.parquet → root；形态不符返回 None。

    只认 daily 目录：一是「只在写日线时评估」，二是免得在别的目录下凭空造 .state。
    """
    p = Path(os.path.abspath(str(table_path)))
    if not p.parents or p.parents[0].name != "daily":
        return None
    daily = p.parents[0]
    # daily 的上一级是 parquet 时，数据根再往上一层；否则 daily 直接挂在数据根下
    if daily.parents and daily.parents[0].name == "parquet":
        return daily.parents[1]
    return daily.parents[0]


def record_deviation_for_table(table_path: str | Path,
                               df: pd.DataFrame) -> set[int]:
    """写日线表时的挂钩：从表路径回推 symbol 与数据根，非日线形态直接不记。"""
    symbol = _daily_symbol_from_name(Path(table_path).name)
    root = _data_root_from_daily_table(table_path)
    if symbol is None or root is None:
        return set()
    try:
        return record_deviation(symbol, df, deviation_ledger_path(root))
    except Exception as e:  # 清单是标注数据，不参与写盘控制流
        _ERROR_COUNT['record_deviation_for_table'] += 1
        print(f"[WARN] amount 偏差登记失败 {table_path}: {e!r}")
        return set()


def _backfill_one(path: Path, dry_run: bool, update_day: bool,
                  out_root: Path | None,
                  start_date: int | None,
                  backup_dir: Path | None = None) -> tuple[int, str | None]:
    try:
        df = pd.read_parquet(path)
    except Exception as e:
        _ERROR_COUNT['_backfill_one'] += 1
        return 0, f"{path.name}: {e!r}"
    filled, n, est = fill_missing_amount_marked(df, start_date=start_date)
    if n == 0:
        # 一行都没补就不会落盘，写盘侧的量级评估也就跑不到；这里补一次纯检测。
        # dry_run 不产生任何副作用（与既有的「估算清单也不记」同口径）。
        if not dry_run:
            record_deviation_for_table(path, df)
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
