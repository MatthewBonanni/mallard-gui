// Copying messages: any element with `data-copy` (its full text) copies by its copy button (`.copy-btn`), its
// context menu, or Cmd/Ctrl+C while it has focus. In a list marked `data-copy-list`, the selected items (`.sel`)
// copy together. A text selection or a field keeps the browser's own copy.
(() => {
  const ITEM = "[data-copy]";
  let menu = null;

  async function write(text) {
    try {
      await navigator.clipboard.writeText(text);
      return;
    } catch (e) { /* no permission, or an old engine: the selection way below */ }
    const area = document.createElement("textarea");
    area.value = text;
    area.setAttribute("readonly", "");
    area.style.cssText = "position:fixed;left:-9999px;top:0";
    document.body.appendChild(area);
    area.select();
    try { document.execCommand("copy"); } finally { area.remove(); }
  }

  // An item and, when it is selected in a list, every selected item of that list, in order.
  function texts(item) {
    const list = item.closest("[data-copy-list]");
    if (list && item.classList.contains("sel")) {
      const sel = [...list.querySelectorAll(ITEM + ".sel")];
      if (sel.length > 1) return sel.map((el) => el.dataset.copy);
    }
    return [item.dataset.copy];
  }

  function flash(el) {
    if (!el) return;
    el.classList.add("copied");
    setTimeout(() => el.classList.remove("copied"), 1200);
  }

  function copy(item, feedback) {
    const all = texts(item).filter(Boolean);
    if (!all.length) return;
    write(all.join("\n\n"));
    flash(feedback || item);
  }

  function closeMenu() {
    if (menu) menu.remove();
    menu = null;
  }

  function selectedText() {
    const sel = window.getSelection();
    return sel && !sel.isCollapsed && sel.toString().trim() !== "";
  }

  document.addEventListener("click", (e) => {
    const button = e.target.closest && e.target.closest(".copy-btn");
    if (menu && !(e.target.closest && e.target.closest(".copy-menu"))) closeMenu();
    if (!button) return;
    const item = button.closest(ITEM);
    if (!item) return;
    e.preventDefault();
    e.stopPropagation(); // the copy button does not also open what its row opens
    copy(item, button);
  }, true);

  document.addEventListener("contextmenu", (e) => {
    const item = e.target.closest && e.target.closest(ITEM);
    closeMenu();
    if (!item || selectedText() || e.target.closest("input, textarea")) return;
    e.preventDefault();
    const n = texts(item).length;
    menu = document.createElement("div");
    menu.className = "copy-menu";
    menu.setAttribute("role", "menu");
    const entry = document.createElement("button");
    entry.type = "button";
    entry.setAttribute("role", "menuitem");
    entry.textContent = n > 1 ? `Copy ${n} messages` : "Copy";
    entry.addEventListener("click", (ev) => { ev.stopPropagation(); copy(item); closeMenu(); });
    menu.appendChild(entry);
    document.body.appendChild(menu);
    const w = menu.offsetWidth, h = menu.offsetHeight;
    menu.style.left = `${Math.min(e.clientX, window.innerWidth - w - 4)}px`;
    menu.style.top = `${Math.min(e.clientY, window.innerHeight - h - 4)}px`;
    entry.focus();
  });

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeMenu();
    if (!(e.metaKey || e.ctrlKey) || e.key.toLowerCase() !== "c" || e.shiftKey || e.altKey) return;
    if (selectedText()) return;
    const at = document.activeElement;
    if (!at || at.closest("input, textarea, [contenteditable='true']")) return;
    let item = at.closest(ITEM);
    if (!item) {
      const list = at.closest("[data-copy-list]");
      item = list && list.querySelector(ITEM + ".sel");
    }
    if (!item) return;
    e.preventDefault();
    copy(item);
  });

  window.addEventListener("resize", closeMenu);
  window.addEventListener("blur", closeMenu);

  // A list's selection after a click on item `i`: alone; Cmd/Ctrl toggles it; Shift extends from the last one.
  function pick(sel, i, e) {
    sel = Array.isArray(sel) ? sel : [];
    if (e && (e.metaKey || e.ctrlKey)) return sel.includes(i) ? sel.filter((j) => j !== i) : [...sel, i];
    if (e && e.shiftKey && sel.length) {
      const from = sel[sel.length - 1];
      const out = [];
      for (let j = Math.min(from, i); j <= Math.max(from, i); j++) out.push(j);
      return from <= i ? out : out.reverse();
    }
    return [i];
  }

  // After the details of item `i` are asked for: the highlighted entry of `selector` scrolled into view.
  function reveal(selector) {
    let tries = 0;
    const go = () => {
      const el = document.querySelector(selector);
      if (el) {
        el.scrollIntoView({ block: "center" });
        return;
      }
      if (++tries < 20) setTimeout(go, 50);
    };
    requestAnimationFrame(go);
  }

  window.mallardCopy = { write, pick, reveal };
})();
