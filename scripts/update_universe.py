#!/usr/bin/env python3
"""`data/all_a_stocks.json` 的**单一写入者**。

为什么要有这个文件：读侧 `tdxfeed/names.py` 一直声称名单「由 `scripts/download_universe.py`
每日更新」，但那个脚本只写日线、一个字的名单都不写 ⇒ 那句话是假声明（D6 类失效引用）。
本文件把「谁写」这一格补成真事实，守卫见 `tests/test_universe_writer.py`。

取名通道**不新造**：只走仓库已有的那一个入口 ——
`akshare.stock_info_a_code_name()`，与 `tdxfeed/full_sync.py::get_all_codes`、
`tdxfeed/symbols.py::get_security_list` 同源（那两处只用它的 `code` 列，本文件把
`name` 列也接上，因为读侧契约就是 `{代码: 名称}`）。

与面板 / ST 的关系（读它之前先看这段）：
  - 本文件产出的是**当日挂牌标的 + 当日名称**，它回答「今天有哪些票、今天叫什么」；
  - 它**不是** ST 历史，也不是 ST 状态的权威：按日 ST 状态在
    `data/st_history/st_events.parquet`，经 `tdxfeed/st_supply.py` 供给（KI-DATA-008）；
  - 名称里的 "ST" 只用于 `names.is_st_name` 的**当日**判定（predict 的实时 ST 二检走这条），
    拿它回溯历史会重演 KI-DATA-008 的错（名称是当前值，不带日期轴）。

写盘纪律：先备份到 `data/_backups/<YYYYMMDD>/`，再经 `tdxfeed.atomic` 换 inode 落位（原子写
只有仓库那一份实现，SEM001 判的就是绕过共享出口自己 tmp→replace）；
闸门（缩表 / 暴涨 / 空名率）不过就**拒写并非零退出**，原文件不动 —— 照
`tdxfeed/adj_factor.py` 的 `fail:shrink` 先例，静默缩表比不更更糟。
入口是 `make data-universe`（手工触发，**没有 timer 调度**）；`make data-universe-dry`
只出对账与闸门读数。容差要临时放宽走 make 的变量（`UNIVERSE_MAX_RISE_PCT=10`），
别改文件里的默认值——默认值管的是「一天内正常变化」，放宽是一次性补课。
"""
from __future__ import annotations

import argparse
import atexit
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tdxfeed import names, paths
from tdxfeed.atomic import atomic_write_bytes

# 吞错计数（SEM002）：键名 = 吞错所在的真函数名
_ERROR_COUNT: Counter[str] = Counter()
atexit.register(lambda: [print(f"[吞错] {k}={v}", file=sys.stderr)
                         for k, v in sorted(_ERROR_COUNT.items()) if v])

SOURCE_ENTRY = "akshare.stock_info_a_code_name"
# 默认容差：名单一天内不该动超过 2% 少 / 5% 多（实测读数见模块末尾 `main` 的打印）
MAX_DROP_PCT = 2.0
MAX_RISE_PCT = 5.0
MIN_NAMED_RATIO = 0.9
# 落盘形状 = 现文件逐字节：indent=2 + sort_keys + ensure_ascii=False + 末尾换行
# （实测 2026-11-06 用这三条参数能把 143,556 字节的现文件逐字节复现出来，
#   所以「无变化的刷新」不会造成假 diff。）
INDENT = 2


def dump_bytes(mapping: dict[str, str]) -> bytes:
    """名单的唯一序列化形状（读侧不关心，但备份/对账要靠它判「真变了没有」）。"""
    return (json.dumps(mapping, ensure_ascii=False, indent=INDENT, sort_keys=True)
            + "\n").encode("utf-8")


