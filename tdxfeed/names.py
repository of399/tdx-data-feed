"""全 A 名单与 ST 判定：scan / predict / daban 的唯一来源。"""
from __future__ import annotations

import json

from .paths import universe_path

_CACHE: dict[str, str] | None = None


def load_universe() -> dict[str, str]:
    """code(6 位) -> name。文件缺失返回空字典，由调用方决定是否致命。"""
    global _CACHE
    if _CACHE is None:
        fp = universe_path()
        _CACHE = json.loads(fp.read_text(encoding="utf-8")) if fp.exists() else {}
    return _CACHE


def is_st_name(name: str) -> bool:
    return "ST" in str(name).replace(" ", "").upper()


def name_of(code: str) -> str:
    c = str(code).lower().strip()
    for p in ("sh", "sz", "bj"):
        if c.startswith(p):
            c = c[2:]
            break
    return load_universe().get(c.zfill(6), "")
