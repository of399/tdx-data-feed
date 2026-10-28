"""ST 判定单一来源：predict 不再自带一份字符串判断。"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import predict  # noqa: E402

from tdxfeed.names import is_st_name  # noqa: E402


def test_predict_is_st_delegates_to_names():
    src = inspect.getsource(predict.is_st)
    assert "is_st_name(" in src
    assert "'ST' in" not in src


def test_predict_is_st_keeps_int_contract():
    assert predict.is_st("平安银行") == 0
    assert predict.is_st("*ST美丽") == 1
    assert predict.is_st(None) == 0


def test_predict_agrees_with_authority():
    for name in ("平安银行", "ST 康佳", "*ST美丽", "", None):
        assert predict.is_st(name) == int(is_st_name(name)), name


def test_run_prediction_has_no_inline_st_check():
    """run_prediction 的实时名称兜底也必须走 names 单一来源。"""
    src = inspect.getsource(predict.run_prediction)
    assert "is_st_name(" in src
    assert "'ST' in" not in src
