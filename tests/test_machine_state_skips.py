"""运行期“按机器状态自跳过”的测试面必须显式登记。

CI 门禁的收集期跳过由 conftest + test_ci_data_skip 管（DATA_DEPENDENT_MODULES）；
但还有一类：模块本身被收集，个别用例在运行期因本机状态（data/、vault 索引、
venv-math、/etc 单元、告警栈/Ollama 端口）而 pytest.skip。这类跳过在本地有数据时
看不见，在 CI 成片出现，是“本机绿 / CI 跳过”分叉的来源。

本守卫把整个运行期自跳过面冻结成一份登记表：每个模块注明它依赖的机器资源。
新增一个会自跳过的测试模块，就必须在这里登记并说明依赖，否则变红——
让这层面在代码评审里可见、在数量上有界（与 CI 的 skipped 上限配套）。
纯静态守卫，不改变任何用例行为。
"""
from __future__ import annotations

import ast
from pathlib import Path

NEEDS_DATA = False

ROOT = Path(__file__).resolve().parents[1]

# 模块 → 它运行期跳过所依赖的机器资源（登记即文档）。
MACHINE_STATE_SKIP_MODULES = {
    "tests/test_alpha158_artifact.py": "venv-qlib 岛解释器（岛↔工件重导比对，以及 --check 的篡改反向验证）",
    "tests/test_alpha158_compose.py": "venv-qlib 岛解释器（158 列组合真值由岛内真 qlib 算子树产出，无岛即无从比对）",
    "tests/test_alpha158_semantics.py": "venv-qlib 岛解释器（算子金标准的 --check 与用户站点隔离验证）",
    "tests/test_amount_deviation_guard.py": "data/parquet/daily 真实日线表（KI-DATA-002 那 5 行的真数据核验）",
    "tests/test_backfill_history_runner.py": "data/ 回填产物（amount 修补接线）",
    "tests/test_daily_history_depth_no_cap_truncation.py": "data/ 审计产物与回填文件",
    "tests/test_daily_listing_and_resolve.py": "data/parquet/daily 行情目录",
    "tests/test_deploy_paths_unified.py": "本机用户级 systemd 部署目录（~/.config/systemd/user）",
    "tests/test_double_up_sample_refresh.py": "data/double_up 样本备份",
    "tests/test_limit_single_source.py": "data/parquet/daily 行情目录",
    "tests/test_math_unit_deployed_matches_repo.py": "/etc 部署的 math-sympy-http 单元 + systemctl",
    "tests/test_math_venv_latex_parsing.py": "venv-math / 主 venv 解释器",
    "tests/test_mf_output_injection.py": "data/parquet/daily 行情目录",
    "tests/test_model_label_version.py": "stage1 模型 / 冻结契约 / 标签 CSV",
    "tests/test_parquet_schema_contract.py": "data/parquet/daily 行情目录",
    "tests/test_plan_e_journal_daban_entry.py": "data/ 回测证据",
    "tests/test_pnl_label_recompute.py": "data/double_up 标签与旧快照",
    "tests/test_position_risk.py": "data/parquet/daily 行情目录",
    "tests/test_qlib_env_island.py": "venv-qlib 专用解释器（pyqlib 只在里面装得上）",
    "tests/test_shares_derivation.py": "data/parquet/finance.parquet（股本反推真数据核验）",
    "tests/test_systemd_system_units_mirror.py": "/etc 引用本仓库的 system 单元",
    "tests/test_theory_reference.py": "Obsidian vault 索引 / 讲义笔记",
    "tests/test_wide_table_label_source.py": "stage1 宽表 / 标签 CSV / 特征清单 / 日线",
    "v5/tests/conftest.py": "math-sympy-http 单元指定的解释器（venv-math）",
    "v5/tests/test_alerting_pipeline.py": "告警栈 :9090/:9093/:8002 + sink",
    "v5/tests/test_llm_ollama.py": "Ollama :11434",
}


def _is_pytest_skip(node: ast.Call) -> bool:
    """只认 pytest.skip / pytest.mark.skipif，避免误匹配无关对象的 .skip()。"""
    func = node.func
    if not isinstance(func, ast.Attribute) or func.attr not in ("skip", "skipif"):
        return False
    base = func.value
    while isinstance(base, ast.Attribute):
        base = base.value
    return isinstance(base, ast.Name) and base.id == "pytest"


def _detect_skip_modules() -> set[str]:
    found: set[str] = set()
    for base in ("tests", "v5/tests"):
        for path in sorted((ROOT / base).glob("*.py")):
            if path.name == "test_machine_state_skips.py":
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except SyntaxError:
                continue
            if any(isinstance(n, ast.Call) and _is_pytest_skip(n) for n in ast.walk(tree)):
                found.add(f"{base}/{path.name}")
    return found


def test_runtime_skip_surface_is_exactly_registered():
    detected = _detect_skip_modules()
    registered = set(MACHINE_STATE_SKIP_MODULES)
    assert detected == registered, (
        f"未登记的自跳过模块（需注明机器资源依赖）: {sorted(detected - registered)}；"
        f"登记表里已不存在的条目: {sorted(registered - detected)}"
    )


def test_registered_modules_exist():
    for rel in MACHINE_STATE_SKIP_MODULES:
        assert (ROOT / rel).exists(), f"登记条目指向不存在的文件: {rel}"
