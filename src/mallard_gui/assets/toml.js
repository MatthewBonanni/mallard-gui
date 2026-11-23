// The input.toml text tab: TOML syntax colors under the plain textarea (which keeps editing, the caret, undo and
// the server's text as they are), a case-tree node's lines scrolled to and highlighted for a moment, and the caret's
// line selecting its node. window.mallardToml.tokenize is the tokenizer alone, for the tests.
(() => {
  const FIELD = 'textarea[aria-label="input.toml"]';
  const DATE = /\d{4}-\d{2}-\d{2}(?:[Tt ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:[Zz]|[+-]\d{2}:\d{2})?)?|\d{2}:\d{2}:\d{2}(?:\.\d+)?/y;
  const NUMBER =
    /[+-]?(?:inf|nan)|0x[0-9A-Fa-f](?:_?[0-9A-Fa-f])*|0o[0-7](?:_?[0-7])*|0b[01](?:_?[01])*|[+-]?\d(?:_?\d)*(?:\.\d(?:_?\d)*)?(?:[eE][+-]?\d(?:_?\d)*)?/y;
  const BOOL = /true|false/y;
  const BARE = /[A-Za-z0-9_-]+/y;
  const SPACE = /[ \t\r]+/y;
  const DOT = /[ \t]*\.[ \t]*/y;
  const WORD = /[^\s,\]}#=]+/y;

  // [class, text] pairs covering `src`: tk-comment, tk-table, tk-key, tk-string, tk-number, tk-bool, tk-date,
  // tk-punct, or "" for spaces and what is not TOML.
  function tokenize(src) {
    const out = [];
    const stack = [];
    const n = src.length;
    let i = 0;
    let expect = "key";
    const emit = (cls, end) => {
      if (end > i) out.push([cls, src.slice(i, end)]);
      i = Math.max(end, i);
    };
    const match = (re, at = i) => {
      re.lastIndex = at;
      const m = re.exec(src);
      return m ? at + m[0].length : -1;
    };
    const ends = (j) => j >= n || " \t\r\n,]}#".includes(src[j]);
    const stringEnd = (j) => {
      const q = src[j];
      const triple = q.repeat(3);
      if (src.startsWith(triple, j)) {
        for (let k = j + 3; ; ) {
          const e = src.indexOf(triple, k);
          if (e < 0) return n;
          let slashes = 0;
          while (q === '"' && src[e - 1 - slashes] === "\\") slashes++;
          if (slashes % 2) {
            k = e + 1;
            continue;
          }
          let end = e + 3;
          while (end < n && end < e + 5 && src[end] === q) end++;
          return end;
        }
      }
      let k = j + 1;
      while (k < n && src[k] !== "\n") {
        if (q === '"' && src[k] === "\\") k += 2;
        else if (src[k] === q) return k + 1;
        else k++;
      }
      return Math.min(k, n);
    };
    const key = () => {
      const start = i;
      for (;;) {
        const c = src[i];
        const e = c === '"' || c === "'" ? stringEnd(i) : match(BARE);
        if (e <= i) break;
        emit("tk-key", e);
        const d = match(DOT);
        if (d < 0) return true;
        const after = src[d];
        if (!(after === '"' || after === "'" || /[A-Za-z0-9_-]/.test(after || ""))) return true;
        const dot = src.indexOf(".", i);
        emit("", dot);
        emit("tk-punct", dot + 1);
        emit("", d);
      }
      return i > start;
    };
    while (i < n) {
      const c = src[i];
      const top = stack[stack.length - 1];
      if (c === "\n") {
        emit("", i + 1);
        if (!stack.length) expect = "key";
        continue;
      }
      if (c === " " || c === "\t" || c === "\r") {
        emit("", match(SPACE));
        continue;
      }
      if (c === "#") {
        const e = src.indexOf("\n", i);
        emit("tk-comment", e < 0 ? n : e);
        continue;
      }
      if (expect === "key") {
        if (c === "[" && !stack.length) {
          const double = src[i + 1] === "[";
          let k = i + (double ? 2 : 1);
          while (k < n && src[k] !== "\n") {
            if (src[k] === '"' || src[k] === "'") k = stringEnd(k);
            else if (src[k] === "]") {
              k += double && src[k + 1] === "]" ? 2 : 1;
              break;
            } else k++;
          }
          emit("tk-table", k);
          expect = "after";
          continue;
        }
        if (key()) {
          expect = "eq";
          continue;
        }
      }
      if (c === "=") {
        emit("tk-punct", i + 1);
        expect = "value";
        continue;
      }
      if (expect === "value") {
        if (c === '"' || c === "'") {
          emit("tk-string", stringEnd(i));
          expect = "after";
          continue;
        }
        if (c === "[" || c === "{") {
          stack.push(c);
          emit("tk-punct", i + 1);
          expect = c === "{" ? "key" : "value";
          continue;
        }
        const scalar = [[DATE, "tk-date"], [BOOL, "tk-bool"], [NUMBER, "tk-number"]].find(([re]) => {
          const e = match(re);
          return e > i && ends(e);
        });
        if (scalar) {
          emit(scalar[1], match(scalar[0]));
          expect = "after";
          continue;
        }
      }
      if ((c === "]" && top === "[") || (c === "}" && top === "{")) {
        stack.pop();
        emit("tk-punct", i + 1);
        expect = "after";
        continue;
      }
      if (c === "," && stack.length) {
        emit("tk-punct", i + 1);
        expect = top === "{" ? "key" : "value";
        continue;
      }
      const e = match(WORD);
      emit("", e > i ? e : i + 1);
    }
    return out;
  }

  const escape = (t) => t.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  const html = (tokens) =>
    tokens.map(([cls, text]) => (cls ? `<span class="${cls}">${escape(text)}</span>` : escape(text))).join("");

  let pending = null; // a reveal the text tab was not shown for: applied once it is
  let lastLine = 0;
  let cursorTimer = 0;

  function metrics(field) {
    const style = getComputedStyle(field);
    return { line: parseFloat(style.lineHeight) || 18, pad: parseFloat(style.paddingTop) || 0 };
  }

  function apply(field) {
    const r = pending;
    if (!r || !field.clientHeight) return;
    pending = null;
    const { line, pad } = metrics(field);
    const [first, last] = r.spans[0];
    const top = pad + (first - 1) * line;
    const bottom = pad + last * line;
    if (top < field.scrollTop || bottom > field.scrollTop + field.clientHeight) {
      const room = field.clientHeight - (bottom - top);
      field.scrollTop = Math.max(0, top - (room > 0 ? Math.min(room / 2, field.clientHeight / 4) : line));
    }
    const marks = field.parentElement.querySelector(".toml-marks");
    if (marks) {
      const width = Math.max(field.scrollWidth, field.clientWidth);
      marks.innerHTML = r.spans
        .map(([a, b]) => `<div class="toml-mark" data-first="${a}" data-last="${b}" ` +
          `style="top:${pad + (a - 1) * line}px;height:${(b - a + 1) * line}px;width:${width}px"></div>`)
        .join("");
      marks.querySelectorAll(".toml-mark").forEach((m) => m.addEventListener("animationend", () => m.remove()));
    }
    field.dispatchEvent(new Event("scroll"));
  }

  function enhance(field) {
    if (field.__mallardToml) return;
    field.__mallardToml = true;
    const under = field.parentElement.querySelector(".toml-under");
    if (!under) return;
    under.innerHTML = '<pre class="toml-code"><div class="toml-marks"></div><code></code></pre>';
    const pre = under.firstChild;
    const code = pre.querySelector("code");
    const sync = () => {
      pre.scrollTop = field.scrollTop;
      pre.scrollLeft = field.scrollLeft;
    };
    const paint = () => {
      code.innerHTML = html(tokenize(field.value)) + "\n";
      sync();
    };
    // The server's text (a reload, Revert, an edit elsewhere) is set as the value: colored at once, before paint.
    const own = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value");
    Object.defineProperty(field, "value", {
      configurable: true,
      get() {
        return own.get.call(this);
      },
      set(v) {
        own.set.call(this, v);
        paint();
      },
    });
    field.addEventListener("input", paint);
    field.addEventListener("scroll", sync);
    const cursor = () => {
      clearTimeout(cursorTimer);
      cursorTimer = setTimeout(() => {
        const at = field.value.slice(0, field.selectionStart).split("\n").length;
        if (at !== lastLine && window.trame) window.trame.trigger("toml_cursor", [at]);
        lastLine = at;
      }, 120);
    };
    field.addEventListener("click", cursor);
    field.addEventListener("keyup", (e) => {
      if (e.key.startsWith("Arrow") || ["PageUp", "PageDown", "Home", "End"].includes(e.key)) cursor();
    });
    // Shown again (the tab, the page): a reveal made while hidden is applied before the first paint.
    new ResizeObserver(() => apply(field)).observe(field);
    lastLine = 0;
    paint();
    apply(field);
  }

  // The tab's textarea is made anew each time it is shown: dressed as it is inserted, before it is painted.
  new MutationObserver(() => {
    const field = document.querySelector(FIELD);
    if (field && !field.__mallardToml) enhance(field);
  }).observe(document.documentElement, { childList: true, subtree: true });

  window.mallardToml = {
    tokenize,
    html,
    reveal(r) {
      if (!r || !r.spans || !r.spans.length) return;
      pending = r;
      const field = document.querySelector(FIELD);
      if (field) {
        enhance(field);
        apply(field);
      }
    },
  };
})();