def build_payload(source: dict[str, str]) -> dict[str, str]:
    """源清单 → 读侧契约 `{6 位数字: str}`。

    归一只用权威实现（`execution.limits.strip_exchange_prefix`，契约 B），
    不在这里再写一份剥前缀逻辑；非数字键（ETF/空串/表头）丢掉并计数——丢多少要打出来，
    静默丢键会让下一轮以为「源本来就这么少」。
    """
    from tdxfeed.execution import limits as _limits

    out: dict[str, str] = {}
    dropped = 0
    for raw_code, raw_name in source.items():
        code = _limits.strip_exchange_prefix(str(raw_code).strip().lower()).zfill(6)
        if not code.isdigit():
            dropped += 1
            continue
        out[code] = str(raw_name).strip()
    if dropped:
        print(f"[建表] 丢掉非数字键 {dropped} 条", file=sys.stderr)
    return out


def fetch_source() -> dict[str, str]:
    """唯一的取名入口：`ak.stock_info_a_code_name()`（与 full_sync / symbols 同一函数）。

    失败一律抛出去，由 `main` 转成非零退出——这里不兜底、不返回旧名单，
    否则「源挂了」会被写成「名单没变」。
    """
    import akshare as ak

    df = ak.stock_info_a_code_name()
    return {str(c): str(n) for c, n in zip(df["code"].tolist(), df["name"].tolist(), strict=True)}


def write_universe(new: dict[str, str], *, target: Path,
                   backup_root: Path) -> Path | None:
    """先备份旧字节，再经 `tdxfeed.atomic` 换 inode 写新内容。返回备份文件路径。

    备份在前：`data/` 不在 git，覆写不可回滚是唯一真实的危险；换 inode 在后：
    与 `tdxfeed.parquet_io.write_table` 同一语义，读者不会看到半截文件。
    原子写只用仓库那一份实现（`atomic_write_bytes` = 同目录 tmp + `os.replace`），
    不在这里手搓第二遍——SEM001 判的正是「有没有绕过共享出口」。
    内容与现文件逐字节相同则不写（也不留备份），避免每天产生一份假变更。
    """
    payload = dump_bytes(new)
    if target.exists() and target.read_bytes() == payload:
        print(f"[写盘] 内容与现文件逐字节相同 ⇒ 不写不备份：{target}")
        return None
    backup: Path | None = None
    if target.exists():
        stamp = datetime.now()
        backup = (backup_root / stamp.strftime("%Y%m%d")
                  / f"all_a_stocks-{stamp.strftime('%H%M%S')}.json")
        atomic_write_bytes(backup, target.read_bytes())
        print(f"[备份] 旧文件 {backup.stat().st_size:,} 字节 → {backup}")
    atomic_write_bytes(target, payload)
    return backup


def reconcile(new: dict[str, str], old: dict[str, str]) -> dict[str, object]:
    """对账四个数 + 逐条差异样本（纯读数，不判对错）。"""
    added = sorted(set(new) - set(old))
    removed = sorted(set(old) - set(new))
    renamed = sorted(c for c in set(new) & set(old) if new[c] != old[c])
    return {"n_old": len(old), "n_new": len(new),
            "added": added, "removed": removed, "renamed": renamed}


def check_gates(new: dict[str, str], old: dict[str, str], *,
                max_drop_pct: float = MAX_DROP_PCT,
                max_rise_pct: float = MAX_RISE_PCT,
                min_named_ratio: float = MIN_NAMED_RATIO) -> list[str]:
    """返回拒写理由清单（空 = 可写）。只判形状，不判内容对错。"""
    reasons: list[str] = []
    n_old, n_new = len(old), len(new)
    if not n_new:
        reasons.append("闸门：新名单 0 条 ⇒ 拒写")
        return reasons
    if n_old:
        drop_pct = (n_old - n_new) / n_old * 100.0
        rise_pct = (n_new - n_old) / n_old * 100.0
        if drop_pct > max_drop_pct:
            reasons.append(f"闸门：缩表 -{drop_pct:.2f}%（{n_old} → {n_new} 只，"
                           f"容差 {max_drop_pct}%）⇒ 拒写")
        if rise_pct > max_rise_pct:
            reasons.append(f"闸门：暴涨 +{rise_pct:.2f}%（{n_old} → {n_new} 只，"
                           f"容差 {max_rise_pct}%）⇒ 拒写")
    named = sum(1 for v in new.values() if str(v).strip())
    ratio = named / n_new
    if ratio < min_named_ratio:
        reasons.append(f"闸门：有名称的行只占 {ratio:.2%}（下限 {min_named_ratio:.0%}）"
                       f"⇒ 名称列不可用，拒写")
    return reasons


