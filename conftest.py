"""根级 pytest 引导：让裸 `pytest tests/` 能解析仓库根级包（tdxfeed / quant / scripts 等）。

`tests/` 里有些文件在模块顶层直接 `from tdxfeed... import`，不做任何 sys.path 引导；
裸 pytest console script 不像 `python -m pytest` 会把当前目录放进 sys.path，于是收集即失败。
守卫见 tests/test_pytest_collection_bootstrap.py。

同一处的第二个职责：CI 没有本地 `data/`，引用行情/产物的模块在收集期就跳过，
让「测试集能在无数据环境跑」这件事有唯一判据。守卫见 tests/test_ci_data_skip.py。
只读源码文本、不读 `data/` 的模块在文件里写一行 `NEEDS_DATA = False` 显式退出这个分类；
数据依赖是间接的（被测模块自己读盘）就写 `NEEDS_DATA = True` 显式加入。
"""
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TESTS_DIR = ROOT / "tests"
# 本机跑：data/ 在，一个都不跳。CI 跑：data/ 不在，依赖它的模块跳过。
# TDX_TEST_DATA_ROOT 只给守卫测试把「数据不存在」演出来，不改真实目录。
DATA_ROOT = Path(os.environ.get("TDX_TEST_DATA_ROOT", str(ROOT / "data")))
# 收集期导入测试模块可能顺带创建 data/（如 mcp/servers/vault.py 的 VectorStore），
# 若在 modifyitems 里现查 exists()，跳过判据会被这些副作用翻成「有数据」。
_HAS_DATA = DATA_ROOT.exists()
_DATA_MARKERS = ("data/", "/home/jiuben")
# 模块级显式退出：只读源码文本、不读 data/ 的模块在**零缩进**处写 `NEEDS_DATA = False`
# （样例字符串里带 data/ 字样会被下面的分类误判；缩进的同名行不算，避免函数内局部变量误触）。
_OPT_OUT_LINE = "NEEDS_DATA = False"
# 反向显式加入：数据依赖是间接的（被测模块自己读盘，测试源码里没有 data/ 字样）时用。
_OPT_IN_LINE = "NEEDS_DATA = True"


def _needs_data(path: Path) -> bool:
    if TESTS_DIR not in path.parents:
        return False
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    lines = [line.rstrip() for line in text.splitlines()]
    if _OPT_IN_LINE in lines:
        return True
    if _OPT_OUT_LINE in lines:
        return False
    return any(marker in text for marker in _DATA_MARKERS)


def pytest_collection_modifyitems(config, items):
    if _HAS_DATA:
        return
    reason = f"缺 {DATA_ROOT}：该模块依赖本地行情/产物，无数据环境里跳过"
    for item in items:
        if _needs_data(Path(str(item.path))):
            item.add_marker(pytest.mark.skip(reason=reason))
