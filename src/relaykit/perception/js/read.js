// Small reads and writes against the node registry the collector filled.
// One entry point with an `op` switch: every call is a round trip, and the
// registry has to be shared with the collector for handles to stay addressable.

(request) => {
__RELAYKIT_DEEP_DOM_HELPERS__

  const store = (window.__relaykit = window.__relaykit || {});
  const nodes = store.nodes || [];
  const node = (i) => nodes[i] || null;

  switch (request.op) {
    case 'viewport': {
      // maxScroll* lets a caller answer "can this page scroll further?" without
      // dispatching a wheel event to find out. That matters more than it looks:
      // a wheel at the scroll limit may never be acknowledged by the compositor,
      // and an unacknowledged input command blocks every later command on the
      // same debuggee behind it.
      const doc = document.documentElement;
      const body = document.body;
      const scrollWidth = Math.max(doc ? doc.scrollWidth : 0, body ? body.scrollWidth : 0);
      const scrollHeight = Math.max(doc ? doc.scrollHeight : 0, body ? body.scrollHeight : 0);
      return {
        width: window.innerWidth,
        height: window.innerHeight,
        scrollX: window.scrollX,
        scrollY: window.scrollY,
        dpr: window.devicePixelRatio || 1,
        maxScrollX: Math.max(0, scrollWidth - window.innerWidth),
        maxScrollY: Math.max(0, scrollHeight - window.innerHeight),
      };
    }

    case 'box': {
      const el = node(request.index);
      if (!el || !el.isConnected) return null;
      let rect;
      try {
        rect = deepDom.getTopViewportRect(el) || el.getBoundingClientRect();
      } catch (_) { return null; }
      // A click at a negative y is a click on nothing, so nudge it into view
      // before reporting where it is.
      if (rect.bottom < 0 || rect.top > window.innerHeight) {
        try { el.scrollIntoView({ block: 'center', inline: 'center' }); } catch (_) {}
        try {
          rect = deepDom.getTopViewportRect(el) || el.getBoundingClientRect();
        } catch (_) { return null; }
      }
      return {
        x: rect.x ?? rect.left, y: rect.y ?? rect.top,
        width: rect.width, height: rect.height,
      };
    }

    case 'value': {
      const el = node(request.index);
      if (!el) return null;
      if (el.isContentEditable) return el.textContent || '';
      return 'value' in el ? String(el.value ?? '') : (el.textContent || '');
    }

    case 'activeValue': {
      const el = document.activeElement;
      if (!el) return '';
      if (el.isContentEditable) return el.textContent || '';
      return 'value' in el ? String(el.value ?? '') : (el.textContent || '');
    }

    case 'focus': {
      const el = node(request.index);
      if (!el) return false;
      try { el.focus({ preventScroll: false }); } catch (_) { return false; }
      return document.activeElement === el;
    }

    case 'clear': {
      const el = node(request.index) || document.activeElement;
      if (!el) return false;
      try {
        if (el.isContentEditable) {
          el.textContent = '';
        } else {
          // Assigning .value directly is invisible to React and friends, which
          // track the native setter. Call it explicitly, then fire the events
          // the framework is listening for.
          const proto = el instanceof HTMLTextAreaElement
            ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
          const setter = Object.getOwnPropertyDescriptor(proto, 'value');
          if (setter && setter.set) { setter.set.call(el, ''); } else { el.value = ''; }
        }
        el.dispatchEvent(new Event('input', { bubbles: true }));
        el.dispatchEvent(new Event('change', { bubbles: true }));
      } catch (_) { return false; }
      return true;
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
      if (!chosen) {
        return {
          ok: false,
          reason: 'no matching option',
          available: Array.from(el.options).map((o) => o.textContent.trim()).slice(0, 20),
        };
      }
      el.value = chosen.value;
      el.dispatchEvent(new Event('input', { bubbles: true }));
      el.dispatchEvent(new Event('change', { bubbles: true }));
      return { ok: true, value: chosen.value, label: chosen.textContent.trim() };
    }

    case 'signature': {
      // Cheap structural fingerprint, used to decide whether an action changed
      // anything. Deliberately not a pixel hash: this survives caret blink and
      // animation, which a pixel hash does not.
      const body = document.body;
      if (!body) return '0';
      let text = '';
      try { text = body.innerText || ''; } catch (_) {}
      const active = document.activeElement;
      const focus = active
        ? active.tagName + ':' + (('value' in active ? active.value : '') ?? '')
        : '';
      return `${document.location.href}|${text.length}|${text.slice(0, 1500)}|${focus}`;
    }

    case 'scrollHeight':
      return document.documentElement.scrollHeight;

    default:
      return null;
  }
}
