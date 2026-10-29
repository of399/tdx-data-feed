"""后复权因子增量更新（baostock adjustflag=1 后复权 / 3 不复权两条序列）。

口径：hfq_factor = hfq_close / raw_close，raw_close 是真不复权价（adjustflag=3）。
日常增量与缺失文件补建都走这里：文件不存在（或空表）时从模块常量 BOOTSTRAP_START
起拉全历史建表，返回 rebase:+N。旧的 scripts/download_hfq_factor.py 口径错误
（把 adjustflag=3 当后复权），已改成拒绝运行，别再拿它补文件。

旧表（2026-10-29 之前）不是后复权：那个脚本把 adjustflag=3（不复权）当成后复权，
又用 adjustflag=2（前复权）做分母，存的实际是 1/前复权因子。本轮起按真后复权
重写，schema 换成可复算的 code/date/hfq_factor/raw_close/hfq_close，
date 是 date32[day]（pandas 里是 datetime.date），不是 string 也不是 timestamp。

与 adj_factor.py 的差异：
- 后复权历史段不随新除权平移，正常只追加；重叠 1 日的用途只剩「源修订检测」：
  旧表 last_date 那天的 hfq_close 与新查询不一致 → 整表重拉（rebase）。
- 没有 _stale_base：后复权因子内部随日期变化（含下降）是正常形态，不能当基准
  漂移信号。
- 网络约束与 adj 相同：baostock 的 HTTP 不自带超时，也不保证一次成功，所以必须
  有 socket 超时、重试与连续失败熔断——否则一次登录失效会把 5221 只拖满几小时。
"""
from __future__ import annotations

import os
import socket
import sys
import time
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq

from tdxfeed.freshness import CN_HOLIDAYS, last_trading_day
from tdxfeed.paths import hfq_factor_dir

socket.setdefaulttimeout(30)

COLUMNS = ["code", "date", "hfq_factor", "raw_close", "hfq_close"]

# 缺失文件（新上市 / 新纳入池 / 空表）从零建表的起点。与旧脚本的 START_DATE 一致，
# 保证同一只票无论走哪条路径，历史段起点相同；已有文件的整表重拉仍取旧表首行日期。
BOOTSTRAP_START = "2020-01-01"

# 连续这么多只失败就中止整轮：登录失效时每只还要退避 3 次，不能一路拖下去。
FAIL_ABORT = 20


def bs_code(code: str) -> str:
    """6 位代码 → baostock 代码。4/8/92 段归北交所。"""
    c = str(code)[-6:]
    if c.startswith(("60", "68", "90", "11", "13")):
        return f"sh.{c}"
    if c.startswith(("4", "8", "92")):
        return f"bj.{c}"
    return f"sz.{c}"


def last_date(path: Path) -> str | None:
    if not Path(path).exists():
        return None
    df = pd.read_parquet(path, columns=["date"])
    if df.empty:
        return None
    return str(df["date"].iloc[-1])


def _rebase_needed(old: pd.DataFrame, hfq: list[list]) -> bool:
    """重叠日的 hfq_close 变了 → 源修订/重算了后复权序列，必须整表重拉。"""
    if old.empty or not hfq:
        return False
    by_date = {row[0]: row[1] for row in hfq}
    last_day = str(old["date"].iloc[-1])
    if last_day not in by_date:
        return False
    try:
        new_val = float(by_date[last_day])
    except (TypeError, ValueError):
        return False
    old_val = float(old["hfq_close"].iloc[-1])
    return abs(new_val - old_val) > 1e-6 * max(1.0, abs(old_val))


def _legacy_schema(old: pd.DataFrame) -> bool:
    """2026-10-29 之前的表是 qfq_close / 1-前复权口径：列都对不上，不能追加。

    这种文件即使重叠日 hfq_close 数值恰好相同（无除权的票就是 raw）也必须整表
    重拉，否则会把两套口径拼进同一张表。
    """
    return not old.empty and any(c not in old.columns for c in COLUMNS)


def needs_build(path: Path) -> bool:
    """文件不存在、空表、或仍是旧 qfq_close 口径 → 需要用全历史从零建。

    全量跑中途断链（baostock 掉线熔断）后，用它只补没建成的那些，
    不必把已建好的 5000 多只再拿 1 天窗口过一遍。
    """
    p = Path(path)
    if not p.exists():
        return True
    try:
        schema = pq.read_schema(p)
        if pq.read_metadata(p).num_rows == 0:
            return True
    except Exception:  # 坏文件按需要重建处理
        return True
    return any(c not in schema.names for c in COLUMNS)


