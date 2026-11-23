// The orientation triad: the x, y and z axes as the view's camera sees them, in a corner of the case view and
// of the results view. Each frame it is drawn from the camera of its view: the browser's own (vtk.js) where the
// browser draws, the one the server's last image was rendered with otherwise. An axis pointing away from the
// viewer is drawn first and fainter; one pointing straight at or away from the viewer is a dot.
(() => {
  const SIZE = 64, HALF = SIZE / 2, REACH = 21; // px: the box, its middle, an axis's length in the picture plane
  const AXES = [["x", [1, 0, 0]], ["y", [0, 1, 0]], ["z", [0, 0, 1]]];
  const sub = (a, b) => [a[0] - b[0], a[1] - b[1], a[2] - b[2]];
  const dot = (a, b) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
  const cross = (a, b) => [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
  const unit = (a) => { const n = Math.hypot(a[0], a[1], a[2]) || 1; return [a[0] / n, a[1] / n, a[2] / n]; };
  const state = (key) => { try { return window.trame.state.get(key); } catch (e) { return undefined; } };

  // The camera of a triad's view: {position, focalPoint, viewUp}, or null while there is none to show.
  function camera(view) {
    const z = window.mallardZoom;
    if (!z) return null;
    if (view === "results" && !state("v_geometry")) {
      const c = state("v_camera");
      return c && c.position ? { position: c.position, focalPoint: c.focal_point, viewUp: c.view_up } : null;
    }
    const v = z.internals(view === "results" ? "geoview" : "caseview");
    if (!v || !v.renderer.getActors().length) return null;
    return v.renderer.getActiveCamera().get("position", "focalPoint", "viewUp");
  }

  function build(el) {
    const ns = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(ns, "svg");
    svg.setAttribute("viewBox", `0 0 ${SIZE} ${SIZE}`);
    svg.setAttribute("width", SIZE);
    svg.setAttribute("height", SIZE);
    const parts = {};
    for (const [name] of AXES) {
      const g = document.createElementNS(ns, "g");
      g.setAttribute("class", `axis ${name}`);
      const line = document.createElementNS(ns, "line");
      const tip = document.createElementNS(ns, "circle");
      const text = document.createElementNS(ns, "text");
      text.textContent = name.toUpperCase();
      g.append(line, tip, text);
      parts[name] = { g, line, tip, text };
    }
    el.replaceChildren(svg);
    return { svg, parts, key: "" };
  }

  const drawn = new WeakMap();
  function draw(el) {
    const cam = camera(el.dataset.view);
    el.classList.toggle("on", !!cam);
    if (!cam) return;
    const key = JSON.stringify(cam);
    const d = drawn.get(el) || build(el);
    drawn.set(el, d);
    if (d.key === key) return;
    d.key = key;
    const f = unit(sub(cam.focalPoint, cam.position));
    const r = unit(cross(f, cam.viewUp));
    const u = cross(r, f);
    const seen = AXES.map(([name, a]) => ({ name, x: dot(a, r), y: dot(a, u), toward: -dot(a, f) }));
    seen.sort((a, b) => a.toward - b.toward); // the farthest first, under the others
    for (const s of seen) {
      const p = d.parts[s.name];
      const x = HALF + REACH * s.x, y = HALF - REACH * s.y;
      p.line.setAttribute("x1", HALF); p.line.setAttribute("y1", HALF);
      p.line.setAttribute("x2", x.toFixed(2)); p.line.setAttribute("y2", y.toFixed(2));
      p.tip.setAttribute("cx", x.toFixed(2)); p.tip.setAttribute("cy", y.toFixed(2));
      p.tip.setAttribute("r", (2.2 + 0.8 * s.toward).toFixed(2));
      const along = Math.hypot(s.x, s.y);
      const lx = along > 0.2 ? x + 7 * s.x / along : x + 6, ly = along > 0.2 ? y - 7 * s.y / along : y - 6;
      p.text.setAttribute("x", lx.toFixed(2)); p.text.setAttribute("y", ly.toFixed(2));
      p.g.classList.toggle("away", s.toward < -0.05);
      p.g.dataset.x = s.x.toFixed(4); p.g.dataset.y = s.y.toFixed(4); p.g.dataset.toward = s.toward.toFixed(4);
      d.svg.append(p.g);
    }
  }

  (function frame() {
    requestAnimationFrame(frame);
    for (const el of document.querySelectorAll(".triad")) {
      try { draw(el); } catch (e) { el.classList.remove("on"); }
    }
  })();
})();
