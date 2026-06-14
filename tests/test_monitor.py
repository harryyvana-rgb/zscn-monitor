import unittest
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


if __name__ == "__main__":
    unittest.main()
