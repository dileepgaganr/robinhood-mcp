import robin_stocks.robinhood as r
import os
import sys
import io
import json
import signal
import time
import uuid
import re
import datetime
import pyotp
import websocket
from dotenv import load_dotenv

load_dotenv()

# ── Login rate limiting ──────────────────────────────────────────────
# Without this, a single expired-session moment would trigger a fresh
# login() call for every command that arrives during the recovery window
# — each one hitting Robinhood's auth endpoint and (if the user is on
# credential + device-approval flow) spamming their phone with multiple
# approval notifications.
#
# _LOGIN_COOLDOWN_SEC is the minimum gap between two login attempts.
# _LOGIN_FAIL_COOLDOWN_SEC kicks in if the previous login failed, so we
# fail fast on subsequent commands instead of retrying every few ms.
_LOGIN_COOLDOWN_SEC = 15.0
_LOGIN_FAIL_COOLDOWN_SEC = 30.0

_last_login_ts = 0.0
_last_login_success = False
_last_login_msg = "not attempted"


def _close_session():
    """Best-effort cleanup of robin_stocks' shared requests.Session pool."""
    try:
        sess = getattr(r.helper, "SESSION", None)
        if sess is not None:
            sess.close()
    except Exception:
        pass


def _shutdown(signum, _frame):
    sys.stderr.write(f"rh_wrapper: received signal {signum}, exiting\n")
    sys.stderr.flush()
    _close_session()
    sys.exit(0)


for _sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
    try:
        signal.signal(_sig, _shutdown)
    except (ValueError, OSError):
        pass


def login(force_credentials=False):
    """Authenticate with Robinhood.

    On startup we honour `ROBINHOOD_ACCESS_TOKEN` from env when set — this
    skips the device-approval push and is fast.

    On a 401 / JWT-expired recovery path the caller passes
    `force_credentials=True`. We then bypass the env token (which we now
    know is invalid) and run the full user/password flow. If
    `ROBINHOOD_MFA_SECRET` is set we generate a TOTP automatically;
    otherwise robin_stocks triggers Robinhood's device-approval push
    notification to the user's mobile app — the user taps "approve" and a
    fresh token is issued + persisted to /root/.tokens/robinhood.pickle.
    """
    access_token = os.getenv("ROBINHOOD_ACCESS_TOKEN")
    if not force_credentials and access_token and access_token.strip():
        if not access_token.startswith("Bearer "):
            access_token = "Bearer " + access_token
        r.helper.SESSION.headers["Authorization"] = access_token
        r.helper.set_login_state(True)
        # Validate the token now — if it's dead, fall through to user/pass
        # below instead of silently returning "success" and forcing the next
        # API call to trigger 401 recovery. Saves one wasted request per
        # startup and surfaces the failure mode early.
        try:
            v = r.helper.SESSION.get(
                "https://api.robinhood.com/user/", timeout=8,
                headers={"Origin": "https://robinhood.com"},
            )
            if v.status_code == 401:
                sys.stderr.write(
                    "⚠️  ROBINHOOD_ACCESS_TOKEN is expired/invalid (401). "
                    "Falling back to user/password login.\n"
                )
                sys.stderr.flush()
                r.helper.SESSION.headers.pop("Authorization", None)
                r.helper.set_login_state(False)
                # fall through to credentials flow below
            else:
                return True, "Logged in with Access Token"
        except Exception as e:
            # Network blip — assume token is OK and proceed.
            sys.stderr.write(f"  (token validation skipped: {e})\n")
            sys.stderr.flush()
            return True, "Logged in with Access Token (unvalidated)"

    user = os.getenv("ROBINHOOD_USER")
    password = os.getenv("ROBINHOOD_PASSWORD")
    mfa_secret = os.getenv("ROBINHOOD_MFA_SECRET")

    if not user or not password:
        return False, "Missing credentials"

    if force_credentials:
        # Wipe the dead Authorization header so robin_stocks doesn't reuse
        # the expired Bearer when its login() fires off internal API calls.
        try:
            r.helper.SESSION.headers.pop("Authorization", None)
            r.helper.set_login_state(False)
        except Exception:
            pass
        sys.stderr.write(
            "🔔 Triggering fresh user/password login — if MFA_SECRET is "
            "unset, Robinhood will push a device-approval notification "
            "to the mobile app. Approve it to complete login.\n"
        )
        sys.stderr.flush()

    try:
        # robin_stocks's login() prints status/errors to stdout which would
        # corrupt our JSON-RPC stream. Capture it instead and surface to
        # stderr only on actual failure for diagnosis.
        old_stdout = sys.stdout
        captured = io.StringIO()
        sys.stdout = captured
        try:
            if mfa_secret and mfa_secret.strip():
                totp = pyotp.TOTP(mfa_secret).now()
                login_result = r.login(user, password, mfa_code=totp)
            else:
                login_result = r.login(user, password)
        finally:
            sys.stdout = old_stdout
        captured_text = captured.getvalue()

        # Detect silent failure. robin_stocks 3.4.0's r.login() does NOT
        # always raise on failure — e.g., a 429 from the device-approval
        # push endpoint just prints "Login failed. Check credentials..."
        # and returns None. So check the actual session state, not just
        # whether an exception fired.
        #   1. r.login should have set an Authorization Bearer header
        #   2. r.login returns a dict with "access_token" on success, None on fail
        auth_header = r.helper.SESSION.headers.get("Authorization", "")
        login_ok = (
            login_result is not None
            and isinstance(login_result, dict)
            and bool(login_result.get("access_token"))
            and auth_header.startswith("Bearer ")
        )
        if not login_ok:
            # Echo robin_stocks' diagnostic text to stderr so the user can
            # see what actually happened (429, MFA timeout, bad creds, etc.).
            tail = captured_text.strip().split("\n")[-5:]
            diag = " | ".join(line.strip() for line in tail if line.strip())
            return False, f"r.login silent failure: {diag}" if diag else "r.login silent failure (no diagnostic)"
        return True, "Logged in"
    except Exception as e:
        return False, str(e)


