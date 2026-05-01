"""
ZSCN Monitor — Pair Scanner
Checks all 28 pairs every 15 minutes using yfinance (free, no API key).
Alerts when EMA 50 is aligned across all 4 timeframes:
  - Daily: EMA below/above price
  - 4H:    EMA same side + curving in that direction
  - 2H:    EMA same side + curving in that direction
  - 1H:    EMA same side
Harry places the fib himself — this bot only watches EMA alignment.
"""

import logging
import threading
from datetime import datetime, timezone, timedelta

import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)

PAIRS = {
    "EURUSD": "EURUSD=X",
    "GBPUSD": "GBPUSD=X",
    "AUDUSD": "AUDUSD=X",
    "NZDUSD": "NZDUSD=X",
    "USDCAD": "USDCAD=X",
    "USDJPY": "USDJPY=X",
    "USDCHF": "USDCHF=X",
    "GBPJPY": "GBPJPY=X",
    "EURJPY": "EURJPY=X",
    "GBPAUD": "GBPAUD=X",
    "GBPNZD": "GBPNZD=X",
    "GBPCAD": "GBPCAD=X",
    "GBPCHF": "GBPCHF=X",
    "EURCAD": "EURCAD=X",
    "EURGBP": "EURGBP=X",
    "EURAUD": "EURAUD=X",
    "EURNZD": "EURNZD=X",
    "EURCHF": "EURCHF=X",
    "AUDCAD": "AUDCAD=X",
    "AUDNZD": "AUDNZD=X",
    "AUDJPY": "AUDJPY=X",
    "AUDCHF": "AUDCHF=X",
    "NZDJPY": "NZDJPY=X",
    "NZDCHF": "NZDCHF=X",
    "CADJPY": "CADJPY=X",
    "CADCHF": "CADCHF=X",
    "CHFJPY": "CHFJPY=X",
    "XAUUSD": "GC=F",
}

ALERT_COOLDOWN_HOURS = 4

pair_status    = {}
recent_alerts  = []
alert_cooldown = {}
_lock = threading.Lock()


def _ema(series: pd.Series, period: int = 50) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def _is_rising(series: pd.Series) -> bool:
    return float(series.iloc[-1]) > float(series.iloc[-2])


def _check_pair(name: str, yf_symbol: str) -> dict:
    status = {
        "pair": name,
        "price": None,
        "direction": "NONE",
        "ema_aligned": False,
        "ema_d_side": None,
        "ema_4h_curving": None,
        "ema_2h_curving": None,
        "last_checked": datetime.now(timezone.utc).strftime("%H:%M UTC"),
        "error": None,
    }

    try:
        # Fetch 1H data (covers 4H, 2H, 1H via resampling)
        df_1h = yf.download(yf_symbol, period="59d", interval="1h",
                            progress=False, auto_adjust=True)
        df_d  = yf.download(yf_symbol, period="200d", interval="1d",
                            progress=False, auto_adjust=True)

        if df_1h.empty or df_d.empty or len(df_1h) < 60 or len(df_d) < 55:
            status["error"] = "Insufficient data"
            return status

        # Flatten MultiIndex columns if present
        if isinstance(df_1h.columns, pd.MultiIndex):
            df_1h.columns = df_1h.columns.get_level_values(0)
        if isinstance(df_d.columns, pd.MultiIndex):
            df_d.columns = df_d.columns.get_level_values(0)

        # Resample to 4H and 2H
        ohlc = {"Open": "first", "High": "max", "Low": "min", "Close": "last"}
        df_4h = df_1h.resample("4h").agg(ohlc).dropna()
        df_2h = df_1h.resample("2h").agg(ohlc).dropna()

        if len(df_4h) < 55 or len(df_2h) < 55:
            status["error"] = "Not enough bars after resample"
            return status

        # EMA 50 on all 4 timeframes
        ema_d_s  = _ema(df_d["Close"])
        ema_4h_s = _ema(df_4h["Close"])
        ema_2h_s = _ema(df_2h["Close"])
        ema_1h_s = _ema(df_1h["Close"])

        price      = float(df_1h["Close"].iloc[-1])
        ema_d_val  = float(ema_d_s.iloc[-1])
        ema_4h_val = float(ema_4h_s.iloc[-1])
        ema_2h_val = float(ema_2h_s.iloc[-1])
        ema_1h_val = float(ema_1h_s.iloc[-1])

        ema_4h_rising = _is_rising(ema_4h_s)
        ema_2h_rising = _is_rising(ema_2h_s)

        # EMA alignment check
        bull = (price > ema_d_val and
                price > ema_4h_val and ema_4h_rising and
                price > ema_2h_val and ema_2h_rising and
                price > ema_1h_val)

        bear = (price < ema_d_val and
                price < ema_4h_val and not ema_4h_rising and
                price < ema_2h_val and not ema_2h_rising and
                price < ema_1h_val)

        status["price"]         = round(price, 5)
        status["ema_aligned"]   = bull or bear
        status["direction"]     = "LONG" if bull else ("SHORT" if bear else "NONE")
        status["ema_d_side"]    = "below" if bull else ("above" if bear else "mixed")
        status["ema_4h_curving"] = "up" if (bull and ema_4h_rising) else ("down" if (bear and not ema_4h_rising) else "-")
        status["ema_2h_curving"] = "up" if (bull and ema_2h_rising) else ("down" if (bear and not ema_2h_rising) else "-")

    except Exception as exc:
        logger.exception(f"[{name}] Error: {exc}")
        status["error"] = str(exc)

    return status


def run_scan(send_telegram_fn):
    logger.info("=== ZSCN scan started ===")
    results = {}
    threads = []

    def worker(name, yf_sym):
        results[name] = _check_pair(name, yf_sym)

    for name, yf_sym in PAIRS.items():
        t = threading.Thread(target=worker, args=(name, yf_sym))
        t.start()
        threads.append(t)

    for t in threads:
        t.join(timeout=30)

    now = datetime.now(timezone.utc)

    with _lock:
        for name, status in results.items():
            pair_status[name] = status

            if not status["ema_aligned"]:
                continue

            direction     = status["direction"]
            cooldown_key  = f"{name}_{direction}"
            last_alerted  = alert_cooldown.get(cooldown_key)

            if last_alerted and (now - last_alerted) < timedelta(hours=ALERT_COOLDOWN_HOURS):
                logger.info(f"[{name}] {direction} - skipping (cooldown)")
                continue

            alert_cooldown[cooldown_key] = now
            alert = {**status, "received_at": now.strftime("%Y-%m-%d %H:%M UTC")}
            recent_alerts.insert(0, alert)
            if len(recent_alerts) > 100:
                recent_alerts.pop()

            send_telegram_fn(status)
            logger.info(f"[{name}] {direction} EMA aligned - alert sent")

    logger.info(f"=== Scan complete. {len(results)} pairs checked ===")
