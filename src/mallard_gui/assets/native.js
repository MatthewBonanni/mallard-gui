// Platform conventions. In the desktop app (window.mallardShell) the page is an application window, not a web
// page: `html.native` switches to the system font, arrow cursors and unselectable chrome (style.css), and the web
// view's own context menu and Reload, Print and Find keys are off where the app has no such commands. In both,
// Cmd/Ctrl+, opens Settings (the desktop menu's accelerator does it there).
(() => {
  const root = document.documentElement;
  const mac = /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent);
  root.classList.toggle("mac", mac);
  if (window.mallardShell) root.classList.add("native");
  const native = () => root.classList.contains("native");
  const selectable = "input, textarea, select, [contenteditable], pre, .log, .ext-diff, .selectable";

  document.addEventListener("contextmenu", (e) => {
    if (native() && !(e.target instanceof Element && e.target.closest(selectable))) e.preventDefault();
  });
  document.addEventListener("dragstart", (e) => {
    if (native() && e.target instanceof Element && e.target.matches("img, a")) e.preventDefault();
  });
  window.addEventListener(
    "keydown",
    (e) => {
      const mod = mac ? e.metaKey : e.ctrlKey;
      const key = e.key.toLowerCase();
      if (native() && (e.key === "F5" || (mod && !e.altKey && ["r", "p", "f", "g"].includes(key)))) {
        e.preventDefault();
        return;
      }
      if (e.key === "Escape" && window.trame && window.trame.state.get("ext_compare")) {
        window.trame.state.set("ext_compare", false);
        return;
      }
      if (!native() && mod && !e.shiftKey && !e.altKey && key === ",") {
        e.preventDefault();
        window.mallardMenu && window.mallardMenu("settings");
      }
    },
    true,
  );
})();
