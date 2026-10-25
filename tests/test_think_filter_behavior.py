"""改过思考链过滤的文件必须还能解析，且常量真的剥得掉东西。

这条守卫是补 test_think_filter_no_op_pattern_banned 的洞：那条只 grep 旧的空转字面量，
所以「代码写坏了、连 ast.parse 都过不去」同样能让它变绿——2026-10-24 批量替换时就把
5 个文件写成了括号未闭合的坏常量，守卫当时报的是 12 passed。所以这里不看文本，看两件事：
文件能解析；把那组剥离常量单独 exec 出来（名字允许 _THINK_STRAY，也允许
_STRAY_OPEN/_STRAY_CLOSE 拆写的），对合成样本必须真的剥掉标记。
"""

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
L, G, S, P = chr(60), chr(62), chr(47), chr(124)
PIPE_OPEN, PIPE_CLOSE = L+P+"think"+P+G, L+P+S+"think"+P+G
IM_END = L+P+"im_end"+P+G
BARE_OPEN, BARE_CLOSE = L+"think"+G, L+S+"think"+G
ANSWER = "结论：观望"

FILES = [
    "scripts/batch_distill.py",
    "scripts/bench_cls2000.py",
    "scripts/distill_outputs.py",
    "training/eval/eval_v4_vs_v7r.py",
    "v5/scripts/v4_batch_analysis.py",
    "v5/scripts/v4_historical_roll.py",
]
CASES = [
    ("pipe_paired", PIPE_OPEN + "先看MACD金叉" + PIPE_CLOSE + "\n" + ANSWER),
    ("im_end", PIPE_OPEN + "推理内容" + IM_END + "\n" + ANSWER),
    ("stray_close", BARE_CLOSE + "\n\n" + ANSWER),
    ("stray_open", BARE_OPEN + ANSWER),
]


def _think_patterns(rel):
    """ exec 出该文件的剥离常量；文件坏了就把异常抛给测试。

    认行为不认名字：多数文件把散标记合成一条 _THINK_STRAY，
    scripts/distill_outputs.py 拆成 _STRAY_OPEN / _STRAY_CLOSE 两条 —— 两种都收，
    守卫要验的是「这些常量真剥得掉东西」，不是六个独立脚本用同一个变量名。
    """
    src = (ROOT / rel).read_text(encoding="utf-8")
    tree = ast.parse(src)                      # 语法坏了这里就炸
    prefixes = ("_THINK_", "_STRAY_")
    blocks = [n for n in tree.body if isinstance(n, ast.Assign)
              and getattr(n.targets[0], "id", "").startswith(prefixes)]
    names = {ast.unparse(n.targets[0]) for n in blocks}
    assert "_THINK_BLOCK" in names, f"{rel} 缺 _THINK_BLOCK：{sorted(names)}"
    # 必须按「连续片段」执行，而不是只挑 _THINK_* 那几条赋值：它们依赖同一块里的
    # _LT / _OPT 等前缀常量，只挑命名匹配的会漏掉定义，测试自己先 NameError。
    lines = src.splitlines()
    first = min(n.lineno for n in blocks)
    # 往前吃到这段常量定义的开头（_LT 那一行或紧跟注释后的第一条赋值）
    while first > 1 and lines[first - 2].lstrip().startswith(("_LT", "#", "_OPT")):
        first -= 1
    seg = "\n".join(lines[first - 1: max(n.end_lineno for n in blocks)])
    ns = {"re": re, "chr": chr}
    exec(compile(seg, rel, "exec"), ns)
    if "_THINK_STRAY" in ns:
        stray = ns["_THINK_STRAY"]
    else:
        missing = [n for n in ("_STRAY_OPEN", "_STRAY_CLOSE") if n not in ns]
        assert not missing, f"{rel} 既没有 _THINK_STRAY 也缺 {missing}：无从剥离散标记"
        stray = re.compile(ns["_STRAY_OPEN"].pattern + "|" + ns["_STRAY_CLOSE"].pattern)
    assert hasattr(stray, "sub"), f"{rel} 的散标记常量不是已编译正则"
    return ns["_THINK_BLOCK"], stray


def _strip(block, stray, raw):
    return stray.sub("", block.sub("", raw)).strip()


TOKEN_SHAPES = [PIPE_OPEN, PIPE_CLOSE, IM_END, BARE_OPEN, BARE_CLOSE]


def test_case_literals_are_shapes_the_production_pattern_claims_to_strip():
    """守卫自己写的标记形状，必须落在被检文件正则的匹配集里

    2026-10-25 这条要是先存在，三处斜杠位置笔误（think 闭标签两次、im_end 一次）
    会直接报在这里，而不是在主测试集上冒充成「产品代码没剥净」的红。
    """
    for rel in FILES:
        _, stray = _think_patterns(rel)
        for shape in TOKEN_SHAPES:
            assert stray.sub("", shape) == "", (
                f"{rel} 的正则根本不认守卫写的 {shape!r}：是用例拼错了形状，"
                "不是产品代码漏剥"
            )


def test_thinking_tags_are_actually_stripped_in_every_touched_file():
    for rel in FILES:
        block, stray = _think_patterns(rel)
        for name, raw in CASES:
            out = _strip(block, stray, raw)
            assert out == ANSWER, f"{rel} 的 {name} 用例没剥净：{out!r}"
        assert _strip(block, stray, ANSWER) == ANSWER, f"{rel} 误伤了无标记文本"
