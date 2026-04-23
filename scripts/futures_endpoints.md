# Robinhood Legend — Reverse-Engineered Futures Endpoints

Source: HAR capture `HAR/robinhood.com.har` (16 MB, 405 entries) from Robinhood Legend web session on 2026-04-22.

## Symbology

Three ID types matter for futures:

| ID | Format | Source | Example |
|---|---|---|---|
| `rhsAccountNumber` | numeric | account number (also `rhfAccountNumber`) | `725878037` |
| Ceres account ID (FUTURES) | UUID | `/ceres/v1/accounts?rhsAccountNumber=…` → pick `accountType=FUTURES` | `67f68786-2755-4aa1-9e2c-edd25f8f99f0` |
| `productId` | UUID | `atc_schedule` response | `fae079ee-…` → `/MNQ` |
| `futuresContractId` | UUID | `atc_schedule.results[].futuresContractId` | `64cda3df-…` → `/MNQM26` |
| `symbol` | string | `/MNQM26`, `/MESZ25`, `/MBTJ26` | — |
| `productSymbol` | string | root: `/MNQ`, `/MBT`, `/ES` | — |

**Gotcha:** `/ceres/v1/accounts` returns TWO entries per user — `accountType: "SWAP"` and `accountType: "FUTURES"`. For futures calls, always use the FUTURES record's `id`.

Auth: standard `Authorization: Bearer <token>` — no custom Legend-only headers needed. Origin must be `https://robinhood.com` (some endpoints CORS-check this).

## Confirmed endpoints (from HAR **and** live-probed against session, all verified 200 OK)

### Account + eligibility
| Method | Path | Notes |
|---|---|---|
| `GET` | `/ceres/v1/accounts?rhsAccountNumber={rhs}` | List accounts; pick `accountType=FUTURES` for futures calls |
| `GET` | `/ceres/v1/futures_account_eligibility/{rhs}` | Returns `{"status": "ALREADY_HAS_ACCOUNT"}` when enabled |
| `GET` | `/futures/{rhs}/futures_buying_power/` | `buying_power`, `futures_buying_power`, `futures_equity`, `futures_margin_requirement`, plus swaps fields |
| `GET` | `/accounts/{rhs}/buying_power_breakdown` | Cross-asset BP breakdown with `Futures equity` + `Futures margin held` |

### Product + Contract discovery
| Method | Path | Notes |
|---|---|---|
| `GET` | `/arsenal/v1/futures/products` | **ALL futures products** (66KB response) — `id`, `combinedCommodityId`, `symbol` (e.g. `/ETH:XCME`), `displaySymbol` (`/ETH`), `description`, `country`, `exchange`, `currency`, `futureSubType`, `underlyingAsset`, `delivery`, etc. |
| `GET` | `/arsenal/v1/futures/products/{pid}` | Single product metadata |
| `GET` | `/arsenal/v1/futures/contracts/{cid}` | **Single contract details** — `productId`, `symbol` (`/ETHJ26:XCME`), `displaySymbol`, `description`, **`multiplier`**, `expirationMmy`, `expiration`, `customerLastCloseDate`, `tradability`, `state` |
| `GET` | `/arsenal/v1/futures/atc_schedule?productIds={pid}` | All expirations for a product: `futuresContractId`, `symbol`, `startDate`, `endDate`, `rollTime`, `productId` |

### Positions
| Method | Path | Notes |
|---|---|---|
| `GET` | `/ceres/v1/accounts/{ceresId}/aggregated_positions` | All futures positions (currently empty when flat) |
| `GET` | `/ceres/v1/accounts/{ceresId}/contract_quantities?contractIds={cid,cid,...}` | Per-contract: `heldQuantity`, `pendingQuantity`, `netQuantity` |
| `GET` | `/ceres/v1/accounts/{ceresId}/pnl_cost_basis` | Futures P&L/cost basis |

### Orders
| Method | Path | Notes |
|---|---|---|
| `GET` | `/wormhole/bw/orders/recent?accountNumber={rhs}` | **Full order history incl. futures legs, refId, averagePrice, fees, commission, gold savings, realizedPnl, positionEffect, derivedState** |

### Portfolio
| Method | Path | Notes |
|---|---|---|
| `GET` | `/portfolios/v2/performance/summary?rhsAccountNumber={rhs}&spans=SPAN_1D&metrics=...` | Includes `futuresMarketValue`, `futuresCash`, returns |

## Futures order schema (from `wormhole/bw/orders/recent`)

Example captured filled futures order:

