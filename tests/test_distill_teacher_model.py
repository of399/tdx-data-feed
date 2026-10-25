"""教师模型不能写死：蒸馏脚本必须显式指定 Ollama tag，没给就立刻退出。

背景（2026-10-24 核实）：`scripts/distill_outputs.py` 确实在本机跑出过
`data/distill/full.jsonl`——7,983 条、键为 instruction/input/output、无一条空 output，
时间戳 2026-09-26。它当时用的教师 tag 是 `tdx-lora:latest`，而这个名字在整个仓库里
没有任何生产者：没有 Modelfile，没有 `ollama create` 步骤，Makefile 注册的只有
MODEL_V5/V6/V7 和 MODEL_BASE=qwen3-14b-tdx-fast，本机 Ollama 到 2026-10-24 也没有它。

为什么这必须是一条测试而不是"记得改一下"：`call()` 的失败路径是「重试 3 次后
return ""」，模型不存在时 Ollama 立刻拒绝，于是整条管线不会抛错——它会把数据集写成
一片空 output 并且**退出码为 0**。写死一个不存在的教师模型，在这条管线上是静默的
数据损毁，不是一个会自己响的报错。
"""

import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "distill_outputs.py"
MAKEFILE = ROOT / "Makefile"


def _src() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def test_teacher_model_comes_from_the_environment():
    """MODEL 必须读环境变量；任何字面量 tag 都会在某天变成不存在的模型。"""
    assignment = re.search(r"^MODEL\s*=.*$", _src(), re.M)
    assert assignment, "scripts/distill_outputs.py 里找不到 MODEL 赋值"
    assert "os.environ" in assignment.group(0), (
        f"教师模型写死了，重跑会把数据集写成空 output 且退出码 0：{assignment.group(0)}"
    )
    assert "TDX_DISTILL_MODEL" in assignment.group(0), (
        f"环境变量名应当是 TDX_DISTILL_MODEL：{assignment.group(0)}"
    )


def test_no_dead_teacher_tag_remains_in_the_script():
    """tdx-lora 在仓库里只有这一处引用，且本机已无此 tag——不允许再出现。"""
    assert "tdx-lora" not in _src(), "scripts/distill_outputs.py 仍引用 tdx-lora"


def test_missing_model_exits_before_touching_data_or_network(tmp_path):
    """没给教师模型时必须非零退出并点名该变量，而且要在读 skeleton、发请求之前就退出。

    skeleton 参数故意指向不存在的文件：如果守卫排在 open() 之后，stderr 里出现的会是
    FileNotFoundError 而不是 TDX_DISTILL_MODEL——那正是"写死的模型名带着坏输入继续往下跑"
    的形状。TDX_OLLAMA_URL 指向一个必然拒绝连接的端口，用来证明没有发出任何请求。
    """
    env = {k: v for k, v in __import__("os").environ.items() if k != "TDX_DISTILL_MODEL"}
    env["TDX_OLLAMA_URL"] = "http://127.0.0.1:1"
    started = time.time()
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), str(tmp_path / "nope.jsonl"), str(tmp_path / "out.jsonl")],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    elapsed = time.time() - started
    assert proc.returncode != 0, "没给教师模型却成功退出——正是静默空数据的形状"
    assert "TDX_DISTILL_MODEL" in proc.stderr, f"stderr 没点名缺失的变量：{proc.stderr[:300]}"
    assert "FileNotFoundError" not in proc.stderr, f"守卫排在了读数据之后：{proc.stderr[:300]}"
    assert elapsed < 10, f"退出用了 {elapsed:.1f}s，像是在重试网络请求而非直接拒绝启动"


def test_makefile_exposes_a_distill_target_that_refuses_an_undefined_model():
    """make distill 必须存在，且 MODEL 没给时拒绝启动而不是拉一个默认 tag。"""
    text = MAKEFILE.read_text(encoding="utf-8")
    recipe = re.search(r"^distill:.*?(?=^\S|\Z)", text, re.M | re.S)
    assert recipe, "Makefile 没有 distill 目标：这条管线只能靠人手抄 python 命令行"
    body = recipe.group(0)
    assert "distill_outputs.py" in body, "distill 目标没有调用 scripts/distill_outputs.py"
    assert "TDX_DISTILL_MODEL" in body, "distill 目标没有把 MODEL 传给 TDX_DISTILL_MODEL"
    assert "-z \"$(MODEL)\"" in body, "MODEL 未指定时 distill 目标会静默用空 tag 启动"
