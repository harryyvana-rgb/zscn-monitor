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

import gc
import json
import logging
import os
import random
import threading
import time
from datetime import datetime, timezone, timedelta

import pandas as pd
import yfinance as yf
import numpy as np

logger = logging.getLogger(__name__)


def _yf_download(symbol: str, period: str, interval: str,
                 retries: int = 4) -> pd.DataFrame:
    """
    yfinance wrapper with exponential backoff on rate-limit errors.
    Also adds a small random jitter before the first request so concurrent
    threads don't all hit Yahoo Finance at the exact same millisecond.
    """
    time.sleep(random.uniform(0.3, 1.5))   # jitter: spread out initial requests
    for attempt in range(retries):
        try:
            df = yf.download(symbol, period=period, interval=interval,
                             progress=False, auto_adjust=True)
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            return df
        except Exception as e:
            msg = str(e).lower()
            if "rate" in msg or "too many" in msg or "429" in msg:
                wait = (2 ** attempt) * 5 + random.uniform(0, 3)
                logger.warning(
                    f"[{symbol}] Rate limited (attempt {attempt+1}/{retries}) "
                    f"— retrying in {wait:.1f}s"
                )
                time.sleep(wait)
            else:
                raise
    logger.warning(f"[{symbol}] All {retries} download attempts failed — returning empty")
    return pd.DataFrame()


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


# ── Outcome tracking (self-learning foundation) ───────────────────────────────
OUTCOMES_LOG_PATH = os.path.join(os.path.dirname(__file__), "data", "trade_outcomes.json")

def _load_outcomes() -> list:
    try:
        if os.path.exists(OUTCOMES_LOG_PATH):
            with open(OUTCOMES_LOG_PATH, "r") as f:
                return json.load(f)
    except Exception as e:
        logger.warning(f"Could not load outcomes: {e}")
    return []

def _save_outcomes(outcomes: list):
    try:
        os.makedirs(os.path.dirname(OUTCOMES_LOG_PATH), exist_ok=True)
        with open(OUTCOMES_LOG_PATH, "w") as f:
            json.dump(outcomes[:1000], f, default=str, indent=2)
    except Exception as e:
        logger.warning(f"Could not save outcomes: {e}")

trade_outcomes = _load_outcomes()

def get_win_rate() -> dict:
    wins   = sum(1 for o in trade_outcomes if o.get("result") == "WIN")
    losses = sum(1 for o in trade_outcomes if o.get("result") == "LOSS")
    total  = wins + losses
    return {"wins": wins, "losses": losses, "total": total,
            "win_rate": round(wins / total * 100, 1) if total > 0 else None}

def update_trade_outcomes(pair_results: dict) -> list:
    """
    Check all pending alerts for TP/SL hits. Updates records and returns
    a list of newly resolved outcomes so the caller can notify Harry.
    """
    global trade_outcomes
    newly_resolved = []
    updated = False
    now = datetime.now(timezone.utc)

    for outcome in trade_outcomes:
        if outcome.get("result") not in (None, "pending"):
            continue
        pair, direction, sl, tp = (outcome.get(k) for k in ("pair", "direction", "sl", "tp"))
        if not all([pair, direction, sl, tp]):
            continue
        try:
            fired = datetime.fromisoformat(outcome.get("fired_at", "").replace(" UTC", "+00:00"))
            if (now - fired).total_seconds() > 7 * 24 * 3600:
                outcome["result"] = "expired"
                updated = True
                continue
        except Exception:
            pass
        status = pair_results.get(pair)
        if not status or not status.get("price"):
            continue
        price = status["price"]
        resolved = False
        if direction == "LONG":
            if price >= tp:
                outcome["result"] = "WIN";  resolved = True
            elif price <= sl:
                outcome["result"] = "LOSS"; resolved = True
        elif direction == "SHORT":
            if price <= tp:
                outcome["result"] = "WIN";  resolved = True
            elif price >= sl:
                outcome["result"] = "LOSS"; resolved = True
        if resolved:
            outcome["result_at"]   = now.strftime("%Y-%m-%d %H:%M UTC")
            outcome["exit_price"]  = price
            newly_resolved.append(outcome.copy())
            updated = True

    if updated:
        _save_outcomes(trade_outcomes)
    return newly_resolved


def get_weekly_trade_report() -> dict:
    """Returns all trades that closed in the last 7 days."""
    now    = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=7)
    weekly = []
    for o in trade_outcomes:
        if o.get("result") not in ("WIN", "LOSS"):
            continue
        try:
            result_dt = datetime.strptime(
                o.get("result_at", ""), "%Y-%m-%d %H:%M UTC"
            ).replace(tzinfo=timezone.utc)
            if result_dt >= cutoff:
                weekly.append(o)
        except Exception:
            pass
    wins   = [o for o in weekly if o["result"] == "WIN"]
    losses = [o for o in weekly if o["result"] == "LOSS"]
    total  = len(wins) + len(losses)
    return {
        "wins":     wins,
        "losses":   losses,
        "total":    total,
        "win_rate": round(len(wins) / total * 100, 1) if total > 0 else None,
    }


# ── Self-calibration + backtest ──────────────────────────────────────────────
CALIBRATION_PATH  = os.path.join(os.path.dirname(__file__), "data", "calibration.json")
BACKTEST_LOG_PATH = os.path.join(os.path.dirname(__file__), "data", "backtest_log.json")
BACKTEST_PAIRS    = ["GBPUSD", "EURUSD", "GBPJPY", "GBPNZD", "GBPCAD",
                     "AUDUSD", "USDCAD", "GBPAUD", "EURGBP", "EURCAD"]

