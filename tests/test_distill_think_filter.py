"""思考链过滤：正则匹配的是不存在的那对标签。

2026-10-24 用 data/distill/full.jsonl（7,983 条，2026-09-26 跑出）实测出的三条事实：

1. 旧正则剥掉的字节数是 **0**——它找的是尖括号 think 那一对，而这批数据里从来没有出现过。
2. 1,728 条以「裸的 think 闭合标签」开头（位置 0），全文没有 think 起始标签、
   没有竖线形式、也没有 im_end。开头那段思考链是被**截断**的：闭合标签留下了，
   起始标签没了，闭合标签之后的答案完整保留。
3. Qwen3 走 ChatML 通道，思考块由竖线形式的 think 起始/闭合标签包住，
   个别实现改用 im_end 收尾——这才是重跑蒸馏会看到的形状。

所以过滤器必须同时管住：竖线配对的块、im_end 收尾的块、以及两种裸的单边标签。
"""

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_LT, _GT, _PIPE, _SLASH = chr(60), chr(62), chr(124), chr(47)

ANSWER = "结论：观望。"


def _pipe(inner):
    """竖线形式的 ChatML 特殊标记，按字节拼出来。"""
    return _LT + _PIPE + inner + _PIPE + _GT


THINK_OPEN_PIPE = _pipe("think")
THINK_CLOSE_PIPE = _pipe("/think")
IM_END = _pipe("im_end")
THINK_CLOSE_BARE = _LT + _SLASH + "think" + _GT
THINK_OPEN_BARE = _LT + "think" + _GT


def _load():
    spec = importlib.util.spec_from_file_location(
        "distill_outputs", ROOT / "scripts" / "distill_outputs.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


clean = _load().clean


def test_importing_the_script_does_not_run_the_pipeline():
    """没有 __main__ 守卫时，import 就会去跑 main()——clean() 于是永远没法单测。"""
    assert clean(ANSWER) == ANSWER


def test_strips_pipe_delimited_thinking_block():
    raw = THINK_OPEN_PIPE + "先看MACD金叉，倾向看多" + THINK_CLOSE_PIPE + "\n" + ANSWER
    assert clean(raw) == ANSWER, "竖线配对的思考块没被剥掉：{!r}".format(clean(raw))


def test_strips_block_closed_by_im_end():
    raw = THINK_OPEN_PIPE + "推理过程" + IM_END + "\n" + ANSWER
    assert clean(raw) == ANSWER, "im_end 收尾的思考块没被剥掉：{!r}".format(clean(raw))


def test_strips_stray_think_close_tag():
    """数据集里 1,728 条的形状：开头一个裸闭合标签，后面跟答案。"""
    raw = THINK_CLOSE_BARE + "\n\n" + ANSWER
    assert clean(raw) == ANSWER, "裸 think 闭合标签没被剥掉：{!r}".format(clean(raw))


def test_strips_stray_think_open_tag():
    raw = THINK_OPEN_BARE + ANSWER
    assert clean(raw) == ANSWER, "裸 think 起始标签没被剥掉：{!r}".format(clean(raw))


def test_removes_reasoning_but_keeps_every_answer_char():
    raw = THINK_OPEN_PIPE + "很长的推理内容不该出现在训练集里" + THINK_CLOSE_PIPE + "\n" + ANSWER
    out = clean(raw)
    assert "很长的推理内容" not in out
    assert ANSWER in out
