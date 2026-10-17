"""Markdown 报告生成器（审计 P1-3-2, 2026-10-17）。

止血目标: 统一散布在 10+ 脚本中的 `L.append(f"# ...")` 同构报告拼接代码。
仅用于**新写**的报告脚本; 旧报告在 P2 plan_e 整合时自然迁移, 不回改。

用法:
    from report_writer import ReportBuilder

    rb = ReportBuilder(title="方案 E 回测报告", meta={"时间": "2026-10-17", "股票数": 136})
    rb.h2("核心指标")
    rb.metrics_table({"年化收益": "12.3%", "夏普": 1.85})
    rb.h2("Top 10")
    rb.table(["Rank", "代码", "PnL"], [["1", "600825", "0.2239"], ...])
    rb.write("data/plan_e/report.md")
"""

from __future__ import annotations

from pathlib import Path


def _escape(cell) -> str:
    """单元格转义: 竖线会破坏表格结构。"""
    return str(cell).replace("|", "\\|")


class ReportBuilder:
    """流式 Markdown 报告构建器。"""

    def __init__(self, title: str, meta: dict | None = None):
        self._lines: list[str] = [f"# {title}", ""]
        if meta:
            self.metrics_table(meta)
            self._lines.append("")

    def _raw(self, text: str) -> None:
        self._lines.append(text)

    def h2(self, text: str) -> None:
        self._lines.extend([f"## {text}", ""])

    def h3(self, text: str) -> None:
        self._lines.extend([f"### {text}", ""])

    def p(self, text: str) -> None:
        self._lines.extend([text, ""])

    def metrics_table(self, kvs: dict) -> None:
        """两列指标表: | 指标 | 值 |。"""
        self._raw("| 指标 | 值 |")
        self._raw("|---|---|")
        for k, v in kvs.items():
            self._raw(f"| {_escape(k)} | {_escape(v)} |")
        self._lines.append("")

    def table(self, headers: list, rows: list[list]) -> None:
        """通用表格。rows 为二维数组, 单元格自动转义。"""
        self._raw("| " + " | ".join(_escape(h) for h in headers) + " |")
        self._raw("|" + "|".join("---" for _ in headers) + "|")
        for row in rows:
            self._raw("| " + " | ".join(_escape(c) for c in row) + " |")
        self._lines.append("")

    def bullet(self, items: list) -> None:
        for it in items:
            self._raw(f"- {it}")
        self._lines.append("")

    def code(self, text: str, lang: str = "") -> None:
        self._lines.extend([f"```{lang}", text, "```", ""])

    def write(self, path) -> Path:
        """写文件并返回路径。父目录自动创建。"""
        fp = Path(path)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text("\n".join(self._lines), encoding="utf-8")
        return fp

    def render(self) -> str:
        return "\n".join(self._lines)