def maybe_login(reason="manual", force_credentials=False):
    """Rate-limited wrapper around login().

    If we attempted a login recently we memoize the prior result instead of
    hitting the network again. Returns (ok, msg, was_cached).

    `force_credentials=True` (used by 401-recovery path) bypasses the env
    `ROBINHOOD_ACCESS_TOKEN` and runs the full user/password flow, which
    triggers Robinhood's device-approval push to the mobile app.
    """
    global _last_login_ts, _last_login_success, _last_login_msg

    now = time.time()
    elapsed = now - _last_login_ts

    # Cooldown applies to BEST-EFFORT logins. When the caller explicitly
    # asks for force_credentials (i.e., the previous "success" was env-token
    # which we now know is invalid because the API just returned 401), we
    # bypass the cache and force a fresh credential login. Otherwise we'd
    # echo the stale cached success and the bot would hit the same 401.
    if _last_login_ts > 0 and not force_credentials:
        # Longer cooldown after a failed attempt — no point hammering auth.
        cooldown = _LOGIN_COOLDOWN_SEC if _last_login_success else _LOGIN_FAIL_COOLDOWN_SEC
        if elapsed < cooldown:
            sys.stderr.write(
                f"🔁 Login skipped (reason={reason}, cached {_last_login_msg!r}, "
                f"{elapsed:.1f}s ago, cooldown={cooldown:.0f}s)\n"
            )
            sys.stderr.flush()
            return _last_login_success, _last_login_msg, True

    sys.stderr.write(
        f"🔑 Login attempt (reason={reason}, force_credentials={force_credentials}) "
        f"at t={now:.1f}\n"
    )
    sys.stderr.flush()

    ok, msg = login(force_credentials=force_credentials)

    _last_login_ts = time.time()
    _last_login_success = ok
    _last_login_msg = msg

    if ok:
        sys.stderr.write(f"✅ Login ok: {msg}\n")
    else:
        sys.stderr.write(f"❌ Login failed: {msg}\n")
    sys.stderr.flush()

    return ok, msg, False


class _suppress_stdout:
    """Context manager to suppress stdout (robin_stocks prints spinners + pagination messages)."""
    def __enter__(self):
        self._old = sys.stdout
        sys.stdout = io.StringIO()
        # Also suppress robin_stocks internal output
        try:
            r.helper.set_output(None)
        except Exception:
            pass
        return self
    def __exit__(self, *args):
        sys.stdout = self._old
        try:
            r.helper.set_output(None)
        except Exception:
            pass


# ── Futures helpers (undocumented Robinhood Legend endpoints) ─────────
# These wrap REST endpoints reverse-engineered from HAR capture of the
# Legend web app. No official API exists — endpoints may change.
_FUT_API = "https://api.robinhood.com"
_FUT_HDRS = {"Origin": "https://robinhood.com", "Referer": "https://robinhood.com/"}
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)

# Cache per-process — these don't change during a session.
_fut_cache = {"rhs": None, "ceres_futures_id": None, "products": None}


def _fut_get(path, params=None):
    url = path if path.startswith("http") else f"{_FUT_API}{path}"
    resp = r.helper.SESSION.get(url, params=params, headers=_FUT_HDRS, timeout=15)
    if not resp.ok:
        raise RuntimeError(f"GET {url} -> {resp.status_code}: {resp.text[:300]}")
    try:
        return resp.json()
    except ValueError:
        return resp.text


