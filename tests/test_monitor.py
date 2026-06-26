import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import pandas as pd

import monitor


class YahooDownloadTests(unittest.TestCase):
    @patch.object(monitor.time, "sleep")
    @patch.object(monitor.random, "uniform", return_value=0)
    @patch.object(monitor.yf, "download")
    def test_empty_response_is_retried(self, download, _uniform, _sleep):
        valid = pd.DataFrame(
            {
                "Open": [1.0],
                "High": [1.1],
                "Low": [0.9],
                "Close": [1.05],
            }
        )
        download.side_effect = [pd.DataFrame(), valid]

        result = monitor._yf_download("EURUSD=X", "45d", "1h", retries=2)

        self.assertFalse(result.empty)
        self.assertEqual(download.call_count, 2)


class LiveStatusTests(unittest.TestCase):
    def test_ema_side_reports_each_timeframe_direction(self):
        self.assertEqual(monitor._ema_side(1.2, 1.1), "BULLISH")
        self.assertEqual(monitor._ema_side(1.0, 1.1), "BEARISH")
        self.assertEqual(monitor._ema_side(1.1, 1.1), "FLAT")

    def test_forex_weekend_is_reported_closed(self):
        sunday_before_open = datetime(2026, 6, 14, 20, 0, tzinfo=timezone.utc)
        sunday_after_open = datetime(2026, 6, 14, 21, 30, tzinfo=timezone.utc)

        self.assertEqual(monitor._market_state(sunday_before_open), "CLOSED")
        self.assertEqual(monitor._market_state(sunday_after_open), "OPEN")
        self.assertEqual(
            monitor._market_state(sunday_before_open, market_kind="CRYPTO"),
            "OPEN",
        )

    def test_red_and_cyan_union_matches_tradingview_lists(self):
        self.assertEqual(len(monitor.PAIRS), 34)
        self.assertEqual(
            monitor.RED_PAIRS,
            {"GBPAUD", "GBPUSD", "EURAUD"},
        )
        self.assertTrue(
            {"NZDCAD", "BTCUSD", "LTCUSDT", "US30USD", "VIX", "TSLA", "EURNZD", "NZDCHF"}
            .issubset(monitor.PAIRS)
        )

    def test_ema_stack_marks_high_conviction_reaction_level(self):
        stack = monitor._ema_stack_reaction(
            price=1.204,
            ema_4h=1.2,
            ema_2h=1.201,
            tf_4h="BULLISH",
            tf_2h="BULLISH",
            ema_4h_slope="UP",
            ema_2h_slope="UP",
        )

        self.assertTrue(stack["ema_stack_aligned"])
        self.assertTrue(stack["ema_stack_high_conviction"])
        self.assertEqual(stack["ema_stack_side"], "BULLISH")
        self.assertEqual(stack["ema_stack_level"], 1.2005)

    def test_trade_plan_keeps_structure_as_king(self):
        status = {
            "direction": "SHORT",
            "ema_aligned": True,
            "daily_trend": "RANGING",
            "h4_trend": "BEARISH",
            "trends_agree": False,
            "bos_detected": True,
            "ema_stack_aligned": True,
            "ema_stack_high_conviction": True,
            "ema_stack_level": 1.2,
            "ema_stack_distance_pct": 0.05,
            "in_golden_zone": True,
            "fib_sr_overlap": True,
            "at_sr": True,
            "has_signal": True,
            "signal_15m": "Engulfing (Bearish)",
            "pullback_valid": True,
        }

        plan = monitor._derive_trade_plan(status)

        self.assertEqual(plan["strategy_stage"], 1)
        self.assertEqual(plan["strategy_grade"], "A")
        self.assertIn("Daily and 4H structure agreement", plan["missing_reasons"])

    def test_15m_fetch_waits_for_real_setup_location(self):
        base = {
            "direction": "LONG",
            "trends_agree": True,
            "all_trends_agree": False,
            "at_sr": False,
            "in_golden_zone": False,
            "fib_sr_overlap": False,
            "ema_stack_high_conviction": False,
            "is_retest": False,
        }

        self.assertFalse(monitor._should_fetch_15m_signal(base))

        near_stack = dict(base, ema_stack_high_conviction=True)
        self.assertTrue(monitor._should_fetch_15m_signal(near_stack))

        bad_structure = dict(near_stack, trends_agree=False, all_trends_agree=False)
        self.assertFalse(monitor._should_fetch_15m_signal(bad_structure))

    @patch.object(monitor, "_save_pair_status_to_disk")
    def test_tradingview_event_updates_dashboard_immediately(self, save_status):
        original = dict(monitor.pair_status)
        try:
            monitor.pair_status.clear()
            updated = monitor.record_live_event("EURAUD", {
                "price": 1.63865,
                "direction": "LONG",
                "stage": 5,
                "tf_d": "bullish",
                "tf_4h": "bearish",
            })

            self.assertEqual(updated["watchlist"], "Red")
            self.assertEqual(updated["source_status"], "Live event")
            self.assertEqual(updated["tf_d"], "BULLISH")
            self.assertEqual(updated["tf_4h"], "BEARISH")
            self.assertFalse(updated["stale"])
            save_status.assert_called_once()
        finally:
            monitor.pair_status.clear()
            monitor.pair_status.update(original)


if __name__ == "__main__":
    unittest.main()
