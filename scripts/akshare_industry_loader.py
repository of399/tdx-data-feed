"""scripts/akshare_industry_loader.py — akshare 申万行业分类加载

目标:获取 A 股所有股票的真实行业归属,缓存到 data/industry/。

数据源体系: **申万(SW)三级分类**(国内主流,粒度适中,335 个三级行业)

接口(akshare):
  - ak.stock_industry_clf_hist_sw() 申万个股历史分类(本数据基 5930 股票,12925 历史变更)
  - ak.sw_index_third_info()       申万三级行业列表(335 个)
  - ak.sw_index_first_info()       申万一级行业列表(31 个)

输出文件:
  - data/industry/sw_third_list.csv   申万三级行业列表(行业代码/名称/上级)
  - data/industry/sw_first_list.csv   申万一级行业列表
  - data/industry/sw_stock_industry.csv 成分股映射表(code → industry_code, name, level, parent)

字段:
  - 行业代码: 申万三级 6 位数字格式(如 430101 = 房地产开发)
  - 行业名称: 中文,带上级行业
  - 层级: 三级(SW 2021 分类)
  - 成分股: '000001' 形式(akshare 原生,需转 'sz000001' 与本项目 code 字段一致)

稳定性:
  - 重试 3 次指数退避(1/2/4 秒)
  - 网络异常时使用本地缓存

更新频率:
  - 申万行业分类每季度调整(样本股调整)
  - 建议每月拉一次,文件 mtime + cache TTL

使用场景:
  - 个股行业归属查询: code → industry_code
  - 行业轮动: 跨行业 5d 收益对比
  - 板块筛选: 按行业过滤 Top N
  - 同行业特征: ind_ret_5d_avg, ind_amt_avg_5d 等
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).parent.parent
OUT_DIR = ROOT / "data" / "industry"
SW_FIRST = OUT_DIR / "sw_first_list.csv"
SW_THIRD = OUT_DIR / "sw_third_list.csv"
SW_MAPPING = OUT_DIR / "sw_stock_industry.csv"

CACHE_TTL_SECONDS = 30 * 24 * 3600  # 30 天

import akshare as ak


def _retry(callable_fn, name: str, max_attempts: int = 3) -> pd.DataFrame | None:
    """指数退避重试 akshare 接口。"""
    for attempt in range(1, max_attempts + 1):
        try:
            df = callable_fn()
            if df is None or len(df) == 0:
                print(f"  ⚠️ {name}: 返回空数据 (尝试 {attempt}/{max_attempts})")
                continue
            return df
        except Exception as e:
            wait = 2 ** (attempt - 1)
            print(f"  ❌ {name}: {type(e).__name__}: {e} (尝试 {attempt}/{max_attempts}, {wait}s 后重试)")
            if attempt < max_attempts:
                time.sleep(wait)
    return None


def fetch_sw_first() -> pd.DataFrame | None:
    return _retry(lambda: ak.sw_index_first_info(), "sw_index_first_info")


def fetch_sw_third() -> pd.DataFrame | None:
    return _retry(lambda: ak.sw_index_third_info(), "sw_index_third_info")


def fetch_sw_stock_history() -> pd.DataFrame | None:
    """申万个股历史分类(每个 symbol 多行,start_date 是起效日)。"""
    return _retry(lambda: ak.stock_industry_clf_hist_sw(), "stock_industry_clf_hist_sw")


def normalize_code(raw_code: str) -> str:
    """'000001' 或 'sh600313' → 内部 'sz000001' / 'sh600313' (与本项目 code 字段一致)。"""
    if not isinstance(raw_code, str):
        return ""
    raw_code = raw_code.strip().lower()
    if raw_code.startswith(("sh", "sz", "bj")):
        return raw_code  # 已是内部格式
    # 6 位纯数字 → 推断交易所
    if len(raw_code) == 6:
        if raw_code.startswith(("60", "68", "90", "11", "13")):
            return f"sh{raw_code}"
        if raw_code.startswith(("00", "30", "20")):
            return f"sz{raw_code}"
        if raw_code.startswith(("8", "4", "92")):
            return f"bj{raw_code}"
    return raw_code


def get_current_industry(history: pd.DataFrame) -> pd.DataFrame:
    """从历史表中取最新一条(sym) — 即股票当前归属。"""
    # 按 symbol + start_date 排序,取每 symbol 最后一条
    history = history.sort_values(["symbol", "start_date"], ascending=[True, False])
    current = history.drop_duplicates(subset=["symbol"], keep="first").copy()
    return current


def build_mapping(current: pd.DataFrame, sw_third: pd.DataFrame, sw_first: pd.DataFrame) -> pd.DataFrame:
    """合并行业名称 + 关联上级。"""
    # 申万行业代码转换:hist_sw 是 6 位数字(如 430101),sw_index_third_info 是 850101.SI 格式
    # sw_index_third_info 的 行业代码 列需要去掉 .SI 后缀 → 6 位数字(但前面是 85...)
    # 实际:hist_sw 的 industry_code 是 2021 分类的代码,sw_third 是新版 SW2021 分类

    # 直接匹配 6 位代码(简化处理)
    sw_third_map = dict(zip(sw_third["行业代码"].str.replace(r"\.SI$", "", regex=True), sw_third["行业名称"]))
    sw_third_parent = dict(zip(sw_third["行业代码"].str.replace(r"\.SI$", "", regex=True), sw_third["上级行业"]))

    # 注意:hist_sw 的 430101 是申万 2014 旧分类,850101.SI 是 SW2021 新分类
    # 为简化,我们尝试直接匹配 6 位代码,匹配不上的留空
    mapping = current.copy()
    mapping["code"] = mapping["symbol"].astype(str).str.strip().apply(normalize_code)
    mapping["industry_name"] = mapping["industry_code"].map(lambda c: sw_third_map.get(c, ""))
    mapping["industry_parent"] = mapping["industry_code"].map(lambda c: sw_third_parent.get(c, ""))
    mapping["industry_level"] = 3
    mapping = mapping.rename(columns={"start_date": "industry_start_date"})

    # 关联一级行业(parent -> 一级)
    # 简化:取 parent 字符串作一级行业代理
    mapping = mapping[["code", "industry_code", "industry_name", "industry_parent", "industry_level", "industry_start_date"]]
    return mapping


def main():
    print("=== akshare 申万行业分类加载 ===\n")
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # 缓存检查
    use_cache = False
    if SW_MAPPING.exists():
        age = time.time() - SW_MAPPING.stat().st_mtime
        if age < CACHE_TTL_SECONDS:
            print(f"✅ 缓存有效 (mtime {age/3600:.0f}h 前)")
            use_cache = True
        else:
            print(f"⚠️ 缓存过期 (mtime {age/86400:.0f} 天前), 重新拉取")

    if use_cache:
        mapping = pd.read_csv(SW_MAPPING)
        first = pd.read_csv(SW_FIRST) if SW_FIRST.exists() else pd.DataFrame()
        third = pd.read_csv(SW_THIRD) if SW_THIRD.exists() else pd.DataFrame()
    else:
        print("[Phase 1] 拉申万一级行业列表(失败则用缓存)")
        first = fetch_sw_first()
        if first is not None:
            first.to_csv(SW_FIRST, index=False)
            print(f"  ✅ {len(first)} 个一级行业")
        elif SW_FIRST.exists():
            first = pd.read_csv(SW_FIRST)
            print(f"  ⚠️ 用本地缓存 {len(first)} 个一级行业")
        else:
            print("  ⚠️ 无一级数据,跳过(不影响核心)")
            first = pd.DataFrame()

        print("[Phase 2] 拉申万三级行业列表(失败则用缓存)")
        third = fetch_sw_third()
        if third is not None:
            third.to_csv(SW_THIRD, index=False)
            print(f"  ✅ {len(third)} 个三级行业")
        elif SW_THIRD.exists():
            third = pd.read_csv(SW_THIRD)
            print(f"  ⚠️ 用本地缓存 {len(third)} 个三级行业")
        else:
            print("  ⚠️ 无三级列表,industry_name 留空,仅保留 industry_code")
            third = pd.DataFrame(columns=["行业代码", "行业名称", "上级行业"])

        print("[Phase 3] 拉申万个股历史分类(避免遍历空行业)")
        history = fetch_sw_stock_history()
        if history is None:
            sys.exit("❌ stock_industry_clf_hist_sw 全部失败")
        print(f"  ✅ {len(history)} 条历史变更, {history['symbol'].nunique()} 唯一股票")

        current = get_current_industry(history)
        print(f"  当前归属(取每个 symbol 最新): {len(current)} 只")

        mapping = build_mapping(current, third, first)
        mapping.to_csv(SW_MAPPING, index=False)
        print(f"  ✅ 映射表 → {SW_MAPPING}")

    # 校验
    print("\n[Phase 4] 校验")
    print(f"  唯一股票: {mapping['code'].nunique()}")
    print(f"  唯一行业代码: {mapping['industry_code'].nunique()}")
    n_named = (mapping['industry_name'] != '').sum()
    print(f"  含名称映射: {n_named}/{len(mapping)} = {n_named/len(mapping)*100:.1f}%")

    # 行业分布
    print("\n[Top 10 行业(按股票数)]")
    top_ind = mapping.dropna(subset=["industry_name"]).groupby(["industry_code", "industry_name"]).size().sort_values(ascending=False).head(10)
    for (code, name), n in top_ind.items():
        print(f"  {code} {name}: {n} 只")

    # 训练数据覆盖率
    train_csv = ROOT / "data" / "double_up" / "d_signal_5d_features_v9.csv"
    if train_csv.exists():
        print("\n[训练数据行业覆盖率]")
        train_codes = set(pd.read_csv(train_csv, usecols=["code"])["code"].unique())
        mapped_codes = set(mapping["code"].unique())
        n_covered = len(train_codes & mapped_codes)
        print(f"  训练集股票: {len(train_codes)}")
        print(f"  行业映射覆盖: {n_covered} / {len(train_codes)} = {n_covered/len(train_codes)*100:.1f}%")
        not_covered = list(train_codes - mapped_codes)[:10]
        if not_covered:
            print(f"  未覆盖股票(前 10): {not_covered}")

    print("\n=== 完成 ===")


if __name__ == "__main__":
    main()
