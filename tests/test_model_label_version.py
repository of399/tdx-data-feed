"""21 维模型必须是用当前宽表训出来的：特征名一致，且预测与冻结超参可复现。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MODEL = ROOT / "data" / "double_up" / "d_signal_5d_model_vpnl_huber_stage1.txt"
FROZEN = ROOT / "data" / "double_up" / "stage1_selected_features.json"

pytestmark = pytest.mark.skipif(not MODEL.exists() or not FROZEN.exists(),
                                reason="缺 stage1 模型或冻结契约")


def test_model_feature_names_match_frozen_contract():
    import lightgbm as lgb
    booster = lgb.Booster(model_file=str(MODEL))
    assert booster.num_feature() == 21
    assert booster.feature_name() == json.loads(FROZEN.read_text())["selected_features"]


def test_model_mtime_is_after_wide_table_label_rewrite():
    wide = ROOT / "data" / "double_up" / "stage1_wide_table.parquet"
    assert MODEL.stat().st_mtime >= wide.stat().st_mtime, "模型比宽表旧：标签换了但模型没重训"
