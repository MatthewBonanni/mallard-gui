// Layout problems in the rendered UI: within every control, icons and labels share one vertical center and a
// button's content is centered in it; icons are one size and inside their boxes; buttons on one row share one
// height; no text is cut off without an ellipsis and no control's content spills out of it; and text has the
// contrast WCAG AA asks (4.5:1, 3:1 for large text) against what is behind it. Returns a list of
// {control, problem, dy} for the test to report.
(tolerance) => {
  const tol = tolerance || 1.0;
  const controls = document.querySelectorAll(
    "button, .tab, .tree-row, .run-row, .badge, .chip, .pill, .tag, .brand, .loc-row > .ticon, .statusbar .loc, .prob, .events-row"
  );
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== "hidden" && s.display !== "none";
  };
  const name = (el) =>
    (el.className && typeof el.className === "string" ? "." + el.className.trim().split(/\s+/).join(".") : "") +
    " " + (el.getAttribute("title") || el.textContent.trim().slice(0, 30));
  // A text node's box: a Range around it, so a label is measured by its glyphs, not its parent.
  const textBox = (node) => {
    const range = document.createRange();
    range.selectNodeContents(node);
    const rects = [...range.getClientRects()].filter((r) => r.height > 0);
    return rects.length ? rects[0] : null;
  };
  const problems = [];
  for (const el of controls) {
    if (!visible(el)) continue;
    const box = el.getBoundingClientRect();
    if (box.height > 48) continue; // multi-line rows are not single controls
    const cy = box.top + box.height / 2;
    const parts = [];
    for (const svg of el.querySelectorAll("svg, img")) {
      if (!visible(svg) || svg.closest(".m-plot")) continue;
      const r = svg.getBoundingClientRect();
      parts.push({ what: "icon", cy: r.top + r.height / 2, r });
    }
    const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
    let first = null, last = null;
    for (let n = walker.nextNode(); n; n = walker.nextNode()) {
      if (!n.textContent.trim() || !visible(n.parentElement)) continue;
      if (n.parentElement.closest("select, option, input, textarea")) continue;
      const r = textBox(n);
      if (!r) continue;
      parts.push({ what: "text", cy: r.top + r.height / 2, r });
      first = first || r;
      last = r;
    }
    for (const p of parts) {
      const dy = p.cy - cy;
      if (Math.abs(dy) > tol + (p.what === "text" ? 0.75 : 0)) {
        problems.push({ control: name(el), problem: `${p.what} off the vertical center`, dy: +dy.toFixed(2) });
      }
    }
    // Buttons, badges and pills: content centered horizontally when the control centers it.
    const centered = el.matches("button.btn, button.tb, .badge, .pill, .tag, .rail button, .icon-btn");
    if (centered && parts.length) {
      const left = Math.min(...parts.map((p) => p.r.left));
      const right = Math.max(...parts.map((p) => p.r.right));
      const dx = (left + right) / 2 - (box.left + box.width / 2);
      if (Math.abs(dx) > tol + 0.75) problems.push({ control: name(el), problem: "content off the horizontal center", dy: +dx.toFixed(2) });
    }
  }
  // One icon size, and each icon inside its box.
  const sizes = new Set();
  for (const svg of document.querySelectorAll("svg[aria-hidden]")) {
    if (!visible(svg)) continue;
    const r = svg.getBoundingClientRect();
    sizes.add(`${Math.round(r.width)}x${Math.round(r.height)}`);
    const box = svg.parentElement.getBoundingClientRect();
    if (r.width > box.width + 0.5 || r.height > box.height + 0.5) {
      problems.push({ control: name(svg.parentElement), problem: "icon overflows its box", dy: +(r.height - box.height).toFixed(2) });
    }
  }
  if (sizes.size > 1) problems.push({ control: "icons", problem: `sizes ${[...sizes].join(", ")}`, dy: 0 });
  // Controls on one toolbar row share one height.
  for (const bar of document.querySelectorAll(".toolbar, .actions, .run-head, .tabs, .topbar, .textbar")) {
    if (!visible(bar)) continue;
    const heights = new Set(
      [...bar.querySelectorAll(":scope > button, :scope > .tb, :scope > .btn")]
        .filter(visible)
        .map((b) => Math.round(b.getBoundingClientRect().height))
    );
    if (heights.size > 1) problems.push({ control: name(bar), problem: `buttons of heights ${[...heights].join(", ")}`, dy: 0 });
  }
  // Text cut off: a box narrower than its text that hides the rest without an ellipsis, or a control whose
  // content spills past its edge.
  for (const el of document.querySelectorAll("body *")) {
    if (!visible(el) || el.closest("svg, select, option, textarea, .m-plot, .log, pre, .case-pins")) continue;
    if (el.scrollWidth <= el.clientWidth + 1 || el.clientWidth === 0) continue;
    const s = getComputedStyle(el);
    const ownText = [...el.childNodes].some((n) => n.nodeType === 3 && n.textContent.trim());
    if (el.matches("button, .btn, .tb, .tab, .pill, .badge, .tag, .chip, .seg") && s.overflowX === "visible") {
      problems.push({ control: name(el), problem: "content wider than the control", dy: el.scrollWidth - el.clientWidth });
    } else if (ownText && s.overflowX === "hidden" && s.textOverflow !== "ellipsis" && !el.matches("input")) {
      problems.push({ control: name(el), problem: "text cut off without an ellipsis", dy: el.scrollWidth - el.clientWidth });
    }
  }
  // Contrast: each text's color over the backgrounds behind it, alpha and opacity included. Disabled controls,
  // placeholders and text over the 3D view are exempt.
  const parse = (c) => {
    let m = c.match(/rgba?\(([^)]+)\)/);
    if (m) {
      const v = m[1].split(/[\s,/]+/).filter(Boolean).map(Number);
      return [v[0], v[1], v[2], v.length > 3 ? v[3] : 1];
    }
    m = c.match(/color\(srgb ([^)]+)\)/);
    if (m) {
      const v = m[1].split(/[\s/]+/).filter(Boolean).map(Number);
      return [v[0] * 255, v[1] * 255, v[2] * 255, v.length > 3 ? v[3] : 1];
    }
    return null;
  };
  const over = (top, under) => {
    const a = top[3];
    return [0, 1, 2].map((i) => top[i] * a + under[i] * (1 - a)).concat([1]);
  };
  const lum = (c) => {
    const f = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; };
    return 0.2126 * f(c[0]) + 0.7152 * f(c[1]) + 0.0722 * f(c[2]);
  };
  const background = (el) => {
    const layers = [];
    for (let e = el; e; e = e.parentElement) {
      const b = parse(getComputedStyle(e).backgroundColor);
      if (b && b[3] > 0) layers.push(b);
      if (b && b[3] >= 1) break;
    }
    let bg = parse(getComputedStyle(document.body).backgroundColor) || [255, 255, 255, 1];
    for (const l of layers.reverse()) bg = over(l, bg);
    return bg;
  };
  const seen = new Set();
  const texts = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  for (let n = texts.nextNode(); n; n = texts.nextNode()) {
    const el = n.parentElement;
    if (!n.textContent.trim() || !el || seen.has(el) || !visible(el)) continue;
    seen.add(el);
    if (el.closest("svg, .viewport .case-pins, .viewport .tip, :disabled, [aria-disabled=true], option, .m-plot, .colorbar .cb-bar")) continue;
    const r = el.getBoundingClientRect();
    if (r.bottom < 0 || r.top > innerHeight || r.right < 0 || r.left > innerWidth) continue;
    const s = getComputedStyle(el);
    let fg = parse(s.color);
    if (!fg) continue;
    let opacity = 1;
    for (let e = el; e; e = e.parentElement) opacity *= Number(getComputedStyle(e).opacity);
    if (opacity < 0.99) continue; // dimmed on purpose: a disabled or inactive state
    const bg = background(el);
    fg = over(fg, bg);
    const [a, b] = [lum(fg), lum(bg)].sort((x, y) => y - x);
    const ratio = (a + 0.05) / (b + 0.05);
    const size = parseFloat(s.fontSize);
    const large = size >= 24 || (size >= 18.66 && Number(s.fontWeight) >= 700);
    if (ratio < (large ? 3 : 4.5) - 0.05) {
      problems.push({ control: name(el), problem: `contrast ${ratio.toFixed(2)}:1`, dy: +ratio.toFixed(2) });
    }
  }
  return problems;
};
