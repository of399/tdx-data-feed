"""vault → v5 训练数据生成器
输入:
  - vault 笔记的 ## v4技术面分析 段 + ## v4历史回溯 段
  - parquet OHLCV 数据
  - v3 训练数据 stock_a.json

输出:
  - v5_train.json  (Alpaca 格式)
  - v5_val.json    (验证集 5%)

v5 增量:
  - 从 v4 提取 ~4500 条（4146 vault 段）
  - 与 v3 8085 条合并 → v5 约 12,000 条
"""

import json
import re
from pathlib import Path

import pandas as pd

VAULT_DIR = Path("/home/jiuben/StockVault/01-标的")
PARQUET_DIR = Path("/home/jiuben/tdx-data-feed/data/parquet/daily")
V3_DATA = Path("/home/workbuddy/lf_project/data/stock_a.json")
OUT_DIR = Path("/home/jiuben/tdx-data-feed/data/v5_train")
WINDOW = 20


def parse_date(d) -> str:
    if isinstance(d, pd.Timestamp):
        return d.strftime("%Y-%m-%d")
    if isinstance(d, (int, float)):
        s = str(int(d))
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}" if len(s) == 8 else s
    return str(d)[:10]


def fmt_date_int(d_int) -> str:
    s = str(int(d_int))
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}" if len(s) == 8 else s


def find_vault_note(code: str) -> Path | None:
    for f in VAULT_DIR.iterdir():
        if not f.name.endswith(".md") or "MOC" in f.name:
            continue
        if f.name.startswith(f"{code}-") or f.name.startswith(f"{code} "):
            return f
    return None


def find_parquet(code: str) -> Path | None:
    for prefix in ("sh", "sz", "bj"):
        p = PARQUET_DIR / f"{prefix}{code}.parquet"
        if p.exists():
            return p
    return None


def build_input_json(df_window: pd.DataFrame) -> str:
    """v3 风格 input JSON 字符串"""
    rows = []
    prev_close = None
    for _, row in df_window.iterrows():
        d_str = parse_date(row["date"])
        close = float(row["close"])
        pct = 0.0
        if prev_close:
            pct = round((close - prev_close) / prev_close * 100, 2)
        vol = round(float(row["volume"]) * 100, 0)  # 手→股
        rows.append(
            {
                "日期": d_str,
                "开盘": round(float(row["open"]), 2),
                "收盘": round(close, 2),
                "最高": round(float(row["high"]), 2),
                "最低": round(float(row["low"]), 2),
                "成交量": vol,
                "涨跌幅": pct,
            }
        )
        prev_close = close
    return json.dumps(rows, ensure_ascii=False)


def extract_v4_tech_sections() -> list[dict]:
    """从 vault 提取所有 ## v4技术面分析 · YYYY-MM-DD 段
    返回 [{code, name, date, output_text}]"""
    results = []
    pattern = re.compile(
        r"## v4技术面分析 · (\d{4}-\d{2}-\d{2})\n\n> 模型:.*?\.?\n\n(.+?)(?=\n\n### |\Z)", re.DOTALL
    )
    for f in VAULT_DIR.iterdir():
        if not f.name.endswith(".md") or "MOC" in f.name:
            continue
        m_code = re.match(r"(\d{6})-(.+)\.md", f.name)
        if not m_code:
            continue
        code, name = m_code.group(1), m_code.group(2)
        text = f.read_text(encoding="utf-8")
        for m in pattern.finditer(text):
            date_str, output = m.group(1), m.group(2).strip()
            results.append({"code": code, "name": name, "date": date_str, "output": output})
    return results


def extract_v4_history_sections() -> list[dict]:
    """从 vault 提取 ## v4历史回溯 (Y1-Y2) 表格行
    返回 [{code, name, date, output_text}]"""
    results = []
    pattern = re.compile(r"## v4历史回溯 \(\d{4}-\d{4}\)\n\n.*?\n\n(\|.*?\n)+", re.DOTALL)
    row_pattern = re.compile(r"\| (\d{8}) \| (\d+\.\d+s) \| (.+?) \|")
    for f in VAULT_DIR.iterdir():
        if not f.name.endswith(".md") or "MOC" in f.name:
            continue
        m_code = re.match(r"(\d{6})-(.+)\.md", f.name)
        if not m_code:
            continue
        code, name = m_code.group(1), m_code.group(2)
        text = f.read_text(encoding="utf-8")
        for sec in pattern.finditer(text):
            tbl = sec.group(0)
            for row_m in row_pattern.finditer(tbl):
                date_int = int(row_m.group(1))
                output = row_m.group(3).strip()
                # 还原原 output 文本（去掉 "..." 截断）
                output = output.replace("...", "").strip()
                results.append(
                    {"code": code, "name": name, "date": fmt_date_int(date_int), "output": output}
                )
    return results


