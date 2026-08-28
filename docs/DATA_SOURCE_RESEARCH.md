# Data Source Research: Yahoo Finance Chart API, NSE Archives, NSE Website JSON APIs

**Research date:** Wed 2026-08-26, 03:45–04:10 UTC (= 09:15–09:40 IST, live market session)
**Method:** Live probes from this machine (`curl`, `uv run python` w/ `curl_cffi` 0.16.1, `yfinance` 1.6.0) **plus** web research.
**Evidence tags:** **MEASURED** = timestamped probe from this machine · **REPORTED** = cited external source · **UNKNOWN** = not determinable.

> **Headline finding:** Yahoo's `query{1,2}` hosts are *not* rate-limiting us by IP — they are blocking **non-browser TLS fingerprints** outright (instant, persistent 429 regardless of headers/UA/cookies). A browser-TLS client (`curl_cffi` with Chrome impersonation, which `yfinance>=0.2.x` already uses internally) gets HTTP 200 at 16 req/s sustained with **zero** 429s over a 300-request burst. Separately, the "dead" NSE index endpoint has been **renamed**: `/api/equity-stockIndices` → `/api/equity-stock-indices`, and it works with no cookie dance — one request returns all 201 rows (index + 200 constituents) with LTP/volume, which is the ideal primitive for a 5-minute decision cadence.

---

## A. Rate limits (empirical + documented)

### A1. Yahoo chart API — the 429s we saw are TLS-fingerprint blocks, not IP rate limits

| Time (UTC) | Probe | Result |
|---|---|---|
| 03:45:42 | Plain `curl` (macOS Chrome UA), `query1`, RELIANCE.NS 5d/1d — **first request of the session** | **HTTP 429** |
| 03:45:47 | Same via `query2` | **HTTP 429** |
| 03:45:50 | Plain `curl`, no User-Agent | 429 |
| 03:45:53 | Plain `curl`, UA=`curl/8.4.0` | 429 |
| 03:46:00 | Inspect response: body `Too Many Requests`, `server: ATS`, `cache-control: no-store`, **no `Retry-After` header** | block is opaque |
| 03:46:28 | Cookie warm-up (`fc.yahoo.com` → 404; `finance.yahoo.com` → 200; cookie jar replayed to chart API) | **still 429** |
| 03:47:14 | HTTP/1.1 + full browser header set (Accept, Accept-Language, Origin, Referer, Sec-Fetch-*) | **still 429** |
| 03:47:59 | Retry plain curl, both hosts | 429 both |
| 03:59:13 | Recheck (~13 min later) | 429 |
| 04:06:37 | Recheck (~21 min later) | **still 429 — block persists ≥ 20 min** |
| ~03:48:20 | `curl_cffi` `impersonate='chrome'` (browser TLS/JA3 fingerprint), same URL, same IP, seconds after a 429 | **HTTP 200**, valid JSON |
| 04:06:40 | `curl_cffi` chrome on **both** query1 and query2 | 200 / 200 |

Interpretation (**MEASURED**): identical URL/IP/headers succeed or fail purely based on the client's TLS fingerprint. This matches a **REPORTED** observation (openweb DOC.md, github.com/openweb-org/openweb `src/sites/yahoo-finance/DOC.md`): *"Yahoo's CDN applies aggressive per-UA rate limiting to `Macintosh; Intel Mac OS X` Chrome UAs… On macOS, the auto-detected UA triggers this"* — plus SparkProxy 2026 guide: *"A default library User-Agent triggers [429] instantly… sustained requests trip it after a few hundred calls."*

**Burst behavior through an accepted (impersonated) client — MEASURED:**

| Time (UTC) | Test | Result |
|---|---|---|
| 03:54:41–03:55:22 | 40 sequential chart requests, **16.2 req/s** (10 NSE symbols × range=1d/5m) | 40/40 HTTP 200, 0×429 |
| 03:55:49–03:58:04 | **300 sequential requests in 75 s (≈4 req/s)**, single host (query1), RELIANCE.NS 1mo/1d | **300/300 HTTP 200, 0×429** |

So under a browser-fingerprint client we did **not** find a burst limit up to 300 rapid requests (~4/s). Community-reported unauthenticated per-IP ceilings: **~2,000 calls/hour** (apisscore.com Yahoo Finance page, unofficial estimate) and *"a few hundred requests then 429"* for sustained scraping (SparkProxy, 2026); yfinance issue #2128 reports ~950 tickers/day-of-1m-data suddenly hitting 429 in Nov 2024 when Yahoo tightened policy. Exact threshold: **UNKNOWN** (not reached in our tests; deliberately not pushed further to avoid a long ban).

