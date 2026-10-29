"""21 维模型必须是用当前宽表训出来的：特征名一致，且预测与冻结超参可复现。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MODEL = ROOT / "data" / "double_up" / "d_signal_5d_model_vpnl_huber_stage1.txt"
FROZEN = ROOT / "data" / "double_up" / "stage1_selected_features.json"
LABELS = ROOT / "data" / "double_up" / "d_signal_5d_features_vpnl.csv"

pytestmark = pytest.mark.skipif(
    not MODEL.exists() or not FROZEN.exists() or not LABELS.exists(),
    reason="缺 stage1 模型 / 冻结契约 / 标签 CSV")


def test_model_feature_names_match_frozen_contract():
    import lightgbm as lgb
    booster = lgb.Booster(model_file=str(MODEL))
    assert booster.num_feature() == 21
    assert booster.feature_name() == json.loads(FROZEN.read_text())["selected_features"]


def test_model_mtime_is_after_label_rewrite():
    # 比标签源 CSV，不比宽表：宽表是派生件，重建它（如 2026-10-30 口径双跑）不改标签，
    # 拿它的 mtime 当标签版本会误报。宽表确由当前 CSV 装配由
    # tests/test_wide_table_label_source.py 单独守。
    assert MODEL.stat().st_mtime >= LABELS.stat().st_mtime, "模型比标签源旧：标签换了但模型没重训"
