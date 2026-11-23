// window.mallardShell: what the desktop shell offers the GUI's page. Absent in a browser.
(() => {
  const invoke = (cmd, args) => window.__TAURI_INTERNALS__.invoke(cmd, args);
  const text = (v) => String(v ?? "");
  Object.defineProperty(window, "mallardShell", {
    value: Object.freeze({
      version: 3,
      // level: "info" | "warning" | "error"; resolves once the notification is posted
      notify: ({ title, body = "", level = "info" } = {}) =>
        invoke("notify", { title: text(title), body: text(body), level: text(level) }),
      // The document the window shows: its title, macOS's edited dot and proxy icon, the menu's Undo, Redo and
      // Show Mesh. The first call tells the shell the page decides about quitting (Save / Don't Save / Cancel).
      setDocument: ({ title, path, edited, canUndo, canRedo, mesh } = {}) =>
        invoke("set_document", {
          title: text(title), path: text(path), edited: !!edited, canUndo: !!canUndo, canRedo: !!canRedo,
          mesh: !!mesh,
        }),
      // The system's open or save panel; resolves to the chosen path, or null when cancelled.
      // mode: "open" | "save"; filters: [{name, extensions: ["toml"]}]
      choosePath: ({ mode, title, directory, name, filters } = {}) =>
        invoke("choose_path", {
          mode: text(mode), title: text(title), directory: text(directory), name: text(name),
          filters: (Array.isArray(filters) ? filters : []).map((f) => ({
            name: text(f && f.name), extensions: (Array.isArray(f && f.extensions) ? f.extensions : []).map(text),
          })),
        }),
      // Once the page has saved or let go of its edits: this window's project is replaced, closed, or the app quits
      openProject: (path) => invoke("open_project", { path: text(path) }),
      closeProject: () => invoke("close_project"),
      quit: () => invoke("quit"),
      reveal: (path) => invoke("reveal", { path: text(path) }),
      openUrl: (url) => invoke("open_url", { url: text(url) }),
    }),
  });
})();
