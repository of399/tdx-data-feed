"""方案 E 退出实盘入口（2026-10-24 决定），研究产出保留。

统计口径修完之后，`plan_e_backtest_v2_latest.md` 的四个检验没有一个能拒绝"无超额收益"：
Deflated Sharpe p=1.000、Newey-West p=0.748、bootstrap 均值 p=0.561、胜率对赔率隐含
盈亏平衡（31.2%）单侧 p=0.296。而 `--mode daily` 原来会照着清单逐笔算
`cash × 10% / 收盘价` 的买入拆单量并打印——等于把一个测不出边际的信号包装成下单指令。

本测试锁三件事：
1. 停用话术必须带着可核验的证据留在脚本里（和 daban 的 `DABAN_LIVE_BLOCK` 同一处理方式）；
2. daily 模式不再输出拆单/仓位建议，清单本身照旧打印，研究用途不受影响；
3. 头部文档里"单股仓位 10%"不能再被读成实盘规则。

测试用 tmp_path 隔离，绝不碰仓库里的真实账本与清单。
"""
from __future__ import annotations

import importlib.util
import os
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT), str(ROOT / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _load(module_name: str, filename: str):
    spec = importlib.util.spec_from_file_location(module_name, ROOT / "scripts" / filename)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _isolate_journal(tmp_path: Path):
    pej = _load("plan_e_journal_retire_test", "plan_e_journal.py")
    live = tmp_path / "data" / "plan_e" / "live"
    pej.ROOT = tmp_path
    pej.LIVE_DIR = live
    pej.JOURNAL_FILE = live / "journal.csv"
    pej.SIM_JOURNAL_FILE = live / "journal_sim.csv"
    pej.POSITIONS_FILE = live / "positions.json"
    pej.SIGNALS_DIR = live / "daily_signals"
    pej.WEEKLY_DIR = live / "weekly_reports"
    pej.PNL_FILE = live / "pnl_summary.csv"
    pej.SIGNALS_DIR.mkdir(parents=True, exist_ok=True)
    return pej, live


def _write_today_list(live: Path, entry_date: str) -> Path:
    """写出盘前 timer 该产出的那份今日清单（mtime = 今天 08:45）。"""
    live.parent.mkdir(parents=True, exist_ok=True)
    fp = live.parent / "plan_e_today.csv"
    pd.DataFrame([{
        "code": "600519", "signal_date": "2026-10-22", "t1_pct": 0.1001,
        "t1_vol_ratio": 2.3, "entry_date": entry_date, "note": "今日开盘入场",
    }]).to_csv(fp, index=False)
    ts = datetime.strptime(f"{entry_date} 08:45:00", "%Y-%m-%d %H:%M:%S").timestamp()
    os.utime(fp, (ts, ts))
    return fp


def test_retirement_notice_records_the_evidence():
    """停用不能只是一句"已停用"，要把可核验的检验数字留在原地（daban 先例）。"""
    pej = _load("plan_e_journal_notice", "plan_e_journal.py")
    notice = pej.PLAN_E_LIVE_RETIREMENT
    assert "退出实盘入口" in notice
    assert "不作建仓" in notice, "停用声明没说这份清单还能不能照着下单"
    assert "盈亏平衡" in notice, "没交代胜率检验比的是哪个基准"
    assert "plan_e_backtest_v2_latest.md" in notice, "证据没有可复核的出处"


def test_daily_mode_prints_the_notice_and_no_order_sizing(tmp_path, capsys):
    pej, live = _isolate_journal(tmp_path)
    today = datetime.now().strftime("%Y-%m-%d")
    _write_today_list(live, today)

    pej.mode_daily(SimpleNamespace(lam=0.05, exec_buckets=6))
    out = capsys.readouterr().out
    assert "退出实盘入口" in out, "daily 模式没有告知实盘入口已停用"
    assert "拆单建议" not in out, "还在给一个测不出边际的信号算下单量"
    assert "600519" in out, "清单本身仍要照常输出（研究用途）"


def test_daily_mode_no_longer_advertises_order_sizing():
    pej = _load("plan_e_journal_doc", "plan_e_journal.py")
    doc = pej.mode_daily.__doc__ or ""
    assert "拆单建议" not in doc, "函数自述还在承诺下单建议"


def test_header_doc_does_not_present_10pct_as_the_live_rule():
    pej = _load("plan_e_journal_head", "plan_e_journal.py")
    head = pej.__doc__
    assert "单股仓位：默认 10% (10,000)\n" not in head, "头部文档把模拟口径写成了实盘规则"
    assert "退出实盘入口" in head, "头部文档没记录实盘入口已停用"