def _fut_post(path, json_body):
    url = path if path.startswith("http") else f"{_FUT_API}{path}"
    hdrs = {**_FUT_HDRS, "Content-Type": "application/json"}
    resp = r.helper.SESSION.post(url, json=json_body, headers=hdrs, timeout=15)
    if not resp.ok:
        raise RuntimeError(f"POST {url} -> {resp.status_code}: {resp.text[:500]}")
    try:
        return resp.json()
    except ValueError:
        return resp.text


def _rhs_account_number():
    if _fut_cache["rhs"]:
        return _fut_cache["rhs"]
    profile = r.profiles.load_account_profile()
    rhs = profile.get("account_number")
    if not rhs:
        raise RuntimeError("Could not determine rhsAccountNumber from profile")
    _fut_cache["rhs"] = rhs
    return rhs


def _ceres_futures_account_id():
    if _fut_cache["ceres_futures_id"]:
        return _fut_cache["ceres_futures_id"]
    rhs = _rhs_account_number()
    data = _fut_get("/ceres/v1/accounts", params={"rhsAccountNumber": rhs})
    for acct in data.get("results", []):
        if acct.get("accountType") == "FUTURES":
            _fut_cache["ceres_futures_id"] = acct["id"]
            return acct["id"]
    raise RuntimeError("No FUTURES ceres account found — is futures trading enabled?")


def _futures_products():
    if _fut_cache["products"] is None:
        data = _fut_get("/arsenal/v1/futures/products")
        _fut_cache["products"] = data.get("results", [])
    return _fut_cache["products"]


def _resolve_contract_id(symbol_or_id):
    """Accept a contract UUID, specific contract symbol (/MNQM26), or product
    root (/MNQ → active front-month). Returns the futuresContractId UUID."""
    s = (symbol_or_id or "").strip()
    if _UUID_RE.match(s):
        return s
    if not s.startswith("/"):
        s = "/" + s.upper()
    else:
        s = s.upper()
    root_and_maybe_expiry = s.split(":")[0]  # strip :XCME suffix if any
    m = re.match(r"^(/[A-Z0-9]+?)([FGHJKMNQUVXZ]\d{1,2})$", root_and_maybe_expiry)
    product_symbol = m.group(1) if m else root_and_maybe_expiry
    expiry_code = m.group(2) if m else None

    product = next(
        (p for p in _futures_products() if p.get("displaySymbol") == product_symbol),
        None,
    )
    if not product:
        raise RuntimeError(f"Futures product not found for symbol: {product_symbol}")

    sched = _fut_get("/arsenal/v1/futures/atc_schedule",
                     params={"productIds": product["id"]})
    results = sched.get("results", []) or []

    if expiry_code:
        match = next((c for c in results
                      if c.get("symbol", "").upper().endswith(expiry_code)), None)
        if not match:
            raise RuntimeError(f"Contract not found: {symbol_or_id}")
        return match["futuresContractId"]

    today = datetime.date.today().isoformat()
    active = [c for c in results
              if c.get("startDate", "") <= today <= c.get("endDate", "9999")]
    if active:
        return active[0]["futuresContractId"]
    # Fall back to earliest upcoming.
    upcoming = sorted([c for c in results if c.get("startDate", "") > today],
                      key=lambda c: c["startDate"])
    if upcoming:
        return upcoming[0]["futuresContractId"]
    raise RuntimeError(f"No active contract for {product_symbol}")


def _require_futures_trading_allowed():
    if os.getenv("ROBINHOOD_ALLOW_FUTURES_TRADING", "").strip() != "1":
        raise RuntimeError(
            "Futures order placement is gated. Set "
            "ROBINHOOD_ALLOW_FUTURES_TRADING=1 in .env to enable."
        )


# ── DXLink streaming (reverse-engineered from Robinhood Legend) ────────
# Robinhood Legend uses a Devexperts DXLink-compatible WebSocket at
# api.robinhood.com/marketdata/streaming/legend/v2/. We fetch a short-lived
# ticket from /marketdata/token/v1/ then open the WS and subscribe to
# per-symbol Quote / Trade / Candle / Order (L2) events.

def _get_streaming_token():
    """Fetch a short-lived DXLink auth token + WSS URL. ~4h TTL."""
    session_id = str(uuid.uuid4())
    resp = r.helper.SESSION.get(
        f"{_FUT_API}/marketdata/token/v1/",
        params={"session_id": session_id, "session_type": "blackwidow"},
        headers={**_FUT_HDRS, "X-Robinhood-Client-Platform": "black-widow"},
        timeout=10,
    )
    if not resp.ok:
        raise RuntimeError(f"streaming token fetch failed: {resp.status_code} {resp.text[:200]}")
    body = resp.json()
    data = body.get("data", {}).get("data", {})
    if not data.get("token"):
        raise RuntimeError(f"streaming token response malformed: {body}")
    return data["token"], data.get("wss_url", "wss://api.robinhood.com/marketdata/streaming/legend/v2/")


