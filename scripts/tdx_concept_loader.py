"""通达信(tdx)题材/行业板块 → 个股归属加载器。

独立于 THS 模块(`akshare_concept_loader.py`)的**另一后端**：解析通达信
标准板块文件，构建「个股 → 概念」的归属关系。

标准板块文件(位于通达信安装目录 `T0002/block/`)：
    block_gn.dat  概念板块
    block_hy.dat  行业板块
    block_fg.dat  风格板块
    block_zs.dat  指数板块
    block_wg.dat  自定义板块

数据来源约束：
    本机两个通达信实例(tdxcfv)当前**未提供**标准概念板块文件——
    `hq_cache/*block.dat` 为专有格式，pytdx 无法解析。因此需用户将
    `block_gn.dat` 等放置到 `$TDX_BLOCK_DIR` 或自动探测目录后，本模块
    方能产出真实数据。放置前 `build_membership()` 返回空并给出明确提示。

与 THS 模块的关系：
    THS(`akshare_concept_loader.py`)仅提供「概念名称 + 代码列表」(无成分股)；
    本模块提供「个股 → 概念」成分归属，二者互补、互不依赖。

用法：
    python -m scripts.tdx_concept_loader fetch
    python -m scripts.tdx_concept_loader list  [--type gn]
    python -m scripts.tdx_concept_loader query --code 600519
    python -m scripts.tdx_concept_loader search --concept 机器人
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import pandas as pd

# 板块类型 -> 中文名
BLOCK_TYPES: dict[str, str] = {
    "gn": "概念",
    "hy": "行业",
    "fg": "风格",
    "zs": "指数",
    "wg": "自定义",
}

# 自动探测目录(存在 block_gn.dat 即命中)
_CANDIDATE_DIRS: list[str | None] = [
    os.environ.get("TDX_BLOCK_DIR"),
    "/home/jiuben/.local/share/tdxcfv/drive_c/tc/T0002/block",
    "/opt/apps/com.tdx.tdxcfv/files/tc/T0002/block",
    os.path.expanduser("~/.wine/drive_c/tdx/T0002/block"),
    "C:/new_tdx/T0002/block",
]

DATA_DIR = Path("/home/jiuben/tdx-data-feed/data/concept")

logger = logging.getLogger(__name__)


def _normalize_code(raw: str) -> str:
    """通达信板块内 code 为 7 字节 utf-8，去除空字符后可能是
    ``600000`` 或带市场前缀(``sh600000`` / ``1A0001``)，统一规整为 6 位代码。
    """
    if not raw:
        return ""
    s = raw.strip().rstrip("\x00").strip()
    if len(s) > 6 and s[:2] in ("sh", "sz", "bj"):
        s = s[2:]
    s = "".join(ch for ch in s if ch.isdigit())
    return s[-6:] if len(s) >= 6 else s


def discover_block_dir() -> Path | None:
    """探测含标准 ``block_gn.dat`` 的目录。"""
    for d in _CANDIDATE_DIRS:
        if not d:
            continue
        p = Path(d)
        if (p / "block_gn.dat").exists():
            return p
    return None


def _read_block_file(path: Path, btype: str) -> pd.DataFrame:
    """用 pytdx ``BlockReader`` 解析单个标准板块文件 → 长表 DataFrame。

    返回列：``code``(6 位个股代码) / ``concept_name``(板块中文名) / ``concept_type``。
    """
    from pytdx.reader import BlockReader
    from pytdx.reader.block_reader import BlockReader_TYPE_FLAT

    reader = BlockReader()
    df = reader.get_df(str(path), BlockReader_TYPE_FLAT)
    if df.empty:
        return df
    df = df.rename(columns={"blockname": "concept_name", "code": "raw_code"})
    df["code"] = df["raw_code"].map(_normalize_code)
    df["concept_type"] = BLOCK_TYPES.get(btype, btype)
    df = df.loc[:, ["code", "concept_name", "concept_type"]].dropna(subset=["code"])
    df = df[df["code"] != ""].drop_duplicates()
    return df


def build_membership() -> dict[str, pd.DataFrame]:
    """扫描所有可用板块类型，返回 ``{btype: DataFrame}``。"""
    block_dir = discover_block_dir()
    if block_dir is None:
        logger.error(
            "未找到标准通达信板块文件(block_gn.dat 等)。请将通达信安装目录下的 "
            "T0002/block/block_gn.dat 等文件放置到 $TDX_BLOCK_DIR 或自动探测目录后重试。"
        )
        return {}
    out: dict[str, pd.DataFrame] = {}
    for btype in BLOCK_TYPES:
        fp = block_dir / f"block_{btype}.dat"
        if not fp.exists():
            logger.info("板块文件缺失，跳过: %s", fp.name)
            continue
        try:
            df = _read_block_file(fp, btype)
            out[btype] = df
            logger.info("解析 %s: %d 条 个股→概念 记录", fp.name, len(df))
        except Exception as exc:  # 具体异常，避免裸 except
            logger.error("解析 %s 失败: %s", fp.name, exc)
    return out


def save_membership(membership: dict[str, pd.DataFrame]) -> None:
    """将归属表落盘为 ``data/concept/tdx_{type}.csv``(utf-8-sig) + meta。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    meta = {
        "source": "tdx_block",
        "available_types": sorted(membership),
        "counts": {btype: len(df) for btype, df in membership.items()},
    }
    for btype, df in membership.items():
        out = DATA_DIR / f"tdx_{btype}.csv"
        df.to_csv(out, index=False, encoding="utf-8-sig")
    (DATA_DIR / "tdx_concept_meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    logger.info("已写出 %d 类板块归属到 %s", len(membership), DATA_DIR)


def load_membership(btype: str | None = None) -> pd.DataFrame:
    """读取已存储的归属表；``btype`` 指定类型，默认合并全部。"""
    if not DATA_DIR.exists():
        return pd.DataFrame(columns=["code", "concept_name", "concept_type"])
    frames = []
    types = [btype] if btype else list(BLOCK_TYPES)
    for t in types:
        fp = DATA_DIR / f"tdx_{t}.csv"
        if fp.exists():
            frames.append(pd.read_csv(fp, dtype=str, keep_default_na=False))
    if not frames:
        return pd.DataFrame(columns=["code", "concept_name", "concept_type"])
    return pd.concat(frames, ignore_index=True)


def get_concepts_of_stock(code: str, btype: str | None = None) -> pd.DataFrame:
    """查询某股所属的所有概念/板块。"""
    df = load_membership(btype)
    return df[df["code"] == _normalize_code(code)]


def get_stocks_of_concept(name: str, btype: str | None = None) -> pd.DataFrame:
    """按概念名(模糊、大小写不敏感)筛选成分股。"""
    df = load_membership(btype)
    return df[df["concept_name"].str.contains(name, case=False, na=False)]


def list_concepts(btype: str | None = None) -> pd.DataFrame:
    """列出全部板块及其成分股数量(降序)。"""
    df = load_membership(btype)
    if df.empty:
        return df
    grouped = (
        df.groupby(["concept_type", "concept_name"], as_index=False)
        .size()
        .rename(columns={"size": "stock_count"})
    )
    return grouped.sort_values(
        ["concept_type", "stock_count"], ascending=[True, False]
    ).reset_index(drop=True)


def main(argv: list[str] | None = None) -> int:
    """CLI 入口。"""
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    p = argparse.ArgumentParser(description="通达信 个股→概念 归属加载器")
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("fetch", help="扫描并解析标准板块文件，写 data/concept/tdx_*.csv")
    lp = sub.add_parser("list", help="列出概念(板块)及其成分股数")
    lp.add_argument("--type", dest="btype", default=None, help="板块类型 gn/hy/fg/zs/wg")
    qp = sub.add_parser("query", help="查某股所属概念")
    qp.add_argument("--code", required=True)
    sp = sub.add_parser("search", help="按概念名筛选成分股")
    sp.add_argument("--concept", required=True)
    sp.add_argument("--type", dest="btype", default=None)

    args = p.parse_args(argv)
    if args.cmd == "fetch":
        m = build_membership()
        if not m:
            print("未找到标准板块文件，见上方日志。放置 block_gn.dat 后重试。")
            return 1
        save_membership(m)
        print(f"已写出 {len(m)} 类板块归属: {sorted(m)}")
        return 0
    if args.cmd == "list":
        print(list_concepts(args.btype).to_string(index=False))
    elif args.cmd == "query":
        print(get_concepts_of_stock(args.code).to_string(index=False))
    elif args.cmd == "search":
        print(get_stocks_of_concept(args.concept, args.btype).to_string(index=False))
    else:
        p.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
