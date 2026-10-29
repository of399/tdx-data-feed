"""复权因子增量更新（baostock 前复权 / 不复权两条序列）。

历史由 scripts/download_adj_factor_b.py 一次性回填；那个脚本命中已存在文件即
return 'cached'，不负责更新，所以每日维护必须走这里。

schema 与现存文件逐列一致：code/date/adj_factor/qfq_close/raw_close，
date 是 date32[day]（pandas 里是 datetime.date），不是 string 也不是 timestamp。
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from tdxfeed.paths import adj_factor_dir

COLUMNS = ["code", "date", "adj_factor", "qfq_close", "raw_close"]


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


def merge_rows(old: pd.DataFrame, qfq: list[list], raw: list[list]) -> pd.DataFrame | None:
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
    new["code"] = old["code"].iloc[0]
    merged = pd.concat([old[COLUMNS], new[COLUMNS]], ignore_index=True)
    # 日期统一回 datetime.date：pyarrow 由此推出 date32[day]，与现存文件一致。
    # 别 astype(str)（会变 string 列），也别留 datetime64（会变 timestamp[ns]）。
    merged["date"] = pd.to_datetime(merged["date"]).dt.date
    merged = merged.drop_duplicates(subset=["date"], keep="last").sort_values("date")
    return merged.reset_index(drop=True)[COLUMNS]


def write_atomic(path: Path, df: pd.DataFrame) -> None:
    """tmp → os.replace。临时文件不以 .parquet 结尾，避免被文件枚举当股票。"""
    tmp = Path(str(path) + ".tmp")
    df.to_parquet(tmp, index=False)
    os.replace(tmp, path)


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


def update_one(bs, code: str, out_dir: Path, today: str) -> str:
    path = Path(out_dir) / f"{code}.parquet"
    start = last_date(path)
    if start is None:
        return "empty"
    if start >= today:
        return "skip"
    old = pd.read_parquet(path)
    start_next = (datetime.strptime(start, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    try:
        qfq = _query(bs, bs_code(code), start_next, today, "2")
        raw = _query(bs, bs_code(code), start_next, today, "3")
    except Exception as e:  # 单只失败不应中断整轮
        return f"fail:{type(e).__name__}:{str(e)[:60]}"
    merged = merge_rows(old, qfq, raw)
    if merged is None:
        return "skip"
    write_atomic(path, merged)
    return f"ok:+{len(merged) - len(old)}"


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
        codes = sorted(p.stem for p in out_dir.glob("*.parquet")
                       if len(p.stem) == 6 and p.stem.isdigit())
    if args.limit:
        codes = codes[: args.limit]
    if not codes:
        print("❌ 没有可更新的复权因子文件", file=sys.stderr)
        return 1

    lg = bs.login()
    if lg.error_code != "0":
        print(f"❌ baostock 登录失败: {lg.error_msg}", file=sys.stderr)
        return 1
    ok = skip = fail = added = 0
    try:
        for i, code in enumerate(codes, 1):
            r = update_one(bs, code, out_dir, args.today)
            if r.startswith("ok:"):
                ok += 1
                added += int(r.split(":")[1].lstrip("+"))
            elif r == "skip":
                skip += 1
            else:
                fail += 1
                if fail <= 5:
                    print(f"  ✗ {code}: {r}", file=sys.stderr)
            if i % 200 == 0:
                print(f"  [{i}/{len(codes)}] ok={ok} skip={skip} fail={fail}", flush=True)
    finally:
        bs.logout()

    total = ok + skip + fail
    print(f"完成：ok={ok} skip={skip} fail={fail} 新增行={added} 截止={args.today}")
    return 1 if fail > max(20, total * 0.02) else 0


if __name__ == "__main__":
    sys.exit(main())
