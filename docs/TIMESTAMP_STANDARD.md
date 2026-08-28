# Timestamp Standard (canonical storage & presentation)

Status: binding standard. Enforced by `tests/test_timezone_fix.py` and
`tests/test_timestamp_standard.py`. First codified during the 2026-08-26
scan_funnels IST-as-UTC repair.

## The rule

1. **Absolute instants are persisted as tz-aware UTC ISO strings**
   (`2026-08-26T03:45:00+00:00`). Naive datetimes are FORBIDDEN for absolute
   instants in the journal DB.
2. **Naive datetime = runner internal IST clock frame.** Runners and bar data
   (`contracts.Bar.ts`) run on naive IST (`lab/runner.py:79 ist_naive_now`);
   strategy code compares wall clocks in that frame.
3. **Conversion happens at the storage boundary, nowhere else.** Presentation
   converts UTC → viewer-local/IST only at render time.

## Boundary converters

| Function | Naive input interpreted as | Used for | Location |
|---|---|---|---|
| `utc_iso(dt)` | **IST** (attach +05:30 → UTC) | runner-frame instants: funnel ts, payloads | `storage/repos.py:43` |
| `iso_utc(dt)` | **UTC** (attach +00:00) | wall-clock "now" columns: created_at, updated_at, incidents defaults | `storage/repos.py:35` |
| `runner._aware/_iso(d)` | **IST** → aware / true-UTC ISO | every intent row + watchdog ts before repo calls | `lab/runner.py:113–120` |
| `factory._iso(d)` | **UTC** | broker-side order/fill rows; safe because PaperBroker's injected clock is `datetime.now(timezone.utc)` (`lab/factory.py:241`, `:267`) |

Raw-SQL writers are compliant too: `manager.py:276–283` (flatten-timeout
incident), `manager.py:300–303` (`ended_at`), `manager.py:147`
(`started_at`), and `api/routes_lab.py:205` (archive marker) all persist
`datetime.now(timezone.utc).isoformat()`.

## Examples

- Market open 09:15 IST → stored `2026-08-26T03:45:00+00:00`.
- First 5m bar close 09:30 IST → `2026-08-26T04:00:00+00:00`.
- Midnight edge: 23:59 IST stays on the same UTC date (`18:29Z`); 00:05 IST
  lands on the **previous** UTC date (`18:35Z` on Aug 26 for Aug 27 IST).
  Lexicographic `ORDER BY ts DESC` therefore stays chronological — all rows
  share the identical `+00:00` suffix format.
- India has no DST: the fixed +05:30 rule is date-independent. Aware inputs
  with any offset pass through `astimezone(utc)` unchanged in instant terms.

## API presentation contract

The REST API returns stored UTC strings **verbatim** (no server-side IST
conversion): `funnel_latest.ts`, `equity_curve[][0]`, `decisions[].ts`,
`trades[].entry_ts/exit_ts`, `heartbeat`. Locked by
`TestApiPresentationCanonicalUtc`.

All human rendering is client-side JS:

- `relTime(iso)` — relative "5m ago"; parses the offset-bearing ISO via
  `new Date()` — `src/sts/api/static/js/lab.js:59–70`, hydrated over
  `[data-rel]` template hooks by `hydrateFormatters` (`lab.js:84–95`).
- `fmtTime(iso)` — chart axes/tooltips, browser-local
  `toLocaleTimeString` — `src/sts/api/static/js/charts.js:42–47`.
- Server-rendered exception: system-health heartbeat is displayed as raw UTC
  with an explicit " UTC" label (`api/routes_pages.py:57–59`).

Nuance: `GET /api/sessions/{sid}` exposes `funnel_latest.ts` from the
SCAN_FUNNEL **journal event** row (the journaling wall-clock instant), not the
funnel's bar-close business instant stored in `scan_funnels.ts`
(`api/routes_api.py:378–388`). Both are canonical aware UTC.

## Migration v5 audit trail

Pre-fix, the watchdog funnel path wrote naive-IST wall clocks stamped as-if-
UTC (+05:30 skew). Migration `_m5_scan_funnels_tz` (`storage/migrations.py:170`)
repairs a row ONLY when a same-session legacy SCAN_FUNNEL journal event
(written via the trusted aware path) differs from it by exactly +05:30 with an
identical microsecond fraction — deterministic evidence. Every change is
recorded in `scan_funnels_tz_audit(scan_funnel_id, old_ts, new_ts, method,
recorded_at)`; methods are `IST_AS_UTC_CORRECTED` and `UNCORRECTED`
(future-dated rows without confirming evidence are left untouched).
Idempotent: audited ids are skipped on re-run.

## Residual finding (2026-08-26, report-only)

15 `scan_funnels` rows written AFTER v5 applied (07:22Z) still carry the exact
+05:30 skew (ids 121/128/142/149/163/170/184/194/205/209/210/… across sessions
`10b0479f…`, `5107f5f5…`; true instants 07:31–09:50Z, all watchdog
`scanned=0` heartbeats). Cause: the long-lived server process was still
running pre-fix code; current on-disk writers are verified canonical. Each row
matches v5's evidence rule exactly, so a follow-up migration re-applying the
identical rule (v6) would correct all 15 deterministically. They are NOT
auto-corrected today because recorded version 5 prevents `_m5` from
re-running. Do not hand-edit; add v6 or let the human decide.
