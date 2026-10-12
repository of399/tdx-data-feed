#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scripts/akshare_concept_loader.py — 同花顺(THS)题材/概念板块数据加载

数据源: ak.stock_board_concept_name_ths()
返回: 同花顺概念板块列表 (name, code)
  - name: 概念名称 (如 "AI手机")
  - code: 同花顺概念板块代码 (如 "308614")

字段(清洗后输出, data/concept/ths_concept_list.csv):
  - concept_code  str  同花顺概念板块代码(零填充字符串)
  - concept_name  str  概念名称

输出文件:
  - data/concept/ths_concept_list.csv        概念板块 名称+代码 列表
  - data/concept/ths_concept_list_meta.json  抓取元数据(fetched_at + count)

更新频率:
  - 同花顺概念板块会随市场热点新增, 建议每日或每周刷新; 默认缓存 7 天 TTL。

错误处理与重试:
  - 3 次指数退避 (1s / 4s / 9s), 与项目规范一致
  - 网络异常 / 空结果 / 字段缺失 均视为失败并触发重试
  - 全部重试失败时回退本地缓存(若存在), 否则报错退出

查询 / 筛选 API (均返回 pandas.DataFrame):
  - load_concept_list(force_refresh=False) -> 全量(缓存感知)
  - query_by_code(code)                    -> 按代码精确匹配
  - query_by_name(name)                    -> 按名称精确匹配(已 strip)
  - filter_by_keyword(keyword)             -> 名称模糊匹配(大小写不敏感)
  - get_all()                              -> 全量(等价于 load_concept_list)

使用场景:
  - 板块筛选: 按概念名称/代码 定位题材
  - 题材轮动: 配合板块行情做轮动分析
  - 文案/标签: 为股票打"题材"标签的候选池

限制说明:
  - akshare 1.18.94 中 THS 概念仅 stock_board_concept_name_ths 可用;
    stock_board_concept_info_ths 返回的是板块汇总(项目/值)而非成分股,
    故本模块聚焦"名称+代码列表 + 查询筛选", 不含成分股明细。
  - 若需成分股(个股→概念归属), 须另接东方财富接口 stock_board_concept_cons_em,
    但那属于不同后端, 不在本 THS 模块职责内。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

import pandas as pd

ROOT = Path(__file__).parent.parent
OUT_DIR = ROOT / "data" / "concept"
CONCEPT_LIST_CSV = OUT_DIR / "ths_concept_list.csv"
CONCEPT_META_JSON = OUT_DIR / "ths_concept_list_meta.json"

CACHE_TTL_SECONDS = 7 * 24 * 3600  # 7 天


# --------------------------------------------------------------------------- #
# 重试与抓取
# --------------------------------------------------------------------------- #
def _retry(callable_fn: Callable[[], pd.DataFrame], name: str, max_attempts: int = 3) -> pd.DataFrame | None:
    """指数退避重试 akshare 接口; 等待 1s / 4s / 9s(项目规范)。

    返回 DataFrame; 全部失败返回 None(由调用方决定是否回退缓存)。
    """
    for attempt in range(1, max_attempts + 1):
        try:
            df = callable_fn()
            if df is None or len(df) == 0:
                print(f"  ⚠️ {name}: 返回空数据 (尝试 {attempt}/{max_attempts})")
                continue
            if "code" not in df.columns or "name" not in df.columns:
                print(f"  ⚠️ {name}: 缺少预期列 code/name (尝试 {attempt}/{max_attempts}); 实际列={list(df.columns)}")
                continue
            return df
        except Exception as e:  # noqa: BLE001 — 网络/解析异常统一重试
            wait = attempt ** 2  # 1, 4, 9
            print(f"  ❌ {name}: {type(e).__name__}: {e} (尝试 {attempt}/{max_attempts}, {wait}s 后重试)")
            if attempt < max_attempts:
                time.sleep(wait)
    return None


def _fetch_raw() -> pd.DataFrame:
    """调用 akshare 接口并做基础列重命名。akshare 懒加载以避免导入期重依赖。"""
    import akshare as ak  # 懒加载
    # 接口返回原始列 name / code; 重命名放到重试校验之后, 避免与 _retry 的列检查冲突
    return ak.stock_board_concept_name_ths()


# --------------------------------------------------------------------------- #
# 清洗与存储
# --------------------------------------------------------------------------- #
def clean_concept_list(df: pd.DataFrame) -> pd.DataFrame:
    """清洗概念列表: 去空格、去重、过滤空值、排序。"""
    out = df.copy()
    out["concept_code"] = out["concept_code"].astype(str).str.strip()
    out["concept_name"] = out["concept_name"].astype(str).str.strip()
    # 丢弃代码或名称为空的行
    out = out[out["concept_code"].str.len() > 0]
    out = out[out["concept_name"].str.len() > 0]
    out = out[out["concept_name"] != "nan"]
    # 去重: 同一代码保留首次出现; 同名不同代码保留全部(同花顺允许)
    out = out.drop_duplicates(subset=["concept_code"], keep="first")
    out = out.sort_values(["concept_name", "concept_code"]).reset_index(drop=True)
    return out[["concept_code", "concept_name"]]


def _write_meta(n_rows: int) -> None:
    meta = {
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "count": int(n_rows),
        "source": "ak.stock_board_concept_name_ths",
    }
    CONCEPT_META_JSON.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def save_concept_list(df: pd.DataFrame) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    df.to_csv(CONCEPT_LIST_CSV, index=False, encoding="utf-8-sig")
    _write_meta(len(df))
    print(f"  ✅ 概念列表 → {CONCEPT_LIST_CSV} ({len(df)} 条)")


