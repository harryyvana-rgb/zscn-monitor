# ZSCN Monitor — Setup Guide

## What this does
- Watches all 28 pairs 24/7 via TradingView cloud alerts
- Sends Telegram message the moment your golden zone setup is detected
- Live dashboard at your Render URL showing all recent alerts

---

## STEP 1 — Push to GitHub

1. Go to github.com → New Repository → name it `zscn-monitor` → Create
2. In terminal, run:
```
cd C:\Users\harry\trading\zscn-monitor
git init
git add .
git commit -m "ZSCN monitor initial setup"
git remote add origin https://github.com/YOUR_USERNAME/zscn-monitor.git
git push -u origin main
```

---

## STEP 2 — Deploy to Render

1. Go to render.com → Sign in with GitHub
2. New → Web Service → Connect your `zscn-monitor` repo
3. Settings:
   - **Runtime**: Python
   - **Build command**: `pip install -r requirements.txt`
   - **Start command**: `gunicorn app:app`
4. Add environment variables (click "Environment"):
   - `TELEGRAM_BOT_TOKEN` = your Telegram bot token
   - `TELEGRAM_CHAT_ID` = your Telegram chat ID
   - `TRADINGVIEW_WEBHOOK_SECRET` = a long random value
5. Click **Deploy**
6. Wait ~2 minutes. Your URL will be: `https://zscn-monitor.onrender.com`

Test it works:
- Visit `https://zscn-monitor.onrender.com/health` → should say `{"ok": true}`
- Visit `https://zscn-monitor.onrender.com/dashboard` → your live dashboard

---

## STEP 3 — Add Pine Script to TradingView

1. Open TradingView Desktop
2. Open Pine Editor → paste the contents of `ZSCN_Alert_Monitor.pine`
3. Save it as "ZSCN Alert Monitor"
4. Add it to your chart on the first pair (e.g. EURUSD on 4H)

---

## STEP 4 — Create TradingView Alerts (do this for each pair)

For each of your 28 pairs:
1. Make sure `ZSCN Alert Monitor` is on the chart
2. Click the **Alerts** bell → Create Alert
3. **Condition**: ZSCN Alert Monitor → "ZSCN LONG Setup"
4. **Message**: leave as is (the JSON is auto-filled from the Pine Script)
5. **Notifications**: check "Webhook URL" → paste:
   ```
   https://zscn-monitor.onrender.com/webhook?secret=YOUR_TRADINGVIEW_WEBHOOK_SECRET
   ```
6. **Expiration**: set to max (1 month, then renew)
7. Repeat for "ZSCN SHORT Setup" on the same pair
8. Switch to the next pair and repeat

> **Tip:** Do EURUSD first, test it, make sure Telegram fires — then do the rest.

---

## What you get in Telegram

When a setup triggers:
```
🟢 LONG GBPNZD — Grade A+ ⭐⭐
━━━━━━━━━━━━━━━━
💰 Price: 2.30168
📍 Fib zone: 50-61.8% retracement

📊 EMA 50 Alignment:
  Daily  → below
  4H     → below curving up
  2H     → below curving up
  1H     → below

📝 Price entered golden zone — 4-TF EMA fully aligned bullish
🕐 2026-05-01 14:00 UTC
```

---

## Dashboard
Visit `https://zscn-monitor.onrender.com/dashboard` anytime to see all recent alerts in a clean dark UI. Auto-refreshes every 60 seconds.

---

## Important Notes
- **Free Render tier spins down after 15 min of inactivity** — the webhook will still fire but the server takes ~30 seconds to wake up on the first hit. To keep it always warm, use UptimeRobot (free) to ping `/health` every 5 minutes.
- TradingView free plan: 2 alerts max. You need **Pro** ($15/mo) for more alerts, or **Pro+** for unlimited. With 28 pairs × 2 directions = 56 alerts, you need Pro+.
- Alerts expire after 1 month on Pro — just renew them.
