# Multi-Session Diagnostic — 2026-08-26

**Scope:** identical scanned counts across 3 running sessions + zero autonomous trades.
**Method:** 4 independent read-only investigators (isolation/concurrency, market-data, strategy-pipeline, DB/API-UI). No code, config, or data was modified. Evidence timestamped.

---

## Executive Summary

The three sessions are **genuinely independent runtimes** — object graphs, queues, tasks, ledgers, and journal rows are all provably session-scoped, and the identical "scanned = 200 / eligible = 197" counts are the *correct* arithmetic of three independent scans over the same NIFTY-200 universe with the same daily data. **Zero trades is not a pipeline fault**: today two of the five armed symbols (KOTAKBANK, SBICARD) actually fired their intraday breakout triggers, and every candidate was blocked at exactly one stage — the **volume rule**, which requires cumulative intraday volume ≥ 1.5× the SMA20 of *full-day* volume. At 09:30–11:30 IST an opening-drive breakout carries only 28–52% of that threshold by construction, so morning breakouts are structurally near-impossible to enter under the rule as written. That is a **design tension for human review**, not a bug in execution. Separately, the diagnosis surfaced six real secondary defects (stale daily parquet with no refresh caller, watchdog threshold tighter than the feed's legal cadence, a silently dead Yahoo fallback, a timezone bug in `scan_funnels`, a broken `incidents_24h` computation, and weakened intent→order atomicity) which degrade observability and will distort future triggers if left unaddressed.

## Observed Symptoms

- All 3 sessions show scanned=200, eligible=197, setups=0, selected=0.
- 0 intents / 0 orders / 0 fills / 0 positions across all sessions.
- Feed reported live; 16 `FEED_STALE_ENTRIES_BLOCKED` incidents today.

## Root Cause

### Confirmed root cause (zero trades) — HIGH confidence
**Stage-block: VOLUME rule.** Stage table reproduced independently from persisted parquet data using runner-exact logic:

| Stage | Count |
|---|---|
| scanned | 200 |
| eligible | 197 |
| regime gate (NIFTY 24,334.6 > SMA50 24,203.6; SMA20>SMA50; VIX 11.07<22) | PASS |
| trend pass | 87 |
| pullback pass | 26 |
| RSI(14) ∈ [45,70] | 26 |
| volume pass (live) | **0** ← blocker |
| breakout | 2 of 5 armed fired (KOTAKBANK @09:30 bar, SBICARD @09:30 bar) |
| intents → orders → fills | 0 / 0 / 0 |

Rule ordering (`pullback_v1.py:176–185`) `continue`s at the failed volume rule before the breakout rule is even evaluated — verified against code. The two breakout symbols' cumulative volume at trigger time: KOTAKBANK 7.68M vs required 14.65M (0.52×); SBICARD 706K vs 2.53M (0.28×). The rule compares a *partial day's* running volume against 1.5× a *full average day* — monotonic-increasing intraday, so it can only pass late-session if at all, while the entry window closes at 14:30. **Morning breakout entries are structurally excluded by this formulation.**

### Confirmed root cause (identical counts) — HIGH confidence
**Legitimate shared-universe scanning.** `funnel.scanned = len(self.symbols)` (runner.py:580) where `symbols` is a per-runner copy of the same configured universe. Identical deterministic inputs → identical counts is correct behavior. Proven per-session: distinct funnel rows with distinct session_ids and microsecond-distinct timestamps in `scan_funnels`.

### Contributing factors (real defects, not today's blocker)
1. **Stale daily parquet — HIGH impact on trigger correctness.** Symbol files end **2026-08-24**; the entire Aug-25 session is missing. "Prior-day high" triggers are actually Aug-24 highs — one session behind intent. Cause: `service.refresh_daily_if_stale()` exists (service.py:323) but **has no caller** (no cron/nightly wiring anywhere).
2. **Watchdog threshold vs feed cadence — MEDIUM.** NSE snapshot aggregation emits a completed bar only when a poll lands after the 5-min boundary → legal inter-delivery gap ≈ 360 s, but `WATCHDOG_STALE_BAR_AFTER_S = 300` → spurious FEED_STALE episodes and fail-closed entry blocks on a *healthy* feed (1 confirmed artifact at 10:57 IST).
3. **Yahoo fallback silently dead — MEDIUM.** Persistent 429s (TLS-fingerprint blocks) are logged at DEBUG (live.py:173) → invisible in sts.log; failover exists but its fallback cannot currently succeed. NSE is the de-facto single source and 404'd/hung twice today.
4. **Timezone bug in `scan_funnels.ts` — MEDIUM (latent).** Writer emits naive IST with `+00:00` semantics → future-dated rows (e.g., `11:13+00:00` = 16:43 IST). Any `ORDER BY ts DESC` reader of that table picks the wrong "latest". Today's API reads the legacy `session_events` channel, so displayed values were correct — but the two funnel stores have already diverged.
5. **`incidents_24h` always ≈ 0 — LOW/MEDIUM.** routes_api.py:218–220 computes `day_ago = _now_iso()` (now, not now−24h). Health reported "0 incidents" during 16 real incidents.
6. **Restart artifacts + intent→order atomicity — LOW.** First watchdog tick after boot opens a staleness episode with no bar yet (3 spurious incident triplets); `RepoSink.on_order(commit=True)` inside an outer `transaction()` prematurely commits, weakening the documented atomicity (session-scoped, so no cross-session leak).

## Session Isolation Audit

**Shared correctly (by design, verified read-only):** market-data singleton (frozen `Bar` dataclasses — assignment raises `FrozenInstanceError`; cached daily DataFrames never mutated — pandas hash unchanged after full strategy evaluation; `reset_index` non-inplace), index/VIX frames, instrument reference, calendar, cost schedule, pure strategy functions.

**Isolated correctly (proven):** per-session `TradingRepo`/`RepoSink`/`PaperBroker`/`OrderManager`/`RiskEngine`/strategy instances (runtime probe: all `is not` each other; `PaperBroker._accounts` keyed by session_id); per-subscriber `asyncio.Queue(maxsize=1000)` with fresh batch lists per subscriber (fan-out, not shared-consumer); one asyncio task per RUNNING session keyed in `manager.tasks`, per-item fault containment; zero module-level mutables, zero mutable default args; DB: 0 orphans across 9 tables, random spot-checks all session-scoped, per-session indexes present.

## Market Data Audit

- Active source **right now**: NSEQuotePoller (`/api/equity-stock-indices`), 200 OK in 0.19–0.27 s, full 200-symbol batch per 5-min window; Yahoo hard-down (429) and silent about it.
- Freshness: completed 5m bars with ≤ ~6-min delivery lag; tick age 115 s at probe time.
- 16 incidents reconstructed: ~7 restart artifacts, ~8 real staleness (NSE stall 10:05–10:45 IST when failover couldn't rescue), 1 threshold artifact.
- SPOFs: NSE sole working source; `dropped_events` counter exists but is exposed nowhere.

## Pipeline Audit (today, per session — identical because inputs identical)

| Stage | hybrid-main | det-only | random-k |
|---|---|---|---|
| scanned | 200 | 200 | 200 |
| eligible | 197 | 197 | 197 |
| setups | 0 | 0 | 0 |
| ML passed | 0 (OFF) | 0 (OFF) | 0 (OFF) |
| portfolio/risk/selected | 0 | 0 | 0 |
| orders/fills | 0/0 | 0/0 | 0/0 |

random-k shares `detect_candidates`; with 0 setups there is nothing to sample — identical zero is expected.

## Database Audit

All rows session-scoped; no cross-session bleed in DB, API, or UI (UI verified per-session via Playwright — random-k even rendered a fresher pass mid-poll, proving independent rendering). Defects: dual funnel stores diverging; IST-as-UTC timestamps in `scan_funnels`.

## Why No Session Has Traded

**Every session blocks at the VOLUME stage** — correct per the rule as specified. Two genuine breakout triggers fired today and were correctly not traded under the current rule formulation. No execution, risk, broker, or isolation fault was involved (intents table literally has 0 rows — nothing ever reached risk).

## Reproduction

Any market day, first ~4 hours: armed symbol breaks prior-day high on opening drive → cumulative volume < 1.5× full-day SMA20 → volume rule fails → setups=0. Reproduced analytically from persisted parquet + live Yahoo 5m (probe 11:32 IST).

## Recommended Fix (NOT implemented — for human review)

1. **Volume rule reformulation (decision required):** replace absolute `1.5 × full-day SMA20` with **time-of-day volume pacing** — expected cumulative volume at time t ≈ (SMA20 full-day) × historical fraction-of-day-volume curve (or simple linear pacing `t/375 min` as v1), requiring `cum_vol(t) ≥ mult × expected(t)`. This is a strategy-specification change → human sign-off required; it alters the research hypothesis H1.
2. **Nightly/intraday daily-refresh wiring:** call `refresh_daily_if_stale()` (schedule pre-open + ~16:30 IST post-bhavcopy) so triggers use the true prior session.
3. Watchdog: raise stale threshold to ≥ 420 s or make it emission-aware (expect next bar within window+poll+latency).
4. Yahoo fallback: promote fetch failures to WARNING; consider curl_cffi impersonation in the raw path (MEASURED to bypass fingerprint blocks).
5. Unify funnel storage on `scan_funnels` after fixing the IST→UTC writer; fix `incidents_24h` window; expose `dropped_events`; suppress boot-tick staleness episodes until first bar or 2× interval.

## Architectural Impact

No change to `ARCHITECTURE_V1.2` isolation model — it held. Item 1 modifies the *strategy specification* (documented as research hypothesis H1 parameters), requiring a param-version bump and re-run of the plateau checks if adopted.

## Confidence

- Zero-trades root cause (volume stage): **HIGH** (independently reproduced from real data + code-path verified)
- Isolation cleanliness: **HIGH** (static + runtime object-identity + DB evidence)
- Identical counts legitimacy: **HIGH**
- Secondary defects list: **HIGH** for items 1–3 (measured), **MEDIUM** for 4–6 (code-read + partial probe)

## Final Status

`ROOT CAUSE IDENTIFIED — READY FOR HUMAN REVIEW`
