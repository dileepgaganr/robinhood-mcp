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
import requests
import websocket
from dotenv import load_dotenv

load_dotenv()

# ── Login rate limiting ──────────────────────────────────────────────
# Without this, a single expired-session moment would trigger a fresh
# login() call for every command that arrives during the recovery window
# — each one hitting Robinhood's auth endpoint and (if the user is on
# credential + device-approval flow) spamming their phone with multiple
# approval notifications. Worse: the wrapper is respawned on every new
# MCP connection and on every idle-timeout cycle (see index.js), and this
# state used to live only in process memory — so each respawn forgot the
# cooldown and fired another live login attempt. That repeated hammering
# is almost certainly what got the account/IP rate-limited by Robinhood.
#
# _LOGIN_MIN_GAP_SEC is a hard floor between any two login attempts —
# success or failure, startup or 401-recovery — persisted to disk (same
# volume the auth token lives in) so the floor survives process respawns
# and container restarts, not just one process's lifetime.
_LOGIN_MIN_GAP_SEC = float(os.getenv("ROBINHOOD_LOGIN_MIN_GAP_SEC", "1800"))
_LOGIN_STATE_FILE = "/root/.tokens/login_state.json"

_last_login_ts = 0.0
_last_login_success = False
_last_login_msg = "not attempted"


def _load_login_state():
    """Sync in-memory cooldown state from disk so a freshly spawned
    process learns about attempts made by a prior process instead of
    starting the cooldown clock at 0."""
    global _last_login_ts, _last_login_success, _last_login_msg
    try:
        with open(_LOGIN_STATE_FILE) as f:
            state = json.load(f)
        _last_login_ts = float(state.get("ts", 0.0))
        _last_login_success = bool(state.get("success", False))
        _last_login_msg = str(state.get("msg", "not attempted"))
    except (FileNotFoundError, ValueError, OSError):
        pass


def _save_login_state():
    try:
        tmp = _unique_tmp(_LOGIN_STATE_FILE)
        with open(tmp, "w") as f:
            json.dump(
                {"ts": _last_login_ts, "success": _last_login_success, "msg": _last_login_msg},
                f,
            )
        os.replace(tmp, _LOGIN_STATE_FILE)
    except OSError:
        pass


def _unique_tmp(path):
    # Every MCP connection runs its own wrapper process, all writing the
    # same files. A shared ".tmp" name let concurrent writers truncate each
    # other's half-written file before os.replace — which wiped session.json
    # (and the live refresh token with it) on 2026-10-29.
    return f"{path}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"


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


# ── Session store + refresh-token renewal ────────────────────────────
# Robinhood dropped TOTP support, so the credentials path now always needs
# a device-approval tap. Access tokens last days, not forever — without
# renewal the server silently died every few weeks. The OAuth token
# response also carries a refresh_token; exchanging it for a new access
# token needs no password and no phone push. We persist the latest pair
# (Robinhood rotates the refresh token on every use) to the tokens volume.
_SESSION_FILE = "/root/.tokens/session.json"
# robin_stocks 3.4.0 stores its session as a pickle; robin_stocks_v2 as JSON.
_LIB_SESSION_FILES = ("/root/.tokens/robinhood.json", "/root/.tokens/robinhood.pickle")
_TOKEN_URL = "https://api.robinhood.com/oauth2/token/"
_CLIENT_ID = "c82SH0WZOsabOXGP2sxqcj34FxkvfnWRZBKlBjFS"  # same as robin_stocks / web app
_REFRESH_EXPIRES_IN = int(os.getenv("ROBINHOOD_REFRESH_EXPIRES_IN", "734000"))
# Renew proactively when the access token has less than this left.
_REFRESH_AHEAD_SEC = float(os.getenv("ROBINHOOD_REFRESH_AHEAD_SEC", "1800"))
# Floor between refresh attempts so a broken refresh can't hammer /oauth2/.
_REFRESH_MIN_GAP_SEC = 60.0
_last_refresh_ts = 0.0
_SESSION_LOCK_FILE = "/root/.tokens/session.lock"
_lock_fd = None
_lock_depth = 0


