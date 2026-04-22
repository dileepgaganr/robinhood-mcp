import robin_stocks.robinhood as r
import os
import sys
import io
import json
import signal
import time
import pyotp
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


def login():
    access_token = os.getenv("ROBINHOOD_ACCESS_TOKEN")
    if access_token and access_token.strip():
        if not access_token.startswith("Bearer "):
            access_token = "Bearer " + access_token
        r.helper.SESSION.headers["Authorization"] = access_token
        r.helper.set_login_state(True)
        return True, "Logged in with Access Token"

    user = os.getenv("ROBINHOOD_USER")
    password = os.getenv("ROBINHOOD_PASSWORD")
    mfa_secret = os.getenv("ROBINHOOD_MFA_SECRET")

    if not user or not password:
        return False, "Missing credentials"

    try:
        # Suppress robin_stocks login prints — they corrupt JSON-RPC stdout
        old_stdout = sys.stdout
        sys.stdout = io.StringIO()
        try:
            if mfa_secret and mfa_secret.strip():
                totp = pyotp.TOTP(mfa_secret).now()
                r.login(user, password, mfa_code=totp)
            else:
                r.login(user, password)
        finally:
            sys.stdout = old_stdout
        return True, "Logged in"
    except Exception as e:
        return False, str(e)


def maybe_login(reason="manual"):
    """Rate-limited wrapper around login().

    If we attempted a login recently we memoize the prior result instead of
    hitting the network again. Returns (ok, msg, was_cached).
    """
    global _last_login_ts, _last_login_success, _last_login_msg

    now = time.time()
    elapsed = now - _last_login_ts

    if _last_login_ts > 0:
        # Longer cooldown after a failed attempt — no point hammering auth.
        cooldown = _LOGIN_COOLDOWN_SEC if _last_login_success else _LOGIN_FAIL_COOLDOWN_SEC
        if elapsed < cooldown:
            sys.stderr.write(
                f"🔁 Login skipped (reason={reason}, cached {_last_login_msg!r}, "
                f"{elapsed:.1f}s ago, cooldown={cooldown:.0f}s)\n"
            )
            sys.stderr.flush()
            return _last_login_success, _last_login_msg, True

    sys.stderr.write(f"🔑 Login attempt (reason={reason}) at t={now:.1f}\n")
    sys.stderr.flush()

    ok, msg = login()

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

            # Auto re-login on 401 Unauthorized and retry once — but only if the
            # rate-limited wrapper actually performed a fresh login. If maybe_login
            # returns a cached result, retrying would produce the same 401.
            if "401" in error_str and "Unauthorized" in error_str:
                ok, msg, cached = maybe_login(reason="401-recovery")
                if ok and not cached:
                    try:
                        response_data = handle_command(request)
                        _emit({"id": request_id, "status": "success", "data": response_data})
                        continue
                    except Exception as retry_err:
                        error_str = str(retry_err)
                elif cached:
                    # Either we just retried and it's still 401, or a recent login
                    # already failed. Fail fast so Node / Claude can surface the error
                    # instead of silently stalling while the cooldown expires.
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
