"""全 A 名单与 ST 判定：scan / predict / daban 的唯一来源。

**唯一入口**（P1-9 名字/池单源，2026-11-02）：

- `load_universe(path=...)`：唯一的股票池**加载**实现，返回 `{代码: 名称}`。
- `universe_codes(path=...)`：唯一的股票池**代码枚举**实现，返回代码列表。

全库任何模块都不得再自己读 `data/all_a_stocks.json`——守卫
`tests/test_universe_path_single_source.py` 同时断言「路径单一来源」与「实现数量 = 1」
（前者管住路径拼法，后者管住读取行为与加载函数定义点）。

口径说明（为什么选这套语义）：

- `load_universe` 返回**文件原样**的 `{代码: 名称}`：不 zfill、不去空格、不改键。
  实测名单文件 5221 条，键全是 6 位数字、名称有 36 条带排版空格（`万  科Ａ`）。
  「原样」让 8 份旧实现里 6 份的行为逐字节不变；只有 `tdxfeed/daban/scanner.py`
  曾经额外去空格（研究脚本的显示口径），收编后随统一口径一并去掉。
- `universe_codes` 返回**零填充到 6 位**的代码（与 predict / 回测等调用方历史口径一致），
  可按 `prefixes` 过滤前缀；名单缺失或为空时可用 `fallback=True` 回退扫日线目录。
- ST 判定与名称查询沿用既有口径，未改。
"""
from __future__ import annotations

import json
from pathlib import Path

from . import paths


def load_universe(path: str | Path | None = None) -> dict[str, str]:
    """唯一的股票池加载实现：返回 `{代码: 名称}`。

    **刻意不做缓存**：名单文件由 `scripts/download_universe.py` 每日更新，而 MCP server
    是长驻进程——缓存会让名称映射在进程存活期内一直是旧的（ST 改名当天就穿帮）。
    143 KB 的 JSON 解析约 1 ms，每次调用重读的成本可以忽略。

    Args:
        path: 名单文件路径；`None` 表示用 `paths.universe_path()`。

    Returns:
        `{代码: 名称}` 映射（文件内容原样）。文件缺失返回空字典——是否致命由调用方决定。
    """
    return _read_universe(Path(path) if path is not None else paths.universe_path())


def universe_codes(path: str | Path | None = None, *,
                   prefixes: tuple[str, ...] | None = None,
                   fallback: bool = False) -> list[str]:
    """唯一的股票池代码枚举实现。

    Args:
        path: 名单文件路径；`None` 表示默认名单。
        prefixes: 只保留这些 2 位前缀（如 `("60", "68", "00", "30")`）。
        fallback: 名单缺失或解析不出代码时，回退扫日线 parquet 目录（降级模式，名称为空）。

    Returns:
        代码列表（零填充到 6 位，顺序 = 名单文件里的顺序）。
    """
    mapping = load_universe(path)
    codes = [str(c).zfill(6) for c in mapping if str(c).isdigit()]
    if not codes and fallback:
        codes = _codes_from_daily()
    if prefixes:
        codes = [c for c in codes if c[:2] in prefixes]
    return codes


def _read_universe(fp: Path) -> dict[str, str]:
    """把名单文件解析成 `{代码: 名称}`（唯一的读 + 解析实现）。

    兼容三种历史结构：`{code: name}`（实际形态）/ `{"codes"|"stocks": [...]}` / `[code, ...]`。
    """
    if not fp.exists():
        return {}
    raw = json.loads(fp.read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        listed = raw.get("codes") or raw.get("stocks")
        if listed:
            return {str(c).zfill(6): "" for c in listed}
        return {str(k): str(v) for k, v in raw.items()}
    if isinstance(raw, list):
        return {str(c).zfill(6): "" for c in raw}
    return {}


def _codes_from_daily() -> list[str]:
    """降级来源：扫日线 parquet 目录得到代码列表（名单文件缺失时的兜底）。"""
    out: set[str] = set()
    for fp in paths.daily_files():
        stem = fp.stem
        out.add(stem[2:] if stem[:2] in ("sh", "sz", "bj") else stem)
    return sorted(out)


def is_st_name(name: str) -> bool:
    """名称是否 ST / *ST（空格是排版噪声，先去掉）。"""
    return "ST" in str(name).replace(" ", "").upper()


def name_of(code: str) -> str:
    """代码 → 名称；不在池里返回空串。"""
    c = str(code).lower().strip()
    for p in ("sh", "sz", "bj"):
        if c.startswith(p):
            c = c[2:]
            break
    return load_universe().get(c.zfill(6), "")