class _session_lock:
    """Cross-process exclusive lock around session read-modify-write and
    refresh. Serializing refresh matters as much as the file: Robinhood
    rotates the refresh token on use, so two processes refreshing with the
    same token means the loser gets invalid_grant. Reentrant within a
    process (flock on a second fd from the same process would deadlock)."""
    def __enter__(self):
        global _lock_fd, _lock_depth
        if _lock_depth == 0:
            import fcntl
            try:
                _lock_fd = os.open(_SESSION_LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o600)
                fcntl.flock(_lock_fd, fcntl.LOCK_EX)
            except OSError:
                _lock_fd = None  # no tokens volume (e.g. tests) — run unlocked
        _lock_depth += 1
        return self

    def __exit__(self, *exc):
        global _lock_fd, _lock_depth
        _lock_depth -= 1
        if _lock_depth == 0 and _lock_fd is not None:
            import fcntl
            fcntl.flock(_lock_fd, fcntl.LOCK_UN)
            os.close(_lock_fd)
            _lock_fd = None


def _strip_bearer(token):
    token = (token or "").strip()
    return token[7:].strip() if token.startswith("Bearer ") else token


def _token_problem(token):
    """Return why a token string is unusable, or None if it looks sane.
    Catches the common copy-paste failure: DevTools elides long header
    values with '…', and pasting that yields a non-ASCII, invalid token."""
    if not token:
        return "empty"
    if not token.isascii():
        return "contains non-ASCII characters (truncated copy from DevTools?)"
    if any(c.isspace() for c in token):
        return "contains whitespace"
    return None


def _jwt_exp(token):
    """Expiry (unix seconds) from a JWT's payload, or None if not a JWT."""
    try:
        import base64
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return float(json.loads(base64.urlsafe_b64decode(payload))["exp"])
    except Exception:
        return None


def _token_fp(token):
    import hashlib
    return hashlib.sha256(token.encode()).hexdigest()[:16]


def _load_session():
    try:
        with open(_SESSION_FILE) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except ValueError:
        sys.stderr.write(f"⚠️  {_SESSION_FILE} is corrupt — refresh chain unreadable\n")
        sys.stderr.flush()
        return {}
    except (FileNotFoundError, OSError):
        return {}


def _save_session(data):
    try:
        tmp = _unique_tmp(_SESSION_FILE)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        os.replace(tmp, _SESSION_FILE)
    except OSError as e:
        sys.stderr.write(f"⚠️  could not persist session: {e}\n")
        sys.stderr.flush()


def _load_lib_tokens():
    """Tokens robin_stocks itself stored after a credentials login."""
    import pickle
    for path in _LIB_SESSION_FILES:
        try:
            if path.endswith(".json"):
                with open(path) as f:
                    data = json.load(f)
            else:
                with open(path, "rb") as f:
                    data = pickle.load(f)
            if isinstance(data, dict) and data:
                return data
        except Exception:
            continue
    return {}


def _store_tokens(access_token, refresh_token=None, device_token=None, source=""):
    with _session_lock():
        _store_tokens_locked(access_token, refresh_token, device_token, source)


def _store_tokens_locked(access_token, refresh_token, device_token, source):
    sess = _load_session()
    sess["access_token"] = _strip_bearer(access_token)
    if refresh_token:
        sess["refresh_token"] = refresh_token
    if device_token:
        sess["device_token"] = device_token
    sess["access_exp"] = _jwt_exp(sess["access_token"])
    sess["saved_at"] = time.time()
    sess["source"] = source
    _save_session(sess)


def _set_auth_header(token):
    r.helper.SESSION.headers["Authorization"] = "Bearer " + _strip_bearer(token)
    r.helper.set_login_state(True)


def _clear_auth_header():
    r.helper.SESSION.headers.pop("Authorization", None)
    r.helper.set_login_state(False)


def _refresh_candidates():
    """Refresh tokens to try, best first, skipping ones already rejected or
    already consumed (rotated away)."""
    sess = _load_session()
    dead = set(sess.get("dead_refresh_fps", []))
    device = _device_token()
    out = []
    for src, tok in (
        ("session", sess.get("refresh_token")),
        ("env", os.getenv("ROBINHOOD_REFRESH_TOKEN")),
        ("robin_stocks", _load_lib_tokens().get("refresh_token")),
    ):
        tok = (tok or "").strip()
        if tok and _dead_fp(tok, device) not in dead and tok not in [t for _, t in out]:
            out.append((src, tok))
    return out


