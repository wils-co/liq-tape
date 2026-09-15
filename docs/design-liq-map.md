# Design: Hyperliquid Liquidation Map (`liqmap`)

Factual design note for the Hyperliquid liquidation mapping subsystem (`liqmap`) in `liq-tape` (PR9) and trigger layers (PR11). Facts only; no implementation code, no trading advice.

---

## 1. Address Universe

### Endpoint & Source of Truth
* **Info Endpoint (`POST /info`):** **UNVERIFIED / NON-EXISTENT**. The official Hyperliquid `/info` API specification ([Hyperliquid Info API](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint)) does not expose a `type: "leaderboard"` request variant. Queries such as `{"type": "leaderboard"}` fail JSON deserialization.
* **Public Dataset Distribution SSOT:** Hyperliquid publishes the complete live leaderboard dataset via its public S3/CloudFront storage endpoint:
  * **URL:** `GET https://stats-data.hyperliquid.xyz/Mainnet/leaderboard`
  * **Method:** `GET` (CORS enabled: `Vary: Origin`, Content-Type: `application/json`)
  * **Payload Schema:** Top-level object containing a single key `leaderboardRows`, which holds an array of ~45,000+ trader objects:
    ```json
    {
      "leaderboardRows": [
        {
          "ethAddress": "0x85ecf584f25db6f146718b86d493e33c5af72052",
          "accountValue": "58886736.8004489988",
          "windowPerformances": [
            ["day", {"pnl": "-457781.835819", "roi": "-0.007097688", "vlm": "1600540987.02"}],
            ["week", {"pnl": "-2392772.072231", "roi": "-0.040795198", "vlm": "9474188043.18"}],
            ["month", {"pnl": "-352636.895616", "roi": "-0.004487820", "vlm": "40396924896.26"}],
            ["allTime", {"pnl": "4027517.001309", "roi": "0.034281934", "vlm": "193965906566.59"}]
          ],
          "prize": 0,
          "displayName": null
        }
      ]
    }
    ```
  * **Payload Size:** ~37 MB.

### Sort Key & Ranking Definition
* **Sort Key:** `accountValue` (parsed as a floating-point number).
* **Rank Meaning:** `accountValue` measures current equity (collateral + unrealized PnL) in USD. It reflects total capital at risk / whale balance. It does **not** reflect distance to liquidation, position leverage, or open volume.
* **Pagination:** None. The endpoint returns all ~45,000+ accounts in one payload.
* **Extraction of Top 200:** Filter and sort `leaderboardRows` descending by `float(row["accountValue"])`, slice the first 200 items `[:200]`, and extract the `ethAddress` values.
* **Polling & Caching Strategy:** Because the file is ~37 MB, it must **never** be downloaded on every 120s tick. The dataset updates on S3 periodically (monitored via `Last-Modified` / `ETag`). The helper client must cache the resolved 200 addresses on local disk with a TTL (recommended: 1 hour to 24 hours), or refresh conditionally with HTTP `If-Modified-Since`.

