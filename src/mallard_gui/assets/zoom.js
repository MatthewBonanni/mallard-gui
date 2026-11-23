// Smooth zoom for every view: the wheel's distance in pixels (lines and pages converted), not its event count,
// sets the factor continuously, so a trackpad's many small deltas zoom as finely as they arrive and a mouse
// notch is a small step. Pinches (ctrl+wheel in Chrome, gesture events in Safari) zoom the same way. Where the
// browser draws (vtk.js), the camera moves here toward the point under the cursor, once per animation frame
// however many events arrive in it, and the server hears the result once the wheel stops. Where the server
// renders images, one accumulated factor goes per frame, and the image itself scales (and, for a pan, moves)
// at once, until a frame that shows the move arrives: the results view's frames carry the sequence number of
// the last move they include (v_frame_seq), so what is still on its way stays previewed.
(() => {
  const LINE = 16; // px per line (deltaMode 1)
  const PAGE = 400; // px per page (deltaMode 2)
  const RATE = 0.0008; // zoom e-folds per wheel pixel: a 100 px mouse notch is 8 %
  const PINCH_RATE = 0.01; // per ctrl+wheel pixel: pinch deltas are small
  const MAX_PX = 200; // one event's most, so no burst jumps
  const SYNC_AFTER = 250; // ms of quiet before the server takes the browser's camera

  function pixels(ev) {
    const k = ev.deltaMode === 1 ? LINE : ev.deltaMode === 2 ? PAGE : 1;
    return Math.max(-MAX_PX, Math.min(MAX_PX, (ev.deltaY || 0) * k));
  }

  // > 1 zooms in (wheel up / pinch out).
  function factor(ev) {
    return Math.exp(-pixels(ev) * (ev.ctrlKey ? PINCH_RATE : RATE));
  }

  const toward = (a, p, k) => [a[0] + (p[0] - a[0]) * k, a[1] + (p[1] - a[1]) * k, a[2] + (p[2] - a[2]) * k];

  // The camera scaled by 1/f about the focal-plane point under (sx, sy): that point stays under the cursor.
  function dolly(cam, f, sx, sy, w, h) {
    const P = window.mallardPins;
    const at = P ? P.unproject(P.frame(cam, w, h), sx, sy, cam.focalPoint) : cam.focalPoint;
    const k = 1 - 1 / f;
    const out = { ...cam, position: toward(cam.position, at, k), focalPoint: toward(cam.focalPoint, at, k) };
    if (cam.parallelProjection) out.parallelScale = cam.parallelScale / f;
    return out;
  }

  const views = [
    { ref: "caseview", sync: "case_camera", remote: "case_zoom", local: () => state("caseview_mode") !== "remote" },
    { ref: "geoview", sync: "geo_camera", local: () => !!state("v_geometry") },
  ];

  function state(key) {
    try { return window.trame.state.get(key); } catch (e) { return undefined; }
  }

  // What trame-vtk keeps of a view the browser draws (its vtk.js renderer, render window and interactor), or
  // null while the view is hidden or not built. The component's own getCamera asks the interactor for the
  // renderer under the pointer, and there is none once the pointer has left the view.
  function internals(ref) {
    const r = window.trame && window.trame.refs && window.trame.refs[ref];
    if (!r || !r.$el || !r.$el.offsetParent) return null;
    const local = (r.$refs && r.$refs.localViewRef) || r;
    const v = local.$ && local.$.provides && local.$.provides.view;
    return v && v.renderer ? v : null;
  }

  // A scene update is applied whole before the next begins. trame-vtk starts each as it arrives and waits for
  // its arrays, so over a link where they take longer than the updates are apart (a dragged region's live
  // highlight sends one every few tens of ms) two were under way at once, and the scene was drawn with the
  // points of one and the cells of the other. Updates queue here in the order sent; once none is left the
  // server is told, which sends the next only then (the latest scene, skipping those in between).
  function inOrder(ref, done) {
    const r = window.trame && window.trame.refs && window.trame.refs[ref];
    const local = r && ((r.$refs && r.$refs.localViewRef) || r);
    const view = local && local.$ && local.$.provides && local.$.provides.view;
    if (!view || view.mallardQueue || typeof view.updateViewState !== "function") return;
    const apply = view.updateViewState.bind(view);
    let last = Promise.resolve(), waiting = 0;
    view.mallardQueue = true;
    view.updateViewState = (state) => {
      waiting += 1;
      last = last.then(() => apply(state)).catch((e) => {
        console.error("a scene update of", ref, "failed:", e); // to page.log (diagnostics.js); the next goes on
      }).then(() => {
        waiting -= 1;
        if (!waiting && done) window.trame.trigger(done);
      });
      return last;
    };
  }
  // The case view's section plane, where the browser draws: vtk.js clips each actor the server names as it
  // draws it (a clipping plane on its mapper), so orbiting a clipped case and moving the plane cost no round
  // trip and no geometry. vtk.js asks a plane only for its origin, its normal and when it changed.
  const cut = { origin: [0, 0, 0], normal: [0, 0, 1], time: 1 };
  const plane = {
    isA: (name) => name === "vtkPlane" || name === "vtkObject",
    getOriginByReference: () => cut.origin, getOrigin: () => cut.origin.slice(),
    getNormalByReference: () => cut.normal, getNormal: () => cut.normal.slice(),
    getMTime: () => cut.time,
  };
  let clippedBy = new Set(); // the mappers that have the plane
  function section() {
    const view = internals("caseview");
    if (!view) return;
    const want = state("case_section") || {};
    const on = !!want.on && state("caseview_mode") !== "remote";
    let changed = false;
    if (on && (String(want.origin) !== String(cut.origin) || String(want.normal) !== String(cut.normal))) {
      cut.origin = want.origin.slice();
      cut.normal = want.normal.slice();
      cut.time += 1;
      changed = true;
    }
    const mappers = new Set();
    if (on) {
      for (const id of want.actors || []) {
        const actor = view.ctx && view.ctx.getInstance && view.ctx.getInstance(id);
        const mapper = actor && actor.getMapper && actor.getMapper();
        if (mapper && mapper.addClippingPlane) mappers.add(mapper);
      }
    }
    for (const mapper of mappers) if (!clippedBy.has(mapper)) { mapper.addClippingPlane(plane); changed = true; }
    for (const mapper of clippedBy) {
      if (!mappers.has(mapper)) { try { mapper.removeClippingPlane(plane); } catch (e) { /* gone */ } changed = true; }
    }
    clippedBy = mappers;
    if (changed) view.render();
  }
  (function guard() {
    requestAnimationFrame(guard);
    try { inOrder("caseview", "case_scene_applied"); inOrder("geoview", null); } catch (e) { /* not built yet */ }
    try { section(); } catch (e) { /* a scene between two updates */ }
  })();

  // The camera of a view the browser draws now, for a command that starts from it (Fit, a preset): the server
  // hears of a drag only a moment after it ends. Null where the server draws, or before the view's first scene.
  function camera(ref) {
    const v = views.find((x) => x.ref === ref);
    const view = v && v.local() ? internals(ref) : null;
    if (!view || !view.renderer.getActors().length) return null;
    return view.renderer.getActiveCamera().get("position", "focalPoint", "viewUp", "parallelProjection", "parallelScale", "viewAngle");
  }

  function viewAt(target) {
    const refs = (window.trame && window.trame.refs) || {};
    for (const v of views) {
      const r = refs[v.ref];
      if (r && r.$el && r.$el.contains && r.$el.contains(target)) return { ...v, r };
    }
    const img = target.closest && target.closest(".results-view img.frame");
    // The image's parent: the image itself may be scaled or moved by a preview.
    return img ? { ref: "results", remote: "v_wheel", el: img.parentElement, local: () => false } : null;
  }

  const pending = new Map(); // trigger -> {f, nx, ny}
  const timers = new Map();

  function flush() {
    for (const [name, z] of pending) {
      window.trame.trigger(name, name === "v_wheel" ? [z.f, z.nx, z.ny, preview.mark()] : [z.f, z.nx, z.ny]);
    }
    pending.clear();
  }

  // Local zooms wait for the next animation frame: one camera change and one render per displayed frame.
  const localZoom = new Map(); // ref -> {v, f, sx, sy, w, h}
  function flushLocal() {
    for (const z of localZoom.values()) {
      let cam = null;
      try { cam = z.v.r.getCamera(); } catch (e) { continue; }
      if (!cam || !cam.position) continue;
      z.v.r.setCamera(dolly(cam, z.f, z.sx, z.sy, z.w, z.h));
      clearTimeout(timers.get(z.v.ref));
      timers.set(z.v.ref, setTimeout(() => window.trame.trigger(z.v.sync, [z.v.r.getCamera()]), SYNC_AFTER));
    }
    localZoom.clear();
  }

  // The results view's image previews the moves the server has not drawn yet. Screen maps x -> a x + b (CSS px
  // in the image's box) compose; total is every move so far, sent[seq] what it was when move seq went out, and
  // a frame drawn through move k shows total as of k, so the image is drawn under total ∘ sent[k]⁻¹.
  // A move is named by this page and its count: the server outlives a page, and a frame it drew through another
  // page's moves (a reload's, another window's) shows none of this one's.
  const preview = (() => {
    const page = Math.random().toString(36).slice(2, 10);
    const name = (n) => `${page}:${n}`;
    const count = (k) => { const [p, n] = String(k || "").split(":"); return p === page ? Number(n) || 0 : 0; };
    let seq = 0;
    let total = { a: 1, bx: 0, by: 0 };
    const sent = new Map([[0, total]]);
    let shown = 0;
    const img = () => document.querySelector(".results-view img.frame");
    const live = () => !state("v_batch");
    function apply() {
      const el = img();
      if (!el) return;
      const k = sent.get(shown) || total;
      const a = total.a / k.a;
      const bx = total.bx - a * k.bx, by = total.by - a * k.by;
      const still = Math.abs(a - 1) < 1e-6 && Math.abs(bx) < 0.01 && Math.abs(by) < 0.01;
      el.style.transformOrigin = "0 0";
      el.style.transform = still ? "" : `translate(${bx}px, ${by}px) scale(${a})`;
    }
    return {
      // The move about to be sent: its sequence number.
      mark() {
        seq += 1;
        sent.set(seq, total);
        return name(seq);
      },
      zoom(f, x, y) {
        if (!live()) return;
        total = { a: f * total.a, bx: f * total.bx + x * (1 - f), by: f * total.by + y * (1 - f) };
        apply();
      },
      pan(dx, dy) {
        if (!live()) return;
        total = { a: total.a, bx: total.bx + dx, by: total.by + dy };
        apply();
      },
      // A frame arrived drawn through move k.
      drawn(k) {
        shown = Math.max(shown, count(k));
        for (const s of [...sent.keys()]) if (s < shown) sent.delete(s);
        if (!sent.has(shown)) sent.set(shown, total);
        apply();
      },
      state: () => ({ seq: name(seq), shown, total }),
    };
  })();
  document.addEventListener("load", (ev) => {
    if (ev.target.matches && ev.target.matches(".results-view img.frame")) preview.drawn(state("v_frame_seq"));
  }, true);

  function zoom(target, f, clientX, clientY) {
    const v = viewAt(target);
    if (!v) return false;
    const el = v.el || v.r.$el;
    const box = el.getBoundingClientRect();
    const sx = clientX - box.left, sy = clientY - box.top;
    if (v.local() && target.tagName === "CANVAS") {
      let cam = null;
      try { cam = v.r.getCamera(); } catch (e) { return false; } // before the pointer first enters it
      if (!cam || !cam.position) return false;
      const z = localZoom.get(v.ref) || { v, f: 1 };
      if (!localZoom.size) requestAnimationFrame(flushLocal);
      localZoom.set(v.ref, { v, f: z.f * f, sx, sy, w: box.width, h: box.height });
      return true;
    }
    if (!v.remote) return false;
    const z = pending.get(v.remote) || { f: 1 };
    if (!pending.size) requestAnimationFrame(flush);
    if (v.ref === "results") preview.zoom(f, sx, sy);
    pending.set(v.remote, { f: z.f * f, nx: sx / box.width, ny: sy / box.height });
    return true;
  }

  // Drags over the results image: one move per animation frame, a pan previewed by moving the image.
  const DRAG_THRESHOLD = 3; // css px, as results_page.py's: a smaller press and release is a click
  let drag = null;
  function sendDrag() {
    if (!drag) return;
    drag.queued = false;
    if (drag.x === drag.sentX && drag.y === drag.sentY) return;
    if (drag.pan) preview.pan(drag.x - drag.sentX, drag.y - drag.sentY);
    drag.sentX = drag.x;
    drag.sentY = drag.y;
    window.trame.trigger("v_move", [drag.x, drag.y, preview.mark()]);
  }
  window.addEventListener("mousedown", (ev) => {
    if (!(ev.target.matches && ev.target.matches(".results-view img.frame"))) return;
    const pan = ev.shiftKey || ev.button === 1 || !!state("v_lock2d");
    const [x, y] = at(ev);
    drag = { x, y, sentX: x, sentY: y, x0: x, y0: y, pan, moved: false, queued: false };
  }, { capture: true });
  // Where a mouse event is in the results view, in CSS px from its top left, whatever the preview does.
  function at(ev) {
    const box = document.querySelector(".results-view").getBoundingClientRect();
    return [ev.clientX - box.left, ev.clientY - box.top];
  }
  window.addEventListener("mousemove", (ev) => {
    if (!drag) return;
    if (!ev.buttons || ev.target !== img()) { drag = null; return; } // left the image: the page sent v_up
    [drag.x, drag.y] = at(ev);
    if (!drag.moved && Math.hypot(drag.x - drag.x0, drag.y - drag.y0) < DRAG_THRESHOLD) return;
    if (!drag.moved) { drag.moved = true; drag.sentX = drag.x0; drag.sentY = drag.y0; }
    if (!drag.queued) { drag.queued = true; requestAnimationFrame(sendDrag); }
  }, { capture: true });
  window.addEventListener("mouseup", () => { if (drag && drag.moved) sendDrag(); drag = null; }, { capture: true });
  const img = () => document.querySelector(".results-view img.frame");

  window.addEventListener("wheel", (ev) => {
    if (zoom(ev.target, factor(ev), ev.clientX, ev.clientY)) {
      ev.preventDefault();
      ev.stopPropagation();
    }
  }, { capture: true, passive: false });

  // A drag in a browser-drawn view (orbit, pan) ends: the server takes the camera, as after a wheel.
  window.addEventListener("pointerup", (ev) => {
    const v = viewAt(ev.target);
    if (!v || !v.sync || !v.local()) return;
    clearTimeout(timers.get(v.ref));
    timers.set(v.ref, setTimeout(() => {
      try { window.trame.trigger(v.sync, [v.r.getCamera()]); } catch (e) { /* not drawn yet */ }
    }, SYNC_AFTER));
  }, { capture: true });

  // Safari's trackpad pinch: gesture events carrying the scale since the gesture began.
  let gesture = null;
  window.addEventListener("gesturestart", (ev) => { gesture = { scale: 1, target: ev.target }; ev.preventDefault(); }, { capture: true, passive: false });
  window.addEventListener("gesturechange", (ev) => {
    if (!gesture) return;
    const f = ev.scale / gesture.scale;
    gesture.scale = ev.scale;
    if (zoom(gesture.target, f, ev.clientX, ev.clientY)) { ev.preventDefault(); ev.stopPropagation(); }
  }, { capture: true, passive: false });
  window.addEventListener("gestureend", () => { gesture = null; }, { capture: true });

  // F (and vtk.js's own R, whose framing is not the server's) fits the case or results view, Shift+F frames its
  // selection (the toolbars' Fit and Frame buttons), unless typing.
  const typing = (el) => el && (el.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName));
  const caseShown = () => { const r = (window.trame && window.trame.refs) || {}; return r.caseview && r.caseview.$el && r.caseview.$el.offsetParent; };
  const resultsShown = () => { const v = document.querySelector(".viewport.results-view"); return v && v.offsetParent; };
  window.addEventListener("keydown", (ev) => {
    if ((ev.code !== "KeyF" && ev.code !== "KeyR") || ev.metaKey || ev.ctrlKey || ev.altKey || typing(ev.target)) return;
    const frame = ev.code === "KeyF" && ev.shiftKey;
    if (caseShown()) {
      ev.preventDefault();
      window.trame.trigger(frame ? "case_frame" : "case_fit", frame ? [] : [camera("caseview")]);
    } else if (resultsShown() && ev.code === "KeyF") {
      ev.preventDefault();
      window.trame.trigger(frame ? "geo_frame" : "geo_fit", [camera("geoview")]);
    }
  }, { capture: true });
  window.addEventListener("keyup", (ev) => { if (ev.code === "KeyR" && caseShown()) ev.stopPropagation(); }, { capture: true });

  // A case view drawn for the first time frames itself (vtk.js): it asks for the server's camera instead,
  // which the server could not send before the view was there.
  let framed = null;
  setInterval(() => {
    const r = window.trame && window.trame.refs && window.trame.refs.caseview;
    if (!r || r === framed || !caseShown() || state("caseview_mode") === "remote") return;
    let cam = null;
    try { cam = r.getCamera(); } catch (e) { return; }
    if (!cam || !cam.position || cam.position[2] === 1) return; // no scene yet
    framed = r;
    window.trame.trigger("case_view_ready");
  }, 200);

  window.mallardZoom = { pixels, factor, dolly, at, camera, internals, clipped: () => clippedBy.size, framed: () => !!framed, preview: preview.state };
})();
