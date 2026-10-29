#!/usr/bin/env python
"""把 data/hfq_factor 回填到上市日：baostock 全历史（adjustflag=1 / 3）。

    venv/bin/python scripts/backfill_hfq_history.py --symbols 600000,000001 --allow-anytime
    venv/bin/python scripts/backfill_hfq_history.py --workers 3     # 18:00 后全量

三道门禁：
  1 重叠区 raw_close 最大相对误差 > 0.5% → 整只 reject，零写入（raw 是交易所价格权威）
  2 重叠区 hfq_close 最大相对误差 > 0.5% → 采用新段并记 rebased（因子可在同源内重算）
  3 合并不缩表：旧表行全保留，行数只增不减

并发用进程而不是线程：baostock 的连接挂在模块级 context 单例上
（baostock/util/socketutil.py 的 SocketUtil），线程会交错收发同一条 socket。
"""
from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import os
import shutil
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tdxfeed.backfill_history import acquire_run_lock  # noqa: E402
from tdxfeed.hfq_factor import (  # noqa: E402
    COLUMNS, FAIL_ABORT, _query_retry, bs_code, exit_code, merge_rows, write_atomic)
from tdxfeed.paths import hfq_factor_dir  # noqa: E402

GATE_TOL = 0.005
START = "1990-01-01"
STATE_PATH = ROOT / "data" / ".state" / "hfq_backfill.json"
BACKUP_DIR = ROOT / "data" / "hfq_factor.bak-20261030"


def max_rel_err(old: pd.Series, new: pd.Series) -> float:
    """逐行相对误差的绝对最大值；任一侧非有限值的行不参与比较。"""
    a = pd.to_numeric(old, errors="coerce").to_numpy(dtype=float)
    b = pd.to_numeric(new, errors="coerce").to_numpy(dtype=float)
    mask = np.isfinite(a) & np.isfinite(b)
    if not mask.any():
        return float("inf")
    return float(np.max(np.abs(b[mask] - a[mask]) / np.maximum(np.abs(a[mask]), 1e-9)))


def decide(old: pd.DataFrame, new: pd.DataFrame) -> tuple[str, str]:
    """返回 (action, detail)，action ∈ {ok, rebase, reject}。"""
    if old.empty:
        return "ok", "bootstrap"
    ov = old[["date", "raw_close", "hfq_close"]].merge(
        new[["date", "raw_close", "hfq_close"]], on="date", suffixes=("_old", "_new"))
    if ov.empty:
        return "reject", "no-overlap"
    raw_err = max_rel_err(ov["raw_close_old"], ov["raw_close_new"])
    if raw_err > GATE_TOL:
        return "reject", f"raw-overlap-err={raw_err:.4%}"
    hfq_err = max_rel_err(ov["hfq_close_old"], ov["hfq_close_new"])
    if hfq_err > GATE_TOL:
        return "rebase", f"hfq-overlap-err={hfq_err:.4%}"
    return "ok", f"raw-err={raw_err:.4%} hfq-err={hfq_err:.4%}"


def combine(old: pd.DataFrame, new: pd.DataFrame) -> pd.DataFrame:
    """旧表全保留、重叠日用新值；行数只增不减是这里的不变量。"""
    merged = new[COLUMNS].copy() if old.empty else pd.concat(
        [old[COLUMNS], new[COLUMNS]], ignore_index=True)
    merged["date"] = pd.to_datetime(merged["date"]).dt.date
    merged = merged.drop_duplicates(subset=["date"], keep="last").sort_values("date")
    merged = merged.reset_index(drop=True)[COLUMNS]
    assert len(merged) >= len(old), "combine 丢了旧行"
    return merged


def _backup_once(path: Path, backup_dir: Path) -> None:
    if not path.exists():
        return
    backup_dir.mkdir(parents=True, exist_ok=True)
    dst = backup_dir / path.name
    if not dst.exists():          # 续跑不覆盖首份备份：那才是回填前的原样
        shutil.copy2(path, dst)


def load_state(path: Path = STATE_PATH) -> dict:
    if not Path(path).exists():
        return {"done": [], "rejected": {}, "not_in_source": [], "rebased": {}}
    with open(path, encoding="utf-8") as fh:
        st = json.load(fh)
    for k, v in (("done", []), ("rejected", {}), ("not_in_source", []), ("rebased", {})):
        st.setdefault(k, v)
    return st