def merge_rows(old: pd.DataFrame, hfq: list[list], raw: list[list],
               code: str | None = None) -> pd.DataFrame | None:
    """把增量两条序列并入旧表；无可用新行时返回 None。"""
    if not hfq or not raw:
        return None
    if _legacy_schema(old):
        return None
    dh = pd.DataFrame(hfq, columns=["date", "hfq_close"])
    dr = pd.DataFrame(raw, columns=["date", "raw_close"])
    new = dh.merge(dr, on="date")
    new["hfq_close"] = pd.to_numeric(new["hfq_close"], errors="coerce")
    new["raw_close"] = pd.to_numeric(new["raw_close"], errors="coerce")
    new = new.dropna(subset=["hfq_close", "raw_close"])
    new = new[new["raw_close"] > 0]
    if new.empty:
        return None
    new["hfq_factor"] = (new["hfq_close"] / new["raw_close"]).round(6)
    # 整表重拉时 old 是空表，取不到 old["code"]，由调用方用 code 填。
    new["code"] = code if code is not None else old["code"].iloc[0]
    merged = new[COLUMNS].copy() if old.empty else pd.concat(
        [old[COLUMNS], new[COLUMNS]], ignore_index=True)
    # 日期统一回 datetime.date：pyarrow 由此推出 date32[day]，与现存文件一致。
    # 别 astype(str)（会变 string 列），也别留 datetime64（会变 timestamp[ns]）。
    merged["date"] = pd.to_datetime(merged["date"]).dt.date
    merged = merged.drop_duplicates(subset=["date"], keep="last").sort_values("date")
    return merged.reset_index(drop=True)[COLUMNS]


def write_atomic(path: Path, df: pd.DataFrame) -> None:
    """tmp → os.replace。临时文件不以 .parquet 结尾，避免被文件枚举当股票。"""
    tmp = Path(str(path) + ".tmp")
    try:
        df.to_parquet(tmp, index=False)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _query(bs, code: str, start: str, end: str, adjustflag: str) -> list[list]:
    rs = bs.query_history_k_data_plus(
        code, "date,close", start_date=start, end_date=end,
        frequency="d", adjustflag=adjustflag,
    )
    # 错误码必须抛：吞掉它会让一次登录失效/接口异常退化成 5221 只「skip」，
    # 退出码仍是 0——正是这批改造要消灭的静默失败。
    if rs.error_code != "0":
        raise RuntimeError(f"baostock {rs.error_code}: {rs.error_msg}")
    rows: list[list] = []
    while rs.next():
        rows.append(rs.get_row_data())
    return rows


def _query_retry(bs, code: str, start: str, end: str, adjustflag: str,
                 attempts: int = 3) -> list[list]:
    last: Exception | None = None
    for i in range(attempts):
        try:
            return _query(bs, code, start, end, adjustflag)
        except Exception as e:
            last = e
            time.sleep(2 ** i)
    raise last  # type: ignore[misc]


def update_one(bs, code: str, out_dir: Path, today: str) -> str:
    """单只更新。返回 ok:+N / rebase:+N / skip / empty / fail:<原因>。

    文件不存在（或空表）时从 BOOTSTRAP_START 起拉全历史建表，记 rebase:+N：
    旧脚本已废弃，这是补缺失文件的唯一入口。只有源也回空才记 empty。
    """
    path = Path(out_dir) / f"{code}.parquet"
    try:
        start = last_date(path)
        if start is None:
            bootstrap = True
            start = BOOTSTRAP_START
            old = pd.DataFrame(columns=COLUMNS)
        else:
            old = pd.read_parquet(path)
            # 旧口径没有可用历史：直接按文件首行日期整表建，别先花两次请求
            # 查一遍 1 天窗口再重拉。
            bootstrap = _legacy_schema(old)
            if bootstrap:
                start = str(old["date"].iloc[0])
            elif start >= today:
                return "skip"
        n_old = len(old)
        old_first = str(old["date"].iloc[0]) if n_old else start
        old_last = str(old["date"].iloc[-1]) if n_old else ""
        hfq = _query_retry(bs, bs_code(code), start, today, "1")
        raw = _query_retry(bs, bs_code(code), start, today, "3")
        if not hfq and not raw:
            if bootstrap:
                # 补建的文件源里没有任何历史（未上市/已退市）：没东西可写，也不是失败。
                return "empty"
            # 只有窗口 (start, today] 全是休市日才算 skip。窗口里本应有交易日却
            # 两条都空 = 源整体异常，记 skip 会让 5221 只全 skip 仍退 0。
            expected = last_trading_day(date.fromisoformat(today),
                                        CN_HOLIDAYS).isoformat()
            if expected > start:
                return "fail:empty-window"
            return "skip"
        if not hfq or not raw:
            return "fail:partial-empty"          # 源只回一条序列 = 数据异常，不是没行情
        rebased = bootstrap or _rebase_needed(old, hfq)
        if rebased:
            if not bootstrap:
                # 起点取文件自身首行日期：源只回部分历史时下面会拦住，不能靠常量猜。
                hfq = _query_retry(bs, bs_code(code), old_first, today, "1")
                raw = _query_retry(bs, bs_code(code), old_first, today, "3")
            old = old.iloc[0:0]                  # 整表替换
        merged = merge_rows(old, hfq, raw, code=code)
        if merged is None:
            return "fail:no-valid-rows"
        # 缩表守卫：写盘前比行数与末日期。源少回了历史就放弃本轮，原文件不动，
        # 否则整表替换会静默把历史截断。旧表为空（补建）时没有历史可比，跳过。
        if n_old and (len(merged) < n_old
                      or str(merged["date"].iloc[-1]) < old_last):
            return "fail:shrink"
        write_atomic(path, merged)
    except Exception as e:  # 单只失败不应中断整轮
        return f"fail:{type(e).__name__}:{str(e)[:60]}"
    if rebased:
        return f"rebase:+{len(merged)}"
    return f"ok:+{len(merged) - n_old}"


