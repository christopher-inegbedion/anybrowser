// RelayKit's Safari bridge — the page half.
//
// Runs the same perception scripts the Chrome engine evaluates over CDP, so both
// browsers see a page identically. It also owns synthetic pointer gestures:
// drag, draw and canvas work, which the accessibility layer cannot express.
//
// What it deliberately does NOT do is click. A synthetic click carries no user
// activation, so anything gated on it -- window.open, clipboard, media, file
// pickers -- silently does nothing while reporting success. Those go through the
// native helper's AXPress instead. See docs/porting/safari.md.

(() => {
  // Safari injects content scripts more than once on some navigations; a second
  // set of listeners would answer the same message twice and the caller would
  // take whichever arrived first.
  if (window.__relaykitContentReady) return;
  window.__relaykitContentReady = true;

  const dispatchPointer = (events) => {
    let dispatched = 0;
    for (const spec of events) {
      const { type, x, y, button = 0, buttons = 0 } = spec;
      const init = {
        bubbles: true,
        cancelable: true,
        composed: true,
        clientX: x,
        clientY: y,
        button,
        // `buttons` is the pressed-button state, and it is the whole game for
        // drag: a move that omits it looks perfect from here and does nothing
        // on any page that checks it, which is most of them and all HTML5 DnD.
        buttons,
        pointerId: 1,
        pointerType: "mouse",
        isPrimary: true,
        view: window,
      };
      const target = document.elementFromPoint(x, y) || document.body;
      if (!target) continue;
      target.dispatchEvent(new PointerEvent(type, init));
      // Pages listen for one or the other, rarely both. Mirroring the pointer
      // event as a mouse event costs nothing and doubles what works.
      const mouseType = { pointerdown: "mousedown", pointerup: "mouseup", pointermove: "mousemove" }[
        type
      ];
      if (mouseType) target.dispatchEvent(new MouseEvent(mouseType, init));
      dispatched += 1;
    }
    return { dispatched };
  };

  const handle = (message) => {
    switch (message.kind) {
      case "perceive":
        return window.__relaykitPerception.collect({ includeText: message.includeText });
      case "read":
        return window.__relaykitPerception.read({ op: message.op, ...(message.args || {}) });
      case "pointer":
        return dispatchPointer(message.events || []);
      case "evaluate":
        // Content scripts run in an isolated world, so this sees the DOM but
        // not the page's own globals. That is the right trade: reaching the
        // page world needs a script tag, which many sites' CSP refuses.
        return { value: window.eval(message.script) };
      default:
        return { error: `unknown kind: ${message.kind}` };
    }
  };

  // Safari waits on a Promise returned from onMessage. `return true` plus
  // sendResponse -- the Chrome idiom -- does not work here: any non-undefined
  // return is taken as the answer, so with several listeners the first to
  // return anything settles the caller, usually with undefined.
  browser.runtime.onMessage.addListener((message) => {
    try {
      return Promise.resolve(handle(message));
    } catch (err) {
      return Promise.resolve({ error: String((err && err.message) || err) });
    }
  });
})();
