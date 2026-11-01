"""日志轮转的两条规则：7 天没动就删，追加日志按大小截尾。

原来的 `find -mtime +7 -delete` 只做得到第一条；`append:` 写法的日志 mtime
永远是今天，canary_history.jsonl 这类只会一直长。这里锁住截尾的行为：
按行边界保留尾部、原地截断（保留 inode）、不碰符号链接。
"""
from __future__ import annotations

import importlib.util
import os
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 全程 tmp_path，不读 data/。
NEEDS_DATA = False


def _clean_logs():
    spec = importlib.util.spec_from_file_location(
        "clean_logs_under_test", ROOT / "scripts" / "clean_logs.py"
    )
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _age(path: Path, days: int) -> None:
    ts = (datetime.now() - timedelta(days=days)).timestamp()
    os.utime(path, (ts, ts))


def test_deletes_only_files_older_than_the_cutoff(tmp_path):
    mod = _clean_logs()
    old = tmp_path / "old.log"
    recent = tmp_path / "recent.log"
    nested = tmp_path / "sub" / "old.log"
    nested.parent.mkdir()
    for p in (old, recent, nested):
        p.write_text("x\n", encoding="utf-8")
    _age(old, 8)
    _age(nested, 30)

    summary = mod.clean(tmp_path, days=7, max_bytes=1024)

    assert not old.exists()
    assert not nested.exists()
    assert recent.exists()
    assert set(summary["deleted"]) == {"old.log", "sub/old.log"}


def test_truncates_oversized_log_to_the_tail_on_a_line_boundary(tmp_path):
    mod = _clean_logs()
    path = tmp_path / "canary_history.jsonl"
    path.write_bytes(b"".join(f"line {i:05d}\n".encode() for i in range(1000)))
    before = path.stat().st_size

    summary = mod.clean(tmp_path, days=7, max_bytes=1000)

    data = path.read_bytes()
    assert len(data) <= 1000
    assert data.endswith(b"line 00999\n"), "最新一行必须留下"
    assert data.startswith(b"line "), "不能留下被截断的半行"
    assert summary["truncated"] == [("canary_history.jsonl", before, len(data))]


def test_dry_run_reports_without_changing_anything(tmp_path):
    mod = _clean_logs()
    old = tmp_path / "old.log"
    big = tmp_path / "big.log"
    old.write_text("x\n", encoding="utf-8")
    big.write_text("y\n" * 1000, encoding="utf-8")
    _age(old, 30)
    before = big.stat().st_size

    summary = mod.clean(tmp_path, days=7, max_bytes=100, dry_run=True)

    assert old.exists()
    assert big.stat().st_size == before
    assert summary["deleted"] == ["old.log"]
    assert summary["truncated"][0][0] == "big.log"


def test_symlinks_are_not_followed_or_removed(tmp_path):
    """链接本身不是日志文件：既不按链接的 mtime 删它，也不顺着它删目标。"""
    mod = _clean_logs()
    target = tmp_path / "target.log"
    target.write_text("x\n", encoding="utf-8")
    link = tmp_path / "link.log"
    link.symlink_to(target)
    # 链接自己的 lstat mtime 变旧；目标仍是最近文件（utime 默认跟链接，必须显式关掉）。
    ts = (datetime.now() - timedelta(days=30)).timestamp()
    os.utime(link, (ts, ts), follow_symlinks=False)

    mod.clean(tmp_path, days=7, max_bytes=1024)

    assert link.is_symlink(), "符号链接本身不是日志文件，不能删"
    assert target.exists(), "不能顺着链接把目标删了"


def test_permission_errors_are_reported_not_raised(tmp_path, monkeypatch):
    """logs/ 里有 root 写的文件；用户级单元删不动它，报出来但不算失败。"""
    mod = _clean_logs()
    path = tmp_path / "root.log"
    path.write_text("x\n", encoding="utf-8")
    _age(path, 30)

    real_unlink = Path.unlink

    def deny(self, *a, **k):
        if self.name == "root.log":
            raise PermissionError("denied")
        return real_unlink(self, *a, **k)

    monkeypatch.setattr(Path, "unlink", deny)
    summary = mod.clean(tmp_path, days=7, max_bytes=1024)

    assert summary["skipped"] == ["root.log"]
    assert summary["deleted"] == []
