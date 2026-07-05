import unittest
from unittest.mock import patch

import app


class AppIntegrationTests(unittest.TestCase):
    def setUp(self):
        app._ai_analyze_cache.clear()

    def test_health_exposes_safe_mode(self):
        response = app.app.test_client().get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json["paper_trading"])
        self.assertEqual(response.json["strategy_version"], "lana-direction-v1")

    def test_rules_fallback_produces_deterministic_long(self):
        snapshot = {
            "rsi": 60, "vol_ratio": 1.8, "funding_rate": 0.01,
            "ma_bull": True, "bb_position": "upper_half", "price": 100,
            "change_24h": 9, "lana_score": 85,
            "rule_direction": "LONG", "long_score": 91, "short_score": 20,
            "direction_score": 91, "score_gap": 71,
            "signal_id": "stable-signal", "feature_hash": "stable-features",
            "strategy_version": "lana-direction-v1", "direction_reason": "MA 多頭排列",
            "atr": 2, "recent_high": 104, "recent_low": 96,
            "candle_close_ts": 1234567890000,
        }
        with patch.object(app, "analyze_coin", return_value=snapshot), \
             patch.object(app, "fetch_klines", return_value=[]), \
             patch.object(app, "PAPER_TRADING_ENABLED", False), \
             patch.dict(app.os.environ, {"GEMINI_API_KEY": "", "ANTHROPIC_API_KEY": ""}):
            result = app._do_ai_analyze("TEST", 103, 12)

        self.assertEqual(result["direction"], "LONG")
        self.assertEqual(result["signal_id"], "stable-signal")
        self.assertEqual(result["model"], "rules")
        self.assertLess(result["stop_loss"], result["entry_zone"])
        self.assertGreater(result["target_2"], result["target_1"])


if __name__ == "__main__":
    unittest.main()
