#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""桌面弹窗"批准"按钮自动点击工具。

功能:
  1. 定位并识别包含指定文本(默认"批准")的弹窗 / 按钮
  2. 弹窗出现时自动触发点击
  3. 可配置检测间隔与重试次数 / 总超时
  4. 记录每次点击尝试的时间戳与结果到日志文件
  5. 找不到目标时给出明确错误提示或超时处理

定位后端(可切换, 见 ClickerConfig.backend):
  - ocr     : 跨平台, 截图 + Tesseract OCR 识别文本后点击中心 (默认)
  - image   : 跨平台, 基于按钮截图的图像匹配 (pyautogui.locateOnScreen)
  - windows : Windows 专属, pywinauto 精确控件定位(最稳)

依赖:
  pip install pyautogui pillow pytesseract pywinauto
  ocr 后端还需在系统安装 Tesseract-OCR 引擎并包含中文包(chi_sim):
    - Ubuntu:  apt install tesseract-ocr tesseract-ocr-chi-sim
    - macOS:   brew install tesseract tesseract-lang
    - Windows: 下载安装包并确保 tesseract 在 PATH

运行示例:
  python tools/desktop_auto_clicker.py --text 批准 --interval 1.0 --retries 30
  python tools/desktop_auto_clicker.py --backend image --image approve.png

负责任使用提示:
  本工具会模拟真实鼠标点击。请勿用于绕过任何应用的安全确认、
  身份认证或第三方的许可弹窗; 仅在你拥有权限的、可信的桌面程序中使用。
