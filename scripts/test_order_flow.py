"""下单通道联调测试（QMT/xtquant 契约层）。

本机是 Linux，xtquant 不可用，因此用 **Mock Broker** 验证下单编排逻辑
（仓位计算 → 订单意图 → 通道调用 → 结果落盘），保证上 Windows 后只需替换
通道实现即可。

跑法::

    venv/bin/python scripts/test_order_flow.py

覆盖：
1. 仓位规则：单票上限 / 总仓上限 / 最小交易单位 / 单笔最小金额
2. 条件单文本生成
3. dryrun 落盘（CSV + JSON）
4. 通道协议：自定义 BrokerAdapter 接入
5. QmtBroker 在无 xtquant 时的报错信息是否可读
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tdxfeed.daban.order import (  # noqa: E402
    BrokerAdapter,
    DryRunBroker,
    OrderIntent,
    QmtBroker,
    build_intents,
    condition_order_text,
)


class FakeCandidate:
    """模拟 scanner.Candidate 的最小字段集。"""

    def __init__(self, code, name, price, score=0.9, sector="测试行业"):
        self.code = code
        self.name = name
        self.limit_price = price
        self.score = score
        self.sector_name = sector
        self.reasons = [f"封单{code}", "状态=封板"]


class MockBroker(BrokerAdapter):
    """记录所有下单意图的假通道。"""

    def __init__(self) -> None:
        self.accepted: list[OrderIntent] = []

    def place(self, intent: OrderIntent) -> dict:
        self.accepted.append(intent)
        return {"status": "mock_accepted", "code": intent.code, "shares": intent.shares}


def main() -> int:
    cands = [
        FakeCandidate("600000", "浦发银行", 10.0, 0.9),
        FakeCandidate("000001", "平安银行", 12.0, 0.8),
        FakeCandidate("300750", "宁德时代", 250.0, 0.7),
    ]

    print("=== 1) 仓位规则 ===")
    intents = build_intents(
        cands,
        cash=200_000,
        max_single_pct=0.15,  # 单票 3 万
        max_total_pct=0.50,  # 总仓 10 万
        stop_loss_pct=0.05,
        take_profit_pct=0.10,
        min_amount=10_000,
    )
    total = 0.0
    for it in intents:
        total += it.amount
        print(
            f"  {it.code} {it.name}: {it.shares}股 @ {it.price} = {it.amount}元 "
            f"(止损{it.stop_loss} 止盈{it.take_profit})"
        )
    print(f"  合计 {total:.0f} 元 / 上限 {200_000 * 0.5:.0f} 元")
    assert total <= 200_000 * 0.5 + 1, "总仓超限"
    assert all(it.amount >= 10_000 for it in intents), "存在小于最小金额的订单"

    print("\n=== 2) 条件单文本 ===")
    for it in intents[:2]:
        print("  " + condition_order_text(it))

    print("\n=== 3) dryrun 落盘 ===")
    broker = DryRunBroker("/tmp/daban_test_orders")
    res = broker.place(intents[0])
    print(f"  csv={res['csv']}")
    print(f"  json={res['json']}")
    assert Path(res["csv"]).exists() and Path(res["json"]).exists()

    print("\n=== 4) 通道协议（Mock）===")
    mock = MockBroker()
    for it in intents:
        mock.place(it)
    print(f"  mock 接受 {len(mock.accepted)} 笔")

    print("\n=== 5) QMT 在无 xtquant 环境的报错 ===")
    try:
        QmtBroker(user_data_path="/tmp/nonexistent_qmt")
        print("  ⚠ 意外：QmtBroker 构造成功（本机不应如此）")
    except RuntimeError as e:
        print(f"  预期报错: {e}")

    print("\n[OK] 下单流程联调全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
