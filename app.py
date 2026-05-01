import os
import logging
import threading
import requests
from datetime import datetime, timezone, timedelta
from flask import Flask, render_template, jsonify
from apscheduler.schedulers.background import BackgroundScheduler

from monitor import run_scan, pair_status, recent_alerts

last_scan_time = None

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s - %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT  = os.environ.get("TELEGRAM_CHAT_ID", "")


def send_telegram(status: dict):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT:
        logger.warning("Telegram credentials not set")
        return

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
        tick = "✅" if any(kw in item for kw in ("AT ZONE", "agree", "CONFIRMED", "below", "above")) else "⬜"
        lines.append(f"{tick} {item}")

    lines += [
        "",
        "👉 Check your fib zone on the chart.",
        datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    ]

    msg = "\n".join(lines)

    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={
            "chat_id": TELEGRAM_CHAT,
            "text": msg,
            "parse_mode": "HTML"
        }, timeout=10)
    except Exception as e:
        logger.error(f"Telegram error: {e}")


def scheduled_scan():
    global last_scan_time
    try:
        run_scan(send_telegram)
        last_scan_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except Exception as e:
        logger.exception(f"Scan error: {e}")


# Two daily scans: 07:00 UTC (pre-London) and 12:30 UTC (pre-New York)
scheduler = BackgroundScheduler(timezone="UTC")
scheduler.add_job(scheduled_scan, "cron", hour=7,  minute=0,  id="pre_london")
scheduler.add_job(scheduled_scan, "cron", hour=12, minute=30, id="pre_newyork")
scheduler.start()
logger.info("Scheduler started — scans at 07:00 UTC (pre-London) and 12:30 UTC (pre-NY)")


@app.route("/")
def index():
    return jsonify({"ok": True, "message": "ZSCN Monitor is live"})


@app.route("/health")
def health():
    return jsonify({
        "ok": True,
        "status": "running",
        "pairs_tracked": len(pair_status),
        "alerts_fired": len(recent_alerts)
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


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