"""

from __future__ import annotations

import argparse
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


# ============================== 配置 ==============================
@dataclass
class ClickerConfig:
    """所有可调参数集中于此, 便于维护与调整。

    修改默认值或经命令行传入即可, 无需改动逻辑代码。
    """

    target_text: str = "批准"            # 弹窗 / 按钮上要匹配的文本
    button_text: str = "批准"            # 控件精确文本(windows 后端使用)
    window_title_hint: str = ""          # 窗口标题关键字, 缩小搜索范围(可选)
    backend: str = "auto"                # auto | ocr | image | windows
    button_image: Optional[str] = None   # image 后端: 按钮截图路径
    detection_interval: float = 1.0      # 每次检测之间的间隔(秒)
    max_retries: int = 30                # 最大检测轮次(0=不限)
    timeout: float = 0.0                 # 总超时(秒, 0=不限, 仅受 max_retries 约束)
    confidence: float = 0.8              # 图像识别置信度阈值
    click_offset: tuple[int, int] = (0, 0)  # 命中后的点击坐标偏移(像素)
    log_file: str = "logs/auto_clicker.log"


@dataclass
class ClickResult:
    """单次点击尝试的结果。"""

    success: bool
    detail: str
    timestamp: str = ""


# ============================== 日志 ==============================
def build_logger(log_file: str) -> logging.Logger:
    """构造同时输出到文件与控制台的结构化日志器。"""
    path = Path(log_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("desktop_auto_clicker")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
    fh = logging.FileHandler(path, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(sh)
    return logger


# ============================== 后端 ==============================
class Backend:
    """定位并点击目标的抽象基类, 便于扩展新的定位策略。"""

    def find_and_click(self, cfg: ClickerConfig) -> ClickResult:
        raise NotImplementedError


class OcrBackend(Backend):
    """跨平台文本后端: 截图 -> OCR -> 命中目标文本 -> 点击中心。

    优点: 不依赖特定窗口 API, 任意含文字的弹窗均可用。
    缺点: 依赖 Tesseract 中文识别准确率, 极小按钮可能漏检。
    """

    def find_and_click(self, cfg: ClickerConfig) -> ClickResult:
        import pyautogui
        import pytesseract

        screenshot = pyautogui.screenshot()
        data = pytesseract.image_to_data(
            screenshot, lang="chi_sim+eng", output_type=pytesseract.Output.DICT
        )
        for i, text in enumerate(data["text"]):
            if cfg.target_text in text:
                x, y, w, h = (
                    data["left"][i],
                    data["top"][i],
                    data["width"][i],
                    data["height"][i],
                )
                cx = x + w // 2 + cfg.click_offset[0]
                cy = y + h // 2 + cfg.click_offset[1]
                pyautogui.click(cx, cy)
                return ClickResult(
                    True, f"OCR 命中 '{text}' 于 ({cx},{cy}) 并已点击"
                )
        return ClickResult(False, f"屏幕 OCR 未找到文本 '{cfg.target_text}'")


class ImageBackend(Backend):
    """图像匹配后端: 在屏幕上查找按钮截图并点击中心。

    适用于按钮文本无法被 OCR 稳定识别、但按钮外观固定的场景。
    """

    def find_and_click(self, cfg: ClickerConfig) -> ClickResult:
        import pyautogui

        if not cfg.button_image or not Path(cfg.button_image).exists():
            return ClickResult(False, f"按钮截图不存在: {cfg.button_image}")
        loc = pyautogui.locateOnScreen(
            cfg.button_image, confidence=cfg.confidence
        )
        if loc is None:
            return ClickResult(False, "屏幕上未匹配到按钮图像")
        cx, cy = pyautogui.center(loc)
        pyautogui.click(cx + cfg.click_offset[0], cy + cfg.click_offset[1])
        return ClickResult(True, f"图像命中于 ({cx},{cy}) 并已点击")


class WindowsBackend(Backend):
    """Windows 专属: 通过 UI 自动化精确定位按钮控件(最稳, 不依赖像素)。"""

    def find_and_click(self, cfg: ClickerConfig) -> ClickResult:
        from pywinauto import Desktop

        desktop = Desktop(backend="uia")
        for win in desktop.windows():
            title = win.window_text()
            if cfg.window_title_hint and cfg.window_title_hint not in title:
                continue
            try:
                btn = win.child_window(
                    title=cfg.button_text, control_type="Button"
                )
                if btn.exists(timeout=0.3):
                    btn.click()
                    return ClickResult(
                        True,
                        f"点击窗口 '{title}' 中的按钮 '{cfg.button_text}'",
                    )
            except Exception:
                # 单个窗口解析失败不影响整体轮询
                continue
        return ClickResult(False, f"未找到含按钮 '{cfg.button_text}' 的窗口")


def select_backend(cfg: ClickerConfig) -> Backend:
    """根据配置 / 运行环境选择定位后端。"""
    backend = cfg.backend
    if backend == "auto":
        try:
            import pywinauto  # noqa: F401

            backend = "windows"
        except Exception:
            backend = "ocr"
    if backend == "windows":
        return WindowsBackend()
    if backend == "image":
        return ImageBackend()
    return OcrBackend()


# ============================ 主控 ============================
class AutoClicker:
    """按配置循环检测并点击目标, 处理重试与超时。"""

    def __init__(self, cfg: ClickerConfig, logger: logging.Logger):
        self.cfg = cfg
        self.logger = logger
        self.backend = select_backend(cfg)

    def run(self) -> ClickResult:
        cfg = self.cfg
        start = time.time()
        attempt = 0
        self.logger.info(
            "启动 | backend=%s text=%s interval=%.1fs retries=%d timeout=%.1fs",
            cfg.backend, cfg.target_text, cfg.detection_interval,
            cfg.max_retries, cfg.timeout,
        )
        while True:
            if cfg.max_retries and attempt >= cfg.max_retries:
                msg = f"已达最大重试次数 {cfg.max_retries}, 仍未找到目标弹窗"
                self.logger.error("失败 | %s", msg)
                return ClickResult(False, msg)
            if cfg.timeout and (time.time() - start) > cfg.timeout:
                msg = f"已达总超时 {cfg.timeout:.1f}s, 仍未找到目标弹窗"
                self.logger.error("超时 | %s", msg)
                return ClickResult(False, msg)

            attempt += 1
            res = self.backend.find_and_click(cfg)
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            if res.success:
                self.logger.info(
                    "成功 | 第%d次 | %s | %s", attempt, ts, res.detail
                )
                return ClickResult(True, res.detail, ts)
            self.logger.info(
                "未命中 | 第%d次 | %s | %s", attempt, ts, res.detail
            )
            time.sleep(cfg.detection_interval)


# ============================ 入口 ============================
def parse_args() -> ClickerConfig:
    p = argparse.ArgumentParser(description="桌面弹窗'批准'按钮自动点击工具")
    p.add_argument("--text", default="批准", help="要匹配的文本")
    p.add_argument("--button-text", default=None, help="按钮精确文本(windows)")
    p.add_argument("--title-hint", default="", help="窗口标题关键字")
    p.add_argument(
        "--backend", default="auto",
        choices=["auto", "ocr", "image", "windows"],
    )
    p.add_argument("--image", default=None, help="按钮截图路径(image 后端)")
    p.add_argument("--interval", type=float, default=1.0, help="检测间隔(秒)")
    p.add_argument("--retries", type=int, default=30, help="最大重试次数(0=不限)")
    p.add_argument("--timeout", type=float, default=0.0, help="总超时(秒, 0=不限)")
    p.add_argument("--confidence", type=float, default=0.8, help="图像置信度")
    p.add_argument("--log", default="logs/auto_clicker.log", help="日志文件")
    a = p.parse_args()
    return ClickerConfig(
        target_text=a.text,
        button_text=a.button_text or a.text,
        window_title_hint=a.title_hint,
        backend=a.backend,
        button_image=a.image,
        detection_interval=a.interval,
        max_retries=a.retries,
        timeout=a.timeout,
        confidence=a.confidence,
        log_file=a.log,
    )


def main() -> int:
    cfg = parse_args()
    logger = build_logger(cfg.log_file)
    clicker = AutoClicker(cfg, logger)
    result = clicker.run()
    return 0 if result.success else 1


if __name__ == "__main__":
    raise SystemExit(main())