def _dxlink_subscribe(subscriptions, fields_by_type, collect_seconds=2.5, max_events=None):
    """Open the DXLink WS, subscribe, collect events for a short window, return them.

    `subscriptions` is a list of dicts like [{"type":"Quote","symbol":"/ESM26:XCME"}, ...]
    `fields_by_type` is the FEED_SETUP eventFields map for each requested type.
    Returns a list of data-entry dicts (concatenation of all FEED_DATA `data` arrays).
    """
    oauth = r.helper.SESSION.headers.get("Authorization", "")
    if oauth.startswith("Bearer "):
        oauth = oauth[7:]
    if not oauth:
        raise RuntimeError("No OAuth token on session (run login first)")

    dx_token, wss_url = _get_streaming_token()
    ws = websocket.create_connection(
        wss_url,
        header=["Origin: https://robinhood.com"],
        subprotocols=["bearer", oauth],
        timeout=10,
    )
    try:
        # SETUP + AUTH + MD_SETUP
        ws.send(json.dumps({
            "type": "SETUP", "channel": 0,
            "keepaliveTimeout": 60, "acceptKeepaliveTimeout": 60,
            "version": "0.1-DXF-JS/0.5.1",
        }))
        ws.send(json.dumps({"type": "AUTH", "channel": 0, "token": dx_token}))
        ws.send(json.dumps({
            "type": "MD_SETUP",
            "enable_heartbeat_timestamp": True,
            "enable_logging_raw_incoming_message": False,
            "enable_subscription_debugging": False,
        }))

        # Wait for AUTHORIZED (first AUTH_STATE is often UNAUTHORIZED, then flips).
        ws.settimeout(5.0)
        authorized = False
        deadline = time.time() + 5.0
        while not authorized and time.time() < deadline:
            msg = json.loads(ws.recv())
            if msg.get("type") == "AUTH_STATE" and msg.get("state") == "AUTHORIZED":
                authorized = True
            elif msg.get("type") == "AUTH_STATE" and msg.get("state") == "UNAUTHORIZED" and msg.get("channel") == 0:
                continue  # initial state
        if not authorized:
            raise RuntimeError("DXLink auth did not flip to AUTHORIZED within 5s")

        # Open FEED channel
        channel = 3
        ws.send(json.dumps({
            "type": "CHANNEL_REQUEST", "channel": channel,
            "service": "FEED", "parameters": {"contract": "AUTO"},
        }))
        opened = False
        while not opened and time.time() < deadline + 3:
            msg = json.loads(ws.recv())
            if msg.get("type") == "CHANNEL_OPENED" and msg.get("channel") == channel:
                opened = True
        if not opened:
            raise RuntimeError("CHANNEL_OPENED not received within 3s")

        # Tell server what fields we want
        ws.send(json.dumps({
            "type": "FEED_SETUP", "channel": channel,
            "acceptAggregationPeriod": 0.25,
            "acceptDataFormat": "FULL",
            "acceptEventFields": fields_by_type,
        }))

        # Subscribe
        ws.send(json.dumps({
            "type": "FEED_SUBSCRIPTION", "channel": channel,
            "reset": True, "add": subscriptions,
        }))

        # Collect FEED_DATA for a short window
        events = []
        ws.settimeout(0.5)
        collection_deadline = time.time() + collect_seconds
        while time.time() < collection_deadline:
            try:
                raw = ws.recv()
            except Exception:
                continue
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            if msg.get("type") == "FEED_DATA":
                data = msg.get("data") or []
                events.extend(data if isinstance(data, list) else [data])
                if max_events and len(events) >= max_events:
                    break
        return events
    finally:
        try:
            ws.close()
        except Exception:
            pass


def _product_source(product_symbol):
    """Map a product root to the DXLink `source` string for Order (L2) subs.
    Almost all Robinhood futures live on CME Globex. Minor tweak point later
    if/when an exchange outside Globex is added."""
    return "GLBX"


def _resolve_dx_symbol(symbol_or_id):
    """Turn a user input into the DX symbol string with exchange suffix.

    Inputs accepted:
      - Full DX symbol: `/ESM26:XCME` (passthrough)
      - Contract symbol: `/ESM26` → adds `:XCME`
      - Contract UUID → resolved to symbol via /arsenal/v1/futures/contracts/{id}
      - Product root `/ES` → resolves to active front-month symbol
    """
    s = (symbol_or_id or "").strip()
    if ":" in s:
        return s
    if _UUID_RE.match(s):
        detail = _fut_get(f"/arsenal/v1/futures/contracts/{s}")
        return detail["symbol"]  # already in "/ESM26:XCME" form
    cid = _resolve_contract_id(s)  # root → front-month, or specific contract
    detail = _fut_get(f"/arsenal/v1/futures/contracts/{cid}")
    return detail["symbol"]


