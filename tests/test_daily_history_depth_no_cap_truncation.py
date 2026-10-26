"""绊线：回填过之后，不该再有「源有更早历史、本地却从近 4 年起」的票。

为什么必须有这条：2026-10-25 之前的整月里，`tdx-verification` 天天红而两套门禁
全绿（`v5/tests/test_math_sympy.py` 的 132 条断言 `make test` 一条也收不到）。截断是
静默的——Sina datalen>1023 与腾讯 count>2000 都返回空而不报错，2026-10-26 又实测
hfq/qfq 的满页顶是 640 而不是 1024。所以「源退化回更短的窗口」这件事只能靠一条
主动比本地档案首行的守卫来发现，而且它必须读**实际写进去的数据**：
只读审计产物会在「runner 报告成功、但翻页提前停了」时照样绿。

三条用例的开关都是「任务 6 的带日期文档存不存在」：文档是「回填完成」这件事的
声明，声明还没发就断言会误伤别的会话（此时 extend 清单本来就还没补）。
"""
from __future__ import annotations

import glob
import inspect
import json
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TARGET = 20000101
DOC = ROOT / "docs" / "daily_history_2000_20261025.md"


def _audit() -> dict | None:
    fs = sorted(glob.glob(str(ROOT / "data" / "audit" / "history_depth_*.json")))
    return json.load(open(fs[-1], encoding="utf-8")) if fs else None


def _done() -> list[str]:
    p = ROOT / "data" / ".state" / "history_backfill.json"
    if not p.exists():
        return []
    return json.load(open(p, encoding="utf-8")).get("done", [])


@pytest.fixture
def backfill_declared():
    """只在「任务 6 已声明回填完成」时生效；文档没落就 skip，别在别人的工作树上假红。"""
    if not DOC.exists():
        pytest.skip(f"{DOC.name} 还没写（任务 6 未执行）")
    return True


def test_audit_product_exists_when_claiming_backfilled(backfill_declared):
    """文档说回填过，就必须拿得出审计产物——否则这条声明不可证伪。"""
    assert _audit() is not None, (
        "文档已声明回填，但没有 data/audit/history_depth_*.json：重跑 "
        "scripts/audit_daily_history_depth.py")


def test_no_extend_left_after_backfill(backfill_declared):
    a = _audit()
    if a is None:
        pytest.skip("没有审计产物（先跑任务 3）")
    leftover = [r["symbol"] for r in a["rows"] if r["state"] == "extend"]
    assert not leftover, f"仍有 {len(leftover)} 只应回填未回填，例：{leftover[:5]}"


def test_backfilled_symbols_actually_reach_the_target(backfill_declared):
    """补过的票，本地首行必须真的 ≤ target——这是分页上限退化时的唯一可见信号。

    runner 报 OK、审计里没有 extend 残留，都可能与「翻页提前停」并存：满页判据一旦
    对不上源的真实上限（1024 / 复权 640 都会漂），循环会当成翻完而 break，首行就停在
    中间某年。所以这里不读产物读结论，直接读写进 parquet 的第一天。
    """
    import pandas as pd

    done = _done()
    if not done:
        pytest.skip("续跑集为空（还没回填过任何一只）")
    shallow = []
    for sym in done:
        p = ROOT / "data" / "parquet" / "daily" / f"{sym}.parquet"
        if not p.exists():
            shallow.append((sym, "文件不存在"))
            continue
        d = pd.to_datetime(pd.read_parquet(p, columns=["date"])["date"])
        first = int(d.min().strftime("%Y%m%d"))
        if first > TARGET:
            shallow.append((sym, first))
    assert not shallow, (
        f"{len(shallow)} 只标了 done 却没到 {TARGET} 之前，例：{shallow[:5]} —— "
        "多半是满页判据与源的真实上限脱钩了（见 PAGE_CAP_BY_ADJUST）")


# amount 估算层（tdxfeed/amount_fix.py + writers.py 的调用）2026-10-26 还只在别的
# 工作树改动里。判据读 Writers.merge_write 函数体：文件在而没接线时用例照样会红。
from tdxfeed.writers import Writers

_AMOUNT_ESTIMATE_WIRED = "fill_missing_amount" in inspect.getsource(Writers.merge_write)


@pytest.mark.skipif(not _AMOUNT_ESTIMATE_WIRED,
                    reason="amount 估算层尚未入库，深取段 amount 就是 0，这条断言无意义")
def test_deep_segment_amount_follows_estimate_convention(backfill_declared):
    """2000 前那段允许是 close×vol×100 的估算值，但不能是有量无额。"""
    import pandas as pd

    done = [s for s in _done() if (ROOT / "data" / "parquet" / "daily" / f"{s}.parquet").exists()]
    if not done:
        pytest.skip("没有回填过的文件")
    bad = []
    for sym in done:
        df = pd.read_parquet(ROOT / "data" / "parquet" / "daily" / f"{sym}.parquet")
        # date 列实测是 object（python date），不是 int：v2 写盘强制 date32，
        # 照 astype("int64") 会拿到 ns 大整数再按 %Y%m%d 解析，必然报错。
        d = pd.to_datetime(df["date"])
        old = df[d.dt.date < date(2000, 1, 1)]
        z = int(((old["amount"] <= 0) & (old["volume"] > 0)).sum())
        if z:
            bad.append((sym, z))
    assert not bad, f"{bad} —— 深取段有量无额，估算层没在这些文件上生效"
