#!/usr/bin/env python3
"""v4fwd 数据集重切分与类别平衡脚本。

数据事实（用户约束）：
    - 数据总体日期范围：2000-01-01 至 2026-10-16
    - 测试集仅包含单日：2026-08-19
    - 验证集覆盖其中 8 天：2026-08-11 至 2026-08-18
    - 训练集样本几乎全部（约 99.98%）集中在 2026-08-11 附近

脚本职责：
    1. 加载原始训练、验证、测试三类文件。
    2. 在保持原始日期边界的前提下，对三个数据集分别做类别平衡。
    3. 将原始数据备份至指定目录后，写入新的训练、验证、测试文件。
    4. 输出每个数据集的类别分布与日期范围。

输入：
    数据目录中包含以下原始文件：
        - 训练文件
        - 验证文件
        - 测试文件
    每个文件为 JSONL 格式，每行一条样本，包含指令、输入、输出三字段。

输出：
    数据目录中生成以下新文件：
        - 训练文件（类别平衡后）
        - 验证文件（类别平衡后）
        - 测试文件（类别平衡后）
        - 验证小文件（验证集的十分之一，用于快速验证）
    原始文件备份至 data/v4fwd/backup_<时间戳> 目录。

用法：
    仅查看分布，不写入文件：
        python 脚本路径 --survey
    实际执行重切分与平衡：
        python 脚本路径
"""
from __future__ import annotations

import argparse
import json
import random
import re
import shutil
from collections import Counter
from datetime import datetime
from pathlib import Path

# === 路径常量 ================================================================
项目根目录 = Path("/home/jiuben/tdx-data-feed")
数据目录 = 项目根目录 / "data" / "v4fwd"
备份目录 = 数据目录 / "backup_2026-10-16"

# 原始文件与输出文件名称
训练文件 = "train.jsonl"
验证文件 = "val.jsonl"
测试文件 = "test.jsonl"
验证小文件 = "val_small.jsonl"

# 日期与类别解析正则表达式
日期正则 = re.compile(r"截至\s*(\d{4}-\d{2}-\d{2})")
类别正则 = re.compile(r"方向：?(涨|跌|震)")

# 三个类别标签
类别涨 = "涨"
类别跌 = "跌"
类别震 = "震"
所有类别 = [类别涨, 类别跌, 类别震]


def 解析日期(样本: dict) -> str | None:
    """从指令字段中解析截至日期。

    参数：
        样本：包含指令、输入、输出三字段的字典。

    返回：
        截至日期字符串，格式为 年-月-日；解析失败返回空值。
    """
    匹配结果 = 日期正则.search(样本.get("instruction", ""))
    return 匹配结果.group(1) if 匹配结果 else None


def 解析类别(输出文本: str) -> str | None:
    """从输出文本中解析方向类别。

    参数：
        输出文本：模型输出字段内容。

    返回：
        类别字符串，取值为 涨、跌、震 之一；解析失败返回空值。
    """
    匹配结果 = 类别正则.search(输出文本)
    return 匹配结果.group(1) if 匹配结果 else None


def 加载所有样本(目录: Path) -> list[dict]:
    """从目录中加载训练、验证、测试三个文件的全部样本。

    每个样本附加截至日期与方向类别两个内部字段，便于后续切分与统计。

    参数：
        目录：包含三个 JSONL 文件的目录路径。

    返回：
        全部有效样本的列表，列表中每个元素是包含截至日期与方向类别的字典。
    """
    全部样本: list[dict] = []
    for 文件名 in [训练文件, 验证文件, 测试文件]:
        文件路径 = 目录 / 文件名
        if not 文件路径.exists():
            continue
        with 文件路径.open("rb") as 文件句柄:
            for 原始行 in 文件句柄:
                # 跳过 UTF-8 文件头标记
                if 原始行.startswith(b"\xef\xbb\xbf"):
                    原始行 = 原始行[3:]
                if not 原始行.strip():
                    continue
                样本 = json.loads(原始行)
                样本["_截至日期"] = 解析日期(样本)
                样本["_方向类别"] = 解析类别(样本.get("output", ""))
                if 样本["_截至日期"] and 样本["_方向类别"]:
                    全部样本.append(样本)
    return 全部样本


def 按时序排序(样本列表: list[dict]) -> list[dict]:
    """按截至日期对样本进行升序排序。

    参数：
        样本列表：待排序的样本列表。

    返回：
        按截至日期升序排列的样本列表。
    """
    return sorted(样本列表, key=lambda 样本: 样本["_截至日期"])


def 报告分布(名称: str, 样本列表: list[dict]) -> None:
    """输出指定数据集的类别分布与日期范围。

    参数：
        名称：数据集名称。
        样本列表：样本列表。
    """
    类别计数 = Counter(样本["_方向类别"] for 样本 in 样本列表)
    样本数 = len(样本列表)
    if 样本数 == 0:
        print(f"  {名称:8s}  样本数=0（无数据）")
        return
    日期列表 = [样本["_截至日期"] for 样本 in 样本列表]
    涨数 = 类别计数.get(类别涨, 0)
    跌数 = 类别计数.get(类别跌, 0)
    震数 = 类别计数.get(类别震, 0)
    print(
        f"  {名称:8s}  样本数={样本数:6d}  "
        f"涨={涨数:5d}({涨数 / 样本数:5.1%})  "
        f"跌={跌数:5d}({跌数 / 样本数:5.1%})  "
        f"震={震数:5d}({震数 / 样本数:5.1%})  "
        f"[{min(日期列表)} ~ {max(日期列表)}]"
    )


