"""
ZSCN Monitor — Full Market Brain
Analyzes all 28 pairs exactly like Harry does:
  Daily → 4H → 2H → 1H → 15M

Confluences checked (Harry places fib himself):
  1. 4-TF EMA 50 alignment (D + 4H curving + 2H curving + 1H)
  2. Trend structure (Daily + 4H both HH/HL or LH/LL)
  3. Price near a key S/R level
  4. 15M signal candle (pin bar or engulfing) at the S/R

Alerts when 3+ confluences stack. Harry then checks his fib zone.
"""

import logging
import threading
from datetime import datetime, timezone, timedelta

import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)

PAIRS = {
    "EURUSD": "EURUSD=X", "GBPUSD": "GBPUSD=X", "AUDUSD": "AUDUSD=X",
    "NZDUSD": "NZDUSD=X", "USDCAD": "USDCAD=X", "USDJPY": "USDJPY=X",
    "USDCHF": "USDCHF=X", "GBPJPY": "GBPJPY=X", "EURJPY": "EURJPY=X",
    "GBPAUD": "GBPAUD=X", "GBPNZD": "GBPNZD=X", "GBPCAD": "GBPCAD=X",
    "GBPCHF": "GBPCHF=X", "EURCAD": "EURCAD=X", "EURGBP": "EURGBP=X",
    "EURAUD": "EURAUD=X", "EURNZD": "EURNZD=X", "EURCHF": "EURCHF=X",
    "AUDCAD": "AUDCAD=X", "AUDNZD": "AUDNZD=X", "AUDJPY": "AUDJPY=X",
    "AUDCHF": "AUDCHF=X", "NZDJPY": "NZDJPY=X", "NZDCHF": "NZDCHF=X",
    "CADJPY": "CADJPY=X", "CADCHF": "CADCHF=X", "CHFJPY": "CHFJPY=X",
    "XAUUSD": "GC=F",
}

ALERT_COOLDOWN_HOURS = 4
SR_PROXIMITY_PCT     = 0.30   # within 0.30% = "at the zone"
SR_CLUSTER_PCT       = 0.20   # merge levels within 0.20% of each other

pair_status    = {}
recent_alerts  = []
alert_cooldown = {}
_lock = threading.Lock()


# ── EMA ──────────────────────────────────────────────────────────────────────
def _ema(series: pd.Series, period: int = 50) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()

def _is_rising(s: pd.Series) -> bool:
    return float(s.iloc[-1]) > float(s.iloc[-2])


# ── Trend structure ───────────────────────────────────────────────────────────
def _pivot_highs(df: pd.DataFrame, n: int = 8) -> list:
    levels = []
    for i in range(n, len(df) - n):
        if df["High"].iloc[i] == df["High"].iloc[i - n: i + n + 1].max():
            levels.append(float(df["High"].iloc[i]))
    return levels

def _pivot_lows(df: pd.DataFrame, n: int = 8) -> list:
    levels = []
    for i in range(n, len(df) - n):
        if df["Low"].iloc[i] == df["Low"].iloc[i - n: i + n + 1].min():
            levels.append(float(df["Low"].iloc[i]))
    return levels

def _trend_structure(df: pd.DataFrame, pivot_n: int = 8) -> str:
    """Return BULLISH / BEARISH / RANGING based on last 2 pivot highs and lows."""
    ph = _pivot_highs(df, pivot_n)
    pl = _pivot_lows(df, pivot_n)
    if len(ph) < 2 or len(pl) < 2:
        return "RANGING"
    hh = ph[-1] > ph[-2]
    hl = pl[-1] > pl[-2]
    lh = ph[-1] < ph[-2]
    ll = pl[-1] < pl[-2]
    if hh and hl:
        return "BULLISH"
    if lh and ll:
        return "BEARISH"
    return "RANGING"


# ── S/R levels ────────────────────────────────────────────────────────────────
def _cluster_levels(raw: list, cluster_pct: float) -> list:
    if not raw:
        return []
    sorted_lvls = sorted(raw)
    clusters = [[sorted_lvls[0]]]
    for lvl in sorted_lvls[1:]:
        if abs(lvl - clusters[-1][-1]) / clusters[-1][-1] * 100 <= cluster_pct:
            clusters[-1].append(lvl)
        else:
            clusters.append([lvl])
    return [sum(c) / len(c) for c in clusters]

