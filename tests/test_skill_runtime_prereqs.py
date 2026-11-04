"""skill 声明的本地运行时前提必须真实在场。

为什么钉这条：`skills/kline-analysis.yaml` 的 `llm.adapter_path` 指向
`train/output/qwen3-14b-tdx-v7`，而该目录实测不存在（`train/output/` 只剩 v8 那批产物）。
skill YAML 不在 Python 侧，`tests/test_model_path_single_source.py` 那套路径守卫收不到它，
所以「声明指向一个不存在的目录」这件事今天没有任何东西会报。

这条判据只管**形状与存在性**，不管该指向哪一版：
「v7 该换成 v8 还是该重训回来」是口径决定，不在本文件里替人拍板 —— 因此 YAML 里那条
已知的缺失路径登记在 `KNOWN_MISSING`，登记项一旦在磁盘上出现就必须反过来红
（防腐化，同 `tests/test_names_single_source.py` 对例外表做的第三条）。
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SKILLS_DIR = ROOT / "skills"

# 绝对路径字面量：以 / 开头、且落在本仓库根下的声明（这类东西换机器/换 TDX_HOME 就漂）。
_ABS_IN_REPO = re.compile(r"^(?P<path>/[^\s]+)")

# 已登记的一次性债务：键为 (skill 文件名, 字段)，值为「为什么暂时允许它不在场」。
# 判据：登记项若已在磁盘上出现 ⇒ 红（该把登记删掉，而不是留着让守卫变聋）。
KNOWN_MISSING: dict[tuple[str, str], str] = {
    (
        "kline-analysis.yaml",
        "llm.adapter_path",
    ): "2026-11-04 实测：train/output/qwen3-14b-tdx-v7 不存在（该目录只剩 v8 产物）；"
    "该指向哪一版属口径决定，待负责人拍板后从本登记里删除",
}


def _skill_files() -> list[Path]:
    files = sorted(SKILLS_DIR.glob("*.yaml"))
    assert files, f"{SKILLS_DIR} 下一个 YAML 都没有，判据无从谈起"
    return files


def _declared_local_paths(skill_name: str, doc: dict) -> list[tuple[str, Path]]:
    """收集 skill 里所有「指向仓库内的本地绝对路径」声明，返回 (字段名, 解析后的 Path)。"""
    found: list[tuple[str, Path]] = []

    def walk(prefix: str, node: object) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                walk(f"{prefix}.{key}" if prefix else str(key), value)
        elif isinstance(node, list):
            for idx, value in enumerate(node):
                walk(f"{prefix}[{idx}]", value)
        elif isinstance(node, str):
            match = _ABS_IN_REPO.match(node.strip())
            if match and ROOT.as_posix() in match.group("path"):
                found.append((prefix, Path(match.group("path"))))

    walk("", doc)
    del skill_name  # 只用于调用侧报错可读性
    return found


def test_every_declared_local_path_is_registered_or_present() -> None:
    """未登记的缺失路径 ⇒ 红；已登记但已在场 ⇒ 也红（登记该删）。"""
    stale: list[str] = []
    unregistered: list[str] = []

    for path in _skill_files():
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(doc, dict):
            continue
        for field, target in _declared_local_paths(path.name, doc):
            key = (path.name, field)
            exists = target.exists()
            if key in KNOWN_MISSING:
                if exists:
                    stale.append(f"{path.name}:{field} -> {target} 已在场，登记项该删")
            elif not exists:
                unregistered.append(f"{path.name}:{field} -> {target} 不存在，且没有登记理由")

    assert not stale, "登记已腐化（缺陷其实已经不在了）：\n" + "\n".join(stale)
    assert not unregistered, "skill 声明了不存在的本地路径：\n" + "\n".join(unregistered)


def test_registration_entries_are_real_paths() -> None:
    """登记表本身不许指向空气：每个登记项都得真的对应到某条声明过的路径。

    防的是「改了 YAML 却留着旧登记」——登记键写错、字段改名、整条删掉，
    都会让守卫安静地放过本该红的一次，正是 AGENTS.md 里「豁免本身必须真的还挂着 importer」
    同一条要求在 YAML 侧的形状。
    """
    declared: set[tuple[str, str]] = set()
    for path in _skill_files():
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(doc, dict):
            continue
        for field, _target in _declared_local_paths(path.name, doc):
            declared.add((path.name, field))

    assert set(KNOWN_MISSING) <= declared, (
        f"登记了 YAML 里并不存在的字段；已知声明={sorted(declared)}"
    )
