"""hfq 写入侧的跨源对账：`raw_close` 必须与**日线收盘**同值（KI-DATA-004）。

为什么盯这一条
--------------
2026-11-03 的增量把 300386 的 11-02/11-03 两行写成 `raw_close = hfq_close`
（48.45245968 / 47.81968502），而同一天的日线表与前复权表都是 10.72 / 10.58。
按 `hfq_factor = hfq_close / raw_close` 直接塌成 1.0，读侧权威
`tdxfeed.adjusted.frame` 在那两天拿到的"后复权价"退化成不复权价——错 4.5 倍，
而形状、行数、日期序列全都正常，任何按形状检查的守卫都看不见它。

机制没能定论（重查 baostock 当时已回正，源侧瞬态还是两路查询接错都无本地凭据），
所以这里不写"因为 X 所以修 X"，只装一条**与成因无关的不变量**：raw_close 是交易所
价格，日线表就是同一价格的另一个来源，两者必须逐日相等。

范围是测出来的，不是拍的（判据目标：data/.state/hfq_raw_close_mismatch.json）
--------------------------------------------------------------------
全历史逐行比对 16,783,596 行里，容差外 2,339 行 / 461 只标的，按年全部落在 1991–2018
（早期收盘在两个源之间本就有修正差，KI-DATA-003 同源问题），**2019 年起 0 行**。
所以**只比本次取数窗口产出的行**：把整表纳入判据会让每次 rebase 都被 1990 年代的
差异拦住，那是把守卫变成故障源；而近期段本来就没有误判。

容差取「一分或 0.5% 取大」：低价票（2 元）上一分钱就是 0.5%，纯相对容差会把舍入
当缺陷拒掉；而 48.45 对 10.72 那种量级错，两个门槛都拦得住。
"""
from __future__ import annotations

from datetime import date

import pandas as pd

from tdxfeed.hfq_factor import (
    COLUMNS,
    raw_close_mismatches,
    update_one,
)

# 只用 tmp_path 造表 + monkeypatch 日线读取，不读真实行情目录，CI 里真跑。
NEEDS_DATA = False


def _old_df():
    return pd.DataFrame(
        [
            ["300386", date(2026, 10, 29), 4.519819, 10.47, 47.32250493],
            ["300386", date(2026, 10, 30), 4.519819, 9.86, 44.56541534],
        ],
        columns=COLUMNS,
    )


# 事故的真实形状：不复权查询回吐了后复权序列
BAD_RAW = [["2026-11-02", "48.45245968"], ["2026-11-03", "47.81968502"]]
GOOD_RAW = [["2026-11-02", "10.7200"], ["2026-11-03", "10.5800"]]
HFQ = [["2026-11-02", "48.45245968"], ["2026-11-03", "47.81968502"]]
DAILY = {"2026-11-02": 10.72, "2026-11-03": 10.58}


def _wire(monkeypatch, raw_rows, closes):
    """把 _query 换成固定回吐、把日线读取换成一个字典。"""
    import tdxfeed.hfq_factor as m

    def fake_query(bs, code, start, end, flag):
        return HFQ if flag == "1" else raw_rows

    monkeypatch.setattr(m, "_query", fake_query)
    monkeypatch.setattr(m, "_daily_closes", lambda code, dates: dict(closes))
    return m


# ------------------------------------------------------------------ 纯函数层 #
def test_mismatch_detects_the_300386_shape():
    """raw 写成 hfq 值时，纯函数必须点名那一天与两个数。"""
    new = pd.DataFrame(
        [["300386", date(2026, 11, 2), 1.0, 48.45245968, 48.45245968],
         ["300386", date(2026, 11, 3), 1.0, 47.81968502, 47.81968502]],
        columns=COLUMNS,
    )
    out = raw_close_mismatches(new, DAILY)
    assert [str(d) for d, _, _ in out] == ["2026-11-02", "2026-11-03"], (
        f"没抓到塌成 1.0 的两行：{out}"
    )
    # 报错要带两个具体数值，只给日期等于让人去重跑一遍才知道错在哪
    assert out[0][1] == 48.45245968 and out[0][2] == 10.72


def test_match_is_silent():
    new = pd.DataFrame(
        [["300386", date(2026, 11, 2), 4.519819, 10.72, 48.45245968],
         ["300386", date(2026, 11, 3), 4.519819, 10.58, 47.81968502]],
        columns=COLUMNS,
    )
    assert raw_close_mismatches(new, DAILY) == []


