// What goes wrong in the page reaches the server's page.log (pagelog.py): uncaught errors and rejected promises,
// errors vtk.js only prints, WebGL contexts lost and restored, and the case view's mode whenever it changes.
// The desktop app has no console to read them from.
(() => {
  const MAX = 300; // events a page reports, so a loop of errors cannot flood the log
  const queue = [];
  const seen = new Map(); // kind + detail -> times seen
  let sent = 0;

  function flush() {
    const t = window.trame;
    if (!t || typeof t.trigger !== "function") return;
    while (queue.length) {
      const [kind, detail] = queue.shift();
      try { Promise.resolve(t.trigger("page_event", [kind, detail])).catch(() => {}); } catch (e) { /* closing */ }
    }
  }

  function report(kind, detail) {
    const text = typeof detail === "string" ? detail : JSON.stringify(detail);
    const key = kind + "\n" + text;
    const times = (seen.get(key) || 0) + 1;
    seen.set(key, times);
    if (times > 3 || sent >= MAX) return; // the same event again and again says nothing new
    sent += 1;
    queue.push([kind, sent === MAX ? text + " (the page reports no more events)" : text]);
    flush();
  }
  setInterval(flush, 1000);

  const where = (canvas) => {
    const view = canvas.closest && canvas.closest(".viewport, .results-view, .geo-view");
    return { view: view ? view.className : "", size: [canvas.width, canvas.height], connected: canvas.isConnected };
  };

  window.addEventListener("error", (e) => {
    const stack = e.error && e.error.stack ? String(e.error.stack).split("\n").slice(0, 6).join(" | ") : "";
    report("error", { message: String(e.message), at: `${e.filename}:${e.lineno}:${e.colno}`, stack });
  });
  window.addEventListener("unhandledrejection", (e) => {
    const r = e.reason;
    const stack = r && r.stack ? String(r.stack).split("\n").slice(0, 6).join(" | ") : "";
    report("unhandledrejection", { message: String((r && r.message) || r), stack });
  });
  // Neither event bubbles; capturing listeners on the window see them for every canvas.
  window.addEventListener("webglcontextlost", (e) => report("webglcontextlost", where(e.target)), true);
  window.addEventListener("webglcontextrestored", (e) => report("webglcontextrestored", where(e.target)), true);

  const printed = console.error.bind(console);
  console.error = (...args) => {
    printed(...args);
    try { report("console.error", args.map((a) => String((a && a.stack) || a)).join(" ").trim().slice(0, 1000)); } catch (e) { /* unprintable */ }
  };

  let mode;
  setInterval(() => {
    let now;
    try { now = window.trame.state.get("caseview_mode"); } catch (e) { return; }
    if (now === mode || now === undefined) return;
    report("caseview_mode", { from: mode || null, to: now });
    mode = now;
  }, 250);

  // The engine and its graphics, once: what a report of a view that stopped drawing needs first.
  let gl = "";
  try {
    const c = document.createElement("canvas").getContext("webgl2");
    const info = c && c.getExtension("WEBGL_debug_renderer_info");
    gl = c ? String(c.getParameter(info ? info.UNMASKED_RENDERER_WEBGL : c.RENDERER)) : "no webgl2";
    const lose = c && c.getExtension("WEBGL_lose_context");
    if (lose) lose.loseContext();
  } catch (e) { gl = String(e); }
  report("page", { agent: navigator.userAgent, pixelRatio: window.devicePixelRatio, gl });

  window.mallardDiagnostics = { report };
})();
