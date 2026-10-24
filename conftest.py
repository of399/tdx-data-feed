"""让裸 `pytest tests/`（AGENTS.md 记的主测试集命令）能解析仓库根级包：tdxfeed / quant / scripts 等。

`tests/` 里有些文件在模块顶层直接 `from tdxfeed... import`，不做任何 sys.path 引导；
裸 pytest console script 不像 `python -m pytest` 会把当前目录放进 sys.path，于是收集即失败。
守卫见 tests/test_pytest_collection_bootstrap.py。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
