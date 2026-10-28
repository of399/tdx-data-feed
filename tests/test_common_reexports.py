"""scripts/_common.py 是路径访问器的再导出桥：名字必须真存在、已声明、且是同一对象。"""
from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

EXPECTED = {
    "DATA_ROOT", "daily_dir", "daily_files", "daily_path",
    "adj_factor_dir", "adj_factor_files", "hfq_factor_dir",
    "minute_dir", "parquet_root", "derived_dir", "xdxr_path", "read_path",
}


def _load_common():
    spec = importlib.util.spec_from_file_location(
        "scripts_common_under_test", ROOT / "scripts/_common.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_common_reexports_every_path_accessor():
    common = _load_common()
    assert sorted(n for n in EXPECTED if not hasattr(common, n)) == []


def test_reexports_are_declared_in_all():
    common = _load_common()
    assert sorted(EXPECTED - set(common.__all__)) == []


def test_reexports_are_the_authority_objects():
    common = _load_common()
    from tdxfeed import paths
    for name in sorted(EXPECTED):
        assert getattr(common, name) is getattr(paths, name), name
