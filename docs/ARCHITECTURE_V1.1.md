# Architecture & Research Design Package — v1.1 (Adversarial Revision)

> **NOTE (2026-08-24):** The application model was revised from single-experiment to **multi-session Trading Lab** — see `ARCHITECTURE_V1.2.md`, which supersedes §15/§22/§23 session-scoping details and the `experiments` schema. All other v1.1 content stands.

**Date:** 2026-08-23 · **Supersedes:** ARCHITECTURE.md v1.0 · **Status:** Reviewed design (gated)

v1.0 sections not touched below carry over unchanged. This document contains: the review verdict, every material revision in structured form, fully rewritten specifications for the areas that failed scrutiny, and the implementation gate.

---

## 0. Verdict summary

**YELLOW.** The overall philosophy (hybrid deterministic+ML, hard risk layer, paper-first, modular monolith) survives scrutiny intact. Four v1.0 decisions were wrong or under-specified enough to produce misleading experimental results if implemented as written:

| # | Failed area | Severity |
|---|---|---|
| R1 | Paper fill model was optimistic in specific, P&L-material ways | High |
| R2 | Stop/exit semantics were internally inconsistent (tick vs bar vs next-bar) | High |
| R3 | Broker choice ignored that Dhan offers sandbox + 5y minute history + server-side brackets — objectively better fit for the paper→live path | Medium |
| R4 | ML layer likely unjustified at realistic sample size (~3k examples); needed demotion from "core component" to "gated experiment" | Medium |

Plus ~10 smaller corrections (regulatory overclaim on market-order ban, ₹25k sizing conflicts, experiment goalpost rules, decision-replay gaps, arbitrary thresholds mislabeled as methodology).

---

## 1. Paper-trading realism (R1) — full rework

### 1.1 Failures found in the v1.0 model

The v1.0 spec ("next bar open + half spread + tiered slippage") sounds conservative but is wrong in both directions depending on order type:

**Optimistic distortions (would inflate P&L):**
1. **Limit fills on touch.** Model implied a buy-limit at L fills when traded price ≤ L. Reality: a touch at exactly L may be a single small trade ahead of us in queue; we may get zero or partial fill. Simulating guaranteed fill-on-touch systematically overstates entry success in fast pullback-resume setups — precisely our setup type.
2. **Stops filled at stop price.** Nothing in v1.0 explicitly said what happens when bar-low < stop. If implemented naively (fill at stop price), we erase gap-through losses, which are the single largest source of real-world swing-trading tail losses.
3. **Same-candle target credit.** If a candle's range covers the target, crediting a target exit assumes the high happened before the low — unknowable from OHLCV.
4. **No queue/volume check.** A ₹25k order in a ₹10cr/day name is trivially small, so *our* size rarely matters — but the model should still require evidence of traded volume at/below the limit before granting a fill, because that's the honesty mechanism that costs nothing at our scale.

**Pessimistic distortions (would deflate P&L):**
5. **Next-bar-open for everything.** For entries submitted at a 5-min close with a limit above the next open, forcing next-open execution rejects fills that would have occurred within seconds. Over 30 trades this biases the experiment downward by roughly the entry-to-next-open drift × trade count. Acceptable if intentional; unacceptable if accidental.
6. **Flat ×2 slippage on low-volume bars** double-penalizes when combined with the half-spread penalty.

### 1.2 What OHLCV + WS-LTP can and cannot simulate

| Knowable from our data (5-min OHLCV + WS ticks + daily volume) | Not knowable |
|---|---|
| Whether price traded through a level during a bar (H/L) | Queue position; whether OUR order would have filled at a touch |
| Approximate spread (20d median range proxy; bid/ask from WS where broker provides quote mode) | Exact bid/ask at historical decision times (only live-forward) |
| Volume traded in the bar | Volume traded specifically at our price level |
| Gap opens (bar open vs prior close) | Intra-bar sequence (did high precede low?) |
| Tick-sequence via live WS stream (forward-only) | Historical tick data without a paid feed |
| Circuit bands (from instrument master + % rules) | Exchange halts called mid-session before they happen |

**Tick/order-book data assessment:** For a strategy whose stop distance is ~2–4% of price and whose decision cadence is minutes, depth data adds realism only at the touch-fill margin. The dominant realism levers are (a) worst-case intrabar sequencing, (b) gap handling, (c) cost accounting — all free. **Decision: no paid tick/depth feed.** Live WS LTP/quote stream (already in plan) is used forward-only to trigger stops realistically; historical simulation uses OHLCV with the conservative rules below. This is a measured trade-off, documented rather than hidden.