### Subaccounts
* **Inclusion:** Every entry in `leaderboardRows` is a distinct 42-character Ethereum address (`ethAddress`).
* **Protocol Rule:** On Hyperliquid, subaccounts have distinct on-chain addresses. Per official documentation ([User Address](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint#user-address)), querying account data requires the specific 42-char address of that subaccount or master account.
* **State Behavior:** If a subaccount holds sufficient equity to place in the top 200, its address appears on the leaderboard and is queried directly. Master and subaccounts are treated as independent addresses by `/info`.

---

## 2. Per-Address State

### Endpoint & Request Schema
* **URL:** `POST https://api.hyperliquid.xyz/info`
* **Docs URL:** [Perpetuals Account Summary](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals#retrieve-users-perpetuals-account-summary)
* **Request Body:**
  ```json
  {
    "type": "clearinghouseState",
    "user": "0x5b5d51203a0f9079f8aeb098a6523a13f298c060"
  }
  ```
  *(Optional parameter: `"dex": ""` defaults to the primary perpetual DEX).*

### Response Schema
Key fields from the returned clearinghouse state object:
* `marginSummary`: `{accountValue, totalMarginUsed, totalNtlPos, totalRawUsd}`
* `crossMarginSummary`: `{accountValue, totalMarginUsed, totalNtlPos, totalRawUsd}`
* `crossMaintenanceMarginUsed`: string representation of maintenance margin threshold.
* `assetPositions`: Array of position objects:
  * `type`: `"oneWay"`
  * `position`:
    * `coin`: string (e.g. `"BTC"`)
    * `szi`: signed position size (string; positive = long, negative = short, `"0.0"` = flat)
    * `entryPx`: string
    * `positionValue`: string
    * `unrealizedPnl`: string
    * `returnOnEquity`: string
    * `liquidationPx`: string or `null`
    * `marginUsed`: string
    * `maxLeverage`: integer
    * `leverage`: `{"type": "cross" | "isolated", "value": int, "rawUsd": string}`

### Rate Limits & Batching
* **Docs URL:** [Rate limits and user limits](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/rate-limits-and-user-limits)
* **Request Weight:** `clearinghouseState` has a documented weight of **2** per request.
* **IP Rate Limit:** Aggregated REST limit of **1,200 weight per minute**.
* **Tick Weight Budget:**
  * 200 addresses × 2 weight = **400 weight per cycle**.
  * Over a 120s window (2 minutes), the total allowed IP weight budget is 2,400 weight.
  * 400 weight represents **16.7%** of the 120s budget (or 33.3% of 1 minute), well clear of 429 thresholds.
* **Batching:** `clearinghouseState` cannot batch multiple user addresses into a single HTTP request. Each address requires an individual HTTP POST.
* **Recommended Pool Size:**
  * Concurrency pool of **4 to 8 worker threads** (e.g. `ThreadPoolExecutor(max_workers=5)` using persistent HTTP keep-alive connections).
  * At 5 concurrent workers and an average HTTP round-trip of 150ms: 200 requests complete in `(200 / 5) * 150ms` = **~6 seconds**.
  * This fits cleanly inside a single 120s poll without connection starvation or bursting IP rate limits.

---

## 3. `liquidationPx`

### Field Mechanics
* **Exchange-Provided Field:** `liquidationPx` is calculated and provided directly by the Hyperliquid clearinghouse engine. **Do not recompute**.
* **Reasoning:** In cross-margin accounts, the exact liquidation threshold is a multivariate function of total cross equity, maintenance margin scaling tiers across all assets in the account, and unrealized PnL of other correlated positions. Recomputing it from single-market heuristics produces inaccurate figures.

### When `liquidationPx` is a Number
* Returned as an exact floating-point string (e.g. `"134488.1355979328"`).
* Occurs when the account has a definite price point at which maintenance margin requirements exceed remaining cross or isolated equity.
* For short positions (`szi < 0`), because upside price is unbounded ($+\infty$), an account with net short exposure almost always produces a numeric liquidation price.

### When `liquidationPx` is `null`
Empirical testing on live whale accounts confirms four distinct scenarios where `liquidationPx` is `null`:
1. **Unliquidatable Cross Long:** A long position (`szi > 0`) held in an account where cross collateral / equity is so large that even if the asset price drops to zero ($0.00), total account value remains above `crossMaintenanceMarginUsed`. Because an asset price cannot drop below zero, liquidation is mathematically impossible.
2. **Fully Collateralized / Low-Leverage Isolated Long:** An isolated long position where allocated margin equals or exceeds position value (e.g. 1x isolated or extra margin transferred into the position).
3. **Zero / Closed Position:** When `szi` is `"0.0"` or `"0"`, or the position is closed.
4. **No Debt / Spot-Like Exposure:** Positions where margin maintenance is zero.

### Row Filtering Rules for `liqmap`
* **Zero Position:** If `float(szi) == 0.0` or missing: **drop the row**.
* **Null or Non-Numeric:** If `liquidation_px is None` or non-numeric: **drop the row** (cannot be mapped to a price coordinate).
* **Coin Mismatch:** If `coin != target_coin`: **drop the row in-client**.

---

## 4. Row Shape for `liqmap`

### Confirmed Subprocess JSON Output
The proposed payload structure is fully sufficient for `server.py` clustering, bucket aggregation, and UI rendering:

```json
{
  "coin": "BTC",
  "asof_ms": 1773561600000,
  "requested": 200,
  "fetched": 198,
  "capped": true,
  "positions": [
    {
      "address": "0x5b5d51203a0f9079f8aeb098a6523a13f298c060",
      "coin": "BTC",
      "szi": -1891.38073,
      "liquidation_px": 134488.14,
      "position_value": 180348987.0
    }
  ]
}
```

### Protocol Fields & Handling
* `coin`: Normalized market string (e.g. `"BTC"`). Client drops all positions in other coins before emitting JSON.
* `asof_ms`: Unix millisecond timestamp at the end of the collection cycle.
* `requested`: Count of target leaderboard addresses (default: 200).
* `fetched`: Count of addresses successfully returned by `clearinghouseState`.
* `capped`: Boolean flag. If `fetched < requested` (due to network drops, individual timeouts, or 429 retries), set `capped: true`; otherwise `false`.
* `positions`: Array of valid rows:
  * `address`: Full 42-character hexadecimal string (`0x...`).
  * `coin`: Market name matching request.
  * `szi`: Signed float (positive = long, negative = short).
  * `liquidation_px`: Verified numeric float.
  * `position_value`: Float notional value in USD.

---

## 5. Triggers (PR11 Scope)

### Endpoint & Distinction of Stop vs Take-Profit
* **URL:** `POST https://api.hyperliquid.xyz/info`
* **Docs URL:** [Frontend Open Orders](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint#retrieve-a-users-open-orders-with-additional-frontend-info)
* **Request Body:**
  ```json
  {
    "type": "frontendOpenOrders",
    "user": "0x5b5d51203a0f9079f8aeb098a6523a13f298c060"
  }
  ```
* **Order Fields:**
  * `isTrigger`: Boolean (`true` for conditional trigger orders, `false` for resting limit orders).
  * `orderType`: String identifying trigger mode (e.g. `"Stop Market"`, `"Stop Limit"`, `"Take Profit Market"`, `"Take Profit Limit"`).
  * `triggerCondition`: Condition string indicating trigger direction (e.g. `"tp"`, `"sl"`, or `"triggerAbove"` / `"triggerBelow"`; non-triggers report `"N/A"`).
  * `triggerPx`: Float string representing the trigger price.
  * `isPositionTpsl`: Boolean indicating whether the order is attached directly to position TP/SL.
  * `reduceOnly`: Boolean (typically `true` for protective stops and take-profit orders).
  * *Exchange Protocol Reference:* [Exchange Endpoint Actions](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/exchange-endpoint#modify-multiple-orders) defines order trigger types explicitly as `"tpsl": "tp" | "sl"`.

### Weight Comparison & Architecture Requirement
* **Weight Disparity:**
  * `clearinghouseState` has weight **2**.
  * `frontendOpenOrders` is not on the weight-2 exemption list and has weight **20** ([Rate limits](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/rate-limits-and-user-limits)).
  * 200 addresses × 20 weight = **4,000 weight**.
  * Aggregated REST rate limit is **1,200 weight per minute**. Querying 200 addresses with `frontendOpenOrders` requires at least 3.33 minutes (200s) to avoid 429 rejection.
* **Payload Size Overhead:**
  * Active whale and market-maker accounts maintain large open order books (empirically observed up to 1,770+ active orders in a single wallet).
  * Serializing and transferring thousands of open order structs per poll creates significant payload bloat compared to position states.
* **Separation Requirement:**
  * Triggers **must be a separate command** (e.g. `python3 hyperliquid_client.py triggers <coin> --top 200 --json`) or run on a dedicated slower cadence (e.g. 300s / 5 minutes).
  * Triggers must **never** run inline inside the 120s `liqmap` poll. An order rate-limit or payload delay must not block or starve `liqmap` or the 12s OI sampler.

---

## 6. Honesty & Boundaries

### What the Top 200 Captures
* Whale accounts, institutional positions, and major named liquidity providers.
* High-notional liquidation levels capable of triggering significant platform margin calls or market-maker book impact.

### What the Top 200 Does Not Capture
* Crowded mid-size retail leverage (e.g. $10k–$500k accounts operating at 20x–50x leverage). These accounts are absent from the account-value top 200, despite often providing the rapid cascade fuel during volatility pulses.
* The remaining ~45,000+ active accounts on the venue.

### Methodology & UI Doctrine
* The UI and API must explicitly label coverage as `N of 200 largest accounts`, **never** "the market", "all liquidations", or "total leverage".
* Must be documented as: "Real `liquidationPx` from tracked universe · age · Hyperliquid only · not modeled open interest".
* Standing clusters reflect open positions at the latest poll; swept clusters reflect levels crossed by mark price within the TTL window.
