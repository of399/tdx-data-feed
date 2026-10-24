"""盘前 `--today` 清单必须只描述"今天"，否则盘前 timer 会把昨天的榜单当今天下单。

三个洞：
1. `plan_e_backtest.py --today` 原本只在**有信号时**写 `data/plan_e/plan_e_today.csv`
   （`if sig.empty: … else: sig.to_csv(...)`）。今天无触发 ⇒ 上一日的文件原地留着，
   mtime 和内容都不动。
2. `plan_e_journal.get_daily_signals()` 跑完子进程后只看 `sig_file.exists()` 就读，
   既不查 subprocess 返回码，也不校验清单里的 `entry_date` 是不是今天。
3. 补上前两道闸后新出现的洞：盘前 08:45 的 timer 写好今日清单，18:10 的 journal 若又跑一次
   CLI —— 那次用的是当日收盘后的数据，算出来的是 entry_date=明天 的榜单，会把今天那份覆盖掉。

合起来的后果就是：盘前跑一次没触发 → journal 读到昨天的 5 只 → 归档成今天的信号 → 打印拆单建议。
测试用 tmp_path + os.utime 隔离与定钟，绝不碰仓库里的真实账本与清单。
"""
from __future__ import annotations

import importlib.util
import inspect
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

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
    pej = _load("plan_e_journal_under_test", "plan_e_journal.py")
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


def _stamp(path: Path, when: str) -> None:
    """把文件 mtime 定到指定日期的 08:45（盘前 timer 的写入时刻）。"""
    ts = datetime.fromisoformat(f"{when} 08:45:00").timestamp()
    os.utime(path, (ts, ts))


def _sig_frame(entry_date: str) -> pd.DataFrame:
    return pd.DataFrame([{
        "code": "600519", "signal_date": "2026-10-22", "t1_pct": 0.1001,
        "t1_vol_ratio": 2.3, "entry_date": entry_date,
        "note": "今日开盘入场（T+2），成交价以开盘后实际为准",
    }])


def _write_list(live, frame: pd.DataFrame, generated_on: str) -> Path:
    live.parent.mkdir(parents=True, exist_ok=True)
    fp = live.parent / "plan_e_today.csv"
    frame.to_csv(fp, index=False)
    _stamp(fp, generated_on)
    return fp


def _rc_ok(monkeypatch, stdout: str = "今日开盘入场: 0 只"):
    """CLI 成功退出但不重写文件——timer 里最常见的两种真相之一。"""
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout, ""))


# ---------- 洞 1：无触发也要把清单落成空，不许留旧文件 ----------

def test_emit_today_signals_writes_even_when_empty(tmp_path):
    pe = _load("plan_e_backtest_under_test", "plan_e_backtest.py")
    out = pe.emit_today_signals(pd.DataFrame(), tmp_path)
    assert out.exists(), "无触发时不写文件 ⇒ 上一日的清单原地活着"
    back = pd.read_csv(out)
    assert back.empty, f"空清单写成了非空: {len(back)} 行"
    assert list(back.columns) == pe.TODAY_SIGNAL_COLS, "空表没表头，下游 read_csv 直接炸"


def test_emit_today_signals_keeps_rows_when_triggered(tmp_path):
    pe = _load("plan_e_backtest_under_test", "plan_e_backtest.py")
    out = pe.emit_today_signals(_sig_frame("2026-10-26"), tmp_path)
    back = pd.read_csv(out, dtype={"code": str})
    assert list(back["code"]) == ["600519"]
    assert "daily" not in back.columns


def test_main_uses_the_single_emit_seam():
    """main() 里必须走 emit_today_signals，否则 if/else 分叉会重新长回来。"""
    pe = _load("plan_e_backtest_under_test", "plan_e_backtest.py")
    src = inspect.getsource(pe.main)
    assert "emit_today_signals" in src, "--today 还在分支里决定写不写文件"


# ---------- 洞 2：journal 只吃当天的清单 ----------

def test_daily_signals_rejects_yesterday_list(monkeypatch, tmp_path):
    """清单既不是今天写的、entry_date 也不是今天 ⇒ 一个字都不吃。"""
    pej, live = _isolate_journal(tmp_path)
    _write_list(live, _sig_frame("2026-10-23"), generated_on="2026-10-23")
    _rc_ok(monkeypatch)

    sig = pej.get_daily_signals("2026-10-26")
    assert sig.empty, "把 2026-10-23 入场的清单当成 2026-10-26 的信号用了"
    assert not (live / "daily_signals" / "signals_20261026.csv").exists(), "过期清单被归档成今日信号"


def test_daily_signals_rejects_failed_subprocess(monkeypatch, tmp_path):
    """子进程非零退出时，磁盘上残留的旧清单绝不能被读成今日信号。"""
    pej, live = _isolate_journal(tmp_path)
    _write_list(live, _sig_frame("2026-10-23"), generated_on="2026-10-23")
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(
                            a, 1, "", "Traceback: freshness guard failed"))

    sig = pej.get_daily_signals("2026-10-26")
    assert sig.empty, "rc=1 仍然读了旧文件"


# ---------- 洞 3：盘前那份清单是当天的正解，晚间不许再覆盖 ----------

def test_daily_signals_reuses_pre_market_list(monkeypatch, tmp_path):
    """盘前 08:45 已写好今日清单，18:10 那次不该再跑 CLI 把它换成明天的。"""
    pej, live = _isolate_journal(tmp_path)
    _write_list(live, _sig_frame("2026-10-26"), generated_on="2026-10-26")

    def boom(*a, **k):
        raise AssertionError("盘前清单还在，却又跑了 plan_e_backtest --today")
    monkeypatch.setattr(subprocess, "run", boom)

    sig = pej.get_daily_signals("2026-10-26")
    assert list(sig["code"].astype(str)) == ["600519"]
    assert (live / "daily_signals" / "signals_20261026.csv").exists(), "今日清单没归档"


def test_daily_signals_trusts_fresh_empty_list(monkeypatch, tmp_path):
    """今天生成、但确实无触发的空表就是今日答案，不该再跑一次 CLI。"""
    pe = _load("plan_e_backtest_under_test", "plan_e_backtest.py")
    pej, live = _isolate_journal(tmp_path)
    out = pe.emit_today_signals(pd.DataFrame(), live.parent)
    _stamp(out, "2026-10-26")

    def boom(*a, **k):
        raise AssertionError("今日空清单已经是答案，却又跑了 CLI")
    monkeypatch.setattr(subprocess, "run", boom)

    assert pej.get_daily_signals("2026-10-26").empty


def test_daily_signals_regenerates_stale_empty_list(monkeypatch, tmp_path):
    """上一日留下的空表不能顶替今天：必须重跑 CLI。"""
    pej, live = _isolate_journal(tmp_path)
    pe = _load("plan_e_backtest_under_test", "plan_e_backtest.py")
    out = pe.emit_today_signals(pd.DataFrame(), live.parent)
    _stamp(out, "2026-10-23")
    ran = []

    def fake_run(*a, **k):
        ran.append(a[0])
        fresh = pe.emit_today_signals(_sig_frame("2026-10-26"), live.parent)
        _stamp(fresh, "2026-10-26")
        return subprocess.CompletedProcess(a, 0, "今日开盘入场: 1 只", "")
    monkeypatch.setattr(subprocess, "run", fake_run)

    sig = pej.get_daily_signals("2026-10-26")
    assert ran, "拿昨天的空表当今日无触发"
    assert list(sig["code"].astype(str)) == ["600519"]
