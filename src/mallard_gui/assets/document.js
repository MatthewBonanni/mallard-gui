// The document's commands in the page: keyboard shortcuts in a browser (the desktop shell's menu has its own),
// the leave-page warning with unsaved edits, the menu's entry point (window.mallardMenu), the calls the
// server asks of the desktop shell's bridge, and choosing paths (window.mallardPaths).
(() => {
  const trigger = (name, args) => window.trame && window.trame.trigger(name, args);
  const get = (key) => {
    try { return window.trame.state.get(key); } catch (e) { return undefined; }
  };
  const editable = (el) =>
    !!el && (el.isContentEditable || el.tagName === "TEXTAREA" ||
      (el.tagName === "INPUT" && !["checkbox", "radio", "button", "range"].includes(el.type)));
  let leaving = false;
  // Cmd/Ctrl+1... the rail's pages, in its order (document_panel.py's PAGES; the desktop menu's accelerators).
  const PAGES = { 1: "case", 2: "runs", 3: "data", 4: "results", 5: "analysis", 6: "compare", 7: "remotes" };

  // A menu item or shortcut: Undo and Redo in a text field are the field's own.
  window.mallardMenu = (id, arg) => {
    if ((id === "undo" || id === "redo") && editable(document.activeElement)) {
      document.execCommand(id);
      return;
    }
    // A field being typed in commits on blur: before Save reads the document.
    if (editable(document.activeElement)) document.activeElement.blur();
    trigger("doc_client", [!!window.mallardShell]);
    trigger("doc_command", arg === undefined ? [id] : [id, arg]);
  };

  window.mallardDocument = {
    shellCall(call) {
      if (!call || !call.seq) return;
      const shell = window.mallardShell;
      const reply = (result, error) => trigger("doc_shell_reply", [call.seq, result ?? null, error || ""]);
      if (call.cmd === "openUrl" && !(shell && shell.openUrl)) {
        window.open(call.args[0], "_blank", "noopener");
        return;
      }
      if (!shell || typeof shell[call.cmd] !== "function") {
        reply(null, "the desktop shell has no " + call.cmd);
        return;
      }
      Promise.resolve(shell[call.cmd](...(call.args || []))).then((r) => reply(r), (e) => reply(null, String(e)));
    },
    navigate(url) {
      if (!url) return;
      leaving = true;
      window.location.assign(url);
    },
    // A diagnostic's line and column in the text tab, once its text field is drawn.
    gotoLine(at, tries = 0) {
      if (!at || !at.line) return;
      const field = document.querySelector('textarea[aria-label="input.toml"]');
      if (!field) {
        if (tries < 50) setTimeout(() => window.mallardDocument.gotoLine(at, tries + 1), 20);
        return;
      }
      const lines = field.value.split("\n");
      const n = Math.min(at.line, lines.length);
      let pos = 0;
      for (let i = 0; i < n - 1; i++) pos += lines[i].length + 1;
      pos += Math.min(Math.max((at.column || 1) - 1, 0), lines[n - 1].length);
      field.focus();
      field.setSelectionRange(pos, pos);
      const height = parseFloat(getComputedStyle(field).lineHeight) || 16;
      field.scrollTop = Math.max(0, (n - 1) * height - field.clientHeight / 3);
    },
    // The default button of a prompt, the name field of the picker: Enter and Escape act at once.
    focus(selector) {
      setTimeout(() => {
        const el = document.querySelector(selector);
        if (el) el.focus();
      }, 0);
    },
  };

  // A version 2 shell (an older app with this server) has a dialog for the document's three choices only.
  const legacyPick = { open_input: "pickOpen", save_input: "pickSave", new_project: "pickNewProject" };

  // The server asks for a path: the shell's native dialog whenever there is a shell, decided now rather than
  // when the page opened; without one, the server's picker of its own machine's files.
  window.mallardPaths = {
    choose(req) {
      if (!req || !req.seq) return;
      const shell = window.mallardShell;
      const legacy = shell && shell[legacyPick[req.kind]];
      const pending = !shell ? null
        : typeof shell.choosePath === "function" ? shell.choosePath(req)
        : typeof legacy === "function" ? legacy({ directory: req.directory, name: req.name })
        : null;
      if (!pending) {
        trigger("path_server", [req.seq]);
        return;
      }
      Promise.resolve(pending).then(
        (path) => trigger("path_reply", [req.seq, path ?? null, ""]),
        (e) => trigger("path_reply", [req.seq, null, String(e)]),
      );
    },
  };

  window.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      if ((get("doc_prompt") || {}).open) trigger("doc_answer", ["cancel"]);
      else if ((get("path_picker") || {}).open) window.trame.state.set("path_picker", { open: false });
      else if ((get("doc_about") || {}).open) window.trame.state.set("doc_about", {});
      return;
    }
    if (window.mallardShell || !(e.metaKey || e.ctrlKey) || e.altKey) return;
    const key = e.key.toLowerCase();
    let id = null;
    if (key === "s") id = e.shiftKey ? "save_as" : "save";
    else if (key === "o" && !e.shiftKey) id = "open";
    else if (key === "z" && !editable(document.activeElement)) id = e.shiftKey ? "redo" : "undo";
    else if (key === "y" && e.ctrlKey && !editable(document.activeElement)) id = "redo";
    else if (!e.shiftKey && PAGES[e.key]) id = "page_" + PAGES[e.key];
    if (id) {
      e.preventDefault();
      window.mallardMenu(id);
    }
  });

  window.addEventListener("beforeunload", (e) => {
    if (!leaving && !window.mallardShell && get("doc_edited") && !get("doc_closed")) {
      e.preventDefault();
      e.returnValue = "";
    }
  });
})();