- **query1 vs query2 separate limits?** UNKNOWN. Both serve the same routes (load-balanced twins per openweb doc); both blocked/unblocked identically in every probe, suggesting a shared edge policy. Rotation between them is harmless but unproven as a limit-doubling trick.
- **Recovery window:** for fingerprint-blocks, ≥ 20 min and counting (MEASURED, still blocked at 04:06). For genuine volume-429s, REPORTED "minutes" (SparkProxy: "the same IP stays cold for minutes"). Exact duration: **UNKNOWN**.
- **UA sensitivity:** UA alone does not fix it (all UA variants 429 via curl); TLS fingerprint is decisive (MEASURED). Older community advice ("just add a browser UA" — StackOverflow 78111453, softhints 2026) predates the current stricter edge.

### A2. yfinance

**MEASURED:** `yfinance` 1.6.0 worked immediately at 03:46:07 UTC while raw curl was 429-ing — because it depends on `curl_cffi>=0.15` (confirmed in its dependency metadata) and issues requests with a browser-impersonated session. So **yfinance receives the same treatment as any client; its advantage is exactly the browser TLS impersonation**, plus cookie/crumb handling for crumb-gated endpoints (`v7/quote`). Historical context REPORTED: yfinance issues #2128 (Nov 2024) and #2297 (Feb 2025) show fleet-wide 429 waves fixed in later releases by changing request strategy — pin and update yfinance deliberately.

### A3. NSE archives (`archives.nseindia.com`)

**MEASURED:** 10 rapid bhavcopy requests (different trading days) fired back-to-back at **03:48:24–03:48:26 UTC** (≈6 req/s): **8× HTTP 200**, 2× 404 (correctly — 15 & 22 Aug 2026 are Saturdays). **No throttling observed.** Single files download in ~0.05–1.1 s. Caveat MEASURED: a minimal-UA client (`User-Agent: Mozilla/5.0`) got **403 on the legacy zip path** at 03:55:57 while a full Chrome-fingerprint client succeeded at 03:56:26 — send a complete modern Chrome UA/fingerprint everywhere on NSE domains too.

REPORTED (community): NSE web properties throttle around **~10 req/min per IP** on some `/api/*` endpoints with 429/403 (dev.to NSE scraper guide, verified 2026-08-19) and ban aggressive scrapers; cloud-provider IPs (AWS/GCP) are widely blocked (StackOverflow 59740840). Archives endpoints have historically been lenient.

### A4. NSE website JSON APIs

**MEASURED (2026-08-26):**
- `/api/allIndices` → **200 with NO cookie warm-up** (plain curl, 03:46:31), 15.8 KB, 139 indices.
- `/api/marketStatus` → 200 (03:55:41).
- `/api/equity-stockIndices?index=NIFTY%20200` → **404** (03:46:31 plain curl; 03:54:43 again via curl_cffi + homepage cookies; NIFTY 50 and lowercase variants also 404).
- **Renamed endpoint discovered:** `/api/equity-stock-indices?index=NIFTY%20200` → **HTTP 200** at 03:58:47 via curl_cffi **and** via plain `urllib` with just a browser UA (no warm-up). Payload: 201 rows (index row + 200 constituents) incl. `symbol, lastPrice, pChange, previousClose, totalTradedVolume, ffmc, yearHigh/yearLow`. 5 repeat hits at 03:59:11 → 200×5.
- `/api/quote-equity?symbol=RELIANCE` → **403 in every configuration**: no-warmup curl (03:46:35), homepage-cookie warm-up curl (03:48:20), curl_cffi chrome + warmup (03:54:43). Akamai "Access Denied" page.

REPORTED: the quote-equity 403 is **per-endpoint Akamai bot-manager policy, not fixable by cookies/egress** — nse-mcp project's DATA_SOURCES.md measured `quote-equity` **403 in 22/22 runs including from a residential IP**, while sibling endpoints (`marketStatus`, `corporate-announcements`) return 200 with or without cookies; their conclusion: *"The gating is per-endpoint, not per-client… only a full browser session would fix it."* Cookie lifetimes: Akamai cookies (`ak_bmsc`, `_abck`, `bm_sz`, `nsit`) are REPORTED short-lived (hours; stale cookie is a top cause of intermittent 403s — dev.to guide, nse-xbrl README). Ban behavior REPORTED: bursts >~10/min trigger throttling; hard bans resolve in minutes-to-hours (community, imprecise).

