"""P1-1 / P1-2：skill_router 的 yaml 契约基线测试。

契约基线（P1-2 将进一步收紧为"真契约"并清除死字段）：
- 每个 skills/*.yaml 必须能解析为映射
- 每个 skill 必须有非空 name（load_all_skills 靠它注册，缺 name 即静默丢弃）
- 不允许同名 skill（重名会导致后者覆盖前者，路由结果不可预期）
"""
import yaml
import pytest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILLS_DIR = ROOT / "skills"

# KI-MCP-002：声明层死字段（loader 从不读取，纯误导）。P1-2 必须清除。
DEAD_FIELDS = {
    "version", "description", "tools", "input_schema",
    "output_schema", "priority", "timeout_sec", "metrics",
}


def test_all_skill_yaml_parse_and_named():
    names = []
    for p in sorted(SKILLS_DIR.glob("*.yaml")):
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
        assert isinstance(data, dict), f"{p.name} 不是映射"
        assert "name" in data and data["name"], f"{p.name} 缺少 name"
        names.append(data["name"])
    assert names, "skills/ 下没有任何有效 skill"
    # 无重名
    assert len(names) == len(set(names)), f"skill 重名：{names}"


def test_skills_dir_not_empty():
    assert list(SKILLS_DIR.glob("*.yaml")), "skills/ 目录为空"


def test_no_dead_fields_in_skill_yaml():
    """P1-2：yaml 死字段（version/description/tools/...）必须清除，否则声明层继续脱钩。"""
    for p in SKILLS_DIR.glob("*.yaml"):
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
        assert not (DEAD_FIELDS & set(data.keys())), f"{p.name} 仍含死字段：{sorted(DEAD_FIELDS & set(data.keys()))}"
