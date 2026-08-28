# API CONTRACT v1 — binding between backend, lab, and frontend agents

Base: FastAPI app at `src/sts/api/app.py`, mounted by `src/sts/main.py` (uvicorn).
All JSON. Errors: `{"detail": "..."}` with proper status codes. No auth in paper mode.

## REST endpoints

```
GET  /api/lab/summary
  -> {"sessions":[{id,name,status,terminal_state,capital_initial,equity,pnl_abs,return_pct,
       max_dd_pct,trades,wins,win_rate,open_positions,strategy_id,ml_enabled,last_decision_at,
       health:"ok"|"stale"|"faulted", sparkline:[equity floats ~60 pts]}...],
      "best":{...same shape or null},
      "system":{"feed":"OPEN"|"CLOSED"|"STALE","last_tick_age_s":int|null,
                "sessions_running":n,"db_ok":true,"incidents_24h":n,"heartbeat":ts}}
POST /api/sessions                      body:{name,capital_initial:int,mode:"paper",
        universe:"NIFTY200",strategy_id:"pullback-v1"|"random-k",
        risk_profile:"small"|"standard", ml_enabled:false,on_stop_policy:"FLATTEN"|"HOLD"}
  -> 201 {id,...}
GET  /api/sessions/{id}                 -> detail (below)
POST /api/sessions/{id}/start|pause|resume|stop     -> 200 {status}
POST /api/sessions/{id}/clone           body:{name?, overrides?{capital_initial,ml_enabled,risk_profile,on_stop_policy}}
  -> 201 {id}   (new CREATED session; never mutates source)
GET  /api/sessions/{id}/decisions/{intent_id}    -> replay chain (below)
GET  /api/lab/compare?ids=id1,id2
  -> {"sessions":[{id,name,equity_curve:[[iso_date,equity]...],
       by_trade:[[trade_n,cum_pnl]...], metrics:{return_pct,max_dd_pct,win_rate,pf,expectancy,
       avg_win,avg_loss,avg_hold_days,turnover,exposure_pct,cost_drag}}]}
GET  /api/system/health                 -> same as system block in summary
GET  /healthz                           -> {"ok":true}
```

## GET /api/sessions/{id} detail

```
{id,name,status,terminal_state,capital_initial,config:{...effective config incl params...},
 portfolio:{cash,invested,unrealized,realized,equity,hwm,drawdown_pct,gross_exposure,total_open_risk},
 positions:[{symbol,qty,avg_entry,last_px,stop_px,target1_px,target2_px,trail_px,
             unrealized_pnl,pnl_pct,held_days,risk_amount,t1_done}],
 trades:[{symbol,side,qty,entry_px,exit_px,entry_ts,exit_ts,pnl,r_multiple,hold_days,
          entry_reason,exit_reason,costs}],
 equity_curve:[[iso_ts,equity]...], drawdown_curve:[[iso_ts,dd_pct]...],
 funnel_latest:{ts,scanned,eligible,setups,ml_passed,portfolio_ok,risk_ok,selected},
 decisions:[{intent_id,ts,symbol,action,score,rejection_reason}... last 50],
 last_decision_at, feed_status}
```

## GET decision replay

```
{ts,symbol,action,
 market_state_ref,{features:{name:value...},
 rules:[{rule_id,description,observed,threshold,passed}...],
 ml:{enabled,model_id,score,prob}|null,
 portfolio:{cash,equity,open_positions,open_risk},
 risk_checks:[{check,threshold,observed,passed}...],
 rejection_reason,
 order:{order_id,status,filled_qty,avg_fill_px}|null}
```

## Frontend pages (Jinja templates + htmx + vanilla JS/SVG charts)
- `/`            Lab Overview (hero "SWING LAB", best session, ranked cards, filters)
- `/sessions/new` Create form (NO symbol field) + recommended-lineup button
- `/sessions/{id}` Session detail dossier
- `/compare?ids=` Comparison view (calendar-time and trade-number x-axes toggle)
- Polling: cards/detail refresh via hx-trigger every 5s on status/summary partials.

---
# CONTRACT ADDENDUM v2 — audit corrections (binding)

