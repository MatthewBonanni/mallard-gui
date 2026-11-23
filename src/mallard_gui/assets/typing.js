// What is being typed stays in its field. The page redraws as a whole whenever any state changes (a frame, a
// run's progress, an answer of the viewer), and a redraw sets every bound field back to its state's value: text
// typed into a field but not yet entered (Enter, Tab or a click elsewhere) was replaced by the old value, and
// then nothing was entered at all. While a field has the focus and holds text the user typed there, a value
// set by the page is kept aside; it is applied when the field is left unchanged (Escape), and dropped once the
// field's own value is entered.
(() => {
  const proto = HTMLInputElement.prototype;
  const value = Object.getOwnPropertyDescriptor(proto, "value");
  if (!value || !value.set) return;
  const typed = new WeakSet();
  const aside = new WeakMap();
  Object.defineProperty(proto, "value", {
    configurable: true,
    enumerable: value.enumerable,
    get() { return value.get.call(this); },
    set(v) {
      if (typed.has(this) && document.activeElement === this) aside.set(this, v);
      else value.set.call(this, v);
    },
  });
  const field = (ev) => (ev.target instanceof HTMLInputElement && !/^(checkbox|radio|range|file)$/.test(ev.target.type) ? ev.target : null);
  window.addEventListener("input", (ev) => { const el = field(ev); if (el && ev.isTrusted !== false) typed.add(el); }, true);
  const entered = (ev) => { const el = field(ev); if (el) { typed.delete(el); aside.delete(el); } };
  window.addEventListener("change", entered, true);
  window.addEventListener("blur", (ev) => {
    const el = field(ev);
    if (!el || !typed.has(el)) return;
    typed.delete(el); // left with no change event: nothing was entered, so the page's value shows again
    if (aside.has(el)) { value.set.call(el, aside.get(el)); aside.delete(el); }
  }, true);
})();
