import json
import os
import logging
import threading
import requests
from datetime import datetime, timezone, timedelta
from flask import Flask, render_template, jsonify, request
from apscheduler.schedulers.background import BackgroundScheduler

from monitor import (run_scan, run_weekly_bias, run_friday_preview,
                     pair_status, recent_alerts, ALERTS_LOG_PATH,
                     get_win_rate, get_market_update_data,
                     get_weekly_trade_report, run_backtest)

last_scan_time = None

# Active setups registered via TradingView ZSCN v4 webhook (stage 5 only)
# key = pair (e.g. "EURUSD"), value = {pair, direction, entry_price, sl, tp, grade, fired_at}
active_webhook_setups: dict = {}

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s - %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT  = os.environ.get("TELEGRAM_CHAT_ID", "")


def _tg_post(msg: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT:
        logger.warning("Telegram credentials not set")
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT, "text": msg, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception as e:
        logger.error(f"Telegram error: {e}")


def _confluence_tick(item: str) -> str:
    if "BONUS" in item:
        return "⭐"
    if any(kw in item for kw in ("partial", "not at zone", "none yet", "diverges")):
        return "⏳"
    return "✅"


def _setup_narrative(status: dict) -> str:
    """One unique sentence describing what makes this specific setup stand out."""
    d    = status["direction"]
    bias = "bullish" if d == "LONG" else "bearish"

    if status.get("is_retest"):
        rtype = status.get("retest_type", "")
        noun  = "old resistance reclaimed as support" if "Resistance" in rtype else "old support flipped to resistance"
        bars  = status.get("bars_since_break", "?")
        return f"{noun.capitalize()} — structural role reversal ({bars} bars since break)."

    if status.get("at_trendline") and status.get("at_sr"):
        tl = status.get("trendline_val", "")
        return f"Diagonal trend line ({tl}) coincides with horizontal S/R — rare dual-confluence zone."

    if status.get("all_trends_agree"):
        h2 = status.get("h2_trend", "").capitalize()
        return f"Daily + 4H + 2H all {bias} ({h2}) — maximum trend alignment on this pair."

    sig_quality = status.get("signal_quality", "None")
    sig_type    = status.get("signal_15m", "")
    if sig_quality == "HIGH" and sig_type != "None":
        action = "rejection from support" if d == "LONG" else "rejection from resistance"
        return f"Aggressive {sig_type.lower()} — strong {action}, wick dominates the candle."

    # Highlight S/R touch count if notable
    nearest = status.get("nearest_sr")
    if nearest:
        res = status.get("sr_resistance", [])
        sup = status.get("sr_support", [])
        for lvl in res + sup:
            if isinstance(lvl, (int, float)) and abs(lvl - nearest) / nearest * 100 < 0.25:
                zone_type = "Resistance" if lvl in res else "Support"
                return f"v4 {zone_type} zone at {round(lvl, 5)} — key 4H structural level."

    sr_dist = status.get("sr_dist_pct")
    nr      = status.get("nearest_sr")
    if nr and sr_dist is not None:
        return f"Price at {nr} ({sr_dist}% from zone) — all required {bias} confluences met."

    return f"All required {bias} confluences stack on this pair."


def _closing_cta(status: dict, is_early: bool) -> str:
    """Varied call-to-action based on grade, signal quality, and setup type."""
    grade       = status.get("alert_grade", "A")
    sig_quality = status.get("signal_quality", "None")
    d           = status["direction"]

    if is_early:
        nr = status.get("nearest_sr", "the zone")
        return f"🕐 Price is AT {nr} — watch 15M for a clean pin bar or engulfing before entering."

    if grade == "A+":
        return "🔥 Highest conviction. Draw your fib — if 50% or 61.8% lands here, this is your entry."

    if sig_quality == "HIGH":
        return "👉 Strong rejection candle confirms the zone. Map fib, place entry, SL and TP."

    if sig_quality == "MEDIUM":
        return "👉 Setup is valid — signal candle is moderate quality. Confirm fib alignment before entering."

    return "👉 All confluences met. Check fib zone on chart, then place entry, SL and TP."


def _build_alert_body(status: dict, is_early: bool) -> list:
    lines = []
    price = status["price"]
    d     = status["direction"]

    # ── Core confluences (first 4 items = scored) ────────────────────────────
    detail      = status.get("confluence_detail", [])
    core_items  = detail[:4]
    extra_items = detail[4:]

    for item in core_items:
        lines.append(f"{_confluence_tick(item)} {item}")

    # ── What's still missing (early warning only) ────────────────────────────
    if is_early and not status.get("has_signal"):
        sig_hint = "bullish pin bar or engulfing" if d == "LONG" else "bearish pin bar or engulfing"
        lines.append(f"")
        lines.append(f"⏳ <b>Waiting for:</b> 15M {sig_hint} at the zone")

    # ── Bonus confluences (separated visually) ───────────────────────────────
    bonus_items = [item for item in extra_items if "BONUS" in item]
    other_items = [item for item in extra_items if "BONUS" not in item]

    for item in other_items:
        lines.append(f"  ↳ {item}")

    if bonus_items:
        lines.append("")
        lines.append("⭐ <b>Bonus confluences</b>")
        for item in bonus_items:
            clean = item.replace("BONUS — ", "").replace("BONUS — ", "")
            lines.append(f"  ⭐ {clean}")

    # ── S/R map ──────────────────────────────────────────────────────────────
    above = status.get("sr_above", [])
    below = status.get("sr_below", [])
    pdh   = status.get("pdh")
    pdl   = status.get("pdl")

    lines.append("")
    lines.append("📊 <b>S/R Map</b>")
    if pdh:
        lines.append(f"  PDH: {pdh}")
    if pdl:
        lines.append(f"  PDL: {pdl}")
    if above:
        lines.append(f"  Resistance above: {' | '.join(str(l) for l in above)}")
    if below:
        lines.append(f"  Support below:    {' | '.join(str(l) for l in below)}")
    if status.get("at_trendline"):
        lines.append(f"  ⭐ Trend line: {status['trendline_val']} ({status.get('trendline_dist_pct')}% away)")
    if status.get("is_retest"):
        lines.append(f"  ↩ Break+Retest: {status['retest_level']} — {status['retest_type']} ({status.get('bars_since_break')} bars ago)")

    # ── SL / TP — only shown on HIGH conviction (HIGH quality signal, not early) ─
    sl  = status.get("sl")
    tp  = status.get("tp")
    sig = status.get("signal_quality", "None")
    show_levels = sl and tp and not is_early and sig == "HIGH"

    if show_levels:
        risk     = abs(price - sl)
        reward   = abs(tp - price)
        rr       = f"{reward/risk:.1f}" if risk > 0 else "—"
        sl_label = "below swing low at S/R" if d == "LONG" else "above swing high at S/R"
        lines.append("")
        lines.append("📍 <b>Trade Levels (1:3 R:R)</b>")
        lines.append(f"  Entry: ~{price}")
        lines.append(f"  SL:    {sl}  ← {sl_label}")
        lines.append(f"  TP:    {tp}  ← R:R {rr}:1")
    elif sl and tp and not is_early and sig != "HIGH":
        lines.append("")
        lines.append(f"⚠️ Signal candle quality is MEDIUM — wait for a stronger rejection before entering.")
        lines.append(f"  Estimated levels when confirmed: SL ~{sl}  /  TP ~{tp}")
    elif is_early:
        if sl and tp:
            lines.append("")
            lines.append(f"📍 <b>Suggested Levels (once signal confirms)</b>")
            lines.append(f"  Entry: ~{price}   SL: ~{sl}   TP: ~{tp}")

    return lines


def send_bos_alert(status: dict, invalidated_reason: str = None):
    """
    Handles all BOS-related Telegram messages:
    - invalidated_reason=None  → Stage 1: fresh BOS, draw fib and watch for pullback
    - invalidated_reason=str   → BOS failed / pullback too deep → setup cancelled
    """
    d     = status["direction"]
    pair  = status["pair"]
    price = status["price"]
    tag   = "🟢 LONG" if d == "LONG" else "🔴 SHORT"

    if invalidated_reason:
        # Setup failed — tell Harry to cancel the watch
        lines = [
            f"❌ SETUP CANCELLED — {tag} <b>{pair}</b>",
            f"Price: <b>{price}</b>",
            "",
            f"<i>{invalidated_reason}</i>",
            "",
            "The setup no longer meets criteria. Remove from watchlist.",
            datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        ]
        _tg_post("\n".join(lines))
        return

    # Stage 1: fresh BOS
    bos_level = status.get("bos_level")
    bars_ago  = status.get("bos_bars_ago", 0)
    broke_what     = "swing HIGH" if d == "LONG" else "swing LOW"
    direction_word = "bullish" if d == "LONG" else "bearish"
    hours_ago      = (bars_ago or 0) * 4

    d_tr  = status.get("daily_trend", "?")
    h4_tr = status.get("h4_trend",   "?")
    h2_tr = status.get("h2_trend",   "?")
    adx   = status.get("adx_4h", "?")

    fib_zone = status.get("fib_zone")
    fib_lines = []
    if fib_zone:
        fib_lines = [
            "",
            "Estimated Fib Zone (verify on chart):",
            f"  50%  → <b>{fib_zone['fib_50']}</b>",
            f"  61.8% → <b>{fib_zone['fib_618']}</b>",
            f"  Impulse: {fib_zone['anchor_low']} → {fib_zone['anchor_high']}",
        ]
        if status.get("fib_sr_overlap"):
            fib_lines.append(
                f"  ⚡ v4 S/R at <b>{status['fib_sr_level']}</b> overlaps the fib zone"
            )

    lines = [
        f"🚨 BREAK OF STRUCTURE — {tag} <b>{pair}</b>",
        f"4H closed {'above' if d == 'LONG' else 'below'} {broke_what} "
        f"at <b>{bos_level}</b>  (~{hours_ago}h ago)",
        "",
        "EMA 50 + Trend — all aligned:",
        f"  Daily {d_tr}  ·  4H {h4_tr}  ·  2H {h2_tr}  ·  ADX {adx}",
        f"  Current price: <b>{price}</b>",
    ]
    lines += fib_lines
    lines += [
        "",
        f"<b>Structure broke {direction_word}.</b>",
        "Draw your fib. Watch for pullback into 50-61.8% zone + v4 S/R.",
        "Entry trigger: 15M pin bar or engulfing at that zone.",
        "",
        datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    ]
    _tg_post("\n".join(lines))


def send_early_warning(status: dict):
    d     = status["direction"]
    pair  = status["pair"]
    price = status["price"]
    score = status["confluence_score"]
    grade = status.get("alert_grade", "WATCH")
    tag   = "🟢 LONG" if d == "LONG" else "🔴 SHORT"

    # Differentiate: came from pullback tracker (post-BOS) vs standalone zone touch
    from_pullback  = status.get("pullback_zone_reached", False)
    pb_depth       = status.get("pullback_depth", "")
    in_golden      = status.get("in_golden_zone", False)
    fib_sr_overlap = status.get("fib_sr_overlap", False)

    if from_pullback and in_golden and fib_sr_overlap:
        header = f"🎯 PULLBACK AT FIB+S/R ZONE — {tag} <b>{pair}</b>"
    elif from_pullback and in_golden:
        header = f"📍 PULLBACK IN FIB ZONE — {tag} <b>{pair}</b>"
    elif status.get("is_retest"):
        header = f"⚠️ BREAK+RETEST FORMING — {tag} <b>{pair}</b>"
    else:
        header = f"⚠️ ZONE REACHED — {tag} <b>{pair}</b>"

    narrative = _setup_narrative(status)
    lines = [
        header,
        f"Grade: <b>{grade}</b>   |   Price: <b>{price}</b>   |   Confluences: <b>{score}/4</b>",
        f"<i>{narrative}</i>",
        "",
    ]
    lines += _build_alert_body(status, is_early=True)

    # Add fib zone detail if available
    fib_zone = status.get("fib_zone")
    if fib_zone:
        depth_tag = {
            "golden": "✅ In golden zone (50-61.8%)",
            "deep":   "⚠️ Deep pullback (61.8-78.6%)",
            "shallow":"⏳ Above 50% — still pulling back",
        }.get(pb_depth, "")
        lines += [
            "",
            f"Fib Zone:  50% {fib_zone['fib_50']}  ·  61.8% {fib_zone['fib_618']}",
        ]
        if depth_tag:
            lines.append(f"Depth:     {depth_tag}")
        if fib_sr_overlap:
            lines.append(f"v4 S/R:    ⚡ {status['fib_sr_level']} inside fib zone — CONFLUENCE")

    lines += [
        "",
        "Waiting for 15M pin bar or engulfing to confirm entry.",
        datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    ]
    _tg_post("\n".join(lines))


def send_telegram(status: dict):
    d     = status["direction"]
    pair  = status["pair"]
    price = status["price"]
    score = status["confluence_score"]
    grade = status.get("alert_grade", "A")
    tag   = "🟢 LONG" if d == "LONG" else "🔴 SHORT"

    if grade == "A+":
        header = f"🔥 HIGH CONVICTION — {tag} <b>{pair}</b>"
    elif status.get("is_retest"):
        header = f"↩ BREAK+RETEST — {tag} <b>{pair}</b>"
    else:
        header = f"✅ ENTRY SIGNAL — {tag} <b>{pair}</b>"

    narrative = _setup_narrative(status)
    lines = [
        header,
        f"Grade: <b>{grade}</b>   |   Price: <b>{price}</b>   |   Confluences: <b>{score}/4</b>",
        f"<i>{narrative}</i>",
        "",
    ]
    lines += _build_alert_body(status, is_early=False)

    # Show fib zone if it's part of the confluence (A+ grade)
    fib_zone = status.get("fib_zone")
    if fib_zone and status.get("in_golden_zone"):
        depth_tag = {
            "golden": "In golden zone (50-61.8%) ✅",
            "deep":   "Deep zone (61.8-78.6%) ⚠️",
        }.get(status.get("pullback_depth", ""), "")
        lines += [
            "",
            f"Fib Zone:  50% {fib_zone['fib_50']}  ·  61.8% {fib_zone['fib_618']}",
        ]
        if depth_tag:
            lines.append(f"Depth:     {depth_tag}")
        if status.get("fib_sr_overlap"):
            lines.append(
                f"v4 S/R:    ⚡ {status['fib_sr_level']} — S/R inside fib zone (triple confluence)"
            )

    lines += [
        "",
        _closing_cta(status, is_early=False),
        datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    ]
    _tg_post("\n".join(lines))


def send_weekly_bias(ready: list, watch: list, early: list):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    lines = [
        f"📅 <b>ZSCN Weekly Bias — {now}</b>",
        "",
    ]

    if ready:
        lines.append("🔥 <b>READY TO ENTER (4/4)</b>")
        for s in ready:
            tag = "🟢" if s["direction"] == "LONG" else "🔴"
            lines.append(f"  {tag} {s['pair']} @ {s['price']}")
        lines.append("")

    if watch:
        lines.append("👀 <b>WATCH THIS WEEK (EMA + Trend aligned)</b>")
        for s in watch:
            tag   = "🟢" if s["direction"] == "LONG" else "🔴"
            score = s.get("confluence_score", 0)
            dist  = s.get("sr_dist_pct")
            dist_str = f" — {dist}% from S/R" if dist else ""
            lines.append(f"  {tag} {s['pair']} {score}/4{dist_str}")
        lines.append("")

    if early:
        lines.append("📌 <b>EARLY STAGE (EMA aligned, structure pending)</b>")
        longs  = [s["pair"] for s in early if s["direction"] == "LONG"]
        shorts = [s["pair"] for s in early if s["direction"] == "SHORT"]
        if longs:
            lines.append(f"  Bullish: {', '.join(longs)}")
        if shorts:
            lines.append(f"  Bearish: {', '.join(shorts)}")
        lines.append("")

    if not ready and not watch and not early:
        lines.append("No aligned pairs found. Market is choppy — stay patient.")

    wr = get_win_rate()
    if wr["total"] >= 3:
        lines.append(f"📊 Scanner record: {wr['wins']}W / {wr['losses']}L ({wr['win_rate']}% win rate, {wr['total']} tracked)")

    lines.append("Good luck this week. Only trade the 4/4 setups. 💪")
    _tg_post("\n".join(lines))


def send_friday_preview(hot: list, building: list, watching: list, early: list):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    lines = [
        f"📅 <b>ZSCN Friday Preview — Next Week — {now}</b>",
        "Here's what to watch when the market reopens Sunday.",
        "",
    ]

    if hot:
        lines.append("🔥 <b>AT THE ZONE — could trigger next week</b>")
        lines.append("  (EMA + trend aligned + price within 1% of S/R)")
        for s in hot:
            tag  = "🟢" if s["direction"] == "LONG" else "🔴"
            dist = s.get("sr_dist_pct", "?")
            nr   = s.get("nearest_sr", "")
            sl   = s.get("sl")
            tp   = s.get("tp")
            line = f"  {tag} <b>{s['pair']}</b> @ {s['price']}  →  {dist}% from S/R {nr}"
            if sl and tp:
                line += f"  |  SL {sl} / TP {tp}"
            lines.append(line)
        lines.append("")

    if building:
        lines.append("📈 <b>BUILDING UP — approaching zone (1–3% away)</b>")
        for s in building:
            tag  = "🟢" if s["direction"] == "LONG" else "🔴"
            dist = s.get("sr_dist_pct", "?")
            nr   = s.get("nearest_sr", "")
            lines.append(f"  {tag} {s['pair']} @ {s['price']}  →  {dist}% from S/R {nr}")
        lines.append("")

    if watching:
        lines.append("👀 <b>WATCHING — trend aligned but far from zone (&gt;3%)</b>")
        longs  = [s["pair"] for s in watching if s["direction"] == "LONG"]
        shorts = [s["pair"] for s in watching if s["direction"] == "SHORT"]
        if longs:
            lines.append(f"  🟢 Bullish: {', '.join(longs)}")
        if shorts:
            lines.append(f"  🔴 Bearish: {', '.join(shorts)}")
        lines.append("")

    if early:
        lines.append("📌 <b>EARLY STAGE — EMA aligned, structure still forming</b>")
        longs  = [s["pair"] for s in early if s["direction"] == "LONG"]
        shorts = [s["pair"] for s in early if s["direction"] == "SHORT"]
        if longs:
            lines.append(f"  Bullish: {', '.join(longs)}")
        if shorts:
            lines.append(f"  Bearish: {', '.join(shorts)}")
        lines.append("")

    if not hot and not building and not watching and not early:
        lines.append("No aligned pairs. Market is choppy — enjoy the weekend, stay out.")

    lines.append("Have a good weekend. Only trade the 4/4 setups. 💪")
    _tg_post("\n".join(lines))


def send_week_opener(ready: list, watch: list, early: list):
    """Sunday 22:00 UTC — market just opened. Urgent version of weekly bias."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    lines = [
        f"🗓 <b>WEEK OPENER — Market Just Opened — {now}</b>",
        "Here's what's live right now as the new week begins.",
        "",
    ]

    if ready:
        lines.append("🔥 <b>READY TO ENTER NOW (4/4)</b>")
        lines.append("  Fib zone is yours — confirm entry on 15M.")
        for s in ready:
            tag   = "🟢" if s["direction"] == "LONG" else "🔴"
            grade = s.get("alert_grade", "A")
            sl    = s.get("sl")
            tp    = s.get("tp")
            line  = f"  {tag} <b>{s['pair']}</b> @ {s['price']}  Grade {grade}"
            if sl and tp:
                line += f"  |  SL {sl} / TP {tp}"
            lines.append(line)
        lines.append("")

    if watch:
        lines.append("👀 <b>WATCH THIS WEEK — EMA + trend ready, approaching zone</b>")
        for s in watch:
            tag   = "🟢" if s["direction"] == "LONG" else "🔴"
            score = s.get("confluence_score", 0)
            dist  = s.get("sr_dist_pct")
            nr    = s.get("nearest_sr", "")
            dist_str = f"  {dist}% from S/R {nr}" if dist else ""
            lines.append(f"  {tag} {s['pair']} {score}/4{dist_str}")
        lines.append("")

    if early:
        lines.append("📌 <b>EARLY STAGE — EMA aligned, structure pending</b>")
        longs  = [s["pair"] for s in early if s["direction"] == "LONG"]
        shorts = [s["pair"] for s in early if s["direction"] == "SHORT"]
        if longs:
            lines.append(f"  🟢 Bullish: {', '.join(longs)}")
        if shorts:
            lines.append(f"  🔴 Bearish: {', '.join(shorts)}")
        lines.append("")

    if not ready and not watch and not early:
        lines.append("No aligned pairs at open. Market may be choppy — be patient.")

    lines.append("Trade well this week. Only take the 4/4. 💪")
    _tg_post("\n".join(lines))


def send_tp_sl_alert(outcome: dict):
    direction = outcome.get("direction", "")
    pair      = outcome.get("pair", "?")
    result    = outcome.get("result", "")
    entry     = outcome.get("entry_price", "?")
    tag       = "🟢 LONG" if direction == "LONG" else "🔴 SHORT"

    if result == "WIN":
        tp = outcome.get("tp", "?")
        lines = [
            f"🎯 TP HIT — {tag} <b>{pair}</b>",
            f"Entry: <b>{entry}</b>  →  TP: <b>{tp}</b>  ✅",
            f"Trade logged as <b>WIN</b>. Well done.",
            datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        ]
    else:
        sl = outcome.get("sl", "?")
        lines = [
            f"💀 SL HIT — {tag} <b>{pair}</b>",
            f"Entry: <b>{entry}</b>  →  SL: <b>{sl}</b>  ❌",
            f"Trade logged as <b>LOSS</b>. Cut it and move on.",
            datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        ]
    _tg_post("\n".join(lines))


def send_weekly_trade_summary():
    report = get_weekly_trade_report()
    now    = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    lines = [f"📋 <b>Weekly Trade Log — {now}</b>", ""]

    if report["total"] == 0:
        lines.append("No closed trades this week — all setups still pending or no signals fired.")
        lines.append("Patience is a position. 💪")
        _tg_post("\n".join(lines))
        return

    wr_str = f"{report['win_rate']}%" if report["win_rate"] is not None else "—"
    lines.append(f"<b>{len(report['wins'])}W / {len(report['losses'])}L</b>  |  Win rate: <b>{wr_str}</b>  |  Trades closed: {report['total']}")
    lines.append("")

    if report["wins"]:
        lines.append("✅ <b>Winners</b>")
        for t in report["wins"]:
            tag   = "🟢" if t.get("direction") == "LONG" else "🔴"
            entry = t.get("entry_price", "?")
            tp    = t.get("tp", "?")
            grade = t.get("grade", "A")
            at    = t.get("result_at", "")[:10]
            lines.append(f"  {tag} <b>{t['pair']}</b> {t.get('direction','')}  Entry {entry} → TP {tp}  [{grade}] {at}")
        lines.append("")

    if report["losses"]:
        lines.append("❌ <b>Stopped out</b>")
        for t in report["losses"]:
            tag   = "🟢" if t.get("direction") == "LONG" else "🔴"
            entry = t.get("entry_price", "?")
            sl    = t.get("sl", "?")
            grade = t.get("grade", "A")
            at    = t.get("result_at", "")[:10]
            lines.append(f"  {tag} <b>{t['pair']}</b> {t.get('direction','')}  Entry {entry} → SL {sl}  [{grade}] {at}")
        lines.append("")

    wr = get_win_rate()
    if wr["total"] >= 3:
        lines.append(f"📊 All-time: {wr['wins']}W/{wr['losses']}L  ({wr['win_rate']}% win rate, {wr['total']} trades tracked)")

    lines.append("")
    lines.append("Review each trade — what worked, what didn't. Only way to grow. 💪")
    _tg_post("\n".join(lines))


def send_backtest_report(results: dict):
    run_at = results.get("run_at", "")
    pairs  = results.get("pairs", [])
    total  = results.get("total", 0)
    wins   = results.get("wins", 0)
    losses = results.get("losses", 0)
    wr     = results.get("win_rate")

    if total == 0:
        _tg_post("📉 Backtest ran but found no qualifying signals in the lookback window.")
        return

    lines = [
        f"🔬 <b>ZSCN Backtest Report — {run_at}</b>",
        f"Overall: <b>{wins}W / {losses}L</b>  |  Win rate: <b>{wr}%</b>  |  {total} signals",
        "",
        "📊 <b>By pair</b>",
    ]

    for r in sorted(pairs, key=lambda x: -(x.get("win_rate") or 0)):
        t = r["wins"] + r["losses"]
        if t == 0:
            lines.append(f"  {r['pair']}: no signals")
            continue
        bar  = "🟢" if (r["win_rate"] or 0) >= 55 else ("🟡" if (r["win_rate"] or 0) >= 40 else "🔴")
        flag = " ⚠️ LOW CONFIDENCE" if (r["win_rate"] or 100) < 40 and t >= 5 else ""
        lines.append(f"  {bar} {r['pair']}: {r['wins']}W/{r['losses']}L  ({r['win_rate']}%){flag}")

    from monitor import LOW_CONFIDENCE_PAIRS
    if LOW_CONFIDENCE_PAIRS:
        lines.append("")
        lines.append(f"⚠️ Pairs SKIPPED going forward (< 40% win rate): {', '.join(LOW_CONFIDENCE_PAIRS)}")
    else:
        lines.append("")
        lines.append("✅ All pairs performing at or above threshold — no pairs skipped.")

    lines.append("")
    lines.append("Strategy calibrated. Next backtest in ~30 days.")
    _tg_post("\n".join(lines))


def send_invalidation_alert(trade: dict, status: dict, reason: str):
    d    = trade["direction"]
    pair = trade["pair"]
    tag  = "🟢 LONG" if d == "LONG" else "🔴 SHORT"
    entry = trade.get("entry_price", "?")
    price = status.get("price", "?")

    lines = [
        f"⚠️ SETUP INVALIDATING — {tag} <b>{pair}</b>",
        f"Entry was: <b>{entry}</b>  |  Current price: <b>{price}</b>",
        "",
        f"❌ {reason}",
        "",
        "Do <b>NOT</b> enter this trade. If already in, review your SL immediately.",
        datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    ]
    _tg_post("\n".join(lines))


def send_market_update(update_data: dict):
    at_zone    = update_data.get("at_zone", [])
    approaching = update_data.get("approaching", [])
    trending   = update_data.get("trending", [])

    if not at_zone and not approaching and not trending:
        return  # nothing to report

    now = datetime.now(timezone.utc)
    session = "London" if now.hour < 12 else "New York"

    lines = [
        f"📡 <b>ZSCN Market Update — {session} Session — {now.strftime('%Y-%m-%d %H:%M UTC')}</b>",
        "",
    ]

    if at_zone:
        lines.append("🎯 <b>AT THE ZONE — waiting for 15M signal</b>")
        for s in at_zone:
            tag   = "🟢" if s["direction"] == "LONG" else "🔴"
            adx   = s.get("adx_4h", "?")
            lines.append(f"  {tag} <b>{s['pair']}</b> @ {s['price']}  |  ADX {adx}  |  S/R {s.get('nearest_sr', '?')}")
        lines.append("")

    if approaching:
        lines.append("📈 <b>APPROACHING ZONE — building up (within 1.5%)</b>")
        for s in approaching:
            tag  = "🟢" if s["direction"] == "LONG" else "🔴"
            dist = s.get("sr_dist_pct", "?")
            nr   = s.get("nearest_sr", "?")
            lines.append(f"  {tag} {s['pair']} @ {s['price']}  |  {dist}% from {nr}")
        lines.append("")

    if trending:
        lines.append("👀 <b>TRENDING — clean setup, far from zone</b>")
        longs  = [s["pair"] for s in trending if s["direction"] == "LONG"]
        shorts = [s["pair"] for s in trending if s["direction"] == "SHORT"]
        if longs:
            lines.append(f"  🟢 {', '.join(longs)}")
        if shorts:
            lines.append(f"  🔴 {', '.join(shorts)}")
        lines.append("")

    lines.append("No action needed yet — monitoring continues every 30 min.")
    _tg_post("\n".join(lines))


def check_webhook_tp_sl():
    """Poll prices for active webhook-registered setups and fire TP/SL alerts."""
    if not active_webhook_setups:
        return
    try:
        import yfinance as yf
    except ImportError:
        return

    pairs_to_close = []
    for pair, setup in list(active_webhook_setups.items()):
        sl = setup.get("sl")
        tp = setup.get("tp")
        if not sl or not tp:
            continue
        try:
            yf_symbol = pair + "=X" if len(pair) == 6 else pair
            info = yf.Ticker(yf_symbol).fast_info
            price = getattr(info, "last_price", None)
            if not price:
                continue
            d = setup.get("direction", "")
            hit = None
            if d == "LONG":
                if price >= tp:
                    hit = "WIN"
                elif price <= sl:
                    hit = "LOSS"
            elif d == "SHORT":
                if price <= tp:
                    hit = "WIN"
                elif price >= sl:
                    hit = "LOSS"
            if hit:
                pairs_to_close.append((pair, hit, price))
        except Exception as e:
            logger.warning(f"Webhook TP/SL price check error for {pair}: {e}")

    for pair, result, price in pairs_to_close:
        setup = active_webhook_setups.pop(pair, {})
        send_tp_sl_alert({
            "pair": pair,
            "direction": setup.get("direction", ""),
            "entry_price": setup.get("entry_price"),
            "sl": setup.get("sl"),
            "tp": setup.get("tp"),
            "result": result,
        })


def scheduled_scan():
    global last_scan_time
    try:
        invalidated, results, newly_resolved = run_scan(
            send_telegram, send_early_warning, send_bos_alert
        )
        last_scan_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

        for trade, status, reason in invalidated:
            send_invalidation_alert(trade, status, reason)

        for outcome in newly_resolved:
            send_tp_sl_alert(outcome)

    except Exception as e:
        logger.exception(f"Scan error: {e}")


def scheduled_weekly_summary():
    try:
        send_weekly_trade_summary()
    except Exception as e:
        logger.exception(f"Weekly summary error: {e}")


def scheduled_backtest():
    def _run():
        try:
            results = run_backtest(lookback_days=180)
            send_backtest_report(results)
        except Exception as e:
            logger.exception(f"Backtest error: {e}")
    threading.Thread(target=_run, daemon=True).start()


def scheduled_market_update():
    try:
        from monitor import pair_status as ps
        if not ps:
            return
        data = get_market_update_data(dict(ps))
        send_market_update(data)
    except Exception as e:
        logger.exception(f"Market update error: {e}")


def scheduled_weekly_bias():
    try:
        run_weekly_bias(send_weekly_bias)
    except Exception as e:
        logger.exception(f"Weekly bias error: {e}")


def scheduled_week_opener():
    try:
        run_weekly_bias(send_week_opener)
    except Exception as e:
        logger.exception(f"Week opener error: {e}")


def scheduled_friday_preview():
    try:
        run_friday_preview(send_friday_preview)
    except Exception as e:
        logger.exception(f"Friday preview error: {e}")


# ── Scheduler ─────────────────────────────────────────────────────────────────
# Continuous scan every 30 min (no session restriction)
# Sunday 11:00 UTC (6:00 AM CDT)  — morning preparation bias
# Sunday 22:00 UTC (5:00 PM CDT)  — market open confirmation
# Friday 20:00 UTC (3:00 PM CDT)  — end-of-week preview for next week
scheduler = BackgroundScheduler(timezone="UTC")
scheduler.add_job(scheduled_scan,            "interval", minutes=20, id="continuous_scan")
scheduler.add_job(check_webhook_tp_sl,       "interval", minutes=15, id="webhook_tp_sl")
scheduler.add_job(scheduled_market_update,   "cron", hour=6,  minute=0,  id="london_update")
scheduler.add_job(scheduled_market_update,   "cron", hour=13, minute=0,  id="ny_update")
scheduler.add_job(scheduled_weekly_bias,     "cron", day_of_week="sun", hour=11, minute=0,  id="weekly_bias")
scheduler.add_job(scheduled_week_opener,     "cron", day_of_week="sun", hour=22, minute=0,  id="week_opener")
scheduler.add_job(scheduled_friday_preview,  "cron", day_of_week="fri", hour=20, minute=0,  id="friday_preview")
scheduler.add_job(scheduled_weekly_summary,  "cron", day_of_week="fri", hour=21, minute=0,  id="weekly_summary")
scheduler.add_job(scheduled_backtest,        "cron", day=1,   hour=3,  minute=0,  id="monthly_backtest")
scheduler.start()
logger.info(
    "Scheduler started — "
    "every 30 min | "
    "06:00 UTC (London) | 13:00 UTC (NY) | "
    "Fri 20:00 UTC (preview) | Fri 21:00 UTC (weekly summary) | "
    "Sun 11:00 UTC (bias) | Sun 22:00 UTC (week opener) | "
    "1st of month 03:00 UTC (backtest + calibration)"
)


@app.route("/")
def index():
    return jsonify({"ok": True, "message": "ZSCN Monitor is live"})


@app.route("/health")
def health():
    return jsonify({
        "ok": True,
        "status": "running",
        "pairs_tracked": len(pair_status),
        "alerts_fired": len(recent_alerts),
        "last_scan": last_scan_time,
    })


@app.route("/dashboard")
def dashboard():
    sorted_pairs = sorted(
        pair_status.values(),
        key=lambda s: (not s.get("alert"), not s.get("ema_aligned"), s["pair"])
    )
    return render_template(
        "dashboard.html",
        pairs=sorted_pairs,
        alerts=recent_alerts,
        last_scan=last_scan_time or "Waiting for first scan…"
    )


@app.route("/api/status")
def api_status():
    return jsonify(list(pair_status.values()))


@app.route("/api/sr-levels/<pair>")
def api_sr_levels(pair):
    """
    Returns all S/R data for a pair — used by Claude Code to draw levels on TradingView.
    Daily S/R: gold solid lines (strong, 2-year lookback, pivot_n=15).
    4H S/R:    blue dashed lines (pivot_n=12 + PDH/PDL).
    """
    status = pair_status.get(pair.upper())
    if not status:
        return jsonify({"error": f"{pair.upper()} not found — trigger a scan first"}), 404
    return jsonify({
        "pair":             status.get("pair"),
        "price":            status.get("price"),
        "direction":        status.get("direction"),
        "alert_grade":      status.get("alert_grade"),
        "confluence_score": status.get("confluence_score"),
        "sr_resistance":    status.get("sr_resistance", []),
        "sr_support":       status.get("sr_support", []),
        "sr_above":         status.get("sr_above", []),
        "sr_below":         status.get("sr_below", []),
        "pdh":              status.get("pdh"),
        "pdl":              status.get("pdl"),
        "nearest_sr":       status.get("nearest_sr"),
        "is_retest":        status.get("is_retest"),
        "retest_level":     status.get("retest_level"),
        "retest_type":      status.get("retest_type"),
        "sl":               status.get("sl"),
        "tp":               status.get("tp"),
        "trendline_val":    status.get("trendline_val"),
        "at_trendline":     status.get("at_trendline"),
    })


@app.route("/api/alerts")
def api_alerts():
    return jsonify(recent_alerts)


@app.route("/api/alerts/history")
def api_alerts_history():
    """Returns all persisted alerts from disk — survives server restarts."""
    try:
        if os.path.exists(ALERTS_LOG_PATH):
            with open(ALERTS_LOG_PATH, "r") as f:
                return jsonify(json.load(f))
    except Exception as e:
        logger.error(f"Error reading alerts log: {e}")
    return jsonify([])


@app.route("/trigger-scan", methods=["POST"])
def trigger_scan():
    threading.Thread(target=scheduled_scan, daemon=True).start()
    return jsonify({"ok": True, "message": "Scan triggered"})


@app.route("/test-telegram", methods=["POST"])
def test_telegram():
    token = TELEGRAM_TOKEN
    chat  = TELEGRAM_CHAT
    if not token or not chat:
        return jsonify({"ok": False, "error": "TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set in environment variables"})
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat, "text": "✅ ZSCN Monitor — Telegram connection test successful."},
            timeout=10,
        )
        return jsonify({"ok": r.ok, "status": r.status_code, "response": r.json()})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/trigger-weekly", methods=["POST"])
def trigger_weekly():
    threading.Thread(target=scheduled_weekly_bias, daemon=True).start()
    return jsonify({"ok": True, "message": "Weekly bias triggered"})


@app.route("/trigger-friday", methods=["POST"])
def trigger_friday():
    threading.Thread(target=scheduled_friday_preview, daemon=True).start()
    return jsonify({"ok": True, "message": "Friday preview triggered"})


@app.route("/trigger-week-opener", methods=["POST"])
def trigger_week_opener():
    threading.Thread(target=scheduled_week_opener, daemon=True).start()
    return jsonify({"ok": True, "message": "Week opener triggered"})


@app.route("/trigger-weekly-summary", methods=["POST"])
def trigger_weekly_summary():
    threading.Thread(target=scheduled_weekly_summary, daemon=True).start()
    return jsonify({"ok": True, "message": "Weekly summary triggered"})


@app.route("/trigger-backtest", methods=["POST"])
def trigger_backtest():
    threading.Thread(target=scheduled_backtest, daemon=True).start()
    return jsonify({"ok": True, "message": "Backtest started — results sent to Telegram in ~3-5 min"})


@app.route("/webhook", methods=["POST"])
def webhook():
    """
    Receives JSON alerts from ZSCN Ultimate v4 via TradingView webhook.
    Pine alert() fires on stage transitions (2=BOS, 4=Confluence, 5=TradeReady, 6=Invalidated).
    Harry creates ONE alert per pair in TradingView: condition = 'Any alert() call'.
    """
    try:
        data = request.get_json(force=True)
        if not data:
            return jsonify({"ok": False, "error": "No JSON body"}), 400

        raw_pair  = str(data.get("pair", "UNKNOWN"))
        pair      = raw_pair.upper().replace("OANDA:", "").replace("FX:", "").replace("FOREXCOM:", "")
        direction = str(data.get("direction", ""))
        stage     = int(data.get("stage", 0))
        price     = float(data.get("price", 0) or 0)

        logger.info(f"Webhook: {pair} stage={stage} {direction} @ {price}")

        if stage == 2:
            fib_50  = float(data.get("fib_50",  0) or 0) or None
            fib_618 = float(data.get("fib_618", 0) or 0) or None
            send_bos_alert({
                "pair": pair, "direction": direction, "price": price,
                "bos_level": price,
                "daily_trend": direction, "h4_trend": direction, "h2_trend": direction,
                "adx_4h": "—",
                "fib_zone": {"fib_50": fib_50, "fib_618": fib_618,
                             "anchor_low": None, "anchor_high": None} if fib_50 else None,
                "fib_sr_overlap": False,
            })

        elif stage == 4:
            fib_50  = float(data.get("fib_50",  0) or 0) or None
            fib_618 = float(data.get("fib_618", 0) or 0) or None
            in_fib  = bool(data.get("in_fib", False))
            send_early_warning({
                "pair": pair, "direction": direction, "price": price,
                "confluence_score": 3, "alert_grade": "B",
                "confluence_detail": [
                    "4-TF EMA aligned (D + 4H + 2H + 1H)",
                    "4H BOS confirmed in trend direction",
                    "Price at confluence zone — waiting 15M signal",
                ],
                "pullback_zone_reached": True,
                "in_golden_zone": in_fib,
                "fib_zone": {"fib_50": fib_50, "fib_618": fib_618,
                             "anchor_low": None, "anchor_high": None} if fib_50 else None,
                "fib_sr_overlap": False,
            })

        elif stage == 5:
            sl      = float(data.get("sl",      0) or 0) or None
            tp      = float(data.get("tp",      0) or 0) or None
            fib_50  = float(data.get("fib_50",  0) or 0) or None
            fib_618 = float(data.get("fib_618", 0) or 0) or None

            active_webhook_setups[pair] = {
                "pair": pair, "direction": direction, "entry_price": price,
                "sl": sl, "tp": tp, "grade": "A+",
                "fired_at": datetime.now(timezone.utc).isoformat(),
            }

            send_telegram({
                "pair": pair, "direction": direction, "price": price,
                "confluence_score": 5, "alert_grade": "A+",
                "sl": sl, "tp": tp,
                "signal_quality": "HIGH" if sl and tp else "MEDIUM",
                "confluence_detail": [
                    "4-TF EMA fully aligned (D + 4H + 2H + 1H)",
                    "4H BOS confirmed in trend direction",
                    "Slow retest to EMA 50 zone confirmed",
                    "Price in fib 50–61.8% + S/R confluence zone",
                    "15M confirmation candle closed",
                ],
                "in_golden_zone": True,
                "fib_zone": {"fib_50": fib_50, "fib_618": fib_618,
                             "anchor_low": None, "anchor_high": None} if fib_50 else None,
                "fib_sr_overlap": False,
            })

        elif stage == 6:
            trade = active_webhook_setups.pop(pair, None)
            status = {"pair": pair, "direction": direction, "price": price}
            if trade:
                send_invalidation_alert(trade, status,
                    "ZSCN v4 stage 6 — trend flipped or BOS reversed. Do not enter.")
            else:
                _tg_post(
                    f"⚠️ <b>{pair}</b> — Setup invalidated (Stage 6).\n"
                    f"Was not in active tracking (may have been cleared already)."
                )

        return jsonify({"ok": True, "pair": pair, "stage": stage})

    except Exception as e:
        logger.exception(f"Webhook error: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/setups")
def api_setups():
    """Active Stage-5 setups registered via ZSCN v4 webhook."""
    return jsonify(list(active_webhook_setups.values()))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
