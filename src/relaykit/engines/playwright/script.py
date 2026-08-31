"""The JavaScript this engine evaluates in the page.

Kept as two functions with an ``op`` switch rather than a dozen snippets: every
call is one round trip, and the node registry (``window.__relaykit.nodes``) has
to be shared between them for handles to stay addressable.
"""

from __future__ import annotations

__all__ = ["COLLECT_JS", "READ_JS"]

#: Collect interactive elements and stash them in a registry the handles index
#: into. Perception quality lives here -- see docs/architecture/perception.md.
COLLECT_JS = r"""
(options) => {
  const store = (window.__relaykit = window.__relaykit || { nodes: [] });
  store.nodes = [];

  const INTERACTIVE = 'a[href], button, input, select, textarea, summary, ' +
    '[role=button], [role=link], [role=checkbox], [role=radio], [role=tab], ' +
    '[role=menuitem], [role=option], [role=switch], [contenteditable=""], ' +
    '[contenteditable="true"], [onclick], [tabindex]:not([tabindex="-1"])';

  const view = {
    width: window.innerWidth,
    height: window.innerHeight,
    scrollX: window.scrollX,
    scrollY: window.scrollY,
    dpr: window.devicePixelRatio || 1,
  };

  const label = (el) => {
    const aria = el.getAttribute('aria-label');
    if (aria) return aria.trim();
    if (el.labels && el.labels.length) return el.labels[0].textContent.trim();
    const title = el.getAttribute('title');
    if (title) return title.trim();
    const text = (el.innerText || el.textContent || '').trim();
    return text.slice(0, 120);
  };

  // A file input is almost always display:none behind a styled button. It is the
  // one element we keep despite failing every visibility test, because without
  // it uploads are impossible on the sites people actually use.
  const isFileInput = (el) =>
    el.tagName === 'INPUT' && (el.getAttribute('type') || '').toLowerCase() === 'file';

  const visible = (el, rect) => {
    if (isFileInput(el)) return true;
    if (rect.width <= 0 || rect.height <= 0) return false;
    const style = getComputedStyle(el);
    if (style.visibility === 'hidden' || style.display === 'none') return false;
    if (parseFloat(style.opacity || '1') < 0.05) return false;
    return rect.bottom > 0 && rect.right > 0 &&
           rect.top < view.height && rect.left < view.width;
  };

  const elements = [];
  for (const el of document.querySelectorAll(INTERACTIVE)) {
    const rect = el.getBoundingClientRect();
    if (!visible(el, rect)) continue;
    const attributes = {};
    for (const attr of el.attributes) {
      if (attr.name === 'style' || attr.name.startsWith('data-reactid')) continue;
      attributes[attr.name] = attr.value.slice(0, 200);
    }
    const index = store.nodes.push(el) - 1;
    elements.push({
      index,
      x: rect.x, y: rect.y, width: rect.width, height: rect.height,
      tag: el.tagName.toLowerCase(),
      role: el.getAttribute('role') || '',
      label: label(el),
      value: 'value' in el ? String(el.value ?? '') : '',
      placeholder: el.getAttribute('placeholder') || '',
      editable: el.isContentEditable ||
        ['INPUT', 'TEXTAREA', 'SELECT'].includes(el.tagName),
      disabled: !!el.disabled,
      attributes,
    });
  }

  const signature = elements.map((e) =>
    `${e.tag}:${Math.round(e.x)},${Math.round(e.y)}:${e.value}`).join('|');

  return {
    viewport: view,
    elements,
    text: options && options.includeText
      ? (document.body ? document.body.innerText.slice(0, 20000) : '')
      : '',
    signature: String(signature.length) + ':' + signature.slice(0, 2000),
  };
}
"""

#: Small reads and writes against the registry the collector filled.
READ_JS = r"""
(request) => {
  const store = (window.__relaykit = window.__relaykit || { nodes: [] });
  const node = (i) => store.nodes[i] || null;

  switch (request.op) {
    case 'viewport':
      return {
        width: window.innerWidth,
        height: window.innerHeight,
        scrollX: window.scrollX,
        scrollY: window.scrollY,
        dpr: window.devicePixelRatio || 1,
      };

    case 'box': {
      const el = node(request.index);
      if (!el || !el.isConnected) return null;
      const rect = el.getBoundingClientRect();
      // Nudge it into view first: a click at a negative y is a click on nothing.
      if (rect.bottom < 0 || rect.top > window.innerHeight) {
        el.scrollIntoView({ block: 'center', inline: 'center' });
      }
      const after = el.getBoundingClientRect();
      return { x: after.x, y: after.y, width: after.width, height: after.height };
    }

    case 'activeValue': {
      const el = document.activeElement;
      if (!el) return '';
      if (el.isContentEditable) return el.textContent || '';
      return 'value' in el ? String(el.value ?? '') : (el.textContent || '');
    }

    case 'select': {
      const el = node(request.index);
      if (!el || el.tagName !== 'SELECT') return { ok: false, reason: 'not a select' };
      let chosen = null;
      for (let i = 0; i < el.options.length; i += 1) {
        const opt = el.options[i];
        const byValue = request.value && opt.value === request.value;
        const byLabel = request.label &&
          opt.textContent.trim().toLowerCase() === String(request.label).trim().toLowerCase();
        const byIndex = request.optionIndex >= 0 && i === request.optionIndex;
        if (byValue || byLabel || byIndex) { chosen = opt; break; }
      }
      if (!chosen) return { ok: false, reason: 'no matching option' };
      el.value = chosen.value;
      el.dispatchEvent(new Event('input', { bubbles: true }));
      el.dispatchEvent(new Event('change', { bubbles: true }));
      return { ok: true, value: chosen.value, label: chosen.textContent.trim() };
    }

    case 'signature': {
      const body = document.body;
      if (!body) return '0';
      const text = body.innerText || '';
      const focus = document.activeElement
        ? document.activeElement.tagName + (document.activeElement.value ?? '')
        : '';
      return `${document.location.href}|${text.length}|${text.slice(0, 1500)}|${focus}`;
    }

    default:
      return null;
  }
}
"""
