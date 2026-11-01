"""find_double_up_c 的标的枚举必须走 paths.daily_files() 单源。

旧实现 `PARQUET.glob('*.parquet')` + `.bak` 过滤会把中断残留
`sh600600.amountfix.tmp.parquet` 当成一只股票去扫（它不含 `.bak`）。
规范枚举只认 前缀+6位.parquet，排除迁移备份与残留（AGENTS 数据路径约定）。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

NEEDS_DATA = False

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))


def _load():
    spec = importlib.util.spec_from_file_location(
        "find_double_up_c_enum", ROOT / "scripts" / "find_double_up_c.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_stock_files_excludes_residue_and_backup(tmp_path, monkeypatch):
    from tdxfeed import paths
    monkeypatch.setattr(paths, "DATA_ROOT", tmp_path)
    daily = paths.daily_dir()
    daily.mkdir(parents=True)
    for name in ("sh600000.parquet", "sz000001.parquet",
                 "sh600600.amountfix.tmp.parquet", "sh600001.v1.bak.parquet"):
        (daily / name).write_bytes(b"")

    mod = _load()
    got = [p.name for p in mod.stock_files()]
    assert got == ["sh600000.parquet", "sz000001.parquet"], got


def test_main_no_longer_uses_the_bak_glob():
    source = (ROOT / "scripts" / "find_double_up_c.py").read_text(encoding="utf-8")
    assert "'.bak' not in p.name" not in source, "枚举仍用 .bak 过滤，会漏掉中断残留"
    assert "daily_files" in source, "枚举应委托 paths.daily_files() 单源"
