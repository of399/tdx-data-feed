"""CI 没有本地 `data/` 时，依赖本地行情/产物的测试模块应被收集期跳过，而不是让整片变红。

守卫对象是根 `conftest.py` 的 `_needs_data` / `pytest_collection_modifyitems`。
真跑一次的验证方式：`TDX_TEST_DATA_ROOT=/nonexistent venv/bin/pytest tests/ -q`，
此时只有引用 `data/` 或绝对路径的模块被 skip，纯模块照跑。
"""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
DATA_DEPENDENT = ROOT / "tests" / "test_adj_factor_history.py"
PURE = ROOT / "tests" / "test_plan_e_random_timing.py"


def _root_conftest():
    spec = importlib.util.spec_from_file_location("root_conftest_guard", ROOT / "conftest.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _item(path: Path) -> SimpleNamespace:
    item = SimpleNamespace(path=path, markers=[])
    item.add_marker = item.markers.append
    return item


def test_needs_data_classifies_modules():
    conftest = _root_conftest()
    assert conftest._needs_data(DATA_DEPENDENT) is True
    assert conftest._needs_data(PURE) is False


def test_missing_data_root_skips_only_dependent_items(monkeypatch):
    conftest = _root_conftest()
    monkeypatch.setattr(conftest, "DATA_ROOT", Path("/nonexistent-tdx-data"))
    dependent, pure = _item(DATA_DEPENDENT), _item(PURE)

    conftest.pytest_collection_modifyitems(None, [dependent, pure])

    assert [m.name for m in dependent.markers] == ["skip"]
    assert "tdx-data" in dependent.markers[0].kwargs["reason"]
    assert pure.markers == []


def test_present_data_root_skips_nothing(monkeypatch, tmp_path):
    conftest = _root_conftest()
    monkeypatch.setattr(conftest, "DATA_ROOT", tmp_path)
    dependent = _item(DATA_DEPENDENT)

    conftest.pytest_collection_modifyitems(None, [dependent])

    assert dependent.markers == []
