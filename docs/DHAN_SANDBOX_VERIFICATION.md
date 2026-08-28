# DhanHQ Sandbox Verification — Implementation Gate #1

**Date:** 2026-08-24 (IST evening, post-market-hours)
**Sandbox base:** `https://sandbox.dhan.co/v2` · **Production endpoints:** NOT touched
**Credentials:** user-provided sandbox JWT (exp 2026-09-23 16:43 UTC) + Client ID `2608248137` — stored in gitignored `.env`; **not reproduced here**.

---

## Results Summary

| Capability | Verdict |
|---|---|
| Authentication | **PASS** |
| Orders GET | **UNSUPPORTED** (application-level stub errors) |
| Account/client identification | **PASS** (`GET /profile`) |
| Market data | **UNSUPPORTED** (404 — endpoint not routed in sandbox) |
| Historical data | **FAIL** (endpoint responds 200 but returns canned dummy data regardless of symbol/dates) |
| Sandbox order placement | **PASS** |
| Sandbox order query | **UNSUPPORTED** |
| Sandbox order cancellation | **UNSUPPORTED** |
| Super Order | **UNAVAILABLE** (404 — not routed) |
| Static IP requirement | **UNVERIFIED** (`/ip/getIP` is 404 in sandbox; production-only API) |

## Detailed Findings

### Authentication — PASS

- Endpoint: `GET https://sandbox.dhan.co/v2/profile`
- Method: GET · Headers: `access-token: <JWT>` (only)
- HTTP 200
- Response: `{"dhanClientId":"2608248137","tokenValidity":"23/09/2026 22:13","activeSegment":"Equity, Derivative, Currency, Commodity","ddpi":"Deactive","dataPlan":"Deactive"}`
- Result: JWT accepted; identity resolvable; **`dataPlan` = `Deactive`** (no production Data-API subscription on this account — relevant below).
- Limitation: none observed.

### Orders GET — UNSUPPORTED

- Endpoint: `GET /orders`, `GET /orders/{order-id}`, `GET /orders/external/{correlation-id}`
- Headers tried: `access-token` alone; plus `client-id`; plus `Content-Type`
- HTTP 500 every time: `{"errorType":"Order_Error","errorCode":"DH-906","errorMessage":"Incorrect request for order and cannot be processed"}`
- Interpretation: identical DH-906 for well-formed book/single/correlation queries immediately after successful placements ⇒ this resource is a **stub that does not implement retrieval**, not a payload problem. Classified UNSUPPORTED rather than FAIL per task rules.

### Account/client identification — PASS

- Via `GET /profile` (above). Funds check `GET /fundlimit` → HTTP 500 `{"errorType":"FUND_LIMIT_ERROR",...}` ⇒ funds/margin querying is **unsupported in sandbox**.

### Market data — UNSUPPORTED

- `POST /marketfeed/ltp` → HTTP 404 `{"status":404,"error":"Not Found","path":"/v2/marketfeed/ltp"}`
- LTP/OHLC/quote endpoints are not routed on the sandbox host at all. Live quote simulation cannot be tested here.

### Historical data — FAIL (as a research data source)

Endpoints `POST /charts/historical` and `POST /charts/intraday` respond HTTP 200 — **but return an identical canned payload for every input**:

Evidence (MD5 of response bodies):

```
charts/historical securityId=1594(INFY) fromDate=2015-01-01  → md5 ed21ff194267aac2f69619e4fd88862b
charts/historical securityId=2885(RELIANCE) 2000–2005        → md5 ed21ff194267aac2f69619e4fd88862b   (identical)
charts/intraday  TCS interval=5 2026-08 week                 → same arrays as both above
```

Payload analysis: 9 daily-shaped bars, prices ~130–134, epoch timestamps `1738813500…1738903500` = **2025-02-06 UTC** regardless of requested range; volume returned as floats.

Therefore, against the sandbox, we could NOT verify:
- oldest available date (docs claim "back to inception" daily / 5 years intraday — **unverified**),
- real intervals behavior (docs: 1/5/15/25/60-min; unverified),
- max range per call (docs note ~90 days per intraday call; unverified),
- corporate-action adjustment (**unverified anywhere — remains Open Question**),
- rate-limit behavior (docs: Data APIs 5/s, 100k/day; unverified),
- instrument coverage (unverified).

The 5-year-minute-history claim from ARCHITECTURE_V1.1 §3 comes from official release notes only and must be re-verified against the **production** Data API once a subscription is active.

### Sandbox order placement — PASS

