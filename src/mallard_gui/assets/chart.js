// Interactive plots for analysis: every <div class="m-chart" id="STATE_KEY"> draws the trame state
// STATE_KEY = {series: [{label, x, y, faint, dash, points, band: {x, lo, hi}}], xlabel, ylabel, logx, logy,
// marks: [{x, label}], span: [x0, x1], select, unitsx, live, view, range, pick, xsnap}. The page owns the view: the
// wheel zooms the x axis
// about the cursor (shift: the y axis), a drag zooms to its box (pan mode: moves the view; window mode: picks
// the analysis window), a double click fits the data, and the crosshair reads every curve at the cursor.
// The server hears the x range once the view settles (`chart_range`), to read finer data for it, and a
// picked window (`chart_span`). Log axes are the page's own: they need no new data. A `range` names another
// trigger for the settled x range; a `pick` trigger hears the x of the sample nearest a click. A live chart
// follows its latest samples (it refits on every update) until a
// zoom or pan pauses it; fit, a double click or its follow button resumes. A new `view` token starts afresh.
(() => {
  const COLORS = ["#56b4e9", "#e69f00", "#009e73", "#f0e442", "#0072b2", "#d55e00", "#cc79a7"];
  const SETTLE = 300; // ms after the last zoom before the server reads finer data
  const M = { left: 58, right: 12, top: 10, bottom: 34 };
  const views = new Map(); // key -> {x0, x1, y0, y1, auto, mode, logx, logy}
  const timers = new Map();
  const NS = "http://www.w3.org/2000/svg";

  const state = (key) => {
    try { return window.trame.state.get(key); } catch (e) { return undefined; }
  };
  const trigger = (name, args) => window.trame && window.trame.trigger(name, args);

  function fmt(v) {
    if (!isFinite(v)) return "–";
    if (v === 0) return "0";
    const a = Math.abs(v);
    if (a >= 1e5 || a < 1e-3) return v.toExponential(2).replace("e+", "e");
    return Number(v.toPrecision(4)).toLocaleString("en-US", { maximumFractionDigits: 20 });
  }

  function niceTicks(lo, hi, n) {
    if (!(isFinite(lo) && isFinite(hi)) || hi <= lo) return [];
    const raw = (hi - lo) / Math.max(n - 1, 1);
    const mag = Math.pow(10, Math.floor(Math.log10(raw)));
    const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw);
    const out = [];
    for (let v = Math.ceil(lo / step - 1e-9) * step; v <= hi + step * 1e-9; v += step) {
      out.push(Math.abs(v) < step * 1e-9 ? 0 : v);
    }
    return out;
  }

  function decades(lo, hi) {
    const a = Math.floor(lo), b = Math.max(Math.ceil(hi), a + 1);
    const step = Math.max(1, Math.ceil((b - a) / 6));
    const out = [];
    for (let k = a; k <= b; k += step) if (k >= lo - 1e-9 && k <= hi + 1e-9) out.push(k);
    if (out.length >= 2) return out;
    // Under a decade or two in view: 2 and 5 times the decades too.
    const fine = [];
    for (let k = a - 1; k <= b; k++) {
      for (const m of [1, 2, 5]) {
        const t = k + Math.log10(m);
        if (t >= lo - 1e-9 && t <= hi + 1e-9) fine.push(t);
      }
    }
    return fine;
  }

  const pow10 = (k) => (k >= -2 && k <= 3 ? fmt(Math.pow(10, k))
    : Number.isInteger(Math.round(k * 1e6) / 1e6) ? `1e${Math.round(k)}` : Math.pow(10, k).toExponential(0).replace("e+", "e"));

  // Values on the axis's own scale: log10 on a log axis, NaN where a log axis cannot show them.
  const tx = (v, log) => (log ? (v > 0 ? Math.log10(v) : NaN) : v);
  const ux = (v, log) => (log ? Math.pow(10, v) : v);

  function extent(cfg, view) {
    let x0 = Infinity, x1 = -Infinity, y0 = Infinity, y1 = -Infinity;
    for (const s of cfg.series || []) {
      for (let i = 0; i < s.x.length; i++) {
        const x = tx(s.x[i], view.logx), y = tx(s.y[i], view.logy);
        if (!isFinite(x) || !isFinite(y)) continue;
        if (x < x0) x0 = x;
        if (x > x1) x1 = x;
      }
    }
    for (const s of cfg.series || []) {
      for (let i = 0; i < s.x.length; i++) {
        const x = tx(s.x[i], view.logx), y = tx(s.y[i], view.logy);
        if (!isFinite(x) || !isFinite(y) || x < x0 || x > x1) continue;
        if (y < y0) y0 = y;
        if (y > y1) y1 = y;
      }
      if (s.band) {
        for (const arr of [s.band.lo, s.band.hi]) {
          for (const v of arr) {
            const y = tx(v, view.logy);
            if (isFinite(y)) { y0 = Math.min(y0, y); y1 = Math.max(y1, y); }
          }
        }
      }
    }
    if (!isFinite(x0)) return null;
    if (x1 === x0) { x0 -= 1; x1 += 1; }
    if (y1 === y0) { const p = Math.abs(y0) * 0.05 || 1; y0 -= p; y1 += p; }
    const pad = (y1 - y0) * 0.04;
    return { x0, x1, y0: y0 - pad, y1: y1 + pad };
  }

  function el(tag, attrs, parent) {
    const e = document.createElementNS(NS, tag);
    for (const k in attrs) e.setAttribute(k, attrs[k]);
    if (parent) parent.appendChild(e);
    return e;
  }

  function viewOf(key, cfg) {
    let v = views.get(key);
    if (!v || v.token !== cfg.view) {
      v = { auto: true, mode: v ? v.mode : "zoom", logx: !!cfg.logx, logy: !!cfg.logy, token: cfg.view };
      views.set(key, v);
    }
    if (cfg.logx !== undefined && v.cfgLogx !== cfg.logx) { v.logx = !!cfg.logx; v.cfgLogx = cfg.logx; v.auto = true; }
    if (cfg.logy !== undefined && v.cfgLogy !== cfg.logy) { v.logy = !!cfg.logy; v.cfgLogy = cfg.logy; v.auto = true; }
    if (v.auto) {
      const e = extent(cfg, v);
      if (e) Object.assign(v, e);
    }
    return v;
  }

  function settle(key, v) {
    clearTimeout(timers.get(key));
    timers.set(key, setTimeout(() => {
      const cfg = state(key) || {};
      trigger(cfg.range || "chart_range", v.auto ? [key, null, null] : [key, ux(v.x0, v.logx), ux(v.x1, v.logx)]);
    }, SETTLE));
  }

  function toolbar(host, key, cfg, v) {
    let bar = host.querySelector(".m-chart-tools");
    if (!bar) {
      bar = document.createElement("div");
      bar.className = "m-chart-tools";
      host.appendChild(bar);
    }
    const buttons = [
      ["zoom", "zoom", "Drag a box to zoom (wheel: x, shift-wheel: y)"],
      ["pan", "pan", "Drag to move the view"],
    ];
    if (cfg.select) buttons.push(["window", "window", "Drag to pick the analysis window"]);
    const html = buttons.map(([m, label, title]) =>
      `<button class="tb${v.mode === m ? " on" : ""}" data-mode="${m}" title="${title}">${label}</button>`)
      .concat([
        `<button class="tb${v.logx ? " on" : ""}" data-log="x" title="Logarithmic x axis">log x</button>`,
        `<button class="tb${v.logy ? " on" : ""}" data-log="y" title="Logarithmic y axis">log y</button>`,
        `<button class="tb" data-fit="1" title="Fit the data (double click)">fit</button>`,
      ]).concat(cfg.live ? [
        `<button class="tb follow${v.auto ? " on" : ""}" data-fit="1" title="${v.auto
          ? "Following the latest samples: a zoom or pan pauses"
          : "Paused by a zoom: follow the latest samples again"}">${v.auto ? "follow" : "paused"}</button>`,
      ] : []).join("");
    host.dataset.follow = cfg.live ? (v.auto ? "on" : "paused") : "";
    if (bar.dataset.html !== html) {
      bar.innerHTML = html;
      bar.dataset.html = html;
      bar.querySelectorAll("button").forEach((b) => {
        b.onclick = (ev) => {
          ev.stopPropagation();
          const vv = views.get(key);
          if (b.dataset.mode) vv.mode = b.dataset.mode;
          if (b.dataset.log) { vv["log" + b.dataset.log] = !vv["log" + b.dataset.log]; vv.auto = true; }
          if (b.dataset.fit) { vv.auto = true; settle(key, vv); }
          draw(host);
        };
      });
    }
  }

  function draw(host) {
    const key = host.id;
    const cfg = state(key);
    let svg = host.querySelector("svg.m-chart-svg");
    if (!svg) {
      svg = el("svg", { class: "m-chart-svg", role: "img" });
      host.insertBefore(svg, host.firstChild);
      attach(host, svg);
    }
    while (svg.firstChild) svg.removeChild(svg.firstChild);
    const W = Math.max(host.clientWidth, 120), H = Math.max(host.clientHeight, 80);
    svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
    svg.setAttribute("width", W);
    svg.setAttribute("height", H);
    if (!cfg || !(cfg.series || []).some((s) => s.x && s.x.length)) {
      el("text", { x: W / 2, y: H / 2, "text-anchor": "middle", class: "m-plot-empty" }, svg).textContent =
        (cfg && cfg.empty) || "no data yet";
      host.__frame = null;
      return;
    }
    const v = viewOf(key, cfg);
    toolbar(host, key, cfg, v);
    svg.setAttribute("aria-label", `${cfg.ylabel || ""} over ${cfg.xlabel || ""}`);
    const legendRows = (cfg.series || []).length > 1 ? 1 : 0;
    const top = M.top + legendRows * 14 + 16;
    const pw = W - M.left - M.right, ph = H - top - M.bottom;
    const sx = (x) => M.left + ((x - v.x0) / (v.x1 - v.x0)) * pw;
    const sy = (y) => top + ph - ((y - v.y0) / (v.y1 - v.y0)) * ph;
    host.__frame = { v, sx, sy, pw, ph, top, W, H, cfg };
    const xt = v.logx ? decades(v.x0, v.x1) : niceTicks(v.x0, v.x1, Math.max(3, Math.floor(pw / 90)));
    const yt = v.logy ? decades(v.y0, v.y1) : niceTicks(v.y0, v.y1, Math.max(3, Math.floor(ph / 40)));
    for (const t of xt) {
      const X = sx(t);
      el("line", { class: "m-grid", x1: X, y1: top, x2: X, y2: top + ph }, svg);
      el("text", { class: "m-tick", x: X, y: top + ph + 13, "text-anchor": "middle" }, svg).textContent =
        v.logx ? pow10(t) : fmt(t);
    }
    for (const t of yt) {
      const Y = sy(t);
      el("line", { class: "m-grid", x1: M.left, y1: Y, x2: M.left + pw, y2: Y }, svg);
      el("text", { class: "m-tick", x: M.left - 5, y: Y + 3, "text-anchor": "end" }, svg).textContent =
        v.logy ? pow10(t) : fmt(t);
    }
    const clipId = "clip-" + key.replace(/[^A-Za-z0-9_-]/g, "");
    const defs = el("defs", {}, svg);
    el("rect", { x: M.left, y: top, width: pw, height: ph }, el("clipPath", { id: clipId }, defs));
    const g = el("g", { "clip-path": `url(#${clipId})` }, svg);
    if (cfg.span && cfg.span.length === 2 && cfg.span[0] !== null) {
      const a = sx(tx(cfg.span[0], v.logx)), b = sx(tx(cfg.span[1], v.logx));
      if (isFinite(a) && isFinite(b)) {
        el("rect", { class: "m-chart-span", x: Math.min(a, b), y: top, width: Math.abs(b - a), height: ph }, g);
      }
    }
    (cfg.series || []).forEach((s, i) => {
      const color = s.color || COLORS[i % COLORS.length];
      if (s.band) {
        const up = [], down = [];
        for (let k = 0; k < s.band.x.length; k++) {
          const X = sx(tx(s.band.x[k], v.logx)), lo = sy(tx(s.band.lo[k], v.logy)), hi = sy(tx(s.band.hi[k], v.logy));
          if (isFinite(X) && isFinite(lo) && isFinite(hi)) { up.push(`${X.toFixed(1)},${hi.toFixed(1)}`); down.unshift(`${X.toFixed(1)},${lo.toFixed(1)}`); }
        }
        if (up.length) el("polygon", { points: up.concat(down).join(" "), fill: color, "fill-opacity": 0.18, stroke: "none" }, g);
      }
      const pts = [];
      for (let k = 0; k < s.x.length; k++) {
        const X = sx(tx(s.x[k], v.logx)), Y = sy(tx(s.y[k], v.logy));
        if (isFinite(X) && isFinite(Y)) pts.push([X, Y]);
      }
      if (s.points) {
        for (const [X, Y] of pts) el("circle", { cx: X.toFixed(1), cy: Y.toFixed(1), r: 2.2, fill: "none", stroke: color, "stroke-width": 1.2 }, g);
      } else {
        el("polyline", {
          points: pts.map(([X, Y]) => `${X.toFixed(1)},${Y.toFixed(1)}`).join(" "),
          fill: "none", stroke: color, "stroke-width": s.faint ? 1 : 1.5,
          "stroke-opacity": s.faint ? 0.35 : 1, "stroke-dasharray": s.dash ? "4 3" : "none",
        }, g);
      }
    });
    for (const m of cfg.marks || []) {
      const X = sx(tx(m.x, v.logx));
      if (!isFinite(X) || X < M.left || X > M.left + pw) continue;
      el("line", { class: "m-mark", x1: X, y1: top, x2: X, y2: top + ph, "stroke-dasharray": "3 3" }, svg);
      if (m.label) {
        el("text", { class: "m-chart-mark", x: X + 4, y: top + 11 }, svg).textContent = m.label;
      }
    }
    el("rect", { class: "m-frame", x: M.left, y: top, width: pw, height: ph }, svg);
    el("path", { class: "m-axisline", d: `M${M.left} ${top}V${top + ph}H${M.left + pw}` }, svg);
    el("text", { class: "m-axis", x: M.left + pw / 2, y: H - 4, "text-anchor": "middle" }, svg).textContent =
      cfg.xlabel || "";
    el("text", { class: "m-axis", transform: `translate(12 ${top + ph / 2}) rotate(-90)`, "text-anchor": "middle" }, svg)
      .textContent = cfg.ylabel || "";
    if (legendRows) {
      let x = M.left;
      (cfg.series || []).forEach((s, i) => {
        if (s.nolegend) return;
        const color = s.color || COLORS[i % COLORS.length];
        el("line", { x1: x, y1: M.top + 4, x2: x + 12, y2: M.top + 4, stroke: color, "stroke-width": 2, "stroke-dasharray": s.dash ? "4 3" : "none" }, svg);
        const t = el("text", { class: "m-legend", x: x + 15, y: M.top + 8 }, svg);
        t.textContent = s.label;
        x += 15 + 6.2 * s.label.length + 10;
      });
    }
    host.__cross = el("g", { class: "m-chart-cross", visibility: "hidden" }, svg);
    host.__box = el("rect", { class: "m-chart-box", visibility: "hidden" }, svg);
  }

  // The nearest sample of each curve to the cursor's x.
  function nearest(xs, x) {
    let lo = 0, hi = xs.length - 1;
    if (hi < 0) return -1;
    while (hi - lo > 1) {
      const mid = (lo + hi) >> 1;
      if (xs[mid] < x) lo = mid; else hi = mid;
    }
    return Math.abs(xs[lo] - x) <= Math.abs(xs[hi] - x) ? lo : hi;
  }

  function crosshair(host, px) {
    const f = host.__frame, g = host.__cross;
    if (!f || !g) return;
    while (g.firstChild) g.removeChild(g.firstChild);
    const { v, sx, sy, top, ph, cfg } = f;
    let x = ux(v.x0 + ((px - M.left) / f.pw) * (v.x1 - v.x0), v.logx);
    if (px < M.left || px > M.left + f.pw) { g.setAttribute("visibility", "hidden"); return; }
    if (cfg.xsnap) {  // only the sampled x are possible (rank counts): the hair and the readout snap to them
      const xs = ((cfg.series || []).find((s) => s.x && s.x.length) || {}).x || [];
      const k = nearest(xs, x);
      if (k >= 0) { x = xs[k]; px = sx(tx(x, v.logx)); }
    }
    g.setAttribute("visibility", "visible");
    el("line", { x1: px, y1: top, x2: px, y2: top + ph, class: "m-chart-hair" }, g);
    const lines = [`${cfg.xname || "x"} ${fmt(x)}`];
    (cfg.series || []).forEach((s, i) => {
      if (s.faint || !s.x.length) return;
      const k = nearest(s.x, x);
      if (k < 0) return;
      const X = sx(tx(s.x[k], v.logx)), Y = sy(tx(s.y[k], v.logy));
      const color = s.color || COLORS[i % COLORS.length];
      if (isFinite(X) && isFinite(Y)) el("circle", { cx: X, cy: Y, r: 3, fill: color }, g);
      lines.push(`${s.label} ${fmt(s.y[k])}`);
    });
    const width = 12 + 6.4 * Math.max(...lines.map((l) => l.length));
    const left = px + width + 14 > f.W ? px - width - 8 : px + 8;
    el("rect", { x: left, y: top + 4, width, height: 6 + 13 * lines.length, rx: 3, class: "m-chart-readout" }, g);
    lines.forEach((l, i) => {
      el("text", { x: left + 6, y: top + 17 + 13 * i, class: "m-chart-readtext" }, g).textContent = l;
    });
    host.dataset.readout = lines.join(" | ");
  }

  // A click on a chart with `pick`: the trigger hears the nearest sample's x of its first curve.
  function pick(key, f, at) {
    const xs = ((f.cfg.series || []).find((s) => s.x && s.x.length) || {}).x || [];
    let best = -1;
    xs.forEach((x, i) => {
      if (best < 0 || Math.abs(tx(x, f.v.logx) - at) < Math.abs(tx(xs[best], f.v.logx) - at)) best = i;
    });
    if (best >= 0) trigger(f.cfg.pick, [key, xs[best]]);
  }

  function attach(host, svg) {
    const key = host.id;
    const local = (ev) => {
      const r = svg.getBoundingClientRect();
      return [ev.clientX - r.left, ev.clientY - r.top];
    };
    const dataX = (f, px) => f.v.x0 + ((px - M.left) / f.pw) * (f.v.x1 - f.v.x0);
    const dataY = (f, py) => f.v.y0 + ((f.top + f.ph - py) / f.ph) * (f.v.y1 - f.v.y0);
    let drag = null;
    svg.addEventListener("wheel", (ev) => {
      const f = host.__frame;
      if (!f) return;
      ev.preventDefault();
      const [px, py] = local(ev);
      const k = Math.exp((ev.deltaY || 0) * (ev.deltaMode === 1 ? 16 : 1) * 0.0015);
      const v = f.v;
      if (ev.shiftKey) {
        const y = dataY(f, py);
        v.y0 = y + (v.y0 - y) * k; v.y1 = y + (v.y1 - y) * k;
      } else {
        const x = dataX(f, px);
        v.x0 = x + (v.x0 - x) * k; v.x1 = x + (v.x1 - x) * k;
      }
      v.auto = false;
      draw(host);
      settle(key, v);
    }, { passive: false });
    svg.addEventListener("mousedown", (ev) => {
      const f = host.__frame;
      if (!f || ev.button !== 0) return;
      const [px, py] = local(ev);
      drag = { px, py, v: { ...f.v } };
      ev.preventDefault();
    });
    svg.addEventListener("mousemove", (ev) => {
      const f = host.__frame;
      if (!f) return;
      const [px, py] = local(ev);
      if (drag && f.v.mode === "pan") {
        const dx = ((px - drag.px) / f.pw) * (drag.v.x1 - drag.v.x0);
        const dy = ((py - drag.py) / f.ph) * (drag.v.y1 - drag.v.y0);
        Object.assign(f.v, { x0: drag.v.x0 - dx, x1: drag.v.x1 - dx, y0: drag.v.y0 + dy, y1: drag.v.y1 + dy, auto: false });
        draw(host);
        return;
      }
      if (drag && host.__box) {
        const xOnly = f.v.mode === "window" || Math.abs(py - drag.py) < 12;
        const x = Math.min(px, drag.px), w = Math.abs(px - drag.px);
        const y = xOnly ? f.top : Math.min(py, drag.py), h = xOnly ? f.ph : Math.abs(py - drag.py);
        Object.entries({ x, y, width: w, height: h, visibility: "visible" }).forEach(([k, val]) => host.__box.setAttribute(k, val));
      }
      crosshair(host, px);
    });
    const finish = (ev) => {
      const f = host.__frame;
      if (!drag || !f) { drag = null; return; }
      const [px, py] = local(ev);
      const d = drag;
      drag = null;
      if (host.__box) host.__box.setAttribute("visibility", "hidden");
      if (f.v.mode === "pan") { settle(key, f.v); return; }
      if (Math.abs(px - d.px) < 4) { if (f.cfg.pick) pick(key, f, dataX(f, px)); return; }
      const a = dataX(f, Math.min(px, d.px)), b = dataX(f, Math.max(px, d.px));
      if (f.v.mode === "window") {
        trigger("chart_span", [key, ux(a, f.v.logx), ux(b, f.v.logx)]);
        return;
      }
      f.v.x0 = a; f.v.x1 = b;
      if (Math.abs(py - d.py) >= 12) {
        const c = dataY(f, Math.max(py, d.py)), e = dataY(f, Math.min(py, d.py));
        f.v.y0 = c; f.v.y1 = e;
      } else {
        const fit = extent({ series: (f.cfg.series || []).map((s) => {
          const keep = s.x.map((x, i) => [x, s.y[i]]).filter(([x]) => { const t = tx(x, f.v.logx); return t >= a && t <= b; });
          return { x: keep.map((p) => p[0]), y: keep.map((p) => p[1]) };
        }) }, f.v);
        if (fit) { f.v.y0 = fit.y0; f.v.y1 = fit.y1; }
      }
      f.v.auto = false;
      draw(host);
      settle(key, f.v);
    };
    svg.addEventListener("mouseup", finish);
    svg.addEventListener("mouseleave", (ev) => {
      if (drag) finish(ev);
      if (host.__cross) host.__cross.setAttribute("visibility", "hidden");
    });
    svg.addEventListener("dblclick", () => {
      const v = views.get(key);
      if (!v) return;
      v.auto = true;
      draw(host);
      settle(key, v);
    });
  }

  const resize = new ResizeObserver((entries) => entries.forEach((e) => draw(e.target)));

  function scan() {
    document.querySelectorAll(".m-chart[id]").forEach((host) => {
      if (!host.__seen) {
        host.__seen = true;
        resize.observe(host);
        draw(host);
      }
    });
  }

  window.mallardChart = {
    // Called by the page when a chart's state changes (trame's ClientStateChange).
    update(key) {
      document.querySelectorAll(`.m-chart[id="${key}"]`).forEach((host) => draw(host));
    },
    view(key) {
      return views.get(key);
    },
    reset(key) {
      const v = views.get(key);
      if (v) v.auto = true;
      this.update(key);
    },
  };

  new MutationObserver(scan).observe(document.documentElement, { childList: true, subtree: true });
  document.addEventListener("DOMContentLoaded", scan);
})();
