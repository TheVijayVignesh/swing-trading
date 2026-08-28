/* ==========================================================================
 * SWING LAB — charts.js
 * Hand-rolled SVG chart library. No dependencies. Colors read from CSS vars.
 *
 * API:
 *   SLCharts.lineChart(svgEl, series, opts)
 *     series: [{name, color, points: [[x, y], ...], dash?: n, area?: bool}]
 *     opts:   { yFmt(x)->str, xType: 'time'|'index', height, pad,
 *               drawIn: bool, endLabels: bool, yTicks: n, area: bool }
 *   SLCharts.barChart(svgEl, values[], opts { height, yFmt, emptyMessage,
 *               annotate, xLabels:[[frac,text]], colorPos, colorNeg })
 *   SLCharts.histogram(values[], bins?) -> [{x0,x1,count}]
 *   SLCharts.sparkline(svgEl, points, opts { color, height, width })
 *   SLCharts.animateNumber(el, from, to, opts { fmt(x)->str, dur })
 * ========================================================================== */
(function () {
  "use strict";

  const NS = "http://www.w3.org/2000/svg";
  let tooltipEl = null;

  function cssVar(name, fallback) {
    const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return v || fallback;
  }

  function el(tag, attrs) {
    const node = document.createElementNS(NS, tag);
    for (const k in attrs) node.setAttribute(k, attrs[k]);
    return node;
  }

  function ensureTooltip() {
    if (tooltipEl) return tooltipEl;
    tooltipEl = document.createElement("div");
    tooltipEl.className = "chart-tooltip";
    tooltipEl.setAttribute("aria-hidden", "true");
    document.body.appendChild(tooltipEl);
    return tooltipEl;
  }

  function fmtTime(iso) {
    if (typeof iso !== "string") return String(iso);
    const d = new Date(iso);
    if (isNaN(d)) return iso;
    const date = d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
    const time = d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
    return date + " " + time;
  }

  function defaultYFmt(v) {
    return v == null ? "–" : Number(v).toLocaleString("en-IN", { maximumFractionDigits: 0 });
  }

  /* ---------- main line chart ---------- */
  function lineChart(svg, series, opts) {
    opts = opts || {};
    const padL = opts.padL != null ? opts.padL : 56;
    const padR = opts.padR != null ? opts.padR : 64; // room for end-label chips
    const padT = opts.padT != null ? opts.padT : 12;
    const padB = opts.padB != null ? opts.padB : 22;
    const yFmt = opts.yFmt || defaultYFmt;
    const xType = opts.xType === "index" ? "index" : "time";
    const yTicksN = opts.yTicks || 5;
    const strokeW = parseFloat(cssVar("--chart-line-w", "1.25")) || 1.25;

    svg.innerHTML = "";
    svg.classList.add("chart-frame");

    // Flatten to compute scales. Points may be [iso, val] or [n, val].
    const pts = [];
    series.forEach((s, si) =>
      (s.points || []).forEach((p) => pts.push({ x: p[0], y: +p[1], si }))
    );
    const box = { w: svg.clientWidth || 640, h: opts.height || parseInt(svg.getAttribute("height"), 10) || 240 };
    svg.setAttribute("viewBox", `0 0 ${box.w} ${box.h}`);
    svg.setAttribute("width", "100%");
    svg.setAttribute("preserveAspectRatio", "none");
    svg.style.aspectRatio = box.w + " / " + box.h;
    svg.removeAttribute("height");

    const emptyState = opts.emptyMessage || "No data yet";
    if (!pts.length || series.every((s) => !(s.points || []).length)) {
      const t = el("text", {
        x: box.w / 2, y: box.h / 2,
        "text-anchor": "middle",
        fill: cssVar("--ink-3", "#8a8377"),
        style: "font-family:var(--font-serif);font-size:13px;font-style:italic",
      });
      t.textContent = emptyState;
      svg.appendChild(t);
      return;
    }

    // X domain
    let xs;
    if (xType === "time") {
      const times = pts.map((p) => new Date(p.x).getTime()).filter((t) => !isNaN(t));
      const t0 = Math.min.apply(null, times);
      const t1 = Math.max.apply(null, times);
      const spanT = t1 - t0 || 1;
      xs = (x) => padL + ((new Date(x).getTime() - t0) / spanT) * (box.w - padL - padR);
      var xTickVals = ticksLinear(t0, t1, 4);
    } else {
      const idx = pts.map((p) => +p.x);
      const i0 = Math.min.apply(null, idx);
      const i1 = Math.max.apply(null, idx);
      const spanI = i1 - i0 || 1;
      xs = (x) => padL + ((+x - i0) / spanI) * (box.w - padL - padR);
      xTickVals = ticksLinear(i0, i1, 4);
    }

    const ys = pts.map((p) => p.y);
    let y0 = Math.min.apply(null, ys);
    let y1 = Math.max.apply(null, ys);
    if (opts.yZero && y0 > 0) y0 = 0;
    if (y1 === y0) { y1 += 1; y0 -= 1; }
    const yPad = (y1 - y0) * 0.06;
    y0 -= yPad; y1 += yPad;
    const yScale = (v) => padT + (1 - (v - y0) / (y1 - y0)) * (box.h - padT - padB);

    // grid — hairlines only
    const gGrid = el("g", {});
    const yTicks = ticksLinear(y0 + yPad, y1 - yPad, yTicksN);
    yTicks.forEach((tv) => {
      gGrid.appendChild(el("line", {
        x1: padL, x2: box.w - padR, y1: yScale(tv), y2: yScale(tv),
        stroke: cssVar("--chart-grid", "rgba(31,29,26,.08)"),
        "stroke-width": 0.75,
        "vector-effect": "non-scaling-stroke",
      }));
      const lbl = el("text", {
        x: padL - 8, y: yScale(tv) + 3,
        "text-anchor": "end",
        fill: cssVar("--ink-3", "#8a8377"),
        style: "font-family:var(--font-mono);font-size:10px",
      });
      lbl.textContent = yFmt(tv);
      gGrid.appendChild(lbl);
    });
    xTickVals.forEach((tv, i) => {
      const lbl = el("text", {
        x: xs(tv), y: box.h - 6,
        "text-anchor": i === 0 ? "start" : "middle",
        fill: cssVar("--ink-3", "#8a8377"),
        style: "font-family:var(--font-mono);font-size:10px",
      });
      lbl.textContent = xType === "time" ? fmtTime(tv).split(" ")[0] : Math.round(tv);
      gGrid.appendChild(lbl);
    });
    svg.appendChild(gGrid);

    // series lines
    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    series.forEach((s) => {
      const P = (s.points || []).map((p) => [xs(p[0]), yScale(+p[1])]);
      if (!P.length) return;

      // optional area fill under the line (exposure-through-time etc.)
      if (opts.area && s.area !== false) {
        const baseY = yScale(Math.max(y0 + yPad, 0));
        const dArea = P.map((p, i) => (i ? "L" : "M") + p[0].toFixed(2) + " " + p[1].toFixed(2)).join("") +
          "L" + P[P.length - 1][0].toFixed(2) + " " + baseY.toFixed(2) +
          "L" + P[0][0].toFixed(2) + " " + baseY.toFixed(2) + "Z";
        svg.appendChild(el("path", {
          d: dArea,
          fill: s.color || cssVar("--indigo", "#3d4e6b"),
          opacity: 0.14,
          stroke: "none",
        }));
      }

      const d = P.map((p, i) => (i ? "L" : "M") + p[0].toFixed(2) + " " + p[1].toFixed(2)).join("");
      const path = el("path", {
        d,
        fill: "none",
        stroke: s.color || cssVar("--indigo", "#3d4e6b"),
        "stroke-width": strokeW,
        "stroke-linejoin": "round",
        "stroke-linecap": "round",
        "vector-effect": "non-scaling-stroke",
      });
      if (s.dash) path.setAttribute("stroke-dasharray", String(s.dash));
      svg.appendChild(path);
      if (opts.drawIn !== false && !reduced) {
        const len = path.getTotalLength();
        path.style.strokeDasharray = len;
        path.style.strokeDashoffset = len;
        path.getBoundingClientRect(); // flush
        path.style.transition = "stroke-dashoffset 900ms ease-out";
        requestAnimationFrame(() => { path.style.strokeDashoffset = "0"; });
      }

      // end-value label chip
      if (opts.endLabels !== false) {
        const lastP = P[P.length - 1];
        const lastV = s.points[s.points.length - 1][1];
        const g = el("g", {});
        const text = yFmt(lastV);
        const wApprox = text.length * 6.4 + 10;
        const rectX = Math.min(box.w - padR + 6, box.w - wApprox - 2);
        g.appendChild(el("rect", {
          x: rectX, y: lastP[1] - 9,
          width: wApprox, height: 17,
          rx: 2,
          fill: s.color || cssVar("--indigo", "#3d4e6b"),
          opacity: 0.92,
        }));
        const t = el("text", {
          x: rectX + wApprox / 2, y: lastP[1] + 3.5,
          "text-anchor": "middle",
          fill: cssVar("--paper", "#f6f1e7"),
          style: "font-family:var(--font-mono);font-size:10px",
        });
        t.textContent = text;
        t.classList.add("chart-endlabel");
        g.appendChild(t);
        svg.appendChild(g);
      }
    });

    // nearest-point hover → shared tooltip
    const overlay = el("rect", {
      x: 0, y: 0, width: box.w, height: box.h, fill: "transparent",
    });
    svg.appendChild(overlay);
    overlay.addEventListener("mousemove", (ev) => {
      const rect = svg.getBoundingClientRect();
      const px = ((ev.clientX - rect.left) / rect.width) * box.w;
      let best = null;
      series.forEach((s) => {
        (s.points || []).forEach((p) => {
          const sx = xs(p[0]);
          const dx = Math.abs(sx - px);
          if (!best || dx < best.dx) best = { dx, s, p };
        });
      });
      if (!best) return;
      const tip = ensureTooltip();
      tip.textContent =
        best.s.name + " · " +
        (xType === "time" ? fmtTime(best.p[0]) : "#" + best.p[0]) +
        "\n" + yFmt(+best.p[1]);
      tip.style.opacity = "1";
      tip.style.left = ev.clientX + 14 + "px";
      tip.style.top = ev.clientY - 14 + "px";
    });
    overlay.addEventListener("mouseleave", () => {
      if (tooltipEl) tooltipEl.style.opacity = "0";
    });
  }

  /* ---------- sparkline mini ---------- */
  function sparkline(svg, points, opts) {
    opts = opts || {};
    const h = opts.height || 44;
    const w = opts.width || (svg.clientWidth || 220);
    svg.innerHTML = "";
    svg.setAttribute("viewBox", `0 0 ${w} ${h}`);
    svg.setAttribute("preserveAspectRatio", "none");
    svg.style.width = "100%";
    svg.style.height = h + "px";
    if (!points || points.length < 2) return;
    const vals = points.map(Number);
    const lo = Math.min.apply(null, vals);
    const hi = Math.max.apply(null, vals);
    const span = hi - lo || 1;
    const up = vals[vals.length - 1] >= vals[0];
    const color = opts.color ||
      (up ? cssVar("--pos", "#3e6b4f") : cssVar("--accent", "#c73e2e"));
    const step = w / (vals.length - 1);
    const d = vals.map((v, i) => {
      const x = i * step;
      const y = 3 + (1 - (v - lo) / span) * (h - 6);
      return (i ? "L" : "M") + x.toFixed(1) + " " + y.toFixed(1);
    }).join("");
    svg.appendChild(el("path", {
      d, fill: "none", stroke: color, "stroke-width": 1.25,
      "stroke-linecap": "round", "vector-effect": "non-scaling-stroke",
    }));
  }

  /* ---------- smooth number tick ---------- */
  function animateNumber(node, from, to, opts) {
    opts = opts || {};
    const fmt = opts.fmt || defaultYFmt;
    const dur = opts.dur || 420;
    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (reduced || isNaN(from) || isNaN(to)) {
      node.textContent = fmt(to);
      return;
    }
    const start = performance.now();
    function frame(now) {
      const t = Math.min(1, (now - start) / dur);
      const eased = 1 - Math.pow(1 - t, 3); // ease-out cubic
      node.textContent = fmt(from + (to - from) * eased);
      if (t < 1) requestAnimationFrame(frame);
    }
    requestAnimationFrame(frame);
  }

  /* ---------- diverging bar chart (± P&L per trade, histograms) ---------- */
  function barChart(svg, values, opts) {
    opts = opts || {};
    const h = opts.height || parseInt(svg.getAttribute("height"), 10) || 150;
    const w = svg.clientWidth || 640;
    svg.innerHTML = "";
    svg.classList.add("chart-frame");
    svg.setAttribute("viewBox", `0 0 ${w} ${h}`);
    svg.setAttribute("width", "100%");
    svg.setAttribute("preserveAspectRatio", "none");
    svg.removeAttribute("height");
    const vals = (values || []).map(Number).filter((v) => !isNaN(v));
    const yFmt = opts.yFmt || defaultYFmt;
    if (!vals.length) {
      const t = el("text", {
        x: w / 2, y: h / 2, "text-anchor": "middle",
        fill: cssVar("--ink-3", "#8a8377"),
        style: "font-family:var(--font-serif);font-size:13px;font-style:italic",
      });
      t.textContent = opts.emptyMessage || "No closed trades yet.";
      svg.appendChild(t);
      return;
    }
    const padT = 8, padB = opts.baselineLabel === false ? 8 : 18;
    const lo = Math.min(0, Math.min.apply(null, vals));
    const hi = Math.max(0, Math.max.apply(null, vals));
    let span = hi - lo;
    if (span <= 0) { span = Math.abs(hi) || 1; }
    const yOf = (v) => padT + (1 - (v - lo) / span) * (h - padT - padB);
    const posColor = opts.colorPos || cssVar("--pos", "#3e6b4f");
    const negColor = opts.colorNeg || cssVar("--accent", "#c73e2e");

    // zero hairline
    if (lo < 0 && hi > 0) {
      svg.appendChild(el("line", {
        x1: 0, x2: w, y1: yOf(0), y2: yOf(0),
        stroke: cssVar("--chart-grid", "rgba(31,29,26,.08)"), "stroke-width": 1,
        "vector-effect": "non-scaling-stroke",
      }));
    }

    const n = vals.length;
    // bar band width — capped so histograms read as bars, not a solid mass
    const gap = n > 120 ? 0 : 1.5;
    const bw = Math.max(1, (w - 4) / n - gap);
    vals.forEach((v, i) => {
      const yTop = v >= 0 ? yOf(v) : yOf(0);
      const bh = Math.max(1, Math.abs(yOf(v) - yOf(0)));
      svg.appendChild(el("rect", {
        x: (2 + i * ((w - 4) / n)).toFixed(2),
        y: yTop.toFixed(2),
        width: bw.toFixed(2),
        height: bh.toFixed(2),
        fill: v >= 0 ? posColor : negColor,
        opacity: 0.85,
      }));
    });

    // min/max annotations
    const mkLbl = (v, anchorX, anchor) => {
      const t = el("text", {
        x: anchorX, y: Math.max(10, yOf(v) - 3), "text-anchor": anchor,
        fill: cssVar("--ink-3", "#8a8377"),
        style: "font-family:var(--font-mono);font-size:10px",
      });
      t.textContent = yFmt(v);
      svg.appendChild(t);
    };
    if (opts.annotate !== false) {
      mkLbl(Math.max.apply(null, vals), w - 4, "end");
      mkLbl(Math.min.apply(null, vals), w - 4, "end");
    }
    if (opts.xLabels && opts.xLabels.length) {
      opts.xLabels.forEach(([frac, text]) => {
        const t = el("text", {
          x: frac * w, y: h - 5, "text-anchor": frac === 0 ? "start" : (frac === 1 ? "end" : "middle"),
          fill: cssVar("--ink-3", "#8a8377"),
          style: "font-family:var(--font-mono);font-size:10px",
        });
        t.textContent = text;
        svg.appendChild(t);
      });
    }
  }

  /* ---------- histogram binning for distributions ---------- */
  function histogram(values, bins) {
    const vs = (values || []).map(Number).filter((v) => !isNaN(v));
    if (!vs.length) return [];
    bins = bins || Math.min(14, Math.max(5, Math.ceil(Math.sqrt(vs.length))));
    const lo = Math.min.apply(null, vs);
    const hi = Math.max.apply(null, vs);
    const span = hi - lo || 1;
    const counts = new Array(bins).fill(0);
    vs.forEach((v) => {
      const i = Math.min(bins - 1, Math.floor(((v - lo) / span) * bins));
      counts[i] += 1;
    });
    return counts.map((c, i) => ({
      x0: lo + (span * i) / bins,
      x1: lo + (span * (i + 1)) / bins,
      count: c,
    }));
  }

  function ticksLinear(a, b, n) {
    const out = [];
    for (let i = 0; i <= n; i++) out.push(a + ((b - a) * i) / n);
    return out;
  }

  window.SLCharts = { lineChart, sparkline, animateNumber, barChart, histogram, fmtTime };
})();
