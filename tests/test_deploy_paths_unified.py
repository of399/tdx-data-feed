"""P1-8 部署脚本路径统一守卫（R-仓库、可验证、无需 root）。

锁四件事：
1. Python 入口默认值（tdxfeed/cli.py / full_sync.py / fin_parquet.py）必须 derive 自
   tdxfeed.paths 单一源，不得出现仓库根字面量 `/home/jiuben/tdx-data-feed`。
2. Shell 部署脚本（scripts/sync-to-obsidian.sh / git_init_vaults.sh / scripts/hooks/pre-commit）
   必须 source scripts/lib/tdx-env.sh（或按脚本位置推导），不留仓库根字面量。
3. systemd 单元必须是**模板**：仓库根一律写 `@TDX_HOME@`，渲染器按单一源替换成真实绝对路径
   （systemd 只在 ExecStart 一类指令上展开 `${VAR}`，WorkingDirectory / StandardOutput /
   Environment 都不展开，实测见 docs/P1-8_DEPLOY_PATH_UNIFICATION.md §5）。
4. 用户级已安装单元（~/.config/systemd/user）必须与「渲染后的模板」逐字节一致——
   改了模板忘了 `make timer-install`，就是单元加载的仍是旧文件。

系统级单元（/etc/systemd/system）的镜像与漂移守卫在
tests/test_systemd_system_units_mirror.py（那一侧需要 root 部署才能有比对对象）。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from scripts.render_systemd_units import PLACEHOLDER, render_text, unit_files

# 只读仓库里的单元模板与源码文本，不读行情/产物：本模块是静态守卫，
# 显式退出 conftest 的「数据依赖」分类（否则它会因含主机路径字面量被当成数据依赖而跳过）。
NEEDS_DATA = False

ROOT = Path(__file__).resolve().parents[1]
REPO_LITERAL = "/home/jiuben/tdx-data-feed"

# 外部 vault（StockVault）不在仓库内，允许作为主机默认保留，不纳入守卫
EXTERNAL_LITERAL = "/home/jiuben/StockVault"

PY_FILES = ["tdxfeed/cli.py", "tdxfeed/full_sync.py", "tdxfeed/fin_parquet.py"]
SH_FILES = [
    "scripts/sync-to-obsidian.sh",
    "scripts/git_init_vaults.sh",
    "scripts/hooks/pre-commit",
]

# 渲染用的假仓库根：证明模板与主机路径无关（换机器不用改模板）
FAKE_HOME = "/__tdx_render_probe__"

# 绝对路径里出现仓库目录名 = 该路径指向本仓库；模板里必须经占位符表达。
# 用目录名而不是完整路径做判据，是为了在换机器/换目录名后依然成立。
_REPO_PATH_TOKEN = re.compile(r"/[^\s\"']*" + re.escape(ROOT.name) + r"[^\s\"']*")

USER_SYSTEMD = Path.home() / ".config" / "systemd" / "user"


def _make_var(name: str) -> list[str]:
    """读 Makefile 里的 `NAME := a b c` 名单（换行续行也算）。"""
    text = (ROOT / "Makefile").read_text(encoding="utf-8")
    m = re.search(rf"^{name}\s*:=\s*((?:.*\\\n)*.*)$", text, re.M)
    assert m, f"Makefile 里没有 {name}"
    return m.group(1).replace("\\\n", " ").split()


def _whitelisted_units() -> list[str]:
    """timer-install 会安装到用户级 systemd 的单元名单。"""
    names: list[str] = []
    for timer in _make_var("USER_TIMERS"):
        names += [f"{timer}.service", f"{timer}.timer"]
    names += _make_var("USER_PATHS")
    names += _make_var("USER_UNIT_TEMPLATES")
    return names


def test_python_entry_paths_derive_from_single_source():
    """Python 入口不得硬编码仓库根，必须引用 tdxfeed.paths 单一源。"""
    for rel in PY_FILES:
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert REPO_LITERAL not in text, (
            f"{rel} 仍硬编码仓库根字面量 {REPO_LITERAL}；应改用 tdxfeed.paths.ROOT/DATA_ROOT"
        )
        assert "tdxfeed.paths" in text, (
            f"{rel} 未引用 tdxfeed.paths 单一源（直接用 ROOT / DATA_ROOT）"
        )


def test_shell_deploy_scripts_derive_repo_root():
    """Shell 部署脚本不得硬编码仓库根，必须 source 单一路径源或按位置推导。"""
    for rel in SH_FILES:
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert REPO_LITERAL not in text, (
            f"{rel} 仍硬编码仓库根字面量 {REPO_LITERAL}（含注释/用法示例）"
        )
        assert "tdx-env.sh" in text or "BASH_SOURCE" in text, (
            f"{rel} 未 source scripts/lib/tdx-env.sh，也未按脚本位置推导仓库根"
        )


def test_tdx_env_sh_is_the_single_source():
    """tdx-env.sh 自身：TDX_HOME 必须推导，不允许把仓库根写死成默认值。"""
    text = (ROOT / "scripts/lib/tdx-env.sh").read_text(encoding="utf-8")
    assert "TDX_HOME" in text
    # TDX_HOME 默认必须来自脚本位置推导（BASH_SOURCE），而非写死的主机路径
    assert "BASH_SOURCE" in text, "tdx-env.sh 的 TDX_HOME 必须按脚本位置推导"
    assert 'TDX_HOME="/home' not in text, "tdx-env.sh 不能把仓库根写死成默认值"


def test_external_vault_default_is_overridable_only():
    """反向确认：仓库根字面量未通过「外部 vault」借口被悄悄保留在受管文件里。"""
    offenders = []
    for rel in PY_FILES + SH_FILES:
        text = (ROOT / rel).read_text(encoding="utf-8")
        # 受管文件里只允许出现外部 vault 默认，不允许出现仓库根默认
        for line in text.splitlines():
            if REPO_LITERAL in line:
                offenders.append(f"{rel}: {line.strip()}")
    assert not offenders, "受管文件出现仓库根字面量：\n" + "\n".join(offenders)
    # 外部 vault 默认必须能被覆盖（出现在 tdx-env.sh 的默认赋值里）
    env = (ROOT / "scripts/lib/tdx-env.sh").read_text(encoding="utf-8")
    assert 'TDX_VAULT_STOCK="${TDX_VAULT_STOCK:-' in env or "TDX_VAULT_STOCK:-" in env


def test_systemd_units_are_templates_without_repo_literal():
    """systemd 单元是模板：指向本仓库的绝对路径必须经 `@TDX_HOME@` 表达。

    判据用「路径里出现仓库目录名」，而不是某个主机的完整路径——换机器、换目录名之后
    这条守卫依然成立（用完整路径写死的话，搬家当天守卫就哑了）。
    """
    problems = []
    for fp in unit_files():
        rel = fp.relative_to(ROOT)
        for lineno, line in enumerate(fp.read_text(encoding="utf-8").splitlines(), start=1):
            for token in _REPO_PATH_TOKEN.findall(line):
                if PLACEHOLDER not in token:
                    problems.append(f"{rel}:{lineno}: {token.strip()}")
    assert not problems, (
        "systemd 单元里仍有指向本仓库的硬编码路径（应写 "
        f"{PLACEHOLDER}）：\n" + "\n".join(problems)
    )


def test_systemd_render_is_home_independent():
    """模板必须与主机路径无关：换个仓库根渲染，结果只该差在这一个路径上。

    反例（模板里写死 `/home/jiuben/tdx-data-feed`）会让「换机器只改一处」失效，
    而且这类字面量在 CI（checkout 在别处）会静默指向一个不存在的目录。
    """
    for fp in unit_files():
        raw = fp.read_text(encoding="utf-8")
        rendered = render_text(raw, FAKE_HOME)
        assert "@TDX" not in rendered, f"{fp.name}: 渲染后仍有占位符残留（拼写不一致？）"
        assert str(ROOT) not in rendered, f"{fp.name}: 渲染结果里仍出现本机仓库路径"
        if PLACEHOLDER in raw:
            assert FAKE_HOME in rendered, f"{fp.name}: 模板有占位符但渲染没替换上"


def test_installed_user_units_match_rendered_templates():
    """已安装到 ~/.config/systemd/user 的单元必须等于渲染结果。

    改了模板忘了 `make timer-install` → systemd 加载的仍是旧文件，这条守卫把它变成红灯。
    """
    if not USER_SYSTEMD.is_dir():
        pytest.skip("本机没有用户级 systemd 目录（CI/干净环境）")
    problems = []
    for name in _whitelisted_units():
        src = ROOT / "systemd" / name
        installed = USER_SYSTEMD / name
        if not src.is_file() or not installed.is_file():
            continue
        expected = render_text(src.read_text(encoding="utf-8"), str(ROOT))
        actual = installed.read_text(encoding="utf-8")
        if expected.rstrip("\n") != actual.rstrip("\n"):
            problems.append(f"{name}: 已安装单元与渲染结果不同（跑 make timer-install）")
    assert not problems, "用户级单元漂移：\n" + "\n".join(problems)
