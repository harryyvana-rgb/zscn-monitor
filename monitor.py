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
  BONUS: Break and retest (broken S/R level now acting as opposite S/R)
  BONUS: 2H trend structure agrees with Daily + 4H
  BONUS: HIGH quality signal candle (wick >= 3x body, or clean engulfing)

Alert fires at 4/4.
Grade A+ = 4/4 + 2+ bonus conditions (HIGH CONVICTION).
Grade A  = 4/4 standard.
Grade WATCH = 3/4 early warning.
"""

import json
import logging
import os
import threading
from datetime import datetime, timezone, timedelta

import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)

ALERTS_LOG_PATH = os.path.join(os.path.dirname(__file__), "data", "alerts_log.json")
_MAX_STORED_ALERTS = 500


def _load_alerts_from_disk() -> list:
    try:
        if os.path.exists(ALERTS_LOG_PATH):
            with open(ALERTS_LOG_PATH, "r") as f:
                return json.load(f)
    except Exception as e:
        logger.warning(f"Could not load alerts from disk: {e}")
    return []


def _save_alerts_to_disk(alerts: list):
    try:
        os.makedirs(os.path.dirname(ALERTS_LOG_PATH), exist_ok=True)
        with open(ALERTS_LOG_PATH, "w") as f:
            json.dump(alerts[:_MAX_STORED_ALERTS], f, default=str, indent=2)
    except Exception as e:
        logger.warning(f"Could not save alerts to disk: {e}")

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

pair_status          = {}
recent_alerts        = _load_alerts_from_disk()
alert_cooldown       = {}
early_alert_cooldown = {}
_lock = threading.Lock()


# ── EMA ──────────────────────────────────────────────────────────────────────
def _ema(series: pd.Series, period: int = 50) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()

def _is_rising(s: pd.Series) -> bool:
    # Compare 3 bars back — single-bar comparison is too noisy on resampled data
    return float(s.iloc[-1]) > float(s.iloc[-4])


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

def _count_touches(level: float, raw_pivots: list, cluster_pct: float) -> int:
    """Count how many raw pivot prices are within cluster_pct% of a level."""
    return sum(1 for p in raw_pivots if abs(p - level) / level * 100 <= cluster_pct)


def _find_sr_levels_detailed(df_d: pd.DataFrame, df_4h: pd.DataFrame) -> dict:
    """
    Returns separate Daily and 4H S/R levels with strength (touch count).

    Daily:  pivot_n=15 — requires 15 bars each side (strong, significant levels).
            Using 730 days of data catches multi-year structural levels.
    4H:     pivot_n=12 — 2 days each side for meaningful 4H structure.
    PDH/PDL included in 4H levels.

    Returns:
        daily: list of {price, touches} sorted by price
        h4:    list of {price, touches} sorted by price
        all:   flat list of all unique clustered prices (for proximity checks)
    """
    pdh_pdl_raw = _pdh_pdl(df_d)

    daily_raw = _pivot_highs(df_d, 15) + _pivot_lows(df_d, 15)
    h4_raw    = _pivot_highs(df_4h, 12) + _pivot_lows(df_4h, 12)

    daily_clustered = _cluster_levels(daily_raw, SR_CLUSTER_PCT)
    h4_clustered    = _cluster_levels(h4_raw + pdh_pdl_raw, SR_CLUSTER_PCT)

    all_raw       = daily_raw + h4_raw + pdh_pdl_raw
    all_clustered = _cluster_levels(all_raw, SR_CLUSTER_PCT)

    daily_levels = [
        {"price": round(l, 5), "touches": _count_touches(l, daily_raw, SR_CLUSTER_PCT)}
        for l in daily_clustered
    ]
    h4_levels = [
        {"price": round(l, 5), "touches": _count_touches(l, h4_raw + pdh_pdl_raw, SR_CLUSTER_PCT)}
        for l in h4_clustered
    ]

    return {
        "daily": daily_levels,
        "h4":    h4_levels,
        "all":   [round(l, 5) for l in all_clustered],
    }


def _find_sr_levels(df_d: pd.DataFrame, df_4h: pd.DataFrame) -> list:
    """Flat list of all S/R levels — used for proximity/scoring checks."""
    return _find_sr_levels_detailed(df_d, df_4h)["all"]

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


def _calc_sl_tp(price: float, direction: str, sr_above: list, sr_below: list,
                df_4h=None, retest_level=None) -> tuple:
    """
    SL placed below the actual swing low of recent rejection candles (LONG)
    or above recent swing high (SHORT) — matches how Harry places SL manually.

    Priority order:
      1. Retest level (if break+retest detected) — SL just beyond that level
      2. Lowest wick of last 3 closed 4H bars (the rejection candles at the zone)
      3. Nearest S/R level below/above as fallback

    TP = 1:3 R:R from current price.
    """
    sl_buf = 0.0005  # 0.05% beyond the low/high

    if direction == "LONG":
        if retest_level is not None:
            sl = retest_level * (1 - sl_buf)
        elif df_4h is not None and len(df_4h) >= 4:
            recent_low = float(df_4h["Low"].iloc[-4:-1].min())
            sl = recent_low * (1 - sl_buf)
        elif sr_below:
            sl = sr_below[0] * (1 - SL_BUFFER_PCT / 100)
        else:
            return None, None
        risk = price - sl
        if risk <= 0:
            return None, None
        tp = price + risk * 3

    elif direction == "SHORT":
        if retest_level is not None:
            sl = retest_level * (1 + sl_buf)
        elif df_4h is not None and len(df_4h) >= 4:
            recent_high = float(df_4h["High"].iloc[-4:-1].max())
            sl = recent_high * (1 + sl_buf)
        elif sr_above:
            sl = sr_above[0] * (1 + SL_BUFFER_PCT / 100)
        else:
            return None, None
        risk = sl - price
        if risk <= 0:
            return None, None
        tp = price - risk * 3

    else:
        return None, None

    return round(sl, 5), round(tp, 5)


# ── 15M signal candle ─────────────────────────────────────────────────────────
def _signal_candle(df_15m: pd.DataFrame, direction: str) -> tuple:
    """
    Check last CLOSED 15M candle for pin bar or engulfing.
    Returns (signal_type, quality) where quality is 'HIGH', 'MEDIUM', or 'None'.
    HIGH: wick >= 3x body OR full body engulfing.
    MEDIUM: wick >= 2x body OR partial engulfing.
    """
    if len(df_15m) < 3:
        return "None", "None"

    c    = df_15m.iloc[-2]
    prev = df_15m.iloc[-3]

    o, h, l, cl = float(c["Open"]), float(c["High"]), float(c["Low"]), float(c["Close"])
    body        = abs(cl - o)
    total       = h - l
    if total < 1e-10:
        return "None", "None"

    upper_wick = h - max(o, cl)
    lower_wick = min(o, cl) - l

    # Minimum meaningful wick: 0.03% of price (~3 pips on a 1.0 pair).
    # Rejects micro-candles from thin/illiquid periods.
    min_wick = cl * 0.0003

    if direction == "LONG":
        # Pin bar: long lower wick rejecting from support
        if body > 0 and lower_wick >= 2 * body and lower_wick > upper_wick and lower_wick >= min_wick:
            quality = "HIGH" if lower_wick >= 3 * body else "MEDIUM"
            return "Pin Bar (Bullish)", quality
        # Engulfing: bullish body fully covers previous bearish body
        if cl > o and cl > float(prev["Open"]) and o < float(prev["Close"]) and body >= min_wick:
            quality = "HIGH" if cl >= float(prev["High"]) or o <= float(prev["Low"]) else "MEDIUM"
            return "Engulfing (Bullish)", quality

    elif direction == "SHORT":
        # Pin bar: long upper wick rejecting from resistance
        if body > 0 and upper_wick >= 2 * body and upper_wick > lower_wick and upper_wick >= min_wick:
            quality = "HIGH" if upper_wick >= 3 * body else "MEDIUM"
            return "Pin Bar (Bearish)", quality
        # Engulfing: bearish body fully covers previous bullish body
        if cl < o and cl < float(prev["Open"]) and o > float(prev["Close"]) and body >= min_wick:
            quality = "HIGH" if cl <= float(prev["Low"]) or o >= float(prev["High"]) else "MEDIUM"
            return "Engulfing (Bearish)", quality

    return "None", "None"


# ── Break and retest ──────────────────────────────────────────────────────────
def _find_break_retest(df_4h: pd.DataFrame, sr_levels: list,
                       price: float, direction: str) -> dict:
    """
    Detect break-and-retest of S/R levels on 4H.
    LONG:  old resistance broken (price closed above), now retesting from above = new support.
    SHORT: old support broken (price closed below), now retesting from below = new resistance.
    Valid retest window: 2–25 bars after the break.
    """
    result = {"is_retest": False, "retest_level": None,
              "retest_type": None, "bars_since_break": None}

    if len(df_4h) < 30:
        return result

    closes = df_4h["Close"].values[-30:]

    for level in sr_levels:
        dist_pct = abs(price - level) / price * 100
        if dist_pct > SR_PROXIMITY_PCT:
            continue

        if direction == "LONG" and price >= level:
            # Find most recent bar where price flipped from below to above the level
            break_idx = None
            for i in range(len(closes) - 2, 1, -1):
                if closes[i] > level and closes[i - 1] < level:
                    break_idx = i
                    break
            if break_idx is None:
                continue
            bars_since = len(closes) - 1 - break_idx
            if bars_since < 2 or bars_since > 25:
                continue
            # Confirm there were multiple bars below before the break
            pre = closes[:break_idx]
            if sum(1 for c in pre[-6:] if c < level) >= 2:
                result.update({
                    "is_retest": True,
                    "retest_level": round(level, 5),
                    "retest_type": "Broken Resistance → Now Support",
                    "bars_since_break": bars_since,
                })
                return result

        elif direction == "SHORT" and price <= level:
            break_idx = None
            for i in range(len(closes) - 2, 1, -1):
                if closes[i] < level and closes[i - 1] > level:
                    break_idx = i
                    break
            if break_idx is None:
                continue
            bars_since = len(closes) - 1 - break_idx
            if bars_since < 2 or bars_since > 25:
                continue
            pre = closes[:break_idx]
            if sum(1 for c in pre[-6:] if c > level) >= 2:
                result.update({
                    "is_retest": True,
                    "retest_level": round(level, 5),
                    "retest_type": "Broken Support → Now Resistance",
                    "bars_since_break": bars_since,
                })
                return result

    return result


# ── Main pair analysis ────────────────────────────────────────────────────────
def _check_pair(name: str, yf_symbol: str) -> dict:
    status = {
        "pair": name, "price": None,
        "direction": "NONE", "ema_aligned": False,
        "daily_trend": "RANGING", "h4_trend": "RANGING",
        "trends_agree": False,
        "nearest_sr": None, "sr_dist_pct": None, "at_sr": False,
        "sr_above": [], "sr_below": [],
        "sr_daily": [], "sr_4h": [],
        "pdh": None, "pdl": None,
        "trendline_val": None, "at_trendline": False, "trendline_dist_pct": None,
        "h2_trend": "RANGING", "all_trends_agree": False,
        "is_retest": False, "retest_level": None, "retest_type": None,
        "signal_15m": "None", "signal_quality": "None", "has_signal": False,
        "confluence_score": 0, "confluence_detail": [],
        "sl": None, "tp": None, "alert_grade": "—",
        "alert": False, "early_warning": False,
        "last_checked": datetime.now(timezone.utc).strftime("%H:%M UTC"),
        "error": None,
    }

    try:
        # ── Fetch data ───────────────────────────────────────────────────────
        df_1h = yf.download(yf_symbol, period="59d",  interval="1h",
                            progress=False, auto_adjust=True)
        df_d  = yf.download(yf_symbol, period="730d", interval="1d",
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
        h2_trend = _trend_structure(df_2h, pivot_n=6)
        status["daily_trend"] = d_trend
        status["h4_trend"]    = h4_trend
        status["h2_trend"]    = h2_trend
        trends_agree = (
            (direction == "LONG"  and d_trend == "BULLISH" and h4_trend == "BULLISH") or
            (direction == "SHORT" and d_trend == "BEARISH" and h4_trend == "BEARISH")
        )
        all_trends_agree = trends_agree and (
            (direction == "LONG"  and h2_trend == "BULLISH") or
            (direction == "SHORT" and h2_trend == "BEARISH")
        )
        status["trends_agree"]     = trends_agree
        status["all_trends_agree"] = all_trends_agree

        # ── S/R levels ───────────────────────────────────────────────────────
        sr_detail = _find_sr_levels_detailed(df_d, df_4h)
        sr_levels = sr_detail["all"]
        nearest_sr, sr_dist = _nearest_sr(price, sr_levels)
        above, below = _levels_above_below(price, sr_levels)

        status["nearest_sr"]  = round(nearest_sr, 5) if nearest_sr else None
        status["sr_dist_pct"] = sr_dist
        status["at_sr"]       = sr_dist is not None and sr_dist <= SR_PROXIMITY_PCT
        status["sr_above"]    = [round(l, 5) for l in above]
        status["sr_below"]    = [round(l, 5) for l in below]
        status["sr_daily"]    = sr_detail["daily"]   # [{price, touches}, ...]
        status["sr_4h"]       = sr_detail["h4"]      # [{price, touches}, ...]

        # ── Trend line (bonus) ───────────────────────────────────────────────
        tl_val = _trendline_value(df_4h, direction, pivot_n=10)
        if tl_val is not None:
            tl_dist = abs(tl_val - price) / price * 100
            status["trendline_val"]      = round(tl_val, 5)
            status["trendline_dist_pct"] = round(tl_dist, 3)
            status["at_trendline"]       = tl_dist <= TRENDLINE_PROXIMITY

        # ── Break and retest (bonus) ─────────────────────────────────────────
        br = _find_break_retest(df_4h, sr_levels, price, direction)
        status.update(br)

        # ── 15M signal candle ────────────────────────────────────────────────
        if len(df_15m) >= 3:
            sig, sig_quality = _signal_candle(df_15m, direction)
            status["signal_15m"]    = sig
            status["signal_quality"] = sig_quality
            status["has_signal"]    = sig != "None"

        # ── Confluence score ─────────────────────────────────────────────────
        score  = 0
        detail = []

        # 1. EMA (4 TFs aligned)
        score += 1
        side  = "below" if direction == "LONG" else "above"
        curve = "curving up" if direction == "LONG" else "curving down"
        detail.append(f"EMA 50: Daily {side}, 4H {side}+{curve}, 2H {side}+{curve}, 1H {side}")

        # 2. Trend structure (Daily + 4H)
        if trends_agree:
            score += 1
            detail.append(f"Trend: Daily {d_trend} + 4H {h4_trend} — both agree")
        else:
            detail.append(f"Trend: Daily {d_trend} / 4H {h4_trend} — partial")

        # 3. S/R zone
        if status["at_sr"]:
            score += 1
            detail.append(f"S/R: {nearest_sr:.5f} ({sr_dist}% away) — AT ZONE")
        elif nearest_sr:
            detail.append(f"S/R: {nearest_sr:.5f} ({sr_dist}% away) — not at zone yet")

        # 4. 15M signal
        if status["has_signal"]:
            score += 1
            qual_tag = f" [{status['signal_quality']} quality]" if status["signal_quality"] != "None" else ""
            detail.append(f"15M signal: {status['signal_15m']}{qual_tag} — CONFIRMED")
        else:
            detail.append("15M signal: none yet")

        status["confluence_score"]  = score
        status["confluence_detail"] = detail
        status["alert"]        = score >= 4
        status["early_warning"] = score == 3

        # ── Bonus confluences (don't raise score, improve grade) ────────────
        if all_trends_agree:
            detail.append(f"BONUS — 2H trend: {h2_trend} — all 3 TFs agree")
        elif trends_agree:
            detail.append(f"2H trend: {h2_trend} — Daily+4H agree, 2H diverges")

        if status["at_trendline"]:
            detail.append(
                f"BONUS — Trend line: {status['trendline_val']} "
                f"({status['trendline_dist_pct']}% away) — DIAGONAL S/R"
            )

        if status["is_retest"]:
            detail.append(
                f"BONUS — Break & Retest: {status['retest_level']} — "
                f"{status['retest_type']} ({status['bars_since_break']} bars ago)"
            )

        # ── Alert grade ───────────────────────────────────────────────────────
        bonus_count = sum([
            all_trends_agree,
            status.get("at_trendline", False),
            status.get("is_retest", False),
            status.get("signal_quality") == "HIGH",
        ])
        if score >= 4:
            status["alert_grade"] = "A+" if bonus_count >= 2 else "A"
        elif score == 3:
            status["alert_grade"] = "WATCH"
        else:
            status["alert_grade"] = "—"

        # ── SL/TP — based on actual swing low/high of rejection candles ──────
        retest_lvl = status["retest_level"] if status["is_retest"] else None
        sl, tp = _calc_sl_tp(price, direction, above, below, df_4h, retest_lvl)
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

            # Hard requirements: EMA aligned AND Daily+4H trends agree.
            # If either fails there is NO setup — Harry's strategy requires both.
            if not status.get("ema_aligned") or direction == "NONE":
                continue
            if not status.get("trends_agree"):
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
                early_alert_cooldown.pop(early_key, None)
                alert = {**status, "received_at": now.strftime("%Y-%m-%d %H:%M UTC")}
                recent_alerts.insert(0, alert)
                if len(recent_alerts) > 100:
                    recent_alerts.pop()
                _save_alerts_to_disk(recent_alerts)
                send_telegram_fn(status)
                logger.info(f"[{name}] {direction} - 4/4 - FULL ALERT sent")

            # ── Early warning (3/4) ──────────────────────────────────────────
            # Only fire when price is within 1.0% of an S/R zone — otherwise
            # there is no zone to trade from and it's not an actionable setup.
            elif score == 3 and send_early_warning_fn is not None:
                sr_dist = status.get("sr_dist_pct")
                if sr_dist is None or sr_dist > 1.0:
                    logger.info(f"[{name}] {direction} - 3/4 but price {sr_dist}% from S/R, skipping")
                    continue
                last_early = early_alert_cooldown.get(early_key)
                if last_early and (now - last_early) < timedelta(hours=ALERT_COOLDOWN_HOURS):
                    logger.info(f"[{name}] {direction} - early cooldown active, skipping")
                    continue
                early_alert_cooldown[early_key] = now
                early = {**status, "received_at": now.strftime("%Y-%m-%d %H:%M UTC")}
                recent_alerts.insert(0, early)
                if len(recent_alerts) > 100:
                    recent_alerts.pop()
                _save_alerts_to_disk(recent_alerts)
                send_early_warning_fn(status)
                logger.info(f"[{name}] {direction} - 3/4 (sr_dist {sr_dist}%) - early warning sent")

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


# ── Friday end-of-week preview ────────────────────────────────────────────────
def run_friday_preview(send_friday_fn):
    """
    Friday scan. Ranks all 28 pairs by proximity to S/R zone so Harry knows
    exactly what to watch the following week.

    Categories (EMA + trend must agree for all of them):
      hot      — price within 1.0% of a key S/R zone — could trigger next week
      building — 1.0–3.0% away — structure building, approaching the zone
      watching — >3% from zone — trend aligned but too far, keep on radar
      early    — EMA aligned only, trend not yet confirmed
    """
    logger.info("=== Friday preview scan started ===")
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

    def sr_sort_key(s):
        d = s.get("sr_dist_pct")
        return d if d is not None else 999.0

    aligned = sorted(
        [s for s in results.values() if s.get("ema_aligned") and s.get("trends_agree")],
        key=sr_sort_key,
    )

    hot      = [s for s in aligned if (s.get("sr_dist_pct") or 999) <= 1.0]
    building = [s for s in aligned if 1.0 < (s.get("sr_dist_pct") or 999) <= 3.0]
    watching = [s for s in aligned if (s.get("sr_dist_pct") or 999) > 3.0]
    early    = [s for s in results.values()
                if s.get("ema_aligned") and not s.get("trends_agree")]

    send_friday_fn(hot, building, watching, early)
    logger.info(
        f"=== Friday preview complete. "
        f"Hot={len(hot)} Building={len(building)} "
        f"Watching={len(watching)} Early={len(early)} ==="
    )
