import json
import os
import logging
import threading
import requests
from datetime import datetime, timezone, timedelta
from flask import Flask, render_template, jsonify
from apscheduler.schedulers.background import BackgroundScheduler

from monitor import (run_scan, run_weekly_bias, run_friday_preview,
                     pair_status, recent_alerts, ALERTS_LOG_PATH,
                     get_win_rate, get_market_update_data)

last_scan_time = None

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
        for lvl in status.get("sr_daily", []):
            if abs(lvl["price"] - nearest) / nearest * 100 < 0.25 and lvl.get("touches", 0) >= 3:
                return f"Daily S/R zone with {lvl['touches']} historical touches — heavily respected level."

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


def send_early_warning(status: dict):
    d     = status["direction"]
    pair  = status["pair"]
    price = status["price"]
    score = status["confluence_score"]
    grade = status.get("alert_grade", "WATCH")
    tag   = "🟢 LONG" if d == "LONG" else "🔴 SHORT"

    if status.get("is_retest"):
        header = f"⚠️ BREAK+RETEST FORMING — {tag} <b>{pair}</b>"
    else:
        header = f"⚠️ EARLY WARNING — {tag} <b>{pair}</b>"

    narrative = _setup_narrative(status)
    lines = [
        header,
        f"Grade: <b>{grade}</b>   |   Price: <b>{price}</b>   |   Confluences: <b>{score}/4</b>",
        f"<i>{narrative}</i>",
        "",
    ]
    lines += _build_alert_body(status, is_early=True)
    lines += [
        "",
        _closing_cta(status, is_early=True),
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


def scheduled_scan():
    global last_scan_time
    try:
        invalidated, results = run_scan(send_telegram, send_early_warning)
        last_scan_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

        # Send invalidation alerts
        for trade, status, reason in invalidated:
            send_invalidation_alert(trade, status, reason)

    except Exception as e:
        logger.exception(f"Scan error: {e}")


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
scheduler.add_job(scheduled_scan,           "interval", minutes=30, id="continuous_scan")
scheduler.add_job(scheduled_market_update,  "cron", hour=6,  minute=0,  id="london_update")
scheduler.add_job(scheduled_market_update,  "cron", hour=13, minute=0,  id="ny_update")
scheduler.add_job(scheduled_weekly_bias,    "cron", day_of_week="sun", hour=11, minute=0,  id="weekly_bias")
scheduler.add_job(scheduled_week_opener,    "cron", day_of_week="sun", hour=22, minute=0,  id="week_opener")
scheduler.add_job(scheduled_friday_preview, "cron", day_of_week="fri", hour=20, minute=0,  id="friday_preview")
scheduler.start()
logger.info(
    "Scheduler started — "
    "every 30 min | "
    "06:00 UTC (London update) | "
    "13:00 UTC (NY update) | "
    "Sun 11:00 UTC (bias) | "
    "Sun 22:00 UTC (week opener) | "
    "Fri 20:00 UTC (Friday preview)"
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
        "sr_daily":         status.get("sr_daily", []),
        "sr_4h":            status.get("sr_4h", []),
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


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
