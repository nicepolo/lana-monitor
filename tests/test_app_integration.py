import unittest
from unittest.mock import patch

import app
from position_assistant import new_store, open_position


class AppIntegrationTests(unittest.TestCase):
    def setUp(self):
        app._ai_analyze_cache.clear()

    def test_health_exposes_safe_mode(self):
        response = app.app.test_client().get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json["paper_trading"])
        self.assertTrue(response.json["position_assistant"])
        self.assertEqual(response.json["strategy_version"], "lana-direction-v1")

    def test_ai_status_never_exposes_keys(self):
        with patch.dict(app.os.environ, {"GEMINI_API_KEY": "secret", "ANTHROPIC_API_KEY": ""}):
            response = app.app.test_client().get("/api/ai/status")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json["providers"]["gemini"]["configured"])
        self.assertNotIn("secret", response.get_data(as_text=True))

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
             patch.object(app, "fetch_position_price", return_value={
                 "price": 100, "price_source": "BINANCE_LIVE", "price_ts": 123,
             }), \
             patch.object(app, "PAPER_TRADING_ENABLED", False), \
             patch.dict(app.os.environ, {"GEMINI_API_KEY": "", "ANTHROPIC_API_KEY": ""}):
            result = app._do_ai_analyze("TEST", 103, 12)

        self.assertEqual(result["direction"], "LONG")
        self.assertEqual(result["signal_id"], "stable-signal")
        self.assertEqual(result["model"], "rules")
        self.assertEqual(result["ai_fallback_reason"]["gemini"], "not_configured")
        self.assertLess(result["stop_loss"], result["entry_zone"])
        self.assertGreater(result["target_2"], result["target_1"])

    def test_ai_signal_is_vetoed_when_live_price_drift_is_too_large(self):
        snapshot = {
            "rsi": 40, "vol_ratio": 1.8, "funding_rate": 0.01,
            "ma_bear": True, "bb_position": "lower_half", "price": 3.374,
            "change_24h": -15, "lana_score": 88,
            "rule_direction": "SHORT", "long_score": 18, "short_score": 88,
            "direction_score": 88, "score_gap": 70,
            "signal_id": "lab-stale-entry", "feature_hash": "stale-features",
            "strategy_version": "lana-direction-v1", "direction_reason": "downtrend",
            "atr": 0.1, "recent_high": 3.5, "recent_low": 3.2,
            "candle_close_ts": 1234567890000,
        }
        with patch.object(app, "analyze_coin", return_value=snapshot), \
             patch.object(app, "fetch_klines", return_value=[]), \
             patch.object(app, "fetch_position_price", return_value={
                 "price": 2.874, "price_source": "BINANCE_LIVE", "price_ts": 123,
             }), \
             patch.object(app, "PAPER_TRADING_ENABLED", False), \
             patch.dict(app.os.environ, {"GEMINI_API_KEY": "", "ANTHROPIC_API_KEY": ""}):
            result = app._do_ai_analyze("LAB", 3.374, -15)

        self.assertEqual(result["direction"], "WATCH")
        self.assertEqual(result["arbiter_reason"], "entry_price_drift_guard")
        self.assertGreater(result["entry_drift_pct"], 10)
        self.assertEqual(result["entry_zone"], 0)

    def test_position_monitor_uses_live_quote_not_closed_candle_price(self):
        store = new_store()
        position, _ = open_position(store, {
            "signal_id": "live-price-test", "coin": "LIT", "direction": "LONG",
            "entry_zone": 100, "stop_loss": 95, "target_1": 110, "target_2": 120,
            "exchange": "BINANCE",
        })
        stale_technical = {
            "coin": "LIT", "price": 101, "rule_direction": "LONG", "direction_score": 80,
        }
        with patch.object(app, "_load_positions", return_value=store), \
             patch.object(app, "_save_positions"), \
             patch.object(app, "analyze_coin", return_value=stale_technical), \
             patch.object(app, "fetch_position_price", return_value={
                 "price": 94, "price_source": "BINANCE_LIVE", "price_ts": 123,
             }):
            response = app.app.test_client().post("/api/positions/monitor")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["alerts"][0]["action"], "CLOSE")
        self.assertEqual(position["last_price"], 94)
        self.assertEqual(position["price_source"], "BINANCE_LIVE")


if __name__ == "__main__":
    unittest.main()
