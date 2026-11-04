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
  两个超时数（逐只查询 30 秒 / 登录握手 120 秒）与重试单源在
  `tdxfeed/baostock_session.py`，这里只调 `apply_query_timeout()` 与
  `login_with_retry(bs)`；进度与「还剩多少只」写 `data/.state/factor_run.json`
  （KI-OPS-007）。

写入侧的跨源对账（KI-DATA-004）
------------------------------
`raw_close` 是交易所价格，日线表 `data/parquet/daily` 里存的是同一个价格的另一个
来源，所以两者必须逐日相等。2026-11-03 的增量把 300386 的 11-02/11-03 两行写成了
`raw_close = hfq_close`（48.45 对 10.72），因子当场塌成 1.0，而形状、行数、日期序列
全都正常——按形状检查的守卫只会说「又下跳了一次」，说不出「这次是取错序列」。
机制至今没能定论（重查 baostock 已回正，源侧瞬态还是我们这侧两路查询接错都无本地
凭据），所以这里装的是**与成因无关的不变量**：本次取数窗口里的行，raw_close 与日线
收盘差超过容差就拒绝落盘，旧文件一字节不动。

范围是测出来的，不是拍的。2026-11-04 全历史逐行比对 16,783,596 行（清单落盘在
`data/.state/hfq_raw_close_mismatch.json`，由 `scripts/audit_hfq_raw_close.py` 产出）：
容差外 **2,339 行 / 461 只标的**，其中 2,195 行在 2000 年及以前（早期收盘在两个源之间
本就有修正差，与 KI-DATA-003 同源），**2019 年起为 0 行**。所以判据只比**本次取数窗口
产出的行**——把整表纳入会让每次 rebase 都被 1990 年代的差异拦住，那是拿守卫造一个新的
停更源；而近期段本来就没有误判。
"""
from __future__ import annotations

import os
import sys
import time
from collections import Counter
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq

from tdxfeed.baostock_session import (
    LOGIN_ATTEMPTS,
    apply_query_timeout,
    describe_failure,
    login_with_retry,
)
from tdxfeed.factor_run_state import HEARTBEAT_EVERY, write_progress
from tdxfeed.freshness import CN_HOLIDAYS, last_trading_day
from tdxfeed.paths import daily_path, hfq_factor_codes, hfq_factor_dir

# 吞错计数（SEM002）：每处被吞掉的错误都计入统计，按函数名分桶，便于事后追溯失败率
_ERROR_COUNT: Counter[str] = Counter()

# 逐只查询的紧上限（30 秒）；登录另给宽上限——两个数单源在 tdxfeed/baostock_session.py。
# 2026-11-04 就是这一行把登录截死的：握手实测要 ~60 秒，30 秒上限让它返回 10002007。
apply_query_timeout()

COLUMNS = ["code", "date", "hfq_factor", "raw_close", "hfq_close"]

# 缺失文件（新上市 / 新纳入池 / 空表）从零建表的起点。与旧脚本的 START_DATE 一致，
# 保证同一只票无论走哪条路径，历史段起点相同；已有文件的整表重拉仍取旧表首行日期。
BOOTSTRAP_START = "2020-01-01"

# 连续这么多只失败就中止整轮：登录失效时每只还要退避 3 次，不能一路拖下去。
FAIL_ABORT = 20

# raw_close 与日线收盘的对账容差：「一分钱或 0.5% 取大」。
# 下限必须是绝对值：2 元的票上一分钱就是 0.5%，纯相对容差会把正常舍入差当缺陷拒写，
# 而每只票每天都在追加行，那等于天天停更。走「取大」而不是「取小」：两个源的价格精度
# 都只到分，绝对档兜住低价票的舍入，相对档兜住高价票本就不小的分位噪声；而真正要拦的
# 那种错（raw 收到后复权序列，48.45 对 10.72）跨的是量级，两档都拦得住。
RAW_VS_DAILY_ABS = 0.011
RAW_VS_DAILY_REL = 0.005


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
        _ERROR_COUNT['_rebase_needed'] += 1
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
        _ERROR_COUNT['needs_build'] += 1
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


def _daily_closes(code: str, dates: list[str]) -> dict[str, float]:
    """从日线表取这几天的收盘，作为 `raw_close` 的第二个来源。

    取不到就回空 dict：日线缺失或坏掉不等于 hfq 不合格——新纳入池、北交所段在 daily
    里本就没有文件，判红会让整条后置任务停更，代价远大于「这几天没有基准」。
    缺失是正常状态所以先看文件在不在（不靠异常表达）；真读不动了才计一次数（SEM002）。
    """
    want = {str(d) for d in dates}
    if not want:
        return {}
    path = daily_path(code)
    if not path.exists():
        return {}
    try:
        df = pd.read_parquet(path, columns=["date", "close"])
    except Exception:
        _ERROR_COUNT['_daily_closes'] += 1
        return {}
    out: dict[str, float] = {}
    for d, c in zip(df["date"], df["close"], strict=True):
        key = str(d)
        if key in want and pd.notna(c):
            out[key] = float(c)
    return out


def raw_close_mismatches(new: pd.DataFrame, closes: dict[str, float],
                         abs_tol: float = RAW_VS_DAILY_ABS,
                         rel_tol: float = RAW_VS_DAILY_REL) -> list[tuple]:
    """逐日比 `raw_close` 与日线收盘，返回 `(date, raw_close, daily_close)` 列表。

    没有基准的日期是**跳过**而不是判错：一次取数窗口里可能混着停牌日，把「查不到」
    当「不合格」会让那只票永久拒写。报错带两个具体数值是刻意的——只给日期等于让
    下一个人再跑一遍才知道错在哪个量级。
    """
    out: list[tuple] = []
    for d, raw in zip(new["date"], new["raw_close"], strict=True):
        ref = closes.get(str(d))
        if ref is None:
            continue
        raw_f = float(raw)
        if abs(raw_f - ref) > max(abs_tol, rel_tol * abs(ref)):
            out.append((d, raw_f, ref))
    return out


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
            _ERROR_COUNT['_query_retry'] += 1
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
        # 跨源对账（KI-DATA-004）：只比本次取数窗口产出的行。整表重拉时窗口从
        # old_first 起，普通增量时从 last_date 起——历史段那两个源本就有修正差，
        # 把它们纳入判据会让每次 rebase 都被 1990 年代的差异拦住。
        boundary = old_first if rebased else start
        fresh = merged.loc[[str(d) >= boundary for d in merged["date"]]]
        bad = raw_close_mismatches(
            fresh, _daily_closes(code, [str(d) for d in fresh["date"]]))
        if bad:
            d, raw_v, ref_v = bad[0]
            return f"fail:raw-vs-daily:{len(bad)}:{d} {raw_v:.2f}≠{ref_v:.2f}"
        write_atomic(path, merged)
    except Exception as e:  # 单只失败不应中断整轮
        _ERROR_COUNT['update_one'] += 1
        return f"fail:{type(e).__name__}:{str(e)[:60]}"
    if rebased:
        return f"rebase:+{len(merged)}"
    return f"ok:+{len(merged) - n_old}"


# baostock 会话在长跑中会掉：10001001 用户未登录、10002007 网络接收错误。
# 重连后重试同一只；数据缺陷（shrink / empty-window）不在此列，重连救不了。
SESSION_LOST_CODES = ("10001001", "10002007")
MAX_RELOGIN = 30


def _session_lost(result: str) -> bool:
    return result.startswith("fail:") and any(c in result for c in SESSION_LOST_CODES)


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

    # 枚举判据在 paths 里单源：审计侧（scripts/audit_hfq_raw_close.py）扫的是同一批
    # 文件，两边的「全市场」才是同一个集合。
    codes = [args.code[-6:]] if args.code else hfq_factor_codes()
    if args.limit:
        codes = codes[: args.limit]
    if args.missing:
        codes = [c for c in codes if needs_build(out_dir / f"{c}.parquet")]
    if not codes:
        print("❌ 没有可更新的后复权因子文件", file=sys.stderr)
        return 1

    started_at = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    target = len(codes)
    ok = skip = fail = rebase = added = 0
    processed = 0
    # 先落一条「本轮开始了、目标 N 只」再登录：登录彻底失败时这份心跳就是
    # finished=false / processed=0 / unfinished=N——今天缺 hfq 就是这样被吞掉的。
    write_progress("hfq_factor", today=args.today, started_at=started_at, target=target,
                   processed=processed, ok=ok, skip=skip, fail=fail)

    lg_ok, lg_msg = login_with_retry(bs)
    if not lg_ok:
        print(f"❌ {describe_failure(LOGIN_ATTEMPTS, lg_msg)}", file=sys.stderr)
        return 1
    consec = 0
    relogin = 0
    aborted = False
    try:
        for i, code in enumerate(codes, 1):
            r = update_one(bs, code, out_dir, args.today)
            if _session_lost(r) and relogin < MAX_RELOGIN:
                relogin += 1
                re_ok, _ = login_with_retry(bs)
                if re_ok:
                    print(f"  ↻ baostock 会话中断，已重连（第 {relogin} 次）：{code}",
                          flush=True)
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
            processed = i
            if i % HEARTBEAT_EVERY == 0:
                print(f"  [{i}/{len(codes)}] ok={ok} rebase={rebase} "
                      f"skip={skip} fail={fail}", flush=True)
                write_progress("hfq_factor", today=args.today, started_at=started_at,
                               target=target, processed=processed, ok=ok, skip=skip,
                               fail=fail, rebase=rebase, added_rows=added)
            # 试跑时逐只打印，便于核对 rebase 起点与行数；全量跑不刷屏。
            if args.code or args.limit:
                print(f"{code}: {r}", flush=True)
    finally:
        bs.logout()

    write_progress("hfq_factor", today=args.today, started_at=started_at, target=target,
                   processed=processed, ok=ok, skip=skip, fail=fail, rebase=rebase,
                   added_rows=added, finished=True, aborted=aborted)
    print(f"完成：ok={ok} rebase={rebase} skip={skip} fail={fail} "
          f"新增行={added} 截止={args.today} 未跑={max(target - processed, 0)}")
    if aborted:
        return 1
    return exit_code(ok, skip, fail)


if __name__ == "__main__":
    sys.exit(main())