### 1.3 REVISED paper-execution model (normative spec)

Definitions: `S` = submitted order; `t` = submission time (falls inside bar B or between bars); engine processes orders against the **event stream**: live WS ticks (paper-live mode) or completed bars (backtest mode). One code path (`FillModel.apply(order, event, state) -> Fill | NoFill | Partial`) used by BOTH backtest and paper — divergence between backtest and paper engines is itself a bug class this kills.

**Entry BUY LIMIT @ L, qty Q:**

```
Paper-live (tick/bar events after t):
  Fill condition: any event where traded_price < L            (strict trade-through)
                  OR (traded_price == L AND cumulative volume since submission ≥ 3 × Q)
  Fill price:    min(L, trigger_px) + half_spread_estimate   ← pay up half-spread even on limit
  Expiry:        cancel at end of entry window (14:30) or after N bars (config, default 6),
                 whichever first → intent marked EXPIRED (a real outcome, logged)

Backtest (bars only):
  Fill iff bar.low < L                                          (strict penetration)
        OR (bar.low == L AND bar.volume ≥ 3 × Q)
  Fill px:      L + half_spread_estimate
  Note: strict `<` is deliberately conservative — a bar that merely touches L grants
  no fill. Quantified bias: pessimistic by ≤ ½ tick on filled trades; unbiased on
  unfilled. Accepted and disclosed.
```

**Stop-loss SELL (protective):**

```
Trigger:  paper-live → first WS LTP ≤ stop_px (this is what the live system does;
                    the simulator must mirror the LIVE trigger mechanism, not a nicer one)
          backtest  → bar.low ≤ stop_px
Fill price rule:
          open of triggering bar ≤ stop_px  (gapped through)
              → fill at bar.open − slippage_tier          (gap loss taken in full)
          else
              → fill at stop_px − slippage_tier − half_spread
Never, under any branch, fill a stop at better than stop_px.
```

**Target SELL LIMIT @ T:** symmetric to entry limit: requires `bar.high > T` (strict) or tick ≥ T with volume condition; fill at `T − half_spread` (we cross spread to exit into strength... actually we hit a bid — modeled conservatively).

**Intrabar ambiguity (stop AND target both inside one bar):**

```
Unknowable from OHLCV. Mandatory rule: assume ADVERSE sequence — stop fires first,
position closed at stop. Target never credited from a bar whose low breached the stop.
In paper-live, WS tick ordering resolves genuinely (first crossing wins) — which is
exactly why paper-live results are the primary evidence and backtests are secondary.
```

**Partial fills:** at Q ≤ ₹7k notional in ≥₹10cr/day names, partials are negligible; model binary fill but *record* a partial-fill counter whenever fill-condition volume is within 2× of Q (audit flag, not a P&L effect).

**Circuit/halt:** if bar has zero volume or symbol flagged halted → order queues; if price outside ±(circuit band) → reject with reason CIRCUIT. Instrument-master bands refreshed daily.

**Boundary submissions:** order submitted in final 60s of session → deferred to next session open, filled per gap rules (open ± slippage). Prevents fake same-close fills.

**Costs:** unchanged from v1.0 §13 (`costs.yaml`, versioned) — that part survived review.

### 1.4 Known residual biases (disclosed, monitored)

- Touch-fills still possible in live WS mode vs strict-penetration in backtest → report both numbers; divergence is itself a metric ("fill-model gap").
- Spread estimate from range proxy overstates true spread for liquid names → costs slightly overstated. Conservative direction; accepted.

---

## 2. Stop-loss & exit semantics (R2) — resolved

v1.0 mixed four mechanisms (WS ticks, internal 5-min checks, exchange-side stops, next-bar execution) without defining precedence. Normative answers:

