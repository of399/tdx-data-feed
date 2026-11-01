"""把蒸馏产物 data/distill/full.jsonl 转换为 LLaMA-Factory 训练集。
用法: venv/bin/python scripts/convert_distill.py [--name stock_10k]
"""
import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # 仓库根：让 tdxfeed.atomic 可导入


SRC = "/home/jiuben/tdx-data-feed/data/distill/full.jsonl"
LF_DATA = "/home/workbuddy/lf_project/data"
INFO_JSON = os.path.join(LF_DATA, "dataset_info.json")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", default="stock_10k")
    args = ap.parse_args()
    name = args.name
    rows, empty = [], 0
    with open(SRC, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            if not d.get("output"):
                empty += 1
                continue
            rows.append({"instruction": d["instruction"], "input": d["input"], "output": d["output"]})
    print(f"读取 {SRC}: 有效 {len(rows)} 条, 空 output 跳过 {empty} 条")
    out_json = os.path.join(LF_DATA, name + ".json")
    with open(f"{out_json}.tmp", "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=1)
    os.replace(f"{out_json}.tmp", out_json)
    print("写出 ->", out_json, f"({len(rows)} 条)")
    with open(INFO_JSON, encoding="utf-8") as f:
        info = json.load(f)
    info[name] = {
        "file_name": name + ".json",
        "formatting": "alpaca",
        "columns": {"prompt": "instruction", "query": "input", "response": "output"},
    }
    with open(f"{INFO_JSON}.tmp", "w", encoding="utf-8") as f:
        json.dump(info, f, ensure_ascii=False, indent=2)
    os.replace(f"{INFO_JSON}.tmp", INFO_JSON)
    print("已注册 dataset_info.json ->", name)

if __name__ == "__main__":
    main()
