#!/usr/bin/env python3
"""
Probe the Robinhood Legend DXLink streaming stack end-to-end.

Flow:
  1) GET /marketdata/token/v1/?session_id=<uuid>&session_type=blackwidow  (with X-Robinhood-Client-Platform: black-widow)
     → { token, wss_url, expiration, ttl_ms, dxfeed_id }
  2) WebSocket to wss_url with Sec-WebSocket-Protocol: bearer, <our OAuth JWT>
  3) Send SETUP → AUTH (token from step 1) → MD_SETUP
  4) Wait for AUTH_STATE: AUTHORIZED
  5) CHANNEL_REQUEST (service=FEED) → CHANNEL_OPENED
  6) FEED_SETUP → FEED_SUBSCRIPTION → collect FEED_DATA

Usage: python3 scripts/dxlink_probe.py [symbol] [event_type] [duration_sec]
"""
import json
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import robin_stocks.robinhood as r  # noqa
import rh_wrapper  # noqa
import websocket


def get_streaming_token():
    """Fetch a short-lived DXLink auth token + the WSS URL."""
    session_id = str(uuid.uuid4())
    resp = r.helper.SESSION.get(
        "https://api.robinhood.com/marketdata/token/v1/",
        params={"session_id": session_id, "session_type": "blackwidow"},
        headers={
            "Origin": "https://robinhood.com",
            "Referer": "https://robinhood.com/",
            "X-Robinhood-Client-Platform": "black-widow",
        },
        timeout=10,
    )
    resp.raise_for_status()
    body = resp.json()
    data = body.get("data", {}).get("data", {})
    return data["token"], data["wss_url"]


def probe(symbol: str, event_type: str, duration: float = 8.0):
    rh_wrapper.maybe_login(reason="dxlink-probe")
    oauth = r.helper.SESSION.headers.get("Authorization", "")
    if oauth.startswith("Bearer "):
        oauth = oauth[7:]
    if not oauth:
        print("No OAuth token on session."); return

    print("Fetching DXLink streaming token…")
    dx_token, wss_url = get_streaming_token()
    print(f"  token: {dx_token[:40]}… ({len(dx_token)} chars)")
    print(f"  wss:   {wss_url}")

    print(f"\nConnecting to {wss_url}")
    ws = websocket.create_connection(
        wss_url,
        header=["Origin: https://robinhood.com"],
        subprotocols=["bearer", oauth],
        timeout=10,
    )
    print("Connected.")

    # 1) SETUP
    ws.send(json.dumps({
        "type": "SETUP", "channel": 0,
        "keepaliveTimeout": 60, "acceptKeepaliveTimeout": 60,
        "version": "0.1-DXF-JS/0.5.1",
    }))
    # 2) AUTH with DXLink ticket
    ws.send(json.dumps({"type": "AUTH", "channel": 0, "token": dx_token}))
    # 3) Robinhood-specific MD_SETUP
    ws.send(json.dumps({
        "type": "MD_SETUP",
        "enable_heartbeat_timestamp": True,
        "enable_logging_raw_incoming_message": False,
        "enable_subscription_debugging": False,
    }))

    # Wait for AUTHORIZED
    authorized = False
    ws.settimeout(5)
    while not authorized:
        raw = ws.recv()
        msg = json.loads(raw)
        t = msg.get("type")
        if t == "AUTH_STATE":
            state = msg.get("state")
            print(f"  AUTH_STATE: {state}")
            if state == "AUTHORIZED":
                authorized = True
            elif state == "UNAUTHORIZED" and msg.get("channel") == 0:
                # server's initial state before processing our AUTH
                continue
            else:
                print(f"  Auth rejected."); ws.close(); return
        elif t == "SETUP":
            print(f"  Server SETUP received (version {msg.get('version')})")

    # 4) CHANNEL_REQUEST
    ws.send(json.dumps({
        "type": "CHANNEL_REQUEST", "channel": 3,
        "service": "FEED", "parameters": {"contract": "AUTO"},
    }))
    # Wait for CHANNEL_OPENED
    opened = False
    while not opened:
        raw = ws.recv()
        msg = json.loads(raw)
        if msg.get("type") == "CHANNEL_OPENED" and msg.get("channel") == 3:
            opened = True
            print(f"  CHANNEL_OPENED (service=FEED)")

    # 5) FEED_SETUP
    ws.send(json.dumps({
        "type": "FEED_SETUP", "channel": 3,
        "acceptAggregationPeriod": 0.25,
        "acceptDataFormat": "FULL",  # Legend uses FULL (named fields)
        "acceptEventFields": {
            "Quote": ["eventSymbol", "bidPrice", "askPrice", "bidSize", "askSize", "bidTime", "askTime"],
            "Trade": ["eventSymbol", "price", "size", "time", "dayVolume"],
            "Candle": ["eventSymbol", "eventTime", "open", "high", "low", "close", "volume", "time"],
            "Order": ["eventSymbol", "eventFlags", "side", "price", "size", "index", "time", "sequence"],
            "Summary": ["eventSymbol", "dayOpenPrice", "dayHighPrice", "dayLowPrice", "dayClosePrice", "prevDayClosePrice"],
        },
    }))

    # 6) FEED_SUBSCRIPTION
    sub = {"type": event_type, "symbol": symbol}
    # For Candle, include fromTime (24h ago) as Legend does
    if event_type == "Candle":
        sub["fromTime"] = int((time.time() - 86400) * 1000)
        sub["instrumentType"] = "futures_product"
    ws.send(json.dumps({
        "type": "FEED_SUBSCRIPTION", "channel": 3,
        "reset": True, "add": [sub],
    }))
    print(f"\nSubscribed {event_type}:{symbol}. Streaming {duration}s…\n")

    start = time.time()
    count = 0
    while time.time() - start < duration:
        try:
            ws.settimeout(1.0)
            raw = ws.recv()
        except Exception:
            continue
        try:
            msg = json.loads(raw)
        except Exception:
            continue
        t = msg.get("type")
        if t == "FEED_DATA":
            count += 1
            if count <= 5:
                print(f"[DATA #{count}] {json.dumps(msg)[:400]}")
        elif t == "KEEPALIVE":
            continue
        elif t == "FEED_CONFIG":
            print(f"  FEED_CONFIG: {json.dumps(msg)[:200]}")
        else:
            print(f"[{t}] {json.dumps(msg)[:200]}")

    print(f"\n=== Total FEED_DATA events received: {count} ===")
    ws.close()


if __name__ == "__main__":
    symbol = sys.argv[1] if len(sys.argv) > 1 else "/ESM26:XCME"
    event = sys.argv[2] if len(sys.argv) > 2 else "Quote"
    dur = float(sys.argv[3]) if len(sys.argv) > 3 else 8.0
    probe(symbol, event, dur)
