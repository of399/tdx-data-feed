"""CI 没有本地 `data/` 时，依赖本地行情/产物的测试模块应被收集期跳过，而不是让整片变红。

守卫对象是根 `conftest.py` 的 `_needs_data` / `pytest_collection_modifyitems`。
真跑一次的验证方式：`TDX_TEST_DATA_ROOT=/nonexistent venv/bin/pytest tests/ -q`，
此时只有引用 `data/` 或绝对路径的模块被 skip，纯模块照跑。
"""

import importlib.util
import re
from pathlib import Path
from types import SimpleNamespace

# 本模块只读 conftest 与测试文件的源码文本、不读 data/；docstring 里的 data/ 字样
# 会被根 conftest 的「无数据跳过」分类误判，故显式退出（否则 CI 上这条守卫自身被 skip）。
# （放在 import 块之后：语句插在 import 之前会触发 E402。）
NEEDS_DATA = False

ROOT = Path(__file__).resolve().parents[1]
# 正样本选真读 data/ 的模块：test_adj_factor_history.py 只用 tmp_path 构造 fixture，
# 数据依赖仅是 docstring 字样，措辞一改分类就翻；test_pnl_label_recompute.py 的
# skipif 直接依赖 data/double_up 下的文件。
DATA_DEPENDENT = ROOT / "tests" / "test_pnl_label_recompute.py"
PURE = ROOT / "tests" / "test_plan_e_random_timing.py"

# 无数据环境里**唯一**允许被收集期跳过的模块集合。新增一条数据依赖测试时
# 必须显式登记到这里；反过来，任何静态守卫都不许悄悄从这个集合外消失。
DATA_DEPENDENT_MODULES = {
    "test_daban_mcp.py",
    "test_daily_history_depth_no_cap_truncation.py",
    "test_hfq_factor_monotone.py",
    "test_pnl_label_recompute.py",
    "test_stage1_candidates.py",
    "test_stage1_factors_parity.py",
    "test_stage1_wide_table.py",
    "test_wide_table_label_source.py",
}


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
    monkeypatch.setattr(conftest, "_HAS_DATA", False)
    dependent, pure = _item(DATA_DEPENDENT), _item(PURE)

    conftest.pytest_collection_modifyitems(None, [dependent, pure])

    assert [m.name for m in dependent.markers] == ["skip"]
    assert "tdx-data" in dependent.markers[0].kwargs["reason"]
    assert pure.markers == []


def test_present_data_root_skips_nothing(monkeypatch, tmp_path):
    conftest = _root_conftest()
    monkeypatch.setattr(conftest, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(conftest, "_HAS_DATA", True)
    dependent = _item(DATA_DEPENDENT)

    conftest.pytest_collection_modifyitems(None, [dependent])

    assert dependent.markers == []


def test_skip_decision_is_snapshotted_before_collection(monkeypatch, tmp_path):
    """收集期导入测试模块会创建 data/；判定必须用导入期快照，不能被它翻成「有数据」。"""
    conftest = _root_conftest()
    monkeypatch.setattr(conftest, "DATA_ROOT", tmp_path)  # 目录真实存在
    monkeypatch.setattr(conftest, "_HAS_DATA", False)
    dependent = _item(DATA_DEPENDENT)

    conftest.pytest_collection_modifyitems(None, [dependent])

    assert [m.name for m in dependent.markers] == ["skip"]


def test_guard_module_opts_out_of_data_skip():
    conftest = _root_conftest()
    guard = ROOT / "tests" / "test_no_hardcoded_data_paths.py"
    assert conftest._needs_data(guard) is False


def test_guard_module_not_skipped_without_data(monkeypatch):
    conftest = _root_conftest()
    monkeypatch.setattr(conftest, "DATA_ROOT", Path("/nonexistent-tdx-data"))
    item = _item(ROOT / "tests" / "test_no_hardcoded_data_paths.py")
    conftest.pytest_collection_modifyitems(None, [item])
    assert item.markers == []


def _fixture_module(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "test_fixture.py"
    path.write_text(text, encoding="utf-8")
    return path


def test_docstring_and_comments_do_not_mark_data_dependent(monkeypatch, tmp_path):
    """说明文字里出现 data/ 不是数据依赖：只有代码里的字面量才算。

    这正是本次收紧的动机：15 个纯静态守卫因为 docstring 提了一句 data/ 而在 CI 整体跳过，
    于是「静态漂移在 CI 里仍可见」变成了空话。
    """
    conftest = _root_conftest()
    monkeypatch.setattr(conftest, "TESTS_DIR", tmp_path)
    path = _fixture_module(
        tmp_path,
        '"""读 data/parquet/daily 的说明。"""\n'
        "# 也提一下 /home/jiuben，但这是注释\n"
        "\n"
        "def test_x():\n"
        "    assert True\n",
    )
    assert conftest._needs_data(path) is False


def test_code_string_literals_still_mark_data_dependent(monkeypatch, tmp_path):
    conftest = _root_conftest()
    monkeypatch.setattr(conftest, "TESTS_DIR", tmp_path)
    path = _fixture_module(
        tmp_path,
        "def test_x():\n"
        '    path = "data/parquet/daily"\n',
    )
    assert conftest._needs_data(path) is True


def test_explicit_flags_win_over_literal_scan(monkeypatch, tmp_path):
    conftest = _root_conftest()
    monkeypatch.setattr(conftest, "TESTS_DIR", tmp_path)
    opted_out = _fixture_module(
        tmp_path,
        "NEEDS_DATA = False\n"
        "\n"
        "def test_x():\n"
        '    path = "data/parquet/daily"\n',
    )
    assert conftest._needs_data(opted_out) is False

    other = tmp_path / "other"
    other.mkdir()
    opted_in = _fixture_module(other, "NEEDS_DATA = True\n")
    assert conftest._needs_data(opted_in) is True


def test_skipped_modules_are_exactly_the_registered_set():
    """无数据时跳过的模块必须与登记集完全一致：多一个是误伤，少一个是漏网。"""
    conftest = _root_conftest()
    skipped = {
        path.name
        for path in sorted((ROOT / "tests").glob("test_*.py"))
        if conftest._needs_data(path)
    }
    assert skipped == DATA_DEPENDENT_MODULES, (
        f"多跳过：{sorted(skipped - DATA_DEPENDENT_MODULES)}；"
        f"少跳过：{sorted(DATA_DEPENDENT_MODULES - skipped)}"
    )


def test_workflow_enforces_a_passed_floor_and_a_skipped_ceiling():
    """只设通过数下限挡不住「整片 skip」：用例数涨了，下限没涨也能过。

    skipped 上限抓的是另一类静默：数据/解释器/服务缺失时成片跳过，
    passed 仍可能高于旧下限。两个方向都要有界。
    """
    workflow = (ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
    assert re.search(r'test "\$\{passed:-0\}" -ge \d+', workflow), \
        "tests.yml 缺通过数下限"
    assert re.search(r'test "\$\{skipped:-0\}" -le \d+', workflow), \
        "tests.yml 缺 skipped 上限：整片跳过时会静默变绿"
