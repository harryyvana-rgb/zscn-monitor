import os
import logging
import threading
import requests
from datetime import datetime, timezone, timedelta
from flask import Flask, render_template, jsonify
from apscheduler.schedulers.background import BackgroundScheduler

from monitor import run_scan, pair_status, recent_alerts

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
    side  = "below" if d == "LONG" else "above"
    curve = "up" if d == "LONG" else "down"
    tag   = "[LONG]" if d == "LONG" else "[SHORT]"

    msg = (
        f"{tag} <b>{pair}</b> - EMA Aligned\n"
        f"Price: <b>{price}</b>\n\n"
        f"EMA 50 on all 4 timeframes:\n"
        f"  Daily  : {side}\n"
        f"  4H     : {side} + curving {curve}\n"
        f"  2H     : {side} + curving {curve}\n"
        f"  1H     : {side}\n\n"
        f"Go check your fib zone on the chart.\n"
        f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
    )

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
    try:
        run_scan(send_telegram)
    except Exception as e:
        logger.exception(f"Scan error: {e}")


# Start scheduler — first scan after 2 minutes, then every 15 minutes
scheduler = BackgroundScheduler(timezone="UTC")
scheduler.add_job(
    scheduled_scan,
    "interval",
    minutes=15,
    id="zscn_scan",
    next_run_time=datetime.now(timezone.utc) + timedelta(minutes=2)
)
scheduler.start()
logger.info("Scheduler started - first scan in 2 minutes")


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
        key=lambda s: (not s.get("setup"), not s.get("ema_aligned"), s["pair"])
    )
    return render_template(
        "dashboard.html",
        pairs=sorted_pairs,
        alerts=recent_alerts,
        last_scan=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
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