def _find_sr_levels(df_d: pd.DataFrame, df_4h: pd.DataFrame) -> list:
    """Combine Daily + 4H pivot levels, cluster them, return significant ones."""
    raw = (_pivot_highs(df_d, 8) + _pivot_lows(df_d, 8) +
           _pivot_highs(df_4h, 10) + _pivot_lows(df_4h, 10))
    return _cluster_levels(raw, SR_CLUSTER_PCT)

def _nearest_sr(price: float, levels: list) -> tuple:
    """Return (nearest_level, distance_pct) or (None, None)."""
    if not levels:
        return None, None
    nearest = min(levels, key=lambda l: abs(l - price))
    dist_pct = abs(nearest - price) / price * 100
    return nearest, round(dist_pct, 3)


# ── 15M signal candle ─────────────────────────────────────────────────────────
def _signal_candle(df_15m: pd.DataFrame, direction: str) -> str:
    """Check last CLOSED 15M candle for pin bar or engulfing."""
    if len(df_15m) < 3:
        return "None"

    c    = df_15m.iloc[-2]   # last closed candle
    prev = df_15m.iloc[-3]

    o, h, l, cl = float(c["Open"]), float(c["High"]), float(c["Low"]), float(c["Close"])
    body        = abs(cl - o)
    total       = h - l
    if total < 1e-10:
        return "None"

    upper_wick = h - max(o, cl)
    lower_wick = min(o, cl) - l

    if direction == "LONG":
        # Bullish pin bar: lower wick >= 2× body, lower wick dominant
        if body > 0 and lower_wick >= 2 * body and lower_wick > upper_wick:
            return "Pin Bar (Bullish)"
        # Bullish engulfing
        if (cl > o and
                cl > float(prev["Open"]) and
                o  < float(prev["Close"])):
            return "Engulfing (Bullish)"
    elif direction == "SHORT":
        # Bearish pin bar: upper wick >= 2× body, upper wick dominant
        if body > 0 and upper_wick >= 2 * body and upper_wick > lower_wick:
            return "Pin Bar (Bearish)"
        # Bearish engulfing
        if (cl < o and
                cl < float(prev["Open"]) and
                o  > float(prev["Close"])):
            return "Engulfing (Bearish)"

    return "None"