def handle_command(data):
    command = data.get("command")
    args = data.get("args", [])
    
    if command == "quote":
        return r.stocks.get_stock_quote_by_symbol(args[0])
    elif command == "portfolio":
        profile = r.profiles.load_portfolio_profile()
        account = r.profiles.load_account_profile()
        return {"portfolio": profile, "account": account}
    elif command == "positions":
        return r.account.build_holdings()
    elif command == "buy":
        return r.orders.order_buy_market(args[0], float(args[1]))
    elif command == "sell":
        return r.orders.order_sell_market(args[0], float(args[1]))
    elif command == "orders":
        return r.orders.get_all_stock_orders()[:10]
    elif command == "instrument":
        return r.stocks.get_instruments_by_symbols(args[0])
    elif command == "watchlists":
        return r.account.get_all_watchlists()
    elif command == "historicals":
        # args: [symbol, interval, span]
        # interval: 5minute, 10minute, hour, day, week
        # span: day, week, month, 3month, year, 5year
        symbol = args[0]
        interval = args[1] if len(args) > 1 else "hour"
        span = args[2] if len(args) > 2 else "month"
        return r.stocks.get_stock_historicals(symbol, interval=interval, span=span)
    elif command == "crypto_historicals":
        # args: [symbol, interval, span]
        symbol = args[0]
        interval = args[1] if len(args) > 1 else "hour"
        span = args[2] if len(args) > 2 else "month"
        return r.crypto.get_crypto_historicals(symbol, interval=interval, span=span)
    elif command == "fundamentals":
        return r.stocks.get_fundamentals(args[0])
    elif command == "earnings":
        return r.stocks.get_earnings(args[0])
    elif command == "news":
        return r.stocks.get_news(args[0])
    elif command == "movers":
        # args: [direction] — "up" or "down"
        direction = args[0] if args else "up"
        return r.markets.get_top_movers_sp500(direction)
    # ── Options ──
    # robin_stocks options functions print spinner/progress to stdout.
    # We must suppress stdout during these calls to keep JSON-RPC clean.
    elif command == "option_chain":
        symbol = args[0]
        with _suppress_stdout():
            return r.options.get_chains(symbol)
    elif command == "option_expirations":
        symbol = args[0]
        with _suppress_stdout():
            chain = r.options.get_chains(symbol)
        return chain.get("expiration_dates", []) if isinstance(chain, dict) else chain
    elif command == "option_find":
        symbol = args[0]
        expiration = args[1]
        option_type = args[2] if len(args) > 2 else "call"
        # Optional 4th arg: center strike to filter around (reduces response size)
        center = float(args[3]) if len(args) > 3 else None
        with _suppress_stdout():
            options = r.options.find_options_by_expiration(
                symbol, expirationDate=expiration, optionType=option_type
            )
        # Filter to +/- 15% of center price to avoid huge responses
        if center and options:
            lo, hi = center * 0.85, center * 1.15
            options = [o for o in options if o.get("strike_price") and lo <= float(o["strike_price"]) <= hi]
        result = []
        for opt in (options or []):
            result.append({
                "strike_price": opt.get("strike_price"),
                "expiration_date": opt.get("expiration_date"),
                "type": opt.get("type"),
                "bid_price": opt.get("bid_price"),
                "ask_price": opt.get("ask_price"),
                "last_trade_price": opt.get("last_trade_price"),
                "mark_price": opt.get("mark_price"),
                "high_price": opt.get("high_price"),
                "low_price": opt.get("low_price"),
                "volume": opt.get("volume"),
                "open_interest": opt.get("open_interest"),
                "implied_volatility": opt.get("implied_volatility"),
                "delta": opt.get("delta"),
                "gamma": opt.get("gamma"),
                "theta": opt.get("theta"),
                "vega": opt.get("vega"),
                "rho": opt.get("rho"),
                "chance_of_profit_long": opt.get("chance_of_profit_long"),
                "chance_of_profit_short": opt.get("chance_of_profit_short"),
                "id": opt.get("id"),
            })
        return result
    elif command == "option_quote":
        symbol = args[0]
        expiration = args[1]
        strike = args[2]
        option_type = args[3] if len(args) > 3 else "call"
        with _suppress_stdout():
            options = r.options.find_options_by_expiration_and_strike(
                symbol, expirationDate=expiration,
                strikePrice=strike, optionType=option_type
            )
            if options:
                market_data = r.options.get_option_market_data_by_id(options[0].get("id"))
                if market_data:
                    return market_data[0] if isinstance(market_data, list) else market_data
        return options
    elif command == "option_positions":
        with _suppress_stdout():
            return r.options.get_open_option_positions()
    elif command == "option_market_data":
        symbol = args[0]
        expiration = args[1]
        strike = str(args[2])
        option_type = args[3] if len(args) > 3 else "call"
        with _suppress_stdout():
            data = r.options.get_option_market_data(
                symbol, expirationDate=expiration,
                strikePrice=strike, optionType=option_type
            )
        if data and isinstance(data, list) and len(data) > 0:
            return data[0]
        return data
    # ── Futures (reverse-engineered from Robinhood Legend) ─────────────
    elif command == "futures_products":
        return {"results": _futures_products()}
    elif command == "futures_product":
        # args: [productId | product symbol like /MNQ]
        pid = args[0]
        if not _UUID_RE.match(pid):
            sym = pid if pid.startswith("/") else "/" + pid.upper()
            prod = next((p for p in _futures_products() if p.get("displaySymbol") == sym.upper()), None)
            if not prod:
                raise RuntimeError(f"Futures product not found: {args[0]}")
            pid = prod["id"]
        return _fut_get(f"/arsenal/v1/futures/products/{pid}")
    elif command == "futures_contract":
        # args: [contractId | symbol like /MNQM26 or /MNQ for front-month]
        cid = _resolve_contract_id(args[0])
        return _fut_get(f"/arsenal/v1/futures/contracts/{cid}")
    elif command == "futures_expirations":
        # args: [productId | product symbol]
        raw = args[0]
        if _UUID_RE.match(raw):
            pid = raw
        else:
            sym = raw if raw.startswith("/") else "/" + raw.upper()
            prod = next((p for p in _futures_products() if p.get("displaySymbol") == sym.upper()), None)
            if not prod:
                raise RuntimeError(f"Futures product not found: {raw}")
            pid = prod["id"]
        return _fut_get("/arsenal/v1/futures/atc_schedule", params={"productIds": pid})
    elif command == "futures_margin_requirement":
        # args: [contractId | symbol, margin_type(OVERNIGHT|DAY, default OVERNIGHT)]
        cid = _resolve_contract_id(args[0])
        margin_type = args[1].upper() if len(args) > 1 else "OVERNIGHT"
        if not margin_type.startswith("MARGIN_TYPE_"):
            margin_type = f"MARGIN_TYPE_{margin_type}"
        return _fut_get("/arsenal/v1/futures/margin_requirement", params={
            "contractId": cid,
            "marginType": margin_type,
            "accountType": "ACCOUNT_TYPE_MARGIN_NO_LIMIT",
        })
    elif command == "futures_eligibility":
        return _fut_get(f"/ceres/v1/futures_account_eligibility/{_rhs_account_number()}")
    elif command == "futures_buying_power":
        return _fut_get(f"/futures/{_rhs_account_number()}/futures_buying_power/")
    elif command == "buying_power_breakdown":
        return _fut_get(f"/accounts/{_rhs_account_number()}/buying_power_breakdown")
    elif command == "futures_positions":
        return _fut_get(f"/ceres/v1/accounts/{_ceres_futures_account_id()}/aggregated_positions")
    elif command == "futures_contract_quantity":
        # args: [contractId | symbol] — accepts multiple comma-separated too
        raw = args[0]
        cids = []
        for piece in raw.split(","):
            cids.append(_resolve_contract_id(piece.strip()))
        return _fut_get(
            f"/ceres/v1/accounts/{_ceres_futures_account_id()}/contract_quantities",
            params={"contractIds": ",".join(cids)},
        )
    elif command == "futures_pnl":
        return _fut_get(f"/ceres/v1/accounts/{_ceres_futures_account_id()}/pnl_cost_basis")
    elif command == "futures_orders":
        return _fut_get("/wormhole/bw/orders/recent",
                        params={"accountNumber": _rhs_account_number()})
    elif command == "futures_order":
        # args: [orderId]
        oid = args[0]
        return _fut_get(f"/wormhole/bw/orders/{oid}", params={
            "assetType": "FUTURES",
            "rhsAccountNumber": _rhs_account_number(),
        })
    elif command == "futures_preview_order":
        # args: [contractId | symbol, side, quantity]
        cid = _resolve_contract_id(args[0])
        side = args[1].upper()
        if side not in ("BUY", "SELL"):
            raise RuntimeError("side must be BUY or SELL")
        qty = str(args[2])
        return _fut_post("/ceres/v1/accounts/buying_power_effect", {
            "accountId": _ceres_futures_account_id(),
            "tentativeFuturesOrder": {
                "legs": [{
                    "contractType": "OUTRIGHT",
                    "contractId": cid,
                    "ratioQuantity": 1,
                    "orderSide": side,
                }],
                "quantity": qty,
            },
        })
    elif command == "futures_place_order":
        # args: [contractId | symbol, side, quantity, order_type, limit_price, time_in_force]
        _require_futures_trading_allowed()
        cid = _resolve_contract_id(args[0])
        side = args[1].upper()
        if side not in ("BUY", "SELL"):
            raise RuntimeError("side must be BUY or SELL")
        qty = str(args[2])
        order_type = args[3].upper() if len(args) > 3 else "LIMIT"
        if order_type not in ("LIMIT", "MARKET"):
            raise RuntimeError("order_type must be LIMIT or MARKET")
        limit_price = str(args[4]) if len(args) > 4 and args[4] not in (None, "", "null") else None
        if order_type == "LIMIT" and not limit_price:
            raise RuntimeError("limit_price required for LIMIT orders")
        tif = args[5].upper() if len(args) > 5 else "GFD"
        if tif not in ("GFD", "GTC"):
            raise RuntimeError("time_in_force must be GFD or GTC")
        body = {
            "accountId": _ceres_futures_account_id(),
            "legs": [{
                "contractType": "OUTRIGHT",
                "contractId": cid,
                "ratioQuantity": 1,
                "orderSide": side,
            }],
            "quantity": qty,
            "orderType": order_type,
            "orderTrigger": "IMMEDIATE",
            "timeInForce": tif,
            "refId": str(uuid.uuid4()),
        }
        if order_type == "LIMIT":
            body["limitPrice"] = limit_price
        sys.stderr.write(
            f"📈 FUTURES ORDER: {side} {qty} {args[0]} "
            f"{order_type}{'@'+limit_price if limit_price else ''} {tif}\n"
        )
        sys.stderr.flush()
        return _fut_post("/ceres/v1/orders", body)
    elif command == "futures_cancel_order":
        # args: [orderId]
        order_id = args[0]
        sys.stderr.write(f"❌ FUTURES CANCEL: {order_id}\n")
        sys.stderr.flush()
        return _fut_post(f"/ceres/v1/orders/{order_id}/cancel", {
            "accountId": _ceres_futures_account_id(),
        })
    # ── DXLink streaming (L1 quote / Trade / Candle / L2 Order book) ──
    elif command == "futures_quote":
        # args: [symbol]
        dx_symbol = _resolve_dx_symbol(args[0])
        events = _dxlink_subscribe(
            [{"type": "Quote", "symbol": dx_symbol}],
            {"Quote": ["eventSymbol", "bidPrice", "askPrice", "bidSize", "askSize", "bidTime", "askTime"]},
            collect_seconds=2.0,
            max_events=10,
        )
        if not events:
            return {"symbol": dx_symbol, "quote": None, "note": "no quote events within 2s (market closed or symbol inactive?)"}
        # Return the most recent event
        return {"symbol": dx_symbol, "quote": events[-1], "events_seen": len(events)}
    elif command == "futures_trade":
        # args: [symbol]
        dx_symbol = _resolve_dx_symbol(args[0])
        events = _dxlink_subscribe(
            [{"type": "Trade", "symbol": dx_symbol}],
            {"Trade": ["eventSymbol", "price", "size", "time", "dayVolume"]},
            collect_seconds=2.0,
            max_events=5,
        )
        if not events:
            return {"symbol": dx_symbol, "trade": None, "note": "no trade events within 2s"}
        return {"symbol": dx_symbol, "trade": events[-1], "events_seen": len(events)}
    elif command == "futures_historicals":
        # args: [symbol, interval, span]
        # interval: '1m','5m','15m','1h','4h','1d' (DX format suffix) default '1h'
        # span: time window in seconds back from now (default 7 days)
        raw_symbol = args[0]
        interval = args[1] if len(args) > 1 else "1h"
        span_sec = int(args[2]) if len(args) > 2 and args[2] not in (None, "") else 7 * 24 * 3600
        dx_symbol = _resolve_dx_symbol(raw_symbol)
        # For candles we use the product-level symbol (e.g. /ES:XCME) with
        # interval notation: /ES:XCME{=1h,tho=false,a=m}
        # Extract the product root from contract symbol: "/ESM26:XCME" -> "/ES:XCME"
        m = re.match(r"^(/[A-Z0-9]+?)[FGHJKMNQUVXZ]\d{1,2}(:.*)$", dx_symbol)
        product_symbol = f"{m.group(1)}{m.group(2)}" if m else dx_symbol
        candle_symbol = f"{product_symbol}{{={interval},tho=false,a=m}}"
        from_time_ms = int((time.time() - span_sec) * 1000)
        events = _dxlink_subscribe(
            [{"type": "Candle", "symbol": candle_symbol,
              "fromTime": from_time_ms, "instrumentType": "futures_product"}],
            {"Candle": ["eventSymbol", "eventTime", "open", "high", "low", "close", "volume", "time"]},
            collect_seconds=3.0,
        )
        # Dedupe by `time` (keep last value per bar), sort ascending by time,
        # drop placeholder bars where all OHLC values are NaN.
        import math
        def _is_nan(v):
            return v in (None, "NaN") or (isinstance(v, float) and math.isnan(v))
        by_time = {}
        for e in events:
            if "time" in e and not all(_is_nan(e.get(k)) for k in ("open", "high", "low", "close")):
                by_time[e["time"]] = e
        bars = sorted(by_time.values(), key=lambda e: e.get("time", 0))
        return {
            "symbol": dx_symbol,
            "product_symbol": product_symbol,
            "interval": interval,
            "from_time_ms": from_time_ms,
            "bars": bars,
            "bar_count": len(bars),
        }
    elif command == "futures_l2_snapshot":
        # args: [symbol, levels(default 10)]
        dx_symbol = _resolve_dx_symbol(args[0])
        levels = int(args[1]) if len(args) > 1 and args[1] not in (None, "") else 10
        events = _dxlink_subscribe(
            [{"type": "Order", "symbol": dx_symbol, "source": "GLBX"}],
            {"Order": ["eventFlags", "eventSymbol", "eventType", "index", "side", "sequence", "price", "size", "time"]},
            collect_seconds=3.0,
        )
        # Order events arrive as a mix of snapshot + deltas. Each entry has
        # an `index` (order ID), `side`, `price`, `size`. Apply in order:
        #   eventFlags & 0x10 (REMOVE_EVENT): delete the order
        #   size missing / "NaN" / <= 0: delete the order
        #   else: insert/update the order at price
        import math
        book = {"BUY": {}, "SELL": {}}
        for e in events:
            side = e.get("side")
            idx = e.get("index")
            if side not in book or idx is None:
                continue
            flags = e.get("eventFlags", 0) or 0
            size = e.get("size")
            price = e.get("price")
            is_nan_size = (size in (None, "NaN")) or (isinstance(size, float) and math.isnan(size))
            if (flags & 0x10) or is_nan_size or (isinstance(size, (int, float)) and size <= 0):
                book[side].pop(idx, None)
            elif isinstance(price, (int, float)) and not (isinstance(price, float) and math.isnan(price)):
                book[side][idx] = {"price": price, "size": size}
        # Aggregate by price level
        def aggregate(side_book, reverse):
            by_price = {}
            for entry in side_book.values():
                p = entry["price"]
                by_price[p] = by_price.get(p, 0) + (entry["size"] or 0)
            return sorted(
                [{"price": p, "size": round(s, 4)} for p, s in by_price.items()],
                key=lambda x: x["price"], reverse=reverse,
            )[:levels]
        bids = aggregate(book["BUY"], reverse=True)
        asks = aggregate(book["SELL"], reverse=False)
        return {
            "symbol": dx_symbol,
            "levels": levels,
            "bids": bids,
            "asks": asks,
            "events_processed": len(events),
        }
    else:
        raise ValueError(f"Unknown command: {command}")