def _dead_fp(refresh_token, device_token):
    # Keyed on the device too: a refresh rejected for a missing/wrong device
    # ID gets another chance once ROBINHOOD_DEVICE_TOKEN is supplied.
    return _token_fp(f"{refresh_token}|{device_token or ''}")


def _mark_refresh_dead(token):
    with _session_lock():
        _mark_refresh_dead_locked(token)


def _mark_refresh_dead_locked(token):
    sess = _load_session()
    dead = sess.get("dead_refresh_fps", [])
    fp = _dead_fp(token, _device_token())
    if fp not in dead:
        dead = (dead + [fp])[-20:]
    sess["dead_refresh_fps"] = dead
    if sess.get("refresh_token") == token:
        sess.pop("refresh_token", None)
    _save_session(sess)


def _device_token():
    """Device ID the refresh token is bound to. A browser-issued refresh
    token only renews with the browser's own device ID (robinhood.com's
    `device_id` cookie); sending none or a different one gets invalid_grant."""
    return (
        os.getenv("ROBINHOOD_DEVICE_TOKEN", "").strip()
        or _load_session().get("device_token")
        or _load_lib_tokens().get("device_token")
    )


def _try_refresh(reason="manual"):
    """Exchange a stored refresh token for a fresh access token.
    No password, no device-approval push. Returns (ok, msg)."""
    global _last_refresh_ts
    if time.time() - _last_refresh_ts < _REFRESH_MIN_GAP_SEC:
        return False, "Refresh attempted too recently"
    with _session_lock():
        # Another process may have refreshed while we waited for the lock;
        # its fresh token is in the store, and our refresh token is spent.
        sess = _load_session()
        current = _strip_bearer(r.helper.SESSION.headers.get("Authorization", ""))
        stored = sess.get("access_token")
        if (stored and stored != current and sess.get("refresh_token")
                and (sess.get("access_exp") or 0) - time.time() > _REFRESH_AHEAD_SEC):
            _set_auth_header(stored)
            return True, "Picked up token refreshed by another process"
        _last_refresh_ts = time.time()
        return _try_refresh_locked(reason)


def _try_refresh_locked(reason):
    candidates = _refresh_candidates()
    if not candidates:
        return False, "No refresh token available"
    device_token = _device_token()
    last_err = "no candidates tried"
    for src, refresh_token in candidates:
        payload = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "scope": "internal",
            "client_id": _CLIENT_ID,
            "expires_in": _REFRESH_EXPIRES_IN,
        }
        if device_token:
            payload["device_token"] = device_token
        try:
            # Don't send a (possibly dead) Bearer on the token endpoint.
            hdrs = {"Origin": "https://robinhood.com", "Authorization": None}
            resp = r.helper.SESSION.post(_TOKEN_URL, data=payload, headers=hdrs, timeout=15)
        except Exception as e:
            last_err = f"refresh request failed: {e}"
            continue  # network problem — token isn't known-bad, keep it
        try:
            body = resp.json()
        except ValueError:
            body = {}
        if resp.ok and body.get("access_token"):
            new_refresh = body.get("refresh_token") or refresh_token
            if new_refresh != refresh_token:
                _mark_refresh_dead(refresh_token)  # rotated; old one is spent
            _store_tokens(body["access_token"], new_refresh, device_token, source=f"refresh:{src}")
            _set_auth_header(body["access_token"])
            exp = _jwt_exp(body["access_token"])
            left = f", valid {((exp - time.time()) / 3600):.1f}h" if exp else ""
            sys.stderr.write(f"🔄 Token refreshed via {src} refresh_token (reason={reason}{left})\n")
            sys.stderr.flush()
            return True, f"Refreshed access token ({src} refresh_token)"
        last_err = f"refresh via {src} rejected: HTTP {resp.status_code} {str(body or resp.text)[:200]}"
        if resp.status_code in (400, 401, 403):
            _mark_refresh_dead(refresh_token)
    sys.stderr.write(f"⚠️  {last_err}\n")
    sys.stderr.flush()
    return False, last_err