1. **When is a stop triggered?** On the FIRST observation of LTP ≤ stop_px. Observation source priority: live WS tick > current forming bar's latest price > last completed bar's low (recovery path). Trigger time = observation time; the position state moves to EXITING immediately.
2. **Bar-low crosses stop between observations** (e.g., WS dropped): treat as triggered at discovery; fill modeled per §1.3 stop rule using that bar's OHLC (open ≤ stop ⇒ gap fill at open).
3. **Gap through stop (overnight or halt-resume):** exit executed at first actionable price = session open (or resume print) minus slippage tier. The stop distance is irrelevant once gapped; P&L absorbs the gap. Simulator must reproduce this exactly — it is why "max single-trade loss" in reports will exceed 1R sometimes, and that is honest.
4./5./6. **Both target and stop inside one candle:** simulator cannot know sequence → §1.3 adverse-sequence rule (stop wins). Never resolve by optimism.
7. **Trailing stop updates:** computed ONLY from completed bars (highest-high since T1 minus 1.5×ATR), updated at bar close, never intrabar — an intrabar trail update would leak future information into backtests (using a bar's high before knowing the bar ended). New trail value becomes active from the NEXT bar.
8. **Overnight gaps:** no special handling beyond (3); positions carry through; pre-open job marks any position gapped-beyond-stop for immediate-exit-at-open.

**Live-side consistency:** where broker supports server-side conditional/bracket protection (Dhan Super Order / Forever OCO — see §3), the LIVE adapter places the protective leg exchange-side so protection survives our process death; the internal engine remains authoritative in paper mode and acts as second line live. Both mechanisms must agree; disagreement = reconciliation incident (§16).

---

## 3. Broker re-evaluation (R3) — decision REVERSED to Dhan primary

Fresh evidence from official docs (Aug 2026):

| Criterion | **Dhan (DhanHQ v2)** [dhanhq.co/docs/v2] | Fyers v3 [myapi.fyers.in] | Zerodha Kite [kite.trade/docs/connect/v3] | Angel One SmartAPI | Upstox v2 |
|---|---|---|---|---|---|
| API cost | Trading free; data API subscription (~₹499/mo reported; verify) | Free | Free Personal (orders only); **₹500/mo Connect incl. WS+historical** (official pricing page confirmed) | Free | Free/paid tiers unclear |
| Minute history | **5 years intraday** (official v2.2 release notes) | ~1–2y (community-reported) | Multi-year, rate-limited bulk | Reported generous | Limited |
| Sandbox | **Yes — public Developer Kit/sandbox** (official docs) | No public sandbox | None (confirmed by Zerodha ecosystem writeups) | No | No |
| Server-side bracket/trailing | **Super Order (entry+target+SL+trail)** and Forever OCO (official) | TP/SL legs attachable (v3 SDK) | GTT (separate trigger orders) | Limited | Limited |
| Rate limits (official) | Orders 10/s · data 100k/day, unlimited/min on minute TF | ~10/s | 200/min quotes, tight historical backfill | ~10/s | ~25/s reported |
| Auth | API key 1-yr validity + daily access token (v2.4) — automatable via TOTP login API | Daily OAuth | Daily token, manual hosted login, no refresh | Daily | Daily |
| Static IP | **Required for ALL order APIs since Apr 1 2026; settable via API endpoint** (official releases page) | Required (verify flow) | Required (verify flow) | Verify | Verify |
| Market-order policy | **API market orders converted to LIMIT-with-MPP since Mar 21 2026** (official) | Verify | Full order types incl. market (algo-tagging caveats apply) | Verify | Verify |
| Retail-algo alignment | Conforms to SEBI framework; changes documented with dates | Conforms | Conforms | Conforms | Conforms |

**Revised decision:** **Dhan = primary broker adapter** (development → sandbox → micro-live is a single-vendor path with a genuine test environment and best-in-class research data). **Zerodha = fallback live adapter** (reliability premium worth ₹500/mo if Dhan live proves shaky; free Personal tier suffices for order-API parity testing). **Fyers retained only as an alternate data source.** Impact: symbol mapping keyed to Dhan security_ids; Phase 8 gains a sandbox parity stage that didn't exist before; data subscription budget ≈ ₹500/mo during active months only.

---

## 4. Regulatory claims audit (R4a) — every v1.0 claim reclassified

| Claim | v1.0 status | v1.1 classification | Basis |
|---|---|---|---|
| All client API orders treated as algo orders requiring tagging | Known | **Confirmed** | NSE FAQ (Nov 3 2025), BSE notice Aug 29 2025 — both exchanges state this verbatim |
| 10 OPS threshold below which standardised tagging suffices | Known | **Confirmed** (threshold), **broker-specific** (mechanics) | NSE FAQ; Dhan cut its own order API to 10/s citing regulations |
| Static IP mandatory for direct-API clients | Known | **Confirmed, and now concrete**: Dhan enforces on all order APIs since 2026-04-01, self-serviceable via their Setup-Static-IP API | DhanHQ v2.4/v2.5.1 release notes (official) |
| Tag format 444444444444 + {0,2,4} | Known | **Potentially outdated / broker-specific** — brokers implement tagging internally; do not build logic around the digit format | NSE FAQ; treat as opaque |
| **"Market and IOC orders not allowed" for algo orders (equity)** | Known | **DOWNGRADED to Needs-direct-confirmation.** Source circular (NSE/MSD/67753) text concerns specific contexts; Dhan's actual implemented policy (Mar 2026) is conversion to LIMIT+MPP, not rejection. v1.0 overstated generality. Our limit-only design is unaffected either way — kept as defense-in-depth, not as regulatory fact | Circular text vs observed broker implementations diverge |
| Mock-session exemption for tech-savvy clients | Known | **Confirmed** | NSE FAQ item 9 |
| Vendor empanelment not required for personal single-user systems | Assumption | **Needs direct confirmation** with chosen broker (in writing, pre-live) | Reasonable reading of framework; zero vendor-facing surface in this project |
| TOTP/daily-token automation permitted | Assumption | **Broker-specific — Confirmed for Dhan** (they publish the TOTP token-generation API themselves) | DhanHQ v2.4 |
| Sandbox/mock availability | Known-ish | **Confirmed for Dhan**, absent elsewhere among candidates | Official docs |

Architectural consequence: all tagging/IP/MPP behavior is confined to `brokers/dhan.py`; strategy/risk layers contain zero regulatory logic. A `compliance_checklist.yaml` per broker records confirmations with dates and source links.

---

## 5. Strategy specification (R5) — kept, but demoted and modularized

**Verdict: parameters are a sensible first hypothesis, NOT established truth — and v1.0's language already said so, but the architecture didn't enforce it.** Revisions:

1. **Strategy = interface, not module.** Formalize:

```python
class SwingStrategy(Protocol):
    version: str
    def evaluate(self, ctx: MarketContext) -> list[CandidateSignal]   # pure
    def exits(self, pos: Position, ctx: MarketContext) -> ExitDirective | None
```
Platform ships `PullbackBreakoutV1` as the sole implementation today; a second deliberately different strategy (e.g., Donchian-trend V2) is added in Phase 4 as an integration test that NOTHING upstream/downstream knows which strategy runs. Config selects strategy by id; experiments pin strategy_version.

2. **ML question answered honestly.** With barrier labels tied to THIS strategy's stop/target geometry, the model learns *"which trades generated by this particular rule set are more likely to hit this particular target before this particular stop"* — a strategy-conditioned quality estimator, not general swing-opportunity value. That is still scientifically useful (it answers the ablation question: does ranking add value to THIS generator?), but the doc previously blurred this. v1.1 states it, adds a **strategy-independent auxiliary label** (forward 10-day return vs universe median, used only for diagnostics/feature sanity — never sizing) so we can see whether learned signal is generic or idiosyncratic to the rule geometry.

3. Parameters stay exactly as v1.0 §7 values (literature-derived, unoptimized), now formally tagged `RESEARCH-HYPOTHESIS-H1` in config with falsification criteria attached.

---

## 6. Hyperparameter methodology (R6) — corrected labeling

The machinery (coarse grid, walk-forward, embargo, plateau selection, deflated metrics) stands. Corrections:

- **"Neighborhood ≥60% of peak OOS" is an arbitrary engineering heuristic, not a statistical procedure.** v1.0 presented it as methodology. Relabeled as such, with rationale (it approximates testing whether the performance surface is locally flat, i.e., the result isn't a knife-edge artifact) and with sensitivity requirement: conclusions must also hold at 50% and 70% thresholds, else the parameter region is reported as unresolved rather than silently accepted.
- **Regime leakage added as explicit check:** walk-forward folds must not let a fold's training window end inside the same volatility regime episode as its test window without embargo covering it; report per-regime (NIFTY>SMA50 vs <, VIX tercile) OOS stats separately so a strategy profitable only in one regime is visible as such.
- Multiple-testing control: keep the bounded search budget (≤5 values × ≤2 params per study, few studies) AND report the deflated Sharpe; additionally maintain a **pre-registered analysis registry** — every grid run gets an ID before results are seen, preventing post-hoc cherry-picking from unlogged experiments.

---

## 7. ML necessity challenge (R7) — sample-size estimate forces redesign

**Estimate.** NIFTY200 × ~250 sessions = 50,000 symbol-days/year. Pullback-breakout conditions fire on ~0.3–1% of symbol-days historically for such filters (trend+pullback+volume confluence is restrictive) → **~150–500 candidates/year → ~750–2,500 over 5 years of daily-bar history.** With purge+embargo discarding boundary samples, effective training ≈ 1,500–2,000 rows; OOS segments ≈ 40–120 candidates each.

**Consequences:**
- LightGBM with ~30 features on ~2k rows will memorize noise; expected OOS edge over logreg ≈ nil, with high variance across seeds. v1.0's progression (logreg → LightGBM) is kept as a *hypothesis test*, not a plan-of-record.
- Feature count cut to **≤15**, mandated (trend/momentum/volume/vol/regime core), with feature-importance stability across folds as a reporting requirement.
- Class balance ~40–55% expected — workable, but OOS segment counts are small enough that per-segment hit-rate comparisons need binomial confidence intervals, not point estimates.
- Calibration remains mandatory (probabilities drive thresholds).
- Frozen-model-for-the-month stands; monthly retraining cadence post-experiment stands.

**Deployment gate (tightened):** ML ships live ONLY IF calibrated OOS probability beats deterministic-only selection on expectancy with ≥95% bootstrap CI excluding zero across ≥3 consecutive walk-forward segments. Otherwise the system RUNS DETERMINISTIC-ONLY and the experiment report says so plainly. ML is now a gated sub-experiment inside the platform, not a load-bearing component. This directly serves the research question ("does complexity add value?") instead of assuming it.

---

## 8. Latency (R8) — quantitative verification

Numbers (typical NIFTY-200 large cap): daily σ ≈ 1.2–1.8%; 75 five-minute bars/session → 5-min σ ≈ 0.15–0.21%; stop distance = 1.5×ATR ≈ 2.0–3.0%.

| Delay | Expected adverse move vs trigger | As fraction of stop distance |
|---|---|---|
| 1 s | ≈0.003% | 0.1% |
| 30 s | ≈0.05% | ~2% |
| 5 min (one bar) | ≈0.18% | ~7% |
| Overnight gap | 0–6% (unbounded) | 0–200%+ |

Conclusion verified: **seconds of latency move outcomes by hundredths of a percent; a single overnight gap can move them by multiples of the entire stop distance.** Engineering effort belongs in gap handling, state integrity, and fill honesty. Where latency DOES matter operationally (all in the minutes regime, none in milliseconds): data-staleness detection (≤2 min), stop-observation cadence (tick-driven, but tolerance is minutes), token-expiry scheduling (fail BEFORE market open, not during), reconciliation cadence (every 5-min bar), alert delivery (<1 min). These are reliability budgets, now stated as such.

---

## 9. Live-data architecture (R9) — authority hierarchy made explicit

Architecture unchanged (WS + REST reconcile + internal 5-min bars). Added normative authority table — exactly one winner per domain:

| Domain | Authoritative source | Everyone else is |
|---|---|---|
| Historical/closed candles | Broker REST EOD fetch, nightly overwrite | Internal bar-builder = provisional until EOD confirms |
| Forming intrabar price | WS LTP stream | REST poll fallback (flagged degraded) |
| Candle truth during session | Internal builder, but EOD overwrite may correct; corrections logged as incidents if >0.05% off close | — |
| Order state | **Broker REST order book** (polled each bar + on WS order-update event) | WS order updates = hints that accelerate polling, never truth |
| Account/funds state | **Broker REST funds/positions** | Local ledger = projection for speed, reconciled每bar |
| Clock | System NTP-synced; broker server-time header compared each session start | Local monotonic clock for intervals only |

Edge rules codified: reconnect → resubscribe + REST snapshot backfill of the gap window; duplicate/out-of-order ticks → dedupe by (symbol, timestamp, seq) keep-last; session boundaries → builder closes bars at 09:15/15:30 sharp regardless of tick arrival, odd ticks near boundary attributed by exchange timestamp; corporate actions → nightly corp-action job adjusts history and flags symbols with pending actions (no new entries into ex-div/bonus day — simple, conservative, avoids adjustment races); stale quote detection = tick-age vs calendar-aware expectation.

---

## 10. Universe (R10) — NIFTY 200 retained; filters adjusted

Comparison stands (NIFTY 50 too few opportunities; NIFTY 500/all-NSE drag in data quality and illiquidity tails disproportionate to candidate gain). Filter revisions:

- ₹10cr median ADV → **softened to ₹5cr** with justification: at ≤₹7k orders our participation is ~0.007% either way; ₹10cr excluded ~30–40 marginal names whose exclusion is a silent universe-selection bias toward mega-caps. Documented as configurable with sensitivity note.
- ₹50 minimum price: **kept but labeled pragmatic** — real reason is tick-size economics (₹0.05 tick = 0.1% of a ₹50 stock vs 0.005% of ₹1000) and spread modeling fidelity, not arbitrariness; sub-₹50 names would need a different spread model.
- 60-day history: **not arbitrary** — hard requirement of SMA50+slope+ATR warmup plus buffer. Keep.
- Range-proxy spread ≤4%: keep, labeled coarse proxy with known conservatism.

---

## 11. Risk model (R11) — defaults kept, ONE real conflict exposed and resolved

All values survive as **configurable V1 defaults** (explicitly labeled plausible-but-unoptimized). But the adversarial check on **₹25,000** exposed a structural conflict v1.0 missed:

> Risk 1% = ₹250. A ₹1,200 stock with 2.5% ATR-stop = ₹30/share → qty = ⌊250/30⌋ = 8 shares = ₹9,600 notional = **38% equity → violates the 20% position cap**. The two constraints collide constantly at ₹25k; most valid signals would be rejected by the cap, and the experiment would quietly measure "how often constraints conflict" rather than strategy quality.

**Resolution (pre-registered):** at equity < ₹50,000 the engine uses `risk_per_trade=1.5%`, `position_cap=33%`, `min_notional=₹4,000`; expected outcome is 1–2 concurrent positions typical. Cost reality also disclosed: round-trip delivery costs ≈0.26% (STT sell side dominates); with avg win ≈1.5–3R and R≈₹300–400, costs consume ~0.5–0.8R-equivalent friction annually-scale — material, which is precisely why the cost model runs always-on and the report isolates cost drag as a headline metric. Alternative honestly noted: ₹50k–100k capital would make constraint collisions rare; user may choose it; protocol does not depend on it.

---

## 12. Experiment design (R12) — anti-goalpost rules added

v1.0 weaknesses fixed:

1. **Auto-extension pre-registered BEFORE any data:** if <20 closed trades at day 20, experiment extends in 2-week increments up to 90 days total, decided by a rule written into `experiment.yaml` at t=0 — never by looking at P&L. Extension criterion is trade count only.
2. **Baselines = separate simulated portfolios** on identical data feeds and identical risk-engine versions: (a) NIFTY50 B&H, (b) NIFTY200 B&H, (c) deterministic-only, (d) hybrid (primary), (e) random-k-selection. Each gets its own account state; no shared capital; identical cost/slippage models. Fairness property: c vs d differ ONLY in ML filter — everything else bit-identical.
3. **20 trades relabeled correctly:** minimum-observation threshold for ANY inference, expected to yield CIs wide enough that month-one conclusions are directional. Report template includes the binomial/bootstrap CI so the report cannot overstate.
4. Regime coverage acknowledged: one calendar month almost certainly samples ONE regime; regime diversity is a reason the checklist (§19 of v1.0) demands ≥2 regimes before live money, potentially extending months.
5. Success criteria locked at t=0 in the config file; the report script reads criteria FROM the config, making post-hoc goalpost-moving mechanically impossible.

---

## 13. Decision-replay capability (R13) — schema extended

v1.0 `intents` table captured most layers but couldn't reconstruct exact market state or individual risk-check outcomes. Additions:

```sql
intents(... existing ...,
  market_state_ref TEXT,      -- pointer/hash to immutable bar-window snapshot in parquet store
  feature_vector_json TEXT,   -- frozen: name→value as fed to ML, with feature_schema_version
  signals_json TEXT,          -- each deterministic rule: id → evaluated value + pass/fail
  portfolio_snapshot_json TEXT, -- cash/positions/exposure at decision instant
  risk_checks_json TEXT       -- [{check, threshold, observed, passed}] EVERY check individually
)
```

Replay tool `scripts/replay_decision.py <intent_id>` renders the full chain (§ requirement) from stored artifacts alone — no re-computation from possibly-changed live data. Golden test: replay output for fixture intents must equal stored decision. This makes "why did you buy this?" answerable deterministically forever, including after schema evolution (feature_schema_version pins interpretation).

## 14. Failure playbook (R14) — deterministic responses, now specified

| Scenario | System behavior (deterministic) |
|---|---|
| WS dies 3 min | Degrade to REST quotes (age-flagged); if >10 min in session → FAIL-CLOSED new entries; exits continue off last-good + widened slippage; reconnect triggers backfill + reconciliation |
| SQLite unavailable mid-session | In-memory ops continue ≤1 bar; journal writes buffered+retried; failure >1 bar → HALT new orders, alert, attempt read-only integrity check; NEVER trade unjournaled |
| Crash right after sending order | Order manager wrote intent+order rows BEFORE API call (write-ahead); boot recovery queries broker order book, adopts or cancels unknowns; unmatched broker orders = incident + fail-closed pending human ack |
| Broker timeout on place_order | Query-before-retry using idempotency correlation id; blind resend forbidden; unresolved after 2 polls → mark UNKNOWN, alert, block symbol |
| Duplicate request (same idempotency key) | Second submission intercepted locally; if it reaches broker anyway, dedupe on CorrelationId/order-book scan |
| Price 15 min stale | Entry blocked (stale >10 min); exits allowed with 2× slippage assumption; freshness incident alerted |
| Stock opens 4% below stop | Pre-open gap job flags position → immediate exit-at-open order; fill at open−slippage; recorded as gap loss (>1R) — expected, journaled |
| Circuit limit hit | Order rejected/queued → cancel working orders, blacklist symbol for day, incident; position (if held) waits for resume, exit on reopen |
| Local says pending, broker says filled | **Broker wins**; local state adopted to broker; discrepancy incident logged with both snapshots |
| Mac reboots 10:30 | launchd restarts; boot sequence: journal load → broker reconcile → data-feed restore → resume; missed bars backfilled from REST before any decision |
| Internet gone, returns 11:15 | Same as reboot path minus process death: reconnect → backfill → reconcile → freshness-gate clears → resume; the 105-min gap is a recorded coverage incident counted in the report |

Every row is implemented as a named handler with a chaos-test counterpart (Phase 8 maps 1:1).

## 15. Portability (R15) — Mac independence enforced

Corrections: `caffeinate` invocation lives OUTSIDE `src/` (launchd wrapper script); no macOS-specific paths/APIs anywhere in `src/` (pathlib only, config-driven data dir); CI runs the full test suite on Linux (GitHub Actions ubuntu runner) from day one — portability is continuously proven, not assumed. Docker: introduced AT the VPS/live stage, not before — for the paper phase, uv + launchd is simpler and the recovery drills are more honest on bare metal; Dockerfile + compose added in Phase 10 as deployment artifact for Linux VPS, running the SAME image in paper mode first for parity verification.

---

## 16. Consolidated change table

| Original decision (v1.0) | Problem discovered | Evidence/research | Revised decision | Impact |
|---|---|---|---|---|
| Next-bar-open + touch-fills paper model | Optimistic on limits/stops; ambiguous intrabar | OHLCV ambiguity analysis §1.1–1.3 | Strict-penetration limits, adverse sequencing, gap-honest stops, one shared FillModel for backtest+paper | Backtest engine and PaperBroker share code; new golden tests |
| Mixed stop semantics | Tick vs bar vs exchange-side precedence undefined | §2 analysis | Single trigger definition; broker-wins reconciliation; trail updates bar-close only | Position state machine rewrite (small); new tests |
| Fyers primary broker | Missed Dhan sandbox + 5y minute history + server-side brackets + API-settable static IP | Official DhanHQ v2 release notes; kite.trade pricing page | Dhan primary, Zerodha fallback, Fyers data-only | Adapter priorities flipped; symbol map = security_id; sandbox stage added to Phase 8/10 |
| "Market orders banned for algo (equity)" as Known | Overgeneralized from circular context | Circular text vs Dhan MPP implementation | Reclassified Needs-confirmation; limit-only stays as defense-in-depth | None on design; compliance yaml updated |
| ML as core layer, 30 features | ~2k training rows; GBDT overfit; strategy-coupled labels conflated with general skill | Sample-size estimate §7 | ML gated; ≤15 features; aux strategy-independent label; deterministic-only default fallback | Smaller ml/ module; experiment can legitimately ship without ML |
| Neighborhood ≥60% plateau rule as methodology | Arbitrary threshold dressed as statistics | §6 | Relabeled engineering heuristic + threshold sensitivity 50/70% + pre-registered analysis registry | Process change only |
| ₹10cr ADV, risk/cap defaults unexamined at ₹25k | Constraint collisions reject most signals at small capital | Worked example §11 | Capital-tiered risk params (<₹50k: 1.5%/33%/₹4k min) | Risk engine gains tier config; report shows collision counts |
| Fixed 1-month, 20-trade threshold | Goalpost risk; unfair baseline comparison | §12 | Pre-registered extension rule; separate baseline portfolios; criteria read from config | Experiment harness change; report templating |
| Intents schema | Couldn't replay market state / per-check outcomes | §13 | Extended columns + replay tool + golden test | Storage migration early in Phase 1 |
| Mac-portability implicit | caffeinate in-tree risk; untested Linux parity | §15 | Wrapper externalized; Linux CI mandatory; Docker at VPS stage | CI pipeline from Phase 1 |

---

## 17. Final verdict

### YELLOW

Architecture is sound and now considerably harder to fool, but the following must be resolved before handing to a coding agent — all are cheap, none require redesign:

1. **Open a Dhan account + API access; verify in person:** sandbox functionality, data-subscription pricing, static-IP self-service flow, Super Order availability for cash equities, and get written confirmation of the §4 "Needs direct confirmation" items.
2. **Confirm the corporate-actions data source** (adjustment correctness gates label validity — Open Question 8 from v1.0, now blocking for Phase 1).
3. **Decide experiment capital** (₹25k with tiered params, or ₹50k+) and lock it into the experiment template BEFORE Phase 1.
4. **Approve the ML demotion** (deterministic-only is the default shipping configuration; ML must earn deployment through the §7 gate).

## 18. Implementation Gate (checklist for the coding agent)

- ☐ Dhan sandbox credentials verified working (order place/query/cancel in sandbox from our machine)
- ☐ `configs/compliance_checklist.yaml` populated with dated confirmations for every §4 non-Confirmed row
- ☐ Corporate-action source chosen and validated on ≥3 known splits/bonuses in NIFTY 200
- ☐ Capital + risk-tier decision recorded in `configs/experiments/exp_001.yaml` (committed before any code depends on it)
- ☐ This document + v1.0 read; the FillModel spec (§1.3) and failure playbook (§14) understood as normative, with their tests written FIRST (test-driven: fill model, risk properties, recovery handlers)
- ☐ Repository scaffold matches v1.0 §22 tree + import-linter dependency rules + Linux CI green on empty scaffold
- ☐ Scope confirmation: Phases 1–8 only; live-order placement stubbed; MODE=paper hard-defaulted

When all boxes are checked: begin Phase 1 (data foundation) per v1.0 §23, incorporating every revision above.

---

## ADDENDUM (2026-08-24): Gate #1 executed — sandbox reality vs this document

Gate #1 was tested live (`docs/DHAN_SANDBOX_VERIFICATION.md`). Result: **BLOCKED** — the Dhan sandbox supports auth + order placement only; order query/cancel/book, market feed, Super Orders, IP APIs and real historical data are absent or synthetic. Minimal corrections to this document:

1. **§18 gate item 1 → partially satisfied:** auth ✅ / client-id ✅ / placement ✅; lifecycle ❌. Remaining closure path recorded in the verification report (support confirmation OR deferred micro-live validation OR alternate broker).
2. **Phase 8 revised:** "sandbox parity stage" replaced by internal PaperBroker lifecycle tests + a thin DhanSandboxSmokeTest (auth/profile/place/correlationId). First true order-lifecycle validation moves to Phase 11 micro-live with 1-share minimum-price orders.
3. **Phase 1 decoupled from Dhan:** research bootstrap uses free historical sources; Dhan production Data API (account currently `dataPlan: Deactive`) becomes a paid upgrade decision before experiment start.
4. **LIMIT-only elevated from convention to hard invariant:** unit test must reject any MARKET-order TradeIntent (sandbox demonstrably accepts MARKET orders, so broker-side conversion cannot be relied on).
5. **Contract tests added:** observed schemas incl. DH-905 string-typing pitfall encoded as fixtures.

No change to strategy, risk, ML, fill-model, or experiment design.