# ── Main pair analysis ────────────────────────────────────────────────────────
def _check_pair(name: str, yf_symbol: str) -> dict:
    status = {
        "pair": name, "price": None,
        "direction": "NONE", "ema_aligned": False,
        "daily_trend": "RANGING", "h4_trend": "RANGING",
        "trends_agree": False,
        "nearest_sr": None, "sr_dist_pct": None, "at_sr": False,
        "signal_15m": "None", "has_signal": False,
        "confluence_score": 0, "confluence_detail": [],
        "alert": False,
        "last_checked": datetime.now(timezone.utc).strftime("%H:%M UTC"),
        "error": None,
    }

    try:
        # ── Fetch data ───────────────────────────────────────────────────────
        df_1h = yf.download(yf_symbol, period="59d",  interval="1h",
                            progress=False, auto_adjust=True)
        df_d  = yf.download(yf_symbol, period="365d", interval="1d",
                            progress=False, auto_adjust=True)

        if df_1h.empty or df_d.empty or len(df_1h) < 60 or len(df_d) < 60:
            status["error"] = "Insufficient data"
            return status

        for df in [df_1h, df_d]:
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)

        ohlc  = {"Open": "first", "High": "max", "Low": "min", "Close": "last"}
        df_4h = df_1h.resample("4h").agg(ohlc).dropna()
        df_2h = df_1h.resample("2h").agg(ohlc).dropna()
        df_15m = yf.download(yf_symbol, period="5d", interval="15m",
                             progress=False, auto_adjust=True)
        if isinstance(df_15m.columns, pd.MultiIndex):
            df_15m.columns = df_15m.columns.get_level_values(0)

        if len(df_4h) < 55 or len(df_2h) < 55:
            status["error"] = "Not enough resampled bars"
            return status

        # ── EMA 50 ───────────────────────────────────────────────────────────
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

        bull_ema = (price > ema_d_val and
                    price > ema_4h_val and ema_4h_rising and
                    price > ema_2h_val and ema_2h_rising and
                    price > ema_1h_val)

        bear_ema = (price < ema_d_val and
                    price < ema_4h_val and not ema_4h_rising and
                    price < ema_2h_val and not ema_2h_rising and
                    price < ema_1h_val)

        status["price"]       = round(price, 5)
        status["ema_aligned"] = bull_ema or bear_ema
        status["direction"]   = "LONG" if bull_ema else ("SHORT" if bear_ema else "NONE")

        if status["direction"] == "NONE":
            return status  # No EMA alignment = no point going further

        direction = status["direction"]

        # ── Trend structure ──────────────────────────────────────────────────
        d_trend  = _trend_structure(df_d,  pivot_n=8)
        h4_trend = _trend_structure(df_4h, pivot_n=10)
        status["daily_trend"] = d_trend
        status["h4_trend"]    = h4_trend
        trends_agree = (
            (direction == "LONG"  and d_trend == "BULLISH" and h4_trend == "BULLISH") or
            (direction == "SHORT" and d_trend == "BEARISH" and h4_trend == "BEARISH")
        )
        status["trends_agree"] = trends_agree

        # ── S/R levels ───────────────────────────────────────────────────────
        sr_levels = _find_sr_levels(df_d, df_4h)
        nearest_sr, sr_dist = _nearest_sr(price, sr_levels)
        status["nearest_sr"]  = round(nearest_sr, 5) if nearest_sr else None
        status["sr_dist_pct"] = sr_dist
        status["at_sr"]       = sr_dist is not None and sr_dist <= SR_PROXIMITY_PCT

        # ── 15M signal candle ────────────────────────────────────────────────
        if len(df_15m) >= 3:
            sig = _signal_candle(df_15m, direction)
            status["signal_15m"] = sig
            status["has_signal"] = sig != "None"

        # ── Confluence score ─────────────────────────────────────────────────
        score  = 0
        detail = []

        # 1. EMA (always true if we got here)
        score += 1
        side  = "below" if direction == "LONG" else "above"
        curve = "curving up" if direction == "LONG" else "curving down"
        detail.append(f"EMA 50: Daily {side}, 4H {side}+{curve}, 2H {side}+{curve}, 1H {side}")

        # 2. Trend structure
        if trends_agree:
            score += 1
            detail.append(f"Trend: Daily {d_trend} + 4H {h4_trend} - both agree")
        else:
            detail.append(f"Trend: Daily {d_trend} / 4H {h4_trend} - partial")

        # 3. S/R zone
        if status["at_sr"]:
            score += 1
            detail.append(f"S/R: {nearest_sr} ({sr_dist}% away) - AT ZONE")
        elif nearest_sr:
            detail.append(f"S/R: {nearest_sr} ({sr_dist}% away) - not at zone yet")

        # 4. 15M signal
        if status["has_signal"]:
            score += 1
            detail.append(f"15M signal: {status['signal_15m']} - CONFIRMED")
        else:
            detail.append(f"15M signal: none yet")

        status["confluence_score"]  = score
        status["confluence_detail"] = detail
        status["alert"] = score >= 4   # only alert on 4/4 confluences

    except Exception as exc:
        logger.exception(f"[{name}] Error: {exc}")
        status["error"] = str(exc)

    return status


# ── Scan all 28 pairs ────────────────────────────────────────────────────────
def run_scan(send_telegram_fn):
    logger.info("=== ZSCN brain scan started ===")
    results = {}
    threads = []

    def worker(name, yf_sym):
        results[name] = _check_pair(name, yf_sym)

    for name, yf_sym in PAIRS.items():
        t = threading.Thread(target=worker, args=(name, yf_sym))
        t.start()
        threads.append(t)

    for t in threads:
        t.join(timeout=45)

    now = datetime.now(timezone.utc)

    with _lock:
        for name, status in results.items():
            pair_status[name] = status

            if not status.get("alert"):
                continue

            direction    = status["direction"]
            cooldown_key = f"{name}_{direction}"
            last_alerted = alert_cooldown.get(cooldown_key)

            if last_alerted and (now - last_alerted) < timedelta(hours=ALERT_COOLDOWN_HOURS):
                logger.info(f"[{name}] {direction} - cooldown active, skipping")
                continue

            alert_cooldown[cooldown_key] = now
            alert = {**status, "received_at": now.strftime("%Y-%m-%d %H:%M UTC")}
            recent_alerts.insert(0, alert)
            if len(recent_alerts) > 100:
                recent_alerts.pop()

            send_telegram_fn(status)
            logger.info(f"[{name}] {direction} - {status['confluence_score']}/4 confluences - alert sent")

    logger.info(f"=== Scan complete. {len(results)} pairs checked ===")