def _emit(payload):
    """Write a single JSON response line to stdout, exiting cleanly on broken pipe."""
    try:
        sys.stdout.write(json.dumps(payload) + "\n")
        sys.stdout.flush()
    except BrokenPipeError:
        # Parent (Node MCP) has gone away. Stop quietly.
        _close_session()
        os._exit(0)


def main():
    maybe_login(reason="startup")

    try:
        for line in sys.stdin:
            if not line.strip():
                continue

            request_id = None
            try:
                request = json.loads(line)
                request_id = request.get("id")
            except Exception as parse_err:
                _emit({"id": None, "status": "error", "error": f"Invalid JSON request: {parse_err}"})
                continue

            try:
                response_data = handle_command(request)
                _emit({"id": request_id, "status": "success", "data": response_data})
                continue
            except Exception as e:
                error_str = str(e)

            # Auto re-login on any 401 (Unauthorized, JWT verification failed,
            # token expired, etc.) and retry once. Force a fresh user/password
            # login so the env-supplied ROBINHOOD_ACCESS_TOKEN — which we now
            # know is invalid — is bypassed. This triggers Robinhood's device-
            # approval push to the mobile app (or auto-completes if MFA secret
            # is set). If maybe_login returns a cached result, retrying would
            # hit the same 401, so fail fast instead.
            if "401" in error_str:
                ok, msg, cached = maybe_login(
                    reason="401-recovery", force_credentials=True
                )
                if ok and not cached:
                    try:
                        response_data = handle_command(request)
                        _emit({"id": request_id, "status": "success", "data": response_data})
                        continue
                    except Exception as retry_err:
                        error_str = str(retry_err)
                elif cached:
                    error_str = (
                        f"Upstream 401 — last login attempt was cached "
                        f"({msg!r}); skipping duplicate re-login"
                    )

            _emit({"id": request_id, "status": "error", "error": error_str})
    except KeyboardInterrupt:
        pass
    finally:
        _close_session()


if __name__ == "__main__":
    main()
