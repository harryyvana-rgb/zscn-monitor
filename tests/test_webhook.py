import os
import unittest
from unittest.mock import patch

os.environ["ZSCN_DISABLE_SCHEDULER"] = "1"

import app


class WebhookTests(unittest.TestCase):
    def setUp(self):
        self.client = app.app.test_client()
        app.active_webhook_setups.clear()

    def test_plain_text_alert_is_rejected_with_helpful_error(self):
        response = self.client.post(
            "/webhook",
            data="ZSCN: Trade ready",
            content_type="text/plain",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("Expected a JSON body", response.get_json()["error"])

    def test_configured_webhook_secret_is_required(self):
        with patch.dict(os.environ, {"TRADINGVIEW_WEBHOOK_SECRET": "test-secret"}):
            response = self.client.post(
                "/webhook",
                json={"pair": "EURUSD", "direction": "LONG", "stage": 5, "price": 1.15},
            )
        self.assertEqual(response.status_code, 401)

    def test_validate_endpoint_checks_secret_without_recording_event(self):
        with patch.dict(os.environ, {"TRADINGVIEW_WEBHOOK_SECRET": "test-secret"}):
            blocked = self.client.post(
                "/webhook/validate",
                json={"pair": "OANDA:GBPUSD", "stage": 6, "event": "bridge_test"},
            )
            allowed = self.client.post(
                "/webhook/validate",
                headers={"X-Trade-Secret": "test-secret"},
                json={"pair": "OANDA:GBPUSD", "stage": 6, "event": "bridge_test"},
            )

        self.assertEqual(blocked.status_code, 401)
        self.assertEqual(allowed.status_code, 200)
        body = allowed.get_json()
        self.assertTrue(body["validated_only"])
        self.assertEqual(body["pair"], "GBPUSD")
        self.assertEqual(app.active_webhook_setups, {})

    @patch.object(app, "send_telegram")
    @patch.object(app, "record_live_event")
    def test_stage_6_registers_trade_ready_setup(self, record_live_event, send_telegram):
        response = self.client.post(
            "/webhook",
            json={
                "pair": "OANDA:EURUSD",
                "direction": "LONG",
                "stage": 6,
                "price": 1.15,
                "sl": 1.14,
                "tp": 1.18,
                "fib_50": 1.151,
                "fib_618": 1.149,
                "in_fib": True,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("EURUSD", app.active_webhook_setups)
        record_live_event.assert_called_once()
        send_telegram.assert_called_once()

    @patch.object(app, "send_invalidation_alert")
    @patch.object(app, "record_live_event")
    def test_stage_7_invalidates_active_setup(self, record_live_event, send_invalidation_alert):
        app.active_webhook_setups["EURUSD"] = {
            "pair": "EURUSD",
            "direction": "LONG",
        }
        response = self.client.post(
            "/webhook",
            json={
                "pair": "EURUSD",
                "direction": "LONG",
                "stage": 7,
                "price": 1.14,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("EURUSD", app.active_webhook_setups)
        record_live_event.assert_called_once()
        send_invalidation_alert.assert_called_once()


if __name__ == "__main__":
    unittest.main()
