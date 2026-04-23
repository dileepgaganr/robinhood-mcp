#!/usr/bin/env python3
"""
Probe Robinhood's undocumented futures endpoints against a live authenticated session.

Prereqs: the same auth env vars used by rh_wrapper.py must be set
(ROBINHOOD_ACCESS_TOKEN, or ROBINHOOD_USER + ROBINHOOD_PASSWORD + optional
ROBINHOOD_MFA_SECRET). Run this from the robinhood-mcp/ directory:

    python3 scripts/futures_probe.py

READ-ONLY: this script never POSTs, never cancels, never places orders.
It only hits GET endpoints to verify what we found in the HAR works against
the current session AND speculatively tries the missing patterns.
"""
from __future__ import annotations
import json
import os
import sys
from pathlib import Path

# Allow importing rh_wrapper (its login helpers) from the parent dir.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import robin_stocks.robinhood as r  # noqa: E402
import rh_wrapper  # noqa: E402


API = "https://api.robinhood.com"
BONFIRE = "https://bonfire.robinhood.com"
ORIGIN_HEADERS = {"Origin": "https://robinhood.com", "Referer": "https://robinhood.com/"}


def _truncate(obj, n=600):
    s = json.dumps(obj, indent=2) if isinstance(obj, (dict, list)) else str(obj)
    return s[:n] + ("…\n[truncated]" if len(s) > n else "")


def _probe(method, url, *, label, params=None, extra_headers=None):
    sess = r.helper.SESSION
    headers = {**ORIGIN_HEADERS, **(extra_headers or {})}
    try:
        resp = sess.request(method, url, params=params, headers=headers, timeout=10)
    except Exception as e:
        print(f"  [ERR ] {label}: {e}")
        return None
    status = resp.status_code
    ctype = resp.headers.get("content-type", "?").split(";")[0]
    size = len(resp.content)
    marker = "[OK  ]" if 200 <= status < 300 else "[FAIL]"
    print(f"  {marker} {status} {ctype} {size}B  {label}")
    try:
        body = resp.json()
    except Exception:
        body = resp.text
    if 200 <= status < 300:
        print(f"         sample: {_truncate(body, 400)}")
    else:
        print(f"         body: {_truncate(body, 200)}")
    return body if 200 <= status < 300 else None


