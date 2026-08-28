# Alternative Free Sandbox Comparison — Indian Equity Trading APIs

**Date:** 2026-08-24 · **Context:** Implementation Gate follow-up after Dhan sandbox was found write-only with synthetic data (`DHAN_SANDBOX_VERIFICATION.md`). Dhan is **excluded** from this evaluation per project decision.

**Marker legend:**
✅ VERIFIED BY ACTUAL TEST · 🟡 DOCUMENTED BUT NOT TESTED · ❌ UNSUPPORTED · ? UNKNOWN

---

## 1. Research summary (official sources only)

| Provider | Dedicated sandbox? | Evidence |
|---|---|---|
| **Upstox** | **YES — official, free, dedicated host** | Official docs: [upstox.com/developer/api-documentation/sandbox](https://upstox.com/developer/api-documentation/sandbox); dedicated host `api-sandbox.upstox.com` (confirmed in [official SDK source](https://github.com/upstox/upstox-python/blob/master/upstox_client/configuration.py)); announcement page |
| Zerodha | **NO** | No sandbox anywhere in [Kite Connect v3 docs](https://kite.trade/docs/connect/v3); ecosystem sources confirm none exists ("Zerodha doesn't offer a sandbox"). Free Personal API tier = orders-only against production account — **not** a sandbox substitute |
| Fyers | **NO** | No sandbox/paper mode in [API v3 docs](https://myapi.fyers.in/docsv3); community thread "Paper Trading environment for Fyers API" unanswered by Fyers |
| Angel One | **NO** | No sandbox in SmartAPI docs ([smartapi.angelone.in/docs](https://smartapi.angelone.in/docs)) |
| 5paisa Xstream | **NO** | Free API ([xstream.5paisa.com](https://xstream.5paisa.com)) with full lifecycle — but all against production; no test environment mentioned in dev docs |
| Alice Blue | **NO** | ANT API docs/community show production-only |
| Shoonya / Finvasia | **? UNCONFIRMED** | Official FAQ lists only live endpoints (`api.shoonya.com/NorenWClientTP`). A third-party integration (Zorro manual) claims a Finvasia "Sandbox endpoint" demo mode — **no official documentation found**. Treated as unverified claim |
| Kotak Neo / ICICI Breeze / Groww | **?** | No evidence of sandboxes found in official docs reviewed |

Broader search found no other credible SEBI-regulated Indian broker offering a genuine order-lifecycle sandbox.

---

## 2. Capability matrix

| Provider | Free | Auth | Place | Query | Modify | Cancel | Orders hist | Trades | Positions | Funds | Fills sim | WebSocket | Realism |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| **Upstox (sandbox)** | ✅ free, 30-day token, 24/7 | 🟡 | 🟡 | ❌/🟡† | 🟡 | 🟡 | ❌/🟡† | ❌ | ❌ | ❌ | ?† | ❌ | ?† |
| Zerodha | orders-free tier 🟡 | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | ❌ | ❌ | ❌ no sandbox |
| Fyers | API free | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ no sandbox |
| Angel One | API free | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ no sandbox |
| 5paisa | API free | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ no sandbox |
| Alice Blue | ? | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ no sandbox |
| Shoonya | API free 🟡 | ? | ? | ? | ? | ? | ? | ? | ? | ? | ❌ | ? | ? unconfirmed third-party claim |

† **The critical open question for Upstox:** official docs' sandbox-enabled list contains ONLY the seven write endpoints (`/v2|v3/order/place|modify|cancel`, `/v2/order/multi/place`) — mirrored exactly by the SDK's client-side `sandbox_urls` whitelist, which raises *"This API is not available in sandbox mode"* for anything else. HOWEVER, Upstox's own announcement states sandbox orders remain active 24h and developers can "retrieve associated data (order details, orderbook, and historical records)". These two statements conflict; only a live call with a sandbox token against `https://api-sandbox.upstox.com/v2/order/book` settles whether READS work server-side even without the doc flag.

## 3. What Upstox's sandbox officially promises

From official pages (documented, not yet tested):
- One free sandbox app per user; token generated directly from the developer portal (**no OAuth login flow needed** — big automation win), valid **30 days**
- Available **24/7**, not restricted to market hours
- Orders persist for a **full 24-hour cycle** enabling place→modify→cancel→retrieve sequences
- Validation rules claimed **identical to live** ("authentic validation protocols")
- **No funds required** to place orders
- Sandbox tokens cannot touch live trading (clean isolation)
- Official Python SDK supports it natively: `Configuration(sandbox=True)`
- Limitations: redirect/postback URLs non-functional in sandbox; phased rollout — read-side APIs not yet flagged

## 4. Verification status & what remains

**Not yet executed:** live lifecycle test. Reason: generating a sandbox token requires logging into an Upstox brokerage account at `account.upstox.com/developer/apps#sandbox` → *New Sandbox App* → *Generate* (KYC-gated; cannot be automated without the account holder). Test script `scripts/upstox_sandbox_verify.sh` is ready (below) and will run the moment a token is provided:

```
GET  https://api-sandbox.upstox.com/v2/profile            → does profile resolve?
POST /v2/order/place   (deep-off-market LIMIT BUY)        → orderId?
GET  /v2/order/book                                     → order visible?          ← decisive
GET  /v2/order/details?order_id=...                     ← decisive
PUT  /v2/order/modify                                    → status change?
DELETE /v2/order/cancel                                 → cancelled?
GET  /v2/order/book (recheck)                           → final state?
GET  /v2/trades  · GET /v2/portfolio/short-term-positions · GET /v2/user/funds  ← bonus probes
POST /v2/order/place (MARKET order)                      → accepted/rejected/MPP?
```

## 5. Recommendation

### Primary Sandbox
**Upstox API Sandbox** — the only official, free, dedicated, isolated sandbox offered by any major Indian broker. Even if reads turn out to be blocked, its write-side (place/modify/cancel with live-identical validation, 24/7, no funds) validates request/response contracts, error codes, rate-limit handling, and our OrderManager idempotency logic against production-shaped payloads — strictly more than any other provider offers. Read-side gaps would be covered by our internal PaperBroker (which owns fill/state simulation anyway, per ARCHITECTURE_V1.1 §1.3).

### Backup Sandbox #1
**None exists among Indian brokers.** Honest answer: if Upstox fails verification, the fallback is our internal PaperBroker + a micro-live smoke stage (₹1-share minimum-price LIMIT orders) as specified in the amended Phase 11. No second Indian sandbox exists to name.

### Backup Sandbox #2
Same as above. (Shoonya's rumored demo endpoint is the only lead worth a support-ticket query before abandoning, given Shoonya's otherwise free full-lifecycle API.)

## 6. Architecture implication

Already satisfied by design (ARCHITECTURE_V1.0 §13, unchanged):

```
Trading Engine → Broker interface → PaperBroker | SandboxBroker(Upstox) | LiveBroker(...)
```

Strategy/risk layers contain zero broker references (enforced by import-linter rule). New adapter requirement added: adapters must declare a capability matrix (what their backend can do) so the engine can degrade gracefully — e.g., SandboxBroker(Upstox) declares `query=false` and the engine relies on local journal state instead of broker truth during sandbox runs.

## 7. Gate statement

> **SANDBOX GATE: READY — VERIFIED (2026-08-24).** Alpaca Paper Only Account created (email-only signup, no KYC) and the complete order lifecycle was executed live against `paper-api.alpaca.markets`: auth ✅ place ✅ query ✅ modify ✅ cancel ✅ history ✅ simulated fills ✅ positions ✅ funds ✅ (details in `NO_KYC_BUILD_PATH.md` §4). Upstox remains the recommended first *Indian* adapter target post-KYC; its write-side sandbox then covers Indian field-mapping parity before micro-live.
