"""原子写助手：先写同目录临时文件，再 `os.replace` 落位。

为什么需要
----------
非原子写（`path.write_text()` / `df.to_csv(path)`）在崩溃或并发下会让读者
看到**半截文件**：写了一半就断电、或读者在写入过程中打开文件。

M-4 语义守卫（SEM001）把这类写法列为违规；本模块是统一的修复出口，
避免每个脚本各写一遍 tmp+replace。

语义保证
--------
1. 落位是原子的（同文件系统内 `os.replace` 是 rename(2)）；
2. 写入失败时**原文件保持不变**（临时文件被清理）；
3. 临时文件与目标同目录 → 同一文件系统 → rename 不会跨设备失败；
4. 可选 `backup=True`：落位前把旧内容留一份带时间戳的 `.bak-YYYYmmdd_HHMMSS`。

用法
----
    from tdxfeed.atomic import atomic_write_text, atomic_write_frame
    atomic_write_text(POSITIONS_FILE, json.dumps(pos, ensure_ascii=False, indent=2))
    atomic_write_frame(df, JOURNAL_FILE, encoding="utf-8-sig")
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

__all__ = [
    "atomic_write_bytes",
    "atomic_write_frame",
    "atomic_write_path",
    "atomic_write_text",
    "tmp_sibling",
]


def tmp_sibling(path: Path | str) -> Path:
    """目标文件同目录下的临时文件路径（含 pid，避免同进程并发写互相覆盖）。"""
    p = Path(path)
    return p.with_name(f"{p.name}.{os.getpid()}.tmp")


def _write_backup(path: Path) -> Path:
    """把旧内容留一份带时间戳的备份（备份本身也走原子落位）。"""
    bak = path.with_name(f"{path.name}.bak-{time.strftime('%Y%m%d_%H%M%S')}")
    data = path.read_bytes()
    tmp = tmp_sibling(bak)
    try:
        tmp.write_bytes(data)
        os.replace(tmp, bak)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
    return bak


def atomic_write_text(path: Path | str, text: str, *, encoding: str = "utf-8",
                      backup: bool = False) -> Path:
    """原子写文本。返回落位后的路径。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if backup and p.exists():
        _write_backup(p)
    tmp = tmp_sibling(p)
    try:
        tmp.write_text(text, encoding=encoding)
        os.replace(tmp, p)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
    return p


def atomic_write_bytes(path: Path | str, data: bytes, *, backup: bool = False) -> Path:
    """原子写二进制。返回落位后的路径。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if backup and p.exists():
        _write_backup(p)
    tmp = tmp_sibling(p)
    try:
        tmp.write_bytes(data)
        os.replace(tmp, p)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
    return p


def atomic_write_frame(df: Any, path: Path | str, *, encoding: str = "utf-8-sig",
                       index: bool = False, backup: bool = False,
                       **kwargs: Any) -> Path:
    """原子写 DataFrame 为 CSV（pandas / polars 的 to_csv）。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if backup and p.exists():
        _write_backup(p)
    tmp = tmp_sibling(p)
    try:
        df.to_csv(tmp, index=index, encoding=encoding, **kwargs)
        os.replace(tmp, p)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
    return p


def atomic_write_path(tmp: Path | str, dst: Path | str, *, backup: bool = False) -> Path:
    """把已写好的临时文件原子落位到目标（用于第三方库只能写文件路径的场景）。

    例：LightGBM `booster.save_model(path)` 只能写文件 → 先存到临时路径，
    再用本函数落位，读者永远看不到半截模型。

    `backup=True` 时，落位前把旧目标留一份带时间戳的 `.bak-YYYYmmdd_HHMMSS`。
    """
    src = Path(tmp)
    target = Path(dst)
    target.parent.mkdir(parents=True, exist_ok=True)
    if backup and target.exists():
        _write_backup(target)
    os.replace(src, target)
    return target
