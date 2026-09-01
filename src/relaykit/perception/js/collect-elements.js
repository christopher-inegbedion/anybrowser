// Collect interactive elements, across shadow roots and same-process iframes,
// in viewport CSS pixels.
//
// Perception quality is most of what decides how well an agent runs, and this
// is where it lives. Two rules here are not obvious and are both deliberate:
//
//   * A file input is kept even though it fails every visibility test. On real
//     upload flows it is display:none behind a styled button, and dropping it
//     makes uploads impossible on the sites people actually automate.
//   * Coordinates are mapped through the iframe chain. An element's own rect is
//     relative to its own document; the engine clicks in the top-level
//     viewport, and the difference is silent and wrong.

(options) => {
__RELAYKIT_DEEP_DOM_HELPERS__

  const opts = options || {};
  const limit = Number(opts.limit || 500);
  const store = (window.__relaykit = window.__relaykit || {});
  store.nodes = [];

  const view = {
    width: window.innerWidth,
    height: window.innerHeight,
    scrollX: window.scrollX,
    scrollY: window.scrollY,
    dpr: window.devicePixelRatio || 1,
  };

  const INTERACTIVE = [
    'a[href]', 'button', 'input', 'select', 'textarea', 'summary', 'label[for]',
    '[role=button]', '[role=link]', '[role=checkbox]', '[role=radio]',
    '[role=tab]', '[role=menuitem]', '[role=option]', '[role=switch]',
    '[role=combobox]', '[role=searchbox]', '[role=textbox]', '[role=slider]',
    '[contenteditable=""]', '[contenteditable="true"]',
    '[onclick]', '[tabindex]:not([tabindex="-1"])',
  ].join(',');

  const isFileInput = (el) =>
    el.tagName === 'INPUT' && (el.getAttribute('type') || '').toLowerCase() === 'file';

  const labelFor = (el) => {
    try {
      const aria = el.getAttribute('aria-label');
      if (aria && aria.trim()) return aria.trim();
      const labelledBy = el.getAttribute('aria-labelledby');
      if (labelledBy) {
        const target = document.getElementById(labelledBy);
        if (target && target.textContent.trim()) return target.textContent.trim();
      }
      if (el.labels && el.labels.length && el.labels[0].textContent.trim()) {
        return el.labels[0].textContent.trim();
      }
      const title = el.getAttribute('title');
      if (title && title.trim()) return title.trim();
      const alt = el.getAttribute('alt');
      if (alt && alt.trim()) return alt.trim();
      const text = (el.innerText || el.textContent || '').trim();
      if (text) return text.slice(0, 160);
      const placeholder = el.getAttribute('placeholder');
      if (placeholder) return placeholder.trim();
      const name = el.getAttribute('name');
      if (name) return name.trim();
    } catch (_) {}
    return '';
  };

  const visible = (el, rect) => {
    if (isFileInput(el)) return true;
    if (!rect || rect.width <= 0 || rect.height <= 0) return false;
    let style;
    try { style = getComputedStyle(el); } catch (_) { return false; }
    if (!style) return false;
    if (style.visibility === 'hidden' || style.display === 'none') return false;
    if (parseFloat(style.opacity || '1') < 0.05) return false;
    // Off-viewport is kept only if it is scrollable into view; fully detached
    // geometry (the classic -9999px hiding trick) is not.
    if (rect.right < -2000 || rect.bottom < -2000) return false;
    return true;
  };

  const attributesOf = (el) => {
    const out = {};
    try {
      for (const attr of el.attributes) {
        if (attr.name === 'style') continue;
        if (attr.name.startsWith('data-react')) continue;
        out[attr.name] = String(attr.value).slice(0, 240);
      }
    } catch (_) {}
    return out;
  };

  const elements = [];
  let roots;
  try {
    roots = deepDom.collectRoots ? deepDom.collectRoots() : [document];
  } catch (_) {
    roots = [document];
  }

  const seen = new Set();
  for (const root of roots) {
    let found;
    try {
      found = root.querySelectorAll(INTERACTIVE);
    } catch (_) {
      continue;
    }
    for (const el of found) {
      if (elements.length >= limit) break;
      if (seen.has(el)) continue;
      seen.add(el);

      let rect;
      try {
        rect = deepDom.getTopViewportRect(el) || el.getBoundingClientRect();
      } catch (_) {
        continue;
      }
      if (!visible(el, rect)) continue;

      const index = store.nodes.push(el) - 1;
      let value = '';
      try { value = 'value' in el ? String(el.value ?? '') : ''; } catch (_) {}

      elements.push({
        index,
        x: rect.x ?? rect.left,
        y: rect.y ?? rect.top,
        width: rect.width,
        height: rect.height,
        tag: el.tagName.toLowerCase(),
        role: el.getAttribute('role') || '',
        label: labelFor(el),
        value,
        placeholder: el.getAttribute('placeholder') || '',
        editable: !!el.isContentEditable ||
          ['INPUT', 'TEXTAREA', 'SELECT'].includes(el.tagName),
        disabled: !!el.disabled,
        frameId: '',
        attributes: attributesOf(el),
      });
    }
  }

  let text = '';
  if (opts.includeText) {
    try { text = document.body ? document.body.innerText.slice(0, 20000) : ''; } catch (_) {}
  }

  const signature = elements
    .map((e) => `${e.tag}:${Math.round(e.x)},${Math.round(e.y)}:${e.value}`)
    .join('|');

  return {
    viewport: view,
    elements,
    text,
    signature: String(signature.length) + ':' + signature.slice(0, 2000),
  };
}