SAMPLE = 12


def _print_reconciliation(rec: dict[str, object]) -> None:
    print(f"[对账] 条目数 {rec['n_old']:,} → {rec['n_new']:,}"
          f"（+{len(rec['added'])} / -{len(rec['removed'])} / 名称变化 {len(rec['renamed'])}）")
    for label, seq in (("新增", rec["added"]), ("删除", rec["removed"])):
        if seq:
            print(f"  {label} {len(seq)} 只（前 {min(len(seq), SAMPLE)} 只）："
                  f"{', '.join(map(str, seq[:SAMPLE]))}"
                  f"{' …' if len(seq) > SAMPLE else ''}")
    if rec["renamed"]:
        old = rec["old_snapshot"]
        new = rec["new_snapshot"]
        seq = rec["renamed"]
        print(f"  改名 {len(seq)} 只（前 {min(len(seq), SAMPLE)} 只）："
              + ", ".join(f"{c}:{old[c]}→{new[c]}" for c in seq[:SAMPLE])
              + (" …" if len(seq) > SAMPLE else ""))


def main(argv: list[str] | None = None, *, fetch=None) -> int:
    """退出码：0 已写/无需写 · 1 源不可用 · 2 闸门拒写。绝不返回「静默成功」。"""
    ap = argparse.ArgumentParser(description="刷新全 A 名单（唯一写入者）")
    ap.add_argument("--out", default=None,
                    help="名单文件路径，默认取 tdxfeed.paths.universe_path()")
    ap.add_argument("--backup-root", default=None,
                    help="备份根目录，默认 <名单所在目录>/_backups")
    ap.add_argument("--dry-run", action="store_true", help="只出对账与闸门读数，不写盘")
    ap.add_argument("--max-drop-pct", type=float, default=MAX_DROP_PCT,
                    help=f"条目数减少的容忍上限（%%），默认 {MAX_DROP_PCT}")
    ap.add_argument("--max-rise-pct", type=float, default=MAX_RISE_PCT,
                    help=f"条目数增加的容忍上限（%%），默认 {MAX_RISE_PCT}")
    ap.add_argument("--min-named-ratio", type=float, default=MIN_NAMED_RATIO,
                    help=f"有名称行的占比下限，默认 {MIN_NAMED_RATIO}")
    args = ap.parse_args(argv)

    target = Path(args.out) if args.out else paths.universe_path()
    backup_root = Path(args.backup_root) if args.backup_root else target.parent / "_backups"

    try:
        source = (fetch or fetch_source)()
    except Exception as e:  # SEM002：计数后转成非零退出，不静默沿用旧名单
        _ERROR_COUNT['main'] += 1
        print(f"[拒写] 取名源不可用（{SOURCE_ENTRY}）：{type(e).__name__}: {e}",
              file=sys.stderr)
        return 1
    if not source:
        print(f"[拒写] 源返回 0 条（{SOURCE_ENTRY}）⇒ 名单不会被清空", file=sys.stderr)
        return 1

    new = build_payload(source)
    old = names.load_universe(target)      # 读旧名单只经读侧唯一入口，不在这里解析 JSON
    rec = reconcile(new, old)
    rec["old_snapshot"], rec["new_snapshot"] = old, new
    _print_reconciliation(rec)
    reasons = check_gates(new, old, max_drop_pct=args.max_drop_pct,
                          max_rise_pct=args.max_rise_pct,
                          min_named_ratio=args.min_named_ratio)
    if reasons:
        for r in reasons:
            print(f"[拒写] {r}", file=sys.stderr)
        return 2
    if args.dry_run:
        print(f"[dry-run] 闸门通过，未写盘：目标 {target}")
        return 0
    write_universe(new, target=target, backup_root=backup_root)
    print(f"[完成] 名单现状：{target}（{len(new):,} 条）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
