"""三份双跑契约（B0/B1/B2）由生成器产出，不许手抄列名。

为什么要有这一层：双跑比的就是一条轴——**特征集合**。只要有任何一份配置是手抄出来的，
「21 维基线」和「21+47 加法」就可能来自两个不同的时刻（比如基线那 21 条是旧契约、加法那
47 条来自新工件），差值就同时掺了名单变化和装配口径变化，读不出结论。所以三条契约都从
两份权威工件程序化派生：

- 基线 21 维 + 超参 = `data/double_up/stage1_selected_features_hfq_20261030.json`
- 47 条净新增 / 15 条现役独立 = `config/alpha158_union.json`（ρ=0.8、h=5 那次去重）

判据：① B0 逐字等于基线契约；② B1 = B0 + 净新增、无重叠、计数对得上；③ B2 与工件的
`union` 同集合；④ 三份共用同一组超参（只动特征这一条轴）；⑤ 生成器里不许出现任何列名
字面量——换成一对合成输入必须照样派生出预期名单；⑥ 未知维名要拒绝，不许静默丢维。
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
GEN = ROOT / "scripts" / "stage1_union_configs.py"
BASE_CONTRACT = ROOT / "data" / "double_up" / "stage1_selected_features_hfq_20261030.json"
UNION_ARTIFACT = ROOT / "config" / "alpha158_union.json"

# 本模块最后一条用例读 data/double_up 的冻结契约（data/ 不在 git 里）。路径是
# `ROOT / "data" / ...` 这种分段写法，`conftest._needs_data` 的 "data/" 字样匹配不到，
# 所以按它自己的约定显式 opt-in（零缩进），并登记进 tests/test_ci_data_skip.py 的集合。
NEEDS_DATA = True

_spec = importlib.util.spec_from_file_location("stage1_union_configs", GEN)
gen = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gen)  # type: ignore[union-attr]


def _inputs(tmp_path: Path) -> tuple[dict, dict]:
    base = {
        "selected_features": ["a1", "a2", "a3"],
        "n_features": 3,
        "hyperparams": {"learning_rate": 0.01, "num_leaves": 44},
        "objective": "huber", "alpha": 0.5, "target": "hypothetical_pnl",
    }
    union = {
        "dedup": {"rho": 0.8, "horizon": 5},
        "feat26_independent": ["a1", "a3"],
        "alpha158_net_new": ["ROC5", "MA60", "CORR10"],
        "union": ["a1", "a3", "ROC5", "MA60", "CORR10"],
        "content_sha256": "deadbeef",
    }
    return base, union


def test_b0_is_the_baseline_contract_verbatim(tmp_path):
    base, union = _inputs(tmp_path)
    out = gen.build_contracts(base, union)
    assert out["b0"]["selected_features"] == ["a1", "a2", "a3"], "基线契约被改写了"
    assert out["b0"]["n_features"] == 3


def test_b1_is_pure_addition_and_disjoint(tmp_path):
    """B1 = 基线 21 + 净新增 47：两侧不得重叠，否则「加法」里有减法。"""
    base, union = _inputs(tmp_path)
    out = gen.build_contracts(base, union)
    b1 = out["b1"]["selected_features"]
    assert b1 == ["a1", "a2", "a3", "ROC5", "MA60", "CORR10"]
    assert len(b1) == len(set(b1)) == 6
    assert out["b1"]["n_alpha158_added"] == 3


def test_b2_matches_the_artifact_union_as_a_set(tmp_path):
    base, union = _inputs(tmp_path)
    out = gen.build_contracts(base, union)
    assert set(out["b2"]["selected_features"]) == set(union["union"])
    assert out["b2"]["n_features"] == len(union["union"])


def test_all_three_share_the_same_hyperparameters(tmp_path):
    """只动特征这一条轴：超参、目标、早停协议必须逐字相同。"""
    base, union = _inputs(tmp_path)
    out = gen.build_contracts(base, union)
    hp = [json.dumps(out[k]["hyperparams"], sort_keys=True) for k in ("b0", "b1", "b2")]
    assert len(set(hp)) == 1, "三份契约的超参不一致，差值就不是纯特征对照"
    for k in ("b0", "b1", "b2"):
        assert out[k]["objective"] == "huber" and out[k]["target"] == "hypothetical_pnl"


def test_generator_contains_no_feature_name_literals():
    """生成器里不许出现任何一维的名字——名单只能从两份工件读进来。"""
    src = GEN.read_text(encoding="utf-8")
    body = "\n".join(line for line in src.splitlines() if not line.strip().startswith("#"))
    real = json.loads(UNION_ARTIFACT.read_text(encoding="utf-8"))
    names = set(real["union"]) | set(
        json.loads(BASE_CONTRACT.read_text(encoding="utf-8"))["selected_features"])
    leaked = sorted(n for n in names if f'"{n}"' in body or f"'{n}'" in body)
    assert not leaked, f"生成器里手抄了列名：{leaked}"


def test_unknown_dimension_is_rejected_not_dropped(tmp_path):
    """净新增里出现宽表没有的维 ⇒ 拒绝。静默少一维会让「68 维」其实是 67 维。"""
    base, union = _inputs(tmp_path)
    union["alpha158_net_new"] = ["ROC5", "NOT_A_DIM"]
    union["union"] = union["feat26_independent"] + union["alpha158_net_new"]
    with pytest.raises(ValueError, match="NOT_A_DIM"):
        gen.build_contracts(base, union, known_cols=["a1", "a2", "a3", "ROC5"])


def test_cli_writes_three_configs_and_check_is_idempotent(tmp_path):
    base, union = _inputs(tmp_path)
    bp, up = tmp_path / "base.json", tmp_path / "union.json"
    bp.write_text(json.dumps(base), encoding="utf-8")
    up.write_text(json.dumps(union), encoding="utf-8")
    out_dir = tmp_path / "out"
    rc = gen.main(["--base", str(bp), "--union", str(up), "--out-dir", str(out_dir)])
    assert rc == 0
    written = sorted(p.name for p in out_dir.glob("*.json"))
    assert written == ["stage1_contract_b0.json", "stage1_contract_b1.json",
                       "stage1_contract_b2.json"], written
    # mkstemp 出来是 0600；契约必须回到 umask 下的常规权限，否则落到 config/ 时
    # 会出现"同一份工件、两种权限"这种看不见的差
    for p in out_dir.glob("*.json"):
        assert p.stat().st_mode & 0o777 == 0o644, (p.name, oct(p.stat().st_mode & 0o777))
    again = gen.main(["--base", str(bp), "--union", str(up), "--out-dir", str(out_dir),
                      "--check"])
    assert again == 0, "重跑必须逐字复现（幂等），否则产物不可复核"
    tampered = json.loads((out_dir / "stage1_contract_b1.json").read_text(encoding="utf-8"))
    tampered["selected_features"] = tampered["selected_features"][:2]
    (out_dir / "stage1_contract_b1.json").write_text(json.dumps(tampered), encoding="utf-8")
    assert gen.main(["--base", str(bp), "--union", str(up), "--out-dir", str(out_dir),
                     "--check"]) != 0, "手改名单必须让 --check 非零"


def test_real_artifacts_produce_the_expected_sizes():
    """真工件：21 / 21+47=68 / 62。这三个数是双跑报告里要引用的，必须当场算出来。"""
    base = json.loads(BASE_CONTRACT.read_text(encoding="utf-8"))
    union = json.loads(UNION_ARTIFACT.read_text(encoding="utf-8"))
    out = gen.build_contracts(base, union)
    assert out["b0"]["n_features"] == 21
    assert out["b1"]["n_features"] == 68
    assert out["b2"]["n_features"] == 62
    # 净新增与基线 21 维不重叠（去重时已把现役当 seed，工件侧再确认一次）
    assert not (set(base["selected_features"]) & set(union["alpha158_net_new"]))
