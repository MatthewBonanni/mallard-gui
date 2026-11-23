// Labels and drag handles over the case view while the browser draws it (vtk.js): each frame, every
// `.case-pin` is placed at its world point (`data-p`) through the view's camera, so pins follow an orbit
// with no server round trip. A handle is dragged here, on the plane through it facing the camera, and so is
// the selected shape's body; positions go to the server (`case_handle`).
(() => {
  const sub = (a, b) => [a[0] - b[0], a[1] - b[1], a[2] - b[2]];
  const add = (a, b) => [a[0] + b[0], a[1] + b[1], a[2] + b[2]];
  const mul = (a, s) => [a[0] * s, a[1] * s, a[2] * s];
  const dot = (a, b) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
  const cross = (a, b) => [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
  const unit = (a) => mul(a, 1 / (Math.hypot(a[0], a[1], a[2]) || 1));

  function frame(cam, w, h) {
    const f = unit(sub(cam.focalPoint, cam.position));
    const r = unit(cross(f, cam.viewUp));
    const u = cross(r, f);
    const t = Math.tan((cam.viewAngle * Math.PI) / 360);
    return { f, r, u, t, w, h, aspect: w / h, cam };
  }

  function project(fr, p) {
    const d = sub(p, fr.cam.position);
    const x = dot(d, fr.r), y = dot(d, fr.u), z = dot(d, fr.f);
    let nx, ny;
    if (fr.cam.parallelProjection) {
      nx = x / (fr.cam.parallelScale * fr.aspect);
      ny = y / fr.cam.parallelScale;
    } else {
      if (z <= 0) return null;
      nx = x / (z * fr.t * fr.aspect);
      ny = y / (z * fr.t);
    }
    return [((nx + 1) / 2) * fr.w, ((1 - ny) / 2) * fr.h];
  }

  function unproject(fr, sx, sy, through) {
    const nx = (2 * sx) / fr.w - 1, ny = 1 - (2 * sy) / fr.h;
    let origin, dir;
    if (fr.cam.parallelProjection) {
      const s = fr.cam.parallelScale;
      origin = add(fr.cam.position, add(mul(fr.r, nx * s * fr.aspect), mul(fr.u, ny * s)));
      dir = fr.f;
    } else {
      origin = fr.cam.position;
      dir = add(fr.f, add(mul(fr.r, nx * fr.t * fr.aspect), mul(fr.u, ny * fr.t)));
    }
    const k = dot(sub(through, origin), fr.f) / dot(dir, fr.f);
    return add(origin, mul(dir, k));
  }

  const held = new Map(); // handle index -> its position while dragged here
  let current = null;

  // The case view's vtk.js renderer, render window and container (zoom.js), or null: hidden, or not built.
  function view() {
    const z = window.mallardZoom;
    return (z && z.internals("caseview")) || null;
  }

  // Where the view draws the world point p, in CSS px from the layer's top left; null when it is behind the
  // camera, outside the view, or there is no view to ask. vtk.js's own transform (the renderer's, through its
  // render window) says where, so a pin sits on what is drawn whatever the projection, the pixel ratio or the
  // canvas's size. While the server draws (images), the canvas is kept off screen with the server's camera:
  // its transform holds as a fraction of the view, which the image fills as the canvas would.
  function onScreen(v, layer, p) {
    const [w, h] = v.openglRenderWindow.getSize();
    if (!w || !h) return null;
    const d = v.openglRenderWindow.worldToDisplay(p[0], p[1], p[2], v.renderer);
    if (!d || !d.every(Number.isFinite) || d[2] < 0 || d[2] > 1) return null;
    const fx = d[0] / w, fy = 1 - d[1] / h;
    if (fx < 0 || fx > 1 || fy < 0 || fy > 1) return null;
    const remote = window.trame.state.get("caseview_mode") === "remote";
    const box = remote ? layer : v.container.getBoundingClientRect();
    if (!box.width || !box.height) return null;
    return [box.left - layer.left + fx * box.width, box.top - layer.top + fy * box.height];
  }

  // Every frame, each pin is placed and shown, or hidden: style.css hides every pin, and one is made visible
  // here in the frame its transform is set (on the element, which the page's redraws leave alone), so a pin
  // that cannot be placed (no view yet, no camera, a hidden or empty layer, a point out of view) never sits
  // at the layer's corner.
  function place() {
    requestAnimationFrame(place);
    const layer = document.querySelector(".case-pins");
    if (!layer) return;
    const pins = layer.querySelectorAll(".case-pin");
    let v = null, box = null;
    try {
      v = view();
      box = layer.getBoundingClientRect();
      if (v && !v.renderer.getActors().length) v = null; // no scene yet: the camera is vtk.js's default
    } catch (e) { v = null; }
    const ok = v && box && box.width > 0 && box.height > 0;
    current = null;
    if (ok) {
      const cam = v.renderer.getActiveCamera().get("position", "focalPoint", "viewUp", "parallelProjection", "parallelScale", "viewAngle");
      current = frame(cam, box.width, box.height);
    }
    // The orientation triad's corner is its own: a label or handle that would sit on it is not drawn.
    const triad = ok && layer.parentElement.querySelector(".triad");
    const corner = triad && triad.offsetParent ? triad.getBoundingClientRect() : null;
    for (const el of pins) {
      let s = null;
      if (ok) {
        try {
          const i = Number(el.dataset.i);
          const p = (el.dataset.kind === "handle" && held.get(i)) || JSON.parse(el.dataset.p || "null");
          s = p && onScreen(v, box, p);
          if (s && corner && !held.has(i)) {
            const x = box.left + s[0], y = box.top + s[1];
            if (x > corner.left - 6 && x < corner.right + 6 && y > corner.top - 6 && y < corner.bottom + 6) s = null;
          }
        } catch (e) { s = null; }
      }
      if (s) el.style.transform = `translate(${s[0].toFixed(1)}px, ${s[1].toFixed(1)}px)`;
      el.style.visibility = s ? "visible" : "";
    }
  }
  requestAnimationFrame(place);

  // The selected shape's body (`case_body`): a drag on it moves the shape by its handle that sets a point.
  function onBody(sx, sy) {
    const body = window.trame && window.trame.state && window.trame.state.get("case_body");
    if (!body || !current) return null;
    const pts = body.points.map((p) => project(current, p)).filter(Boolean);
    if (!pts.length) return null;
    if (body.area) return insideHull(hull(pts), sx, sy) ? body : null;
    for (const [a, b] of body.segments) {
      const p = project(current, body.points[a]), q = project(current, body.points[b]);
      if (p && q && segmentDistance(p, q, sx, sy) <= 6) return body;
    }
    return null;
  }

  function hull(pts) {
    const s = [...pts].sort((a, b) => a[0] - b[0] || a[1] - b[1]);
    const turn = (o, a, b) => (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0]);
    const half = (list) => {
      const out = [];
      for (const p of list) {
        while (out.length >= 2 && turn(out[out.length - 2], out[out.length - 1], p) <= 0) out.pop();
        out.push(p);
      }
      out.pop();
      return out;
    };
    return [...half(s), ...half(s.reverse())];
  }

  function insideHull(h, x, y) {
    if (h.length < 3) return h.some((p) => Math.hypot(p[0] - x, p[1] - y) <= 6);
    for (let k = 0; k < h.length; k++) {
      const a = h[k], b = h[(k + 1) % h.length];
      if ((b[0] - a[0]) * (y - a[1]) - (b[1] - a[1]) * (x - a[0]) < 0) return false;
    }
    return true;
  }

  function segmentDistance(p, q, x, y) {
    const dx = q[0] - p[0], dy = q[1] - p[1];
    const t = Math.max(0, Math.min(1, ((x - p[0]) * dx + (y - p[1]) * dy) / (dx * dx + dy * dy || 1)));
    return Math.hypot(p[0] + t * dx - x, p[1] + t * dy - y);
  }

  // A drag of handle i: from its handle dot, or from the body (by the pointer's offset from where it took
  // hold). Its position goes to the server at most every 50 ms and once more, as released, when the drag
  // ends however it ends (pointerup, pointercancel, a lost capture).
  function drag(ev, i, start, layer, el) {
    ev.preventDefault();
    ev.stopPropagation();
    const target = el || layer;
    const box0 = layer.getBoundingClientRect();
    const grab = unproject(current, ev.clientX - box0.left, ev.clientY - box0.top, start);
    let last = 0, timer = null, pos = start, ended = false;
    held.set(i, start);
    if (el) el.classList.add("held");
    try { target.setPointerCapture(ev.pointerId); } catch (e) { /* a synthetic pointer */ }
    const send = (done) => window.trame.trigger("case_handle", [i, pos, done]);
    const move = (e) => {
      if (e.pointerId !== ev.pointerId) return;
      const box = layer.getBoundingClientRect();
      const at = unproject(current, e.clientX - box.left, e.clientY - box.top, start);
      pos = add(start, sub(at, grab));
      held.set(i, pos);
      const now = performance.now();
      if (now - last > 50) { last = now; send(false); }
      else if (!timer) timer = setTimeout(() => { timer = null; last = performance.now(); send(false); }, 50);
    };
    const end = (e) => {
      if (ended || (e && e.pointerId !== ev.pointerId)) return;
      ended = true;
      for (const t of ["pointermove", "pointerup", "pointercancel", "lostpointercapture"]) {
        target.removeEventListener(t, t === "pointermove" ? move : end);
      }
      if (timer) clearTimeout(timer);
      if (el) el.classList.remove("held");
      send(true);
      setTimeout(() => { if (held.get(i) === pos) held.delete(i); }, 400); // until the server's pins carry it
    };
    target.addEventListener("pointermove", move);
    for (const t of ["pointerup", "pointercancel", "lostpointercapture"]) target.addEventListener(t, end);
  }

  document.addEventListener("pointerdown", (ev) => {
    if (ev.button !== 0 || !current) return;
    const el = ev.target.closest && ev.target.closest(".case-pin.handle");
    if (el) {
      drag(ev, Number(el.dataset.i), JSON.parse(el.dataset.p), el.closest(".case-pins"), el);
      return;
    }
    const layer = !ev.shiftKey && ev.target.closest && ev.target.closest(".viewport") && document.querySelector(".case-pins");
    if (!layer) return;
    const box = layer.getBoundingClientRect();
    const body = onBody(ev.clientX - box.left, ev.clientY - box.top);
    if (!body) return;
    const handle = layer.querySelector(`.case-pin.handle[data-i="${body.handle}"]`);
    if (handle) drag(ev, body.handle, JSON.parse(handle.dataset.p), layer, handle);
  }, true);

  // A press that moved is a pan or an orbit: its release is no click, so the view picks nothing under it.
  let pressed = null;
  const inView = (ev) => ev.target.closest && ev.target.closest(".viewport");
  window.addEventListener("pointerdown", (ev) => { pressed = inView(ev) ? [ev.clientX, ev.clientY] : null; }, true);
  window.addEventListener("click", (ev) => {
    if (pressed && inView(ev) && Math.hypot(ev.clientX - pressed[0], ev.clientY - pressed[1]) > 3) ev.stopPropagation();
  }, true);

  window.mallardPins = { project, unproject, frame, onBody };
})();
