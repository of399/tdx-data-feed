"""单元文件里的 `Documentation=` 必须是 URL（2026-10-24 校准）。

systemd 按空白切分 `Documentation=`，逐段要求 `scheme://...`；写不进这个形状的就整段丢掉。
`systemd-analyze --user verify` 对本仓库报了三处，等于这些单元自称的出处从来没人看见过：

  systemd/plan-e-monitor.service:3: Invalid URL, ignoring: scripts/monitor_plan_e_live.py
  systemd/pushplus-bridge.service:3: Invalid URL, ignoring: | / token / 见 / v5/scripts/.pushplus.env
  systemd/tdx-clean-logs.service:3:  Invalid URL, ignoring: make / -C / …/clean-logs

最后一处本来是想写「怎么手动跑这个清理」，那是命令不是链接；链接归链接（file:// 指向文档），
命令归 Makefile。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCHEMES = ("http://", "https://", "file://", "man:", "html:")


def _doc_tokens() -> list[tuple[Path, str]]:
    out = []
    for fp in sorted((ROOT / "systemd").glob("*")):
        if fp.suffix not in (".service", ".timer", ".path"):
            continue
        for line in fp.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("Documentation="):
                for token in stripped[len("Documentation="):].split():
                    out.append((fp, token))
    return out


def test_every_documentation_token_is_a_url():
    bad = [(fp.name, tok) for fp, tok in _doc_tokens() if not tok.startswith(SCHEMES)]
    assert not bad, f"systemd 会整段丢掉这些出处，等于没写：{bad}"


def test_documentation_targets_exist_in_repo():
    """file:// 指向库内文档时要真的在——文档改名比写错更难被发现。"""
    for fp, token in _doc_tokens():
        if not token.startswith("file://"):
            continue
        rel = token[len("file://"):].removeprefix(str(ROOT) + "/")
        target = ROOT / rel
        assert target.exists(), f"{fp.name} 指向不存在的文档 {rel}"
