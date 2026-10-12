#!/usr/bin/env python3
"""扫描 daily / adj_factor parquet 的实际 schema 分布，输出 v2 合规率报告。

用法:
    venv/bin/python scripts/check_schema.py            # 详细报告
    venv/bin/python scripts/check_schema.py --quiet    # 仅异常概览
    venv/bin/python scripts/check_schema.py --json     # JSON 输出（机读）

【目的】
- 验证 daily.{code}.parquet 是否符合 v2 (date date32, 7 列)
- 验证 adj_factor/{code}.parquet 是否符合 v2 (5 列含 code/date/adj_factor/qfq_close/raw_close)
- 输出当前 date 类型三态分布 (int64 / timestamp[us] / timestamp[ns])
- 给 migrate_v1_to_v2.py 提供执行前 baseline
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

import pyarrow.parquet as pq

ROOT = Path(__file__).parent.parent
DAILY_DIR = ROOT / "data" / "parquet" / "daily"
ADJ_DIR = ROOT / "data" / "adj_factor"

DAILY_EXPECTED_COLS = {"date", "open", "high", "low", "close", "volume", "amount"}
ADJ_EXPECTED_COLS = {"code", "date", "adj_factor", "qfq_close", "raw_close"}
DAILY_EXPECTED_DATE_TYPE = "date32[day]"
ADJ_EXPECTED_DATE_TYPE = "date32[day]"


def scan_dir(paths: list[Path], expected_cols: set[str], expected_date_type: str,
             label: str) -> dict:
    """扫描一组 parquet，统计 schema 合规率。"""
    # 过滤掉 .v1.bak 等迁移备份文件（不应计入合规统计）
    paths = [p for p in paths if ".v1.bak" not in p.name]
    total = len(paths)
    type_counter: Counter[str] = Counter()
    col_anomaly: list[str] = []
    date_type_anomaly: list[str] = []
    row_under_200: list[str] = []
    read_fail: list[str] = []
    checked = 0
    date_type_compliant = 0   # date 列为期望类型（date32[day]）
    v2_full_compliant = 0     # 列集合正确 且 date 类型为 date32[day]

    for p in paths:
        try:
            schema = pq.read_schema(p)
            nrows = pq.read_metadata(p).num_rows
            checked += 1
        except Exception as e:
            read_fail.append(f"{p.name}: {type(e).__name__}")
            continue

        # 列检查
        col_set = {f.name for f in schema}
        col_ok = col_set == expected_cols
        if not col_ok:
            col_anomaly.append(f"{p.name}: cols={sorted(col_set)}")

        # date 类型检查
        date_ok = False
        try:
            date_field = next(f for f in schema if f.name == "date")
            date_type = str(date_field.type)
            type_counter[date_type] += 1
            if date_type == expected_date_type:
                date_ok = True
            else:
                date_type_anomaly.append(f"{p.name}: date={date_type}")
        except StopIteration:
            date_type_anomaly.append(f"{p.name}: no 'date' field")

        if date_ok:
            date_type_compliant += 1
        if col_ok and date_ok:
            v2_full_compliant += 1

        # 行数检查
        if nrows < 200:
            row_under_200.append(f"{p.name}: {nrows} rows")

    return {
        "label": label,
        "total_files": total,
        "read_ok": checked,
        "read_fail": len(read_fail),
        "col_anomaly_count": len(col_anomaly),
        "date_type_distribution": dict(type_counter),
        "date_type_anomaly_count": len(date_type_anomaly),
        "date_type_compliant_count": date_type_compliant,
        "date_type_compliant_pct": round(100.0 * date_type_compliant / checked, 2) if checked else 0.0,
        "v2_full_compliant_count": v2_full_compliant,
        "v2_full_compliant_pct": round(100.0 * v2_full_compliant / checked, 2) if checked else 0.0,
        "rows_under_200": len(row_under_200),
        "compliant_pct": round(100.0 * checked / total, 2) if total else 0.0,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="扫描 parquet schema 合规率")
    parser.add_argument("--quiet", action="store_true", help="仅异常概览")
    parser.add_argument("--json", action="store_true", help="JSON 输出")
    args = parser.parse_args()

    daily_paths = sorted(DAILY_DIR.glob("*.parquet"))
    adj_paths = sorted(ADJ_DIR.glob("*.parquet"))

    daily_report = scan_dir(daily_paths, DAILY_EXPECTED_COLS, DAILY_EXPECTED_DATE_TYPE, "daily")
    adj_report = scan_dir(adj_paths, ADJ_EXPECTED_COLS, ADJ_EXPECTED_DATE_TYPE, "adj_factor")

    full_report = {
        "generated_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
        "root": str(ROOT),
        "v2_expected": {
            "daily": {"cols": sorted(DAILY_EXPECTED_COLS), "date_type": DAILY_EXPECTED_DATE_TYPE},
            "adj_factor": {"cols": sorted(ADJ_EXPECTED_COLS), "date_type": ADJ_EXPECTED_DATE_TYPE},
        },
        "daily": daily_report,
        "adj_factor": adj_report,
    }

    if args.json:
        print(json.dumps(full_report, ensure_ascii=False, indent=2))
        return

    print("=" * 70)
    print("TDX data schema v2 合规率扫描")
    print("=" * 70)
    print(f"扫描时间: {full_report['generated_at']}")
    print(f"项目根:   {ROOT}")
    print()
    print("【v2 期望】")
    print(f"  daily:      cols={sorted(DAILY_EXPECTED_COLS)}  date={DAILY_EXPECTED_DATE_TYPE}")
    print(f"  adj_factor: cols={sorted(ADJ_EXPECTED_COLS)}  date={ADJ_EXPECTED_DATE_TYPE}")
    print()

    for rep in (daily_report, adj_report):
        print(f"## {rep['label']}  ({rep['total_files']} files)")
        print(f"  读取成功:     {rep['read_ok']}")
        print(f"  读取失败:     {rep['read_fail']}")
        print(f"  列异常:       {rep['col_anomaly_count']}")
        print(f"  date 类型分布: {rep['date_type_distribution']}")
        print(f"  date 类型异常: {rep['date_type_anomaly_count']}")
        print(f"  date 类型合规: {rep['date_type_compliant_count']}/{rep['read_ok']} ({rep['date_type_compliant_pct']}%)")
        print(f"  v2 完整合规:   {rep['v2_full_compliant_count']}/{rep['read_ok']} ({rep['v2_full_compliant_pct']}%)")
        print(f"  rows<200:     {rep['rows_under_200']}")
        print(f"  读取成功率:    {rep['compliant_pct']}%")
        print()


if __name__ == "__main__":
    main()