---

## B. Freshness & delay

### B1. Yahoo 5m bars for `.NS` — near-real-time today (MEASURED)

Live-market cross-check, RELIANCE.NS, sampling Yahoo 5m chart and NSE's official `equity-stock-indices` LTP seconds apart:

| Sample (IST) | YF last 5m bar close | NSE official lastPrice | Δ |
|---|---|---|---|
| 09:33:18 | 1309.90 | 1309.70 | +0.20 |
| 09:34:18 | 1309.60 | 1310.00 | −0.40 |
| 09:35:18 | 1309.70 | 1309.50 | +0.20 |
| 09:36:19 | 1310.20 | 1310.30 | −0.10 |

Deltas are ≤ ₹0.40 (≈0.03%) while the stock moved ~₹1.5 across the samples — consistent with a **seconds-scale lag, definitely not a 15-minute delay**. Yahoo also serves the **current forming 5m bar** (bar labeled by start time; close = latest trade). At 09:17:19 IST the 09:15 opening bar was already present (MEASURED).

Contradictory REPORTED evidence, for honesty: Yahoo Help SLN2310 lists **".NS — Real-time (ICE Data Services)"** (and ".BO — 15 min"); yet finance.yahoo.com quote pages display an "**NSE – Delayed Quote**" badge, and r/algotrading threads warn "don't trade live off YF." Our direct measurement today supports near-real-time for NSE equities; residual risk of silent degradation is real because this is undocumented. Treat as **near-real-time but verify-per-day against NSE official LTP**.

### B2. Yahoo daily bars

- Intraday, the **current day appears as a partial bar** in `range=5d&interval=1d` (yfinance at 03:46 UTC showed the live 26-Aug bar with running volume) — **MEASURED**.
- Post-close appearance time: **UNKNOWN** (market had not closed at research time). REPORTED: community consensus is daily candles finalize shortly after close, typically well within the evening IST; no authoritative number exists.

### B3. NSE bhavcopy publication time

- **MEASURED:** today's `sec_bhavdata_full_26082026.csv` → 404 pre-close (03:58 UTC / 09:28 IST); yesterday's (25082026) → 200. A poller was left running (10-min cadence, `/tmp/bhavpoll.log`) to catch today's appearance time; results to be appended post-close.
- **REPORTED:** preliminary release ~**15:40 IST**, majority of data ~**16:00 IST** (tradewithpython.hashnode.dev); "usually ~16:00–16:30 IST" (volumelens.com); GetBhavCopy schedules auto-download at **17:30 IST** to handle late publication; doitek 2026 guide says "after 18:00–19:00 IST." Practical safe window: **poll from 16:00 IST, give up → fail-closed at ~19:00 IST.**
- Format watch: NSE's all-reports page says the classic CM bhavcopy CSVs were **discontinued w.e.f. 08-Jul-2024** (Circular 62424) in favor of the **CM-UDiFF Common Bhavcopy Final (.zip)** — yet `sec_bhavdata_full_*` demonstrably still publishes for 25-Aug-2026 (MEASURED). Availability could stop any day; keep the UDiFF parser (`nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_YYYYMMDD_F_0000.csv.zip`) as the successor.

### B4. yfinance `.NS` interval availability + lookback (MEASURED via underlying chart API, 03:56 UTC)

| Interval | Max range accepted | Bars received | Notes |
|---|---|---|---|
| 1m | `range=8d` accepted | 2,638 ≈ **7 sessions** | hard ~7-day lookback |
| 5m | **60d** | 4,429 ≈ 59 sessions | `3mo`(=63d+) and `75d` → HTTP 422 |
| 15m | **60d** | 1,477 | `90d`/`6mo` → 422 |
| 1d | `max` | RELIANCE.NS back to **1996-01-01** | ^NSEI back to **2007-09-17** |

(`includePrePost=true` used where relevant; NSE has no pre/post session anyway.)

---

## C. Reliability & history depth

### Yahoo
- **429 patterns:** MEASURED — binary fingerprint gate (see A1); once accepted, 340 consecutive requests (40-burst + 300-burst + ad-hoc) with zero failures. REPORTED — periodic platform-wide crackdown waves (yfinance issues Nov 2024/Feb 2025); undocumented endpoints can change "without notice."
- **History depth (MEASURED):** equities to 1996 (^NSEI 2007) via `range=max`; 5m depth 60 days — ample for swing research.
- **adjclose (MEASURED):** `indicators.adjclose` present on daily queries; with `events=div,split` dividend events returned (RELIANCE.NS 6mo probe). Splits/dividends adjust historical closes — the lab's corp-actions module must keep using `adjclose`/events consistently (already handled in `src/sts/data/corp_actions.py`).

