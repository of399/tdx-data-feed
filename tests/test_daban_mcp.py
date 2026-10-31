"""daban MCP 工具回归测试（tdx-backtest.daban_top_picks）。

覆盖：
1. 工具在 server 与 schemas/tools.json 中双端注册，计数一致；
2. 扫描 worker 的特征契约（29 维、名称与模型一致、数值有限）；
3. `prev_close` 特征名不被行情元数据覆盖（历史命名冲突导致的打分偏移）；
4. 非交易日 / 非法参数的行为；
5. daban skill YAML 的 MCP 引用。
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import numpy as np
import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 间接依赖行情数据：backtest 模块自己读 data/，本文件源码里没有 data/ 字样。
NEEDS_DATA = True


def _load(name: str, relpath: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relpath)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


backtest = _load("backtest_server_under_test", "mcp/servers/backtest.py")


@pytest.fixture(scope="module")
def latest() -> str:
    return backtest._latest_market_date()


@pytest.fixture(scope="module")
def model():
    return backtest._load_daban_model()


def test_tool_registered_in_server_and_schema():
    schema = json.loads((ROOT / "mcp/schemas/tools.json").read_text(encoding="utf-8"))
    names = [t["name"] for t in schema["servers"]["tdx-backtest"]["tools"]]
    assert "daban_top_picks" in backtest.TOOLS
    assert "daban_top_picks" in names
    assert len(backtest.TOOLS) == len(names) == 4
    actual = sum(len(s["tools"]) for s in schema["servers"].values())
    assert schema["tool_count"] == schema["total_tools"] == actual


def test_universe_contains_only_sh_sz_stocks():
    universe = backtest._load_universe()
    assert len(universe) > 5000
    assert all(code[:2] in ("60", "68", "00", "30") for code in universe)
    assert not any(code.startswith("920") for code in universe)
    assert universe.get("600000")


def test_scan_worker_feature_contract(latest, model):
    r = backtest._daban_scan_one(("600000", latest))
    assert r["status"] == "ok"
    assert set(r["feats"]) == set(model.feature_name())
    assert len(r["feats"]) == 29
    assert {"consecutive_boards", "is_limit_up_today", "board_position"} <= set(r["feats"])
    assert all(np.isfinite(v) for v in r["feats"].values())
    assert r["feats"]["limit"] == pytest.approx(0.095)
    assert abs(r["pct_today"]) <= 0.5


def test_feature_frame_keeps_prev_close_feature(latest):
    r = backtest._daban_scan_one(("600000", latest))
    frame = backtest._daban_rows_to_frame([r])
    assert frame.loc[0, "prev_close"] == pytest.approx(r["feats"]["prev_close"])
    assert frame.loc[0, "code"] == "600000"
    assert frame.loc[0, "close"] == pytest.approx(r["close"])


def test_scan_worker_no_bar_on_non_trading_day():
    r = backtest._daban_scan_one(("600000", "2026-10-18"))  # 周日
    assert r["status"] == "no_bar"


def test_invalid_params_rejected_before_scan():
    with pytest.raises(backtest.McpError):
        backtest.daban_top_picks(top=0)
    with pytest.raises(backtest.McpError):
        backtest.daban_top_picks(top=51)
    with pytest.raises(backtest.McpError):
        backtest.daban_top_picks(date="not-a-date")
    with pytest.raises(backtest.McpError):
        backtest.daban_top_picks(min_amount=-1)


def test_skill_yaml_wires_mcp_tool():
    skill = yaml.safe_load((ROOT / "skills/daban.yaml").read_text(encoding="utf-8"))
    assert skill["name"] == "daban"
    assert "mcp:tdx-backtest.daban_top_picks" in skill["tools"]
    assert skill["llm"]["model_id"] == "none"
    assert skill["input_schema"]["properties"]["top"]["default"] == 5
