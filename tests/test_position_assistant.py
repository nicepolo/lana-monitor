import unittest
from datetime import datetime, timedelta, timezone

from position_assistant import close_position, monitor_positions, new_store, open_position, update_entry


BASE_SIGNAL = {
    "signal_id": "signal-1", "coin": "LIT", "direction": "LONG",
    "entry_zone": 100, "stop_loss": 95, "target_1": 110, "target_2": 120,
    "strategy_version": "lana-direction-v1",
}


class PositionAssistantTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 7, 6, 4, 0, tzinfo=timezone.utc)
        self.store = new_store()
        self.position, reason = open_position(self.store, BASE_SIGNAL, self.now)
        self.assertEqual(reason, "tracking_started")

    def test_duplicate_and_same_coin_are_blocked(self):
        same, reason = open_position(self.store, BASE_SIGNAL, self.now)
        self.assertEqual(reason, "already_tracking")
        self.assertEqual(same["position_id"], self.position["position_id"])
        other = dict(BASE_SIGNAL, signal_id="signal-2")
        position, reason = open_position(self.store, other, self.now)
        self.assertIsNone(position)
        self.assertEqual(reason, "coin_already_tracking")

    def test_add_only_after_profit_and_same_strong_direction(self):
        alerts, _ = monitor_positions(self.store, {"LIT": {
            "price": 103, "rule_direction": "LONG", "direction_score": 80,
        }}, self.now + timedelta(minutes=15))
        self.assertEqual([a["action"] for a in alerts], ["ADD"])
        alerts, _ = monitor_positions(self.store, {"LIT": {
            "price": 104, "rule_direction": "LONG", "direction_score": 90,
        }}, self.now + timedelta(minutes=30))
        self.assertEqual(alerts, [])

    def test_stop_alert_is_sent_once(self):
        snap = {"LIT": {"price": 94, "rule_direction": "SHORT", "direction_score": 90}}
        alerts, _ = monitor_positions(self.store, snap, self.now + timedelta(minutes=15))
        self.assertEqual([a["action"] for a in alerts], ["CLOSE"])
        alerts, _ = monitor_positions(self.store, snap, self.now + timedelta(minutes=30))
        self.assertEqual(alerts, [])

    def test_targets_reduce_and_raise_stop(self):
        alerts, _ = monitor_positions(self.store, {"LIT": {
            "price": 110, "rule_direction": "LONG", "direction_score": 82,
        }}, self.now + timedelta(minutes=15))
        self.assertEqual([a["action"] for a in alerts], ["REDUCE_50"])
        self.assertGreaterEqual(self.position["stop_loss"], 100)
        alerts, _ = monitor_positions(self.store, {"LIT": {
            "price": 120, "rule_direction": "LONG", "direction_score": 82,
        }}, self.now + timedelta(minutes=30))
        self.assertEqual([a["action"] for a in alerts], ["REDUCE_30"])

    def test_hourly_hold_and_manual_close(self):
        alerts, _ = monitor_positions(self.store, {"LIT": {
            "price": 101, "rule_direction": "LONG", "direction_score": 70,
        }}, self.now + timedelta(minutes=60))
        self.assertEqual([a["action"] for a in alerts], ["HOLD"])
        position, reason = close_position(self.store, self.position["position_id"], now=self.now)
        self.assertEqual(reason, "closed")
        self.assertEqual(position["status"], "CLOSED")

    def test_actual_fill_and_exchange_are_recorded(self):
        store = new_store()
        signal = dict(BASE_SIGNAL, actual_entry_price=101, exchange="BINANCE")
        position, reason = open_position(store, signal, self.now)
        self.assertEqual(reason, "tracking_started")
        self.assertEqual(position["entry_price"], 101)
        self.assertEqual(position["signal_entry_price"], 100)
        self.assertEqual(position["exchange"], "BINANCE")

    def test_user_can_calibrate_actual_fill(self):
        position, reason = update_entry(self.store, "LIT", 102, "BINANCE")
        self.assertEqual(reason, "updated")
        self.assertEqual(position["entry_price"], 102)
        self.assertEqual(position["initial_risk"], 7)
        self.assertEqual(position["entry_source"], "user_reported")


if __name__ == "__main__":
    unittest.main()