def _access_candidates():
    """(source, token, exp) for each sane access token, latest expiry first."""
    sess = _load_session()
    out = []
    for src, tok in (
        ("session", sess.get("access_token")),
        ("env", os.getenv("ROBINHOOD_ACCESS_TOKEN")),
    ):
        tok = _strip_bearer(tok)
        if not tok:
            continue
        problem = _token_problem(tok)
        if problem:
            sys.stderr.write(f"⚠️  Ignoring {src} access token: {problem}\n")
            sys.stderr.flush()
            continue
        out.append((src, tok, _jwt_exp(tok)))
    # Once a refresh chain exists, its session token wins even over a
    # longer-lived env token: using it means the chain renews every few
    # days and never sits idle long enough to lapse. Otherwise freshest
    # expiry first; unknown expiry sorts last but is still tried.
    chain = bool(sess.get("refresh_token"))
    out.sort(key=lambda c: (chain and c[0] == "session", c[2] if c[2] is not None else 0), reverse=True)
    return out


def _try_access_token():
    """Pick the freshest stored access token and validate it with a single
    lightweight GET.

    This is deliberately NOT gated by the login cooldown: it's a token
    check, not a login attempt, and isn't the operation that risks
    tripping Robinhood's abuse detection. Gating it would mean a freshly
    supplied valid token couldn't be used until an unrelated cooldown from
    a previous credentials failure expired. Returns (ok, msg); ok=False
    means "no usable token, fall through to refresh / credentials".
    """
    candidates = _access_candidates()
    if not candidates:
        return False, "No usable access token (session or ROBINHOOD_ACCESS_TOKEN)"

    # With a refresh chain, a dead session token means "refresh now" — not
    # "fall back to the env token". Falling back left the chain unused from
    # Oct 7 until the env token expired on Oct 30, by which point it was gone.
    chain = bool(_load_session().get("refresh_token"))
    now = time.time()
    for src, token, exp in candidates:
        if exp is not None and exp <= now:
            sys.stderr.write(f"⚠️  {src} access token expired {((now - exp) / 3600):.1f}h ago\n")
            sys.stderr.flush()
            if src == "session" and chain:
                ok, msg = _try_refresh(reason="session-expired")
                if ok:
                    return True, msg
            continue
        _set_auth_header(token)
        try:
            v = r.helper.SESSION.get(
                "https://api.robinhood.com/user/", timeout=8,
                headers={"Origin": "https://robinhood.com"},
            )
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            # Genuine network blip — assume token is OK and proceed.
            sys.stderr.write(f"  (token validation skipped: {e})\n")
            sys.stderr.flush()
            return True, f"Logged in with {src} access token (unvalidated)"
        if v.status_code == 401:
            sys.stderr.write(f"⚠️  {src} access token rejected (401)\n")
            sys.stderr.flush()
            if src == "session" and chain:
                # Header still holds the revoked token, so _try_refresh won't
                # mistake it for one another process just refreshed.
                ok, msg = _try_refresh(reason="session-401")
                if ok:
                    return True, msg
            _clear_auth_header()
            continue
        if src == "env" and not chain and _load_session().get("access_token") != token:
            # Adopt a newly pasted env token into the session store so its
            # expiry is tracked. Never over a refresh chain (that would park
            # the chain behind the env token), and only when it changed —
            # rewriting on every process start is what raced.
            _store_tokens(token, source="env")
        return True, f"Logged in with {src} access token"
    _clear_auth_header()
    return False, "All access tokens expired/invalid"


def _access_seconds_left():
    exp = _jwt_exp(_strip_bearer(r.helper.SESSION.headers.get("Authorization", "")))
    return None if exp is None else exp - time.time()


def _refresh_if_expiring(reason):
    """Renew ahead of expiry. A failed refresh leaves the current (still
    valid) Authorization header untouched."""
    left = _access_seconds_left()
    if left is not None and left < _REFRESH_AHEAD_SEC:
        _try_refresh(reason=f"{reason}-ahead")


_KEEP_WARM_EVERY_SEC = float(os.getenv("ROBINHOOD_KEEP_WARM_SEC", str(2 * 86400)))
_KEEP_WARM_CHECK_SEC = 3600.0