def test_one_cent_on_a_cheap_stock_is_not_a_defect():
    """2 元票上一分钱 = 0.5%，纯相对容差会把舍入当缺陷。容差下限是一分钱。"""
    new = pd.DataFrame(
        [["000001", date(2026, 11, 2), 1.0, 2.01, 2.01]], columns=COLUMNS
    )
    assert raw_close_mismatches(new, {"2026-11-02": 2.00}) == [], "一分钱的舍入差不该判错"
    worse = pd.DataFrame([["000001", date(2026, 11, 2), 1.0, 2.03, 2.03]], columns=COLUMNS)
    assert raw_close_mismatches(worse, {"2026-11-02": 2.00}), "三分钱差价必须拦住"


def test_dates_without_a_daily_reference_are_skipped_not_flagged():
    """停牌/新股没有日线行：没有基准不等于不合格，否则整只票会被永久拒写。"""
    new = pd.DataFrame(
        [["300386", date(2026, 11, 2), 1.0, 48.45, 48.45]], columns=COLUMNS
    )
    assert raw_close_mismatches(new, {}) == []


# ---------------------------------------------------------------- 写盘闸门 #
def test_update_one_refuses_raw_close_that_disagrees_with_daily(tmp_path, monkeypatch):
    """事故复现：把这次的两行喂给生产写路径，必须拒绝落盘并原样保留旧文件。"""
    p = tmp_path / "300386.parquet"
    _old_df().to_parquet(p, index=False)
    before = p.read_bytes()
    _wire(monkeypatch, BAD_RAW, DAILY)

    result = update_one(object(), "300386", tmp_path, "2026-11-03")
    assert result.startswith("fail:raw-vs-daily"), (
        f"塌成 1.0 的两行被写成 {result}；这条判据的全部意义就是不让它进表"
    )
    assert "2026-11-02" in result and "48.45" in result, f"报错要点名日期与数值：{result}"
    assert p.read_bytes() == before, "拒绝写入时旧文件必须一字节都不动"
    assert not list(tmp_path.glob("*.tmp*")), "拒绝路径也要清掉临时文件"


def test_update_one_writes_when_daily_agrees(tmp_path, monkeypatch):
    p = tmp_path / "300386.parquet"
    _old_df().to_parquet(p, index=False)
    _wire(monkeypatch, GOOD_RAW, DAILY)

    assert update_one(object(), "300386", tmp_path, "2026-11-03") == "ok:+2"
    out = pd.read_parquet(p)
    assert [round(float(v), 6) for v in out["hfq_factor"].tail(2)] == [4.519819, 4.519819], (
        f"新行因子 = {out['hfq_factor'].tail(2).tolist()}，应为 4.519819（10.72/48.45 那条链）"
    )


def test_reconciliation_only_looks_at_appended_rows(tmp_path, monkeypatch):
    """历史段与日线本就有 0.5% 以上的差（全市场实测 2,339 行 / 461 只，落在 1991–2018）。

    把整表纳入判据的代价是每次 rebase 都被历史差异拦住——那等于用守卫造一个新的停更源。
    这里旧表带一行"历史差异"（1996 年 raw 与日线差 1.4%），增量只追加两行正常数据，
    必须照常写入。
    """
    p = tmp_path / "300386.parquet"
    old = _old_df()
    legacy_row = pd.DataFrame(
        [["300386", date(1996, 4, 4), 1.2, 8.3, 9.96]], columns=COLUMNS
    )
    pd.concat([legacy_row, old], ignore_index=True).to_parquet(p, index=False)
    # 日线里没有 1996 那行（dates 只包含本次新增），所以基准字典里也不给
    _wire(monkeypatch, GOOD_RAW, DAILY)
    assert update_one(object(), "300386", tmp_path, "2026-11-03") == "ok:+2", (
        "只追加两行正常数据却被拒：判据范围扩到了历史行"
    )


def test_missing_daily_table_does_not_block_the_chain(tmp_path, monkeypatch):
    """日线文件不存在（新纳入池/北交所段）时不能判红：宁可不验，也别停掉整条后置任务。

    这一条同时钉住「不验」是可发生的状态而不是异常——`_daily_closes` 回空 dict 就放行。
    """
    p = tmp_path / "300386.parquet"
    _old_df().to_parquet(p, index=False)
    _wire(monkeypatch, BAD_RAW, {})   # 没有基准，哪怕 raw 是错的也放行
    assert update_one(object(), "300386", tmp_path, "2026-11-03") == "ok:+2"


