from __future__ import annotations

import argparse
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib import error, parse, request


DEFAULT_RENDER_WEBHOOK = "https://zscn-monitor.onrender.com/webhook"
EMA_STACK_PROXIMITY_PCT = 0.18
EMA_STACK_APPROACH_PCT = 0.45
BRIDGE_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EVENT_LOG = BRIDGE_ROOT / "data" / "mcp_bridge_events.jsonl"


def _as_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        text = str(value).strip()
        if not text:
            return None
        return float(text)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


def _as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in {"true", "1", "yes", "y", "on"}:
        return True
    if text in {"false", "0", "no", "n", "off"}:
        return False
    return None


def _pct_distance(a: float | None, b: float | None) -> float | None:
    if a is None or b is None or b == 0:
        return None
    return round(abs(a - b) / abs(b) * 100, 3)


def normalize_pair(value: Any) -> str:
    pair = str(value or "UNKNOWN").strip().upper()
    for prefix in ("OANDA:", "FX:", "FOREXCOM:", "FX_IDC:", "TVC:", "BINANCE:"):
        pair = pair.replace(prefix, "")
    return pair.replace("/", "").replace("-", "").replace("_", "")


def normalize_direction(value: Any) -> str:
    text = str(value or "").strip().upper()
    if text in {"LONG", "BUY", "BULL", "BULLISH", "UP"}:
        return "LONG"
    if text in {"SHORT", "SELL", "BEAR", "BEARISH", "DOWN"}:
        return "SHORT"
    return "NONE"


def normalize_tf(value: Any) -> str | None:
    text = str(value or "").strip().lower()
    if not text:
        return None
    if "bull" in text or "below" in text or text == "up":
        return "BULLISH"
    if "bear" in text or "above" in text or text == "down":
        return "BEARISH"
    if "flat" in text or "mixed" in text:
        return "FLAT"
    return None


def normalize_slope(value: Any) -> str | None:
    text = str(value or "").strip().lower()
    if "curving up" in text or "rising" in text or text == "up":
        return "UP"
    if "curving down" in text or "falling" in text or text == "down":
        return "DOWN"
    return None


def infer_stage(raw_stage: Any, event_text: str, signal_text: str) -> int:
    text = f"{event_text} {signal_text}".lower().replace("_", " ")
    stage = _as_int(raw_stage)

    # Old Pine scripts used stage 5 for trade ready and stage 6 for invalidated.
    # The Render command center uses stage 6 for trade ready and stage 7 invalidated.
    if "invalid" in text or "cancel" in text:
        return 7
    if "trade ready" in text or "take trade" in text:
        return 6
    if stage is not None:
        return stage
    if "confluence" in text or "golden" in text or "zone" in text:
        return 5
    if "bos" in text or "break of structure" in text:
        return 2
    return 0


def _copy_numeric(payload: dict[str, Any], raw: dict[str, Any], key: str, aliases: tuple[str, ...] = ()) -> None:
    for source_key in (key, *aliases):
        value = _as_float(raw.get(source_key))
        if value is not None:
            payload[key] = round(value, 5)
            return


def _copy_bool(payload: dict[str, Any], raw: dict[str, Any], key: str, aliases: tuple[str, ...] = ()) -> None:
    for source_key in (key, *aliases):
        value = _as_bool(raw.get(source_key))
        if value is not None:
            payload[key] = value
            return


def add_ema_stack(payload: dict[str, Any]) -> None:
    ema_4h = _as_float(payload.get("ema_4h"))
    ema_2h = _as_float(payload.get("ema_2h"))
    price = _as_float(payload.get("price"))
    if ema_4h is None or ema_2h is None:
        return

    level = round((ema_4h + ema_2h) / 2, 5)
    stack_distance = _pct_distance(ema_4h, ema_2h)
    price_distance = _pct_distance(price, level)
    tf_4h = payload.get("tf_4h")
    tf_2h = payload.get("tf_2h")
    same_side = tf_4h == tf_2h and tf_4h in {"BULLISH", "BEARISH"}
    aligned = stack_distance is not None and stack_distance <= EMA_STACK_PROXIMITY_PCT and same_side
    high_conviction = (
        aligned
        and price_distance is not None
        and price_distance <= EMA_STACK_APPROACH_PCT
    )

    payload.setdefault("ema_stack_level", level)
    payload.setdefault("ema_stack_distance_pct", stack_distance)
    payload.setdefault("ema_stack_price_distance_pct", price_distance)
    payload.setdefault("ema_stack_side", tf_4h if same_side else "MIXED")
    payload.setdefault("ema_stack_aligned", aligned)
    payload.setdefault("ema_stack_high_conviction", high_conviction)


