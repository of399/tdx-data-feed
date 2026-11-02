"""P1-7：Makefile 口径对齐生产（台账 KI-MAKE-001 的守卫）。

验收：
- `make predict` 与 `predict-daily.service` 参数逐项一致（small/Top5/并行8，不再用 dev 的 Top30）
- `ci` 不含 `canary`（CI 必红项移除）
- `setup` / `pipeline` 用 `full-sync`（全市场）而非单标的 `data-sync`

实现说明：仅解析 Makefile 文本与 systemd unit，不实际执行 make / 预测 / CI。
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAKEFILE = ROOT / "Makefile"
PREDICT_SERVICE = ROOT / "systemd" / "predict-daily.service"


def _read_makefile() -> str:
    return MAKEFILE.read_text(encoding="utf-8")


def _recipe(target: str) -> str:
    """取某个 target 的完整配方（target 定义行 + 缩进续行），不含后续 target。

    剥离行尾 `##` 注释，只校验真实依赖/命令（注释里的说明性文字不计）。
    """
    lines = _read_makefile().splitlines()
    out: list[str] = []
    capture = False
    for line in lines:
        if line.startswith(f"{target}:"):
            capture = True
            out.append(line.split("##", 1)[0])
            continue
        if capture:
            if line[:1] in ("\t", " "):
                out.append(line.split("##", 1)[0])
            else:
                break
    return "\n".join(out)


def test_predict_matches_production_service():
    recipe = _recipe("predict")
    assert "--position-mode small" in recipe
    assert "--threshold 0.7" in recipe
    assert "--workers 8" in recipe
    assert "--top 30" not in recipe, "make predict 不应再用 dev 的 Top30"


def test_predict_recipe_parity_with_service():
    recipe = _recipe("predict")
    service = PREDICT_SERVICE.read_text(encoding="utf-8")
    exec_line = next(
        l for l in service.splitlines() if l.strip().startswith("ExecStart=")
    )
    for tok in ("--position-mode small", "--threshold 0.7", "--workers 8"):
        assert tok in recipe, f"make predict 缺失生产参数 {tok}"
        assert tok in exec_line, f"predict-daily.service 缺失参数 {tok}"


def test_ci_excludes_canary():
    recipe = _recipe("ci")
    assert "canary" not in recipe, "CI 必红项 canary 不得进 ci 门禁链"


def test_setup_uses_full_sync():
    recipe = _recipe("setup")
    assert "full-sync" in recipe
    assert "data-sync" not in recipe


def test_pipeline_uses_full_sync():
    recipe = _recipe("pipeline")
    assert "full-sync" in recipe
    assert "data-sync" not in recipe
