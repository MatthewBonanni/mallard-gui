// The one dropdown (ui/dropdown.py): a `.dd` button whose data-options (JSON: strings, or {value, label, note,
// swatch}, a swatch being the page's own SVG drawn before the label)
// open as a list under it with a search field that has the focus, so typing filters at once. Arrow keys move
// through what is shown, Enter picks, Escape closes (the button keeps the focus), a click outside closes. With
// data-free, text that names no option can be picked as typed. A pick is a `dd-pick` event on the button whose
// detail is the value: the page's handler (`@dd-pick`) sends it on.
(() => {
  let cur = null; // {button, pop, input, list, items, shown, active}

  const parse = (button) => {
    try {
      return (JSON.parse(button.dataset.options || "[]") || []).map((o) =>
        typeof o === "object" && o !== null
          ? { value: String(o.value), label: String(o.label ?? o.value), note: String(o.note ?? ""), swatch: o.swatch || "" }
          : { value: String(o), label: String(o), note: "" });
    } catch (e) {
      return [];
    }
  };

  function place() {
    const r = cur.button.getBoundingClientRect();
    const pop = cur.pop;
    pop.style.minWidth = Math.max(r.width, 180) + "px";
    pop.style.left = Math.max(4, Math.min(r.left, window.innerWidth - pop.offsetWidth - 4)) + "px";
    const below = window.innerHeight - r.bottom - 8;
    const up = below < Math.min(pop.offsetHeight, 220) && r.top > below;
    pop.style.top = up ? "" : r.bottom + 2 + "px";
    pop.style.bottom = up ? window.innerHeight - r.top + 2 + "px" : "";
    pop.style.maxHeight = Math.max(120, (up ? r.top : below) - 8) + "px";
  }

  function render() {
    const q = cur.input.value.trim().toLowerCase();
    // Labels that start with the text, then labels that hold it, then notes that do.
    const rank = (o) => {
      const l = o.label.toLowerCase();
      return l.startsWith(q) ? 0 : l.includes(q) ? 1 : o.note.toLowerCase().includes(q) ? 2 : 3;
    };
    cur.shown = q ? cur.items.map((o, i) => [rank(o), i, o]).filter((x) => x[0] < 3)
      .sort((a, b) => a[0] - b[0] || a[1] - b[1]).map((x) => x[2]) : cur.items.slice();
    const free = cur.button.dataset.free && q && !cur.items.some((o) => o.value.toLowerCase() === q);
    if (free) cur.shown.push({ value: cur.input.value.trim(), label: cur.input.value.trim(), note: "as typed", free: true });
    cur.active = Math.max(0, Math.min(cur.active, cur.shown.length - 1));
    if (q && !cur.shown[cur.active]) cur.active = 0;
    cur.list.replaceChildren(...cur.shown.map((o, i) => {
      const el = document.createElement("div");
      el.className = "dd-item" + (i === cur.active ? " active" : "") + (o.value === cur.button.dataset.value ? " on" : "")
        + (o.free ? " free" : "");
      el.setAttribute("role", "option");
      el.setAttribute("aria-selected", String(i === cur.active));
      el.dataset.index = String(i);
      if (o.swatch) {
        const swatch = document.createElement("span");
        swatch.className = "dd-swatch";
        swatch.innerHTML = o.swatch;
        el.append(swatch);
      }
      const label = document.createElement("span");
      label.className = "dd-label";
      label.textContent = o.label;
      el.append(label);
      if (o.note) {
        const note = document.createElement("span");
        note.className = "dd-note";
        note.textContent = o.note;
        el.append(note);
        el.title = o.label + " · " + o.note;
      }
      return el;
    }));
    if (!cur.shown.length) {
      const none = document.createElement("div");
      none.className = "dd-none";
      none.textContent = "no match";
      cur.list.append(none);
    }
    const act = cur.list.children[cur.active];
    if (act && act.scrollIntoView) act.scrollIntoView({ block: "nearest" });
  }

  function open(button, typed) {
    close(false);
    const pop = document.createElement("div");
    pop.className = "dd-pop";
    pop.setAttribute("role", "dialog");
    const input = document.createElement("input");
    input.className = "dd-search";
    input.setAttribute("aria-label", "search " + (button.getAttribute("aria-label") || ""));
    input.placeholder = "search";
    input.spellcheck = false;
    const list = document.createElement("div");
    list.className = "dd-list";
    list.setAttribute("role", "listbox");
    pop.append(input, list);
    document.body.append(pop);
    const items = parse(button);
    const at = items.findIndex((o) => o.value === button.dataset.value);
    cur = { button, pop, input, list, items, shown: [], active: Math.max(0, at) };
    button.setAttribute("aria-expanded", "true");
    input.value = typed || "";
    render();
    place();
    input.focus();
    input.addEventListener("input", () => { cur.active = 0; render(); });
    input.addEventListener("keydown", key);
    list.addEventListener("mousedown", (ev) => ev.preventDefault());
    list.addEventListener("click", (ev) => {
      const item = ev.target.closest(".dd-item");
      if (item) pick(+item.dataset.index);
    });
    list.addEventListener("mousemove", (ev) => {
      const item = ev.target.closest(".dd-item");
      if (item && +item.dataset.index !== cur.active) { cur.active = +item.dataset.index; render(); }
    });
  }

  function close(refocus) {
    if (!cur) return;
    const { button, pop } = cur;
    cur = null;
    pop.remove();
    button.removeAttribute("aria-expanded");
    if (refocus && button.isConnected) button.focus();
  }

  function pick(i) {
    const o = cur && cur.shown[i];
    if (!o) return;
    const button = cur.button;
    close(true);
    button.dispatchEvent(new CustomEvent("dd-pick", { detail: o.value }));
  }

  function key(ev) {
    if (!cur) return;
    const n = cur.shown.length;
    if (ev.key === "ArrowDown" || ev.key === "ArrowUp") {
      ev.preventDefault();
      if (n) cur.active = (cur.active + (ev.key === "ArrowDown" ? 1 : n - 1)) % n;
      render();
    } else if (ev.key === "PageDown" || ev.key === "PageUp") {
      ev.preventDefault();
      cur.active = Math.max(0, Math.min(n - 1, cur.active + (ev.key === "PageDown" ? 8 : -8)));
      render();
    } else if (ev.key === "Enter") {
      ev.preventDefault();
      pick(cur.active);
    } else if (ev.key === "Escape") {
      ev.preventDefault();
      ev.stopPropagation();
      close(true);
    } else if (ev.key === "Tab") {
      close(false);
    }
  }

  document.addEventListener("click", (ev) => {
    const button = ev.target.closest && ev.target.closest(".dd");
    if (button && !button.disabled) {
      ev.preventDefault();
      if (cur && cur.button === button) close(true);
      else open(button, "");
    } else if (cur && !cur.pop.contains(ev.target)) {
      close(false);
    }
  });
  // A focused dropdown opens on the keys a list opens on, or on a typed character (which starts the search).
  document.addEventListener("keydown", (ev) => {
    const button = ev.target.closest && ev.target.closest(".dd");
    if (!button || button.disabled || cur) return;
    if (["ArrowDown", "ArrowUp", "Enter", " "].includes(ev.key)) {
      ev.preventDefault();
      open(button, "");
    } else if (ev.key.length === 1 && !ev.metaKey && !ev.ctrlKey && !ev.altKey) {
      ev.preventDefault();
      open(button, ev.key);
    }
  });
  window.addEventListener("resize", () => close(false));
  document.addEventListener("scroll", (ev) => { if (cur && !cur.pop.contains(ev.target)) close(false); }, true);
})();
