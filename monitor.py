"""
ZSCN Monitor — Pair Scanner
Checks all 28 pairs every 15 minutes using yfinance (free, no API key).
Sends Telegram alert when Harry's golden zone setup is detected:
  - EMA 50 aligned on D + 4H (curving) + 2H (curving) + 1H
  - Price in 50–61.8% fib retracement zone
"""

import logging
import threading
from datetime import datetime, timezone, timedelta

import numpy as np
import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)

# ── Pair map: TradingView symbol → yfinance symbol ──────────────────────────
PAIRS = {
    "EURUSD":  "EURUSD=X",
    "GBPUSD":  "GBPUSD=X",
    "AUDUSD":  "AUDUSD=X",
    "NZDUSD":  "NZDUSD=X",
    "USDCAD":  "USDCAD=X",
    "USDJPY":  "USDJPY=X",
    "USDCHF":  "USDCHF=X",
    "GBPJPY":  "GBPJPY=X",
    "EURJPY":  "EURJPY=X",
    "GBPAUD":  "GBPAUD=X",
    "GBPNZD":  "GBPNZD=X",
    "GBPCAD":  "GBPCAD=X",
    "GBPCHF":  "GBPCHF=X",
    "EURCAD":  "EURCAD=X",
    "EURGBP":  "EURGBP=X",
    "EURAUD":  "EURAUD=X",
    "EURNZD":  "EURNZD=X",
    "EURCHF":  "EURCHF=X",
    "AUDCAD":  "AUDCAD=X",
    "AUDNZD":  "AUDNZD=X",
    "AUDJPY":  "AUDJPY=X",
    "AUDCHF":  "AUDCHF=X",
    "NZDJPY":  "NZDJPY=X",
    "NZDCHF":  "NZDCHF=X",
    "CADJPY":  "CADJPY=X",
    "CADCHF":  "CADCHF=X",
    "CHFJPY":  "CHFJPY=X",
    "XAUUSD":  "GC=F",
}

# How long before we can re-alert on the same pair+direction
ALERT_COOLDOWN_HOURS = 4

# ── In-memory state ──────────────────────────────────────────────────────────
pair_status   = {}   # { pair: { "direction": "LONG"/"SHORT"/"NONE", "ema_aligned": bool, ... } }
recent_alerts = []   # last 100 alerts for dashboard
alert_cooldown = {}  # { "GBPNZD_LONG": datetime } — suppress repeat alerts
_lock = threading.Lock()


def _ema(series: pd.Series, period: int = 50) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def _find_swing_points(df: pd.DataFrame, lookback: int = 60):
    """Return (swing_high, swing_low) from recent 4H bars."""
    recent = df["Close"].tail(lookback)
    return recent.max(), recent.min()


def _is_rising(series: pd.Series) -> bool:
    return series.iloc[-1] > series.iloc[-2]


