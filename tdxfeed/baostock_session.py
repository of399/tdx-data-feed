"""baostock 会话的超时与登录重试（单源）。

**为什么登录和查询必须是两个超时数**（2026-11-04 实测）：
`tdxfeed/adj_factor.py` 与 `tdxfeed/hfq_factor.py` 都在 import 时执行
`socket.setdefaulttimeout(30)`，然后用裸 `bs.login()`。而 baostock 的**登录握手**
在当日 EOD 发布窗口要 ~60 秒——30 秒的上限把握手自己截断了：`login()` 返回
`10002007 网络接收错误`，模块 `return 1`，再被单元里的 `ExecStartPost=-` 吞成
`Finished`，整条后复权链就这么静默落空。同夜成对实验（只有一处变量）：走模块路径
`--limit 1` → 33 秒失败；裸登录 + 同样 30 秒上限 → 31 秒同一错误码；不设上限 →
60 秒 `login success`。所以紧上限只对**逐只查询**有意义（防止一只挂死的票拖住整轮），
登录必须单独放宽。

日志里能区分这两种失败：自家截断的下两行是 `timed out`，真服务端故障是
`服务器连接失败` / `[Errno 32] Broken pipe`。`logs/tdx-systemd.log` 现存 4 次登录失败
里只有 11-04 那次是前者（10-29 一次、10-30 两次是后者）——放宽到 120 秒治前者，
有界重试 3 次治后者，两件事分别在案，别并成"baostock 不稳定"一句话。

约定（不可协商）：
- 生产码不得再写 `socket.setdefaulttimeout(30)` 字面量，也不得裸调 `bs.login()`；
  两个数与重试都从这里取。守卫 `tests/test_baostock_login_retry.py`。
- `login_with_retry` 返回 `(ok, msg)` 而不是抛异常：调用方要按既有形状打印后
  `return 1`，把失败留在退出码里（`-` 前缀会吞掉判红，所以措辞要带上尝试次数，
  让人一眼看出这是"重试过还是不行"，不是"没试"）。
"""
from __future__ import annotations

import socket
import time
from collections import Counter
from collections.abc import Callable
from typing import Any

# 吞错计数（SEM002）：登录抛出的异常被吞成"这次没登录上"，必须计入统计。
_ERROR_COUNT: Counter[str] = Counter()

# 逐只查询的紧上限：沿用改造前的 30 秒，行为不变。
QUERY_TIMEOUT_SEC = 30

# 登录的宽上限：实测慢握手 60 秒，给两倍。
LOGIN_TIMEOUT_SEC = 120

# 登录最多试几次，以及每次失败后睡多久（最后一次不睡）。
LOGIN_ATTEMPTS = 3
LOGIN_BACKOFF_SEC = (15, 45)


def apply_query_timeout() -> None:
    """把 socket 默认超时设回逐只查询的紧上限（模块 import 时调用一次）。"""
    socket.setdefaulttimeout(QUERY_TIMEOUT_SEC)


def login_with_retry(bs: Any, *,
                     attempts: int = LOGIN_ATTEMPTS,
                     login_timeout: int = LOGIN_TIMEOUT_SEC,
                     query_timeout: int = QUERY_TIMEOUT_SEC,
                     sleep: Callable[[float], None] = time.sleep) -> tuple[bool, str]:
    """登录并做有界重试；登录期间放宽 socket 上限，返回前一定恢复紧上限。

    `sleep` 可注入：测试传假的，避免 CI 真等 60 秒。
    返回 `(是否成功, 说明)`；说明带上第几次成功 / 最后一次的错误。
    """
    last = "未尝试"
    socket.setdefaulttimeout(login_timeout)
    try:
        for n in range(1, attempts + 1):
            try:
                lg = bs.login()
            except Exception as e:  # SEM002：登录抛异常按"这次没上"处理，但必须计数
                _ERROR_COUNT["login_with_retry"] += 1
                last = f"{type(e).__name__}: {e}"
            else:
                if getattr(lg, "error_code", None) == "0":
                    return True, f"第 {n}/{attempts} 次成功"
                last = getattr(lg, "error_msg", None) or "未知错误"
            if n < attempts:
                # 退避只影响 sleep；上限已经在 try 之前放宽，异常也要走 finally 恢复。
                sleep(LOGIN_BACKOFF_SEC[min(n - 1, len(LOGIN_BACKOFF_SEC) - 1)])
        return False, last
    finally:
        # 无论成败，返回后都回到逐只查询的紧上限——一只挂死的票不该拖住整轮。
        socket.setdefaulttimeout(query_timeout)


def describe_failure(attempts: int, msg: str) -> str:
    """登录彻底失败时给单元日志用的一行（带尝试次数，便于区分"没试"与"试过"）。"""
    return f"baostock 登录失败（已重试 {attempts} 次，登录上限 {LOGIN_TIMEOUT_SEC}s）: {msg}"
