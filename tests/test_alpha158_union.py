"""候选并集清单（`config/alpha158_union.json`）的守卫。

为什么要有这一层：并集的 62 条是**判据的产物**（ρ=0.8、h=5、seed=20261104 那次面板），
不是人挑的列名。宽表装配与基线评测都只能读这份工件；一旦有人把名单抄进 .py 常量表，
下一次重装配换了窗口或阈值，代码里那份就是第二套口径——而它不会报错。

四条判据：
① 工件内部自洽（并集 = 现役独立 + 净新增，两侧不重叠，计数与列表一致）；
② 每条净新增维都能在 158 清单工件里找到，每条现役独立维都在 `FEAT26_ORDER` 里；
③ `content_sha256` 自证：手改列表必须让校验非零；
④ 导出器从 run_meta 重算能逐字复现工件，漂移（改 ρ / 改维名）必须被 `--check` 抓到。

①②③ 不依赖 `data/`（CI 真跑）；④ 需要 run_meta 在场，落在本机判据里，缺文件时显式跳过。
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
ARTIFACT = ROOT / "config" / "alpha158_union.json"
EXPORTER = ROOT / "scripts" / "alpha158_union_list.py"
EXPRESSIONS = ROOT / "config" / "alpha158_expressions.json"

sys.path.insert(0, str(ROOT))
from tdxfeed.stage1_wide_table import FEAT26_ORDER  # noqa: E402


def _load() -> dict:
    return json.loads(ARTIFACT.read_text(encoding="utf-8"))


def _content_hash(body: dict) -> str:
    """与导出器同一算法：去掉 content_sha256 后按 sort_keys 序列化再 sha256。"""
    stripped = {k: v for k, v in body.items() if k != "content_sha256"}
    return hashlib.sha256(
        json.dumps(stripped, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def test_union_artifact_exists_and_is_not_a_hand_copy() -> None:
    assert ARTIFACT.exists(), (
        "候选并集清单必须是落盘工件（由 scripts/alpha158_union_list.py 从 run_meta 产出），"
        "不许把列名抄进宽表或基线脚本")
    body = _load()
    for key in ("version", "dedup", "feat26_independent", "alpha158_net_new",
                "union", "counts", "provenance", "content_sha256"):
        assert key in body, f"工件缺字段 {key}"


def test_union_is_the_sum_of_the_two_views_and_they_are_disjoint() -> None:
    """并集 = 现役独立 + 净新增；两侧名字必须不重叠，否则「相加」这个动作是假的。"""
    body = _load()
    f26 = body["feat26_independent"]
    nn = body["alpha158_net_new"]
    union = body["union"]
    assert set(f26) & set(nn) == set(), f"两侧有重名维: {sorted(set(f26) & set(nn))}"
    assert set(union) == set(f26) | set(nn), "union 不等于两侧之并"
    assert len(union) == len(f26) + len(nn) == body["counts"]["union"], "计数与列表不一致"


def test_every_named_dim_resolves_to_an_authority() -> None:
    """47 条在 158 清单工件里、15 条在 FEAT26_ORDER 里——不许有第三条来源。"""
    body = _load()
    names = {f["name"] for f in json.loads(EXPRESSIONS.read_text(encoding="utf-8"))["features"]}
    stray_nn = [c for c in body["alpha158_net_new"] if c not in names]
    assert not stray_nn, f"净新增里有没有 158 工件来源的维: {stray_nn}"
    stray_f = [c for c in body["feat26_independent"] if c not in FEAT26_ORDER]
    assert not stray_f, f"现役独立里有没有不在生产清单的维: {stray_f}"


def test_content_hash_self_proofs_against_hand_edit(tmp_path: Path) -> None:
    """手改列表（多一条/少一条）必须让校验非零——这条抓的是「工件被人顺手编辑过」。"""
    body = _load()
    assert body["content_sha256"] == _content_hash(body), "在场工件的自证哈希已经不对"
    tampered = json.loads(json.dumps(body))
    tampered["alpha158_net_new"] = [*tampered["alpha158_net_new"][:-1], "KMID"]
    tampered["union"] = sorted(set(tampered["feat26_independent"])
                               | set(tampered["alpha158_net_new"]))
    assert _content_hash(tampered) != body["content_sha256"], "哈希算法没起作用"
    path = tmp_path / "tampered.json"
    path.write_text(json.dumps(tampered, ensure_ascii=False, indent=2), encoding="utf-8")
    rc = subprocess.run([sys.executable, str(EXPORTER), "--check", str(path)],
                        capture_output=True, text=True).returncode
    assert rc != 0, "被改过的工件必须让 --check 非零退出"


def test_exporter_reproduces_the_artifact_from_run_meta() -> None:
    """本机判据：从 run_meta 重算应逐字复现在场工件（除 generated_at）。"""
    prov = _load()["provenance"]
    run_meta = ROOT / prov["run_meta"]
    if not run_meta.exists():
        pytest.skip("run_meta 在 data/（gitignore），本条只在导出那台机器上跑")
    cmd = [sys.executable, str(EXPORTER),
           "--run-meta", str(run_meta),
           "--rho", str(_load()["dedup"]["rho"]),
           "--horizon", str(_load()["dedup"]["horizon"]),
           "--check", str(ARTIFACT)]
    res = subprocess.run(cmd, capture_output=True, text=True)
    assert res.returncode == 0, f"岛外重算与工件不一致：{res.stdout}{res.stderr}"


def test_exporter_builds_a_union_from_a_synthetic_run_meta(tmp_path: Path) -> None:
    """导出器的行为判据：net_new.kept_dims + within.FEAT26.kept_dims，且 --check 认它。"""
    run_meta = tmp_path / "run_meta_x.json"
    dims_f = list(FEAT26_ORDER[:3])
    names = [f["name"] for f in json.loads(EXPRESSIONS.read_text(encoding="utf-8"))["features"]]
    dims_nn = names[:4]
    run_meta.write_text(json.dumps({
        "tag": "x", "start": "2024-01-01", "end": "2026-09-15", "seed": 1,
        "dedup": {"0.8": {
            "net_new": {"kept_dims": dims_nn},
            "within": {"FEAT26": {"kept_dims": dims_f}},
        }},
        "horizons": {"5": {}},
    }), encoding="utf-8")
    out = tmp_path / "union.json"
    rc = subprocess.run([sys.executable, str(EXPORTER), "--run-meta", str(run_meta),
                         "--rho", "0.8", "--horizon", "5", "--out", str(out)],
                        capture_output=True, text=True).returncode
    assert rc == 0, "导出器在小 run_meta 上没跑通"
    body = json.loads(out.read_text(encoding="utf-8"))
    assert body["alpha158_net_new"] == dims_nn
    assert body["counts"]["union"] == len(set(dims_f) | set(dims_nn))
    assert subprocess.run([sys.executable, str(EXPORTER), "--check", str(out)],
                          capture_output=True).returncode == 0
