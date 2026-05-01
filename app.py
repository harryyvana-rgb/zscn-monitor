import os
import logging
import requests
from datetime import datetime, timezone
from flask import Flask, render_template, jsonify
from apscheduler.schedulers.background import BackgroundScheduler

from monitor import run_scan, pair_status, recent_alerts

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s — %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT  = os.environ.get("TELEGRAM_CHAT_ID", "")


# ── Telegram ─────────────────────────────────────────────────────────────────
def send_telegram(status: dict):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT:
        logger.warning("Telegram credentials not set")
        return

    d = status["direction"]
    arrow   = "🟢 LONG" if d == "LONG" else "🔴 SHORT"
    pair    = status["pair"]
    price   = status["price"]
    fib_50  = status.get("fib_50", "—")
    fib_618 = status.get("fib_618", "—")

    ema_d_side  = "below ✅" if d == "LONG" else "above ✅"
    ema_4h_side = f"{'below' if d == 'LONG' else 'above'} + curving {'up' if d == 'LONG' else 'down'} ✅"
    ema_2h_side = ema_4h_side
    ema_1h_side = "below ✅" if d == "LONG" else "above ✅"

    msg = (
        f"{arrow} <b>{pair}</b> — Grade A+ ⭐⭐\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"💰 Price: <b>{price}</b>\n"
        f"📍 Fib zone: <b>{fib_618} – {fib_50}</b>\n"
        f"\n"
        f"📊 EMA 50 Alignment:\n"
        f"  Daily → {ema_d_side}\n"
        f"  4H    → {ema_4h_side}\n"
        f"  2H    → {ema_2h_side}\n"
        f"  1H    → {ema_1h_side}\n"
        f"\n"
        f"📝 Price in golden fib zone — all 4 timeframes aligned\n"
        f"🕐 {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
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


# ── Scheduler ─────────────────────────────────────────────────────────────────
def scheduled_scan():
    try:
        run_scan(send_telegram)
    except Exception as e:
        logger.exception(f"Scheduled scan error: {e}")


scheduler = BackgroundScheduler(timezone="UTC")
scheduler.add_job(scheduled_scan, "interval", minutes=15, id="zscn_scan",
                  next_run_time=datetime.now(timezone.utc) + __import__("datetime").timedelta(seconds=90))
scheduler.start()
logger.info("Scheduler started — first scan in 90 seconds, then every 15 minutes")


# ── Routes ────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return jsonify({"ok": True, "message": "ZSCN Monitor is live"})


@app.route("/health")
def health():
    return jsonify({"ok": True, "status": "running",
                    "pairs_tracked": len(pair_status),
                    "alerts_fired": len(recent_alerts)})


@app.route("/dashboard")
def dashboard():
    # Sort pairs: setups first, then EMA-aligned, then rest
    sorted_pairs = sorted(
        pair_status.values(),
        key=lambda s: (not s.get("setup"), not s.get("ema_aligned"), s["pair"])
    )
    return render_template("dashboard.html",
                           pairs=sorted_pairs,
                           alerts=recent_alerts,
                           last_scan=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))


@app.route("/api/status")
def api_status():
    return jsonify(list(pair_status.values()))


@app.route("/api/alerts")
def api_alerts():
    return jsonify(recent_alerts)


@app.route("/trigger-scan", methods=["POST"])
def trigger_scan():
    """Manual scan trigger — useful for testing."""
    import threading
    threading.Thread(target=scheduled_scan).start()
    return jsonify({"ok": True, "message": "Scan triggered"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