LOW_CONFIDENCE_PAIRS: set = set()

def _load_calibration():
    global LOW_CONFIDENCE_PAIRS
    try:
        if os.path.exists(CALIBRATION_PATH):
            with open(CALIBRATION_PATH) as f:
                data = json.load(f)
            LOW_CONFIDENCE_PAIRS = set(data.get("low_confidence_pairs", []))
            logger.info(f"Calibration loaded — low-confidence pairs: {LOW_CONFIDENCE_PAIRS or 'none'}")
    except Exception as e:
        logger.warning(f"Could not load calibration: {e}")

def _save_calibration(data: dict):
    try:
        os.makedirs(os.path.dirname(CALIBRATION_PATH), exist_ok=True)
        with open(CALIBRATION_PATH, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        logger.warning(f"Could not save calibration: {e}")

_load_calibration()


def _backtest_pair(name: str, yf_sym: str, lookback_days: int) -> dict | None:
    """
    Walk-forward backtest on a single pair over lookback_days.
    Uses the same EMA + trend + ADX + S/R + 1H signal logic as the live scanner.
    Steps every 4 4H bars (~1 day) to keep runtime reasonable.
    """
    try:
        fetch  = lookback_days + 60
        df_1h  = _yf_download(yf_sym, period=f"{fetch}d",      interval="1h")
        df_d   = _yf_download(yf_sym, period=f"{fetch+100}d", interval="1d")
        if df_1h.empty or df_d.empty or len(df_1h) < 200 or len(df_d) < 60:
            return None
        for df in [df_1h, df_d]:
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
        ohlc  = {"Open": "first", "High": "max", "Low": "min", "Close": "last"}
        df_4h = df_1h.resample("4h").agg(ohlc).dropna()
        df_2h = df_1h.resample("2h").agg(ohlc).dropna()

        # Start the walk-forward window 100 bars in (need history for indicators)
        start = max(100, len(df_4h) - lookback_days * 6)
        signals = []
        last_sig = -20

        for i in range(start, len(df_4h) - 40, 4):
            if i - last_sig < 8:
                continue
            h4   = df_4h.iloc[:i]
            d    = df_d[df_d.index <= df_4h.index[i - 1]]
            h1_e = df_1h.index.searchsorted(df_4h.index[i - 1], side="right")
            h1   = df_1h.iloc[:h1_e]
            h2   = df_2h[df_2h.index <= df_4h.index[i - 1]]
            if len(d) < 60 or len(h4) < 60 or len(h1) < 60 or len(h2) < 55:
                continue

            price = float(h1["Close"].iloc[-1])
            ema_d_v  = float(_ema(d["Close"]).iloc[-1])
            ema_4h_s = _ema(h4["Close"])
            ema_2h_s = _ema(h2["Close"])
            ema_1h_s = _ema(h1["Close"])
            r4 = _is_rising(ema_4h_s)
            r2 = _is_rising(ema_2h_s)

            bull = (price > ema_d_v and price > float(ema_4h_s.iloc[-1]) and r4 and
                    price > float(ema_2h_s.iloc[-1]) and r2 and price > float(ema_1h_s.iloc[-1]))
            bear = (price < ema_d_v and price < float(ema_4h_s.iloc[-1]) and not r4 and
                    price < float(ema_2h_s.iloc[-1]) and not r2 and price < float(ema_1h_s.iloc[-1]))
            if not (bull or bear):
                continue

            direction = "LONG" if bull else "SHORT"
            d_tr  = _trend_structure(d)
            h4_tr = _trend_structure(h4)
            if direction == "LONG"  and not (d_tr == "BULLISH" and h4_tr == "BULLISH"): continue
            if direction == "SHORT" and not (d_tr == "BEARISH" and h4_tr == "BEARISH"): continue

            adx = _adx(h4)
            if adx is None or adx < 20:
                continue

            sr_levels = _find_sr_levels(h4)
            nearest, sr_dist = _nearest_sr(price, sr_levels)
            if sr_dist is None or sr_dist > SR_PROXIMITY_PCT:
                continue

            sig, sig_q = _signal_candle(h1, direction)
            if sig == "None":
                continue

            above, below = _levels_above_below(price, sr_levels)
            sl, tp = _calc_sl_tp(price, direction, above, below, h4)
            if not sl or not tp:
                continue

            outcome = "EXPIRED"
            future  = df_4h.iloc[i:i + 60]
            for _, row in future.iterrows():
                hi, lo = float(row["High"]), float(row["Low"])
                if direction == "LONG":
                    if lo <= sl: outcome = "LOSS"; break
                    if hi >= tp: outcome = "WIN";  break
                else:
                    if hi >= sl: outcome = "LOSS"; break
                    if lo <= tp: outcome = "WIN";  break

            signals.append({
                "date":       str(df_4h.index[i])[:10],
                "direction":  direction,
                "price":      round(price, 5),
                "sl":         sl, "tp": tp,
                "sig_quality": sig_q,
                "adx":        round(adx, 1),
                "outcome":    outcome,
            })
            last_sig = i

        wins   = [s for s in signals if s["outcome"] == "WIN"]
        losses = [s for s in signals if s["outcome"] == "LOSS"]
        total  = len(wins) + len(losses)
        return {
            "pair":     name,
            "signals":  len(signals),
            "wins":     len(wins),
            "losses":   len(losses),
            "win_rate": round(len(wins) / total * 100, 1) if total > 0 else None,
            "recent":   signals[-5:],
        }
    except Exception as e:
        logger.warning(f"[{name}] Backtest error: {e}")
        return None
    finally:
        gc.collect()


def _calibrate_from_backtest(results: list):
    """Flag pairs with < 40% win rate (min 5 signals) as low-confidence."""
    global LOW_CONFIDENCE_PAIRS
    poor = set()
    for r in results:
        total = r["wins"] + r["losses"]
        if total >= 5 and (r["win_rate"] or 100) < 40.0:
            poor.add(r["pair"])
            logger.info(f"[{r['pair']}] Low confidence ({r['win_rate']}%) — will skip alerts")
    improved = LOW_CONFIDENCE_PAIRS - poor
    new_poor  = poor - LOW_CONFIDENCE_PAIRS
    if improved:
        logger.info(f"Pairs RESTORED (improved): {improved}")
    if new_poor:
        logger.info(f"Pairs FLAGGED low-confidence: {new_poor}")
    LOW_CONFIDENCE_PAIRS = poor
    _save_calibration({
        "low_confidence_pairs": list(poor),
        "calibrated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    })


def run_backtest(lookback_days: int = 180) -> dict:
    """
    Walk-forward backtest on BACKTEST_PAIRS over lookback_days.
    Runs in a background thread — results sent to Telegram by app.py.
    After completion runs _calibrate_from_backtest to update thresholds.
    """
    logger.info(f"=== Backtest started ({lookback_days}d lookback, {len(BACKTEST_PAIRS)} pairs) ===")
    all_results = []
    for name in BACKTEST_PAIRS:
        yf_sym = PAIRS.get(name)
        if not yf_sym:
            continue
        result = _backtest_pair(name, yf_sym, lookback_days)
        if result:
            all_results.append(result)
            logger.info(f"[{name}] {result['wins']}W/{result['losses']}L win_rate={result['win_rate']}%")

    total = sum(r["wins"] + r["losses"] for r in all_results)
    wins  = sum(r["wins"] for r in all_results)
    losses = sum(r["losses"] for r in all_results)
    summary = {
        "pairs":    all_results,
        "total":    total,
        "wins":     wins,
        "losses":   losses,
        "win_rate": round(wins / total * 100, 1) if total > 0 else None,
        "run_at":   datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    }
    try:
        os.makedirs(os.path.dirname(BACKTEST_LOG_PATH), exist_ok=True)
        with open(BACKTEST_LOG_PATH, "w") as f:
            json.dump(summary, f, indent=2)
    except Exception:
        pass
    _calibrate_from_backtest(all_results)
    logger.info(f"=== Backtest complete. {wins}W/{losses}L overall ({summary['win_rate']}%) ===")
    return summary


# ── Trend quality — ADX + consolidation ──────────────────────────────────────
def _adx(df: pd.DataFrame, period: int = 14) -> float | None:
    """ADX on OHLC data. <20 = ranging/choppy, 20-30 = trending, >30 = strong."""
    if len(df) < period * 3:
        return None
    try:
        h, l, c = df["High"], df["Low"], df["Close"]
        tr = pd.concat([h - l, (h - c.shift(1)).abs(), (l - c.shift(1)).abs()], axis=1).max(axis=1)
        up, down = h.diff(), -l.diff()
        plus_dm  = up.where((up > down) & (up > 0), 0.0)
        minus_dm = down.where((down > up) & (down > 0), 0.0)
        com = period - 1
        atr    = tr.ewm(com=com, adjust=False).mean()
        sm_pdm = plus_dm.ewm(com=com, adjust=False).mean()
        sm_ndm = minus_dm.ewm(com=com, adjust=False).mean()
        di_p   = 100 * sm_pdm / atr.replace(0, float("nan"))
        di_m   = 100 * sm_ndm / atr.replace(0, float("nan"))
        dx     = 100 * (di_p - di_m).abs() / (di_p + di_m).replace(0, float("nan"))
        adx    = dx.ewm(com=com, adjust=False).mean()
        val    = float(adx.iloc[-1])
        return None if np.isnan(val) else val
    except Exception:
        return None

def _is_consolidating(df: pd.DataFrame, lookback: int = 20, threshold_pct: float = 1.5) -> bool:
    """True if price has been in a tight range — choppy, no directional move."""
    if len(df) < lookback:
        return False
    recent = df.iloc[-lookback:]
    rng = float(recent["High"].max() - recent["Low"].min())
    mid = float(recent["Close"].mean())
    return (rng / mid * 100) < threshold_pct


# ── Active trade tracker (invalidation alerts) ───────────────────────────────
_active_trades: dict = {}
_active_trades_lock  = threading.Lock()

def _register_active_trade(name: str, direction: str, status: dict):
    key = f"{name}_{direction}"
    with _active_trades_lock:
        _active_trades[key] = {
            "pair": name, "direction": direction,
            "entry_price": status.get("price"),
            "sl":          status.get("sl"),
            "tp":          status.get("tp"),
            "sr_level":    status.get("nearest_sr"),
            "grade":       status.get("alert_grade"),
            "fired_at":    datetime.now(timezone.utc).isoformat(),
        }
    logger.info(f"[{name}] Registered as active trade @ {status.get('price')}")

def check_invalidations(pair_results: dict) -> list:
    """
    Returns list of (trade, status, reason) for setups that are now failing.
    Clears those trades from the active tracker.
    """
    invalidated = []
    now = datetime.now(timezone.utc)

    with _active_trades_lock:
        to_remove = []
        for key, trade in list(_active_trades.items()):
            pair, direction = trade["pair"], trade["direction"]
            try:
                fired = datetime.fromisoformat(trade.get("fired_at", ""))
                if (now - fired).total_seconds() > 48 * 3600:
                    logger.info(f"[{pair}] Active trade expired 48h")
                    to_remove.append(key)
                    continue
            except Exception:
                pass
            status = pair_results.get(pair)
            if not status or not status.get("price"):
                continue
            price, sl, sr = status["price"], trade.get("sl"), trade.get("sr_level")
            reason = None

            if sl:
                if direction == "LONG"  and price <= sl:
                    reason = f"SL level {sl} reached — setup stopped out"
                elif direction == "SHORT" and price >= sl:
                    reason = f"SL level {sl} reached — setup stopped out"

            if not reason and sr:
                if direction == "LONG"  and price < sr * 0.997:
                    reason = f"Support zone {sr} broken — price closed back below"
                elif direction == "SHORT" and price > sr * 1.003:
                    reason = f"Resistance zone {sr} broken — price closed back above"

            if not reason and not status.get("ema_aligned"):
                reason = "EMA 50 alignment lost — price crossed back through EMA"

            if not reason:
                curr_dir = status.get("direction", "NONE")
                if curr_dir not in (direction, "NONE"):
                    reason = f"EMA flipped to {curr_dir} — original {direction} bias no longer valid"

            if not reason:
                d_tr, h4_tr = status.get("daily_trend", ""), status.get("h4_trend", "")
                if direction == "LONG"  and (d_tr == "BEARISH" or h4_tr == "BEARISH"):
                    reason = f"Trend structure broke — Daily:{d_tr} / 4H:{h4_tr}"
                elif direction == "SHORT" and (d_tr == "BULLISH" or h4_tr == "BULLISH"):
                    reason = f"Trend structure broke — Daily:{d_tr} / 4H:{h4_tr}"

            if reason:
                invalidated.append((trade, status, reason))
                to_remove.append(key)
                logger.info(f"[{pair}] Invalidated: {reason}")

        for key in to_remove:
            _active_trades.pop(key, None)

    return invalidated


# ── Market update helper ──────────────────────────────────────────────────────
def get_market_update_data(pair_results: dict) -> dict:
    """Categorise pairs for a market status briefing (not entry signals)."""
    at_zone     = []
    approaching = []
    trending    = []

    for status in pair_results.values():
        if not status.get("ema_aligned") or not status.get("trends_agree"):
            continue
        if not status.get("is_trending", True):
            continue
        if status.get("confluence_score", 0) >= 4:
            continue  # already fired

        sr_dist = status.get("sr_dist_pct")
        if status.get("at_sr"):
            at_zone.append(status)
        elif sr_dist is not None and sr_dist <= 1.5:
            approaching.append(status)
        else:
            trending.append(status)

    def by_dist(s):
        return s.get("sr_dist_pct") or 999

    at_zone.sort(key=by_dist)
    approaching.sort(key=by_dist)
    trending.sort(key=by_dist)

    return {"at_zone": at_zone[:4], "approaching": approaching[:5], "trending": trending[:5]}

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
bos_alert_cooldown   = {}   # separate 8h cooldown per pair/direction for BOS alerts
_lock = threading.Lock()
_scan_semaphore = threading.Semaphore(4)  # max 4 pairs analysed concurrently


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


# ── Fib zone estimation ───────────────────────────────────────────────────────
def _estimate_fib_zone(df_4h: pd.DataFrame, direction: str,
                       anchor_override: float = None) -> dict | None:
    """
    Estimate the golden zone (50-61.8% retracement) from the most recent impulse.
    Matches ZSCN v8 big-picture fib logic:
      LONG:  from min(pl1, pl2) [lowest of last 2 pivot lows] → anchor_high (last HH or BOS level)
      SHORT: from max(ph1, ph2) [highest of last 2 pivot highs] → anchor_low (last LL or BOS level)

    anchor_override: use the BOS level as the impulse top/bottom when available.
    Returns fib_50, fib_618, fib_786, anchor_low, anchor_high — all rounded to 5dp.
    """
    ph = _pivot_highs(df_4h, 10)
    pl = _pivot_lows(df_4h, 10)

    if direction == "LONG":
        anchor_high = anchor_override if anchor_override else (ph[-1] if ph else None)
        if not anchor_high:
            return None
        lows_below = [p for p in pl if p < anchor_high]
        if not lows_below:
            return None
        anchor_low = min(lows_below[-2:]) if len(lows_below) >= 2 else lows_below[-1]
        rng = anchor_high - anchor_low
        if rng <= 0:
            return None
        return {
            "anchor_low":  round(anchor_low, 5),
            "anchor_high": round(anchor_high, 5),
            "fib_50":      round(anchor_high - 0.500 * rng, 5),
            "fib_618":     round(anchor_high - 0.618 * rng, 5),
            "fib_786":     round(anchor_high - 0.786 * rng, 5),
        }

    elif direction == "SHORT":
        anchor_low = anchor_override if anchor_override else (pl[-1] if pl else None)
        if not anchor_low:
            return None
        highs_above = [p for p in ph if p > anchor_low]
        if not highs_above:
            return None
        anchor_high = max(highs_above[-2:]) if len(highs_above) >= 2 else highs_above[-1]
        rng = anchor_high - anchor_low
        if rng <= 0:
            return None
        return {
            "anchor_low":  round(anchor_low, 5),
            "anchor_high": round(anchor_high, 5),
            "fib_50":      round(anchor_low + 0.500 * rng, 5),
            "fib_618":     round(anchor_low + 0.618 * rng, 5),
            "fib_786":     round(anchor_low + 0.786 * rng, 5),
        }

    return None


def _in_golden_zone(price: float, fib_zone: dict) -> bool:
    """True if price is within the 50-61.8% retracement band (the golden zone)."""
    if not fib_zone:
        return False
    lo = min(fib_zone["fib_50"], fib_zone["fib_618"])
    hi = max(fib_zone["fib_50"], fib_zone["fib_618"])
    return lo <= price <= hi


def _fib_sr_overlap(fib_zone: dict, sr_levels: list) -> tuple:
    """
    Check if any v4 S/R level falls within the 50-78.6% fib band (the full pullback zone).
    Returns (has_overlap, nearest_level_in_zone).
    """
    if not fib_zone or not sr_levels:
        return False, None
    lo = min(fib_zone["fib_50"], fib_zone["fib_786"])
    hi = max(fib_zone["fib_50"], fib_zone["fib_786"])
    inside = [l for l in sr_levels if lo <= l <= hi]
    if not inside:
        return False, None
    centre = fib_zone["fib_618"]
    best = min(inside, key=lambda l: abs(l - centre))
    return True, round(best, 5)


def _pullback_depth(price: float, fib_zone: dict, bos_level: float,
                    direction: str) -> tuple:
    """
    Classify where price is in the pullback relative to the fib zone.
    Returns (is_valid, depth_label).

    depth_label values:
      "bos_broken" — price crossed back through the BOS level → setup failed
      "too_deep"   — price past 78.6% → reversal likely, setup failing
      "deep"       — between 61.8% and 78.6% → still valid but stretched
      "golden"     — between 50% and 61.8% → ideal entry zone
      "shallow"    — pullback not yet at 50% → still waiting
    """
    if not fib_zone:
        return True, "unknown"

    f50  = fib_zone["fib_50"]
    f618 = fib_zone["fib_618"]
    f786 = fib_zone["fib_786"]

    if direction == "LONG":
        if price < bos_level:
            return False, "bos_broken"
        if price < f786:
            return False, "too_deep"
        if price < f618:
            return True, "deep"
        if price <= f50:
            return True, "golden"
        return True, "shallow"
    else:
        if price > bos_level:
            return False, "bos_broken"
        if price > f786:
            return False, "too_deep"
        if price > f618:
            return True, "deep"
        if price >= f50:
            return True, "golden"
        return True, "shallow"


# ── Pullback state tracker (persists between scans) ───────────────────────────
_pullback_tracker: dict = {}
_pullback_lock = threading.Lock()


def _register_pullback_watch(name: str, direction: str, bos_level: float,
                              fib_zone: dict | None):
    key = f"{name}_{direction}"
    with _pullback_lock:
        _pullback_tracker[key] = {
            "pair":       name,
            "direction":  direction,
            "bos_level":  bos_level,
            "fib_zone":   fib_zone,
            "registered": datetime.now(timezone.utc).isoformat(),
            "stage":      "waiting_pullback",
            "zone_alerted": False,
        }
    logger.info(f"[{name}] Registered in pullback tracker @ BOS {bos_level}")


def get_pullback_tracked() -> dict:
    """Read-only snapshot of the pullback tracker for dashboard."""
    with _pullback_lock:
        return dict(_pullback_tracker)


# ── Break of Structure detection ─────────────────────────────────────────────
def _detect_bos(df_4h: pd.DataFrame, direction: str, pivot_n: int = 10) -> dict:
    """
    Detect if a BOS (Break of Structure) occurred recently on 4H.

    Bullish BOS: a 4H bar closed above a confirmed swing high within the last 10 bars.
    Bearish BOS: a 4H bar closed below a confirmed swing low within the last 10 bars.

    The BOS level is the most recent confirmed pivot high/low from BEFORE the recent
    window — i.e. the structural level that price just broke through.

    Returns: {detected, level, bars_ago}
    """
    result = {"detected": False, "level": None, "bars_ago": None}
    lookback = 10  # 10 × 4H = ~40 hours

    if len(df_4h) < pivot_n * 3 + lookback:
        return result

    # History up to the start of the recent window (need pivot_n buffer)
    history_cut = len(df_4h) - lookback - pivot_n
    if history_cut < pivot_n * 2:
        return result

    pre_window = df_4h.iloc[:history_cut]
    closes     = df_4h["Close"].values

    if direction == "LONG":
        ph_list = _pivot_highs_indexed(pre_window, pivot_n)
        if not ph_list:
            return result
        _, bos_level = ph_list[-1]   # most recent confirmed swing high

        # Price was below that level before the window; has it crossed above?
        anchor_close = closes[-(lookback + 1)]
        if anchor_close >= bos_level:
            return result  # already above — not a new BOS

        recent = closes[-lookback:]
        for i, c in enumerate(recent):
            if c > bos_level:
                result = {
                    "detected": True,
                    "level":    round(bos_level, 5),
                    "bars_ago": lookback - 1 - i,
                }
                return result

    elif direction == "SHORT":
        pl_list = _pivot_lows_indexed(pre_window, pivot_n)
        if not pl_list:
            return result
        _, bos_level = pl_list[-1]   # most recent confirmed swing low

        anchor_close = closes[-(lookback + 1)]
        if anchor_close <= bos_level:
            return result

        recent = closes[-lookback:]
        for i, c in enumerate(recent):
            if c < bos_level:
                result = {
                    "detected": True,
                    "level":    round(bos_level, 5),
                    "bars_ago": lookback - 1 - i,
                }
                return result

    return result


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


def _find_sr_levels_v4(df_4h: pd.DataFrame) -> dict:
    """
    Replicates ZSCN Ultimate v4 S/R zone detection exactly.

    Uses ONLY 4H pivots (left=10, right=10) — no Daily, no PDH/PDL.

    Resistance: pivot highs where (pivot_high - close) / pivot_high >= 0.30%
                i.e. the high is above current price and price already moved away.
    Support:    pivot lows where (close - pivot_low) / pivot_low >= 0.30%
                i.e. the low is below current price and price already moved away.

    Deduplication: levels within 0.20% of an existing level are skipped.
    Max 5 levels each side (most recent first, matching v4's array.unshift logic).

    Returns:
        resistance: list of up to 5 prices (above current close)
        support:    list of up to 5 prices (below current close)
        all:        flat sorted list for proximity checks
    """
    if len(df_4h) < 22:
        return {"resistance": [], "support": [], "all": []}

    close       = float(df_4h["Close"].iloc[-1])
    cluster_tol = SR_CLUSTER_PCT / 100   # 0.20% as fraction
    min_move    = SR_PROXIMITY_PCT / 100  # 0.30% as fraction

    pivot_highs_list = _pivot_highs(df_4h, 10)
    pivot_lows_list  = _pivot_lows(df_4h, 10)

    def _is_dup(arr: list, level: float) -> bool:
        return any(abs(level - e) / e <= cluster_tol for e in arr)

    # Resistance: pivot highs above close that confirmed a genuine move down
    res_levels: list = []
    for ph in pivot_highs_list:
        if ph <= close:
            continue
        if (ph - close) / ph < min_move:
            continue
        if _is_dup(res_levels, ph):
            continue
        res_levels.insert(0, ph)   # most recent first (array.unshift)
        if len(res_levels) > 5:
            res_levels.pop()

    # Support: pivot lows below close that confirmed a genuine move up
    sup_levels: list = []
    for pl in pivot_lows_list:
        if pl >= close:
            continue
        if (close - pl) / pl < min_move:
            continue
        if _is_dup(sup_levels, pl):
            continue
        sup_levels.insert(0, pl)   # most recent first
        if len(sup_levels) > 5:
            sup_levels.pop()

    all_levels = sorted({round(l, 5) for l in res_levels + sup_levels})

    return {
        "resistance": [round(l, 5) for l in res_levels],
        "support":    [round(l, 5) for l in sup_levels],
        "all":        all_levels,
    }


def _find_sr_levels(df_4h: pd.DataFrame) -> list:
    """Flat list of all S/R levels — used for proximity/scoring checks."""
    return _find_sr_levels_v4(df_4h)["all"]

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
        "sr_resistance": [], "sr_support": [],
        "pdh": None, "pdl": None,
        "trendline_val": None, "at_trendline": False, "trendline_dist_pct": None,
        "h2_trend": "RANGING", "all_trends_agree": False,
        "bos_detected": False, "bos_level": None, "bos_bars_ago": None,
        "fib_zone": None, "in_golden_zone": False,
        "fib_sr_overlap": False, "fib_sr_level": None,
        "pullback_valid": True, "pullback_depth": "unknown",
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
        df_1h = _yf_download(yf_symbol, period="45d",  interval="1h")
        df_d  = _yf_download(yf_symbol, period="365d", interval="1d")

        if df_1h.empty or df_d.empty or len(df_1h) < 60 or len(df_d) < 60:
            status["error"] = "Insufficient data"
            return status

        ohlc   = {"Open": "first", "High": "max", "Low": "min", "Close": "last"}
        df_4h  = df_1h.resample("4h").agg(ohlc).dropna()
        df_2h  = df_1h.resample("2h").agg(ohlc).dropna()
        df_15m = _yf_download(yf_symbol, period="5d", interval="15m")

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

        # ── S/R levels (v4 logic — 4H pivots only, left=10 right=10) ────────
        sr_detail = _find_sr_levels_v4(df_4h)
        sr_levels = sr_detail["all"]
        nearest_sr, sr_dist = _nearest_sr(price, sr_levels)
        above, below = _levels_above_below(price, sr_levels)

        status["nearest_sr"]    = round(nearest_sr, 5) if nearest_sr else None
        status["sr_dist_pct"]   = sr_dist
        status["at_sr"]         = sr_dist is not None and sr_dist <= SR_PROXIMITY_PCT
        status["sr_above"]      = [round(l, 5) for l in above]
        status["sr_below"]      = [round(l, 5) for l in below]
        status["sr_resistance"] = sr_detail["resistance"]  # levels above price
        status["sr_support"]    = sr_detail["support"]     # levels below price

        # ── Trend line (bonus) ───────────────────────────────────────────────
        tl_val = _trendline_value(df_4h, direction, pivot_n=10)
        if tl_val is not None:
            tl_dist = abs(tl_val - price) / price * 100
            status["trendline_val"]      = round(tl_val, 5)
            status["trendline_dist_pct"] = round(tl_dist, 3)
            status["at_trendline"]       = tl_dist <= TRENDLINE_PROXIMITY

        # ── Break of Structure ───────────────────────────────────────────────
        bos = _detect_bos(df_4h, direction)
        status["bos_detected"] = bos["detected"]
        status["bos_level"]    = bos["level"]
        status["bos_bars_ago"] = bos["bars_ago"]

        # ── Fib zone (estimated from impulse, anchored to BOS level if available) ──
        fib_anchor = bos["level"] if bos["detected"] else None
        # Also check pullback tracker for a previously registered BOS
        _pb_key = f"{name}_{direction}"
        _tracked = _pullback_tracker.get(_pb_key)
        if _tracked and not fib_anchor:
            fib_anchor = _tracked.get("bos_level")
        fib_zone = _estimate_fib_zone(df_4h, direction, anchor_override=fib_anchor)
        if fib_zone:
            in_golden    = _in_golden_zone(price, fib_zone)
            has_overlap, overlap_level = _fib_sr_overlap(fib_zone, sr_levels)
            pb_valid, pb_depth = _pullback_depth(
                price, fib_zone,
                bos_level=fib_anchor or fib_zone["anchor_high"] if direction == "LONG" else fib_anchor or fib_zone["anchor_low"],
                direction=direction,
            )
            status["fib_zone"]      = fib_zone
            status["in_golden_zone"] = in_golden
            status["fib_sr_overlap"] = has_overlap
            status["fib_sr_level"]   = overlap_level
            status["pullback_valid"] = pb_valid
            status["pullback_depth"] = pb_depth

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

        # ── Trend quality (ADX + consolidation) ─────────────────────────────
        adx_val = _adx(df_4h)
        consolidating = _is_consolidating(df_4h)
        is_trending   = (adx_val is not None and adx_val >= 20) and not consolidating
        trend_strength = (
            "STRONG"   if adx_val and adx_val >= 30 else
            "MODERATE" if adx_val and adx_val >= 20 else
            "WEAK"
        )
        status["adx_4h"]         = round(adx_val, 1) if adx_val is not None else None
        status["is_trending"]    = is_trending
        status["trend_strength"] = trend_strength
        status["consolidating"]  = consolidating

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

        if status["in_golden_zone"] and status["fib_sr_overlap"]:
            fz = status["fib_zone"]
            detail.append(
                f"BONUS — Fib+S/R overlap: price in golden zone "
                f"({fz['fib_50']}–{fz['fib_618']}) + v4 S/R at {status['fib_sr_level']}"
            )
        elif status["in_golden_zone"]:
            fz = status["fib_zone"]
            detail.append(
                f"Fib zone: price in golden zone ({fz['fib_50']}–{fz['fib_618']}) "
                f"— no S/R overlap yet"
            )

        # ── Alert grade ───────────────────────────────────────────────────────
        fib_confluence = status["in_golden_zone"] and status["fib_sr_overlap"]
        bonus_count = sum([
            all_trends_agree,
            status.get("at_trendline", False),
            status.get("is_retest", False),
            status.get("signal_quality") == "HIGH",
            fib_confluence,   # fib zone + S/R overlap = the gold standard
        ])
        if score >= 4:
            # A+ requires fib+S/R overlap OR 2 other bonus conditions
            status["alert_grade"] = "A+" if (fib_confluence or bonus_count >= 2) else "A"
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

    finally:
        gc.collect()

    return status


# ── Scan all 28 pairs ─────────────────────────────────────────────────────────
def run_scan(send_telegram_fn, send_early_warning_fn=None, send_bos_alert_fn=None):
    logger.info("=== ZSCN brain scan started ===")
    results = {}

    for name, yf_sym in PAIRS.items():
        results[name] = _check_pair(name, yf_sym)

    now = datetime.now(timezone.utc)

    # Check outcomes and invalidations against latest results
    newly_resolved = update_trade_outcomes(results)
    invalidated    = check_invalidations(results)
    # Caller (app.py) handles notifications for both

    with _lock:
        for name, status in results.items():
            pair_status[name] = status
            direction    = status.get("direction", "NONE")
            score        = status.get("confluence_score", 0)

            # Hard requirements: EMA aligned AND Daily+4H trends agree.
            if not status.get("ema_aligned") or direction == "NONE":
                continue
            if not status.get("trends_agree"):
                continue

            # Quality gate: skip choppy/ranging markets
            if not status.get("is_trending", True):
                adx = status.get("adx_4h", "?")
                logger.info(f"[{name}] {direction} - ADX {adx} < 20 (choppy), skipping")
                continue

            # Quality gate: skip pairs with poor backtest history
            if name in LOW_CONFIDENCE_PAIRS:
                logger.info(f"[{name}] Low-confidence pair (backtest < 40% win rate) — skipping")
                continue

            cooldown_key = f"{name}_{direction}"
            early_key    = f"{name}_{direction}_early"

            pb_key = f"{name}_{direction}"

            # ── Pullback tracker: check pairs registered after a BOS ─────────
            # This runs BEFORE the main alert logic so we can handle
            # invalidations and zone-reached alerts for tracked pairs.
            tracked = _pullback_tracker.get(pb_key)
            if tracked:
                bos_lvl = tracked.get("bos_level")
                price    = status["price"]
                fib_zone = tracked.get("fib_zone") or status.get("fib_zone")
                pb_valid, pb_depth = _pullback_depth(
                    price, fib_zone, bos_lvl, direction
                )

                if pb_depth == "bos_broken":
                    # Setup invalidated — BOS failed, price crossed back through
                    reason = f"Price closed back through BOS level {bos_lvl} — structure broke"
                    if send_bos_alert_fn:
                        send_bos_alert_fn(status, invalidated_reason=reason)
                    with _pullback_lock:
                        _pullback_tracker.pop(pb_key, None)
                    logger.info(f"[{name}] Pullback INVALIDATED — {reason}")

                elif pb_depth == "too_deep":
                    # Pullback went past 78.6% — likely reversing, warn Harry
                    if not tracked.get("deep_warned"):
                        if send_bos_alert_fn:
                            send_bos_alert_fn(status,
                                invalidated_reason=(
                                    f"Pullback went past 78.6% fib ({fib_zone['fib_786'] if fib_zone else '?'}) "
                                    f"— setup is stretching too far"
                                ))
                        with _pullback_lock:
                            if pb_key in _pullback_tracker:
                                _pullback_tracker[pb_key]["deep_warned"] = True
                    logger.info(f"[{name}] Pullback too deep (>{pb_depth})")

                elif pb_depth in ("golden", "deep") and not tracked.get("zone_alerted"):
                    # Price entered the fib zone — now check S/R + signal
                    has_fib_sr = status.get("fib_sr_overlap") or status.get("at_sr")
                    if has_fib_sr and send_early_warning_fn:
                        # Zone reached — fire a "zone reached" alert even if
                        # score is 3 (no 15M signal yet)
                        with _pullback_lock:
                            if pb_key in _pullback_tracker:
                                _pullback_tracker[pb_key]["zone_alerted"] = True
                        early = {**status, "received_at": now.strftime("%Y-%m-%d %H:%M UTC"),
                                 "pullback_zone_reached": True}
                        recent_alerts.insert(0, early)
                        if len(recent_alerts) > 100:
                            recent_alerts.pop()
                        _save_alerts_to_disk(recent_alerts)
                        send_early_warning_fn(status)
                        logger.info(
                            f"[{name}] {direction} — pullback in fib zone "
                            f"({pb_depth}) + S/R — zone alert sent"
                        )

                # Auto-expire tracked pairs after 5 days
                try:
                    registered = datetime.fromisoformat(tracked.get("registered", ""))
                    if (now - registered).total_seconds() > 5 * 24 * 3600:
                        with _pullback_lock:
                            _pullback_tracker.pop(pb_key, None)
                        logger.info(f"[{name}] Pullback tracker expired (5d)")
                except Exception:
                    pass

            # ── Full alert (4/4) ─────────────────────────────────────────────
            if status.get("alert"):
                last_alerted = alert_cooldown.get(cooldown_key)
                if last_alerted and (now - last_alerted) < timedelta(hours=ALERT_COOLDOWN_HOURS):
                    logger.info(f"[{name}] {direction} - full cooldown active, skipping")
                    continue
                alert_cooldown[cooldown_key] = now
                early_alert_cooldown.pop(early_key, None)
                # Full alert reached — remove from pullback tracker (setup complete)
                with _pullback_lock:
                    _pullback_tracker.pop(pb_key, None)
                alert = {**status, "received_at": now.strftime("%Y-%m-%d %H:%M UTC")}
                recent_alerts.insert(0, alert)
                if len(recent_alerts) > 100:
                    recent_alerts.pop()
                _save_alerts_to_disk(recent_alerts)
                # Log for outcome tracking
                trade_outcomes.insert(0, {
                    "pair": name, "direction": direction,
                    "entry_price": status.get("price"),
                    "sl":          status.get("sl"),
                    "tp":          status.get("tp"),
                    "grade":       status.get("alert_grade"),
                    "adx":         status.get("adx_4h"),
                    "fired_at":    now.strftime("%Y-%m-%d %H:%M UTC"),
                    "result":      "pending",
                })
                _save_outcomes(trade_outcomes)
                _register_active_trade(name, direction, status)
                send_telegram_fn(status)
                logger.info(f"[{name}] {direction} - 4/4 - FULL ALERT sent")

            # ── BOS alert ────────────────────────────────────────────────────
            # Stage 1: BOS detected with full EMA + trend alignment.
            # Register pair in pullback tracker + alert Harry to draw fib.
            if (status.get("bos_detected") and send_bos_alert_fn is not None
                    and not status.get("alert")):
                bos_key = f"{name}_{direction}_bos"
                last_bos = bos_alert_cooldown.get(bos_key)
                if not last_bos or (now - last_bos) >= timedelta(hours=8):
                    bos_alert_cooldown[bos_key] = now
                    # Register in pullback tracker so subsequent scans watch this pair
                    _register_pullback_watch(
                        name, direction,
                        bos_level=status["bos_level"],
                        fib_zone=status.get("fib_zone"),
                    )
                    send_bos_alert_fn(status)
                    logger.info(
                        f"[{name}] {direction} - BOS at {status['bos_level']} "
                        f"({status['bos_bars_ago']} bars ago) - BOS alert sent"
                    )

            # ── Early warning (3/4 at zone, not from a tracked BOS) ──────────
            elif (score == 3 and send_early_warning_fn is not None
                    and not tracked):  # tracked pairs handled above
                if not status.get("at_sr"):
                    sr_dist = status.get("sr_dist_pct")
                    logger.info(f"[{name}] {direction} - 3/4 but not at zone yet ({sr_dist}%), skipping")
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
                logger.info(f"[{name}] {direction} - 3/4 at zone - early warning sent")

    logger.info(f"=== Scan complete. {len(results)} pairs checked ===")
    return invalidated, results, newly_resolved


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

    for name, yf_sym in PAIRS.items():
        results[name] = _check_pair(name, yf_sym)

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

    for name, yf_sym in PAIRS.items():
        results[name] = _check_pair(name, yf_sym)

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
