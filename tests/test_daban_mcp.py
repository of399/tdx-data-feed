"""daban MCP 工具回归测试（tdx-backtest.daban_top_picks）。

覆盖：
1. 工具在 server 与 schemas/tools.json 中双端注册，计数一致；
2. 扫描 worker 的特征契约（29 维、名称与模型一致、数值有限）；
3. `prev_close` 特征名不被行情元数据覆盖（历史命名冲突导致的打分偏移）；
4. 非交易日 / 非法参数的行为；
5. daban skill YAML 只声明 runtime 真读的字段，工具默认值取自真实入口 schemas/tools.json。
"""
from __future__ import annotations

import importlib.util
import inspect
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
    universe = backtest._pool()
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
    """KI-SKILL-003：本用例原先断言 `skill["tools"]` 与 `skill["input_schema"]`，
    而这两个都是 KI-MCP-002 登记的死字段——`skill_router/runtime.py` 从不读取，
    P1-2（71195c5）已把它们从 9 份 skill yaml 里删掉。断言死字段等于钉住装饰，
    所以这里改钉活的两半：

    ① yaml 只声明 runtime 真读的字段（`name` / `llm.model_id`），死字段不得回来；
    ② 工具的默认值取自真实入口 `mcp/schemas/tools.json`，不是 yaml 里的副本。
    """
    skill = yaml.safe_load((ROOT / "skills/daban.yaml").read_text(encoding="utf-8"))
    assert skill["name"] == "daban"
    assert skill["llm"]["model_id"] == "none", "daban 是纯工具技能，runtime 据此跳过 LLM"
    for dead in ("tools", "input_schema"):
        assert dead not in skill, f"{dead} 是 KI-MCP-002 死字段（P1-2 已删），别为旧断言加回来"
    schema = json.loads((ROOT / "mcp/schemas/tools.json").read_text(encoding="utf-8"))
    tools = {t["name"]: t for t in schema["servers"]["tdx-backtest"]["tools"]}
    declared = tools["daban_top_picks"]["inputSchema"]["properties"]["top"]["default"]
    actual = inspect.signature(backtest.daban_top_picks).parameters["top"].default
    assert declared == actual == 5, "MCP 客户端看到的 top 默认值必须与实现签名同源"
