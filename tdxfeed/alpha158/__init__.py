"""Alpha158 因子层：qlib 清单与语义工件的**消费侧**实现（主 venv，不 import qlib）。

分工见 AGENTS.md 的「qlib 只在 venv-qlib/ 里跑」与两份工件：

- `config/alpha158_expressions.json` — 158 条表达式清单（步骤 ①，岛内导出器产出）
- `config/alpha158_ops.json` — 求值面的语义金标准（步骤 ②，同一条代码路径产出）：
  28 个算子 = 18 个函数算子 + 6 个比较（`> >= < <= == !=` → `Gt/Ge/Lt/Le/Eq/Ne`）
  + 4 个四则（`+ - * /` → `Add/Sub/Mul/Div`）

`ops` 是算子层，`eval` 是封闭解释器：读 ① 的表达式串、按 ② 的符号表把运算符接到
`ops.REGISTRY` 上，产出按工件顺序命名的 158 列。数值全部在这里算，岛只负责给出
「表达式」「每个算子的真值」和「组合真值的核对路径」，不参与生产读路径。

`ic`（日频横截面 IC 汇总，步骤③的判据核）**不在这里再导出**：它依赖
`quant.multifactor.cv_utils`，而本包的 `eval`/`ops` 是训练与读侧都会碰的层，不该为了
一个研究判据把 quant 栈拉进每次 `import tdxfeed.alpha158`。要用就显式
`from tdxfeed.alpha158.ic import daily_ic, summarize_ic`。
"""
from __future__ import annotations

from tdxfeed.alpha158.eval import Alpha158Error, evaluate_feature, evaluate_frame
from tdxfeed.alpha158.ops import REGISTRY

__all__ = ["REGISTRY", "Alpha158Error", "evaluate_feature", "evaluate_frame"]
