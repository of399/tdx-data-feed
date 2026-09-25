"""D3.2 自动 step 抽取（M2 W11）

依据：roadmap §13.4 W11 D3.2

从 Obsidian markdown 笔记的 `<calc>` 代码块中自动抽取推导步骤：
  ```calc
  start_latex
  ---
  rule_name
  ---
  target_latex
  ```

每个 calc 块 = 一个三元组 (start, rule, target)。
支持批量抽取 + 单块验证 + 链式 DAG 构建。
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

# ============ 数据结构 ============


@dataclass
class CalcStep:
    """单个 calc 步骤三元组"""

    start: str  # 起点 LaTeX
    rule: str  # 应用的规则
    target: str  # 终点 LaTeX
    line_no: int = 0  # 在 markdown 中的行号
    raw_block: str = ""  # 原始 calc 块（用于调试）

    def to_dict(self) -> dict:
        return asdict(self)

    def is_valid(self) -> bool:
        """检查三元组是否完整且字段非空"""
        return bool(self.start.strip() and self.rule.strip() and self.target.strip())


@dataclass
class ExtractionResult:
    """markdown 抽取结果"""

    source_path: str
    steps: list[CalcStep] = field(default_factory=list)
    invalid_blocks: list[dict] = field(default_factory=list)  # 解析失败的块 + 错误
    total_blocks: int = 0

    def to_dict(self) -> dict:
        return {
            "source_path": self.source_path,
            "total_blocks": self.total_blocks,
            "valid_steps": len(self.steps),
            "invalid_blocks": len(self.invalid_blocks),
            "steps": [s.to_dict() for s in self.steps],
            "errors": self.invalid_blocks,
        }


# ============ 核心抽取 ============

# 匹配 ```calc ... ``` 块（非贪婪）
CALC_BLOCK_RE = re.compile(
    r"```calc\s*\n(.*?)```",
    re.DOTALL,
)

# 三行格式：start / rule / target
STEP_RE = re.compile(
    r"^\s*(?P<start>.+?)\s*\n\s*---\s*\n\s*(?P<rule>\w[\w_]*)\s*\n\s*---\s*\n\s*(?P<target>.+?)\s*$",
    re.MULTILINE,
)

# 备选格式：YAML frontmatter 风格
STEP_FRONTMATTER_RE = re.compile(
    r"---\s*\nstart:\s*(?P<start>.+?)\s*\nrule:\s*(?P<rule>\w[\w_]*)\s*\ntarget:\s*(?P<target>.+?)\s*\n---",
    re.DOTALL,
)


def _parse_block(content: str) -> tuple[CalcStep | None, str | None]:
    """解析 calc 块内容，返回 (step, error)"""
    content = content.strip()
    # 格式 1: 三行 (start / --- / rule / --- / target)
    m = STEP_RE.search(content)
    if m:
        return CalcStep(
            start=m.group("start").strip(),
            rule=m.group("rule").strip(),
            target=m.group("target").strip(),
            raw_block=content,
        ), None

    # 格式 2: YAML frontmatter 风格
    m = STEP_FRONTMATTER_RE.search(content)
    if m:
        return CalcStep(
            start=m.group("start").strip(),
            rule=m.group("rule").strip(),
            target=m.group("target").strip(),
            raw_block=content,
        ), None

    return None, "无法解析为 (start / rule / target) 三元组"


def extract_steps_from_markdown(
    markdown_text: str,
    source_path: str = "<inline>",
) -> ExtractionResult:
    """从 markdown 文本中抽取所有 calc 块步骤

    Args:
        markdown_text: 完整 markdown 内容
        source_path: 来源路径（仅用于结果标注）

    Returns:
        ExtractionResult（含 valid_steps + invalid_blocks + total_blocks）
    """
    result = ExtractionResult(source_path=source_path)

    for block_match in CALC_BLOCK_RE.finditer(markdown_text):
        # 计算行号（用于错误定位）
        line_no = markdown_text[: block_match.start()].count("\n") + 1

        content = block_match.group(1)
        result.total_blocks += 1

        step, error = _parse_block(content)
        if step is None:
            result.invalid_blocks.append(
                {
                    "line_no": line_no,
                    "error": error,
                    "raw": content[:200],
                }
            )
            continue

        step.line_no = line_no
        if not step.is_valid():
            result.invalid_blocks.append(
                {
                    "line_no": line_no,
                    "error": "三元组字段存在但为空",
                    "raw": content[:200],
                }
            )
            continue

        result.steps.append(step)

    return result


def extract_steps_from_file(file_path: str) -> ExtractionResult:
    """从 markdown 文件抽取"""
    with open(file_path, encoding="utf-8") as f:
        text = f.read()
    return extract_steps_from_markdown(text, source_path=file_path)


def steps_to_chain_request(
    steps: list[CalcStep],
    var: str = "x",
) -> list[dict]:
    """将步骤列表转为 build_chain 可消费的输入序列

    返回 [{"start_latex": ..., "target_latex": ..., "expected_rule": ...}, ...]
    """
    return [
        {
            "start_latex": s.start,
            "target_latex": s.target,
            "expected_rule": s.rule,
            "line_no": s.line_no,
        }
        for s in steps
    ]


# ============ 验证：用 build_chain 逐步核对 ============


def verify_steps(
    steps: list[CalcStep],
    var: str = "x",
    max_depth: int = 3,
    timeout_s: float = 2.0,
) -> dict:
    """用 build_chain 验证每个 calc 步骤是否真的可达

    返回 {"verified": int, "mismatches": [{line_no, expected_rule, got_rule, ...}]}
    """
    from mcp_math.derivation import build_chain

    verified = 0
    mismatches = []

    for step in steps:
        try:
            # 用 step.rule 作为唯一允许的规则
            dag = build_chain(
                start_latex=step.start,
                target_latex=step.target,
                var=var,
                max_depth=max_depth,
                rules=[step.rule, "identity"],  # identity = 直接成功（trivially_equal）
                timeout_s=timeout_s,
            )
            if dag.success:
                # 检查用的规则是否匹配（除了 identity）
                real_rules = [r for r in dag.rules_used if r != "identity"]
                if step.rule in real_rules or dag.reason == "trivially_equal":
                    verified += 1
                else:
                    mismatches.append(
                        {
                            "line_no": step.line_no,
                            "expected_rule": step.rule,
                            "got_rules": real_rules,
                            "reason": dag.reason,
                        }
                    )
            else:
                mismatches.append(
                    {
                        "line_no": step.line_no,
                        "expected_rule": step.rule,
                        "got_rules": [],
                        "reason": dag.reason,
                    }
                )
        except Exception as e:
            mismatches.append(
                {
                    "line_no": step.line_no,
                    "expected_rule": step.rule,
                    "error": str(e),
                }
            )

    return {
        "verified": verified,
        "total": len(steps),
        "mismatches": mismatches,
        "verified_ratio": verified / len(steps) if steps else 1.0,
    }
