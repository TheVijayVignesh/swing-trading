/* ==========================================================================
 * SWING LAB — lab.js
 * Theme · INR formatting · relative time · toasts · modal · filters
 * polling orchestration (single consolidated pollers + JSON hydration) ·
 * card navigation + menus (archive/restore/delete/clone) · lineup confirm ·
 * full-hyperparameter create form · activity/dossier extras · timeline ·
 * diagnostic scan · trade analytics charts · replay · compare enrichment
 * ========================================================================== */
(function () {
  "use strict";

  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));
  const REDUCED = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  function escapeHtml(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }
  function cssVarSafe(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || "#3d4e6b";
  }

  /* ---------------- theme ---------------- */
  const themeBtn = $("#theme-toggle");
  function applyTheme(t) {
    document.documentElement.setAttribute("data-theme", t);
    try { localStorage.setItem("sl-theme", t); } catch (e) {}
    if (themeBtn) {
      themeBtn.textContent = t === "dark" ? "☀" : "☾";
      themeBtn.setAttribute("aria-label",
        t === "dark" ? "Switch to light theme" : "Switch to dark theme");
    }
    redrawCharts();
  }
  function initTheme() {
    let saved = null;
    try { saved = localStorage.getItem("sl-theme"); } catch (e) {}
    applyTheme(saved || "light");
    if (themeBtn) themeBtn.addEventListener("click", () => {
      const cur = document.documentElement.getAttribute("data-theme") || "light";
      applyTheme(cur === "dark" ? "light" : "dark");
    });
  }

  /* ---------------- formatting helpers (used everywhere) ---------------- */
  function formatINR(v, opts) {
    opts = opts || {};
    if (v == null || isNaN(+v)) return opts.fallback || "–";
    const sign = +v < 0 ? "-₹" : (opts.forceSign ? "+₹" : "₹");
    return sign + Math.abs(Math.round(+v)).toLocaleString("en-IN");
  }
  function formatPct(v, opts) {
    opts = opts || {};
    if (v == null || isNaN(+v)) return "–";
    const s = +v >= 0 && opts.forceSign ? "+" : "";
    return s + (+v).toFixed(2) + "%";
  }
  function relTime(iso, fallback) {
    if (!iso) return fallback || "–";
    const then = new Date(iso).getTime();
    if (isNaN(then)) return fallback || "–";
    const s = Math.max(0, Math.round((Date.now() - then) / 1000));
    if (s < 60) return s + "s ago";
    const m = Math.floor(s / 60);
    if (m < 60) return m + "m ago";
    const h = Math.floor(m / 60);
    if (h < 24) return h + "h ago";
    return Math.floor(h / 24) + "d ago";
  }
  function fmtDuration(ms) {
    if (ms == null || ms < 0 || isNaN(ms)) return "—";
    const s = Math.floor(ms / 1000);
    const d = Math.floor(s / 86400);
    const h = Math.floor((s % 86400) / 3600);
    const m = Math.floor((s % 3600) / 60);
    const sec = s % 60;
    if (d > 0) return d + "d " + h + "h " + m + "m";
    if (h > 0) return h + "h " + m + "m " + sec + "s";
    return m + "m " + sec + "s";
  }

  /* Normalize any [data-inr] / [data-pct] / [data-rel] nodes to canonical text */
  function hydrateFormatters(root) {
    $$("[data-inr]", root).forEach((n) => { n.textContent = formatINR(n.dataset.inr); });
    $$("[data-pct]", root).forEach((n) => {
      n.textContent = formatPct(n.dataset.pct, { forceSign: n.hasAttribute("data-sign") });
      const v = parseFloat(n.dataset.pct);
      if (!isNaN(v)) {
        n.classList.toggle("pos", v > 0);
        n.classList.toggle("neg", v < 0);
      }
    });
    $$("[data-rel]", root).forEach((n) => { n.textContent = relTime(n.dataset.rel); });
  }

  /* ---------------- toasts (aria-live polite) ---------------- */
  let toastRegion = null;
  function ensureToastRegion() {
    if (toastRegion) return toastRegion;
    toastRegion = document.createElement("div");
    toastRegion.className = "toast-region";
    toastRegion.setAttribute("role", "status");
    toastRegion.setAttribute("aria-live", "polite");
    document.body.appendChild(toastRegion);
    return toastRegion;
  }
  function toast(msg, ms) {
    const region = ensureToastRegion();
    const t = document.createElement("div");
    t.className = "toast";
    t.textContent = msg;
    region.appendChild(t);
    setTimeout(() => {
      t.classList.add("leaving");
      setTimeout(() => t.remove(), 220);
    }, ms || 4200);
  }

  /* ---------------- fetch wrapper ---------------- */
  async function api(method, url, body) {
    let res;
    try {
      res = await fetch(url, {
        method,
        headers: body ? { "Content-Type": "application/json" } : undefined,
        body: body ? JSON.stringify(body) : undefined,
      });
    } catch (e) {
      toast("Network error — is the lab process running?");
      throw e;
    }
    if (!res.ok) {
      let detail = res.status + " " + res.statusText;
      try { const j = await res.json(); if (j && j.detail) detail = j.detail; } catch (e) {}
      toast("Request failed: " + detail);
      const err = new Error(detail);
      err.status = res.status;
      throw err;
    }
    if (res.status === 204) return null;
    return res.json();
  }

  /* ---------------- confirm modal (focus trap, Esc closes) ---------------- */
  function confirmModal(opts) {
    return new Promise((resolve) => {
      const backdrop = document.createElement("div");
      backdrop.className = "modal-backdrop";
      backdrop.innerHTML =
        '<div class="modal" role="dialog" aria-modal="true" aria-labelledby="modal-title">' +
        '<h2 id="modal-title"></h2><p class="dim"></p>' +
        (opts.extraHtml || "") +
        '<div class="modal-actions">' +
        '<button type="button" class="btn btn-quiet" data-x="cancel">Cancel</button>' +
        '<button type="button" class="btn ' + (opts.danger ? "btn-danger" : "btn-primary") +
        '" data-x="ok">' + (opts.okLabel || "Confirm") + "</button></div></div>";
      $("h2", backdrop).textContent = opts.title || "Are you sure?";
      $("p", backdrop).textContent = opts.body || "";
      document.body.appendChild(backdrop);

      const okBtn = $('[data-x="ok"]', backdrop);
      const cancelBtn = $('[data-x="cancel"]', backdrop);
      const prevFocus = document.activeElement;
      okBtn.focus();

      function close(result) {
        document.removeEventListener("keydown", onKey, true);
        backdrop.remove();
        if (prevFocus && prevFocus.focus) prevFocus.focus();
        resolve(result);
      }
      function onKey(e) {
        if (e.key === "Escape") { e.stopPropagation(); close(false); }
        else if (e.key === "Tab") {
          const focusables = $$("button, input, select, [tabindex]:not([tabindex='-1'])", backdrop)
            .filter((n) => !n.disabled);
          if (!focusables.length) return;
          const i = focusables.indexOf(document.activeElement);
          e.preventDefault();
          const next = e.shiftKey
            ? focusables[(i - 1 + focusables.length) % focusables.length]
            : focusables[(i + 1) % focusables.length];
          next.focus();
        }
      }
      document.addEventListener("keydown", onKey, true);
      okBtn.addEventListener("click", () => close(true));
      cancelBtn.addEventListener("click", () => close(false));
      backdrop.addEventListener("mousedown", (e) => { if (e.target === backdrop) close(false); });
    });
  }

  /* ---------------- petals (hero only) ---------------- */
  function initPetals() {
    const host = $(".petals");
    if (!host || REDUCED) return;
    const COUNT = 5;
    for (let i = 0; i < COUNT; i++) {
      const wrap = document.createElementNS("http://www.w3.org/2000/svg", "svg");
      wrap.setAttribute("viewBox", "0 0 16 16");
      wrap.setAttribute("width", "13");
      wrap.setAttribute("height", "13");
      wrap.setAttribute("class", "petal");
      wrap.innerHTML =
        '<path d="M8 1 C12 4 13 9 8 15 C3 9 4 4 8 1 Z" fill="currentColor"/>';
      wrap.style.color = "var(--accent)";
      wrap.style.left = (6 + Math.random() * 70) + "%";
      wrap.style.opacity = String(0.18 + Math.random() * 0.17); // ≤ .35
      wrap.style.animationDuration = (14 + Math.random() * 10) + "s";
      wrap.style.animationDelay = (-Math.random() * 20) + "s";
      host.appendChild(wrap);
    }
  }

  /* ---------------- ink-stroke fade on scroll (transform/opacity only) ---------------- */
  function initScrollFade() {
    if (REDUCED) return;
    const targets = $$(".ink-stroke, .rising-disc, .side-label");
    if (!targets.length) return;
    let ticking = false;
    window.addEventListener("scroll", () => {
      if (ticking) return;
      ticking = true;
      requestAnimationFrame(() => {
        const y = window.scrollY;
        const f = Math.max(0, 1 - y / 480);
        targets.forEach((t) => {
          t.style.opacity = String(f * baseOpacity(t));
          t.style.transform = "translateY(" + Math.min(24, y * 0.06) + "px)";
        });
        ticking = false;
      });
    }, { passive: true });
    function baseOpacity(t) {
      const cs = getComputedStyle(t);
      const o = parseFloat(cs.opacity);
      t.dataset.baseOpacity = String(o);
      return o;
    }
  }

  /* ---------------- chart registry & redraw ---------------- */
  const chartJobs = [];   // {svg, kind:'line'|'bar', job:{data,opts}, drawn}
  function registerChart(svgEl, job, kind) {
    const entry = { svg: svgEl, job, kind: kind || "line", drawn: false };
    chartJobs.push(entry);
    runChartJob(entry);
  }
  function dropChartJobs(svgEl) {
    for (let i = chartJobs.length - 1; i >= 0; i--) {
      if (chartJobs[i].svg === svgEl) chartJobs.splice(i, 1);
    }
  }
  function runChartJob(entry) {
    const data = entry.job.data();
    if (!data) return;
    if (entry.kind === "bar") {
      window.SLCharts.barChart(entry.svg, data.values,
        Object.assign({}, entry.job.opts, {}));
    } else {
      window.SLCharts.lineChart(entry.svg, data.series,
        Object.assign({}, entry.job.opts, { drawIn: !entry.drawn }));
    }
    entry.drawn = true;
  }
  function redrawCharts() {
    // colors are CSS-var driven; re-render after theme flip
    chartJobs.forEach(runChartJob);
  }

  /* =====================================================================
   * OVERVIEW PAGE — ONE consolidated 5s JSON poll drives cards, health
   * strip, footer heartbeat AND the recent-decisions strip.
   * ==================================================================== */
  const PALETTE = ["#c73e2e", "#3d4e6b", "#3e6b4f", "#9c7c2e", "#7a4a6b", "#4a7a86"];
  function paletteColor(i) { return PALETTE[i % PALETTE.length]; }

  let archivedFilterOn = false;

  function buildCardEl(s) {
    const art = document.createElement("article");
    art.className = "session-card entering";
    art.dataset.id = s.id || "";
    art.dataset.status = s.status || "";
    art.dataset.health = s.health || "ok";
    art.dataset.ml = s.ml_enabled ? "on" : "off";
    art.dataset.strategy = s.strategy_id || "";
    art.dataset.archived = s.archived ? "1" : "0";
    art.tabIndex = 0;
    art.setAttribute("role", "link");
    art.setAttribute("aria-label", "Open session " + (s.name || "Untitled"));
    const ret = s.return_pct || 0;
    const pnl = s.pnl_abs || 0;
    art.innerHTML =
      (s.archived ? '<span class="chip chip-accent archived-flag">Archived</span>' : "") +
      '<div class="card-head">' +
      '<a class="card-name" href="/sessions/' + encodeURIComponent(s.id) + '">' + escapeHtml(s.name || "Untitled session") + "</a>" +
      '<span style="display:flex;align-items:center;gap:8px">' +
      '<span class="chip">' + escapeHtml(s.status || "—") + "</span>" +
      (s.terminal_state ? '<span class="chip chip-accent">' + escapeHtml(s.terminal_state) + "</span>" : "") +
      '<span class="status-dot" data-s="' + escapeHtml(s.status || "") + '" role="img" aria-label="status"></span>' +
      cardMenuHtml(s.id, s.name, s.status, s.archived) +
      "</span></div>" +
      '<div class="equity-row"><span class="label">Equity</span>' +
      '<span class="equity-num" data-equity data-cur="' + (s.equity || 0) + '">' + formatINR(s.equity || 0) + "</span>" +
      '<span class="return-pct" data-return-pct data-cur="' + ret + '">' + (ret >= 0 ? "+" : "") + ret.toFixed(2) + "%</span></div>" +
      '<div class="card-stats">' +
      '<span class="stat"><span class="label">P&amp;L</span><span class="v" data-pnl-abs>' + formatINR(pnl, { forceSign: pnl > 0 }) + "</span></span>" +
      '<span class="stat"><span class="label">Max DD</span><span class="v" data-max-dd>' + formatPct(s.max_dd_pct || 0) + "</span></span>" +
      '<span class="stat"><span class="label">Trades</span><span class="v" data-trades-line>' + (s.trades || 0) + " trades · " +
      (s.win_rate != null ? (s.win_rate * 100).toFixed(0) : "0") + "% win</span></span>" +
      '<span class="stat"><span class="label">Open pos</span><span class="v" data-open-positions>' + (s.open_positions || 0) + "</span></span>" +
      '<span class="stat"><span class="label">Capital</span><span class="v">' + formatINR(s.capital_initial || 0) + "</span></span>" +
      '</div><div class="spark-wrap"></div>' +
      '<div class="card-foot"><span style="display:flex;gap:6px;flex-wrap:wrap;align-items:center">' +
      '<span class="chip' + (s.ml_enabled ? " chip-on" : "") + '">ML ' + (s.ml_enabled ? "ON" : "OFF") + "</span>" +
      '<span class="chip">' + escapeHtml(s.strategy_id || "strategy?") + "</span>" +
      '<span class="health-label" data-health-text="' + (s.health || "ok") + '">health: ' + healthText(s.health) + "</span>" +
      "</span><span>last decision <span data-last-decision>" + relTime(s.last_decision_at, "no decisions yet") + "</span></span></div>";
    if (Array.isArray(s.sparkline) && s.sparkline.length > 1) {
      const spark = document.createElementNS("http://www.w3.org/2000/svg", "svg");
      spark.dataset.spark = JSON.stringify(s.sparkline);
      $(".spark-wrap", art).appendChild(spark);
      window.SLCharts.sparkline(spark, s.sparkline);
    }
    return art;
  }

  function healthText(h) {
    return h === "faulted" ? "faulted" : (h === "stale" ? "stale feed" : "ok");
  }

  function cardMenuHtml(sid, sname, status, archived) {
    return '<details class="card-menu"><summary aria-label="Session actions menu" title="Session actions">⋯</summary>' +
      '<div class="menu-body" role="menu">' +
      '<button type="button" role="menuitem" data-card-action="archive" data-sid="' + sid + '">Archive</button>' +
      '<button type="button" role="menuitem" data-card-action="restore" data-sid="' + sid + '"' +
      (archived ? "" : " hidden") + ">Restore</button>" +
      '<button type="button" role="menuitem" data-card-action="clone" data-sid="' + sid + '" data-sname="' + escapeHtml(sname || "") + '">Clone…</button>' +
      '<button type="button" role="menuitem" data-card-action="delete" data-sid="' + sid + '" data-status="' + escapeHtml(status || "") + '">Delete…</button>' +
      "</div></details>";
  }

  function initOverview() {
    if (!$("[data-page='overview']") && !$("#lab-grid")) return;

    $$(".spark-wrap svg[data-spark]").forEach((svg) =>
      window.SLCharts.sparkline(svg, JSON.parse(svg.dataset.spark))
    );

    async function pollSummary() {
      let data;
      try {
        data = await api("GET",
          "/api/lab/board" + (archivedFilterOn ? "?include_archived=1" : ""));
      } catch (e) {
        // board endpoint unavailable → fall back to plain summary
        try { data = await api("GET", "/api/lab/summary"); } catch (e2) { return; }
      }
      updateHealth(data.system);
      const seen = new Set();
      (data.sessions || []).forEach((s) => { seen.add(s.id); updateCard(s); });
      // cards removed server-side (archive toggle / hard delete) fade out
      $$(".session-card[data-id]").forEach((card) => {
        if (!seen.has(card.dataset.id)) card.remove();
      });
      updateRecentDecisions(data.recent_decisions);
      const faulted = (data.sessions || []).filter((s) => s.health === "faulted").length;
      if (faulted > 0) announce(faulted + " session(s) in faulted state");
    }

    function updateRecentDecisions(items) {
      const host = $("[data-recent-decisions]");
      if (!host || !items) return;
      let list = $(".recent-strip", host);
      if (!list) {
        host.innerHTML = '<span class="label">Recent decisions · last 8 across sessions</span>';
        list = document.createElement("ul");
        list.className = "recent-strip";
        host.appendChild(list);
      }
      list.innerHTML = "";
      items.forEach((r) => {
        const li = document.createElement("li");
        li.className = "recent-item";
        li.innerHTML =
          '<a href="/sessions/' + encodeURIComponent(r.session_id) + '" class="recent-session">' +
          escapeHtml(r.session_name || "session") + "</a>" +
          '<span class="action-chip" data-a="' + escapeHtml(r.action || "") + '">' + escapeHtml(r.action || "—") + "</span>" +
          '<span class="sym mono">' + escapeHtml(r.symbol || "—") + "</span>" +
          (r.rejection_reason ? '<span class="dim">' + escapeHtml(r.rejection_reason) + "</span>" : "") +
          '<time class="dim mono" data-rel="' + r.ts + '">' + relTime(r.ts) + "</time>";
        list.appendChild(li);
      });
      if (!items.length) {
        host.innerHTML += '<p class="dim" style="padding:10px 0">No decisions journaled yet.</p>';
      }
    }

    function updateHealth(sys) {
      if (!sys) return;
      const strip = $(".health-strip");
      if (!strip) return;
      const feed = $("[data-health-feed]", strip);
      if (feed) {
        feed.textContent = sys.feed || "–";
        feed.dataset.v = sys.feed || "";
        const ageEl = $("[data-feed-age]", strip);
        if (ageEl) {
          ageEl.textContent = sys.last_tick_age_s == null ? "no ticks"
            : sys.last_tick_age_s + "s since last tick";
        }
      }
      const db = $("[data-health-db]", strip);
      if (db) db.textContent = sys.db_ok === false ? "degraded" : "ok";
      const run = $("[data-health-running]", strip);
      if (run) run.textContent = String(sys.sessions_running != null ? sys.sessions_running : "–") + " running";
      const inc = $("[data-health-incidents]", strip);
      if (inc) inc.textContent = (sys.incidents_24h != null ? sys.incidents_24h : "–") + " incidents 24h";
      const hb = $("[data-heartbeat]");
      if (hb) hb.textContent = relTime(sys.heartbeat, "waiting…").replace(" ago", "");
      updateFeedHealth(strip, sys.feed_health, sys.feed_degraded);
    }

    // Activity-state icon/label metadata — mirrors the session_body.html
    // banner map; INITIALIZING covers the pre-first-poll session state.
    const ACTIVITY_STATE_META = {
      TRADING: { icon: "◉", label: "TRADING" },
      SCANNING: { icon: "◌", label: "SCANNING" },
      NO_SETUPS: { icon: "∅", label: "NO SETUPS" },
      RISK_BLOCKED: { icon: "⊘", label: "RISK BLOCKED" },
      WAITING_MARKET_OPEN: { icon: "◔", label: "WAITING FOR MARKET OPEN" },
      FEED_STALE: { icon: "≈", label: "FEED STALE" },
      FAULTED: { icon: "✕", label: "FAULTED" },
      INITIALIZING: { icon: "◐", label: "INITIALIZING" }
    };

    function humanAge(s) {
      if (s == null || isNaN(s)) return "—";
      return s < 90 ? s + "s" : Math.round(s / 60) + "m";
    }

    function fmtLastBar(lb) {
      let hhmmss = "";
      try {
        // The API persists bar ts as naive IST (runner frame). JS new Date()
        // would interpret that as browser-local wall time, drifting off by
        // the local-vs-IST offset. Attach the IST offset before parsing when
        // the string lacks one, so the formatter always renders IST.
        let ts = String(lb.ts || "");
        if (ts && !/Z$|[+-]\d{2}:?\d{2}$/.test(ts)) {
          ts = ts + "+05:30";
        }
        hhmmss = new Date(ts).toLocaleTimeString("en-IN",
          { timeZone: "Asia/Kolkata", hour12: false });
      } catch (e) { hhmmss = ""; }
      return escapeHtml(String(lb.symbol || "?")) + " " +
        (hhmmss || escapeHtml(String(lb.ts || ""))) + " IST";
    }

    function setSide(el, side) {
      if (!el) return;
      const st = side && side.status ? String(side.status).toUpperCase() : "UNKNOWN";
      el.textContent = st;
      el.dataset.status = st;
      el.title = (side && side.name ? side.name : "") +
        (side && side.consecutive_failures ? " · " + side.consecutive_failures + " consecutive failures" : "") +
        (side && side.last_error ? " · " + side.last_error : "");
    }

    function updateFeedHealth(strip, fh, degraded) {
      const block = $("#feed-health", strip);
      if (!block) return;
      fh = fh || {};
      block.dataset.degraded = degraded ? "1" : "0";
      const setText = (sel, v) => { const n = $(sel, strip); if (n) n.textContent = v; };
      setText("[data-feed-source]", fh.source || "—");
      setText("[data-feed-state]", fh.state || "—");
      const lbEl = $("[data-feed-last-bar]", strip);
      if (lbEl) lbEl.innerHTML = fh.last_bar ? fmtLastBar(fh.last_bar) : "none yet";
      setText("[data-feed-age-h]",
        fh.last_bar ? humanAge(fh.last_bar.age_s) : "—");
      setText("[data-feed-dropped]",
        String(fh.dropped_events != null ? fh.dropped_events : 0));
      setSide($("[data-feed-primary]", strip), fh.primary);
      setSide($("[data-feed-fallback]", strip), fh.fallback);
    }

    function updateCard(s) {
      let card = $('.session-card[data-id="' + CSS.escape(s.id) + '"]');
      if (!card) {
        // new session appeared → DOM append (never location.reload)
        const grid = $("#lab-grid");
        if (!grid) return;
        const emptyMsg = $("#overview-empty");
        if (emptyMsg) emptyMsg.remove();
        card = buildCardEl(s);
        grid.appendChild(card);
        applyFilters();
        toast("New session appeared: " + (s.name || s.id));
      }
      card.dataset.archived = s.archived ? "1" : "0";
      const sdot = $(".status-dot", card);
      if (sdot && s.activity_state) {
        const meta = ACTIVITY_STATE_META[s.activity_state];
        sdot.title = meta ? meta.icon + " " + meta.label : String(s.activity_state);
      }
      const flag = $(".archived-flag", card);
      if (flag && !s.archived) flag.remove();
      const restoreBtn = $('[data-card-action="restore"]', card);
      if (restoreBtn) restoreBtn.hidden = !s.archived;
      const setNum = (sel, val, fmtFn) => {
        const elNode = $(sel, card);
        if (!elNode) return;
        elNode.removeAttribute("data-inr"); // JS owns this value from now on
        const from = parseFloat(elNode.dataset.cur);
        window.SLCharts.animateNumber(elNode, isNaN(from) ? val : from, val,
          { fmt: fmtFn || ((x) => formatINR(x)) });
        elNode.dataset.cur = String(val);
      };
      setNum("[data-equity]", s.equity);
      setNum("[data-pnl-abs]", s.pnl_abs);
      const ret = $("[data-return-pct]", card);
      if (ret) {
        const from = parseFloat(ret.dataset.cur);
        const v = s.return_pct || 0;
        window.SLCharts.animateNumber(ret, isNaN(from) ? v : from, v, {
          fmt: (x) => (x >= 0 ? "+" : "") + x.toFixed(2) + "%",
        });
        ret.dataset.cur = String(v);
        ret.classList.toggle("pos", v > 0);
        ret.classList.toggle("neg", v < 0);
      }
      const dd = $("[data-max-dd]", card);
      if (dd) dd.textContent = formatPct(s.max_dd_pct);
      const dot = $(".status-dot", card);
      if (dot) dot.dataset.s = s.status || dot.dataset.s;
      const pos = $("[data-open-positions]", card);
      if (pos) pos.textContent = String(s.open_positions != null ? s.open_positions : 0);
      const trades = $("[data-trades-line]", card);
      if (trades) trades.textContent =
        (s.trades != null ? s.trades : 0) + " trades · " +
        (s.win_rate != null ? (s.win_rate * 100).toFixed(0) : "–") + "% win";
      const lastDec = $("[data-last-decision]", card);
      if (lastDec) lastDec.textContent = relTime(s.last_decision_at, "no decisions yet");
      const healthLbl = $("[data-health-text]", card);
      if (healthLbl) {
        healthLbl.dataset.healthText = s.health || "ok";
        healthLbl.textContent = "health: " + healthText(s.health);
      }
      card.dataset.health = s.health || "ok";
      card.dataset.status = s.status || "";
      const sparkSvg = $(".spark-wrap svg[data-spark]", card);
      if (sparkSvg && s.sparkline) {
        sparkSvg.dataset.spark = JSON.stringify(s.sparkline);
        window.SLCharts.sparkline(sparkSvg, s.sparkline);
      } else if (!sparkSvg && Array.isArray(s.sparkline) && s.sparkline.length > 1) {
        const wrap = $(".spark-wrap", card);
        if (wrap) {
          const sp = document.createElementNS("http://www.w3.org/2000/svg", "svg");
          sp.dataset.spark = JSON.stringify(s.sparkline);
          wrap.appendChild(sp);
          window.SLCharts.sparkline(sp, s.sparkline);
        }
      }
    }

    pollSummary();
    setInterval(pollSummary, 5000);   // the ONLY 5s poller on this page
  }

  /* ---------------- filters (client-side, data attributes) ---------------- */
  function applyFilters() {
    const bar = $("[data-filters]");
    if (!bar) return;
    const pressed = $$(".filter-chip[data-filter-status]", bar)
      .find((c) => c.getAttribute("aria-pressed") === "true");
    const statusFilter = pressed ? pressed.dataset.filterStatus : "ALL";
    const mlSel = $("[data-filter-ml]", bar);
    const stratSel = $("[data-filter-strategy]", bar);
    const ml = mlSel ? mlSel.value : "ANY";
    const strat = stratSel ? stratSel.value : "ANY";
    let visible = 0;
    $$(".session-card").forEach((card) => {
      const okS = statusFilter === "ALL" ||
        (statusFilter === "TERMINAL"
          ? ["STOPPED", "ABORTED"].includes(card.dataset.status)
          : card.dataset.status === statusFilter);
      const okM = ml === "ANY" || String(card.dataset.ml).toLowerCase() === ml;
      const okStrat = strat === "ANY" || card.dataset.strategy === strat;
      const okArch = archivedFilterOn || card.dataset.archived !== "1";
      const show = okS && okM && okStrat && okArch;
      card.hidden = !show;
      if (show) visible++;
    });
    const emptyMsg = $("#grid-empty");
    if (emptyMsg) emptyMsg.hidden = visible !== 0;
  }

  function initFilters() {
    const bar = $("[data-filters]");
    if (!bar) return;
    bar.addEventListener("click", (e) => {
      const chip = e.target.closest(".filter-chip[data-filter-status]");
      if (chip) {
        $$(".filter-chip[data-filter-status]", bar).forEach((c) =>
          c.setAttribute("aria-pressed", String(c === chip)));
        applyFilters();
        return;
      }
      const arch = e.target.closest("[data-filter-archived]");
      if (arch) {
        archivedFilterOn = !archivedFilterOn;
        arch.setAttribute("aria-pressed", String(archivedFilterOn));
        applyFilters();
      }
    });
    const mlSel = $("[data-filter-ml]", bar);
    if (mlSel) mlSel.addEventListener("change", applyFilters);
    const stratSel = $("[data-filter-strategy]", bar);
    if (stratSel) stratSel.addEventListener("change", applyFilters);
  }

  /* ---------------- P0-1 whole-card navigation (delegated) --------------- */
  function initCardNavigation() {
    document.addEventListener("click", (e) => {
      const card = e.target.closest(".session-card[role='link']");
      if (!card) return;
      if (e.target.closest("a, button, summary, select, input, .menu-body")) return;
      const link = $("a.card-name", card);
      if (link) window.location.href = link.href;
    });
    document.addEventListener("keydown", (e) => {
      if (e.key !== "Enter" && e.key !== " ") return;
      const card = e.target.closest && e.target.closest(".session-card[role='link']");
      if (!card || e.target !== card) return;
      e.preventDefault();
      const link = $("a.card-name", card);
      if (link) window.location.href = link.href;
    });
  }

  /* ---------------- archive / delete / clone menus (cards + detail) ------ */
  async function doArchive(sid) {
    const ok = await confirmModal({
      title: "Archive this session?",
      body: "Archived sessions are hidden from the default overview. Nothing is deleted — restore any time from the Archived filter.",
      okLabel: "Archive",
    });
    if (!ok) return;
    try {
      await api("POST", "/api/sessions/" + encodeURIComponent(sid) + "/archive");
      toast("Session archived.");
      refreshAfterMutation(sid);
    } catch (err) { /* toast shown */ }
  }
  async function doRestore(sid) {
    try {
      await api("POST", "/api/sessions/" + encodeURIComponent(sid) + "/restore");
      toast("Session restored.");
      refreshAfterMutation(sid);
    } catch (err) { /* toast shown */ }
  }
  async function doDelete(sid, status) {
    const eligible = status === "CREATED";
    const ok = await confirmModal({
      title: "Delete this session?",
      danger: true,
      okLabel: "Delete permanently",
      body: eligible
        ? "This never-started session will be removed from the journal. This cannot be undone."
        : "Only never-started (CREATED) sessions can be deleted — the API will refuse anything with history. Prefer Archive instead.",
    });
    if (!ok) return;
    try {
      await api("DELETE", "/api/sessions/" + encodeURIComponent(sid));
      toast("Session deleted.");
      refreshAfterMutation(sid);
    } catch (err) {
      if (err.status === 409) toast("Not deletable: only never-started sessions qualify — archive it instead.");
    }
  }
  function doClone(sid, sname) {
    window.location.href = "/sessions/new?clone=" + encodeURIComponent(sid) +
      (sname ? "&from=" + encodeURIComponent(sname) : "");
  }
  function refreshAfterMutation(sid) {
    const card = $('.session-card[data-id="' + CSS.escape(sid) + '"]');
    if (card && !$("#lab-grid")) {
      // on the detail page reflect immediately
      refreshBody();
    }
  }

  function initMenus() {
    document.addEventListener("click", (e) => {
      const btn = e.target.closest("[data-card-action], [data-session-action]");
      if (!btn) return;
      e.stopPropagation();
      const action = btn.dataset.cardAction || btn.dataset.sessionAction;
      const sid = btn.dataset.sid;
      if (!sid) return;
      const details = btn.closest("details");
      if (details) details.open = false;
      if (action === "archive") doArchive(sid);
      else if (action === "restore") doRestore(sid);
      else if (action === "delete") doDelete(sid, btn.dataset.status || "");
      else if (action === "clone") doClone(sid, btn.dataset.sname || "");
    });
    // close open menus on outside click
    document.addEventListener("click", (e) => {
      $$("details.card-menu[open]").forEach((d) => {
        if (!d.contains(e.target)) d.open = false;
      });
    });
  }

  /* ---------------- P0-2 recommended lineup — always confirms first ----- */
  const LINEUP_DEFS = [
    { suffix: "hybrid-main", strategy_id: "pullback-v1", ml_enabled: true },
    { suffix: "det-only", strategy_id: "pullback-v1", ml_enabled: false },
    { suffix: "random-k", strategy_id: "random-k", ml_enabled: false },
  ];
  function initLineup() {
    document.addEventListener("click", async (e) => {
      const btn = e.target.closest("[data-recommended-lineup]");
      if (!btn) return;
      e.preventDefault();
      e.stopPropagation();
      const capInput = $(btn.dataset.lineupCapitalInput || "#f-capital");
      const cap = parseInt(capInput && capInput.value ? capInput.value : "25000", 10) || 25000;

      // modal lists EXACTLY what will be created; nothing happens before confirm
      const rows = LINEUP_DEFS.map((d) =>
        '<tr><td>lineup-' + d.suffix + "</td><td>" + d.strategy_id + "</td><td>" +
        (d.ml_enabled ? "ML ON" : "ML OFF") + '</td><td class="mono" data-cap-cell>' +
        formatINR(cap) + "</td></tr>").join("");
      const ok = await confirmModal({
        title: "Create and start the recommended lineup?",
        okLabel: "Create Lineup",
        extraHtml:
          '<table class="data" style="margin-top:12px"><thead>' +
          '<tr><th scope="col">name</th><th scope="col">strategy</th><th scope="col">ml</th><th scope="col">capital</th></tr>' +
          "</thead><tbody>" + rows + "</tbody></table>" +
          '<label class="label" for="lineup-capital" style="margin-top:12px">Capital per session (₹)</label>' +
          '<input type="number" id="lineup-capital" min="1000" step="500" value="' + cap + '">',
        body: "Three fresh paper sessions will be created and started immediately.",
      });
      if (!ok) { toast("Lineup cancelled — nothing was created."); return; }
      const newCap = parseInt($("#lineup-capital").value, 10);
      if (!(newCap >= 1000)) { toast("Capital must be at least ₹1,000."); return; }

      btn.disabled = true;
      btn.textContent = "Preparing lineup…";
      const ids = [];
      try {
        for (const d of LINEUP_DEFS) {
          const r = await api("POST", "/api/sessions", {
            name: "lineup-" + d.suffix,
            capital_initial: newCap,
            mode: "paper",
            universe: "NIFTY200",
            strategy_id: d.strategy_id,
            risk_profile: newCap < 30000 ? "micro" : "small",
            ml_enabled: d.ml_enabled,
            on_stop_policy: "FLATTEN",
          });
          ids.push(r.id);
        }
        for (const id of ids) await api("POST", "/api/sessions/" + id + "/start");
        toast("Lineup started: hybrid-main, det-only, random-k.");
        window.location.href = "/";
      } catch (err) {
        btn.disabled = false;
        btn.textContent = "Start Recommended Lineup";
      }
    });
  }

  /* =====================================================================
   * SESSION NEW FORM — full hyperparameters, effective-config panel,
   * numeric validation, profile auto-suggestion, clone prefill.
   * ==================================================================== */
  const STRATEGY_DEFAULTS = {
    sma_fast: 20, sma_slow: 50, slope_lookback: 10, rsi_period: 14,
    rsi_low: 45, rsi_high: 70, atr_period: 14, stop_atr_mult: 1.5,
    trail_atr_mult: 1.5, vol_lookback: 20, vol_mult: 1.5,
    pullback_lookback: 5, pullback_atr_thresh: 1.0, breakout_lookback: 20,
    entry_window_end: "14:30", min_rr: 1.5,
  };
  const PROFILE_VALUES = {
    micro: { risk_per_trade: 0.02, max_position_pct: 0.60, min_notional: 3000 },
    small: { risk_per_trade: 0.015, max_position_pct: 0.33, min_notional: 4000 },
    standard: { risk_per_trade: 0.01, max_position_pct: 0.20, min_notional: 5000 },
  };
  const RISK_DEFAULTS = {
    max_positions: 4, max_total_open_risk: 0.02, max_gross_exposure: 0.8,
    daily_loss_limit: 0.03, drawdown_kill: 0.10, max_sector_positions: 2,
    max_sector_exposure: 0.40, max_correlation: 0.7, max_adv_participation: 0.005,
    t1_multiple: 1.0, t2_multiple: 3.0, time_stop_days: 10,
  };

  function initNewSession() {
    const form = $("[data-new-session-form]");
    if (!form) return;
    const honeypot = form.querySelector('input[name="symbol"]');
    const effPanel = $("#effective-config");
    const payloadPreview = $("#config-preview");
    const suggestHint = $("#risk-suggest-hint");

    const dirtyRisk = new Set();     // fields the user explicitly overrode
    let profileTouched = false;      // user chose a profile explicitly

    function selectedProfile() {
      const elNode = form.querySelector('input[name="risk_profile"]:checked');
      return elNode ? elNode.value : "small";
    }
    function capital() {
      return parseInt($("#f-capital").value, 10) || 0;
    }

    // profile change → prefill the profile-resolved trio unless overridden
    $$('input[name="risk_profile"]', form).forEach((radio) => {
      radio.addEventListener("change", () => {
        profileTouched = true;
        suggestHint.textContent = "";
        fillProfileFields();
        refreshEffConfig();
      });
    });

    function fillProfileFields() {
      const pv = PROFILE_VALUES[selectedProfile()] || PROFILE_VALUES.small;
      ["min_notional", "risk_per_trade", "max_position_pct"].forEach((k) => {
        const input = form.querySelector('[data-risk-param="' + k + '"]');
        if (input && !dirtyRisk.has(k)) input.value = String(pv[k]);
      });
    }
    $$("[data-risk-param]", form).forEach((input) => {
      input.addEventListener("input", () => dirtyRisk.add(input.dataset.riskParam));
    });

    // auto-suggestion by capital
    $("#f-capital").addEventListener("input", () => {
      const cap = capital();
      if (!profileTouched && cap > 0 && cap < 30000 &&
          selectedProfile() !== "micro") {
        const microRadio = form.querySelector('input[name="risk_profile"][value="micro"]');
        if (microRadio) microRadio.checked = true;
        suggestHint.textContent =
          "Suggested: micro profile — under ₹30,000 the standard/small sizing envelope is near-empty.";
      }
      refreshEffConfig();
    });

    function collectParams() {
      const params = {};
      $$('[name^="param:"]', form).forEach((input) => {
        const key = input.name.slice("param:".length);
        let v;
        if (input.type === "time") v = input.value || STRATEGY_DEFAULTS[key];
        else v = input.dataset.paramType === "int"
          ? parseInt(input.value, 10)
          : parseFloat(input.value);
        if (v !== undefined && !isNaN(v)) params[key] = v;
        else if (typeof v === "string" && v) params[key] = v;
      });
      // t1/t2/time_stop are contract-level risk_overrides but live with strategy knobs
      return params;
    }
    function collectRiskOverrides() {
      const ov = {};
      $$("[data-risk-param]", form).forEach((input) => {
        const k = input.dataset.riskParam;
        const v = input.dataset.paramType === "int"
          ? parseInt(input.value, 10) : parseFloat(input.value);
        if (!isNaN(v)) ov[k] = v;
      });
      Object.entries(RISK_DEFAULTS).forEach(([k, dv]) => {
        if (!(k in ov)) ov[k] = dv;
      });
      return ov;
    }

    function validate() {
      const problems = [];
      const name = $("#f-name").value.trim();
      if (!name) problems.push("name your experiment first");
      const cap = parseInt($("#f-capital").value, 10);
      if (!(cap >= 1000)) problems.push("initial capital must be an integer ≥ ₹1,000");
      $$("input[type='number'][name^='param:'], input[type='number'][data-risk-param]", form)
        .forEach((input) => {
          if (input.value === "") return;
          const v = parseFloat(input.value);
          const mn = parseFloat(input.min), mx = parseFloat(input.max);
          if (isNaN(v)) problems.push(input.name.replace(/^(param|risk):/, "") + " must be numeric");
          else if (!isNaN(mn) && v < mn) problems.push(
            input.name.replace(/^(param|risk):/, "") + " below minimum " + mn);
          else if (!isNaN(mx) && v > mx) problems.push(
            input.name.replace(/^(param|risk):/, "") + " above maximum " + mx);
        });
      return problems;
    }

    function resolvedEffConfig() {
      const pv = PROFILE_VALUES[selectedProfile()] || {};
      const ro = collectRiskOverrides();
      const resolvedRisk = {};
      Object.keys(ro).forEach((k) => {
        resolvedRisk[k] = (k in pv && !dirtyRisk.has(k))
          ? pv[k] : ro[k];
      });
      return {
        basic: {
          name: $("#f-name").value.trim() || "(unnamed)",
          capital_initial: capital(),
          mode: "paper",
          universe: $("#f-universe").value,
          strategy_id: $("#f-strategy").value,
          risk_profile: selectedProfile(),
          ml_enabled: $("#f-ml").checked,
          on_stop_policy:
            (form.querySelector('input[name="on_stop_policy"]:checked') || {}).value,
        },
        strategy_params: Object.assign({}, STRATEGY_DEFAULTS, collectParams()),
        risk_resolved: resolvedRisk,
      };
    }

    function refreshEffConfig() {
      if (effPanel) effPanel.textContent = JSON.stringify(resolvedEffConfig(), null, 2);
    }
    form.addEventListener("input", refreshEffConfig);
    form.addEventListener("change", refreshEffConfig);

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      if (honeypot && honeypot.value) return; // bot — silently drop
      const problems = validate();
      if (problems.length) { toast("Fix before creating: " + problems.join("; ")); return; }
      const b = resolvedEffConfig().basic;
      const p = Object.assign({}, b, {
        params: Object.assign({}, STRATEGY_DEFAULTS, collectParams()),
        risk_overrides: collectRiskOverrides(),
      });
      try {
        const created = await api("POST", "/api/sessions", p);
        window.location.href = "/sessions/" + created.id;
      } catch (err) { /* toast already shown */ }
    });

    // ---- clone prefill (?clone=<id>) ----
    const cloneId = new URLSearchParams(window.location.search).get("clone");
    if (cloneId) prefillClone(cloneId);

    async function prefillClone(sid) {
      try {
        const d = await api("GET", "/api/sessions/" + encodeURIComponent(sid));
        if ($("#f-name") && !$("#f-name").value) {
          $("#f-name").value = (d.name || "session") + " (clone)";
        }
        if ($("#f-capital")) {
          const c = (d.config || {}).capital_initial || d.capital_initial;
          if (c) $("#f-capital").value = String(Math.round(c));
        }
        if ((d.config || {}).universe) $("#f-universe").value = d.config.universe;
        if (d.config && d.config.strategy_id) $("#f-strategy").value = d.config.strategy_id;
        if (d.config && d.config.risk_profile) {
          const r = form.querySelector(
            'input[name="risk_profile"][value="' + d.config.risk_profile + '"]');
          if (r) { r.checked = true; profileTouched = true; }
        }
        if (d.config && d.config.ml_enabled != null) {
          $("#f-ml").checked = !!d.config.ml_enabled;
        }
        if (d.config && d.config.on_stop_policy) {
          const s = form.querySelector(
            'input[name="on_stop_policy"][value="' + d.config.on_stop_policy + '"]');
          if (s) s.checked = true;
        }
        const params = (d.config || {}).params || {};
        Object.entries(params).forEach(([k, v]) => {
          const input = $('[name="param:' + CSS.escape(k) + '"]', form);
          if (input) input.value = String(v);
        });
        const cfgRisk = {};
        ["max_positions", "max_total_open_risk", "max_gross_exposure",
         "daily_loss_limit", "drawdown_kill", "time_stop_days",
         "trail_mult_atr", "t1_multiple", "t2_multiple"].forEach((k) => {
          if (d.config && d.config[k] != null) cfgRisk[k] = d.config[k];
        });
        Object.assign(cfgRisk, params.risk_overrides || {});
        Object.entries(cfgRisk).forEach(([k, v]) => {
          const targetKey = k === "trail_mult_atr" ? "trail_atr_mult" : k;
          const input = $('[data-risk-param="' + targetKey + '"]', form) ||
                        $('[name="param:' + CSS.escape(targetKey) + '"]', form);
          if (input) { input.value = String(v); }
        });
        fillProfileFields();
        refreshEffConfig();
        toast("Prefilled from clone source — adjust anything before creating.");
      } catch (err) { /* source gone; form stays at defaults */
        toast("Could not load the clone source — starting from defaults.");
      }
    }

    fillProfileFields();
    refreshEffConfig();
  }

  /* ---------------- session detail ---------------- */
  let bodyRefreshTimer = null;
  function refreshBody() {
    clearTimeout(bodyRefreshTimer);
    bodyRefreshTimer = setTimeout(() => {
      const body = $("#session-body");
      if (body && window.htmx) window.htmx.trigger(body, "refresh");
    }, 350);
  }

  const detailState = { sid: null, benchmark: null };

  function initDetail() {
    const page = $("[data-page='detail']");
    if (!page) return;
    const sid = page.dataset.sessionId;
    detailState.sid = sid;

    // running-duration live ticker
    const durEl = $("[data-running-duration]");
    if (durEl && durEl.dataset.startedTs) {
      const tickDur = () => {
        const start = Date.parse(durEl.dataset.startedTs);
        if (isNaN(start)) { durEl.textContent = "—"; return; }
        const endTs = durEl.dataset.endedTs ? Date.parse(durEl.dataset.endedTs) : Date.now();
        durEl.textContent = fmtDuration(endTs - start);
      };
      tickDur();
      setInterval(tickDur, 1000);
    }

    // lifecycle controls — delegated so polled swaps keep them working
    document.addEventListener("click", async (e) => {
      const btn = e.target.closest("[data-control]");
      if (!btn) return;
      const action = btn.dataset.control;
      if (action === "stop") {
        const policy = btn.dataset.policy || "FLATTEN";
        const ok = await confirmModal({
          title: "Stop this session?",
          danger: true,
          okLabel: "Stop · " + policy,
          body: policy === "FLATTEN"
            ? "Stop policy FLATTEN is configured: working orders will be cancelled and all open positions exited at the next actionable prices before the session becomes terminal. This cannot be undone."
            : "Stop policy HOLD is configured: decisions freeze immediately and open positions remain held. The session can later be restarted from a clone.",
        });
        if (!ok) return;
      }
      btn.disabled = true;
      try {
        await api("POST", "/api/sessions/" + sid + "/" + action);
        toast("Session: " + action + " accepted.");
        refreshBody();
      } catch (err) { /* toast shown */ }
      finally { btn.disabled = false; }
    });

    window.slMountDetailCharts = mountDetailCharts;
    mountDetailCharts();

    initTimeline(sid);
    initDiagnosticScan(sid);

    // decisions → replay pane
    const replayHost = $("#replay-pane");
    document.addEventListener("click", async (e) => {
      const row = e.target.closest("[data-decision-intent]");
      if (!row || !replayHost) return;
      const intentId = row.dataset.decisionIntent;
      replayHost.hidden = false;
      replayHost.innerHTML = '<p class="dim">Loading replay…</p>';
      replayHost.scrollIntoView({ behavior: REDUCED ? "auto" : "smooth", block: "nearest" });
      let r;
      try {
        r = await api("GET", "/api/sessions/" + sid + "/decisions/" + encodeURIComponent(intentId));
      } catch (err) {
        replayHost.innerHTML = "<p>Replay unavailable.</p>";
        return;
      }
      renderReplay(replayHost, r);
    });
  }

  /* ---- detail charts: equity (+benchmark overlay), dd, trade analytics -- */
  let benchmarkFetched = false;
  function mountDetailCharts() {
    const curveSvg = $("#equity-chart");
    if (!curveSvg) return;
    const curve = JSON.parse(curveSvg.dataset.curve || "[]");
    dropChartJobs(curveSvg);
    registerChart(curveSvg, {
      data: () => {
        if (!curve.length) return null;
        const series = [{
          name: "equity", color: cssVarSafe("--indigo"),
          points: curve.map((p) => [p[0], +p[1]]),
        }];
        if (detailState.benchmark && detailState.benchmark.length) {
          series.push({
            name: "NIFTY 50 (norm.)", color: cssVarSafe("--ink-3"),
            dash: "4 4", points: detailState.benchmark,
          });
        }
        return { series };
      },
      opts: {
        xType: "time",
        yFmt: (v) => formatINR(v),
        height: 260,
        emptyMessage: "Equity curve appears after the first decision.",
      },
    });
    curveSvg.setAttribute("aria-label", "Equity curve over time");

    const ddSvg = $("#dd-chart");
    if (ddSvg) {
      const ddCurve = JSON.parse(ddSvg.dataset.dd || curveSvg.dataset.dd || "[]");
      dropChartJobs(ddSvg);
      registerChart(ddSvg, {
        data: () => (ddCurve.length ? {
          series: [{ name: "drawdown", color: cssVarSafe("--accent"), points: ddCurve }],
        } : null),
        opts: {
          xType: "time", yTicks: 3, height: 110, endLabels: false,
          yFmt: (v) => v.toFixed(1) + "%",
          emptyMessage: "No drawdown recorded.",
        },
      });
    }

    // per-trade analytics (P1-8) — real persisted arrays only, honest empty states
    const pnlSvg = $("#pnl-chart");
    if (pnlSvg) {
      dropChartJobs(pnlSvg);
      registerChart(pnlSvg, {
        data: () => {
          const vals = JSON.parse(pnlSvg.dataset.pnl || "[]");
          return vals.length ? { values: vals } : null;
        },
        opts: { height: 150, yFmt: (v) => formatINR(v), emptyMessage: "No closed trades yet." },
      }, "bar");
    }
    const rdistSvg = $("#rdist-chart");
    if (rdistSvg) {
      dropChartJobs(rdistSvg);
      registerChart(rdistSvg, {
        data: () => {
          const rs = JSON.parse(rdistSvg.dataset.r || "[]");
          if (!rs.length) return null;
          const bins = window.SLCharts.histogram(rs);
          return { values: bins.map((b) => b.count),
                   opts: {} , labels: bins };
        },
        opts: { height: 150, yFmt: (v) => String(v), annotate: false,
                emptyMessage: "No R-multiples recorded yet." },
      }, "bar");
    }
    const holdSvg = $("#holddist-chart");
    if (holdSvg) {
      dropChartJobs(holdSvg);
      registerChart(holdSvg, {
        data: () => {
          const hs = JSON.parse(holdSvg.dataset.hold || "[]");
          if (!hs.length) return null;
          const bins = window.SLCharts.histogram(hs);
          return { values: bins.map((b) => b.count) };
        },
        opts: { height: 150, yFmt: (v) => String(v), annotate: false,
                emptyMessage: "No holding-time data yet." },
      }, "bar");
    }
    const expoSvg = $("#exposure-chart");
    if (expoSvg) {
      dropChartJobs(expoSvg);
      registerChart(expoSvg, {
        data: () => {
          const ex = JSON.parse(expoSvg.dataset.exposure || "[]");
          return ex.length ? {
            series: [{ name: "exposure %", color: cssVarSafe("--gold"), points: ex }],
          } : null;
        },
        opts: {
          xType: "time", area: true, endLabels: false, yTicks: 3, padR: 16,
          yFmt: (v) => (v * 100).toFixed(1) + "%",
          emptyMessage: "Exposure history appears after snapshots accumulate.",
        },
      });
    }

    fetchBenchmarkOnce(curve);
  }

  function fetchBenchmarkOnce(curve) {
    if (benchmarkFetched || !curve.length || detailState.benchmark) return;
    benchmarkFetched = true;
    const dates = Array.from(new Set(curve.map((p) => String(p[0]).slice(0, 10))));
    const url = "/api/lab/benchmark?dates=" +
      encodeURIComponent(dates[0]) + "," + encodeURIComponent(dates[dates.length - 1]);
    api("GET", url).then((r) => {
      const note = $("#benchmark-note-detail");
      if (r && r.available && Array.isArray(r.series) && r.series.length) {
        detailState.benchmark = r.series;
        if (note) note.textContent = "· vs " + (r.source || "benchmark");
        mountDetailCharts();
      } else if (note) {
        note.textContent = "· benchmark unavailable (" +
          ((r && r.reason) || "no series") + ")";
      }
    }).catch(() => {});
  }

  /* ---- P1-7 timeline ---- */
  function initTimeline(sid) {
    const listEl = $("#timeline-list");
    const moreBtn = $("#timeline-more");
    const emptyEl = $("#timeline-empty");
    if (!listEl) return;
    let offset = 0;
    async function loadMore() {
      let r;
      try {
        r = await api("GET", "/api/sessions/" + encodeURIComponent(sid) +
          "/timeline?limit=50&offset=" + offset);
      } catch (err) {
        if (emptyEl) { emptyEl.hidden = false; emptyEl.textContent = "Timeline unavailable."; }
        return;
      }
      (r.items || []).forEach((item) => {
        const li = document.createElement("li");
        li.dataset.kind = item.kind || "event";
        li.innerHTML = '<span class="kind-chip">' + escapeHtml(item.kind || "?") + "</span>" +
          '<time data-rel="' + item.ts + '">' + relTime(item.ts) + "</time>" +
          escapeHtml(item.text || "");
        listEl.appendChild(li);
      });
      offset = r.offset || offset + (r.items || []).length;
      if (emptyEl) emptyEl.hidden = !!(r.items || []).length || listEl.children.length > 0;
      if (moreBtn) moreBtn.hidden = !r.has_more;
    }
    moreBtn.addEventListener("click", loadMore);
    loadMore();
  }

  /* ---- P0-5 diagnostic scan ---- */
  function initDiagnosticScan(sid) {
    document.addEventListener("click", async (e) => {
      const btn = e.target.closest("[data-diagnostic-scan]");
      if (!btn) return;
      const pane = $("#diagnostic-pane");
      const out = $("#diagnostic-output");
      if (!pane || !out) return;
      btn.disabled = true;
      pane.hidden = false;
      out.innerHTML = '<p class="dim">Scanning against latest persisted data…</p>';
      pane.scrollIntoView({ behavior: REDUCED ? "auto" : "smooth", block: "nearest" });
      try {
        const r = await api("POST", "/api/sessions/" + encodeURIComponent(sid) + "/scan");
        renderDiagnostic(out, r);
      } catch (err) {
        out.innerHTML = "<p>Diagnostic scan failed.</p>";
      } finally {
        btn.disabled = false;
      }
    });
  }

  function funnelTable(f) {
    if (!f) return "<p class='dim'>No scan funnel has been journaled yet.</p>";
    const keys = [["scanned", "scanned"], ["eligible", "eligible"], ["setups", "setups"],
                  ["ml_passed", "ml passed"], ["portfolio_ok", "portfolio ok"],
                  ["risk_ok", "risk ok"], ["selected", "selected"]];
    return '<table class="data"><thead><tr><th scope="col">stage</th><th scope="col">count</th></tr></thead><tbody>' +
      keys.map(([k, label]) =>
        "<tr><td>" + label + "</td><td>" + (f[k] != null ? f[k] : "—") + "</td></tr>").join("") +
      "</tbody></table>" +
      (f.ts ? '<p class="hint">funnel ts: <span class="mono">' + escapeHtml(f.ts) + "</span></p>" : "");
  }

  function renderDiagnostic(host, r) {
    let html = "";
    html += "<h3 style='font-size:15px;margin:8px 0'>Funnel</h3>" + funnelTable(r.funnel);
    if (r.deferrals && r.deferrals.length) {
      html += "<h3 style='font-size:15px;margin:12px 0 4px'>Deferrals</h3><ul>" +
        r.deferrals.map((d) =>
          "<li class='dim'><span class='chip chip-accent'>" + escapeHtml(d.reason) +
          "</span> " + escapeHtml(d.detail || "") + "</li>").join("") + "</ul>";
    }
    if (r.candidates && r.candidates.length) {
      html += "<h3 style='font-size:15px;margin:12px 0 4px'>Recent candidates</h3>" +
        '<div class="table-scroll"><table class="data"><thead><tr>' +
        "<th scope=\"col\">ts</th><th scope=\"col\">symbol</th><th scope=\"col\">decision</th>" +
        "<th scope=\"col\">score</th><th scope=\"col\">reason</th></tr></thead><tbody>" +
        r.candidates.map((c) => "<tr><td>" + escapeHtml(c.ts || "—") + "</td><td>" +
          escapeHtml(c.symbol || "—") + "</td><td>" + escapeHtml(c.decision || "—") +
          "</td><td>" + (c.score != null ? c.score : "—") + "</td><td class='dim'>" +
          escapeHtml(c.rejection_reason || "—") + "</td></tr>").join("") +
        "</tbody></table></div>";
    } else {
      html += "<p class='dim' style='margin-top:8px'>No candidates journaled — see deferrals above for why.</p>";
    }
    host.innerHTML = html;
  }

  /* ---------------- decision replay rendering ---------------- */
  function renderReplay(host, r) {
    const tick = (b) => b === true
      ? '<span class="tick" aria-label="passed">✓</span>'
      : '<span class="cross" aria-label="failed">✗</span>';
    const kvTable = (rows) =>
      "<table class=\"data\"><tbody>" + rows.map(([k, v]) =>
        "<tr><th scope=\"row\">" + k + "</th><td>" + (v == null ? "—" : v) + "</td></tr>").join("") +
      "</tbody></table>";
    let html = "";
    html += "<div class=\"replay-step\"><span class=\"label\">Decision</span>" +
      kvTable([
        ["ts", r.ts], ["symbol", r.symbol],
        ["action", '<span class="action-chip" data-a="' + (r.action || "") + '">' + (r.action || "—") + "</span>"],
        ["rejection_reason", r.rejection_reason || "—"],
      ]) + "</div>";
    html += "<div class=\"replay-step\"><span class=\"label\">Market state</span>" +
      "<p class=\"mono dim\">" + (r.market_state_ref || "—") + "</p></div>";
    const feats = Object.entries(r.features || {});
    html += "<div class=\"replay-step\"><span class=\"label\">Features</span>" +
      (feats.length
        ? feats.map(([k, v]) => '<span class="chip mono">' + k + "=" + v + "</span>").join(" ")
        : "<p class=\"dim\">—</p>") + "</div>";
    html += "<div class=\"replay-step\"><span class=\"label\">Rules</span>" +
      ((r.rules || []).length
        ? '<table class="data"><thead><tr><th scope="col">rule</th><th scope="col">observed</th><th scope="col">threshold</th><th scope="col">pass</th></tr></thead><tbody>' +
          r.rules.map((ru) => "<tr><td>" + (ru.rule_id || "") + (ru.description ? " — " + ru.description : "") +
            "</td><td>" + (ru.observed != null ? ru.observed : "—") + "</td><td>" +
            (ru.threshold != null ? ru.threshold : "—") + "</td><td>" + tick(ru.passed) + "</td></tr>").join("") +
          "</tbody></table>"
        : "<p class=\"dim\">—</p>") + "</div>";
    html += "<div class=\"replay-step\"><span class=\"label\">ML</span>" +
      (r.ml ? (r.ml.enabled
        ? "model <span class='mono'>" + (r.ml.model_id || "—") + "</span> · score " +
          (r.ml.score != null ? r.ml.score : "—") + " · prob " + (r.ml.prob != null ? r.ml.prob : "—")
        : "ML OFF")
      : "ML OFF") + "</div>";
    const pf = r.portfolio || {};
    html += "<div class=\"replay-step\"><span class=\"label\">Portfolio state</span>" +
      kvTable([["cash", formatINR(pf.cash)], ["equity", formatINR(pf.equity)],
               ["open_positions", pf.open_positions != null ? pf.open_positions : "—"],
               ["open_risk", pf.open_risk != null ? pf.open_risk : "—"]]) + "</div>";
    html += "<div class=\"replay-step\"><span class=\"label\">Risk checks</span>" +
      ((r.risk_checks || []).length
        ? '<table class="data"><thead><tr><th scope="col">check</th><th scope="col">observed</th><th scope="col">threshold</th><th scope="col">pass</th></tr></thead><tbody>' +
          r.risk_checks.map((rc) => "<tr><td>" + (rc.check || "") + "</td><td>" +
            (rc.observed != null ? rc.observed : "—") + "</td><td>" +
            (rc.threshold != null ? rc.threshold : "—") + "</td><td>" + tick(rc.passed) + "</td></tr>").join("") +
          "</tbody></table>"
        : "<p class=\"dim\">—</p>") + "</div>";
    const o = r.order;
    html += "<div class=\"replay-step\"><span class=\"label\">Order / fill</span>" +
      (o ? kvTable([["order_id", o.order_id], ["status", o.status],
                    ["filled_qty", o.filled_qty != null ? o.filled_qty : "—"],
                    ["avg_fill_px", o.avg_fill_px != null ? o.avg_fill_px : "—"]])
         : "<p class=\"dim\">No order placed.</p>") + "</div>";
    host.innerHTML = html;
  }

  /* ---------------- htmx partial polling bridge ----------------------- */
  function initHtmxBridge() {
    if (!window.htmx) return;
    document.body.addEventListener("htmx:afterSwap", (evt) => {
      hydrateFormatters(evt.target);
      document.dispatchEvent(new CustomEvent("sl:body-updated"));
    });
    document.body.addEventListener("htmx:responseError", () =>
      toast("Refresh failed — retrying in 5s."));
    document.body.addEventListener("htmx:sendError", () =>
      toast("Lab unreachable — retrying in 5s."));
  }

  /* ---------------- footer heartbeat — ALL pages, every 30s ------------ */
  function startHeartbeatPolling() {
    const elNode = $("#footer-heartbeat");
    if (!elNode) return;
    async function beat() {
      try {
        const sys = await api("GET", "/api/system/health");
        if (sys && sys.heartbeat) {
          elNode.textContent = new Date(sys.heartbeat).toLocaleTimeString();
          elNode.style.color = "";
        }
      } catch (e) {
        elNode.textContent = "offline";
        elNode.style.color = "var(--accent)";
      }
    }
    beat();
    setInterval(beat, 30000);
  }

  /* ---------------- compare page -------------------------------------- */
  function initCompare() {
    const page = $("[data-page='compare']");
    if (!page) return;
    const state = { xType: "time", ids: [], cache: null, extra: {}, hidden: new Set() };
    const preIds = (page.dataset.ids || "").split(",").filter(Boolean);

    async function buildPicker() {
      const host = $("#compare-picker");
      if (!host) return;
      let data;
      try { data = await api("GET", "/api/lab/board"); } catch (e) {
        try { data = await api("GET", "/api/lab/summary"); } catch (e2) { return; }
      }
      const sessions = (data && data.sessions) || [];
      if (!sessions.length) {
        host.innerHTML =
          '<div class="empty-state" style="grid-column:1/-1">' +
          "<p>Nothing to compare yet — create sessions first.</p></div>";
        return;
      }
      host.innerHTML = "";
      sessions.forEach((s) => {
        const label = document.createElement("label");
        label.className = "switch";
        label.style.justifyContent = "flex-start";
        label.innerHTML =
          '<input type="checkbox" name="ids" value="' + s.id + '">' +
          '<span class="track" aria-hidden="true"></span>' +
          "<span>" + escapeHtml(s.name || s.id) +
          ' <small class="mono dim">' + escapeHtml(s.status || "") + "</small></span>";
        host.appendChild(label);
      });
      let auto = [];
      if (preIds.length) {
        $$('input[name="ids"]', host).forEach((i) => {
          i.checked = preIds.includes(i.value);
          if (i.checked) auto.push(i.value);
        });
      }
      if (auto.length) loadCompare(auto);
    }

    $("[data-compare-form]").addEventListener("submit", (e) => {
      e.preventDefault();
      const checked = $$('input[name="ids"]:checked').map((i) => i.value);
      if (!checked.length) { toast("Select at least one session."); return; }
      loadCompare(checked);
    });

    $$(".axis-toggle button").forEach((btn) =>
      btn.addEventListener("click", () => {
        state.xType = btn.dataset.axis;
        $$(".axis-toggle button").forEach((b) =>
          b.setAttribute("aria-pressed", String(b === btn)));
        renderCurves();
      }));

    async function loadCompare(ids) {
      const section = $("#compare-results");
      section.hidden = false;
      $("#compare-loading").hidden = false;
      try {
        const data = await api("GET", "/api/lab/compare?ids=" + ids.map(encodeURIComponent).join(","));
        state.ids = ids;
        state.cache = data;
        state.hidden.clear();
        try {
          const extra = await api("GET", "/api/lab/compare_extra?ids=" +
            ids.map(encodeURIComponent).join(","));
          state.extra = {};
          (extra.sessions || []).forEach((x) => { state.extra[x.id] = x; });
        } catch (e2) { state.extra = {}; }  // enrichment optional
        $("#compare-loading").hidden = true;
        renderAll(data);
      } catch (err) {
        $("#compare-loading").hidden = true;
        section.hidden = true;
      }
    }

    function renderAll(data) {
      renderCurves();
      renderDrawdownSmallMultiples(data);
      renderMetrics(data);
    }

    function visibleSessions() {
      return (state.cache.sessions || []).filter((s) => !state.hidden.has(s.id));
    }

    function renderCurves() {
      const svg = $("#compare-equity-chart");
      if (!svg || !state.cache.sessions) return;
      const all = state.cache.sessions || [];
      const series = visibleSessions().map((s) => ({
        id: s.id,
        name: s.name,
        color: paletteColor(all.indexOf(s)),
        points: state.xType === "index"
          ? (s.by_trade || []).map((p) => [p[0], p[1]])
          : (s.equity_curve || []).map((p) => [p[0], p[1]]),
      }));
      window.SLCharts.lineChart(svg, series, {
        xType: state.xType,
        yFmt: (v) => formatINR(v),
        height: 320,
        emptyMessage: "No qualifying opportunities recorded yet.",
      });
      renderLegend(all);
      const note = $("#benchmark-note");
      if (note) note.hidden = !state.cache.benchmark;
    }

    // interactive legend — click toggles a series on/off (a11y buttons)
    function renderLegend(all) {
      const legend = $("#compare-legend");
      legend.innerHTML = "";
      all.forEach((s, i) => {
        const off = state.hidden.has(s.id);
        const pts = state.xType === "index" ? (s.by_trade || []) : (s.equity_curve || []);
        const lastV = pts.length ? pts[pts.length - 1][1] : null;
        const item = document.createElement("button");
        item.type = "button";
        item.className = "legend-item legend-toggle";
        item.setAttribute("aria-pressed", String(!off));
        item.title = off ? "Show series" : "Hide series";
        item.innerHTML = '<span class="swatch" style="background:' + paletteColor(i) +
          (off ? ';opacity:.25' : '') + '"></span>' +
          "<strong>" + escapeHtml(s.name) + "</strong> " +
          '<span class="mono"' + (off ? ' style="opacity:.4"' : "") + ">" + formatINR(lastV) + "</span>";
        item.addEventListener("click", () => {
          if (state.hidden.has(s.id)) state.hidden.delete(s.id);
          else if (visibleSessions().length > 1) state.hidden.add(s.id);
          renderCurves();
        });
        legend.appendChild(item);
      });
    }

    function renderDrawdownSmallMultiples(data) {
      const host = $("#dd-multiples");
      host.innerHTML = "";
      (data.sessions || []).forEach((s, i) => {
        // derive a lightweight drawdown series client-side from the equity curve
        const eq = (s.equity_curve || []).map((p) => +p[1]);
        let peak = -Infinity;
        const ddPts = (s.equity_curve || []).map((p, idx) => {
          peak = Math.max(peak, eq[idx]);
          return [p[0], peak ? ((eq[idx] - peak) / peak) * 100 : 0];
        });
        const cell = document.createElement("div");
        cell.innerHTML = '<span class="label">' + escapeHtml(s.name) + "</span>";
        const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
        svg.setAttribute("width", "100%");
        cell.appendChild(svg);
        host.appendChild(cell);
        window.SLCharts.lineChart(svg,
          [{ name: s.name, color: paletteColor(i), points: ddPts }],
          { xType: "time", yTicks: 2, height: 96, padL: 40, padR: 8,
            endLabels: false, yFmt: (v) => v.toFixed(1) + "%",
            emptyMessage: "—" });
      });
    }

    function renderMetrics(data) {
      const table = $("#metrics-table");
      if (!table) return;
      const mkeys = [
        ["return_pct", "Return"], ["cagr_pct", "CAGR"], ["sharpe", "Sharpe"],
        ["sortino", "Sortino"], ["max_dd_pct", "Max DD"], ["win_rate", "Win rate"],
        ["pf", "Profit factor"], ["expectancy", "Expectancy"], ["avg_win", "Avg win"],
        ["avg_loss", "Avg loss"], ["avg_hold_days", "Avg hold"], ["turnover", "Turnover"],
        ["exposure_pct", "Exposure"], ["cost_drag", "Cost drag"],
        ["candidates_total", "Candidates"], ["rejections_top", "Top rejections"],
      ];
      const head = table.querySelector("thead tr");
      head.innerHTML = "<th scope=\"col\">metric</th>" +
        data.sessions.map((s) => '<th scope="col">' + escapeHtml(s.name) + "</th>").join("");
      const body = table.querySelector("tbody");
      body.innerHTML = mkeys.map(([k, label]) => {
        const cells = data.sessions.map((s) => {
          const m = s.metrics || {};
          const x = state.extra[s.id] || {};
          const v = (k in m) ? m[k]
            : (k in x ? x[k] : null);   // addendum fields via routes_lab proxy
          let txt;
          if (k === "candidates_total") txt = v == null ? "—" : String(v);
          else if (k === "rejections_top") {
            txt = Array.isArray(v) && v.length
              ? v.map(([r, n]) => escapeHtml(String(r)) + "×" + n).join(", ") : "—";
          } else if (k === "sharpe" || k === "sortino" || k === "cagr_pct") {
            txt = v == null ? "—" : (+v).toFixed(2) + (k === "cagr_pct" ? "%" : "");
          } else if (v == null) txt = "—";
          else if (/pct|rate/.test(k)) txt = (+v).toFixed(2) + "%";
          else if (/expectancy|drag|win$|loss$/.test(k)) txt = formatINR(v);
          else txt = String(v);
          return "<td>" + txt + "</td>";
        }).join("");
        return "<tr><td>" + label + "</td>" + cells + "</tr>";
      }).join("");
    }

    buildPicker();
  }

  /* ---------------- polite announcements ---------------- */
  function announce(msg) {
    const region = $("#sr-status");
    if (region) region.textContent = msg;
  }

  /* ---------------- boot ---------------- */
  document.addEventListener("DOMContentLoaded", () => {
    initTheme();
    initPetals();
    initScrollFade();
    hydrateFormatters(document);
    initOverview();
    initFilters();
    initCardNavigation();
    initMenus();
    initLineup();
    initNewSession();
    initDetail();
    initCompare();
    initHtmxBridge();
    startHeartbeatPolling();

    // keep relative times honest between polls
    setInterval(() => $$("[data-rel]").forEach((n) => {
      n.textContent = relTime(n.dataset.rel);
    }), 30000);

    // detail-body chart mount when polled swap brings fresh curve data
    document.addEventListener("sl:body-updated", () => {
      if (typeof window.slMountDetailCharts === "function") window.slMountDetailCharts();
      hydrateFormatters(document);
    });
  });
})();
