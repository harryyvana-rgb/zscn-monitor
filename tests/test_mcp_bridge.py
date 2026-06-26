import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

from scripts import mcp_bridge


class MCPBridgeTests(unittest.TestCase):
    def test_normalizes_old_trade_ready_stage_to_render_stage_6(self):
        payload = mcp_bridge.normalize_event({
            "pair": "OANDA:GBPUSD",
            "stage": 5,
            "direction": "SHORT",
            "price": "1.2501",
            "signal": "TRADE READY",
            "ema_d": "above",
            "ema_4h": "above curving down",
            "ema_2h": "above curving down",
            "ema_1h": "above",
            "in_fib": "true",
        })

        self.assertEqual(payload["pair"], "GBPUSD")
        self.assertEqual(payload["direction"], "SHORT")
        self.assertEqual(payload["stage"], 6)
        self.assertEqual(payload["tf_d"], "BEARISH")
        self.assertEqual(payload["tf_4h"], "BEARISH")
        self.assertEqual(payload["ema_4h_slope"], "DOWN")
        self.assertTrue(payload["in_golden_zone"])
        self.assertNotIn("ema_4h", payload)

    def test_computes_ema_stack_from_numeric_values(self):
        payload = mcp_bridge.normalize_event({
            "symbol": "FX:GBPAUD",
            "direction": "LONG",
            "price": 1.914,
            "ema_4h": 1.91,
            "ema_2h": 1.911,
            "tf_4h": "bullish",
            "tf_2h": "bullish",
        })

        self.assertTrue(payload["ema_stack_aligned"])
        self.assertTrue(payload["ema_stack_high_conviction"])
        self.assertEqual(payload["ema_stack_side"], "BULLISH")

    def test_invalidated_signal_maps_to_render_stage_7(self):
        payload = mcp_bridge.normalize_event({
            "ticker": "EURAUD",
            "stage": 6,
            "direction": "LONG",
            "signal": "INVALIDATED",
        })

        self.assertEqual(payload["stage"], 7)

    def test_bridge_event_log_records_forwarded_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "events.jsonl"
            with patch.dict("os.environ", {"ZSCN_BRIDGE_EVENT_LOG": str(log_path)}):
                mcp_bridge.append_event_log(
                    {"pair": "GBPUSD", "stage": 6, "event": "trade_ready", "direction": "SHORT"},
                    {"status": 200},
                )
                events = mcp_bridge.read_event_log()

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["pair"], "GBPUSD")
        self.assertEqual(events[0]["stage"], 6)
        self.assertTrue(events[0]["ok"])


if __name__ == "__main__":
    unittest.main()