def test_bootstrap_full_build_is_reconciled_too(tmp_path, monkeypatch):
    """补建（整表从零）走同一条判据：新增行就是全表，塌成 1.0 时同样必须拒写。"""
    import tdxfeed.hfq_factor as m

    _wire(monkeypatch, BAD_RAW, DAILY)
    result = update_one(object(), "300386", tmp_path, "2026-11-03")
    assert result.startswith("fail:raw-vs-daily"), f"补建路径绕过了对账：{result}"
    assert not (tmp_path / "300386.parquet").exists(), "被拒的补建不该留下半个文件"
    assert hasattr(m, "raw_close_mismatches")


# ------------------------------------------------------ 取证侧：全市场清单 #
# scripts/audit_hfq_raw_close.py 的产物是台账 KI-DATA-004 的判据目标（文件在场才算
# 「范围有据可查」）。这里用 monkeypatch 喂假扫描结果，测的是**汇总**那一层：
# 计数与条目必须来自同一次扫描，否则清单里的「差异 3,429 行」和 items 会各说各话。
def _audit_module():
    import importlib.util
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "audit_hfq_raw_close_t", root / "scripts" / "audit_hfq_raw_close.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["audit_hfq_raw_close_t"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_inventory_counts_come_from_the_same_scan_as_items(monkeypatch):
    audit = _audit_module()
    fake = {
        "000001": ([{"date": "1992-03-04", "raw_close": 3.1,
                     "daily_close": 3.0, "rel_dev": 0.033}], 500),
        "000002": ([], 400),
        "000003": ([{"date": "1991-07-01", "raw_close": 100.0,
                     "daily_close": 90.0, "rel_dev": 0.111},
                    {"date": "2000-01-05", "raw_close": 8.0,
                     "daily_close": 7.0, "rel_dev": 0.142857}], 300),
    }
    monkeypatch.setattr(audit, "scan_code",
                        lambda code: fake.get(code, ([], 0)))
    monkeypatch.setattr(audit, "hfq_factor_codes", lambda: sorted(fake))

    report = audit.build_report(audit.hfq_factor_codes())
    assert report["mismatch_codes"] == len(report["items"]) == 2
    assert report["mismatch_rows"] == sum(len(v) for v in report["items"].values()) == 3
    assert report["rows_compared"] == 1200, "有基准的行数要单独计，别混成扫描行数"
    assert report["mismatch_by_year"] == {"1991": 1, "1992": 1, "2000": 1}
    # 容差与写入侧同源：清单必须自报用的是哪条判据，读数的人才不用去猜
    assert report["tolerance"]["abs"] == 0.011 and report["tolerance"]["rel"] == 0.005


def test_inventory_survives_a_bad_code_and_reports_it(monkeypatch):
    """读不动的单只记一次数并继续；没有日线基准的单只也要单独计数。

    两个桶都要有：整轮审计的说服力来自「5221 只里我验到了几只」，把没验到的算成
    干净，等于清单自己造假。
    """
    audit = _audit_module()

    def scan(code):
        if code == "900001":
            raise ValueError("坏 parquet")
        if code == "900002":
            return ([], 0)          # 日线文件不存在 → 一行都没比过
        return ([{"date": "1993-01-04", "raw_close": 2.0,
                  "daily_close": 1.0, "rel_dev": 1.0}], 10)

    monkeypatch.setattr(audit, "scan_code", scan)
    report = audit.build_report(["000001", "900001", "900002"])
    assert report["codes_scanned"] == 3
    assert report["codes_read_failed"] == 1, "读失败必须计数，不许静默少一只"
    assert report["codes_without_baseline"] == 1, "没基准的票不能算进「已验证」"
    assert report["rows_compared"] == 10
    assert list(report["items"]) == ["000001"]


def test_inventory_write_leaves_no_temp_file_behind(tmp_path):
    """SEM001：读者不能看到半截清单；写完不留 .tmp（枚举器会把怪名字当标的）。"""
    audit = _audit_module()
    out = tmp_path / ".state" / "hfq_raw_close_mismatch.json"
    audit.write_atomic(out, {"mismatch_rows": 0, "items": {}})
    assert out.exists() and not list(tmp_path.rglob("*.tmp*"))
    import json

    assert json.loads(out.read_text(encoding="utf-8"))["mismatch_rows"] == 0