## New/changed endpoints
```
POST /api/sessions/{id}/archive         -> 200 {status:"ARCHIVED"}  (soft; excluded from default lab view)
POST /api/sessions/{id}/restore         -> 200
DELETE /api/sessions/{id}               -> 204 ONLY if status==CREATED (else 409); hard delete allowed only for never-started
POST /api/sessions/{id}/scan            -> 200 {funnel, candidates:[...], deferrals} — diagnostic scan-now against latest data; persists funnel + intents (candidates deferred w/ reason MARKET_CLOSED when feed not OPEN)
GET  /api/sessions/{id}/timeline        -> [{ts,kind,text,detail}...] merged from session_events+intents+orders+fills (last 200)
GET  /api/sessions/{id}                 -> ADD: created_at, started_at, ml_model_id, strategy_version,
     activity:{state:"TRADING"|"SCANNING"|"NO_SETUPS"|"RISK_BLOCKED"|"WAITING_MARKET_OPEN"|"FEED_STALE"|"FAULTED",
               explanation:"human sentence", blocker_detail:{...}},
     last_bar:{symbol,ts,close}|null, latest_journal_event:{ts,kind}|null,
     funnel_latest + rejections:{stage:{reason:count}}
GET  /api/lab/summary                   -> sessions EXCLUDE ARCHIVED by default (?include_archived=1 to add);
     ADD per session: activity state (same enum)
GET  /api/lab/compare                   -> metrics ADD sharpe, sortino, cagr_pct, candidates_total, rejections_top
POST /api/sessions                      -> body ADD optional params:{...strategy overrides...}, risk_overrides:{
     max_positions,max_total_open_risk,max_gross_exposure,daily_loss_limit,drawdown_kill,time_stop_days,
     trail_mult_atr,t1_multiple,t2_multiple,min_notional,risk_per_trade,max_position_pct,max_sector_positions,
     max_sector_exposure,max_correlation,max_adv_participation}; effective resolved values stored in config_yaml + hash
```
## New risk profile
`micro` (default for capital < ₹30,000): risk_per_trade 0.02, max_position_pct 0.50, min_notional 3000.
Rationale: audit proved 1.5%/33%/4000 yields a near-empty feasible sizing envelope for large caps.

## Session detail additions (positions/trades)
positions[] ADD: exchange:"NSE", market_value, entry_ts, entry_reason, ml_score|null
trades[] ADD: fees, slippage, strategy_version, ml_model_id; entry_reason is a REAL reason (no version hacks)

## CANONICAL METRICS (binding as of addendum v2)
`sts.metrics.canonical` is the SINGLE source of truth for all session metrics.
Every consumer (routes_api summary/detail/compare, lab UI, reports) MUST
delegate to it — no inline metric computation. Signatures are pure:
`fn(conn, session_id)`.

| field | definition | rounding |
|---|---|---|
| current_equity | last account_snapshot equity, else capital_initial | raw |
| return_pct / total_return_pct | (current_equity/capital_initial − 1)·100 | 4dp |
| max_dd_pct | max peak-to-trough over the FULL-RESOLUTION snapshot curve | 4dp |
| win_rate | wins/trades | 4dp, null if no trades |
| pf (profit_factor) | gross_profit/gross_loss; gl=0 → gp if wins else null | 4dp |
| expectancy | mean trade P&L in currency | 2dp |
| expectancy_r | mean R-multiple (pnl/initial risk per trade) | raw |
| avg_win / avg_loss | mean win; mean loss NEGATIVE-signed | 2dp |
| avg_hold_days | calendar-day hold averaged over closed trades | 2dp |
| sharpe / sortino | daily last-equity pct returns, rf=0, annualized √252 | 4dp |
| cagr_pct | (end/start)^(365.25/days)−1 over the curve span | 4dp |
| exposure_pct | mean(invested/equity)·100 across snapshots | 4dp |
| turnover | Σ invested / capital_initial | 4dp |
| cost_drag | Σ fill cost totals / capital_initial ·100 | 6dp |

KNOWN API DIVERGENCES (for the API agent to resolve by delegating):
1. `/api/lab/compare` anchors return_pct/max_dd_pct on the DATE-COALESCED
   equity curve (last snapshot per day, first coalesced point as base) —
   canonical/summary anchor on capital_initial over the full-resolution
   curve. These differ whenever the first day ends flat≠start or multiple
   snapshots share a day. compare must switch to canonical.
2. `/api/lab/compare` does not yet expose sharpe/sortino/cagr_pct promised by
   this addendum — read them from `canonical.compare_metrics`.
3. `routes_api._trades` joined a non-existent `orders.symbol` column (would
   500 on any session with a closed trade); fixed schema-side: migration v3
   added orders.symbol (backfilled from intents) and the sink populates it.

## Timestamp standard (binding, 2026-08-26)

All absolute instants are persisted as **tz-aware UTC ISO-8601 strings**
(e.g. `2026-08-26T05:43:09.858884+00:00`). Because every stored timestamp is
UTC ISO, string comparison IS chronological comparison — SQL `ts >= cutoff`
window filters are correct as written.

Naive datetimes anywhere in the runners are the **internal IST clock
convention** (bar ts, decision times): their true instant is naive − 5:30.
Writers must convert via `sts.storage.repos.utc_iso()` (naive→attach +05:30→
UTC; aware passes through). Never persist a naive datetime as-if-UTC.

`incidents_24h` (system block) counts incidents over the interval
**[now − 24h, now], boundary inclusive**, across all sessions.
