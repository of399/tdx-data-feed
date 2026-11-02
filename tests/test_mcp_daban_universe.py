"""MCP daban 股票池回退枚举也要走 paths.daily_files() 单源。

`backtest._pool()` 在股票池文件缺失时回退扫 parquet；旧 `.bak` 过滤会把
`sh600999.amountfix.tmp.parquet` 这类残留当成一只股票（600999 并非真实标的）。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

NEEDS_DATA = False

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load():
    spec = importlib.util.spec_from_file_location(
        "mcp_backtest_universe", ROOT / "mcp" / "servers" / "backtest.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_universe_fallback_excludes_residue(tmp_path, monkeypatch):
    from tdxfeed import paths
    monkeypatch.setattr(paths, "DATA_ROOT", tmp_path)
    daily = paths.daily_dir()
    daily.mkdir(parents=True)
    (daily / "sh600000.parquet").write_bytes(b"")
    (daily / "sh600999.amountfix.tmp.parquet").write_bytes(b"")  # 残留，非真实标的

    mod = _load()
    monkeypatch.setattr(mod, "DAILY_DIR", daily)

    names = mod._pool()
    assert "600000" in names
    # 残留会被旧枚举收成 malformed 键 "600999.amountfix.tmp"；规范枚举后只剩 6 位代码。
    assert all(len(c) == 6 and c.isdigit() for c in names), f"非规范代码混入: {sorted(names)}"
    assert not any("600999" in c for c in names), f"残留被当成股票: {sorted(names)}"