- Endpoint: `POST /orders`
- Working minimal body (HTTP 200):
```json
{"dhanClientId":"2608248137","correlationId":"gate1a","transactionType":"BUY",
 "exchangeSegment":"NSE_EQ","productType":"INTRADAY","orderType":"LIMIT",
 "validity":"DAY","securityId":"11536","quantity":1,"price":10.05,
 "triggerPrice":0,"afterMarketOrder":false}
```
- Response: `{"orderId":"712608242009","orderStatus":"TRANSIT"}` (second test order `...019`)
- Notes:
  - Omitting optional fields (`disclosedQuantity`, `amoTime`, `bo*`) works; including `"disclosedQuantity":""` (string-empty, per doc example) causes `DH-905 Input_Exception` (400). **Docs' sample payloads are unreliable; use typed values.**
  - A **MARKET order was ACCEPTED** by the sandbox (`orderId ...019`). The production policy documented for Mar 2026 ("API market orders converted to LIMIT with MPP") is **not emulated** in sandbox.
  - `CNC` + `afterMarketOrder=true` rejected at 22:20 IST: `DH-906 "Markets are Offline or Blocked"` — CNC path appears market-session-bound even in sandbox; INTRADAY was not.
  - Deep-below-market LIMIT chosen deliberately so no fill ambiguity arises.

### Sandbox order query / cancellation — UNSUPPORTED

Full intended lifecycle `place → query → cancel → verify` **could not be completed**:

```
place   POST   /orders                    → 200 orderId=712608242009 TRANSIT
query   GET    /orders/{id}               → 500 DH-906
query   GET    /orders                    → 500 DH-906
cancel  DELETE /orders/{id}               → 500 DH-906
verify  GET    /orders/{id}               → 500 DH-906
lookup  GET    /orders/external/gate1a    → 500 DH-906
```

Both placed orders remain unverifiable and non-cancellable through the API. No tradebook either (`GET /trades` → 500 `TRADE_RESOURCE_ERROR`), positions (`GET /positions` → 500 `CONVERT_POSITION_ERROR`).

### Super Order — UNAVAILABLE

- `POST /super/orders` → HTTP 404 `path:/v2/super/orders`. Not routed in sandbox. Entry+target+SL bracket functionality **cannot be validated pre-live here**. Production availability per docs stands, unverified.

### Static IP requirement — UNVERIFIED

- `GET /ip/getIP` → HTTP 404 on sandbox. The IP-management APIs appear production-only. Docs (official) state order-placement APIs require whitelisted static IP since 2026-04-01; whether the *sandbox* enforces it: **it did not block our placements** from a residential dynamic IP — i.e., sandbox does not emulate the static-IP gate. Production enforcement still expected; UNVERIFIED.

## Current API limitations (sandbox)

1. Order lifecycle is write-only: place works; read/modify/cancel do not exist functionally.
2. No market data, no Super Orders, no IP APIs, no funds/positions/trades.
3. Historical charts serve one fixed dummy dataset — useless for research or integration testing.
4. Doc-sample payloads contain type errors (string-typed numbers) that themselves trigger rejections.
5. Sandbox does not emulate session gating uniformly (INTRADAY accepted post-hours; CNC not), nor MPP conversion, nor static-IP enforcement — i.e., **it validates almost nothing about production risk/compliance behavior**.

## Exact commands/scripts used

Script: `scripts/dhan_sandbox_verify.sh` (committed; sources `.env`; prints responses + HTTP codes to `/tmp/dhan_evidence/`). Representative redacted calls:

```bash
BASE=https://sandbox.dhan.co/v2
curl -s -w '\n%{http_code}' "$BASE/profile" -H "access-token: $TOK"
curl -s -w '\n%{http_code}' "$BASE/orders" -H "access-token: $TOK"
curl -s -X POST "$BASE/marketfeed/ltp" -H "access-token: $TOK" -H "client-id: $CID" \
     -H 'Content-Type: application/json' -d '{"NSE_EQ":[2885,11536]}'
curl -s -X POST "$BASE/charts/historical" -H "access-token: $TOK" \
     -H 'Content-Type: application/json' \
     -d '{"securityId":"2885","exchangeSegment":"NSE_EQ","instrument":"EQUITY",
          "expiryCode":0,"oi":false,"fromDate":"2000-01-01","toDate":"2005-01-01"}'
curl -s -X POST "$BASE/orders" -H "access-token: $TOK" -H 'Content-Type: application/json' \
     -d '{"dhanClientId":"'"$CID"'","transactionType":"BUY","exchangeSegment":"NSE_EQ",
          "productType":"INTRADAY","orderType":"LIMIT","validity":"DAY",
          "securityId":"11536","quantity":1,"price":10.05,"triggerPrice":0,
          "afterMarketOrder":false}'
curl -s -X DELETE "$BASE/orders/712608242009" -H "access-token: $TOK"
```