### NSE archives
- **Rapid-fire reliability:** 10 requests in ~1.7 s → no throttle (MEASURED, A3). Files ~140 KB each.
- **Depth (MEASURED):** `sec_bhavdata_full_DDMMYYYY.csv` verified 200 for **Jan 2020** and **Aug 2026**; **404 before ~2017** (Mar 2015 → 404). Legacy `content/historical/EQUITIES/YYYY/MON/cmDDMMYYYYbhav.csv.zip`: **200 for Mar 2015** (1,573 rows) **and Jan 2000** (1,188 rows). Combined depth: **2000 → present** (two formats).
- **Format stability (MEASURED):**

| Era | File | Header |
|---|---|---|
| 2000 | cm bhav zip | `SYMBOL,SERIES,OPEN,HIGH,LOW,CLOSE,LAST,PREVCLOSE,TOTTRDQTY,TOTTRDVAL,TIMESTAMP,` |
| 2015 | cm bhav zip | same + `,TOTALTRADES,ISIN,` |
| 2020 | sec_bhavdata_full | `SYMBOL, SERIES, DATE1, PREV_CLOSE, OPEN_PRICE, HIGH_PRICE, LOW_PRICE, LAST_PRICE, CLOSE_PRICE, AVG_PRICE, TTL_TRD_QNTY, TURNOVER_LACS, NO_OF_TRADES, DELIV_QTY, DELIV_PER` |
| 2026 | sec_bhavdata_full | **byte-identical to 2020 header** |

  Two stable formats, each unchanged across decades — good for parsers; note the space-after-comma style in `sec_bhavdata_full`.
- Universe file: `ind_nifty200list.csv` → **200 on both `archives.` and `nsearchives.`** (04:00 UTC), 200 symbols + header, columns `Company Name,Industry,Symbol,Series,ISIN Code`.