```json
{
  "id": "69e8fa96-a5c6-462f-99ce-bbdb5df6d34b",
  "assetType": "FUTURES",
  "type": "MARKET",
  "submittedAt": "2026-04-22T16:43:02.799599Z",
  "quantity": "1",
  "timeInForce": "GFD",
  "filledQuantity": "1",
  "derivedState": "FILLED",
  "futuresOrder": {
    "legs": [{
      "legId": "A",
      "contractId": "0bb2b320-…",
      "symbol": "/MBTJ26",
      "lastDayToTrade": "2026-04-24T00:00:00Z",
      "side": "SELL",
      "ratioQuantity": 1,
      "productSymbol": "/MBT"
    }],
    "rhdAccountId": "67f68786-…",
    "trigger": "IMMEDIATE",
    "refId": "ec8b301d-…",
    "averagePrice": "78910",
    "totalFee": {"amount": "1.67", "currency_code": "USD"},
    "totalCommission": {"amount": "0.5", "currency_code": "USD"},
    "totalGoldSavings": {"amount": "0.25", "currency_code": "USD"},
    "realizedPnl": {"amount": "-63.17", "currency_code": "USD"},
    "positionEffect": "CLOSE"
  }
}
```

Enums observed: `assetType=FUTURES|EQUITY|OPTION|CRYPTO`; `type=MARKET|LIMIT`; `side=BUY|SELL`; `timeInForce=GFD|GTC`; `trigger=IMMEDIATE|STOP`; `positionEffect=OPEN|CLOSE`; `derivedState=FILLED|CANCELLED|QUEUED|CONFIRMED`.

### Trading (all confirmed via 2nd HAR with real placed+cancelled order)

| Method | Path | Notes |
|---|---|---|
| `POST` | `/ceres/v1/orders` | **Place order.** See body schema below. Returns `orderId`, `orderState`, `derivedState`. |
| `POST` | `/ceres/v1/orders/{orderId}/cancel` | **Cancel order.** Body: `{"accountId": "<ceres-id>"}`. Response: `{"requestStatus": "SUCCESS"}`. |
| `POST` | `/ceres/v1/accounts/buying_power_effect` | **Margin preview** (Legend calls this repeatedly as qty/price change). See body schema below. |
| `POST` | `/ceres/v1/accounts/{ceresId}/order_checks` | Pre-submit validation — same `orderInfo` shape as place-order. |
| `GET` | `/arsenal/v1/futures/margin_requirement?contractId={cid}&marginType=MARGIN_TYPE_OVERNIGHT&accountType=ACCOUNT_TYPE_MARGIN_NO_LIMIT` | Per-contract margin ($). Other marginType likely: `MARGIN_TYPE_DAY`. |
| `GET` | `/wormhole/bw/orders/{orderId}?assetType=FUTURES&rhsAccountNumber={rhs}` | Single order details (post-submit lookup). |

#### `POST /ceres/v1/orders` body (verified):
```json
{
  "accountId": "<ceres FUTURES uuid>",
  "legs": [{
    "contractType": "OUTRIGHT",
    "contractId": "<futuresContractId uuid>",
    "ratioQuantity": 1,
    "orderSide": "BUY"
  }],
  "quantity": "2",
  "orderType": "LIMIT",
  "orderTrigger": "IMMEDIATE",
  "timeInForce": "GFD",
  "limitPrice": "0.5",
  "refId": "<client-generated uuid>"
}
```
Enums observed: `orderSide=BUY|SELL`; `orderType=LIMIT|MARKET` (others exist); `orderTrigger=IMMEDIATE|STOP`; `timeInForce=GFD|GTC`; `contractType=OUTRIGHT` (spread types presumably).

#### `POST /ceres/v1/accounts/buying_power_effect` body:
```json
{
  "accountId": "<ceres id>",
  "tentativeFuturesOrder": {
    "legs": [{"contractType":"OUTRIGHT","contractId":"<cid>","ratioQuantity":1,"orderSide":"BUY"}],
    "quantity": "2"
  }
}
```
Returns: `buyingPowerEffect`, `totalFee`, `totalCommission`, `totalGoldSavings`, `estimatedMarginRequired`, `staleData`.

## Still MISSING (deferred — likely non-HAR-capturable streaming)

| Need | Status |
|---|---|
| Futures **L1 quote** (bid/ask/last) | All REST probes 404. Zero WS upgrades in 3 HARs. Almost certainly delivered via **gRPC-Web / Protocol Buffers** over HTTP/2 which Chrome HAR export does NOT capture. |
| Futures **historicals / OHLC bars** | Same — not in any HAR |
| **L2 / DOM depth** | Same — not in any HAR |

For v1.2, workaround: use per-contract `margin_requirement` + the last traded price from `wormhole/bw/orders/recent` order history. For live quotes, defer to v1.3 (requires mitmproxy + SSL-pin bypass, or a different capture tool that sees HTTP/2 streams like Wireshark with SSLKEYLOGFILE).

## Known non-futures / UI-only namespaces (skip these)

- `/hippo/bw/*` — Legend UI widget/layout state; no market data
- `/goku/*`, `/observability/*` — analytics/telemetry
- `/bw/config`, `/bw/refresh` — Legend session config
- `/discovery/lists/*` — marketing lists (trending, movers)
- `/midlands/notifications/*` — in-app notifications
