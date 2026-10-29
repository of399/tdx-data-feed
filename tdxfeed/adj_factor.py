"""复权因子增量更新（baostock 前复权 / 不复权两条序列）。

历史由 scripts/download_adj_factor_b.py 一次性回填；那个脚本命中已存在文件即
return 'cached'，不负责更新，所以每日维护必须走这里。

schema 与现存文件逐列一致：code/date/adj_factor/qfq_close/raw_close，
date 是 date32[day]（pandas 里是 datetime.date），不是 string 也不是 timestamp。

两条不能省的约束：
- 增量窗口从 last_date 当日起（重叠 1 日），用重叠日比对 qfq_close；前复权基准
  一除权就整段平移，只追加会把旧段留在旧基准上。
- baostock 的 HTTP 不自带超时，也不保证一次成功，所以必须有 socket 超时、重试与
  连续失败熔断——否则一次登录失效会让 5221 只 × 3 次退避拖满几小时。
"""
from __future__ import annotations

import os
import socket
import sys
import time
from datetime import datetime
from pathlib import Path

import pandas as pd

from tdxfeed.paths import adj_factor_dir, adj_factor_files

socket.setdefaulttimeout(30)

COLUMNS = ["code", "date", "adj_factor", "qfq_close", "raw_close"]

# 整表重拉的起点：现存文件最早一行都在 2020-01 及以后。
START_DATE = "2020-01-01"

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


def _rebase_needed(old: pd.DataFrame, qfq: list[list]) -> bool:
    """重叠日的 qfq_close 变了 → 除权已重算前复权基准，必须整表重拉。"""
    if old.empty or not qfq:
        return False
    by_date = {row[0]: row[1] for row in qfq}
    last_day = str(old["date"].iloc[-1])
    if last_day not in by_date:
        return False
    try:
        new_val = float(by_date[last_day])
    except (TypeError, ValueError):
        return False
    old_val = float(old["qfq_close"].iloc[-1])
    return abs(new_val - old_val) > 1e-6 * max(1.0, abs(old_val))


def _stale_base(old: pd.DataFrame) -> bool:
    """旧表自己就有两套基准：前复权因子随时间只能不升（分红/送转把历史价压低），
    一旦「越往后越小」，说明前段是旧基准、后段是新基准——只追加不回算留下的接缝。

    首轮修复前写进 000513 的正是这种：09-24 adj=1.0、09-28 adj=0.965341。
    这种接缝在重叠日上已经看不出来（重叠日在接缝之后，两边同基准），只能查内部。
    """
    if old.empty:
        return False
    af = pd.to_numeric(old["adj_factor"], errors="coerce").dropna()
    return bool((af.diff() < -1e-6).any())


def merge_rows(old: pd.DataFrame, qfq: list[list], raw: list[list],
               code: str | None = None) -> pd.DataFrame | None:
    """把增量两条序列并入旧表；无可用新行时返回 None。"""
    if not qfq or not raw:
        return None
    dq = pd.DataFrame(qfq, columns=["date", "qfq_close"])
    dr = pd.DataFrame(raw, columns=["date", "raw_close"])
    new = dq.merge(dr, on="date")
    new["qfq_close"] = pd.to_numeric(new["qfq_close"], errors="coerce")
    new["raw_close"] = pd.to_numeric(new["raw_close"], errors="coerce")
    new = new.dropna(subset=["qfq_close", "raw_close"])
    new = new[new["raw_close"] > 0]
    if new.empty:
        return None
    new["adj_factor"] = (new["qfq_close"] / new["raw_close"]).round(6)
    # 整表重拉时 old 是空表，取不到 old["code"]，由调用方用 code 填。
    new["code"] = code if code is not None else old["code"].iloc[0]
    merged = pd.concat([old[COLUMNS], new[COLUMNS]], ignore_index=True)
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
    """单只更新。返回 ok:+N / rebase:+N / skip / empty / fail:<原因>。"""
    path = Path(out_dir) / f"{code}.parquet"
    try:
        start = last_date(path)
        if start is None:
            return "empty"
        if start >= today:
            return "skip"
        old = pd.read_parquet(path)
        qfq = _query_retry(bs, bs_code(code), start, today, "2")
        raw = _query_retry(bs, bs_code(code), start, today, "3")
        if not qfq and not raw:
            return "skip"                        # 周末/节假日：没有新交易日
        if not qfq or not raw:
            return "fail:partial-empty"          # 源只回一条序列 = 数据异常，不是没行情
        rebased = _rebase_needed(old, qfq) or _stale_base(old)
        n_old = len(old)
        if rebased:
            qfq = _query_retry(bs, bs_code(code), START_DATE, today, "2")
            raw = _query_retry(bs, bs_code(code), START_DATE, today, "3")
            old = old.iloc[0:0]                  # 整表替换
        merged = merge_rows(old, qfq, raw, code=code)
        if merged is None:
            return "fail:no-valid-rows"
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

    ap = argparse.ArgumentParser(description="复权因子增量更新（baostock）")
    ap.add_argument("--code", help="只更新一只，如 600000")
    ap.add_argument("--limit", type=int, help="只处理前 N 只（试跑用）")
    ap.add_argument("--today", default=datetime.now().strftime("%Y-%m-%d"),
                    help="截止日期，默认今天（测试可覆盖）")
    args = ap.parse_args(argv)

    import baostock as bs

    out_dir = adj_factor_dir()
    if not out_dir.exists():
        print(f"❌ 复权因子目录不存在: {out_dir}", file=sys.stderr)
        return 1

    if args.code:
        codes = [args.code[-6:]]
    else:
        codes = [p.stem for p in adj_factor_files()
                 if len(p.stem) == 6 and p.stem.isdigit()]
    if args.limit:
        codes = codes[: args.limit]
    if not codes:
        print("❌ 没有可更新的复权因子文件", file=sys.stderr)
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
            if args.code:
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