def _check_pair(name: str, yf_symbol: str) -> dict:
    """
    Fetch data and evaluate the setup for one pair.
    Returns a status dict always (even when no setup).
    """
    status = {
        "pair": name,
        "price": None,
        "ema_d": None, "ema_4h": None, "ema_2h": None, "ema_1h": None,
        "ema_aligned": False,
        "direction": "NONE",
        "in_fib_zone": False,
        "setup": False,
        "grade": "—",
        "fib_50": None, "fib_618": None,
        "last_checked": datetime.now(timezone.utc).strftime("%H:%M UTC"),
        "error": None,
    }

    try:
        # ── Fetch data ───────────────────────────────────────────────────────
        df_1h = yf.download(yf_symbol, period="59d", interval="1h",
                            progress=False, auto_adjust=True)
        df_d  = yf.download(yf_symbol, period="200d", interval="1d",
                            progress=False, auto_adjust=True)

        if df_1h.empty or df_d.empty or len(df_1h) < 60 or len(df_d) < 55:
            status["error"] = "Insufficient data"
            return status

        # Flatten MultiIndex columns if present (yfinance sometimes returns them)
        if isinstance(df_1h.columns, pd.MultiIndex):
            df_1h.columns = df_1h.columns.get_level_values(0)
        if isinstance(df_d.columns, pd.MultiIndex):
            df_d.columns = df_d.columns.get_level_values(0)

        # ── Resample 1H → 4H and 2H ─────────────────────────────────────────
        ohlc = {"Open": "first", "High": "max", "Low": "min", "Close": "last"}
        df_4h = df_1h.resample("4h").agg(ohlc).dropna()
        df_2h = df_1h.resample("2h").agg(ohlc).dropna()

        if len(df_4h) < 55 or len(df_2h) < 55:
            status["error"] = "Not enough resampled bars"
            return status

        # ── EMA 50 on all 4 timeframes ───────────────────────────────────────
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

        status["price"]   = round(price, 5)
        status["ema_d"]   = round(ema_d_val, 5)
        status["ema_4h"]  = round(ema_4h_val, 5)
        status["ema_2h"]  = round(ema_2h_val, 5)
        status["ema_1h"]  = round(ema_1h_val, 5)

        # ── EMA alignment ────────────────────────────────────────────────────
        bull_aligned = (price > ema_d_val and
                        price > ema_4h_val and ema_4h_rising and
                        price > ema_2h_val and ema_2h_rising and
                        price > ema_1h_val)

        bear_aligned = (price < ema_d_val and
                        price < ema_4h_val and not ema_4h_rising and
                        price < ema_2h_val and not ema_2h_rising and
                        price < ema_1h_val)

        status["ema_aligned"] = bull_aligned or bear_aligned
        status["direction"] = "LONG" if bull_aligned else ("SHORT" if bear_aligned else "NONE")

        if not bull_aligned and not bear_aligned:
            return status

        # ── Fib zone detection ───────────────────────────────────────────────
        swing_high, swing_low = _find_swing_points(df_4h, lookback=60)
        fib_range = swing_high - swing_low
        tol = price * 0.003  # 0.3% tolerance

        if bull_aligned:
            fib_50  = swing_high - fib_range * 0.500
            fib_618 = swing_high - fib_range * 0.618
            in_zone = (fib_618 - tol) <= price <= (fib_50 + tol)
        else:
            fib_50  = swing_low + fib_range * 0.500
            fib_618 = swing_low + fib_range * 0.618
            in_zone = (fib_50 - tol) <= price <= (fib_618 + tol)

        status["fib_50"]    = round(fib_50, 5)
        status["fib_618"]   = round(fib_618, 5)
        status["in_fib_zone"] = in_zone
        status["setup"]     = in_zone
        status["grade"]     = "A+" if in_zone else "Watching"

    except Exception as exc:
        logger.exception(f"[{name}] Error: {exc}")
        status["error"] = str(exc)

    return status


def run_scan(send_telegram_fn):
    """
    Scan all 28 pairs in parallel threads.
    Fires Telegram alert for any pair that has a new setup.
    """
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

            if not status["setup"]:
                continue

            direction = status["direction"]
            cooldown_key = f"{name}_{direction}"
            last_alerted = alert_cooldown.get(cooldown_key)

            if last_alerted and (now - last_alerted) < timedelta(hours=ALERT_COOLDOWN_HOURS):
                logger.info(f"[{name}] {direction} — skipping (cooldown)")
                continue

            # Fire alert
            alert_cooldown[cooldown_key] = now
            alert = {**status, "received_at": now.strftime("%Y-%m-%d %H:%M UTC")}
            recent_alerts.insert(0, alert)
            if len(recent_alerts) > 100:
                recent_alerts.pop()

            send_telegram_fn(status)
            logger.info(f"[{name}] {direction} setup — alert sent")

    logger.info(f"=== Scan complete. {len(results)} pairs checked ===")