def normalize_event(raw: dict[str, Any]) -> dict[str, Any]:
    event_text = str(raw.get("event") or raw.get("signal") or raw.get("alert") or "").strip()
    signal_text = str(raw.get("signal") or raw.get("setup") or "").strip()
    pair = raw.get("pair") or raw.get("symbol") or raw.get("ticker") or raw.get("tickerid")

    payload: dict[str, Any] = {
        "pair": normalize_pair(pair),
        "direction": normalize_direction(raw.get("direction") or raw.get("bias") or raw.get("side")),
        "stage": infer_stage(raw.get("stage"), event_text, signal_text),
        "event": event_text.lower().replace(" ", "_") or "mcp_event",
        "source": "mcp_bridge",
    }

    price = _as_float(raw.get("price") or raw.get("close") or raw.get("last") or raw.get("current_price"))
    if price is not None:
        payload["price"] = round(price, 5)

    # Timeframe direction can arrive directly as tf_* or indirectly as old EMA text.
    tf_sources = {
        "tf_d": ("tf_d", "daily_tf", "ema_d"),
        "tf_4h": ("tf_4h", "h4_tf", "ema_4h_text", "ema_4h"),
        "tf_2h": ("tf_2h", "h2_tf", "ema_2h_text", "ema_2h"),
        "tf_1h": ("tf_1h", "h1_tf", "ema_1h_text", "ema_1h"),
    }
    for target, sources in tf_sources.items():
        for source in sources:
            tf = normalize_tf(raw.get(source))
            if tf:
                payload[target] = tf
                break

    slope_4h = normalize_slope(raw.get("ema_4h_slope") or raw.get("ema_4h"))
    slope_2h = normalize_slope(raw.get("ema_2h_slope") or raw.get("ema_2h"))
    if slope_4h:
        payload["ema_4h_slope"] = slope_4h
    if slope_2h:
        payload["ema_2h_slope"] = slope_2h

    for key in ("ema_d", "ema_4h", "ema_2h", "ema_1h"):
        _copy_numeric(payload, raw, key)

    _copy_numeric(payload, raw, "ema_stack_level")
    _copy_numeric(payload, raw, "ema_stack_distance_pct")
    _copy_numeric(payload, raw, "ema_stack_price_distance_pct")
    _copy_numeric(payload, raw, "nearest_sr", ("sr_level", "support_resistance"))
    _copy_numeric(payload, raw, "sr_dist_pct")
    _copy_numeric(payload, raw, "fib_50")
    _copy_numeric(payload, raw, "fib_618")
    _copy_numeric(payload, raw, "fib_786")
    _copy_numeric(payload, raw, "sl")
    _copy_numeric(payload, raw, "tp")
    _copy_numeric(payload, raw, "strategy_score")

    for key in (
        "ema_stack_aligned",
        "ema_stack_high_conviction",
        "trends_agree",
        "bos_detected",
        "in_golden_zone",
        "fib_sr_overlap",
        "has_signal",
        "pullback_valid",
    ):
        aliases = ("in_fib",) if key == "in_golden_zone" else ()
        _copy_bool(payload, raw, key, aliases)

    for key in (
        "daily_trend",
        "h4_trend",
        "h2_trend",
        "signal_15m",
        "signal_quality",
        "strategy_stage_label",
        "strategy_action",
        "strategy_grade",
        "pullback_depth",
    ):
        if raw.get(key) is not None:
            payload[key] = raw[key]

    add_ema_stack(payload)
    return payload


def render_url() -> str:
    return os.environ.get("ZSCN_RENDER_WEBHOOK_URL", DEFAULT_RENDER_WEBHOOK)