def save_state(st: dict, path: Path = STATE_PATH) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(st, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def plan_codes(out_dir: Path, state: dict, *, symbols: str = "", limit: int = 0,
               force: bool = False) -> list[str]:
    if symbols:
        codes = [s.strip()[-6:] for s in symbols.split(",") if s.strip()]
    else:
        codes = [p.stem for p in sorted(Path(out_dir).glob("*.parquet"))
                 if len(p.stem) == 6 and p.stem.isdigit()]
    done = set(state["done"]) | set(state["not_in_source"])
    if force:
        done = set(state["not_in_source"])
    codes = [c for c in codes if c not in done]
    return codes[:limit] if limit else codes


def _drain(rs) -> bool:
    hit = False
    while rs.next():
        if rs.get_row_data()[0]:
            hit = True
    return hit


def _in_source(bs, code: str) -> bool:
    """源里根本没有这个代码（退市/指数段）才算 not-in-source，接口异常必须抛。"""
    rs = bs.query_stock_basic(code=bs_code(code))
    if rs.error_code != "0":
        raise RuntimeError(f"baostock {rs.error_code}: {rs.error_msg}")
    return _drain(rs)


def _mark(state: dict, key: str, code: str, value=None) -> None:
    """状态集只增不重：--force 重跑同一只时不能再 append 一次。"""
    if key in ("done", "not_in_source"):
        if code not in state[key]:
            state[key].append(code)
    else:
        state[key][code] = value


def backfill_one(bs, code: str, out_dir: Path, today: str, backup_dir: Path) -> str:
    """单只回填。返回 ok:+N / rebase:+N:detail / reject:原因 / not-in-source / fail:原因。"""
    path = Path(out_dir) / f"{code}.parquet"
    try:
        old = pd.read_parquet(path) if path.exists() else pd.DataFrame(columns=COLUMNS)
        hfq = _query_retry(bs, bs_code(code), START, today, "1")
        raw = _query_retry(bs, bs_code(code), START, today, "3")
        if not hfq and not raw:
            return "fail:empty-source" if _in_source(bs, code) else "not-in-source"
        if not hfq or not raw:
            return "fail:partial-empty"
        new = merge_rows(pd.DataFrame(columns=COLUMNS), hfq, raw, code=code)
        if new is None:
            return "fail:no-valid-rows"
        action, detail = decide(old, new)
        if action == "reject":
            return f"reject:{detail}"
        merged = combine(old, new)
        _backup_once(path, Path(backup_dir))
        write_atomic(path, merged)
        added = len(merged) - len(old)
    except Exception as e:                 # 逐只隔离：一只坏不中断整轮
        return f"fail:{type(e).__name__}:{str(e)[:120]}"
    if action == "rebase":
        return f"rebase:+{added}:{detail}"
    return f"ok:+{added}"


_BS = None


def _get_bs():
    global _BS
    if _BS is None:
        import baostock as bs
        lg = bs.login()
        if lg.error_code != "0":
            raise RuntimeError(f"baostock 登录失败: {lg.error_msg}")
        _BS = bs
    return _BS


def _worker(code: str, out_dir: str, today: str, backup_dir: str):
    try:
        return code, backfill_one(_get_bs(), code, Path(out_dir), today, Path(backup_dir))
    except Exception as e:
        return code, f"fail:{type(e).__name__}:{str(e)[:120]}"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="hfq 因子回填到上市日（baostock 全历史）")
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--symbols", default="", help="逗号分隔，彩排用，如 600000,000001")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--today", default=datetime.now().strftime("%Y-%m-%d"))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true", help="忽略 done 集重跑")
    ap.add_argument("--allow-anytime", action="store_true",
                    help="跳过 18:00 时窗检查（只用于彩排）")
    args = ap.parse_args(argv)

    if not args.allow_anytime and datetime.now().hour < 18:
        print("❌ 18:00 前不跑：16:30 的 tdx-sync 后置任务会写同一批 hfq 文件；"
              "彩排请显式加 --allow-anytime", file=sys.stderr)
        return 1

    out_dir = hfq_factor_dir()
    lock = acquire_run_lock(str(STATE_PATH) + ".lock")
    if lock is None:
        print(f"❌ 已有回填进程持有 {STATE_PATH}.lock", file=sys.stderr)
        return 1
    state = load_state()
    todo = plan_codes(out_dir, state, symbols=args.symbols, limit=args.limit,
                      force=args.force)
    print(f"待回填 {len(todo)} 只（done {len(state['done'])} / "
          f"rejected {len(state['rejected'])} / not_in_source {len(state['not_in_source'])}）")
    if not todo:
        return 0
    if args.dry_run:
        print(f"dry-run 前 10 只 {todo[:10]}")
        return 0

    ok = skip = fail = 0
    streak = 0
    tripped = False
    # 3.14 的默认 forkserver 在本机会在认证阶段 EAGAIN；spawn 走独立解释器，
    # 语义与 forkserver 相同（每个 worker 各自 login），启动开销可忽略。
    with ProcessPoolExecutor(max_workers=args.workers,
                             mp_context=mp.get_context("spawn")) as ex:
        futs = {ex.submit(_worker, c, str(out_dir), args.today, str(BACKUP_DIR)): c
                for c in todo}
        for i, fut in enumerate(as_completed(futs), 1):
            try:
                code, r = fut.result()
            except Exception as e:
                code, r = futs[fut], f"fail:{type(e).__name__}"
            if r.startswith("ok:") or r.startswith("rebase:"):
                ok += 1
                streak = 0
                _mark(state, "done", code)
                state["rejected"].pop(code, None)   # 修好后不再留在拒绝集里
                if r.startswith("rebase:"):
                    state["rebased"][code] = r.split(":", 2)[-1]
                if args.symbols:                    # 彩排/调试：逐只看结果
                    print(f"  {code}: {r}")
            elif r == "not-in-source":
                skip += 1
                streak = 0
                _mark(state, "not_in_source", code)
            else:
                fail += 1
                streak += 1
                if r.startswith("reject:"):
                    _mark(state, "rejected", code, r.split(":", 1)[1])
                print(f"  ✗ {code}: {r}", file=sys.stderr)
                if streak >= FAIL_ABORT:
                    pending = sum(1 for f in futs if f.cancel())
                    tripped = True
                    print(f"❌ 连续 {streak} 只失败，撤销 {pending} 个未启动任务",
                          file=sys.stderr)
                    break
            save_state(state)          # 单写者：只有主线程改 state
            if i % 100 == 0:
                print(f"  [{i}/{len(todo)}] ok={ok} skip={skip} fail={fail}", flush=True)

    print(f"完成：ok={ok} not_in_source={skip} fail={fail} 计划={len(todo)}")
    if tripped:
        return 1
    return exit_code(ok, skip, fail)


if __name__ == "__main__":
    sys.exit(main())
