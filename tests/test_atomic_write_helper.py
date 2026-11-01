"""原子写助手 tdxfeed/atomic.py 的单元测试（KI-ATOMIC-001/002/004 的公共基础设施）。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tdxfeed.atomic import (  # noqa: E402
    atomic_write_bytes,
    atomic_write_frame,
    atomic_write_path,
    atomic_write_text,
    tmp_sibling,
)

pd = pytest.importorskip("pandas")


def test_write_text_lands_content(tmp_path: Path) -> None:
    f = tmp_path / "a.json"
    atomic_write_text(f, '{"x": 1}')
    assert f.read_text(encoding="utf-8") == '{"x": 1}'


def test_write_text_leaves_no_tmp(tmp_path: Path) -> None:
    f = tmp_path / "a.json"
    atomic_write_text(f, "x")
    assert list(tmp_path.glob("*.tmp")) == []


def test_overwrite_is_atomic_never_torn(tmp_path: Path) -> None:
    """覆盖写时读者只能看到旧值或新值，不会看到半截。"""
    f = tmp_path / "a.txt"
    atomic_write_text(f, "old")
    seen = []

    import threading

    def writer() -> None:
        atomic_write_text(f, "new-content-longer")

    t = threading.Thread(target=writer)
    t.start()
    for _ in range(200):
        if f.exists():
            seen.append(f.read_text(encoding="utf-8"))
    t.join()
    assert all(v in ("old", "new-content-longer") for v in seen), seen[:5]
    assert f.read_text(encoding="utf-8") == "new-content-longer"


def test_failed_write_keeps_original(tmp_path: Path) -> None:
    """写入失败时原文件不变，且临时文件被清理。"""
    f = tmp_path / "a.txt"
    atomic_write_text(f, "good")

    class Boom:
        def write_text(self, *a, **k):
            raise OSError("disk full")

    import tdxfeed.atomic as atomic_mod

    orig = Path.write_text

    def fake_write_text(self, *a, **k):
        if self.name.endswith(".tmp"):
            raise OSError("disk full")
        return orig(self, *a, **k)

    Path.write_text = fake_write_text  # type: ignore[method-assign]
    try:
        with pytest.raises(OSError):
            atomic_write_text(f, "bad")
    finally:
        Path.write_text = orig  # type: ignore[method-assign]
    assert f.read_text(encoding="utf-8") == "good"
    assert list(tmp_path.glob("*.tmp")) == []
    _ = Boom


def test_backup_created(tmp_path: Path) -> None:
    f = tmp_path / "a.json"
    atomic_write_text(f, "v1")
    atomic_write_text(f, "v2", backup=True)
    baks = list(tmp_path.glob("a.json.bak-*"))
    assert len(baks) == 1
    assert baks[0].read_text(encoding="utf-8") == "v1"


def test_write_bytes(tmp_path: Path) -> None:
    f = tmp_path / "a.bin"
    atomic_write_bytes(f, b"\x00\x01")
    assert f.read_bytes() == b"\x00\x01"


def test_write_frame(tmp_path: Path) -> None:
    f = tmp_path / "a.csv"
    df = pd.DataFrame({"code": ["000001"], "v": [1]})
    atomic_write_frame(df, f, encoding="utf-8-sig")
    back = pd.read_csv(f, dtype={"code": str})
    assert back["code"].tolist() == ["000001"]
    assert list(tmp_path.glob("*.tmp")) == []


def test_write_path_moves_tmp_into_place(tmp_path: Path) -> None:
    tmp = tmp_path / "model.tmp"
    tmp.write_text("MODEL")
    dst = tmp_path / "model.txt"
    atomic_write_path(tmp, dst)
    assert dst.read_text() == "MODEL"
    assert not tmp.exists()


def test_tmp_sibling_is_same_dir_and_unique(tmp_path: Path) -> None:
    t = tmp_sibling(tmp_path / "x.txt")
    assert t.parent == tmp_path
    assert t.name.endswith(".tmp")
    assert str(os.getpid()) in t.name
