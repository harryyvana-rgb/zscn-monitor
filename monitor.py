"""
ZSCN Monitor — Full Market Brain
Analyzes all 28 pairs exactly like Harry does:
  Daily → 4H → 2H → 1H → 15M

Confluences checked (Harry places fib himself):
  1. 4-TF EMA 50 alignment (D + 4H curving + 2H curving + 1H)
  2. Trend structure (Daily + 4H both HH/HL or LH/LL)
  3. Price near a key S/R level (includes PDH/PDL/PWH/PWL)
  4. 15M signal candle (pin bar or engulfing) at the S/R
  BONUS: Trend line confluence (diagonal S/R from pivot highs/lows)

Alert fires at 4/4. Weekly bias scan runs every Sunday.
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

ALERT_COOLDOWN_HOURS  = 4
SR_PROXIMITY_PCT      = 0.30   # within 0.30% = "at the zone"
SR_CLUSTER_PCT        = 0.20   # merge levels within 0.20% of each other
TRENDLINE_PROXIMITY   = 0.25   # within 0.25% of trend line = bonus confluence
SL_BUFFER_PCT         = 0.10   # SL placed 0.10% beyond the S/R level

pair_status         = {}
recent_alerts       = []
alert_cooldown      = {}
early_alert_cooldown = {}
_lock = threading.Lock()


# ── EMA ──────────────────────────────────────────────────────────────────────
def _ema(series: pd.Series, period: int = 50) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()

def _is_rising(s: pd.Series) -> bool:
    return float(s.iloc[-1]) > float(s.iloc[-2])


# ── Trend structure ───────────────────────────────────────────────────────────
def _pivot_highs_indexed(df: pd.DataFrame, n: int = 8) -> list:
    """Return list of (bar_index, price) for pivot highs."""
    result = []
    for i in range(n, len(df) - n):
        if df["High"].iloc[i] == df["High"].iloc[i - n: i + n + 1].max():
            result.append((i, float(df["High"].iloc[i])))
    return result

def _pivot_lows_indexed(df: pd.DataFrame, n: int = 8) -> list:
    """Return list of (bar_index, price) for pivot lows."""
    result = []
    for i in range(n, len(df) - n):
        if df["Low"].iloc[i] == df["Low"].iloc[i - n: i + n + 1].min():
            result.append((i, float(df["Low"].iloc[i])))
    return result

def _pivot_highs(df: pd.DataFrame, n: int = 8) -> list:
    return [p for _, p in _pivot_highs_indexed(df, n)]

def _pivot_lows(df: pd.DataFrame, n: int = 8) -> list:
    return [p for _, p in _pivot_lows_indexed(df, n)]

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


# ── Trend line ────────────────────────────────────────────────────────────────
def _trendline_value(df: pd.DataFrame, direction: str, pivot_n: int = 10) -> float | None:
    """
    Extrapolate trend line to current bar.
    LONG  → ascending line through last 2 pivot lows
    SHORT → descending line through last 2 pivot highs
    """
    current_bar = len(df) - 1
    if direction == "SHORT":
        pts = _pivot_highs_indexed(df, pivot_n)
        if len(pts) < 2:
            return None
        (i1, p1), (i2, p2) = pts[-2], pts[-1]
    else:
        pts = _pivot_lows_indexed(df, pivot_n)
        if len(pts) < 2:
            return None
        (i1, p1), (i2, p2) = pts[-2], pts[-1]

    if i2 == i1:
        return None
    slope = (p2 - p1) / (i2 - i1)
    return p2 + slope * (current_bar - i2)


# ── S/R levels ────────────────────────────────────────────────────────────────
def _pdh_pdl(df_d: pd.DataFrame) -> list:
    """Previous Day High/Low and Previous Week High/Low."""
    levels = []
    if len(df_d) >= 2:
        levels.append(float(df_d["High"].iloc[-2]))  # PDH
        levels.append(float(df_d["Low"].iloc[-2]))   # PDL
    if len(df_d) >= 6:
        levels.append(float(df_d["High"].iloc[-6:-1].max()))  # PWH
        levels.append(float(df_d["Low"].iloc[-6:-1].min()))   # PWL
    return levels

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
    """Daily + 4H pivots + PDH/PDL/PWH/PWL, clustered."""
    raw = (
        _pivot_highs(df_d, 8) + _pivot_lows(df_d, 8) +
        _pivot_highs(df_4h, 10) + _pivot_lows(df_4h, 10) +
        _pdh_pdl(df_d)
    )
    return _cluster_levels(raw, SR_CLUSTER_PCT)

def _nearest_sr(price: float, levels: list) -> tuple:
    """Return (nearest_level, distance_pct) or (None, None)."""
    if not levels:
        return None, None
    nearest = min(levels, key=lambda l: abs(l - price))
    dist_pct = abs(nearest - price) / price * 100
    return nearest, round(dist_pct, 3)

def _levels_above_below(price: float, levels: list, n: int = 3) -> tuple:
    """Return (n closest above, n closest below) sorted nearest first."""
    above = sorted([l for l in levels if l > price], key=lambda l: l - price)
    below = sorted([l for l in levels if l < price], key=lambda l: price - l)
    return above[:n], below[:n]


def _calc_sl_tp(price: float, direction: str, sr_above: list, sr_below: list) -> tuple:
    """
    SL: just beyond the nearest S/R level (SL_BUFFER_PCT beyond it).
    TP: 1:3 R:R from entry price.
    Returns (sl, tp) or (None, None).
    """
    if direction == "LONG":
        if not sr_below:
            return None, None
        sl_level = sr_below[0]
        sl = sl_level * (1 - SL_BUFFER_PCT / 100)
        tp = price + (price - sl) * 3
    elif direction == "SHORT":
        if not sr_above:
            return None, None
        sl_level = sr_above[0]
        sl = sl_level * (1 + SL_BUFFER_PCT / 100)
        tp = price - (sl - price) * 3
    else:
        return None, None
    return round(sl, 5), round(tp, 5)


# ── 15M signal candle ─────────────────────────────────────────────────────────
def _signal_candle(df_15m: pd.DataFrame, direction: str) -> str:
    """Check last CLOSED 15M candle for pin bar or engulfing."""
    if len(df_15m) < 3:
        return "None"

    c    = df_15m.iloc[-2]
    prev = df_15m.iloc[-3]

    o, h, l, cl = float(c["Open"]), float(c["High"]), float(c["Low"]), float(c["Close"])
    body        = abs(cl - o)
    total       = h - l
    if total < 1e-10:
        return "None"

    upper_wick = h - max(o, cl)
    lower_wick = min(o, cl) - l

    if direction == "LONG":
        if body > 0 and lower_wick >= 2 * body and lower_wick > upper_wick:
            return "Pin Bar (Bullish)"
        if (cl > o and cl > float(prev["Open"]) and o < float(prev["Close"])):
            return "Engulfing (Bullish)"
    elif direction == "SHORT":
        if body > 0 and upper_wick >= 2 * body and upper_wick > lower_wick:
            return "Pin Bar (Bearish)"
        if (cl < o and cl < float(prev["Open"]) and o > float(prev["Close"])):
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
        "sr_above": [], "sr_below": [],
        "pdh": None, "pdl": None,
        "trendline_val": None, "at_trendline": False, "trendline_dist_pct": None,
        "signal_15m": "None", "has_signal": False,
        "confluence_score": 0, "confluence_detail": [],
        "sl": None, "tp": None,
        "alert": False, "early_warning": False,
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

        ohlc   = {"Open": "first", "High": "max", "Low": "min", "Close": "last"}
        df_4h  = df_1h.resample("4h").agg(ohlc).dropna()
        df_2h  = df_1h.resample("2h").agg(ohlc).dropna()
        df_15m = yf.download(yf_symbol, period="5d", interval="15m",
                             progress=False, auto_adjust=True)
        if isinstance(df_15m.columns, pd.MultiIndex):
            df_15m.columns = df_15m.columns.get_level_values(0)

        if len(df_4h) < 55 or len(df_2h) < 55:
            status["error"] = "Not enough resampled bars"
            return status

        # ── PDH / PDL ────────────────────────────────────────────────────────
        if len(df_d) >= 2:
            status["pdh"] = round(float(df_d["High"].iloc[-2]), 5)
            status["pdl"] = round(float(df_d["Low"].iloc[-2]),  5)

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
            return status

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
        above, below = _levels_above_below(price, sr_levels)

        status["nearest_sr"]  = round(nearest_sr, 5) if nearest_sr else None
        status["sr_dist_pct"] = sr_dist
        status["at_sr"]       = sr_dist is not None and sr_dist <= SR_PROXIMITY_PCT
        status["sr_above"]    = [round(l, 5) for l in above]
        status["sr_below"]    = [round(l, 5) for l in below]

        # ── Trend line (bonus) ───────────────────────────────────────────────
        tl_val = _trendline_value(df_4h, direction, pivot_n=10)
        if tl_val is not None:
            tl_dist = abs(tl_val - price) / price * 100
            status["trendline_val"]      = round(tl_val, 5)
            status["trendline_dist_pct"] = round(tl_dist, 3)
            status["at_trendline"]       = tl_dist <= TRENDLINE_PROXIMITY

        # ── 15M signal candle ────────────────────────────────────────────────
        if len(df_15m) >= 3:
            sig = _signal_candle(df_15m, direction)
            status["signal_15m"] = sig
            status["has_signal"] = sig != "None"

        # ── Confluence score ─────────────────────────────────────────────────
        score  = 0
        detail = []

        # 1. EMA
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
            detail.append(f"S/R: {nearest_sr:.5f} ({sr_dist}% away) - AT ZONE")
        elif nearest_sr:
            detail.append(f"S/R: {nearest_sr:.5f} ({sr_dist}% away) - not at zone yet")

        # 4. 15M signal
        if status["has_signal"]:
            score += 1
            detail.append(f"15M signal: {status['signal_15m']} - CONFIRMED")
        else:
            detail.append(f"15M signal: none yet")

        # BONUS: Trend line
        if status["at_trendline"]:
            detail.append(
                f"BONUS - Trend line: {status['trendline_val']} "
                f"({status['trendline_dist_pct']}% away) - EXTRA CONFLUENCE"
            )

        status["confluence_score"]  = score
        status["confluence_detail"] = detail
        status["alert"] = score >= 4
        status["early_warning"] = score == 3

        # SL/TP based on nearest S/R levels (1:3 R:R)
        sl, tp = _calc_sl_tp(price, direction, above, below)
        status["sl"] = sl
        status["tp"] = tp

    except Exception as exc:
        logger.exception(f"[{name}] Error: {exc}")
        status["error"] = str(exc)

    return status


# ── Scan all 28 pairs ─────────────────────────────────────────────────────────
def run_scan(send_telegram_fn, send_early_warning_fn=None):
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
            direction    = status.get("direction", "NONE")
            score        = status.get("confluence_score", 0)

            if not status.get("ema_aligned") or direction == "NONE":
                continue

            cooldown_key = f"{name}_{direction}"
            early_key    = f"{name}_{direction}_early"

            # ── Full alert (4/4) ─────────────────────────────────────────────
            if status.get("alert"):
                last_alerted = alert_cooldown.get(cooldown_key)
                if last_alerted and (now - last_alerted) < timedelta(hours=ALERT_COOLDOWN_HOURS):
                    logger.info(f"[{name}] {direction} - full cooldown active, skipping")
                    continue
                alert_cooldown[cooldown_key] = now
                # reset early cooldown so it won't spam after a full alert fires
                early_alert_cooldown.pop(early_key, None)
                alert = {**status, "received_at": now.strftime("%Y-%m-%d %H:%M UTC")}
                recent_alerts.insert(0, alert)
                if len(recent_alerts) > 100:
                    recent_alerts.pop()
                send_telegram_fn(status)
                logger.info(f"[{name}] {direction} - 4/4 - FULL ALERT sent")

            # ── Early warning (3/4) ──────────────────────────────────────────
            elif score == 3 and send_early_warning_fn is not None:
                last_early = early_alert_cooldown.get(early_key)
                if last_early and (now - last_early) < timedelta(hours=ALERT_COOLDOWN_HOURS):
                    logger.info(f"[{name}] {direction} - early cooldown active, skipping")
                    continue
                early_alert_cooldown[early_key] = now
                early = {**status, "received_at": now.strftime("%Y-%m-%d %H:%M UTC")}
                recent_alerts.insert(0, early)
                if len(recent_alerts) > 100:
                    recent_alerts.pop()
                send_early_warning_fn(status)
                logger.info(f"[{name}] {direction} - 3/4 - early warning sent")

    logger.info(f"=== Scan complete. {len(results)} pairs checked ===")


# ── Weekly bias scan (Sundays) ────────────────────────────────────────────────
def run_weekly_bias(send_weekly_fn):
    """
    Sunday morning scan. Categorises all 28 pairs into:
      - Ready (4/4) — full setup, check fib and enter
      - Watch  (2-3/4) — EMA + trend agree, approaching S/R
      - Early  (1/4) — EMA aligned only, structure not confirmed
    """
    logger.info("=== Weekly bias scan started ===")
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

    with _lock:
        for name, status in results.items():
            pair_status[name] = status

    ready = [s for s in results.values() if s.get("confluence_score", 0) >= 4]
    watch = [s for s in results.values()
             if s.get("ema_aligned") and s.get("trends_agree")
             and s.get("confluence_score", 0) < 4]
    early = [s for s in results.values()
             if s.get("ema_aligned") and not s.get("trends_agree")]

    send_weekly_fn(ready, watch, early)
    logger.info(f"=== Weekly bias complete. Ready={len(ready)} Watch={len(watch)} Early={len(early)} ===")
