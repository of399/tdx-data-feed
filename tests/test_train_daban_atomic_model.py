"""KI-ATOMIC-004 的回归测试：训练产出的模型必须原子落位。

原问题：`model.save_model(str(model_out))` 直接写现役模型文件，训练中途崩溃 /
与 predict 并发时，predict 可能读到半截模型文件。
修复：先写同目录临时文件，再 os.replace 落位，并留一份带时间戳的备份。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

pd = pytest.importorskip("pandas")
pytest.importorskip("lightgbm")


def _load_train_module():
    spec = importlib.util.spec_from_file_location(
        "train_daban_t", ROOT / "scripts" / "train_daban_model.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["train_daban_t"] = mod
    spec.loader.exec_module(mod)
    return mod


def _fake_df(n: int = 400) -> pd.DataFrame:
    """构造可训练的小样本（date/label + 若干特征列）。

    日期必须跨过训练切分点 SPLIT_DATE，否则 test 集为空 → LightGBM 拒绝训练。
    """
    import numpy as np
    rng = np.random.default_rng(7)
    dates = pd.date_range("2025-01-01", periods=n, freq="D").strftime("%Y-%m-%d")
    feats = {f"f{i}": rng.normal(size=n) for i in range(6)}
    label = (rng.random(n) < 0.3).astype(int)
    df = pd.DataFrame({**feats, "label": label})
    df.insert(0, "date", dates)
    df.insert(0, "code", [f"{i:06d}" for i in range(n)])
    return df


def test_model_is_landed_atomically(tmp_path: Path) -> None:
    mod = _load_train_module()
    out = tmp_path / "model.txt"
    r = mod.train(_fake_df(), model_out=out)
    assert out.exists(), "模型未落位"
    assert out.stat().st_size > 0
    assert "auc" in r
    # 不留临时文件
    assert list(tmp_path.glob("*.tmp")) == []


def test_previous_model_is_backed_up(tmp_path: Path) -> None:
    """覆盖现役模型前自动留备份（崩溃可回退）。"""
    mod = _load_train_module()
    out = tmp_path / "model.txt"
    out.write_text("OLD-MODEL-CONTENT", encoding="utf-8")
    mod.train(_fake_df(), model_out=out)
    baks = list(tmp_path.glob("model.txt.bak-*"))
    assert len(baks) == 1
    assert baks[0].read_text(encoding="utf-8") == "OLD-MODEL-CONTENT"
    assert out.read_text(encoding="utf-8") != "OLD-MODEL-CONTENT"


def test_reader_never_sees_partial_model(tmp_path: Path) -> None:
    """落位期间读者只能看到旧内容或完整新模型，不会看到半截。"""
    import threading

    mod = _load_train_module()
    out = tmp_path / "model.txt"
    out.write_text("OLD", encoding="utf-8")
    seen: list[int] = []

    def reader() -> None:
        for _ in range(400):
            if out.exists():
                seen.append(len(out.read_bytes()))

    t = threading.Thread(target=reader)
    t.start()
    mod.train(_fake_df(), model_out=out)
    t.join()
    assert all(n in (3, out.stat().st_size) for n in seen), "读者读到了半截模型"
