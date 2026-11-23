// Splitters between panes (ui/panes.py): a `.splitter` resizes its pane (its parent, or with data-pane="prev"
// the element before it) along data-axis from data-edge, within data-min..data-max, leaving data-keep px to the
// flexible neighbor that gives up the space. Drag (one update per frame), arrow keys (Shift: larger steps),
// Home/End, double-click for the default size, Enter or the toggle to collapse. A size is the variable
// `--<data-split>` on `.body`; on release it goes to the state `pane_layout`, which the server saves.
(() => {
  const STEP = 10, BIG = 50, STRIP = 12, SLOP = 3, SAVE_AFTER = 300;
  const get = (key) => { try { return window.trame.state.get(key); } catch (e) { return undefined; } };
  // What was last committed, until the state surely holds it: two quick toggles see the first.
  let recent = null, recentTimer = 0;
  const layout = () => ({ ...(recent || get("pane_layout") || {}) });
  const commit = (next) => {
    recent = next;
    clearTimeout(recentTimer);
    recentTimer = setTimeout(() => { recent = null; }, 1000);
    try { window.trame.state.set("pane_layout", next); } catch (e) { /* no server */ }
  };

  const splitterOf = (el) => (el && el.closest ? el.closest(".splitter") : null);
  const across = (s) => s.dataset.axis !== "y";
  const sign = (s) => (s.dataset.edge === "start" ? -1 : 1);
  const paneOf = (s) => (s.dataset.pane === "prev" ? s.previousElementSibling : s.parentElement);
  const isCollapsed = (s) => s.hasAttribute("data-collapsed");
  const sizeOf = (el, s) => { const r = el.getBoundingClientRect(); return across(s) ? r.width : r.height; };
  const host = (s) => s.closest(".body") || document.documentElement;

  // The pane that gives up the space: the nearest shown sibling on the splitter's side that grows.
  function neighbor(s) {
    const step = s.dataset.edge === "start" ? "previousElementSibling" : "nextElementSibling";
    for (let el = paneOf(s)[step]; el; el = el[step]) {
      if (el.classList.contains("splitter") || !el.getClientRects().length) continue;
      if (el.classList.contains("center") || parseFloat(getComputedStyle(el).flexGrow) > 0) return el;
    }
    return null;
  }

  function limits(s) {
    const pane = paneOf(s);
    const min = +s.dataset.min || 80, keep = +s.dataset.keep || 0;
    let max = +s.dataset.max || 4000;
    const n = neighbor(s);
    const room = n ? sizeOf(pane, s) + sizeOf(n, s) : sizeOf(pane.parentElement, s);
    max = Math.min(max, room - keep);
    // A pane's own percentage cap (it keeps a short window usable) bounds the drag too.
    const cap = getComputedStyle(pane)[across(s) ? "maxWidth" : "maxHeight"];
    if (s.dataset.pane !== "prev" && cap.endsWith("%")) max = Math.min(max, (parseFloat(cap) / 100) * sizeOf(pane.parentElement, s));
    return { min, max: Math.max(min, Math.floor(max)) };
  }

  const clamp = (v, lim) => Math.round(Math.min(lim.max, Math.max(lim.min, v)));

  function aria(s, size) {
    const lim = limits(s);
    s.setAttribute("aria-valuemin", String(lim.min));
    s.setAttribute("aria-valuemax", String(lim.max));
    s.setAttribute("aria-valuenow", String(Math.round(size ?? sizeOf(paneOf(s), s))));
  }

  function show(s, px) {
    host(s).style.setProperty("--" + s.dataset.split, px + "px");
    aria(s, px);
  }

  function save(s, px) {
    const next = layout();
    next[s.dataset.split] = px;
    delete next[s.dataset.split + "-collapsed"];
    commit(next);
  }

  function toggle(s) {
    if (!s.hasAttribute("data-collapsed") && !s.querySelector(".splitter-toggle")) return;
    const next = layout(), key = s.dataset.split + "-collapsed";
    if (next[key]) delete next[key];
    else next[key] = true;
    commit(next);
  }

  function reset(s) {
    const next = layout();
    delete next[s.dataset.split];
    delete next[s.dataset.split + "-collapsed"];
    host(s).style.removeProperty("--" + s.dataset.split);
    commit(next);
    requestAnimationFrame(() => aria(s));
  }

  // Dragging
  let drag = null;
  function frame() {
    drag.raf = 0;
    if (drag.px !== null) show(drag.s, drag.px);
  }

  document.addEventListener("pointerdown", (e) => {
    const s = splitterOf(e.target);
    if (!s || e.button !== 0 || e.target.closest(".splitter-toggle")) return;
    e.preventDefault();
    s.focus({ preventScroll: true });
    s.setPointerCapture(e.pointerId);
    drag = {
      s, id: e.pointerId, at: across(s) ? e.clientX : e.clientY,
      size: isCollapsed(s) ? STRIP : sizeOf(paneOf(s), s), lim: limits(s), moved: false, px: null, raf: 0,
    };
  });

  document.addEventListener("pointermove", (e) => {
    if (!drag || e.pointerId !== drag.id) return;
    const delta = ((across(drag.s) ? e.clientX : e.clientY) - drag.at) * sign(drag.s);
    if (!drag.moved) {
      if (Math.abs(delta) < SLOP) return;
      drag.moved = true;
      drag.s.classList.add("dragging");
      document.documentElement.classList.add("pane-drag", across(drag.s) ? "pane-drag-x" : "pane-drag-y");
    }
    drag.px = clamp(drag.size + delta, drag.lim);
    if (!drag.raf) drag.raf = requestAnimationFrame(frame);
  });

  function end(e) {
    if (!drag || e.pointerId !== drag.id) return;
    const d = drag;
    drag = null;
    if (d.raf) cancelAnimationFrame(d.raf);
    d.s.classList.remove("dragging");
    document.documentElement.classList.remove("pane-drag", "pane-drag-x", "pane-drag-y");
    if (d.moved && d.px !== null && e.type === "pointerup") {
      show(d.s, d.px);
      save(d.s, d.px);
    } else if (!d.moved && isCollapsed(d.s) && e.type === "pointerup") {
      toggle(d.s); // a click on the strip brings the pane back
    } else if (d.moved) { // cancelled: back to what is stored
      const stored = layout()[d.s.dataset.split];
      if (isCollapsed(d.s)) show(d.s, STRIP);
      else if (stored) show(d.s, stored);
      else host(d.s).style.removeProperty("--" + d.s.dataset.split);
    }
  }
  document.addEventListener("pointerup", end);
  document.addEventListener("pointercancel", end);

  document.addEventListener("dblclick", (e) => {
    const s = splitterOf(e.target);
    if (s && !e.target.closest(".splitter-toggle")) reset(s);
  });

  document.addEventListener("click", (e) => {
    const t = e.target.closest && e.target.closest(".splitter-toggle");
    if (t) toggle(splitterOf(t));
  });

  // Keyboard
  const timers = new Map();
  document.addEventListener("keydown", (e) => {
    const s = e.target;
    if (!s.classList || !s.classList.contains("splitter")) return;
    const keys = across(s) ? { ArrowLeft: -1, ArrowRight: 1 } : { ArrowUp: -1, ArrowDown: 1 };
    const lim = limits(s);
    let px;
    if (e.key in keys) {
      if (isCollapsed(s)) return;
      px = clamp(sizeOf(paneOf(s), s) + keys[e.key] * sign(s) * (e.shiftKey ? BIG : STEP), lim);
    } else if (e.key === "Home" || e.key === "End") {
      if (isCollapsed(s)) return;
      px = e.key === "Home" ? lim.min : lim.max;
    } else if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      toggle(s);
      return;
    } else {
      return;
    }
    e.preventDefault();
    show(s, px);
    clearTimeout(timers.get(s));
    timers.set(s, setTimeout(() => save(s, px), SAVE_AFTER));
  });

  document.addEventListener("focusin", (e) => { if (e.target.classList && e.target.classList.contains("splitter")) aria(e.target); });

  window.mallardPanes = { limits, neighbor };
})();
