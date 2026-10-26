"""mcp-llm-ollama 单元、安装脚本与文档里的变量名契约（2026-10-25 校准）。

`v5/systemd/mcp-llm-ollama.service` 从没部署过（本机 `/etc/systemd/system/` 里没有它），
所以 7 条 `Environment=` 里 5 条写的名字 `llm_ollama.py` 根本不读，一直没暴露：
unit 写 `LLM_DEFAULT_CHAT`，脚本读 `DEFAULT_CHAT_MODEL`；模型选对纯属脚本默认值恰好相等。
同一个地址现存三种拼写 —— `OLLAMA_URL`（脚本真正读的）、`OLLAMA_HOST`（那是 Ollama
daemon 自己的变量，Docker 侧用法正确，塞进这个 client unit 是错的）、
`OLLAMA_BASE_URL`（`s7-mcp-servers.json`）。

六条断言分开，因为失效原因不同：

1. unit 的 `Environment=` 键必须被脚本读到 —— 否则是空写。
2. 安装脚本的 `SERVICES` 不得含 `ollama.service` —— 本机 Ollama 由 snap 提供，
   再起一个 `ExecStart=/snap/bin/ollama serve` 会跟已在跑的 daemon 抢 11434。
3. unit 不得 `Requires=`/`After=` 一个本机不存在的 `ollama.service` —— 否则
   `cp + daemon-reload + start` 立刻失败，头注释说的「自启」就是假的。
4. README「调优参数」表和 client 示例里的名字必须是脚本真读的 —— 那张表照 unit 抄，
   会把错名当规格继续传播。
5. `s7-mcp-servers.json` 里指向 llm_ollama.py 的条目，env 键必须是脚本真读的 ——
   它是 `s7-install-mcp-config.py` 的合并源，写错会被一次次灌回客户端配置。
6. v5 两份 README 的安装步骤不得 cp/enable 那个独立 `ollama` 单元。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UNIT = ROOT / "v5" / "systemd" / "mcp-llm-ollama.service"
SCRIPT = ROOT / "v5" / "mcp_servers" / "llm_ollama.py"
INSTALLER = ROOT / "v5" / "scripts" / "s6-install-systemd.sh"
README = ROOT / "v5" / "README-llm-ollama.md"
REGISTRATION = ROOT / "v5" / "scripts" / "s7-mcp-servers.json"
DOCS_WITH_INSTALL_STEPS = (ROOT / "v5" / "README.md", README)

_ENV_READ = re.compile(r"""os\.(?:environ\.get|getenv)\(\s*["']([A-Za-z_][A-Za-z0-9_]*)["']""")
_UNIT_ENV = re.compile(r'^Environment="?\s*([A-Za-z_][A-Za-z0-9_]*)=')
_TABLE_ROW = re.compile(r"^\|\s*`([A-Z][A-Z0-9_]*)`", re.M)
_JSON_KEY = re.compile(r'"([A-Z][A-Z0-9_]{2,})"\s*:')
_SERVICES = re.compile(r"^SERVICES=\((.*?)^\)", re.M | re.S)
# 独立的 ollama 单元（不是 mcp-llm-ollama）：前面的 (?<![-\w]) 用来排除
# "mcp-llm-ollama.service" 这种同名尾缀，否则文档里正常的 mcp 安装步骤会被误判。
_OLLAMA_UNIT_INSTALL = re.compile(
    r"(?:cp|systemctl\s+(?:enable(?:\s+--now)?|start|restart))\b"
    r"[^\n#]*?(?<![-\w])ollama(?:\.service)?\b"
)


def _script_reads() -> set[str]:
    return set(_ENV_READ.findall(SCRIPT.read_text(encoding="utf-8")))


def _unit_environment_keys() -> list[str]:
    keys = []
    for line in UNIT.read_text(encoding="utf-8").splitlines():
        m = _UNIT_ENV.match(line.strip())
        if m:
            keys.append(m.group(1))
    return keys


def test_unit_environment_keys_are_read_by_the_server():
    dead = [k for k in _unit_environment_keys() if k not in _script_reads()]
    assert not dead, f"unit 里这些 Environment= 键没人读，是空写：{dead}"


def test_installer_does_not_start_an_ollama_unit():
    m = _SERVICES.search(INSTALLER.read_text(encoding="utf-8"))
    assert m, "没解析到 SERVICES 数组，守卫失效"
    listed = re.findall(r'"([^"]+)"', m.group(1))
    assert "ollama.service" not in listed, (
        f"本机 Ollama 由 snap 占有 11434，安装脚本却又 enable 一个抢同一端口的单元：{listed}"
    )


def test_unit_does_not_depend_on_ollama_unit():
    bad = []
    for line in UNIT.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        for directive in ("Requires", "After", "Wants", "BindsTo", "Requisite"):
            prefix = f"{directive}="
            if stripped.startswith(prefix) and "ollama.service" in stripped[len(prefix):]:
                bad.append(stripped)
    assert not bad, f"ollama.service 在本机不存在（snap 提供），硬依赖会让 unit 起不来：{bad}"


def test_readme_names_are_read_by_the_server():
    text = README.read_text(encoding="utf-8")
    documented = {m.group(1) for m in _TABLE_ROW.finditer(text)}
    documented |= {m.group(1) for m in _JSON_KEY.finditer(text)}
    dead = sorted(n for n in documented if n not in _script_reads())
    assert not dead, f"README 把这些名字当规格写给 client，但脚本读不到：{dead}"


def test_client_registration_env_names_are_read_by_the_server():
    """s7-mcp-servers.json 是今天真正在用的那条启动路径（client fork stdio 进程）。

    只查 args 指向 llm_ollama.py 的条目 —— 别的 server 有自己读的变量名。
    """
    cfg = json.loads(REGISTRATION.read_text(encoding="utf-8"))
    servers = cfg.get("mcpServers", cfg)
    read = _script_reads()
    dead = []
    for name, spec in servers.items():
        args = " ".join(spec.get("args", []))
        if "llm_ollama.py" not in args:
            continue
        for key in (spec.get("env") or {}):
            if key not in read:
                dead.append((name, key))
    assert not dead, f"注册文件里这些 env 键脚本读不到，等于没配：{dead}"


def test_docs_do_not_install_the_standalone_ollama_unit():
    """文档不能教人 cp/enable 那个独立 ollama 单元：本机 :11434 不归 systemd，抢端口。"""
    offenders = []
    for doc in DOCS_WITH_INSTALL_STEPS:
        for i, line in enumerate(doc.read_text(encoding="utf-8").splitlines(), 1):
            if _OLLAMA_UNIT_INSTALL.search(line):
                offenders.append(f"{doc.relative_to(ROOT)}:{i}: {line.strip()}")
    assert not offenders, "以下安装步骤会跟 snap 起的 daemon 抢 11434：\n" + "\n".join(offenders)
