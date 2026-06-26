# ZSCN MCP Bridge

The bridge connects local MCP/TradingView analysis on Harry's computer to the live Render dashboard.

Flow:

```text
Local MCP / TradingView event file -> ZSCN bridge -> Render /webhook -> dashboard + Telegram
```

Render can receive webhook data, but it cannot directly reach into a private MCP server on this computer. The bridge solves that by accepting local MCP events and pushing them out to Render.

## Environment

Set these in the terminal that runs the bridge:

```powershell
$env:ZSCN_RENDER_WEBHOOK_URL = "https://zscn-monitor.onrender.com/webhook"
$env:ZSCN_WEBHOOK_SECRET = "<same value as Render TRADINGVIEW_WEBHOOK_SECRET>"
```

Optional local protection:

```powershell
$env:ZSCN_BRIDGE_TOKEN = "<local-only-token>"
```

If `ZSCN_BRIDGE_TOKEN` is set, local callers must send `X-Bridge-Token` or `?token=`.

## Test Render Connectivity

This checks Render auth and JSON shape without changing dashboard state:

```powershell
.\.venv\Scripts\python.exe .\scripts\mcp_bridge.py test
```

Expected result includes:

```json
{
  "ok": true,
  "validated_only": true
}
```

## Run The Bridge

```powershell
.\scripts\start_mcp_bridge.ps1
```

By default, the bridge watches the trade-hybrid MCP event file directly:

```text
C:\Users\harry\Documents\Codex\2026-06-13\i-want-you-to-look-at\work\trade_hybrid\tradingview-events.jsonl
```

The local MCP server can also POST JSON to:

```text
http://127.0.0.1:8788/event
```

If you open that URL in a browser, it shows a bridge status page. The real dashboard is:

```text
https://zscn-monitor.onrender.com/dashboard
```

You can also open this local shortcut, which redirects to Render:

```text
http://127.0.0.1:8788/dashboard
```

Example event:

```json
{
  "pair": "OANDA:GBPUSD",
  "direction": "SHORT",
  "stage": 5,
  "signal": "TRADE READY",
  "price": 1.2501,
  "tf_d": "BEARISH",
  "tf_4h": "BEARISH",
  "tf_2h": "BEARISH",
  "tf_1h": "BEARISH",
  "ema_4h": 1.251,
  "ema_2h": 1.2508,
  "in_fib": true,
  "fib_sr_overlap": true,
  "sl": 1.255,
  "tp": 1.235
}
```

The bridge normalizes older Pine/MCP payloads:

- `OANDA:GBPUSD` becomes `GBPUSD`
- old stage `5` + `TRADE READY` becomes Render stage `6`
- old stage `6` + `INVALIDATED` becomes Render stage `7`
- `ema_d: "above"` becomes `tf_d: "BEARISH"`
- numeric 4H/2H EMA values are converted into the EMA-stack fields

## Send One Event Manually

```powershell
.\.venv\Scripts\python.exe .\scripts\mcp_bridge.py send --json '{"pair":"GBPUSD","direction":"SHORT","stage":5,"signal":"TRADE READY","price":1.2501}'
```

Use this carefully: `send` forwards to the real Render webhook and may update the dashboard or Telegram depending on the stage.
