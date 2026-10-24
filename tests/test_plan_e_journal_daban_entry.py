"""predict list 不再自动写实盘 journal（2026-10-24 决定：daban 退出实盘入口，保留研究产出）。

`--from-predict-list --mode real` 直接把 predict.py 的默认榜单（daban 小仓位 Top5）逐笔灌进
journal.csv，而 journal.csv 是喂给 `plan_e_real_samples.py → --real` 的"实盘"账本。
按诚实成交口径，这张榜没有正期望：
  - docs/DABAN_TOPK_PNL_20261022.md：K=5 成交 1509 笔，−0.78%/笔，累计 −94.5%，
    未计成本毛收益 −0.58%（负期望来自 T+1 开盘买入的入场时点，不是成本）
  - data/daban/execution_backtest_20261022.json 的 gate：primary 六条规则全部未达标，
    overall_go=false

研究侧不动：排序、夜间 predict 产物、`--mode simulate` 的 journal_sim.csv 全部保留。
测试把 ROOT 与 live 目录都指到 tmp_path，绝不碰仓库里的真实账本。
"""
from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT), str(ROOT / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _load_journal():
    spec = importlib.util.spec_from_file_location(
        "plan_e_journal_under_test", ROOT / "scripts" / "plan_e_journal.py"
    )
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _isolate(tmp_path: Path):
    """把脚本里所有落盘路径搬到 tmp_path，避免测试写真实 journal / positions。"""
    pej = _load_journal()
    live = tmp_path / "data" / "plan_e" / "live"
    pej.ROOT = tmp_path
    pej.LIVE_DIR = live
    pej.JOURNAL_FILE = live / "journal.csv"
    pej.SIM_JOURNAL_FILE = live / "journal_sim.csv"
    pej.POSITIONS_FILE = live / "positions.json"
    pej.SIGNALS_DIR = live / "daily_signals"
    pej.WEEKLY_DIR = live / "weekly_reports"
    pej.PNL_FILE = live / "pnl_summary.csv"
    return pej, live


def _seed_predict(tmp_path: Path, rows: list[tuple[str, float, float]]) -> None:
    pred_dir = tmp_path / "data" / "predictions"
    pred_dir.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(
        {"code": [r[0] for r in rows],
         "close": [r[1] for r in rows],
         "prob": [r[2] for r in rows]}
    ).to_csv(pred_dir / "predict_20261024_180500.csv", index=False)


def test_real_mode_refuses_predict_list(monkeypatch, tmp_path):
    pej, live = _isolate(tmp_path)
    _seed_predict(tmp_path, [("600519", 10.0, 0.97), ("300296", 7.32, 0.93)])

    with pytest.raises(SystemExit) as exc:
        pej.mode_from_predict_list("real")

    msg = str(exc.value)
    assert "daban" in msg, msg
    assert "simulate" in msg, msg        # 指回研究通道
    assert not (live / "journal.csv").exists(), "实盘账本被写了，闸门没生效"
    assert not (live / "positions.json").exists(), "持仓文件被写了，闸门没生效"


def test_simulate_path_keeps_recording(tmp_path):
    """研究产出保留：同一张榜仍可按笔写入 journal_sim.csv。"""
    pej, live = _isolate(tmp_path)
    _seed_predict(tmp_path, [("600519", 10.0, 0.97), ("300296", 7.32, 0.93)])

    pej.mode_from_predict_list("simulate")

    booked = pd.read_csv(live / "journal_sim.csv")
    assert sorted(booked["code"].astype(str).str.zfill(6)) == ["300296", "600519"]
    assert set(booked["source"]) == {"sim"}
    assert not (live / "journal.csv").exists()


def test_refusal_cites_evidence_that_exists():
    """闸门文案里的证据必须可点开，不能是不存在的路径或编造的文件名。"""
    pej = _load_journal()
    msg = pej.DABAN_LIVE_BLOCK
    for rel in re.findall(r"(docs/[\w.\-]+\.md|data/daban/[\w.\-]+\.json)", msg):
        assert (ROOT / rel).exists(), rel
    assert "-0.78" in msg or "−0.78" in msg
    assert "94.5" in msg
