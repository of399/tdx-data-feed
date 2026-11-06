#!/usr/bin/env python
"""日期化 ST 事件面板的采集入口（A 任务）：抓 → 落原始 JSONL → 解析 → 写面板 → 报覆盖度。

为什么分两步落盘
----------------
面板是后续 B1（`get_limit` 传 `on_date`）与 B2（daban 重训）的输入，所以抓取与解析必须能
**分开重放**：原始段落先落 `raw/`（`paths.st_f10_ledger_path()`），解析只读本地文件，
网络挂了不必重抓。续跑判据是「raw 里已有 status=ok 的 code」，不是内存计数。

产物落点（全部经 `tdxfeed/paths.py`，业务代码不拼字面量）
------------------------------------------------------
- 事件面板：`paths.st_event_path()` = `data/st_history/st_events.parquet`，与原始台账、覆盖度
  报告同目录归组；列 `code, ann_date, impl_date, action, kind, source, confidence, fetched_at`，
  两个日期列都是 `date32[day]`，只经 `parquet_io.write_table` 写。
  **面板不存原文列**：`raw/f10_ledger.jsonl` 的 `section_lines` 与深市那层的整行就是审计底座，
  再往 parquet 里塞一份是第二处真相；面板只留结构化字段 + `fetched_at` 这个时间锚。
- 累积快照表：`paths.st_board_snapshot_path()` = `data/st_history/st_board_snapshot.parquet`，
  逐日累积 `block_gn` 的 ST 板块成员（`snapshot_date, code, block_name, source`）——这是未来
  逃出「台账只能回溯几条」的唯一手段，历史段结构性盖不到，故写进展与覆盖度而不假装是全量。
- 原始审计基线：`paths.st_raw_dir()`（F10 台账 JSONL 等）与 `paths.st_coverage_path()`。

四个源与它们的真实上限（2026-11-05 本机实测，别把任何一个读成全历史权威）
------------------------------------------------------------------------
- `f10`（→ `source=f10_ledger`）：通达信 F10 的【特别处理】段落，字段 `公告日期/实施日期/处理类型`。
  全市场 5,229 只实测（2026-11-05 本机跑完，失败 0），条数分布
  `{0:4739, 1:182, 2:233, 3:60, 4:11, 5:4}` ⇒ **没有「每票最多 3 条」这个硬上限**（观测最大 5 条）。
  真正压制覆盖率的是**大面积缺席**：**4,739/5,229 = 90.63% 的票这一栏是「暂无数据」**，
  只有 490 只有事件；深度不均而不是被卡在某个条数上。截断的另一组读数：票级孤儿撤销
  **87 只**（最早一条就是撤销）、F10 单层起点不可见 **129 段**、三层并读后 **74 段**；
  最早实施 2014-08-06。整轮 500s / 10.3 票/s。
- `ak_sz`（→ `source=akshare_sz`）：`ak.stock_info_sz_change_name('简称变更')`。
  实测 **7,501 行带变更日期**，其中含 ST 的 **2,283 行 / 673 只深市代码**，日期跨
  **1998-04-28 → 2026-11-04** ⇒ 它是**长**的那一层，但它是「简称变更日」不是「特别处理实施日」。
  与 F10 的 `实施日期` 差多少是 `crosscheck` 量出来的（2026-11-05 全市场实测）：可配对的
  558 条里 **92.1% 精确同一天**，`|差|∈1..7 天` 是 **0 条**，剩下 7.9% 全是 >7 天——那批不是
  「同一事件差几天」，而是**配到了不同轮次**（37/44 是 撤销↔撤销，F10 侧 kind 多为
  `撤销退市风险实施其他`：撤的是退市风险警示、其他风险警示还在，简称变更面板不会记这一笔）。
  沪市没有对应的带日期接口（`ak.stock_info_change_name('600608')` 只回 `index/name`，**无日期**）。
- `block`（→ `source=block_gn`）：`block_gn.dat` 的 ST 板块成员，一次 1,379,375 行 / 1.86s，
  **字段里没有日期列** ⇒ 只能给「今天谁是 ST」一个截面，`kind` 写 `当日成员快照`。
- `infer`（→ `source=price_inferred`）：价格上限反推，复用
  `scripts/audit_daban_st_tier_pollution.py` 的 medium 档判据（**不在这里重抄阈值**）。
  与台账互证的精确率约 0.8 ⇒ 每行必须带 `confidence`，且消费侧必须分层读
  （`st_history.day_states(sources=...)` 会拒绝混读）。

不做什么
--------
不挂 systemd（`tdx-sync.service` 是系统级单元，改 `ExecStartPost` 要 root 部署才生效，本批只做
可重复的手工入口 + `make data-st-history`）；不 import 期设 `socket.setdefaulttimeout`
（AGENTS.md 里 baostock 登录超时那条坑的成因）；连接超时与逐票超时**分成两个数**（30 秒上限
会砍断约 60 秒的握手，同 KI-OPS 那条）；抓取失败逐票计数并在末尾列出，不静默吞。
"""
from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
import math
import re
import sys
import time
from collections import Counter
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for _p in (str(ROOT), str(ROOT / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pandas as pd  # noqa: E402

from tdxfeed import paths, st_history  # noqa: E402
from tdxfeed.atomic import atomic_write_text  # noqa: E402  # SEM001 原子写
from tdxfeed.execution.limits import strip_exchange_prefix  # noqa: E402
from tdxfeed.names import is_st_name, load_universe  # noqa: E402  # 名称/股票池唯一入口
from tdxfeed.sources.tdx_hosts import (  # noqa: E402
    DATA_PROBE_TIMEOUT_SEC,
    DEFAULT_SCAN_LIMIT as SCAN_LIMIT,
    last_stats,
    select_server,
)
from tdxfeed.symbols import INDICES, group, market_of, sym_of_index  # noqa: E402

# 吞错计数（SEM002）：每个被放弃的票/行都要有地方看得见
_ERRORS: Counter[str] = Counter()

DEFAULT_SLEEP = 0.02
INFERENCE_CONFIDENCE = 0.8   # 与台账互证的量级（2,974 票日、41 只样本），不是随手取的数


# --------------------------------------------------------------------------- #
# 宇宙与连接
# --------------------------------------------------------------------------- #
def codes_with_daily_bars() -> list[str]:
    """训练宇宙：active 日线文件的 6 位码，排除与个股同码的指数文件。"""
    skip = {sym_of_index(c) for c in INDICES}
    seen: set[str] = set()
    out: list[str] = []
    for fp in paths.daily_files():
        if fp.stem in skip:
            _ERRORS["index_symbol_excluded"] += 1
            continue
        code = str(strip_exchange_prefix(fp.stem))
        if code not in seen:
            seen.add(code)
            out.append(code)
    return out


def _select_host(*, want: str = "f10") -> dict | None:
    """主机选择**只走** `tdxfeed.sources.tdx_hosts.select_server`。

    原先这里自带一份轮询表（`hq_hosts[:14]` + 逐台 connect），与 `mootdx_source` 是两套
    互不知情的逻辑。现在两边走同一个选择器，且 `want` 按通道分档：F10 段落与 K 线的
    可用性在本环境是**两回事**（2026-11-05 逐台实测前 14 台：connect 6/14、bars 0/14、
    f10 6/14 —— 连得上的那 6 台 F10 都有正文、K 线都是 0 根），
    拿「K 线探活」当判据会把 F10 采集误判成无路，拿「TCP 通」当判据会把日线读成有路。
    """
    sel = select_server(want=want)
    if sel is None:
        _ERRORS["no_reachable_host"] += 1
        print(f"[host] ❌ want={want} 全表探活失败 试了={SCAN_LIMIT} 台以内 统计={last_stats()}",
              flush=True)
        return None
    print(f"[host] 选中 {sel['name']} {sel['ip']}:{sel['port']}（试了 {sel['tried']} 台，"
          f"用时 {last_stats()['elapsed_sec']}s）", flush=True)
    return sel


def connect(api, *, want: str = "f10") -> tuple[str, str] | None:
    """选中主机后把调用方给的 api 连上去；连不上就计数并返回 None。"""
    sel = _select_host(want=want)
    if sel is None:
        return None
    try:
        if api.connect(sel["ip"], sel["port"], time_out=DATA_PROBE_TIMEOUT_SEC):
            return (sel["name"], sel["ip"])
        _ERRORS["connect_false"] += 1
    except Exception as e:  # 连不上就计数，别只打一行
        _ERRORS["connect_error"] += 1
        print(f"[connect-fail] {sel['ip']}: {type(e).__name__} {str(e)[:60]}", flush=True)
    return None


def _section_window(text: str, cap: int = 120) -> list[str]:
    """只留【特别处理】段落的原文行（含结束到下一个段标）。"""
    lines = [ln.strip() for ln in str(text).splitlines() if ln.strip()]
    start = next((i for i, ln in enumerate(lines) if st_history._SECTION_MARK in ln), None)
    if start is None:
        return []
    body: list[str] = [lines[start]]
    for ln in lines[start + 1:]:
        if ln.startswith("【"):
            break
        body.append(ln)
        if len(body) >= cap:
            _ERRORS["section_capped"] += 1
            break
    return body


# --------------------------------------------------------------------------- #
# f10
# --------------------------------------------------------------------------- #
def cmd_f10(args: argparse.Namespace) -> int:
    from pytdx.hq import TdxHq_API

    ledger = paths.st_f10_ledger_path()
    ledger.parent.mkdir(parents=True, exist_ok=True)
    done = set()
    if args.resume:
        for rec in st_history.read_f10_ledger(ledger):
            if rec.get("status") == "ok":
                done.add(str(rec["code"]))
        print(f"续跑：raw 台账已有 {len(done)} 只 status=ok", flush=True)

    codes = args.codes or codes_with_daily_bars()
    todo = [c for c in codes if c not in done]
    if args.limit:
        todo = todo[:args.limit]
    if not todo:
        print("没有待抓的票（都已 status=ok）")
        return 0
    print(f"待抓={len(todo)}  宇宙={len(codes)}  判据=每票 2 个请求  sleep={args.sleep}s", flush=True)

    api = TdxHq_API(heartbeat=False)
    if not connect(api):
        print("❌ 全部服务器都连不上；一只都没抓", flush=True)
        return 2
    ok = empty = fail = 0
    t0 = time.perf_counter()
    with ledger.open("a", encoding="utf-8") as fh:
        for i, code in enumerate(todo, 1):
            mkt = market_of(code)
            rec: dict = {"code": code, "market": mkt,
                         "asof": dt.date.today().isoformat(), "status": "fail",
                         "fetched_at": dt.datetime.now().isoformat(timespec="seconds"),
                         "section_lines": [], "events": []}
            try:
                cats = api.get_company_info_category(mkt, code)
                if not cats:
                    rec["status"] = "no_category"
                    _ERRORS["no_category"] += 1
                else:
                    cat = next((c for c in cats if "提示" in str(c.get("name", ""))), cats[0])
                    txt = api.get_company_info_content(
                        mkt, code, cat["filename"], cat["start"], cat["length"])
                    lines = _section_window(txt or "")
                    rec["section_lines"] = lines
                    events = st_history.parse_f10_section(code, "\n".join(lines))
                    rec["events"] = [{**e, "ann_date": _iso(e["ann_date"]),
                                      "impl_date": _iso(e["impl_date"])} for e in events]
                    rec["status"] = "ok"
                    rec["n_events"] = len(events)
                    ok += 1
                    empty += int(not events)
            except Exception as e:  # 单票失败不中断整轮，但要计数并留痕
                _ERRORS[f"f10_request_{type(e).__name__}"] += 1
                rec["error"] = f"{type(e).__name__}: {str(e)[:80]}"
                fail += 1
                if fail % 50 == 1:
                    print(f"  [fail] {code} {rec['error']}", flush=True)
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            fh.flush()
            if i % args.progress == 0 or i == len(todo):
                el = time.perf_counter() - t0
                print(f"进度 {i}/{len(todo)}  ok={ok} 无段落={empty} 失败={fail} "
                      f"用时={el:.0f}s 速率={i / max(el, 1e-9):.1f} 票/s", flush=True)
            time.sleep(args.sleep)
    try:
        api.disconnect()
    except Exception:  # 断不开就留计数，不影响结果
        _ERRORS["disconnect_failed"] += 1
    print(f"\n完成：ok={ok}（其中无特别处理段落={empty}）失败={fail} "
          f"台账={ledger} 计数={dict(_ERRORS)}", flush=True)
    if fail:
        print(f"⚠️ 失败的票（前 20）：{todo[:min(fail, 20)]}", flush=True)
    return 0 if fail == 0 else 3


def _iso(v) -> str | None:
    return v.isoformat() if isinstance(v, dt.date) else None


def _from_iso(s) -> dt.date | None:
    return dt.date.fromisoformat(str(s)) if s else None


def _snapshot_registry_check() -> dict:
    """「采到日」登记 vs 累积表实况的自洽读数——登记是声称，表是证据，两者必须对得上。

    为什么要把这一项摆出来：上一轮**测试**曾把两个日子（含一个未来的日子）写进生产登记，
    而表根本不在场。这类串味只会让「采到 N 天」被读成「有 N 个可裁定端点」，
    所以不静默修、也不静默报：把三个集合的差算出来计数，让人看得见再决定。
    今天这一版把登记的落点改成跟着表走（`st_history._days_path_for`），测试再也写不到生产。
    """
    table = st_history.read_snapshot()
    reg = set(st_history.captured_days_for(paths.st_board_snapshot_path()))
    in_table = set(table["snapshot_date"]) if len(table) else set()
    today = dt.date.today()
    future = sorted(d for d in reg if d > today)
    if future:
        _ERRORS["_snapshot_registry_check"] += 1
    return {"登记日数": len(reg), "表内日数": len(in_table),
            "登记了但表内无行": len(reg - in_table), "表内有但没登记": len(in_table - reg),
            "晚于今天的登记日": [d.isoformat() for d in future]}


# --------------------------------------------------------------------------- #
# block_gn
# --------------------------------------------------------------------------- #
def cmd_block(args: argparse.Namespace) -> int:
    from pytdx.hq import TdxHq_API

    api = TdxHq_API(heartbeat=False)
    if not connect(api):
        return 2
    t0 = time.perf_counter()
    try:
        rec_rows = api.get_and_parse_block_info("block_gn.dat")
    except Exception as e:  # 拉不到就是拉不到，退码非零并计数（SEM002）
        _ERRORS["block_fetch_failed"] += 1
        print(f"❌ block_gn 拉取失败 {type(e).__name__} {str(e)[:80]}")
        return 3
    el = time.perf_counter() - t0
    try:
        api.disconnect()
    except Exception:  # 断不开就留计数，不影响结果
        _ERRORS["disconnect_failed"] += 1
    if not rec_rows:
        _ERRORS["block_empty"] += 1
        print("❌ block_gn 返回空")
        return 3
    st_codes = sorted({str(r["code"]) for r in rec_rows if str(r["blockname"]) == args.block})
    out = paths.st_block_snapshot_path()
    payload = {"asof": dt.date.today().isoformat(), "block": args.block,
               "total_rows": len(rec_rows), "elapsed_sec": round(el, 2),
               "fields": sorted(rec_rows[0].keys()), "members": st_codes}
    atomic_write_text(out, json.dumps(payload, ensure_ascii=False, indent=1))
    # 累积表：今天的成员并进 `data/st_history/st_board_snapshot.parquet`，并登记「今天采到了」。
    # 这张表是**唯一**能逃出「台账只回溯几条」的手段——历史段结构性盖不到，只能往后攒。
    snap = pd.DataFrame([{"snapshot_date": dt.date.today(), "code": c,
                          "block_name": args.block, "source": "block_gn"}
                         for c in st_codes],
                        columns=list(st_history.SNAPSHOT_COLUMNS))
    if st_codes:
        st_history.append_snapshot(snap)
        days = st_history.captured_days_for(paths.st_board_snapshot_path())
        total = len(st_history.read_snapshot())
        print(f"累积快照：今天并入 {len(st_codes)} 只，表内共 {total:,} 行 / "
              f"采到 {len(days)} 天（{days[0]} → {days[-1]}，断档 "
              f"{st_history.snapshot_gap_days(snap, days=days)} 天）")
        print(f"登记自洽：{_snapshot_registry_check()}")
    else:
        _ERRORS["block_empty_members_skipped_accumulation"] += 1
        print("❌ 成员为空：不并进累积表（坏采集不能当「集体出板块」）")
    print(f"block_gn：总行={len(rec_rows):,} 用时={el:.2f}s 字段={payload['fields']} "
          f"『{args.block}』成员={len(st_codes)} → {out}")
    print(f"计数（未静默）: {dict(_ERRORS) or '无'}")
    return 0


# --------------------------------------------------------------------------- #
# akshare 深市带日期简称变更
# --------------------------------------------------------------------------- #
def cmd_ak_sz(args: argparse.Namespace) -> int:
    """深市带日期简称变更 → `akshare_sz` 事件行（长历史那一层，但日期语义是「变更日」）。"""
    import akshare as ak

    try:
        df = ak.stock_info_sz_change_name(symbol="简称变更")
    except Exception as e:  # 拉不到就退非零，不编数据；计数让调用方看得见（SEM002）
        _ERRORS["ak_fetch_failed"] += 1
        print(f"❌ akshare 简称变更拉取失败 {type(e).__name__} {str(e)[:80]}")
        return 3
    if df is None or df.empty:
        _ERRORS["ak_empty"] += 1
        print("❌ akshare 返回空表")
        return 3
    rows = []
    for r in df.to_dict("records"):
        before, after = str(r.get("变更前简称") or ""), str(r.get("变更后简称") or "")
        # ST 判定走 names 权威层（去排版空格 + 大写），不在这里另抄一份字符串判据
        if not (is_st_name(before) or is_st_name(after)):
            continue
        impl = _from_iso(str(r.get("变更日期"))[:10])
        code = str(r.get("证券代码")).zfill(6)
        if impl is None:
            _ERRORS["ak_bad_date"] += 1
            continue
        if not st_history.CODE_RE.fullmatch(code):
            _ERRORS["ak_bad_code"] += 1
            continue
        rows.append({"code": code, "ann_date": None, "impl_date": impl,
                     "action": "实施" if is_st_name(after) else "撤销",
                     "kind": after or before,
                     # 源的两个简称只进 raw 底座（这一层没有别的地方能重取），不进面板列
                     "name_before": before, "name_after": after,
                     "source": "akshare_sz", "confidence": None})
    out = paths.st_raw_dir() / "akshare_sz_names.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as fh:
        for rec in rows:
            fh.write(json.dumps({**rec, "ann_date": None,
                                 "impl_date": rec["impl_date"].isoformat()},
                                ensure_ascii=False) + "\n")
    n_codes = len({r["code"] for r in rows})
    span = (min(r["impl_date"] for r in rows), max(r["impl_date"] for r in rows)) if rows else None
    print(f"akshare_sz：总表行={len(df):,} 含ST 事件行={len(rows):,} 覆盖代码={n_codes} "
          f"日期跨度={span[0]} → {span[1]} → {out}")
    print(f"计数（未静默）: {dict(_ERRORS) or '无'}")
    return 0


# --------------------------------------------------------------------------- #
# 价格反推（复用审计脚本的 medium 档判据，不在这里重抄阈值）
# --------------------------------------------------------------------------- #
def _audit_module():
    spec = importlib.util.spec_from_file_location(
        "st_price_infer", ROOT / "scripts" / "audit_daban_st_tier_pollution.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def cmd_infer(args: argparse.Namespace) -> int:
    """medium 档价格反推 → `price_inferred` 事件行（带 confidence，主板、并轨前）。

    全市场默认关：不带 `--full` 时只抽 `--limit` 只（默认 200），并且这一步要吃整轮日线读，
    实跑请包在 `systemd-run --user --scope -p MemoryMax=10G -p MemorySwapMax=2G` 里。
    """
    import random

    aud = _audit_module()
    pool = aud._daily_iter(("main",))          # 5% 档只作用于主板；双创/北交所不在这一层
    if args.full:
        chosen = pool
    else:
        random.seed(args.seed)
        chosen = random.sample(pool, min(args.limit or 200, len(pool)))
    print(f"反推范围={len(chosen)} 只（{'全市场' if args.full else '抽样'}）  "
          f"判据=medium {aud.MEDIUM}  confidence={args.confidence}  "
          f"并轨日={aud.RULE_DAY.date()}", flush=True)
    codes_files = chosen
    rows: list[dict] = []
    n_codes: set[str] = set()
    for i, (code, fp) in enumerate(codes_files, 1):
        s = aud._series(code, fp, args.min_rows)
        if s is None or s["n"] < aud.MEDIUM[1]:
            continue
        ceil_, w_, k_ = aud.MEDIUM
        reg = aud._regime5(s["abs_pct"], s["ok"], ceil_, w_, k_)
        at = s["idx"]
        # 样本区段 → 全长度标记
        flags = [bool(reg[pos]) for pos in at]
        dates = [s["date_at"][j] for j in range(len(at))]
        run_start: dt.date | None = None
        for on, d in zip(flags, dates, strict=True):
            day = pd.Timestamp(d).date()
            if day >= aud.RULE_DAY.date():
                if run_start is not None:
                    rows.append({"code": code, "ann_date": None, "impl_date": day,
                                 "action": "撤销", "kind": "price_revoke",
                                 "source": st_history.INFERRED_SOURCE,
                                 "confidence": args.confidence})
                    n_codes.add(code)
                    run_start = None
                break
            if on and run_start is None:
                run_start = day
                rows.append({"code": code, "ann_date": None, "impl_date": day,
                             "action": "实施", "kind": "price_regime5",
                             "source": st_history.INFERRED_SOURCE,
                             "confidence": args.confidence})
                n_codes.add(code)
            elif not on and run_start is not None:
                rows.append({"code": code, "ann_date": None, "impl_date": day,
                             "action": "撤销", "kind": "price_revoke",
                             "source": st_history.INFERRED_SOURCE,
                             "confidence": args.confidence})
                run_start = None
        if run_start is not None:
            _ERRORS["inferred_open_run"] += 1
        if i % args.progress == 0:
            print(f"进度 {i}/{len(codes_files)} 行={len(rows)} 票={len(n_codes)}", flush=True)
    out = paths.st_raw_dir() / "price_inferred.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    note = (f"判据=medium ceil={aud.MEDIUM[0]} w={aud.MEDIUM[1]} k={aud.MEDIUM[2]}"
            f" 并轨日={aud.RULE_DAY.date()} 阈值来源=audit_daban_st_tier_pollution(单源)")
    with out.open("w", encoding="utf-8") as fh:
        for rec in rows:
            # 判据注记只进 raw 底座；面板里这一层靠 `confidence` 表态，不复制原文
            rec["judge_note"] = note
            fh.write(json.dumps({**rec, "impl_date": rec["impl_date"].isoformat()},
                                ensure_ascii=False) + "\n")
    print(f"price_inferred：事件行={len(rows):,} 覆盖票={len(n_codes)} → {out}")
    print(f"计数（未静默）: {dict(_ERRORS)}")
    return 0


# --------------------------------------------------------------------------- #
# reparse：用当前解析器重析 raw 台账（不重打网络）
# --------------------------------------------------------------------------- #
def cmd_reparse(args: argparse.Namespace) -> int:
    """把 raw 台账里的 `section_lines` 用**当前**解析器再析一遍。

    这是「抓取与解析分两步落盘」的全部意义：解析器改了词表、或面板的列收拢了（本轮就是把
    事件字典从 9 键收成 8 键），都不必再打 5,229 次网络。原文件按 `tdxfeed.atomic` 的约定
    留 `.bak` 备份。台账原文（`section_lines`）本身**不改写**，所以面板不存原文也不丢证据。
    """
    src = paths.st_f10_ledger_path()
    if not src.exists():
        print(f"❌ 台账不在场：{src}")
        return 2
    recs: list[dict] = []
    for line in src.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            recs.append(json.loads(line))
        except json.JSONDecodeError:
            _ERRORS["reparse_line_unparsed"] += 1
    changed = with_lines = 0
    for rec in recs:
        if rec.get("status") != "ok" or not rec.get("section_lines"):
            continue
        with_lines += 1
        events = st_history.parse_f10_section(
            str(rec["code"]), "\n".join(rec["section_lines"]))
        new = [{**e, "ann_date": _iso(e["ann_date"]), "impl_date": _iso(e["impl_date"])}
               for e in events]
        if new != (rec.get("events") or []):
            rec["events"] = new
            rec["n_events"] = len(events)
            changed += 1
    print(f"重析：台账记录={len(recs):,} 有段落原文={with_lines:,} 事件被改写={changed:,}"
          f"{'（--dry-run 未落盘）' if args.dry_run else ''}")
    if not args.dry_run:
        body = "\n".join(json.dumps(r, ensure_ascii=False) for r in recs) + "\n"
        atomic_write_text(src, body, backup=True)
        print(f"已落盘 → {src}（原件已备份）")
    print(f"计数（未静默）: {dict(_ERRORS) or '无'}")
    return 0


# --------------------------------------------------------------------------- #
# build：raw → 面板 → 覆盖度
# --------------------------------------------------------------------------- #
def _rows_from_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out: list[dict] = []
    with path.open(encoding="utf-8", errors="ignore") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                _ERRORS["jsonl_line_unparsed"] += 1
    return out


def _daily_file_map() -> dict[str, Path]:
    """code → active 日线文件；指数文件按 `codes_with_daily_bars()` 同一条规则排除。"""
    skip = {sym_of_index(c) for c in INDICES}
    out: dict[str, Path] = {}
    for fp in paths.daily_files():
        if fp.stem in skip:
            continue
        out.setdefault(str(strip_exchange_prefix(fp.stem)), fp)
    return out


def _dates_of_loader() -> tuple[Callable[[str], list[dt.date]], dict]:
    """给 `covered_ticket_days` 用的交易日历回调：只读 date 一列，按代码缓存。

    读不到的代码返回空列表（`covered_ticket_days` 会把它计进「无日历的代码」），
    不返回 None、也不静默——异常按类型计数。
    """
    files = _daily_file_map()
    cache: dict[str, list[dt.date]] = {}

    def dates_of(code: str) -> list[dt.date]:
        if code in cache:
            return cache[code]
        fp = files.get(code)
        vals: list[dt.date] = []
        if fp is None:
            _ERRORS["calendar_no_daily_file"] += 1
        else:
            try:
                s = pd.read_parquet(fp, columns=["date"])["date"]
                vals = sorted({pd.Timestamp(v).date() for v in s})
            except Exception as e:
                _ERRORS[f"calendar_read_{type(e).__name__}"] += 1
        cache[code] = vals
        return vals

    return dates_of, cache


def _file_fetched(path: Path) -> dt.datetime:
    """raw 文件的 mtime = 这一层是什么时候抓的（不编时间戳，用文件系统的事实）。"""
    return dt.datetime.fromtimestamp(path.stat().st_mtime)


def _record_fetched(rec: dict, fallback: dt.datetime) -> dt.datetime:
    """台账行的抓取时刻：新记录带 `fetched_at`；改动之前抓的退到 `asof` 那天 00:00。"""
    raw = rec.get("fetched_at")
    if isinstance(raw, str):
        try:
            return dt.datetime.fromisoformat(raw)
        except ValueError:
            _ERRORS["bad_fetched_at"] += 1
    elif raw is not None:
        _ERRORS["bad_fetched_at"] += 1
    asof = _from_iso(rec.get("asof"))
    return dt.datetime(asof.year, asof.month, asof.day) if asof else fallback


def _quantify_reference(log_path: Path) -> dict:
    """从 `quantify --full` 的真实日志里取出**同一分母口径**的读数，并按同一批档位切段。

    为什么要单独把这一份摆进覆盖度报告：面板自己数出来的翻转只落在「权威段内」，
    而上一轮被拍定的权威中心值（+24.42%）是 quantify 在全市场 16,763,761 行上数的。
    两个数不同源、不同分母，混在一起就会被读成「面板把 24.42% 复算了」。
    这里把 quantify 的分子分母原样搬进来（**不重算、不改写**），并按面板用的同一批
    `st_history.FLIP_BANDS` 把逐年表切成段，这样两臂对照和覆盖度都在同一把尺上。
    """
    if not log_path.exists():
        _ERRORS["_quantify_reference"] += 1
        return {"来源": str(log_path),
                "不可用": "给的日志路径不在场。这项必须是 quantify --full 的真实读数，"
                          "不留空表、也不用面板数顶替。"}
    txt = log_path.read_text(encoding="utf-8", errors="ignore")

    def _num(pat: str) -> int | None:
        m = re.search(pat, txt)
        return int(m.group(1).replace(",", "")) if m else None

    years: dict[int, tuple[int, int, int]] = {}
    for m in re.finditer(r"^\s*(\d{4}):\s*翻转=\s*([\d,]+)\s+现役正=\s*([\d,]+)"
                         r"\s+行数=\s*([\d,]+)", txt, re.M):
        years[int(m.group(1))] = (int(m.group(2).replace(",", "")),
                                  int(m.group(3).replace(",", "")),
                                  int(m.group(4).replace(",", "")))
    head = re.search(r"^(扫描范围=.*)", txt, re.M)
    out: dict = {"来源": str(log_path), "判据行": head.group(1) if head else None,
                 "票数": _num(r"票数=\s*([\d,]+)"),
                 "样本行": _num(r"样本行=\s*([\d,]+)"),
                 "现役正样本": _num(r"现役 y=1=\s*([\d,]+)"),
                 "全历史翻转": _num(r"应翻转=\s*([\d,]+)"),
                 "年份覆盖": [min(years), max(years)] if years else None, "分档": {}}
    if not years:
        _ERRORS["_quantify_reference"] += 1
        out["不可用"] = "日志里没有可解析的逐年翻转表（判据行格式变了？）"
        return out
    for name, lo, hi in st_history.FLIP_BANDS:
        seg = [(f, p, r) for y, (f, p, r) in years.items() if lo <= y <= hi]
        flips = sum(x[0] for x in seg)
        pos = sum(x[1] for x in seg)
        rows = sum(x[2] for x in seg)
        out["分档"][name] = {"翻转": flips, "现役正样本": pos, "样本行": rows,
                             "翻转占现役正样本": (flips / pos) if pos else None,
                             "翻转占段内行": (flips / rows) if rows else None,
                             "覆盖年数": len(seg)}
    tot_flip = sum(x[0] for x in years.values())
    out["逐年合计核对"] = {"逐年翻转之和": tot_flip, "头部读数 应翻转": out["全历史翻转"],
                          "相等": out["全历史翻转"] is None or tot_flip == out["全历史翻转"]}
    if out["全历史翻转"] is not None and tot_flip != out["全历史翻转"]:
        _ERRORS["_quantify_reference"] += 1
    return out


def _flip_vs_quantify(panel: dict | None, quant: dict | None) -> dict:
    """面板段内翻转 ÷ quantify 全市场翻转 = 「权威面板能解释多少污染面」。

    两边的口径差异必须写在结果里，不然这比值会被读成「面板复算了 +24.42%」：
    quantify 的 52,919 是用 **medium 档价格判据**在全市场样本行上数的（= 推断层那一侧的量），
    面板这一侧只数**权威段内、且起点可见**的行，而且 `_flip_measure` 只扫主板。
    所以这一项答的是「臂 A 能覆盖臂 B 的多少」，不是两个数是否相等。
    """
    if not panel or not quant or quant.get("不可用"):
        return {"不可用": "缺 翻转（权威段） 或 --quantify-log，两项都在才算得出对照"}
    ref = {"全历史": {"翻转": quant["全历史翻转"], "现役正样本": quant["现役正样本"],
                      "样本行": quant["样本行"]}}
    ref.update({k: {"翻转": v["翻转"], "现役正样本": v["现役正样本"], "样本行": v["样本行"]}
                for k, v in quant["分档"].items()})
    out: dict = {}
    buckets = {"全历史": panel["全历史"]} | panel["分档"]
    for name, pb in buckets.items():
        qb = ref.get(name)
        if not qb or not qb.get("翻转"):
            out[name] = {"不可用": "quantify 日志没有这一档的逐年数"}
            continue
        out[name] = {"面板权威段翻转": pb["翻转行数"], "quantify 同段翻转": qb["翻转"],
                     "面板可解释占比": pb["翻转行数"] / qb["翻转"],
                     "面板翻转占全市场现役正样本": (pb["翻转行数"] / qb["现役正样本"]
                                                     if qb["现役正样本"] else None),
                     "quantify 翻转占全市场现役正样本": (qb["翻转"] / qb["现役正样本"]
                                                         if qb["现役正样本"] else None)}
    out["口径差"] = {
        "面板侧": "只数权威段内、起点可见的行；`_flip_measure` 只扫主板（5% 档只对主板 ST 成立）",
        "quantify 侧": "全市场样本行，ST 与否由 medium 档价格判据定（推断层那一侧）",
        "所以这个比值答": "臂 A（权威）能把臂 B（含推断）的污染面变成可判定的程度，不是相等性检验"}
    return out


def _arm_delta(hi: dict | None, lo: dict | None) -> dict:
    """臂 A′ 与臂 A 的逐档差 = **`akshare_sz` 这一层到底值多少**（留给用户拍板的那个数）。

    只做减法，不重新判定：两臂的段集来自同一张面板，差的就是「加不加深市简称变更日」。
    """
    if not hi or not lo:
        return {"不可用": "没算翻转（--skip-flips）或缺 quantify 之外的某一臂"}
    out: dict = {"说明": "A′（三层并读）− A（f10_ledger+block_gn）= akshare_sz 那一档的净贡献"}
    names = ["全历史", *hi["分档"].keys()]
    for name in names:
        hb = hi["全历史"] if name == "全历史" else hi["分档"][name]
        lb = lo["全历史"] if name == "全历史" else lo["分档"][name]
        out[name] = {"段内行数差": hb["段内行数"] - lb["段内行数"],
                     "翻转行数差": hb["翻转行数"] - lb["翻转行数"],
                     "票数差": hb["票数"] - lb["票数"]}
    return out


def _flip_measure(states: pd.DataFrame, *, arm: str,
                  cache: dict[str, list] | None = None) -> dict:
    """权威段内的翻转计数：行样本空间与上一批的 quantify **同一条**（复用审计模块的 `_series`）。

    只扫主板：5% 档只对主板 ST 成立，双创/北交所不在这档里（把主板判定值套到双创上
    就是上一轮 +19.2% 被否证的原因）。判定值由 `flip_counts` 现算权威层，不写死。

    `arm` 是**必须带的标签**：`f10_ledger+block_gn`（臂 A）与三层并读（臂 A′）的段集不同，
    不标就会把两个数当成同一个——§13 的两臂对照按的就是这两个分别的数。
    `cache` 传同一个字典可以让第二臂复用第一臂已读过的日线（同一批票，不必重读）。
    """
    aud = _audit_module()
    files = {code: fp for code, fp in aud._daily_iter(("main",))}
    if cache is None:
        cache = {}

    def rows_of(code: str) -> list:
        if code in cache:
            return cache[code]
        out: list = []
        fp = files.get(code)
        if fp is not None:
            s = aud._series(code, fp)
            if s is not None:
                for d, r in zip(s["date_at"], s["ret_next"], strict=True):
                    if r is not None and math.isfinite(r):
                        out.append((pd.Timestamp(d).date(), float(r)))
            else:
                _ERRORS["flip_series_unavailable"] += 1
        else:
            _ERRORS["flip_no_main_board_file"] += 1
        cache[code] = out
        return out

    rep = st_history.flip_counts(states, rows_of=rows_of)
    rep["臂"] = arm
    rep["扫描范围"] = {"主板有日线票": len(files), "该臂覆盖票": int(states["code"].nunique()),
                      "本进程已读票": len(cache)}
    return rep


def cmd_build(args: argparse.Namespace) -> int:
    rows: list[dict] = []
    ledger = st_history.read_f10_ledger()
    ledger_path = paths.st_f10_ledger_path()
    ledger_fb = _file_fetched(ledger_path) if ledger_path.exists() else dt.datetime.now()
    for rec in ledger:
        fetched = _record_fetched(rec, ledger_fb)
        for e in rec.get("events") or []:
            rows.append({"code": str(e["code"]), "ann_date": _from_iso(e.get("ann_date")),
                         "impl_date": _from_iso(e.get("impl_date")), "action": e["action"],
                         "kind": e["kind"], "source": e["source"],
                         "confidence": None, "fetched_at": fetched})
    ak_path = paths.st_raw_dir() / "akshare_sz_names.jsonl"
    ak_fetched = _file_fetched(ak_path) if ak_path.exists() else dt.datetime.now()
    for rec in _rows_from_jsonl(ak_path):
        rec = dict(rec)
        rec["impl_date"] = _from_iso(rec.get("impl_date"))
        rec["fetched_at"] = _record_fetched(rec, ak_fetched)
        rows.append(rec)
    # block_gn 走**累积表**：历史段靠一天一天攒，端点只在「下一个可用采到日」上判撤销
    snap_table = st_history.read_snapshot()
    snap_days = st_history.captured_days_for(paths.st_board_snapshot_path())
    if len(snap_table):
        block_fetched = (_file_fetched(paths.st_board_snapshot_path())
                         if paths.st_board_snapshot_path().exists() else dt.datetime.now())
        blk = st_history.board_events_from_snapshots(snap_table, days=snap_days).to_dict("records")
        for r in blk:
            r["fetched_at"] = block_fetched
        rows.extend(blk)
    if args.include_inferred:
        inf_path = paths.st_raw_dir() / "price_inferred.jsonl"
        inf_fetched = _file_fetched(inf_path) if inf_path.exists() else dt.datetime.now()
        for rec in _rows_from_jsonl(inf_path):
            rec = dict(rec)
            rec["impl_date"] = _from_iso(rec.get("impl_date"))
            rec["fetched_at"] = _record_fetched(rec, inf_fetched)
            rows.append(rec)

    # 面板的列就是这里**唯一**的收拢点：raw 里那些只属于审计底座的键（`section_lines`、
    # `name_before/name_after`、`judge_note`）传不到面板，也不需要在每一处手工剥。
    df = pd.DataFrame(rows, columns=list(st_history.EVENT_COLUMNS))
    # 合并前逐源计数：`by_source` 里的是**存活**行数，被更高优先源吃掉的那部分必须另外报，
    # 否则「akshare 票数 508」会被读成「akshare 只覆盖 508 只」（采集侧是 673 只）。
    pre_merge = {src: {"采集行数": len(sub), "采集票数": int(sub["code"].nunique())}
                 for src, sub in df.groupby("source")}
    # 同一 (code, impl_date, action) 只能有一条（面板的硬判据），跨源同日按权威度择优：
    # f10_ledger（有实施日与处理类型）> akshare_sz（有日期、语义是变更日）> block_gn（当日截面）
    prio = {"f10_ledger": 0, "akshare_sz": 1, "block_gn": 2, st_history.INFERRED_SOURCE: 3}
    before = len(df)
    df = (df.assign(_p=df["source"].map(prio).fillna(9))
            .sort_values(["code", "impl_date", "action", "_p"])
            .drop_duplicates(subset=["code", "impl_date", "action"], keep="first")
            .drop(columns="_p")
            .reset_index(drop=True))
    merged = before - len(df)
    problems = st_history.validate(df)
    if problems:
        print("❌ 面板未通过校验：")
        for p in problems:
            print(f"   - {p}")
        return 4
    out = st_history.write_panel(df)
    name_gap = _name_gap()
    dates_of, cal_cache = _dates_of_loader()
    auth_states = st_history.day_states(df, sources=st_history.AUTHORITY_SOURCES)
    arm_a_states = st_history.day_states(df, sources=("f10_ledger", "block_gn"))
    flip_cache: dict[str, list] = {}
    flips = None if args.skip_flips else _flip_measure(
        auth_states, arm="A′ = f10_ledger + akshare_sz + block_gn", cache=flip_cache)
    # 第二臂**必须**也数一遍：`akshare_sz` 归不归权威层是本轮留给用户拍板的一项
    # （研究文档 `docs/research/2026-11-05-daban-st-tier-label-pollution.md` §13.1），
    # 它值多少翻转行数只有两个数都摆出来才能拍。A′ 先算，A 的票是它的子集，共用缓存不再读盘。
    flips_a = None if args.skip_flips else _flip_measure(
        arm_a_states, arm="A = f10_ledger + block_gn", cache=flip_cache)
    rep = st_history.coverage_report(df, name_gap=name_gap, ledger_records=ledger,
                                     dates_of=dates_of, flips=flips)
    rep["累积快照登记"] = {
        "采到日数": len(st_history.captured_days_for(paths.st_board_snapshot_path())),
        "断档天数": st_history.snapshot_gap_days(
            st_history.read_snapshot(),
            days=st_history.captured_days_for(paths.st_board_snapshot_path())),
        "自洽": _snapshot_registry_check(),
    }
    if args.quantify_log:
        rep["同分母口径（quantify --full）"] = _quantify_reference(Path(args.quantify_log))
    else:
        rep["同分母口径（quantify --full）"] = {
            "不可用": "本次 build 没传 --quantify-log。这项必须是 quantify 在全市场样本行上的"
                      "真实读数，不留空表、也不用面板数顶替。"}
    rep["翻转（臂 A：f10_ledger+block_gn）"] = flips_a
    rep["akshare_sz 这一臂值多少"] = _arm_delta(flips, flips_a)
    rep["翻转口径对照（面板 vs quantify）"] = _flip_vs_quantify(
        flips, rep["同分母口径（quantify --full）"])
    rep["翻转口径对照（臂 A vs quantify）"] = _flip_vs_quantify(
        flips_a, rep["同分母口径（quantify --full）"])
    rep["日历读取"] = {"已读代码数": len(cal_cache), "空日历代码数": sum(
        1 for v in cal_cache.values() if not v)}
    rep["面板行数（去重后）"] = len(df)
    rep["跨源同日合并掉的行数"] = merged
    rep["合并前逐源"] = pre_merge
    rep["面板文件"] = str(out)
    atomic_write_text(paths.st_coverage_path(),
                      json.dumps(rep, ensure_ascii=False, indent=1))
    _print_report(rep)
    return 0


def _name_gap() -> dict:
    """「无名称源的代码数」= 有 active 日线但当日名单里没有的代码。"""
    uni = {str(c) for c in load_universe()}
    codes = set(codes_with_daily_bars())
    return {"有日线但名单里没有该码": len(codes - uni),
            "名单里有但没有 active 日线": len(uni - codes),
            "唯一 6 位码": len(codes), "名单条数": len(uni)}


def _print_report(rep: dict) -> None:
    print("\n=== 覆盖度（逐源分开；推断行绝不与权威行合并计数） ===")
    print(f"{'source':16s} {'行数':>9s} {'票数':>7s} {'实施':>7s} {'撤销':>7s} "
          f"{'票日':>9s} {'段':>6s} {'起点未知':>9s} 日期跨度")
    for src, d in rep["by_source"].items():
        span = "-"
        if d["最早实施"]:
            span = f"{d['最早实施']} → {d['最晚实施']}"
        print(f"{src:16s} {d['行数']:>9,} {d['票数']:>7,} {d['实施事件']:>7,} "
              f"{d['撤销事件']:>7,} {d.get('票日数', 0):>9,} {d.get('段数', 0):>6,} "
              f"{d.get('起点不可见段数', 0):>9,} {span}")
    tot = rep.get("权威层合计")
    if tot:
        span_auth = rep.get("权威层票日可判区间", {})
        print(f"\n权威层合计（三层并读）：票日={tot['票日数']:,}  段数={tot['段数']:,}  "
              f"起点不可见段={tot['起点不可见段数']:,}  无日历代码={tot['无日历的代码数']:,}")
        print(f"  正确口径一句话：**有事件的票 / 有可判区间的票 / 票日 / 段** = "
              f"{rep.get('上限', {}).get('有事件的票数', 0):,} / "
              f"{span_auth.get('票数', 0):,} / {tot['票日数']:,} / {tot['段数']:,}"
              f"（其中起点不可见 {tot['起点不可见段数']:,} 段）——深度不均，不是卡在某个条数上")
        print("  注：票日按区间右开 [实施, 撤销) 数该代码自己的交易日；"
              "开口段算到该代码最后一根日线")
    print(f"  日历读取：{rep.get('日历读取', {})}")
    print("\n=== 上限（这些是面板的边界，不是漏项） ===")
    lim = rep["上限"]
    print(f"  ★ 无【特别处理】段落的票数 = {lim.get('F10 无特别处理段落的票数', 0):,}"
          f"（占 {lim.get('F10 无特别处理段落的占比', 0):.2%}）"
          "  ← 这才是压制覆盖率的那一项，不是「条数被卡在 3」")
    print(f"  有事件的票数              = {lim.get('有事件的票数', 0):,}")
    print(f"  条目数分布                = {lim.get('台账条目数分布', {})}")
    print(f"  条目数最大值              = {lim.get('台账条目数最大值', 0)}"
          f"   恰为 3 的票数 = {lim.get('条目数恰为 3 的票数', 0):,}"
          f"   >3 的票数 = {lim.get('条目数大于 3 的票数', 0):,}")
    print(f"  ≤3 条的票数（含有事件的票）= "
          f"{lim.get('条目数不超过 3 的票数（含有事件的票）', 0):,}")
    if lim.get("上限说明"):
        print(f"  上限说明                  {lim['上限说明']}")
    print(f"  请求失败的票数            = {lim.get('请求失败的票数', 0):,}")
    print(f"  孤儿撤销（最早一条即撤销） = {lim.get('孤儿撤销（最早一条即撤销）票数', 0):,}"
          "   ← 按日期取**最早**那条判（源是新在前，取 events[0] 会数成「当前已不在 ST」）；"
          "这些票的实施端点在台账里永远看不见")
    gap = lim.get("无名称源的代码数", {})
    print(f"  无名称源/名单外代码        = {gap}")
    if lim.get("累积快照表的结构性上限"):
        print(f"  快照表上限                {lim['累积快照表的结构性上限']}")
    snap = rep.get("累积快照登记")
    if snap:
        print("\n=== 累积快照登记（block_gn 逐日累积，历史段盖不到是结构性的） ===")
        print(f"  采到日数={snap['采到日数']:,}  断档天数={snap['断档天数']:,}")
        print("  注：断档=首末采到日之间缺的日子；断档那天下发的「不在板块」不能判成撤销，"
              "`board_events_from_snapshots` 会跳过并计数")
    for fl in (rep.get("翻转（权威段）"), rep.get("翻转（臂 A：f10_ledger+block_gn）")):
        if not fl:
            continue
        print(f"\n=== 翻转（{fl.get('臂', '权威段')}：按 ST 档才成立、按现役档被判 y=0 的行） ===")
        print(f"  判据={fl['判据']}")
        print(f"  扫描范围={fl.get('扫描范围', {})}")
        print(f"  {'口径':10s} {'段内行':>12s} {'翻转行':>10s} {'翻转/段内行':>12s} "
              f"{'段内已达现役档':>14s} {'冲突占比':>10s} {'票数':>7s}")
        buckets = [("全历史", fl["全历史"])] + [(n, fl["分档"][n]) for n in fl["分档"]]
        for name, b in buckets:
            r2 = b["翻转占段内行"]
            r3 = b.get("段内冲突占比")
            print(f"  {name:10s} {b['段内行数']:>12,} {b['翻转行数']:>10,} "
                  f"{('-' if r2 is None else f'{r2 * 100:.2f}%'):>12s} "
                  f"{b['段内已达现役档行数']:>14,} "
                  f"{('-' if r3 is None else f'{r3 * 100:.3f}%'):>10s} {b['票数']:>7,}")
        print("  注：段内已达现役档 = 「区间说这票那天是 ST，价格说它够得着 10% 档」的冲突行；"
              "真面板里它只占段内行 0.08%，所以本表**不给**「翻转÷段内现役正样本」那种比值"
              "（分母太小会得出 13080%），可比的分母见下一节 同分母口径")
    delta = rep.get("akshare_sz 这一臂值多少")
    if delta and not delta.get("不可用"):
        print("\n=== akshare_sz 归不归权威层：这一层单独值多少（留给用户拍板） ===")
        print(f"  {delta['说明']}")
        for name in [k for k in delta if k != "说明"]:
            d = delta[name]
            print(f"  {name:10s} 段内行 +{d['段内行数差']:>9,}  翻转行 +{d['翻转行数差']:>7,}"
                  f"  票 +{d['票数差']:>5,}")
    qr = rep.get("同分母口径（quantify --full）")
    if qr:
        print("\n=== 同分母口径（quantify --full 全市场读数，不是面板数） ===")
        if qr.get("不可用"):
            print(f"  不可用：{qr['不可用']}")
        else:
            print(f"  来源={qr['来源']}")
            print(f"  {qr.get('判据行')}")
            print(f"  票数={qr['票数']:,}  样本行={qr['样本行']:,}  "
                  f"现役正样本={qr['现役正样本']:,}  全历史翻转={qr['全历史翻转']:,}  "
                  f"年份覆盖={qr['年份覆盖']}")
            print(f"  {'段':10s} {'翻转':>9s} {'现役正样本':>12s} {'样本行':>13s} "
                  f"{'翻转/正样本':>12s} {'翻转/段内行':>12s} {'年':>4s}")
            for name, b in qr["分档"].items():
                r1 = b["翻转占现役正样本"]
                r2 = b["翻转占段内行"]
                print(f"  {name:10s} {b['翻转']:>9,} {b['现役正样本']:>12,} "
                      f"{b['样本行']:>13,} "
                      f"{('-' if r1 is None else f'{r1 * 100:.2f}%'):>12s} "
                      f"{('-' if r2 is None else f'{r2 * 100:.4f}%'):>12s} "
                      f"{b['覆盖年数']:>4d}")
            print(f"  逐年合计核对={qr['逐年合计核对']}")
    _print_cmp("面板（A′ 三层并读）", rep.get("翻转口径对照（面板 vs quantify）"))
    _print_cmp("面板（臂 A，不含 akshare_sz）", rep.get("翻转口径对照（臂 A vs quantify）"))
    pre = rep.get("合并前逐源", {})
    if pre:
        print("\n=== 合并前逐源（存活行数见上表；差值=被更高优先源吃掉） ===")
        for src, d in pre.items():
            print(f"  {src:16s} 采集行数={d['采集行数']:>6,}  采集票数={d['采集票数']:>5,}")
    print(f"\n计数（未静默）: {rep['解析计数（未静默）']}")
    print("  注：`day_states` 那一项是**跨多次分层调用累加**的「撤销先于可见实施」次数，"
          "不是票数也不是段数；段级的起点不可见读数看上表的「起点未知」列，"
          "票级（台账里最早一条就是撤销）看「孤儿撤销」一项，三者不同分母。")


def _print_cmp(title: str, cmp: dict | None) -> None:
    """一臂的面板翻转 ÷ quantify 同段翻转。两臂各印一次，读的人才知道 akshare_sz 那一步值多少。"""
    if not cmp:
        return
    print(f"\n=== 翻转口径对照 · {title} 能解释 quantify 污染面的多少 ===")
    if cmp.get("不可用"):
        print(f"  不可用：{cmp['不可用']}")
        return
    for name, d in cmp.items():
        if name == "口径差":
            continue
        if d.get("不可用"):
            print(f"  {name:10s} 不可用：{d['不可用']}")
            continue
        pa = d["面板翻转占全市场现役正样本"]
        qa = d["quantify 翻转占全市场现役正样本"]
        print(f"  {name:10s} 面板段内翻转={d['面板权威段翻转']:>7,} / "
              f"quantify 同段={d['quantify 同段翻转']:>7,} = "
              f"{d['面板可解释占比'] * 100:.2f}%   "
              f"同一现役正样本分母下：面板 {pa * 100:.2f}% vs quantify {qa * 100:.2f}%")
    print(f"  口径差={cmp['口径差']['面板侧']} / quantify 侧={cmp['口径差']['quantify 侧']}")


# --------------------------------------------------------------------------- #
# crosscheck：F10 实施日 vs akshare 变更日（B1 需要知道这两个数差多少）
# --------------------------------------------------------------------------- #
def cmd_crosscheck(args: argparse.Namespace) -> int:
    """F10 的 `实施日期` vs 深市面板的 `变更日期`：B1 用哪个日期得知道差多少。

    两个必须避开的坑（都在这轮实测里踩过）：

    1. **比对用 raw，不用面板**。面板里跨源同 `(code, impl_date, action)` 已按权威度
       合并掉一份（`f10_ledger` 优先），于是「两源同日」那些 0 天差的样本**恰好被合并
       消失了**——拿面板互证会把最该有的证据读成 0 条。raw 在 `data/st_history/raw/` 里
       原样存着，这才是可互证的那两份。
    2. 配对是**每条 F10 事件找同 (code, action) 里日期最近的一条 ak 事件**（有向 1:1），
       不是 `merge(on=["code","action"])` 的笛卡尔积——一只票两段实施 × 三段变更会产出
       6 个配对，中位数会被重复配对拖歪。

    配不上的按交易所分开报：沪市本来就没有带日期的名称源（`ak.stock_info_change_name`
    只回 index/name），那部分是**结构性缺口**，不是分歧。
    """
    f10_rows: list[dict] = []
    latest: dict[str, dict] = {}
    for rec in st_history.read_f10_ledger():
        if rec.get("status") == "ok":
            latest[str(rec["code"])] = rec          # 续跑会追加多条，取每票最后一条 ok
    for rec in latest.values():
        for e in rec.get("events") or []:
            d = _from_iso(e.get("impl_date"))
            if d is None:
                _ERRORS["crosscheck_f10_no_impl_date"] += 1
                continue
            f10_rows.append({"code": str(e["code"]), "action": e["action"], "impl_date": d})

    ak_rows = []
    for rec in _rows_from_jsonl(paths.st_raw_dir() / "akshare_sz_names.jsonl"):
        d = _from_iso(rec.get("impl_date"))
        if d is None:
            _ERRORS["crosscheck_ak_no_impl_date"] += 1
            continue
        ak_rows.append({"code": str(rec["code"]), "action": rec["action"], "impl_date": d})

    if not f10_rows or not ak_rows:
        print(f"两个源都得先抓到才有得比：F10 事件={len(f10_rows)} ak 事件={len(ak_rows)}")
        return 2

    ak_by: dict[tuple[str, str], list] = {}
    for r in ak_rows:
        ak_by.setdefault((r["code"], r["action"]), []).append(r["impl_date"])

    deltas: list[int] = []
    unmatched = Counter()
    for r in f10_rows:
        cands = ak_by.get((r["code"], r["action"]))
        if not cands:
            unmatched[group(r["code"])] += 1
            continue
        best = min(cands, key=lambda d: abs((d - r["impl_date"]).days))
        deltas.append((best - r["impl_date"]).days)

    ser = pd.Series(deltas, dtype="int64")
    print(f"raw 事件：F10={len(f10_rows):,}（{len(latest):,} 只 status=ok） "
          f"akshare_sz={len(ak_rows):,}  可配对={len(ser):,}  "
          f"配对率={len(ser) / len(f10_rows):.1%}")
    print("  配不上（按交易所）：" + "  ".join(
        f"{k}={v:,}" for k, v in sorted(unmatched.items())))
    if len(ser):
        print(f"  差值（ak 变更日 − F10 实施日，自然日）：中位={int(ser.median())}  "
              f"p25={int(ser.quantile(.25))}  p75={int(ser.quantile(.75))}  "
              f"最小={int(ser.min())}  最大={int(ser.max())}")
        for lo, hi, label in ((0, 0, "=0"), (1, 3, "1..3"), (4, 7, "4..7"),
                              (8, 10 ** 9, ">7")):
            n = int(((ser.abs() >= lo) & (ser.abs() <= hi)).sum())
            print(f"  |差|∈{label:6s}: {n:>6,}  ({n / len(ser):.1%})")
        neg = int((ser < 0).sum())
        print(f"  ak 早于 F10 的配对={neg:,}  ak 晚于 F10 的配对={int((ser > 0).sum()):,}")
    print(f"  计数（未静默）：{dict(_ERRORS)}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    ledger = st_history.read_f10_ledger()
    print(f"宇宙={len(codes_with_daily_bars())}  raw 台账记录={len(ledger)} "
          f"其中 status=ok={sum(1 for r in ledger if r.get('status') == 'ok')}")
    panel = paths.st_event_path()
    snap = paths.st_board_snapshot_path()
    print(f"面板在场={panel.exists()}  覆盖度报告在场={paths.st_coverage_path().exists()}")
    print(f"累积快照表在场={snap.exists()}  采到日数="
          f"{len(st_history.captured_days_for(snap))}  快照行数="
          f"{len(st_history.read_snapshot()):,}")
    for name in ("akshare_sz_names.jsonl", "price_inferred.jsonl"):
        p = paths.st_raw_dir() / name
        print(f"  {name}: {'在场' if p.exists() else '缺'}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    def _scan(sp, *, limit=None, progress=200):
        sp.add_argument("--limit", type=int, default=limit, help="只跑前 N 只（默认全市场）")
        sp.add_argument("--min-rows", type=int, default=30)
        sp.add_argument("--progress", type=int, default=progress)

    f = sub.add_parser("f10", help="抓 F10【特别处理】段落 → raw JSONL")
    _scan(f, progress=200)
    f.add_argument("--codes", nargs="*", help="显式代码清单（默认按日线目录枚举）")
    f.add_argument("--sleep", type=float, default=DEFAULT_SLEEP)
    f.add_argument("--no-resume", dest="resume", action="store_false")
    f.set_defaults(resume=True, fn=cmd_f10)

    b = sub.add_parser("block", help="拉 block_gn.dat 的 ST 板块当日成员")
    b.add_argument("--block", default="ST板块")
    b.set_defaults(fn=cmd_block)

    a = sub.add_parser("ak-sz", help="深市带日期简称变更 → akshare_sz 事件行")
    a.set_defaults(fn=cmd_ak_sz)

    i = sub.add_parser("infer", help="价格反推（medium 档）→ price_inferred 事件行")
    _scan(i, progress=500)
    i.add_argument("--full", action="store_true", help="全市场主板（默认关，关时抽 --limit 只）")
    i.add_argument("--seed", type=int, default=42)
    i.add_argument("--confidence", type=float, default=INFERENCE_CONFIDENCE)
    i.set_defaults(fn=cmd_infer)

    rp = sub.add_parser("reparse", help="用当前解析器重析 raw 台账的原文行（不打网络）")
    rp.add_argument("--dry-run", action="store_true", help="只报差异，不落盘")
    rp.set_defaults(fn=cmd_reparse)

    bd = sub.add_parser("build", help="raw → 面板 parquet + 覆盖度报告")
    bd.add_argument("--include-inferred", action="store_true",
                    help="把 price_inferred 行一起进面板（默认不进，避免推断混读成权威）")
    bd.add_argument("--skip-flips", action="store_true",
                    help="不算翻转（省一轮主板读盘；覆盖度里就没有 翻转（权威段） 这一项）")
    bd.add_argument("--quantify-log", default=None,
                    help="quantify --full 的日志路径：把同一分母口径的读数搬进覆盖度报告")
    bd.set_defaults(fn=cmd_build)

    cc = sub.add_parser("crosscheck", help="F10 实施日 vs akshare 变更日的差分布")
    cc.set_defaults(fn=cmd_crosscheck)

    st = sub.add_parser("status", help="看当前抓了多少、面板在不在场")
    st.set_defaults(fn=cmd_status)

    args = p.parse_args()
    rc = args.fn(args)
    if hasattr(args, "fn") and rc == 0:
        print("DONE_COLLECT")
    return rc


if __name__ == "__main__":
    sys.exit(main())