Two sandbox orders created (`712608242009`, `712608242019`, deep-off-market LIMIT BUY TCS qty 1); neither executable harm; cancellation unavailable (documented above). No production endpoint called; no real-money anything.

## Official documentation sources

- Auth/IP/TOTP/profile: <https://dhanhq.co/docs/v2/authentication/>
- Orders: <https://dhanhq.co/docs/v2/orders/>
- Historical data: <https://dhanhq.co/docs/v2/historical-data/> (intraday intervals; ~90-day poll limit note; 5-year claim in release notes v2.2)
- Market Quote: <https://dhanhq.co/docs/v2/market-quote/> (client-id header requirement; 1000-instrument cap; 1 rps)
- Releases (static-IP/MPP/rate-limit dates): <https://dhanhq.co/docs/v2/releases/>

## Architecture discrepancies discovered

| # | Discrepancy vs ARCHITECTURE_V1.1 | Impact |
|---|---|---|
| D1 | v1.1 assumed sandbox enables "adapter parity testing" incl. order lifecycle. Reality: place-only. | Phase 8/10 sandbox-parity stage is impossible as designed; adapter correctness must be validated another way |
| D2 | v1.1 cited Dhan's 5-year minute history as a primary reason for broker choice. Sandbox serves canned data; production claim unverified; account `dataPlan: Deactive`. | Research-data plan needs a production Data-API subscription decision (cost) OR fallback sources; broker-choice rationale partially contingent again |
| D3 | Sandbox accepted a raw MARKET order (production converts to LIMIT+MPP per docs). | Confirms sandbox ≠ production compliance behavior; our limit-only execution discipline must be enforced in OUR code, never assumed from broker-side conversion |
| D4 | Doc sample payloads are type-invalid (string quantities). | SDK/adapter layer must use strictly typed serialization; add contract tests |
| D5 | CNC rejected post-hours while INTRADAY accepted. | Product-type/session interactions need production verification; V1 uses CNC-equivalent delivery semantics in PaperBroker regardless |

## Required architecture changes (minimal)

1. **Phase 8 revision:** replace "sandbox parity tests" with (a) internal PaperBroker lifecycle tests (unchanged), (b) a thin `DhanSandboxSmokeTest` covering what IS possible: auth refresh, profile, order-place acceptance + correlationId generation, (c) **first-time order-lifecycle validation deferred into Phase 11 micro-live** using 1-share minimum-price LIMIT orders, with kill-switch and ₹-cap interlocks already specified.
2. **Data foundation (Phase 1) decoupled from Dhan:** proceed with free historical sources (e.g., yfinance/NSE archives) for research bootstrap; treat Dhan production Data API as an upgrade once subscribed. Add explicit decision point: subscribe (~₹499/mo reported; verify) before experiment start if minute-history depth proves necessary.
3. **Compliance posture hardened:** since neither sandbox nor docs can prove runtime MPP/tagging behavior, the adapter MUST send LIMIT-only by construction (already v1.0 rule — now load-bearing, elevate from convention to hard assertion with a unit test rejecting MARKET intents).
4. **Contract tests:** encode observed request/response schemas (incl. DH-905 typing pitfalls) as fixtures so future API drift is detected.

---

## Final verdict

> **Implementation Gate #1: BLOCKED**

Exact reason: sandbox credentials authenticate and accept order placements, but the sandbox does not implement order query/cancel/order-book, offers no market data, serves only synthetic historical data, and lacks Super Order and static-IP endpoints. The gate requirement — *"Dhan sandbox credentials verified working"* as a basis for validating the Broker-adapter lifecycle paper→live — cannot be satisfied by this environment. Partially verified: authentication ✅, client identification ✅, placement acceptance ✅.

Unblocking options (any one):
1. Dhan confirms (support ticket) whether full order-lifecycle testing exists elsewhere in their developer flow;
2. Accept deferral of lifecycle verification to Phase 11 micro-live under the amended plan above (gate then closes via option 2 approval);
3. Re-run this verification against a different broker sandbox (none currently offered by Zerodha/Angel/Fyers/Upstox — see v1.1 §3).

Main implementation remains **not started**, per instructions.