def main():
    print("=" * 72)
    print("Robinhood futures endpoint probe")
    print("=" * 72)

    ok, msg, _ = rh_wrapper.maybe_login(reason="probe")
    if not ok:
        print(f"Login failed: {msg}")
        sys.exit(1)
    print(f"Auth: {msg}")

    # 1) Get the RHS account number from the existing account profile.
    print("\n--- 1. Account discovery ---")
    try:
        acct_profile = r.profiles.load_account_profile()
        rhs = acct_profile.get("account_number")
        print(f"  rhsAccountNumber: {rhs}")
    except Exception as e:
        print(f"  ERR: could not load account profile: {e}")
        sys.exit(1)

    # 2) Get Ceres accounts (two will come back — SWAP and FUTURES).
    print("\n--- 2. Ceres accounts (pick FUTURES type) ---")
    ceres_accts = _probe("GET", f"{API}/ceres/v1/accounts",
                         params={"rhsAccountNumber": rhs},
                         label="/ceres/v1/accounts")
    ceres_futures_id = None
    if ceres_accts and "results" in ceres_accts:
        for a in ceres_accts["results"]:
            if a.get("accountType") == "FUTURES":
                ceres_futures_id = a["id"]
                print(f"  FUTURES ceres id: {ceres_futures_id}")
                break
    if not ceres_futures_id:
        print("  WARN: no FUTURES ceres account found; some probes will be skipped.")

    # 3) Eligibility + BP (confirmed in HAR).
    print("\n--- 3. Eligibility + buying power ---")
    _probe("GET", f"{API}/ceres/v1/futures_account_eligibility/{rhs}",
           label="/ceres/v1/futures_account_eligibility/{rhs}")
    _probe("GET", f"{API}/futures/{rhs}/futures_buying_power/",
           label="/futures/{rhs}/futures_buying_power/")
    _probe("GET", f"{API}/accounts/{rhs}/buying_power_breakdown",
           label="/accounts/{rhs}/buying_power_breakdown")

    # 4) Discover an active futures product and contract to use as probe targets.
    #    We need to learn productIds from somewhere. Try some guessed listing endpoints.
    print("\n--- 4. Futures product/contract discovery (speculative) ---")
    probe_pid = None
    probe_cid = None
    for guess in [
        f"{API}/arsenal/v1/futures/products",
        f"{API}/arsenal/v1/futures/products/",
        f"{API}/arsenal/v1/futures/product_list",
        f"{API}/arsenal/v1/futures/symbols",
        f"{API}/arsenal/v1/futures/contracts",
        f"{API}/arsenal/v1/futures/contracts/",
    ]:
        body = _probe("GET", guess, label=guess.replace(API, "<api>"))
        if body and isinstance(body, dict):
            res = body.get("results") or body.get("products") or body.get("contracts")
            if res and isinstance(res, list) and res:
                first = res[0]
                probe_pid = first.get("productId") or first.get("id")
                probe_cid = first.get("futuresContractId") or first.get("id")
                if probe_pid or probe_cid:
                    print(f"    -> using pid={probe_pid} cid={probe_cid}")
                    break

    # Fallback: use known /MNQ productId from the HAR.
    if not probe_pid:
        probe_pid = "fae079ee-e816-401b-8bb9-b45e31accb1a"  # /MNQ
        print(f"  falling back to known /MNQ pid: {probe_pid}")

    # 5) atc_schedule (confirmed in HAR).
    print("\n--- 5. atc_schedule (confirmed) ---")
    sched = _probe("GET", f"{API}/arsenal/v1/futures/atc_schedule",
                   params={"productIds": probe_pid},
                   label="/arsenal/v1/futures/atc_schedule")
    if sched and isinstance(sched, dict) and sched.get("results"):
        # Prefer an active (front-month) contract.
        active = [c for c in sched["results"]
                  if c.get("startDate", "") <= "2026-04-22" <= c.get("endDate", "9999")]
        pick = active[0] if active else sched["results"][0]
        probe_cid = pick["futuresContractId"]
        probe_symbol = pick["symbol"]
        print(f"  active contract from atc: {probe_symbol} ({probe_cid})")

    # 6) Aggregated positions + contract_quantities (confirmed).
    if ceres_futures_id:
        print("\n--- 6. Positions (confirmed) ---")
        _probe("GET", f"{API}/ceres/v1/accounts/{ceres_futures_id}/aggregated_positions",
               label="/ceres/v1/accounts/{ceres}/aggregated_positions")
        if probe_cid:
            _probe("GET", f"{API}/ceres/v1/accounts/{ceres_futures_id}/contract_quantities",
                   params={"contractIds": probe_cid},
                   label="/ceres/v1/accounts/{ceres}/contract_quantities")
        _probe("GET", f"{API}/ceres/v1/accounts/{ceres_futures_id}/pnl_cost_basis",
               label="/ceres/v1/accounts/{ceres}/pnl_cost_basis")

    # 7) Orders (confirmed).
    print("\n--- 7. Orders (confirmed) ---")
    _probe("GET", f"{API}/wormhole/bw/orders/recent",
           params={"accountNumber": rhs},
           label="/wormhole/bw/orders/recent")

    # 8) SPECULATIVE: L1 futures quote.
    print("\n--- 8. Futures L1 quote (speculative) ---")
    if probe_cid:
        for guess_url, params in [
            (f"{API}/arsenal/v1/futures/quotes", {"contractIds": probe_cid}),
            (f"{API}/arsenal/v1/futures/quotes/", {"contractIds": probe_cid}),
            (f"{API}/arsenal/v1/futures/marketdata/quotes", {"contractIds": probe_cid}),
            (f"{API}/marketdata/futures/quotes/", {"ids": probe_cid}),
            (f"{API}/marketdata/futures/quotes/", {"contractIds": probe_cid}),
            (f"{API}/futures/marketdata/quotes/", {"ids": probe_cid}),
            (f"{API}/futures/quotes/", {"ids": probe_cid}),
            (f"{API}/futures/quotes/{probe_cid}/", None),
            (f"{API}/marketdata/futures/", {"contractIds": probe_cid}),
            (f"{API}/ceres/v1/futures/quotes", {"contractIds": probe_cid}),
            (f"{BONFIRE}/futures/quotes/", {"contractIds": probe_cid}),
        ]:
            _probe("GET", guess_url, params=params,
                   label=guess_url.replace(API, "<api>").replace(BONFIRE, "<bonfire>"))

    # 9) SPECULATIVE: contract details (tick size, multiplier).
    print("\n--- 9. Contract details (speculative) ---")
    if probe_cid:
        for url in [
            f"{API}/arsenal/v1/futures/contracts/{probe_cid}",
            f"{API}/arsenal/v1/futures/contracts/{probe_cid}/",
            f"{API}/ceres/v1/contracts/{probe_cid}",
            f"{API}/futures/contracts/{probe_cid}/",
        ]:
            _probe("GET", url, label=url.replace(API, "<api>"))

    # 10) SPECULATIVE: product details.
    print("\n--- 10. Product details (speculative) ---")
    if probe_pid:
        for url in [
            f"{API}/arsenal/v1/futures/products/{probe_pid}",
            f"{API}/arsenal/v1/futures/products/{probe_pid}/",
            f"{API}/futures/products/{probe_pid}/",
        ]:
            _probe("GET", url, label=url.replace(API, "<api>"))

    # 11) SPECULATIVE: historicals / chart bars.
    print("\n--- 11. Historicals (speculative) ---")
    if probe_cid:
        common_params = {"interval": "hour", "span": "week", "bounds": "24_7"}
        for guess_url, params in [
            (f"{API}/arsenal/v1/futures/historicals", {"contractIds": probe_cid, **common_params}),
            (f"{API}/arsenal/v1/futures/historicals/", {"contractIds": probe_cid, **common_params}),
            (f"{API}/arsenal/v1/futures/historicals/{probe_cid}/", common_params),
            (f"{API}/marketdata/futures/historicals/", {"ids": probe_cid, **common_params}),
            (f"{API}/marketdata/futures/historicals/{probe_cid}/", common_params),
            (f"{API}/midlands/futures/historicals/{probe_cid}/", common_params),
            (f"{API}/futures/historicals/{probe_cid}/", common_params),
        ]:
            _probe("GET", guess_url, params=params,
                   label=guess_url.replace(API, "<api>"))

    # 12) SPECULATIVE: L2 / DOM depth snapshot (unlikely to exist as REST, but worth trying).
    print("\n--- 12. L2 depth snapshot (speculative, likely WS-only) ---")
    if probe_cid:
        for url, params in [
            (f"{API}/arsenal/v1/futures/depth", {"contractIds": probe_cid}),
            (f"{API}/arsenal/v1/futures/depth/{probe_cid}", None),
            (f"{API}/marketdata/futures/depth/", {"ids": probe_cid}),
            (f"{API}/futures/depth/{probe_cid}/", None),
            (f"{API}/futures/ladder/{probe_cid}/", None),
        ]:
            _probe("GET", url, params=params, label=url.replace(API, "<api>"))

    print("\n" + "=" * 72)
    print("Probe complete. Review output above — any [OK] line under §8-§12 is a discovery.")
    print("=" * 72)


if __name__ == "__main__":
    main()