def keep_warm():
    """Background loop: renew the stored chain every couple of days even when
    no MCP client connects. A refresh token left unused past its lifetime
    (~8.5 days) dies, and then only a fresh browser/device login recovers.
    Checks hourly against wall-clock `saved_at` so host sleep doesn't
    stretch the interval."""
    sys.stderr.write(f"♨️  keep-warm started (every {_KEEP_WARM_EVERY_SEC / 3600:.0f}h)\n")
    sys.stderr.flush()
    while True:
        try:
            sess = _load_session()
            age = time.time() - (sess.get("saved_at") or 0)
            if sess.get("refresh_token") and age >= _KEEP_WARM_EVERY_SEC:
                # Present the stored token as ours so _try_refresh renews it
                # instead of "picking up" what looks like another process's.
                if sess.get("access_token"):
                    _set_auth_header(sess["access_token"])
                ok, msg = _try_refresh(reason="keep-warm")
                sys.stderr.write(f"♨️  keep-warm: {msg}\n")
                sys.stderr.flush()
        except Exception as e:
            sys.stderr.write(f"⚠️  keep-warm error: {e}\n")
            sys.stderr.flush()
        time.sleep(_KEEP_WARM_CHECK_SEC)


def _try_credentials_login():
    """Full username/password (+ optional TOTP) login via robin_stocks.

    This is the EXPENSIVE, risky path — a real auth POST to Robinhood that,
    without ROBINHOOD_MFA_SECRET, triggers a device-approval push to the
    mobile app. This is the path `_LOGIN_MIN_GAP_SEC` exists to protect; a
    fresh token is issued + persisted to /root/.tokens/ (robin_stocks session file) on
    success.
    """
    user = os.getenv("ROBINHOOD_USER")
    password = os.getenv("ROBINHOOD_PASSWORD")
    mfa_secret = os.getenv("ROBINHOOD_MFA_SECRET")

    if not user or not password:
        return False, "Missing credentials"

    # Wipe any dead Authorization header so robin_stocks doesn't reuse an
    # expired Bearer when its login() fires off internal API calls.
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
        # robin_stocks answers SMS/email challenges with input(), which here
        # would read the next JSON-RPC request off stdin as the "code". Fail
        # that challenge loudly instead of corrupting the command stream.
        import builtins
        old_input = builtins.input

        def _no_input(prompt=""):
            raise RuntimeError(
                "Robinhood issued an SMS/email code challenge, which can't be "
                "answered non-interactively. Log in once on robinhood.com, or "
                "set ROBINHOOD_REFRESH_TOKEN."
            )

        builtins.input = _no_input
        try:
            if mfa_secret and mfa_secret.strip():
                totp = pyotp.TOTP(mfa_secret).now()
                login_result = r.login(user, password, mfa_code=totp)
            else:
                login_result = r.login(user, password)
        finally:
            sys.stdout = old_stdout
            builtins.input = old_input
        captured_text = captured.getvalue()

        # Detect silent failure. robin_stocks' r.login() does NOT
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
        # Keep the refresh token so the next expiry renews silently instead
        # of needing another device approval.
        _store_tokens(
            login_result["access_token"],
            login_result.get("refresh_token"),
            _load_lib_tokens().get("device_token"),
            source="credentials",
        )
        return True, "Logged in"
    except Exception as e:
        return False, str(e)


def login(force_credentials=False):
    """Authenticate with Robinhood.

    Tries the cheap `ROBINHOOD_ACCESS_TOKEN` fast path first (unless
    `force_credentials=True`, e.g. the 401-recovery path where we already
    know the token is dead) and falls back to the full user/password flow
    otherwise.
    """
    if not force_credentials:
        ok, msg = _try_access_token()
        if ok:
            return True, msg
    ok, msg = _try_refresh(reason="login")
    if ok:
        return True, msg
    return _try_credentials_login()


