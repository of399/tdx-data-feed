"""daily 表列举唯一走 paths.daily_files()；三份 resolve_parquet 委托给 paths.daily_path。"""
from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

from tdxfeed import paths

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"

pytestmark = pytest.mark.skipif(
    not paths.daily_dir().exists(), reason="需要 data/parquet/daily（CI 精简集没有 data/）"
)

# 入库文件：干净 checkout 里也必须过。
LISTING_SITES = [
    "scripts/factor_engine.py",
    "scripts/check_schema.py",
    "tdxfeed/incremental.py",
    "tdxfeed/precompute/indicators.py",
    "v5/scripts/v4_batch_analysis.py",
    "quant/multifactor/engine.py",
]

# 只在本机存在的文件：不入库也必须过（不入库不等于可以绕过约定）。
LOCAL_ONLY_SITES = ["train/scripts/build_v8_dataset.py"]  # train/ 在 .gitignore，CI 无此文件


def _load(rel: str, name: str):
    """按路径加载脚本：scripts/ 不在包里，v5/scripts 需要能 `from _paths import ...`。"""
    for extra in (SCRIPTS, ROOT / "v5" / "scripts"):
        if str(extra) not in sys.path:
            sys.path.insert(0, str(extra))
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod          # dataclass / 内部自引用需要先登记
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def resolvers():
    return {
        "factor_engine": _load("scripts/factor_engine.py", "probe_factor_engine"),
        "quant_factors": _load("quant/multifactor/factors.py", "probe_mf_factors"),
        "sim_daily_compare": _load("scripts/sim_daily_compare.py", "probe_sim_daily_compare"),
    }


@pytest.mark.parametrize("name", ["factor_engine", "quant_factors", "sim_daily_compare"])
@pytest.mark.parametrize("code", ["sh600000", "sz000001", "600519"])
def test_resolve_agrees_with_authority(resolvers, name, code):
    fp = resolvers[name].resolve_parquet(code)
    assert fp == paths.daily_path(code)
    assert fp.exists()


@pytest.mark.parametrize("name", ["factor_engine", "quant_factors", "sim_daily_compare"])
def test_resolve_raises_on_unknown(resolvers, name):
    with pytest.raises(FileNotFoundError):
        resolvers[name].resolve_parquet("sh999999")


def test_sim_daily_compare_bare_code_picks_stock_not_index(resolvers):
    """裸 000001 是平安银行（sz）；上证指数是同号的 sh000001，旧实现返回后者。"""
    assert resolvers["sim_daily_compare"].resolve_parquet("000001").name == "sz000001.parquet"


def test_v4_batch_analysis_lists_canonical_only():
    mod = _load("v5/scripts/v4_batch_analysis.py", "probe_v4_batch_analysis")
    stems = mod.get_all_stocks()
    assert len(stems) == len(paths.daily_files())
    assert not [s for s in stems if s.endswith(".v1.bak")]


def test_precompute_all_iterates_canonical_codes(monkeypatch):
    from tdxfeed.precompute import indicators

    seen: list[str] = []
    monkeypatch.setattr(indicators, "process_one", lambda code, force=False: seen.append(code))
    monkeypatch.setattr(sys, "argv", ["indicators", "--all"])
    indicators.main()
    assert len(seen) == len(paths.daily_files())
    assert not [c for c in seen if c.endswith(".v1.bak")]


@pytest.mark.parametrize("rel", LISTING_SITES)
def test_listing_goes_through_daily_files(rel):
    src = (ROOT / rel).read_text(encoding="utf-8")
    assert "daily_files(" in src, rel
    assert not re.search(r"\.glob\(\s*['\"]\*\.parquet['\"]\s*\)", src), rel


@pytest.mark.parametrize("rel", LOCAL_ONLY_SITES)
def test_listing_goes_through_daily_files_local_only(rel):
    src_path = ROOT / rel
    if not src_path.exists():
        pytest.skip(f"{rel} 不入库（.gitignore: train/），本机才有")
    src = src_path.read_text(encoding="utf-8")
    assert "daily_files(" in src, rel
    assert not re.search(r"\.glob\(\s*['\"]\*\.parquet['\"]\s*\)", src), rel