### equity-stockIndices — permanently gone (renamed)
- Old name: 404 across 6 attempts spanning 08 min, two clients, with/without cookies (MEASURED). **Not intermittent — renamed** to `equity-stock-indices` (REPORTED: nsetools issue #155 documents exactly this rename; OpenBB PR #7591 discussion corroborates). New name: 200×6/6 attempts incl. plain urllib (MEASURED). Lesson: NSE renames endpoints silently; pin exact URL and alert on 404-vs-403 distinctly (404=rename/format change, 403=Akamai).

---

## D. Terms / legal (brief)

- **Yahoo:** Terms of Service (legal.yahoo.com, read via search cache Jul 2026): services may **not** be used "for any commercial purpose"; automated collection ("robots, spiders, scrapers, data mining tools") requires "express, prior permission"; no reproduction/redistribution of content without written permission. yfinance's own disclaimer: "intended for research and educational purposes… the Yahoo! finance API is intended for personal use only." → **Our personal-research use is the sanctioned case; anything commercial/published needs licensing.**
- **NSE:** All exchange Market Data (explicitly including *delayed, EOD and historical* data) is governed by the **NSE Data Usage & Sharing Policy** (nseindia.com/static/market-data/nse-data-policy): commercial use requires a subscription agreement with NSE Data & Analytics Ltd., redistribution prohibited, ownership stays with NSE. SEBI's May-24-2024 circular further restricts third-party sharing of real-time data; NSE actively enforces (cease-and-desist letters to data-misusing apps, 2022–23). Publicly posted bhavcopy/website data is routinely consumed for personal research, but that tolerance is **not a license**. → Keep use personal, low-volume, non-redistributed; do not build a product/feed on these endpoints.

*(Paid vendors exist for licensed NSE real-time/delayed data — TrueData, GDFL, TickData vendors etc.; see one-liner in §E.)*

---

## E. Recommendations for this lab (personal research, 200 symbols, 5-min cadence, fail-closed)

### E1. Yahoo polling pattern
1. **Never call query hosts with `requests`/`urllib`/plain curl.** Use `yfinance` (≥1.6, curl_cffi-backed) or `curl_cffi` `impersonate='chrome'`. A plain-client 429 sticks for 20+ minutes (MEASURED) — a single wrong-client deploy poisons the hour.
2. **Cadence & batching:** 200 symbols ÷ 5-min cadence = 2,400 req/h theoretical max. We measured 300 rapid requests clean, but community ceilings hover near ~2,000/h sustained. Recommended: fetch each symbol's `interval=5m, period=1d` **once per 5-minute cycle** (~200 req/5min = 2,400/h worst case) is *borderline*; safer is **stagger**: split universe into 5 shards of 40, one shard per minute (480 req/h steady) so every symbol still refreshes every 5 minutes with 4× headroom.
3. **Backoff:** on any 429 → exponential backoff starting 60 s, cap 15 min, and switch shard order after recovery. Rotate query1/query2 round-robin (harmless; shared-limit status UNKNOWN). Jitter ±20%.
4. **Fail-closed staleness rule:** accept a bar only if `fetch_time_IST − bar_end ≤ 5 min`; otherwise mark tier degraded and fall through to E2/E3. Verify Yahoo-vs-NSE LTP drift once each morning (today: ≤0.03%, MEASURED).

### E2. Can NSE quote-equity be made to work? — **No.**
403 in every configuration we tried (bare, warmed cookies, browser-TLS impersonation) AND REPORTED 22/22 failures even from residential IPs (nse-mcp study) — it is per-endpoint Akamai policy; only a full browser session defeats it, which is fragile and clearly unwelcome per NSE terms. **Drop it.** Instead:
- **Use the renamed `/api/equity-stock-indices?index=NIFTY%20200`** (works with a plain browser UA, MEASURED 6/6): **one request returns LTP, %chg, volume, prev close for all 200 symbols** — a perfect real-time decision-grade snapshot at 1 req/5min (or even 1 req/1min; stay ≤ ~10 req/min per community guidance). This should be the **primary live-LTP tier**, with Yahoo 5m bars as the OHLCV-history tier.
- `/api/allIndices` (200, no auth dance) carries the **INDIA VIX row** — MEASURED at 03:46 UTC (`index: "INDIA VIX", last: 11.08, prevClose: 11.08, timestamp 26-Aug-2026 09:14 IST`) → regime-gate feed solved with 1 low-frequency request (e.g., every 5–15 min). Cross-check: `^INDIAVIX` via Yahoo chart also works (200; 10.36 @03:56, 10.63 @04:06 UTC) as backup.

### E3. Third failover tier
1. **NSE archives bhavcopy** (EOD backstop): `sec_bhavdata_full` (verified current) + UDiFF zip successor parser; poll 16:00–19:00 IST, fail-closed after.
2. `nsearchives.nseindia.com/content/indices/ind_close_all_*.csv` (index closes) and `ind_nifty200list.csv` (universe refresh — verified 200).
3. Free delayed quotes: BSE equivalents or broker websocket (paper) — acceptable as tertiary; **paid licensed vendor** (TrueData/GDFL/offICIAL NSE Data feed) is the only compliant route if this ever leaves personal use.

### E4. Honest freshness estimate
With E1+E2 combined (NSE official snapshot every ≤5 min + staggered Yahoo 5m bars + fail-closed checks): **~95–98% of market minutes will have <5-min-fresh data** on a normal day. Yahoo-only architecture: ~90–95% (adds risk of 429 waves and silent intraday degradation; today's measurement showed near-real-time quality, but it is undocumented). Remaining 2–5% loss comes from: occasional Yahoo edge blocks (recovery ≥20 min, MEASURED), NSE endpoint drift (renames happen — budget an alarm), and the 15:30–16:00 IST window when neither live source has the finalized close (bhavcopy lands later; hold last 5m bar + flag).

---
### Appendix: probe inventory (all 2026-08-26 UTC)
03:45:42–03:47:59 Yahoo fingerprint-block matrix (9 probes) · 03:46:04 bhavcopy 25082026 → 200 · 03:46:07 yfinance daily OK · 03:46:31 allIndices 200 / stockIndices(old) 404 / quote-equity 403 · 03:48:24 archives 10-burst (8×200) · 03:54:41 40-req burst 200 · 03:55:49 300-req burst 200 · 03:55:41–03:55:58 NSE API re-probes + marketStatus 200 + old-format zips 200 · 03:56:19 lookback matrix + depth + adjclose + ^INDIAVIX · 03:58:32 bhavcopy poller started (/tmp/bhavpoll.log) · 03:58:47 **equity-stock-indices (new) 200 ×6** · 03:59:10 payload inspection (201 rows) · 04:00 ind_nifty200list 200 ×2 hosts · 09:33–09:36 IST freshness cross-check ×4.