def render_secret() -> str:
    return os.environ.get("ZSCN_WEBHOOK_SECRET") or os.environ.get("TRADINGVIEW_WEBHOOK_SECRET", "")


def event_log_path() -> Path:
    return Path(os.environ.get("ZSCN_BRIDGE_EVENT_LOG", str(DEFAULT_EVENT_LOG)))


def append_event_log(payload: dict[str, Any], result: dict[str, Any]) -> None:
    path = event_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "received_at": datetime.now(timezone.utc).isoformat(),
        "pair": payload.get("pair"),
        "stage": payload.get("stage"),
        "event": payload.get("event"),
        "direction": payload.get("direction"),
        "render_status": result.get("status"),
        "ok": result.get("status", 500) < 400,
    }
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, separators=(",", ":")) + "\n")


def read_event_log(limit: int = 20) -> list[dict[str, Any]]:
    path = event_log_path()
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()[-limit:]
        return [json.loads(line) for line in lines if line.strip()]
    except Exception:
        return []


def validate_url(url: str) -> str:
    parsed = parse.urlparse(url)
    path = parsed.path.rstrip("/")
    if path.endswith("/webhook"):
        path = f"{path}/validate"
    else:
        path = f"{path}/webhook/validate"
    return parse.urlunparse(parsed._replace(path=path))


def post_json(url: str, payload: dict[str, Any], secret: str = "") -> dict[str, Any]:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if secret:
        headers["X-Trade-Secret"] = secret
    req = request.Request(url, data=body, headers=headers, method="POST")
    try:
        with request.urlopen(req, timeout=20) as response:
            text = response.read().decode("utf-8")
            return {"status": response.status, "body": json.loads(text) if text else {}}
    except error.HTTPError as exc:
        text = exc.read().decode("utf-8", errors="replace")
        try:
            body_json = json.loads(text)
        except json.JSONDecodeError:
            body_json = {"raw": text}
        return {"status": exc.code, "body": body_json}


def load_event(args: argparse.Namespace) -> dict[str, Any]:
    if args.file:
        with open(args.file, "r", encoding="utf-8") as f:
            return json.load(f)
    if args.json:
        return json.loads(args.json)
    raise SystemExit("Provide --json or --file")


