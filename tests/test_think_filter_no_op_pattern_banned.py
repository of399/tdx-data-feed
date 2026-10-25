"""全仓库禁用「只匹配尖括号 think 那一对」的空转正则。

2026-10-24 实测：这种 pattern 剥掉的字节数是 0，因为模型从来不发这一对标签。真实的两种形状是
竖线形式的 ChatML 思考块（起始标记到闭合标记或 im_end 收尾），以及被截断后只剩单个 think
闭合标记的残留——历史数据集里 1,728 条就是后者。所以过滤器必须同时管住配对块和单边残留，
见 scripts/distill_outputs.py 的 clean 与 tests/test_distill_think_filter.py。

扫描范围是 git 跟踪的 .py，排除归档目录（SKIP_DIRS 里的 `_archive`——归档的 deprecated 脚本是
历史现场，不是活代码）。按跟踪文件扫有两个原因：一是被 gitignore 的草稿树不该让守卫长期报错——
train/scripts/eval_v8.py 现在就带着这个空转写法，而 train/ 整个被 .gitignore:44 忽略；
二是这条守卫是后补的，范围只能按「已跟踪且不在归档目录」判定。没有逐文件豁免名单：
旧写法要保留，就把它移进 _archive/，别在守卫里开列外。
"""

import subprocess
from pathlib import Path

L = chr(60)
G = chr(62)
S = chr(47)
# 空转写法：起始 think 标记 .*? 闭合 think 标记
BAD = L + "think" + G + ".*?" + L + S + "think" + G

SKIP_DIRS = {"venv", "_archive", "data", "logs", "models", ".git", ".pytest_cache"}
ROOT = Path(__file__).resolve().parent.parent


def _live_py_files():
    """git 跟踪的 .py，排除归档目录。"""
    out = subprocess.run(
        ["git", "ls-files", "-z", "--", "*.py"], cwd=ROOT, capture_output=True
    )
    assert out.returncode == 0, "git ls-files 失败，守卫无从判定范围"
    tracked = [s for s in out.stdout.decode().split("\0") if s]
    assert tracked, "没扫到任何跟踪的 .py——守卫会空转"
    for rel in tracked:
        parts = rel.split("/")
        if any(part in SKIP_DIRS for part in parts):
            continue
        yield rel, ROOT / rel


def test_no_bare_think_pair_regex_left_in_live_code():
    offenders = []
    for rel, p in _live_py_files():
        try:
            text = p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if BAD in text:
            line_no = next(i + 1 for i, l in enumerate(text.splitlines()) if BAD in l)
            offenders.append(f"{rel}:{line_no}")
    assert not offenders, (
        "这些位置还在用剥不掉任何东西的 think 正则（剥掉 0 字节）：" + ", ".join(offenders)
    )


def test_live_scan_actually_covers_the_known_entry_points():
    """守卫本身不能扫了个空：把已知的生成/评测入口列进来对照。"""
    rels = {str(rel) for rel, _ in _live_py_files()}
    for known in [
        "scripts/batch_distill.py",
        "scripts/bench_cls2000.py",
        "scripts/distill_outputs.py",
        "training/eval/eval_v4_vs_v7r.py",
        "v5/scripts/v4_historical_roll.py",
        "v5/scripts/v4_batch_analysis.py",
    ]:
        assert known in rels, f"守卫没扫到 {known}"
    assert not any("archive" in r.lower() for r in rels), "守卫扫到了归档目录"
