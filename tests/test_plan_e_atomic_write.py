"""KI-ATOMIC-001 / KI-ATOMIC-002 / KI-ATOMIC-003 的回归测试。

覆盖：
  - KI-ATOMIC-001  plan_e_backtest.emit_today_signals → plan_e_today.csv 原子落位
  - KI-ATOMIC-002  plan_e_journal.save_positions        → positions.json 原子写 + 备份
  - KI-ATOMIC-003  plan_e_journal.append_journal         → 读-改-写加锁 + 原子落位
"""
from __future__ import annotations

import importlib.util
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

pd = pytest.importorskip("pandas")


def _load(name: str, rel: str):
    """动态加载脚本模块（脚本目录不在包内）。"""
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# --------------------------------------------------------------------------- #
# KI-ATOMIC-001
# --------------------------------------------------------------------------- #
def test_emit_today_signals_is_atomic(tmp_path: Path) -> None:
    mod = _load("plan_e_backtest_t", "scripts/plan_e_backtest.py")
    sig = pd.DataFrame({"code": ["000001"], "signal_date": ["2026-11-01"],
                        "t1_pct": [1.0], "daily": ["x"]})
    fp = mod.emit_today_signals(sig, tmp_path)
    assert fp.exists()
    back = pd.read_csv(fp, dtype={"code": str})
    assert back["code"].tolist() == ["000001"]
    # 无残留临时文件
    assert list(tmp_path.glob("*.tmp")) == []


def test_emit_today_signals_never_torn(tmp_path: Path) -> None:
    """写入期间读者只能看到完整旧值或完整新值，不会读到半截。"""
    mod = _load("plan_e_backtest_t2", "scripts/plan_e_backtest.py")
    out = tmp_path / "plan_e"
    old = pd.DataFrame({"code": ["000001"], "signal_date": ["2026-10-01"],
                        "t1_pct": [1.0]})
    assert len(old) == 1
    mod.emit_today_signals(old, out)
    new = pd.DataFrame({"code": ["000002", "000003"],
                        "signal_date": ["2026-11-01", "2026-11-01"],
                        "t1_pct": [2.0, 3.0]})
    seen: list[int] = []

    def reader() -> None:
        for _ in range(300):
            f = out / "plan_e_today.csv"
            if f.exists():
                try:
                    seen.append(len(pd.read_csv(f, dtype={"code": str})))
                except Exception:
                    seen.append(-1)   # 解析失败 = 读到了半截

    t_read = threading.Thread(target=reader)
    t_read.start()
    for _ in range(20):
        mod.emit_today_signals(new, out)
    t_read.join()
    assert all(n in (1, 2) for n in seen), f"读者读到了半截文件: {set(seen)}"


# --------------------------------------------------------------------------- #
# KI-ATOMIC-002
# --------------------------------------------------------------------------- #
def test_save_positions_is_atomic_and_backed_up(tmp_path: Path) -> None:
    mod = _load("plan_e_journal_t", "scripts/plan_e_journal.py")
    mod.POSITIONS_FILE = tmp_path / "positions.json"
    mod.save_positions({"cash": 100.0, "positions": {}})
    assert mod.POSITIONS_FILE.exists()
    mod.save_positions({"cash": 200.0, "positions": {}})
    baks = list(tmp_path.glob("positions.json.bak-*"))
    assert len(baks) == 1
    assert "100.0" in baks[0].read_text(encoding="utf-8")
    assert "200.0" in mod.POSITIONS_FILE.read_text(encoding="utf-8")
    assert list(tmp_path.glob("*.tmp")) == []


def test_save_positions_keeps_original_on_failure(tmp_path: Path) -> None:
    """写失败时旧账本内容不变。"""
    mod = _load("plan_e_journal_t2", "scripts/plan_e_journal.py")
    target = tmp_path / "positions.json"
    mod.POSITIONS_FILE = target
    mod.save_positions({"cash": 1.0, "positions": {}})
    orig = Path.write_text

    def boom(self, *a, **k):
        if ".tmp" in self.name:
            raise OSError("disk full")
        return orig(self, *a, **k)

    Path.write_text = boom  # type: ignore[method-assign]
    try:
        with pytest.raises(OSError):
            mod.save_positions({"cash": 2.0, "positions": {}})
    finally:
        Path.write_text = orig  # type: ignore[method-assign]
    assert "1.0" in target.read_text(encoding="utf-8")
    assert list(tmp_path.glob("*.tmp")) == []


# --------------------------------------------------------------------------- #
# KI-ATOMIC-003
# --------------------------------------------------------------------------- #
def test_append_journal_keeps_all_rows(tmp_path: Path) -> None:
    mod = _load("plan_e_journal_t3", "scripts/plan_e_journal.py")
    mod.JOURNAL_FILE = tmp_path / "journal.csv"
    rec = [{"code": "000001", "action": "BUY", "qty": 100}]
    mod.append_journal(rec)
    mod.append_journal([{"code": "000002", "action": "BUY", "qty": 200}])
    back = pd.read_csv(mod.JOURNAL_FILE, dtype={"code": str})
    assert len(back) == 2
    assert set(back["code"]) == {"000001", "000002"}
    assert list(tmp_path.glob("*.tmp")) == []


def test_append_journal_is_locked(tmp_path: Path) -> None:
    """并发追加不丢行（读-改-写必须加锁）。"""
    mod = _load("plan_e_journal_t4", "scripts/plan_e_journal.py")
    mod.JOURNAL_FILE = tmp_path / "journal.csv"

    def worker(i: int) -> None:
        mod.append_journal([{"code": f"00000{i}", "action": "BUY", "qty": 100}])

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    back = pd.read_csv(mod.JOURNAL_FILE, dtype={"code": str})
    assert len(back) == 8, f"并发追加丢行：{len(back)}/8"
