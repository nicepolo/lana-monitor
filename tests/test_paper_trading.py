import unittest
from datetime import datetime, timezone

from paper_trading import mark_positions, new_book, open_trade, record_signal


class PaperTradingTests(unittest.TestCase):
    def setUp(self):
        self.book = new_book({
            "capital": 10000, "risk_pct": 0.5, "min_signal_score": 70,
            "max_open_positions": 3, "leverage": 3,
            "fee_rate": 0.0005, "slippage_rate": 0.0005,
            "trailing_pct": 0.02, "max_hold_hours": 24,
        })
        self.signal = {
            "signal_id": "signal-1", "strategy_version": "test",
            "coin": "TEST", "direction": "LONG", "score": 80,
            "entry_zone": 100, "stop_loss": 95,
            "target_1": 105, "target_2": 110,
        }

    def test_duplicate_signal_cannot_open_twice(self):
        _, created = record_signal(self.book, self.signal)
        self.assertTrue(created)
        trade, reason = open_trade(self.book, self.signal)
        self.assertEqual(reason, "opened")
        duplicate, reason = open_trade(self.book, self.signal)
        self.assertIsNone(duplicate)
        self.assertEqual(reason, "duplicate_signal")
        self.assertEqual(len(self.book["trades"]), 1)
        self.assertGreater(trade["risk_amount"], 0)

    def test_watch_signal_never_opens(self):
        signal = dict(self.signal, signal_id="watch-1", direction="WATCH")
        trade, reason = open_trade(self.book, signal)
        self.assertIsNone(trade)
        self.assertEqual(reason, "watch_signal")

    def test_partial_take_profit_then_trailing_exit(self):
        trade, _ = open_trade(self.book, self.signal)
        now = datetime.now(timezone.utc)
        mark_positions(self.book, {"TEST": 105}, now)
        self.assertTrue(trade["tp1_hit"])
        self.assertLess(trade["remaining_qty"], trade["original_qty"])

        mark_positions(self.book, {"TEST": 110}, now)
        self.assertTrue(trade["tp2_hit"])
        self.assertEqual(trade["status"], "OPEN")

        mark_positions(self.book, {"TEST": 107}, now)
        self.assertEqual(trade["status"], "CLOSED")
        self.assertEqual(trade["exit_reason"], "TRAILING_STOP")
        self.assertGreater(trade["realized_pnl"], 0)

    def test_stop_loss_closes_position(self):
        trade, _ = open_trade(self.book, self.signal)
        mark_positions(self.book, {"TEST": 94}, datetime.now(timezone.utc))
        self.assertEqual(trade["status"], "CLOSED")
        self.assertEqual(trade["exit_reason"], "STOP_LOSS")
        self.assertLess(trade["realized_pnl"], 0)


if __name__ == "__main__":
    unittest.main()