def make_alpaca(
    code: str, name: str, as_of_date: str, output: str, df_window: pd.DataFrame
) -> dict | None:
    """构造 Alpaca 格式样本"""
    if df_window is None or len(df_window) < WINDOW:
        return None
    last_date = parse_date(df_window.iloc[-1]["date"])
    instruction = (
        f"你是专业的A股量化分析师。请基于以下截至{last_date}的近{WINDOW}个交易日行情数据，"
        f"对股票 {name}（{code}）做技术面分析并给出操作建议。"
    )
    return {
        "instruction": instruction,
        "input": build_input_json(df_window),
        "output": output,
    }


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # 1. 提取 v4 vault 段
    print("[1] 提取 vault v4 段...")
    tech_sections = extract_v4_tech_sections()
    hist_sections = extract_v4_history_sections()
    print(f"  v4技术面: {len(tech_sections)} 条")
    print(f"  v4历史回溯: {len(hist_sections)} 条")

    # 2. 配 parquet 重建 input
    print("\n[2] 配对 parquet → Alpaca...")
    alpaca_v5 = []
    skipped = 0

    # 加载 v3 训练集（用于补回 input 重建时需要的日期约束）
    v3_data = []
    if V3_DATA.exists():
        with V3_DATA.open() as f:
            v3_data = json.load(f)
    print(f"  v3 训练数据: {len(v3_data)} 条")

    # 处理 v4 技术面段
    for _i, sec in enumerate(tech_sections):
        code = sec["code"]
        as_of_date = sec["date"]
        output = sec["output"]
        parquet = find_parquet(code)
        if not parquet:
            skipped += 1
            continue
        # 转换为 int date
        try:
            year, month, day = as_of_date.split("-")
            date_int = int(f"{year}{month}{day}")
        except Exception:
            skipped += 1
            continue

        df = pd.read_parquet(parquet, columns=["date", "open", "high", "low", "close", "volume"])
        # 兼容 int64 (YYYYMMDD) 和 datetime64
        if pd.api.types.is_integer_dtype(df["date"]):
            df = df[df["date"] <= date_int].reset_index(drop=True)
        else:
            cutoff = pd.Timestamp(as_of_date)
            df = df[df["date"] <= cutoff].reset_index(drop=True)
        if len(df) < WINDOW:
            skipped += 1
            continue
        window_df = df.tail(WINDOW).reset_index(drop=True)
        sample = make_alpaca(code, sec["name"], as_of_date, output, window_df)
        if sample:
            alpaca_v5.append(sample)

    # 处理 v4 历史回溯段
    for sec in hist_sections:
        code = sec["code"]
        as_of_date = sec["date"]
        output = sec["output"]
        parquet = find_parquet(code)
        if not parquet:
            skipped += 1
            continue
        try:
            year, month, day = as_of_date.split("-")
            date_int = int(f"{year}{month}{day}")
        except Exception:
            skipped += 1
            continue

        df = pd.read_parquet(parquet, columns=["date", "open", "high", "low", "close", "volume"])
        # 兼容 int64 / datetime64
        if pd.api.types.is_integer_dtype(df["date"]):
            df = df[df["date"] <= date_int].reset_index(drop=True)
        else:
            cutoff = pd.Timestamp(as_of_date)
            df = df[df["date"] <= cutoff].reset_index(drop=True)
        if len(df) < WINDOW:
            skipped += 1
            continue
        window_df = df.tail(WINDOW).reset_index(drop=True)
        sample = make_alpaca(code, sec["name"], as_of_date, output, window_df)
        if sample:
            alpaca_v5.append(sample)

    print(f"  配对成功: {len(alpaca_v5)} 条 (skipped {skipped})")

    # 3. 合并 v3 + v5 新增
    print("\n[3] 合并 v3 + v5 → 总训练数据...")
    # 用 code 反查 name 修正 v3 instruction
    code_to_name = {}
    for f in VAULT_DIR.iterdir():
        if not f.name.endswith(".md") or "MOC" in f.name:
            continue
        m = re.match(r"(\d{6})-(.+)\.md", f.name)
        if m:
            code_to_name[m.group(1)] = m.group(2)

    combined = []
    for s in v3_data:
        # 提取 code
        m = re.search(r"对股票\s*(\d+)", s["instruction"])
        if m:
            code = m.group(1)
            name = code_to_name.get(code, "未知")
            # 重写 instruction
            ins = re.sub(r"对股票\s*\d+\s*", f"对股票 {name}（{code}） ", s["instruction"])
            combined.append({"instruction": ins, "input": s["input"], "output": s["output"]})
        else:
            combined.append(s)

    combined.extend(alpaca_v5)
    print(f"  合并后: {len(combined)} 条 (v3: {len(v3_data)} + v5新增: {len(alpaca_v5)})")

    # 4. 切分 train/val (95%/5%)
    import random

    random.seed(42)
    indices = list(range(len(combined)))
    random.shuffle(indices)
    split = int(len(indices) * 0.95)
    train_idx = indices[:split]
    val_idx = indices[split:]

    train_data = [combined[i] for i in train_idx]
    val_data = [combined[i] for i in val_idx]

    # 5. 写 v5 文件
    train_path = OUT_DIR / "v5_train.json"
    val_path = OUT_DIR / "v5_val.json"
    with train_path.open("w", encoding="utf-8") as f:
        json.dump(train_data, f, ensure_ascii=False, indent=1)
    with val_path.open("w", encoding="utf-8") as f:
        json.dump(val_data, f, ensure_ascii=False, indent=1)

    print("\n[4] v5 输出:")
    print(f"  训练集: {train_path} ({len(train_data)} 条)")
    print(f"  验证集: {val_path} ({len(val_data)} 条)")

    # 6. 统计
    print("\n[5] 统计:")
    print(f"  v5_train.json: {train_path.stat().st_size / 1024:.1f} KB")
    print(f"  v5_val.json:   {val_path.stat().st_size / 1024:.1f} KB")
    sample = train_data[0]
    print(f"  样本 instruction: {sample['instruction'][:120]}...")
    print(f"  样本 input 长度: {len(sample['input'])} chars")
    print(f"  样本 output 长度: {len(sample['output'])} chars")
    print(f"  样本 output: {sample['output'][:200]}")

    return train_path, val_path


if __name__ == "__main__":
    main()