def maybe_login(reason="manual", force_credentials=False):
    """Rate-limited wrapper around the EXPENSIVE credentials login path.

    The cheap `ROBINHOOD_ACCESS_TOKEN` validation (a single GET, not a
    login POST) always runs first via `_try_access_token()` and is never
    gated — see that function's docstring for why. Only the full
    username/password fallback is gated.

    `_LOGIN_MIN_GAP_SEC` is a hard floor between any two *credentials*
    login attempts — success or failure, any `reason` — persisted to disk
    so it holds across process respawns, not just one process's lifetime.
    Returns (ok, msg, was_cached).

    `force_credentials=True` (used by the 401-recovery path) skips the
    token fast path entirely (we already know it's dead). Before the
    gated credentials flow, a refresh-token renewal is tried — it needs
    no password or phone approval, so it's the normal way an expired
    session recovers. Only if that fails does the credentials flow run,
    once the gap has elapsed — it does NOT bypass the gap itself. If
    MFA_SECRET is unset this triggers Robinhood's device-approval push to
    the mobile app; if that approval never happens, the resulting failure
    still respects the same floor before the next attempt (and the next push).
    """
    global _last_login_ts, _last_login_success, _last_login_msg

    if not force_credentials:
        token_ok, token_msg = _try_access_token()
        if token_ok:
            sys.stderr.write(f"✅ Login ok (token fast-path, reason={reason}): {token_msg}\n")
            sys.stderr.flush()
            _refresh_if_expiring(reason)
            return True, token_msg, False

    refresh_ok, refresh_msg = _try_refresh(reason=reason)
    if refresh_ok:
        return True, refresh_msg, False

    _load_login_state()

    now = time.time()
    elapsed = now - _last_login_ts

    if _last_login_ts > 0 and elapsed < _LOGIN_MIN_GAP_SEC:
        sys.stderr.write(
            f"🔁 Credentials login skipped (reason={reason}, cached {_last_login_msg!r}, "
            f"{elapsed:.1f}s ago, min_gap={_LOGIN_MIN_GAP_SEC:.0f}s)\n"
        )
        sys.stderr.flush()
        return _last_login_success, _last_login_msg, True

    sys.stderr.write(
        f"🔑 Credentials login attempt (reason={reason}) at t={now:.1f}\n"
    )
    sys.stderr.flush()

    ok, msg = _try_credentials_login()

    _last_login_ts = time.time()
    _last_login_success = ok
    _last_login_msg = msg
    _save_login_state()

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

# Stash the REAL stdout at module load time. Several robin_stocks paths
# (pickle loading, login verification, options pagination, etc.) print
# diagnostics to whatever sys.stdout currently is. We swap sys.stdout to
# an in-memory buffer while running their code and emit to this stash
# directly so error/status text never corrupts our JSON-RPC stream.
_REAL_STDOUT = sys.stdout


def _emit(payload):
    """Write a single JSON response line to the REAL stdout (bypassing any
    swapped-in suppression buffer)."""
    try:
        _REAL_STDOUT.write(json.dumps(payload) + "\n")
        _REAL_STDOUT.flush()
    except BrokenPipeError:
        # Parent (Node MCP) has gone away. Stop quietly.
        _close_session()
        os._exit(0)


def _handle_command_quiet(request):
    """Run handle_command with stdout fully captured so library noise
    (robin_stocks pickle-load errors, login prints, etc.) cannot leak
    into the JSON-RPC stream. _emit always writes to _REAL_STDOUT."""
    with _suppress_stdout():
        return handle_command(request)


def main():
    # Same suppression for the startup login — pickle-load errors print
    # to stdout from inside robin_stocks before our login() wrapper even
    # gets a chance.
    with _suppress_stdout():
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

            # The process lives for days; renew before the token lapses
            # mid-session rather than waiting for a 401.
            with _suppress_stdout():
                _refresh_if_expiring("command")

            try:
                response_data = _handle_command_quiet(request)
                _emit({"id": request_id, "status": "success", "data": response_data})
                continue
            except Exception as e:
                error_str = str(e)

            # Auto re-login on any 401 (Unauthorized, JWT verification failed,
            # token expired, etc.) and retry once. The access token we hold is
            # known-dead, so skip it: maybe_login tries a silent refresh-token
            # renewal first, and only if that fails falls back to user/password
            # (which triggers Robinhood's device-approval push). If maybe_login
            # returns a cached result, retrying would hit the same 401, so fail
            # fast instead.
            if "401" in error_str:
                with _suppress_stdout():
                    ok, msg, cached = maybe_login(
                        reason="401-recovery", force_credentials=True
                    )
                if ok and not cached:
                    try:
                        response_data = _handle_command_quiet(request)
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
    if "--keep-warm" in sys.argv:
        keep_warm()
    else:
        main()
