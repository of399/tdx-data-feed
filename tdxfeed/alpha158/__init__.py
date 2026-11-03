"""Alpha158 因子层：qlib 清单与语义工件的**消费侧**实现（主 venv，不 import qlib）。

分工见 AGENTS.md 的「qlib 只在 venv-qlib/ 里跑」与两份工件：

- `config/alpha158_expressions.json` — 158 条表达式清单（步骤 ①，岛内导出器产出）
- `config/alpha158_ops.json` — 18 个算子的语义金标准（步骤 ②，同一条代码路径产出）

`ops` 是算子层，`eval`（随后落地）把表达式字符串接到这层上。数值全部在这里算，
岛只负责给出「表达式」和「每个算子的真值」，不参与生产读路径。
"""
from __future__ import annotations

from tdxfeed.alpha158.ops import REGISTRY

__all__ = ["REGISTRY"]