# --------------------------------------------------------------------------- #
# 加载(缓存感知)
# --------------------------------------------------------------------------- #
def load_concept_list(force_refresh: bool = False) -> pd.DataFrame:
    """加载概念列表; 默认缓存有效则读本地, 否则重新抓取。"""
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    use_cache = False
    if CONCEPT_LIST_CSV.exists() and not force_refresh:
        age = time.time() - CONCEPT_LIST_CSV.stat().st_mtime
        if age < CACHE_TTL_SECONDS:
            use_cache = True
            print(f"✅ 缓存有效 (mtime {age / 3600:.0f}h 前), 直接读取")
        else:
            print(f"⚠️ 缓存过期 (mtime {age / 86400:.0f} 天前), 重新拉取")

    if use_cache:
        return pd.read_csv(CONCEPT_LIST_CSV, dtype={"concept_code": str}, encoding="utf-8-sig")

    print("[Phase 1] 抓取同花顺概念板块列表")
    df = _retry(_fetch_raw, "stock_board_concept_name_ths")
    if df is None:
        if CONCEPT_LIST_CSV.exists():
            print("  ⚠️ 抓取失败, 回退本地缓存")
            return pd.read_csv(CONCEPT_LIST_CSV, dtype={"concept_code": str}, encoding="utf-8-sig")
        sys.exit("❌ stock_board_concept_name_ths 全部失败且无本地缓存")

    clean = clean_concept_list(df.rename(columns={"name": "concept_name", "code": "concept_code"}))
    save_concept_list(clean)
    return clean


# --------------------------------------------------------------------------- #
# 查询 / 筛选 API
# --------------------------------------------------------------------------- #
def get_all() -> pd.DataFrame:
    """返回全量概念列表(缓存感知)。"""
    return load_concept_list()


def query_by_code(code: str) -> pd.DataFrame:
    """按概念代码精确匹配。"""
    df = load_concept_list()
    return df[df["concept_code"].astype(str).str.strip() == str(code).strip()].reset_index(drop=True)


def query_by_name(name: str) -> pd.DataFrame:
    """按概念名称精确匹配(已 strip)。"""
    df = load_concept_list()
    return df[df["concept_name"].str.strip() == str(name).strip()].reset_index(drop=True)


def filter_by_keyword(keyword: str) -> pd.DataFrame:
    """按名称模糊匹配(大小写不敏感, 部分包含即可)。"""
    df = load_concept_list()
    kw = str(keyword).strip().lower()
    mask = df["concept_name"].str.lower().str.contains(kw, na=False)
    return df[mask].reset_index(drop=True)


# --------------------------------------------------------------------------- #
# 输出格式化
# --------------------------------------------------------------------------- #
def _to_md(df: pd.DataFrame, title: str = "概念板块") -> str:
    if df.empty:
        return f"# {title}\n\n(无匹配结果)\n"
    lines = [f"# {title}", "", f"共 **{len(df)}** 条", "", "| 概念代码 | 概念名称 |", "|---:|---|"]
    for _, r in df.iterrows():
        lines.append(f"| {r['concept_code']} | {r['concept_name']} |")
    return "\n".join(lines)


def _emit(df: pd.DataFrame, fmt: str, title: str = "概念板块") -> None:
    if fmt == "json":
        print(df.to_json(force_ascii=False, orient="records", indent=2))
    elif fmt == "md":
        print(_to_md(df, title))
    else:  # csv
        print(df.to_csv(index=False, encoding="utf-8-sig"))


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main() -> None:
    parser = argparse.ArgumentParser(description="同花顺(THS)题材/概念板块数据加载与查询")
    sub = parser.add_subparsers(dest="cmd")

    p_fetch = sub.add_parser("fetch", help="抓取并存储概念列表")
    p_fetch.add_argument("--force", action="store_true", help="忽略缓存, 强制重新抓取")

    p_list = sub.add_parser("list", help="列出全部概念(可限制条数)")
    p_list.add_argument("--limit", type=int, default=0, help="最多显示条数(0=全部)")
    p_list.add_argument("--format", choices=["csv", "json", "md"], default="md")

    p_query = sub.add_parser("query", help="按名称/代码精确查询")
    p_query.add_argument("--name", type=str, default=None, help="概念名称")
    p_query.add_argument("--code", type=str, default=None, help="概念代码")
    p_query.add_argument("--format", choices=["csv", "json", "md"], default="md")

    p_search = sub.add_parser("search", help="按关键词模糊筛选名称")
    p_search.add_argument("--keyword", type=str, required=True, help="名称包含的关键词")
    p_search.add_argument("--format", choices=["csv", "json", "md"], default="md")

    args = parser.parse_args()
    cmd = args.cmd or "fetch"

    if cmd == "fetch":
        df = load_concept_list(force_refresh=getattr(args, "force", False))
        print(f"\n=== 完成: 共 {len(df)} 个概念板块 ===")

    elif cmd == "list":
        df = load_concept_list()
        if args.limit and args.limit > 0:
            df = df.head(args.limit)
        _emit(df, args.format, title="同花顺概念板块列表")

    elif cmd == "query":
        if args.name:
            df = query_by_name(args.name)
            title = f"查询名称='{args.name}'"
        elif args.code:
            df = query_by_code(args.code)
            title = f"查询代码='{args.code}'"
        else:
            parser.error("query 需指定 --name 或 --code")
            return
        _emit(df, args.format, title=title)

    elif cmd == "search":
        df = filter_by_keyword(args.keyword)
        _emit(df, args.format, title=f"模糊匹配关键词='{args.keyword}'")


if __name__ == "__main__":
    main()
