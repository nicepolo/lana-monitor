import unittest

from lana_strategy import (
    arbitrate_ai_result,
    build_trade_levels,
    make_signal_id,
    score_direction,
)


def features(**overrides):
    data = {
        "coin": "TEST", "timeframe": "1h", "candle_close_ts": 1234567890000,
        "price": 100.0, "change_24h": 9.0,
        "ma7": 110.0, "ma30": 100.0, "ma120": 90.0,
        "rsi": 60.0, "vol_ratio": 1.8, "bb_position": "upper_half",
        "funding_rate": 0.01, "oi_change_24h": 10.0,
        "atr": 2.0, "recent_high": 104.0, "recent_low": 96.0,
    }
    data.update(overrides)
    return data


class DirectionTests(unittest.TestCase):
    def test_strong_long_and_short_are_symmetric(self):
        long_result = score_direction(features())
        short_result = score_direction(features(
            change_24h=-9.0, ma7=90.0, ma30=100.0, ma120=110.0,
            rsi=40.0, bb_position="lower_half",
        ))
        self.assertEqual(long_result["direction"], "LONG")
        self.assertEqual(short_result["direction"], "SHORT")
        self.assertGreaterEqual(long_result["selected_score"], 70)
        self.assertGreaterEqual(short_result["selected_score"], 70)

    def test_ambiguous_snapshot_is_watch(self):
        result = score_direction(features(
            change_24h=0, ma7=100, ma30=100, ma120=100,
            rsi=50, vol_ratio=1, bb_position="middle", oi_change_24h=None,
        ))
        self.assertEqual(result["direction"], "WATCH")

    def test_ai_cannot_reverse_rule_direction(self):
        rules = score_direction(features())
        result = arbitrate_ai_result(rules, {"direction": "SHORT", "score": 90})
        self.assertEqual(result["direction"], "WATCH")
        self.assertIn("不一致", result["arbiter_reason"])

    def test_ai_confirmation_keeps_rule_direction(self):
        rules = score_direction(features())
        result = arbitrate_ai_result(rules, {"direction": "LONG", "score": 80})
        self.assertEqual(result["direction"], "LONG")
        self.assertGreaterEqual(result["score"], 70)

    def test_signal_id_is_stable_per_snapshot(self):
        original = features()
        self.assertEqual(make_signal_id(original), make_signal_id(dict(original)))
        # Live price/derivatives may drift, but the same closed candle stays one signal.
        self.assertEqual(make_signal_id(original), make_signal_id(features(price=101, change_24h=10)))
        changed = features(candle_close_ts=1234567890001)
        self.assertNotEqual(make_signal_id(original), make_signal_id(changed))

    def test_trade_levels_have_correct_sides(self):
        long_levels = build_trade_levels(features(), "LONG")
        short_levels = build_trade_levels(features(), "SHORT")
        self.assertLess(long_levels["stop_loss"], long_levels["entry_zone"])
        self.assertGreater(long_levels["target_2"], long_levels["target_1"])
        self.assertGreater(short_levels["stop_loss"], short_levels["entry_zone"])
        self.assertLess(short_levels["target_2"], short_levels["target_1"])


if __name__ == "__main__":
    unittest.main()