def 类别平衡(样本列表: list[dict], 随机种子: int = 42) -> list[dict]:
    """将样本下采样至三类严格 1 比 1 比 1 平衡。

    参数：
        样本列表：待平衡的样本列表。
        随机种子：随机数生成器种子。

    返回：
        平衡后的样本列表，列表长度等于最少类别的样本数乘以三。

    异常：
        若任意类别样本数为零，抛出运行时错误。
    """
    按类别分组: dict[str, list[dict]] = {类别: [] for 类别 in 所有类别}
    for 样本 in 样本列表:
        按类别分组[样本["_方向类别"]].append(样本)
    最少样本数 = min(len(列表) for 列表 in 按类别分组.values())
    if 最少样本数 == 0:
        raise RuntimeError(
            f"无法平衡：某类别样本数为零，"
            f"各类别数：{[(类别, len(按类别分组[类别])) for 类别 in 所有类别]}"
        )
    平衡后样本: list[dict] = []
    for 类别样本 in 按类别分组.values():
        平衡后样本.extend(类别样本[:最少样本数])
    随机生成器 = random.Random(随机种子)
    随机生成器.shuffle(平衡后样本)
    return 平衡后样本


def 写入文件(文件路径: Path, 样本列表: list[dict]) -> None:
    """将样本列表写入指定 JSONL 文件，并剥离内部字段。

    参数：
        文件路径：目标文件路径。
        样本列表：待写入的样本列表。
    """
    with 文件路径.open("w", encoding="utf-8") as 文件句柄:
        for 样本 in 样本列表:
            干净样本 = {
                键: 值 for 键, 值 in 样本.items() if not 键.startswith("_")
            }
            文件句柄.write(json.dumps(干净样本, ensure_ascii=False) + "\n")


def 解析单个文件(文件路径: Path) -> list[dict]:
    """从 JSONL 文件加载样本并解析截至日期与方向类别。

    参数：
        文件路径：JSONL 文件路径。

    返回：
        包含截至日期与方向类别两个内部字段的有效样本列表。
    """
    样本列表: list[dict] = []
    with 文件路径.open("rb") as 文件句柄:
        for 原始行 in 文件句柄:
            if 原始行.startswith(b"\xef\xbb\xbf"):
                原始行 = 原始行[3:]
            if not 原始行.strip():
                continue
            样本 = json.loads(原始行)
            样本["_截至日期"] = 解析日期(样本)
            样本["_方向类别"] = 解析类别(样本.get("output", ""))
            if 样本["_截至日期"] and 样本["_方向类别"]:
                样本列表.append(样本)
    return 样本列表


def 主流程() -> int:
    """脚本主入口。"""
    参数解析器 = argparse.ArgumentParser(
        description="v4fwd 数据集类别平衡（保持原切分）"
    )
    参数解析器.add_argument(
        "--survey",
        action="store_true",
        help="仅查看分布，不写入文件",
    )
    参数解析器.add_argument(
        "--随机种子",
        type=int,
        default=42,
        help="类别平衡时的随机种子（默认 42）",
    )
    参数 = 参数解析器.parse_args()

    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] 开始处理 v4fwd 数据集")

    # 备份原始数据
    if not 参数.survey:
        备份目录.mkdir(parents=True, exist_ok=True)
        for 文件名 in [训练文件, 验证文件, 验证小文件, 测试文件]:
            源文件 = 数据目录 / 文件名
            if 源文件.exists():
                shutil.copy2(源文件, 备份目录 / 文件名)
        print(f"  原始数据已备份至：{备份目录}")

    # 分别处理训练、验证、测试三个文件
    三个集合 = [训练文件, 验证文件, 测试文件]
    三个新文件: dict[str, list[dict]] = {}

    print("\n[1] 原始三类分布：")
    for 文件名 in 三个集合:
        样本列表 = 解析单个文件(数据目录 / 文件名)
        报告分布(文件名, 样本列表)
        三个新文件[文件名] = 样本列表

    print("\n[2] 类别平衡后三类分布：")
    for 文件名 in 三个集合:
        平衡后 = 类别平衡(三个新文件[文件名], 随机种子=参数.随机种子)
        三个新文件[文件名] = 平衡后
        报告分布(文件名, 平衡后)

    # 验证集十分之一作为验证小文件（兼容旧用法）
    print("\n[3] 生成验证小文件（验证集的十分之一）：")
    验证样本 = 三个新文件[验证文件]
    随机生成器 = random.Random(参数.随机种子 + 1)
    验证小样本 = 随机生成器.sample(验证样本, len(验证样本) // 10)
    三个新文件[验证小文件] = 验证小样本
    报告分布(验证小文件, 验证小样本)

    if 参数.survey:
        print("\n-- 仅查看模式：不写入文件")
        return 0

    print(f"\n[4] 写入新文件至：{数据目录}")
    for 文件名, 样本列表 in 三个新文件.items():
        写入文件(数据目录 / 文件名, 样本列表)
        print(f"  已写入 {文件名}（{len(样本列表)} 条）")

    print("\n[5] 最终分布验证：")
    for 文件名 in 三个新文件:
        新样本 = 解析单个文件(数据目录 / 文件名)
        报告分布(文件名, 新样本)

    print(f"\n[{datetime.now():%Y-%m-%d %H:%M:%S}] 处理完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(主流程())
