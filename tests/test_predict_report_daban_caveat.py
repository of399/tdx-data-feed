"""夜间 predict 报告必须自带「只作排序、不是买入指令」的口径声明。

P0-4 的决定是「daban 退出实盘入口，保留研究产出」：`--from-predict-list --mode real`
被停用，但 `predict.py` 的默认模型仍是 daban，夜间照样产出 `predict_latest.md`。
榜单一旦被当成指令读，就绕过了停用。所以警告必须跟着产物走，而不是只挡在命令行入口。
"""
from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT), str(ROOT / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _load_predict():
    spec = importlib.util.spec_from_file_location(
        "predict_under_test", ROOT / "scripts" / "predict.py"
    )
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # predict.py 顶层 `from _common import ...` 依赖 scripts/ 在 sys.path 上
    # （作为脚本直接运行时由解释器注入，importlib 加载不会）
    spec.loader.exec_module(mod)
    return mod


pred = _load_predict()


def _args(**overrides) -> argparse.Namespace:
    ns = argparse.Namespace(
        date="2026-10-24",
        top=5,
        threshold=0.7,
        emit_top=False,
        emit_top_n=50,
        emit_threshold=0.7,
        emit_fields="code,prob",
    )
    for k, v in overrides.items():
        setattr(ns, k, v)
    return ns


def _feat_df(n: int = 3) -> pd.DataFrame:
    return pd.DataFrame({
        "rank": range(1, n + 1),
        "code": [f"60000{i}" for i in range(n)],
        "prob": [0.91, 0.83, 0.77][:n],
        "close": [10.0, 20.0, 30.0][:n],
        "qfq_close": [10.0, 20.0, 30.0][:n],
        "pct_t1": [0.098, 0.10, 0.05][:n],
        "ret_5d": [0.31, 0.22, 0.11][:n],
        "vol_ratio_t1": [2.4, 1.8, 1.2][:n],
        "limit": [0.10, 0.10, 0.20][:n],
        "consecutive_boards": [2, 1, 0][:n],
    })


def _emit(monkeypatch, tmp_path: Path, model_name: str, active_name: str, is_reg: bool) -> str:
    monkeypatch.setattr(pred, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(pred, "ACTIVE_MODEL_NAME", active_name)
    monkeypatch.setattr(pred, "ACTIVE_IS_REG", is_reg)
    feat_df = _feat_df()
    pred.emit_outputs(_args(), model_name, feat_df, feat_df.head(2))
    return (tmp_path / "predict_latest.md").read_text(encoding="utf-8")


def test_daban_report_declares_ranking_only(monkeypatch, tmp_path):
    md = _emit(monkeypatch, tmp_path, "daban", "daban", is_reg=False)
    assert "不是买入指令" in md
    assert "-0.78" in md
    assert "docs/DABAN_TOPK_PNL_20261022.md" in md


def test_caveat_follows_active_model_not_only_argument(monkeypatch, tmp_path):
    """显式 --model daban 但实际回退到别的模型时，不该挂上 daban 的警告。"""
    md = _emit(monkeypatch, tmp_path, "daban", pred.FALLBACK_MODEL, is_reg=True)
    assert "不是买入指令" not in md


def test_fallback_model_report_has_no_daban_caveat(monkeypatch, tmp_path):
    md = _emit(monkeypatch, tmp_path, pred.FALLBACK_MODEL, pred.FALLBACK_MODEL, is_reg=True)
    assert "不是买入指令" not in md
    assert "预期 PnL" in md


def test_cited_doc_path_exists():
    assert (ROOT / "docs" / "DABAN_TOPK_PNL_20261022.md").exists()
