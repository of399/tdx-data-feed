#!/usr/bin/env python
"""板块成分股/名称加载器（补齐计划书 §A.1 的 Sec 因子数据缺口）。

数据源：申万三级行业（akshare）
- ``ak.sw_index_third_info()``      行业列表（335 个三级行业）
- ``ak.sw_index_third_cons(symbol)`` 行业成分股（含"申万3级"名称与当日涨幅）

输出：
- ``data/industry/sector_name.csv``  code,name,sector_code   （个股 → 行业名）
- ``data/industry/sector_cons.csv``  sector_code,sector_name,code,code_name,pct_1d
  （板块成分股明细，含最新涨跌幅，可直接做板块强度）

增量：已存在的行业默认跳过（``--force`` 可重建）。
注意：东财系接口（stock_board_*_cons_em）在部分网络环境不可用，
      故本脚本只依赖申万接口（sw_index_third_cons，走 swhyscp / swsresearch 域名）。
"""
from __future__ import annotations

import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

OUT_DIR = _ROOT / "data" / "industry"
NAME_CSV = OUT_DIR / "sector_name.csv"
CONS_CSV = OUT_DIR / "sector_cons.csv"


def bare_code(raw: str) -> str:
    """'688001.SH' / 'sh688001' / '688001' → '688001'。"""
    s = str(raw).strip().upper()
    for suf in (".SH", ".SZ", ".BJ"):
        if s.endswith(suf):
            s = s[: -len(suf)]
    for pre in ("SH", "SZ", "BJ"):
        if s.startswith(pre):
            s = s[2:]
    return s.zfill(6)


def fetch_industry_list() -> pd.DataFrame:
    import akshare as ak

    df = ak.sw_index_third_info()
    df.columns = [str(c).strip() for c in df.columns]
    return df


def fetch_cons(symbol: str, retries: int = 3) -> pd.DataFrame | None:
    import akshare as ak

    last = None
    for i in range(retries):
        try:
            df = ak.sw_index_third_cons(symbol=symbol)
            if df is None or df.empty:
                return None
            return df
        except Exception as e:
            last = e
            time.sleep(1.5 * (i + 1))
    print(f"[warn] 行业成分获取失败 {symbol}: {last}", file=sys.stderr)
    return None


def main() -> int:
    p = argparse.ArgumentParser(description="申万行业成分股/名称加载")
    p.add_argument("--force", action="store_true", help="重建（默认增量）")
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--limit", type=int, default=0, help="只处理前 N 个行业（调试用）")
    args = p.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    try:
        industries = fetch_industry_list()
    except Exception as e:
        print(f"[fail] 申万行业列表拉取失败: {e}", file=sys.stderr)
        return 1

    code_col = next((c for c in industries.columns if "代码" in str(c)), None)
    name_col = next((c for c in industries.columns if "行业名称" in str(c)), None)
    if not code_col or not name_col:
        print(f"[fail] 行业列表字段异常: {list(industries.columns)}", file=sys.stderr)
        return 1

    existing: set[str] = set()
    if CONS_CSV.exists() and not args.force:
        old = pd.read_csv(CONS_CSV, dtype=str)
        if "sector_code" in old.columns:
            existing = set(old["sector_code"].astype(str))
    todo = industries[[code_col, name_col]].copy()
    todo.columns = ["sector_code", "sector_name"]
    todo = todo[~todo["sector_code"].astype(str).isin(existing)]
    if args.limit:
        todo = todo.head(args.limit)
    print(f"[info] 行业总数 {len(industries)}，待补 {len(todo)}")

    rows: list[dict] = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for (_, row), df in zip(
            todo.iterrows(),
            pool.map(lambda r: fetch_cons(r["sector_code"]), todo.to_dict("records")),
            strict=True,
        ):
            if df is None:
                continue
            sc = row["sector_code"]
            sn = row["sector_name"]
            code_col_c = next((c for c in df.columns if "股票代码" in str(c)), None)
            if not code_col_c:
                continue
            for _, r in df.iterrows():
                code = bare_code(r[code_col_c])
                if not code or not code.isdigit():
                    continue
                cn = next(
                    (str(r[c]) for c in df.columns if "股票简称" in str(c)),
                    "",
                )
                sector_name = next(
                    (str(r[c]) for c in df.columns if "申万3级" in str(c)),
                    sn,
                )
                pct = 0.0
                for c in df.columns:
                    if "近1日涨幅" in str(c):
                        try:
                            pct = float(str(r[c]).replace("%", ""))
                        except (TypeError, ValueError):
                            pct = 0.0
                        break
                rows.append(
                    {
                        "sector_code": sc,
                        "sector_name": sn,
                        "code": code,
                        "code_name": cn,
                        "pct_1d": pct,
                        "member_sector_name": sector_name,
                    }
                )

    if not rows:
        print("[warn] 无新增成分股数据")
        return 0

    new = pd.DataFrame(rows)
    if CONS_CSV.exists() and not args.force:
        old = pd.read_csv(CONS_CSV, dtype=str)
        new = pd.concat([old, new], ignore_index=True)
    new.to_csv(CONS_CSV, index=False, encoding="utf-8-sig")

    name_map = new[["code", "member_sector_name"]].rename(
        columns={"member_sector_name": "name"}
    )
    name_map = name_map.drop_duplicates(subset=["code"], keep="last")
    name_map.to_csv(NAME_CSV, index=False, encoding="utf-8-sig")

    print(f"[ok] 成分股 {len(new)} 条，覆盖 {new['code'].nunique()} 只股票")
    print(f"[ok] {CONS_CSV}")
    print(f"[ok] {NAME_CSV}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