def exit_code(ok: int, skip: int, fail: int) -> int:
    total = ok + skip + fail
    if total == 0:
        return 1
    if total < 1000:                 # --code / --limit 试跑：不允许有失败
        return 1 if fail else 0
    return 1 if fail > max(20, total * 0.02) else 0


def main(argv=None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="后复权因子增量更新（baostock）")
    ap.add_argument("--code", help="只更新一只，如 600000")
    ap.add_argument("--limit", type=int, help="只处理前 N 只（试跑用）")
    ap.add_argument("--missing", action="store_true",
                    help="只处理没有文件 / 空表 / 仍是旧 qfq_close 口径的标的")
    ap.add_argument("--today", default=datetime.now().strftime("%Y-%m-%d"),
                    help="截止日期，默认今天（测试可覆盖）")
    args = ap.parse_args(argv)

    import baostock as bs

    out_dir = hfq_factor_dir()
    if not out_dir.exists():
        print(f"❌ 后复权因子目录不存在: {out_dir}", file=sys.stderr)
        return 1

    if args.code:
        codes = [args.code[-6:]]
    else:
        codes = [p.stem for p in sorted(out_dir.glob("*.parquet"))
                 if len(p.stem) == 6 and p.stem.isdigit()]
    if args.limit:
        codes = codes[: args.limit]
    if args.missing:
        codes = [c for c in codes if needs_build(out_dir / f"{c}.parquet")]
    if not codes:
        print("❌ 没有可更新的后复权因子文件", file=sys.stderr)
        return 1

    lg = bs.login()
    if lg.error_code != "0":
        print(f"❌ baostock 登录失败: {lg.error_msg}", file=sys.stderr)
        return 1
    ok = skip = fail = rebase = added = 0
    consec = 0
    aborted = False
    try:
        for i, code in enumerate(codes, 1):
            r = update_one(bs, code, out_dir, args.today)
            if r.startswith("ok:") or r.startswith("rebase:"):
                ok += 1
                if r.startswith("rebase:"):
                    rebase += 1
                added += int(r.split(":")[1].lstrip("+"))
                consec = 0
            elif r == "skip":
                skip += 1
                consec = 0
            else:
                fail += 1
                consec += 1
                if fail <= 5:
                    print(f"  ✗ {code}: {r}", file=sys.stderr)
                if consec >= FAIL_ABORT:
                    # 登录失效/接口全挂：后面只会一只接一只退避，趁早停。
                    print(f"❌ 连续 {consec} 只失败，中止整轮（最后一只 {code}: {r}）",
                          file=sys.stderr)
                    aborted = True
                    break
            if i % 200 == 0:
                print(f"  [{i}/{len(codes)}] ok={ok} rebase={rebase} "
                      f"skip={skip} fail={fail}", flush=True)
            # 试跑时逐只打印，便于核对 rebase 起点与行数；全量跑不刷屏。
            if args.code or args.limit:
                print(f"{code}: {r}", flush=True)
    finally:
        bs.logout()

    print(f"完成：ok={ok} rebase={rebase} skip={skip} fail={fail} "
          f"新增行={added} 截止={args.today}")
    if aborted:
        return 1
    return exit_code(ok, skip, fail)


if __name__ == "__main__":
    sys.exit(main())
