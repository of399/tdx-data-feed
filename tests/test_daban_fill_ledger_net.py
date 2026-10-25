"""成交回填：净额只扣**券商回单里的真实费用**，缺费用就不算平仓（设计 §4.3 / §7）。

三个口径互相咬住，写错任何一个，停止线统计出来的就不是这条线能不能赚：

- 委托单发出 ≠ 成交（`docs/DABAN_EXEC_RESEARCH_20261022.md`：成交到的那批封板率 39.7%，
  未成交 95.4%）。所以 `--no-fill` 也是必须记的一行，它只进成交率、不进均值。
- 费用没有真实数字就不臆造（不假设万 2.5 / 单笔 5 元）。缺卖出费用的那笔保持 `closed=0`，
  自然被均值的分母排除，`--report` 会点名有几笔被这样排除。
- 均值的分母是**已平仓** pair；用委托价当成交价会把样本算偏，所以成交价也必须回填。
"""
from __future__ import annotations

import csv
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT), str(ROOT / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from tdxfeed.daban import trial  # noqa: E402
from tdxfeed.daban.order import OrderIntent  # noqa: E402

DAY = "20261026"


def _load_fill():
    fp = ROOT / "scripts" / "daban_fill.py"
    assert fp.exists(), f"{fp} 不存在"
    espec = importlib.util.spec_from_file_location("daban_fill_under_test", fp)
    mod = importlib.util.module_from_spec(espec)
    assert espec.loader
    espec.loader.exec_module(mod)
    return mod


def seed(tmp_path, codes=("600001", "600002")):
    """按试验仓额度写好当日委托单，回填才有对象。"""
    intents = [
        OrderIntent(code=c, name=f"票{i}", side="buy", price=10.0, shares=1000, amount=10000.0,
                    stop_loss=9.5, take_profit=11.0, score=0.6, sector="板块", reason="封单比 1.8")
        for i, c in enumerate(codes)
    ]
    trial.upsert_tickets(tmp_path, DAY, intents, day=trial.load_day(tmp_path, DAY))


def read_rows(tmp_path):
    fp = trial.fills_path(tmp_path, DAY)
    assert fp.exists(), f"没有生成 {fp}"
    return list(csv.DictReader(fp.open(encoding="utf-8")))


def run(mod, tmp_path, *args):
    return mod.main(["--root", str(tmp_path), *args])


def test_fill_then_sell_nets_out_with_real_fees(tmp_path):
    mod = _load_fill()
    seed(tmp_path)

    assert run(mod, tmp_path, "--code", "600001", "--fill", "--price", "10.00",
               "--shares", "1000", "--fee", "5.00") == 0
    assert run(mod, tmp_path, "--code", "600001", "--sell", "--price", "11.00",
               "--fee", "6.00", "--date", "20261027") == 0

    row = next(r for r in read_rows(tmp_path) if r["code"] == "600001")
    # 毛额差 1000 元，扣两边真实回单费用 11 元 ⇒ 净 989 元 / 本金 10000 元 = +9.89%
    assert float(row["net_yuan"]) == 989.0, row
    assert abs(float(row["net_pct"]) - 9.89) < 1e-9, row
    assert int(row["closed"]) == 1


def test_sell_without_a_fee_record_keeps_the_pair_open(tmp_path):
    """没有卖出费用就不算平仓：不臆造费率，宁可让它不进均值。"""
    mod = _load_fill()
    seed(tmp_path)
    run(mod, tmp_path, "--code", "600001", "--fill", "--price", "10.00", "--shares", "1000", "--fee", "5.00")

    assert run(mod, tmp_path, "--code", "600001", "--sell", "--price", "11.00") == 0

    row = next(r for r in read_rows(tmp_path) if r["code"] == "600001")
    assert int(row["closed"]) == 0
    assert row["net_pct"] == ""
    assert row["net_yuan"] == ""


def test_report_names_rows_excluded_for_missing_fees(tmp_path, capsys):
    mod = _load_fill()
    seed(tmp_path)
    run(mod, tmp_path, "--code", "600001", "--fill", "--price", "10.00", "--shares", "1000", "--fee", "5.00")
    run(mod, tmp_path, "--code", "600001", "--sell", "--price", "11.00")

    assert run(mod, tmp_path, "--report") == 0
    printed = capsys.readouterr().out
    assert "1 笔" in printed, printed
    assert "费用" in printed, printed


def test_no_fill_row_enters_the_miss_ratio_not_the_mean(tmp_path):
    """未成交也要记账：丢掉这一半，样本就只剩「买到的恰好是更容易输的那批」。"""
    mod = _load_fill()
    seed(tmp_path)
    run(mod, tmp_path, "--code", "600001", "--no-fill", "--reason", "封死没排到")

    rows = read_rows(tmp_path)
    row = next(r for r in rows if r["code"] == "600001")
    assert int(row["filled"]) == 0 and int(row["closed"]) == 0

    v = trial.evaluate_stop(trial.load_fills(tmp_path))
    assert v.n_closed == 0
    assert v.miss_ratio == 1.0
    assert v.n_backfilled == 0


def test_backfill_requires_a_matching_ticket(tmp_path):
    """回填的 code 必须真在当日委托单里——不许凭空造一笔。"""
    import pytest

    mod = _load_fill()
    seed(tmp_path)
    with pytest.raises(SystemExit) as exc:
        run(mod, tmp_path, "--code", "688888", "--no-fill")
    assert exc.value.code not in (0, None)
    printed_msg = str(exc.value)
    assert "600001" in printed_msg, "错误信息要列出当日委托单的 code 集合"


def test_report_prints_the_paper_line_contrast(tmp_path, capsys):
    """停止线放宽于纸盘的 +0.20% 这件事要在 --report 里对照打印，不能只写在代码注释里。"""
    mod = _load_fill()
    seed(tmp_path)
    run(mod, tmp_path, "--report")
    printed = capsys.readouterr().out
    assert "+0.20" in printed, printed


def test_contrast_line_holds_its_verdict_until_the_judging_threshold(tmp_path, capsys):
    """满判停门槛之前，纸盘口径那句对照也不许下判断——门槛对两个口径同时生效。

    2026-10-25 活体彩排：1 笔平仓就打印出「按纸盘口径当前均值已低于那条线」，读起来是个结论，
    而第 3 条停止线自己要到 `STOP_N_MIN` 笔才判。早期噪声既不能停这条线，也不能给它背书。
    """
    mod = _load_fill()
    seed(tmp_path)
    run(mod, tmp_path, "--code", "600001", "--fill", "--price", "9.00", "--shares", "1000", "--fee", "5.00")
    run(mod, tmp_path, "--code", "600001", "--sell", "--price", "8.00", "--fee", "6.00", "--date", "20261027")

    assert run(mod, tmp_path, "--report") == 0
    printed = capsys.readouterr().out
    assert "未满判停门槛" in printed, printed
    assert "已低于那条线" not in printed, printed
    assert "不低于那条线" not in printed, printed


def test_contrast_line_compares_once_the_sample_is_judgeable(tmp_path, capsys):
    """满了门槛就得真的对照——否则上一条把这句话永久禁掉也没人发现。"""
    mod = _load_fill()
    codes = tuple(f"6000{i:02d}" for i in range(trial.STOP_N_MIN))
    seed(tmp_path, codes)
    for c in codes:
        run(mod, tmp_path, "--code", c, "--fill", "--price", "10.00", "--shares", "1000", "--fee", "5.00")
        run(mod, tmp_path, "--code", c, "--sell", "--price", "11.00", "--fee", "6.00", "--date", "20261027")

    assert run(mod, tmp_path, "--report") == 0
    printed = capsys.readouterr().out
    assert "按纸盘口径当前均值不低于那条线" in printed, printed


def test_miss_ratio_prints_as_one_decimal_percent(tmp_path, capsys):
    """占比用 `:.1%`，跟第 5 条停止线的判据字符串同一个格式，不吐裸浮点。"""
    mod = _load_fill()
    seed(tmp_path, ("600001", "600002", "600003"))
    run(mod, tmp_path, "--code", "600001", "--fill", "--price", "9.00", "--shares", "1000", "--fee", "5.00")
    run(mod, tmp_path, "--code", "600002", "--no-fill", "--reason", "封死没排到")
    run(mod, tmp_path, "--code", "600003", "--no-fill", "--reason", "封死没排到")

    assert run(mod, tmp_path, "--report") == 0
    printed = capsys.readouterr().out
    assert "66.7%" in printed, printed
    assert "0.666666" not in printed, printed


def test_marker_text_prints_both_numbers_at_the_report_precision(tmp_path):
    """marker 是事后翻查的唯一凭据，同一个占比/均值不能一处 `66.7%` 一处裸浮点。"""
    mod = _load_fill()
    seed(tmp_path, ("600001", "600002", "600003"))
    run(mod, tmp_path, "--code", "600001", "--fill", "--price", "9.00", "--shares", "1000", "--fee", "5.00")
    run(mod, tmp_path, "--code", "600001", "--sell", "--price", "8.00", "--fee", "6.00", "--date", "20261027")
    run(mod, tmp_path, "--code", "600002", "--no-fill", "--reason", "封死没排到")
    run(mod, tmp_path, "--code", "600003", "--no-fill", "--reason", "封死没排到")

    marker = trial.evaluate_stop(trial.load_fills(tmp_path)).marker_text
    assert "66.7%" in marker, marker
    assert "-11.233%/笔" in marker, marker
    assert "0.666666" not in marker and "11.2333333" not in marker, marker
