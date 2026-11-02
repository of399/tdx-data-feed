#!/usr/bin/env python3
"""systemd 单元渲染器：把模板里的仓库根占位符替换成真实绝对路径（P1-8 步骤 4）。

**为什么是渲染而不是 `${TDX_HOME}` 变量展开**（2026-11-02 在本机 systemd 259 实测）：

| 指令 | `${TDX_HOME}` 展开 | 实测结果 |
|---|---|---|
| `ExecStart=` / `ExecStartPre=` / `ExecStartPost=` | ✅ | 生效（未加引号的词） |
| `WorkingDirectory=` | ❌ | **单元直接加载失败**（"bad unit file setting"） |
| `StandardOutput=append:` / `StandardError=append:` | ❌ | **静默回落 journal**（"path is not absolute"），日志文件丢失 |
| `Environment="PATH=${TDX_HOME}/..."` | ❌ | 原样保留 `${TDX_HOME}/...`，不展开 |
| `Documentation=` | ❌ | systemd 不做任何变量展开 |

也就是说：只有 ExecStart 一类能用变量，其余三类（工作目录、日志落盘、环境变量）必须拿到
**真实绝对路径**。所以单一源不能落在 systemd 的变量展开上，只能落在**渲染**上：

```
仓库单元（模板）              渲染器                        部署物（真实绝对路径）
systemd/*.service  ──▶  scripts/render_systemd_units.py  ──▶  ~/.config/systemd/user/*
systemd/system/*           TDX_HOME ← scripts/lib/tdx-env.sh      /etc/systemd/system/*
```

渲染是纯字符串替换：模板里出现的仓库根一律写作 `@TDX_HOME@`，渲染后得到与改造前
**逐字节相同**的内容（渲染语义 = 原有字面量），因此运行期行为零变化。

单一路径源：
- Shell 侧 `scripts/lib/tdx-env.sh` 的 `TDX_HOME`（未设则按脚本位置推导）。
- Python 侧本模块 `repo_root()`：优先环境变量 `TDX_HOME`，否则按本文件位置推导仓库根
  （与 `tdxfeed/paths.py::ROOT` 同规则）。

用法：
    venv/bin/python scripts/render_systemd_units.py --check
    venv/bin/python scripts/render_systemd_units.py --out-dir build/systemd
    venv/bin/python scripts/render_systemd_units.py --unit systemd/predict-daily.service
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tdxfeed.atomic import atomic_write_text  # noqa: E402  # SEM001 原子写

ROOT = _ROOT
SYSTEMD_DIR = ROOT / "systemd"
SYSTEM_DIR = SYSTEMD_DIR / "system"

# 模板里的仓库根占位符。systemd 里 % 是 specifier、@ 只用于模板实例名（onfailure-alert@.service），
# 所以 @TDX_HOME@ 不会与既有内容冲突。
PLACEHOLDER = "@TDX_HOME@"

UNIT_SUFFIXES = (".service", ".timer", ".path")


def repo_root() -> str:
    """仓库根：环境变量 TDX_HOME 优先，否则按本文件位置推导（与 tdxfeed.paths.ROOT 同规则）。"""
    env = os.environ.get("TDX_HOME", "").strip()
    return str(Path(env).expanduser().resolve()) if env else str(ROOT)


def render_text(text: str, home: str) -> str:
    """把模板文本里的 `@TDX_HOME@` 替换成 `home`。

    Args:
        text: 模板文本。
        home: 仓库根绝对路径。

    Returns:
        渲染后的文本。

    Raises:
        ValueError: 渲染后仍残留占位符（说明模板里有拼写不一致的占位符）。
    """
    out = text.replace(PLACEHOLDER, home)
    if PLACEHOLDER in out:
        raise ValueError(f"渲染后仍残留占位符 {PLACEHOLDER}")
    return out


def unit_files() -> list[Path]:
    """仓库里的全部单元模板（用户级 + 系统级），按路径排序。"""
    files: list[Path] = []
    for base in (SYSTEMD_DIR, SYSTEM_DIR):
        if not base.is_dir():
            continue
        for fp in sorted(base.iterdir()):
            if fp.suffix in UNIT_SUFFIXES and fp.is_file():
                files.append(fp)
    return files


def render_file(path: Path, home: str) -> str:
    """渲染单个单元文件。"""
    return render_text(path.read_text(encoding="utf-8"), home)


def check(home: str) -> int:
    """校验模板层不变量：不含真实仓库根字面量、渲染后无残留占位符。

    Returns:
        0 表示全部通过，1 表示有违规（并逐条打印）。
    """
    problems: list[str] = []
    for fp in unit_files():
        raw = fp.read_text(encoding="utf-8")
        rel = fp.relative_to(ROOT)
        if home in raw:
            problems.append(f"{rel}: 模板里仍有仓库根字面量 {home}（应写 {PLACEHOLDER}）")
        try:
            render_text(raw, home)
        except ValueError as exc:
            problems.append(f"{rel}: {exc}")
    if problems:
        print("✗ systemd 模板校验未通过：", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1
    print(f"✓ {len(unit_files())} 个单元模板通过校验（占位符 {PLACEHOLDER} → {home}）")
    return 0


def render_tree(out_dir: Path, home: str) -> list[Path]:
    """把全部模板渲染到 out_dir：用户级 → out_dir/，系统级 → out_dir/system/。

    Returns:
        写出的文件路径列表。
    """
    written: list[Path] = []
    for base, target in ((SYSTEMD_DIR, out_dir), (SYSTEM_DIR, out_dir / "system")):
        if not base.is_dir():
            continue
        target.mkdir(parents=True, exist_ok=True)
        for fp in unit_files():
            if fp.parent != base:
                continue
            dst = target / fp.name
            atomic_write_text(dst, render_file(fp, home), encoding="utf-8")
            written.append(dst)
    return written


def main(argv: list[str] | None = None) -> int:
    """命令行入口。

    Returns:
        进程退出码。
    """
    parser = argparse.ArgumentParser(description="systemd 单元模板渲染器（P1-8 步骤 4）")
    parser.add_argument("--check", action="store_true", help="只校验模板不变量，不写文件")
    parser.add_argument("--out-dir", type=Path, help="渲染输出目录（用户级 → DIR/，系统级 → DIR/system/）")
    parser.add_argument("--unit", type=Path, help="只渲染一个单元并打印到 stdout")
    parser.add_argument("--home", default=None, help="覆盖仓库根（默认取 TDX_HOME 或按位置推导）")
    args = parser.parse_args(argv)

    home = str(Path(args.home).expanduser().resolve()) if args.home else repo_root()

    if args.unit:
        print(render_file(args.unit, home), end="")
        return 0

    if args.out_dir:
        written = render_tree(args.out_dir, home)
        print(f"✓ 已渲染 {len(written)} 个单元到 {args.out_dir}（仓库根 {home}）")
        return 0

    return check(home)


if __name__ == "__main__":
    raise SystemExit(main())
