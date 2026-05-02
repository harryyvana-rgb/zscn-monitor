import os
import logging
import threading
import requests
from datetime import datetime, timezone, timedelta
from flask import Flask, render_template, jsonify
from apscheduler.schedulers.background import BackgroundScheduler

from monitor import run_scan, run_weekly_bias, pair_status, recent_alerts

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


def send_telegram(status: dict):
    d     = status["direction"]
    pair  = status["pair"]
    price = status["price"]
    score = status["confluence_score"]
    tag   = "🟢 LONG" if d == "LONG" else "🔴 SHORT"

    lines = [
        f"{tag} — <b>{pair}</b>",
        f"Price: <b>{price}</b>   |   Confluences: <b>{score}/4</b>",
        "",
    ]

    for item in status.get("confluence_detail", []):
        if any(kw in item for kw in ("AT ZONE", "agree", "CONFIRMED")):
            tick = "✅"
        elif "BONUS" in item:
            tick = "⭐"
        else:
            tick = "✅"
        lines.append(f"{tick} {item}")

    # S/R map
    above = status.get("sr_above", [])
    below = status.get("sr_below", [])
    pdh   = status.get("pdh")
    pdl   = status.get("pdl")

    lines.append("")
    lines.append("📊 <b>Key Levels</b>")

    if pdh:
        lines.append(f"  PDH: {pdh}")
    if pdl:
        lines.append(f"  PDL: {pdl}")

    if above:
        lines.append(f"  Resistance above: {' | '.join(str(l) for l in above)}")
    if below:
        lines.append(f"  Support below:    {' | '.join(str(l) for l in below)}")

    if status.get("at_trendline"):
        lines.append(f"  ⭐ Trend line at: {status['trendline_val']}")

    lines += [
        "",
        "👉 Check your fib zone on the chart.",
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

    lines.append("Good luck this week. Only trade the 4/4 setups. 💪")
    _tg_post("\n".join(lines))


def scheduled_scan():
    global last_scan_time
    try:
        run_scan(send_telegram)
        last_scan_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except Exception as e:
        logger.exception(f"Scan error: {e}")


def scheduled_weekly_bias():
    try:
        run_weekly_bias(send_weekly_bias)
    except Exception as e:
        logger.exception(f"Weekly bias error: {e}")


# Scans: 07:00 UTC pre-London, 12:30 UTC pre-NY
# Weekly bias: Sunday 11:00 UTC (6:00 AM CDT)
scheduler = BackgroundScheduler(timezone="UTC")
scheduler.add_job(scheduled_scan,         "cron", hour=7,  minute=0,  id="pre_london")
scheduler.add_job(scheduled_scan,         "cron", hour=12, minute=30, id="pre_newyork")
scheduler.add_job(scheduled_weekly_bias,  "cron", day_of_week="sun", hour=11, minute=0, id="weekly_bias")
scheduler.start()
logger.info("Scheduler started — 07:00 UTC, 12:30 UTC daily | Sunday 11:00 UTC weekly bias")


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


@app.route("/api/alerts")
def api_alerts():
    return jsonify(recent_alerts)


@app.route("/trigger-scan", methods=["POST"])
def trigger_scan():
    threading.Thread(target=scheduled_scan, daemon=True).start()
    return jsonify({"ok": True, "message": "Scan triggered"})


@app.route("/trigger-weekly", methods=["POST"])
def trigger_weekly():
    threading.Thread(target=scheduled_weekly_bias, daemon=True).start()
    return jsonify({"ok": True, "message": "Weekly bias triggered"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