class BridgeHandler(BaseHTTPRequestHandler):
    server_version = "ZSCNMCPBridge/1.0"

    def _send_html(self, status: int, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        token = os.environ.get("ZSCN_BRIDGE_TOKEN", "")
        if not token:
            return True
        query = parse.parse_qs(parse.urlparse(self.path).query)
        provided = self.headers.get("X-Bridge-Token", "") or (query.get("token", [""])[0])
        return provided == token

    def _status_page(self) -> str:
        dashboard_url = "https://zscn-monitor.onrender.com/dashboard"
        events = read_event_log(limit=1)
        last_event = events[-1] if events else None
        last_text = (
            f"Last MCP post: {last_event.get('pair')} stage {last_event.get('stage')} at {last_event.get('received_at')}"
            if last_event else
            "No MCP posts received by this bridge yet."
        )
        return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>ZSCN MCP Bridge</title>
  <style>
    body {{ margin: 0; min-height: 100vh; display: grid; place-items: center; background: #0d1117; color: #e6edf3; font-family: Segoe UI, Arial, sans-serif; }}
    main {{ width: min(720px, calc(100vw - 32px)); background: #161b22; border: 1px solid #30363d; border-radius: 14px; padding: 28px; box-shadow: 0 18px 50px rgba(0,0,0,.28); }}
    h1 {{ margin: 0 0 8px; font-size: 26px; }}
    p {{ color: #8b949e; line-height: 1.55; }}
    code {{ background: #0d1117; border: 1px solid #30363d; border-radius: 6px; padding: 2px 6px; color: #79c0ff; }}
    .ok {{ display: inline-block; background: rgba(63,185,80,.14); color: #3fb950; border: 1px solid rgba(63,185,80,.35); border-radius: 999px; padding: 4px 10px; font-weight: 700; font-size: 12px; }}
    .actions {{ display: flex; flex-wrap: wrap; gap: 10px; margin-top: 18px; }}
    a {{ color: #fff; background: #238636; text-decoration: none; padding: 10px 14px; border-radius: 8px; font-weight: 700; }}
    a.secondary {{ background: #21262d; color: #e6edf3; border: 1px solid #30363d; }}
  </style>
</head>
<body>
  <main>
    <span class="ok">Bridge running</span>
    <h1>ZSCN MCP Bridge</h1>
    <p>This local page is not the trading dashboard. It is the intake bridge that receives MCP/TradingView events from your computer and forwards them to Render.</p>
    <p>MCP should POST live events to: <code>http://127.0.0.1:8788/event</code></p>
    <p>The real live dashboard is on Render.</p>
    <p><strong>{last_text}</strong></p>
    <div class="actions">
      <a href="{dashboard_url}">Open ZSCN Dashboard</a>
      <a class="secondary" href="/health">Bridge Health JSON</a>
      <a class="secondary" href="/stats">Bridge Activity JSON</a>
    </div>
  </main>
</body>
</html>"""

    def do_GET(self) -> None:
        path = parse.urlparse(self.path).path
        if path == "/health":
            self._send_json(200, {"ok": True, "service": "zscn-mcp-bridge"})
            return
        if path == "/stats":
            events = read_event_log(limit=20)
            self._send_json(200, {
                "ok": True,
                "events_received": len(events),
                "last_event": events[-1] if events else None,
                "recent": events,
            })
            return
        if path == "/dashboard":
            self.send_response(302)
            self.send_header("Location", "https://zscn-monitor.onrender.com/dashboard")
            self.end_headers()
            return
        if path in {"/", "/event", "/webhook", "/mcp-event"}:
            self._send_html(200, self._status_page())
            return
        self._send_json(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:
        path = parse.urlparse(self.path).path
        if path not in {"/event", "/webhook", "/mcp-event"}:
            self._send_json(404, {"ok": False, "error": "not found"})
            return
        if not self._authorized():
            self._send_json(401, {"ok": False, "error": "unauthorized"})
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw_body = self.rfile.read(length).decode("utf-8")
            raw_event = json.loads(raw_body)
            payload = normalize_event(raw_event)
            result = post_json(render_url(), payload, render_secret())
            append_event_log(payload, result)
            self._send_json(200, {"ok": result["status"] < 400, "forwarded": payload, "render": result})
        except Exception as exc:
            self._send_json(500, {"ok": False, "error": str(exc)})


def run_server(args: argparse.Namespace) -> None:
    host = args.host or os.environ.get("ZSCN_BRIDGE_HOST", "127.0.0.1")
    port = args.port or int(os.environ.get("ZSCN_BRIDGE_PORT", "8788"))
    server = ThreadingHTTPServer((host, port), BridgeHandler)
    print(f"ZSCN MCP bridge listening on http://{host}:{port}/event")
    print(f"Forwarding to {render_url()}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nBridge stopped")


def cmd_send(args: argparse.Namespace) -> int:
    payload = normalize_event(load_event(args))
    result = post_json(render_url(), payload, render_secret())
    print(json.dumps({"sent": payload, "render": result}, indent=2))
    return 0 if result["status"] < 400 else 1


def cmd_test(args: argparse.Namespace) -> int:
    raw = {
        "pair": args.pair,
        "direction": "NONE",
        "stage": 0,
        "event": "bridge_test",
        "price": args.price,
    }
    payload = normalize_event(raw)
    result = post_json(validate_url(render_url()), payload, render_secret())
    print(json.dumps({"validated": payload, "render": result}, indent=2))
    return 0 if result["status"] < 400 else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Forward local MCP/TradingView events to ZSCN Render.")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="Run a local HTTP bridge for MCP events.")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    serve.set_defaults(func=lambda args: (run_server(args), 0)[1])

    send = sub.add_parser("send", help="Normalize and forward one event.")
    send.add_argument("--json")
    send.add_argument("--file")
    send.set_defaults(func=cmd_send)

    test = sub.add_parser("test", help="Validate Render connectivity without changing dashboard state.")
    test.add_argument("--pair", default="EURUSD")
    test.add_argument("--price", default="1.0")
    test.set_defaults(func=cmd_test)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
