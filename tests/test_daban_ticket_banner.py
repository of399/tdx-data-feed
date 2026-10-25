"""委托单横幅：负期望证据必须写进去，而且引用的路径必须在**仓库里**存在（设计 §5）。

两条容易混淆的判据分开测：

1. 文件在工作树里存在**不够**。`docs/DABAN_*_20261022.md` 这批研究快照一直是未跟踪状态，
   现有守卫 `tests/test_plan_e_journal_daban_entry.py:92` 只查 `exists()`，所以在脏工作树里
   一路绿灯、在干净 checkout 上必红。横幅是发给「只有仓库、没有本机」的读者的，
   所以这条守卫查的是 `git ls-files --error-unmatch`。
2. 被 gitignore 的产物不能写进横幅。`data/` 在 `.gitignore:42`、`logs/` 在 :48，
   本机在、仓库不在，等于让人去点一个打不开的路径。
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT), str(ROOT / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from tdxfeed.daban import trial  # noqa: E402

DOC_RE = re.compile(r"docs/[\w.\-]+\.md")
IGNORED_RE = re.compile(r"\b(data|logs|models|downloads)/[\w.\-/]+")


def _tracked(rel: str) -> bool:
    out = subprocess.run(
        ["git", "ls-files", "--error-unmatch", rel], cwd=ROOT, capture_output=True
    )
    return out.returncode == 0


def test_banner_declares_no_positive_expectation():
    text = trial.BANNER + trial.stop_lines_text()
    assert "不是买入指令" in text
    for figure in ("0.78", "94.5", "0.903", "39.7", "95.4"):
        assert figure in text, figure
    assert "人工执行" in text and "不下单" in text


def test_banner_cites_evidence_that_is_committed():
    cited = DOC_RE.findall(trial.BANNER)
    assert cited, "横幅里一个证据路径都没有"
    for rel in cited:
        assert (ROOT / rel).exists(), f"{rel} 在工作树里都不存在"
        assert _tracked(rel), f"{rel} 只活在本地工作树，没入库——干净 checkout 上点开是 404"


def test_banner_never_cites_gitignored_artifacts():
    assert not IGNORED_RE.search(trial.BANNER), IGNORED_RE.search(trial.BANNER)


def test_ticket_md_carries_banner_and_all_five_stop_lines():
    md = trial.BANNER + trial.stop_lines_text()
    assert trial.BANNER_HEAD in md
    numbered = re.findall(r"^\s+\d\)", md, flags=re.M)
    assert len(numbered) == 5, numbered
    assert "+0.20" in md, "放宽于纸盘 +0.20% 停止线这件事必须写在横幅里，不能只在代码注释里"
