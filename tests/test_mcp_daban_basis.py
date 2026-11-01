"""MCP daban 选股的模型口径必须与特征口径一致。

_daban_scan_one 用 raw 帧算特征（compute_window_features_extended 不传 frame_hfq），
所以它加载的模型必须是 raw 口径；若换成 hfq 模型却没让特征走 hfq，打分整体偏移。
本守卫把“模型注册表口径 ↔ 扫描特征口径”钉成一致，任一侧单独改动都会变红。
（当前线上 daban 模型是 raw、特征也是 raw，自洽；这是防未来漂移的钉子，不是改行为。）
"""
from __future__ import annotations

import importlib.util
import inspect
import sys
from pathlib import Path

NEEDS_DATA = False

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT), str(ROOT / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_mcp_daban_feature_basis_matches_model():
    bt = _load("mcp_backtest_basis", "mcp/servers/backtest.py")
    pr = _load("predict_basis", "scripts/predict.py")

    model_name = Path(bt.DABAN_MODEL).name
    entries = [v for v in pr.MODEL_REGISTRY.values() if Path(v[0]).name == model_name]
    assert entries, f"MCP 加载的模型 {model_name} 不在 predict 注册表里"
    basis = entries[0][2]

    src = inspect.getsource(bt._daban_scan_one)
    uses_hfq = "frame_hfq" in src
    assert (basis == "hfq") == uses_hfq, (
        f"模型口径 {basis} 与扫描特征口径 {'hfq' if uses_hfq else 'raw'} 不一致："
        "换模型须同步改特征构建，否则打分偏移"
    )
