// AnyBrowser's Safari bridge — the background half.
//
// Safari exposes no CDP: the Web Inspector protocol needs private Apple
// entitlements. So the Safari engine is split. The native helper
// (anybrowser.engines.safari) owns the things only the system can do — trusted
// clicks through the accessibility tree, screenshots of an occluded window,
// native dialogs. This extension owns everything reachable from inside a page:
// the DOM, element geometry, and synthetic pointer gestures.
//
// This file is only a router. It holds the socket to the engine and forwards
// requests to the content script in the target tab.
//
//   engine -> here    {"id":"7","type":"perceive","tabId":"3","includeText":true}
//   here -> engine    {"id":"7","ok":true,"result":{...}}

const DEFAULT_ENDPOINT = "ws://127.0.0.1:8788";
const PROTOCOL_VERSION = 1;
const RECONNECT_MIN_MS = 500;
const RECONNECT_MAX_MS = 15000;

let socket = null;
let reconnectDelay = RECONNECT_MIN_MS;

const send = (message) => {
  if (socket && socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify(message));
};

const endpoint = async () => {
  try {
    const stored = await browser.storage.local.get("endpoint");
    return (stored && stored.endpoint) || DEFAULT_ENDPOINT;
  } catch {
    return DEFAULT_ENDPOINT;
  }
};

// --------------------------------------------------------------------------
// Talking to the page
// --------------------------------------------------------------------------

const activeTabId = async () => {
  const tabs = await browser.tabs.query({ active: true, currentWindow: true });
  if (!tabs.length) throw new Error("no active tab");
  return tabs[0].id;
};

const askTab = async (tabId, payload) => {
  const id = tabId ? Number(tabId) : await activeTabId();
  // Safari resolves a Promise returned from onMessage. Anything else non-undefined
  // is read as the answer itself, so the content script must never use the
  // `return true` + sendResponse idiom -- with several listeners in one world the
  // first to return anything settles the caller, usually with undefined.
  const reply = await browser.tabs.sendMessage(id, payload);
  if (reply && reply.error) throw new Error(reply.error);
  return reply;
};

const listTabs = async () => {
  const tabs = await browser.tabs.query({});
  return tabs.map((tab) => ({
    tab_id: String(tab.id),
    url: tab.url || "",
    title: tab.title || "",
    active: !!tab.active,
    window_id: String(tab.windowId),
    attached: true,
  }));
};

const handleRequest = async (message) => {
  switch (message.type) {
    case "ping":
      return { ok: true };
    case "tabs":
      return { tabs: await listTabs() };
    case "perceive":
      return await askTab(message.tabId, {
        kind: "perceive",
        includeText: message.includeText !== false,
      });
    case "read":
      return await askTab(message.tabId, {
        kind: "read",
        op: message.op,
        args: message.args || {},
      });
    case "pointer":
      return await askTab(message.tabId, { kind: "pointer", events: message.events || [] });
    case "evaluate":
      return await askTab(message.tabId, { kind: "evaluate", script: message.script });
    case "navigate": {
      const id = message.tabId ? Number(message.tabId) : await activeTabId();
      await browser.tabs.update(id, { url: message.url });
      return {};
    }
    case "activate_tab": {
      const id = Number(message.tabId);
      await browser.tabs.update(id, { active: true });
      return { tab_id: String(id) };
    }
    case "close_tab":
      await browser.tabs.remove(Number(message.tabId));
      return {};
    case "create_tab": {
      const tab = await browser.tabs.create({ url: message.url || "about:blank" });
      return { tab_id: String(tab.id) };
    }
    default:
      throw new Error(`unknown request type: ${message.type}`);
  }
};

// --------------------------------------------------------------------------
// Connection
// --------------------------------------------------------------------------

const connect = async () => {
  const url = await endpoint();
  try {
    socket = new WebSocket(url);
  } catch {
    scheduleReconnect();
    return;
  }

  socket.onopen = async () => {
    reconnectDelay = RECONNECT_MIN_MS;
    // Report what this extension can actually see, not just that it is here.
    // Being enabled is not the same as being permitted: Safari injects no
    // content script until a site is granted, so the bridge can connect and
    // still be blind. Saying so in the greeting turns that from a mystery --
    // perception returning nothing, for no stated reason -- into a fact the
    // engine can act on.
    let granted = null;
    try {
      granted = await browser.permissions.getAll();
    } catch {
      // Older Safari, or the API is unavailable. Unknown, not empty.
    }
    // Whether a content script can actually be reached, which is the thing
    // that matters. permissions.getAll() reports what the extension *asked
    // for*, not what any site has granted, so it says "<all_urls>" even when
    // Safari is injecting nothing anywhere.
    let reach = "unknown";
    try {
      const [tab] = await browser.tabs.query({ active: true, currentWindow: true });
      if (!tab) {
        reach = "no active tab";
      } else {
        const reply = await browser.tabs.sendMessage(tab.id, { kind: "ping" });
        reach = reply === undefined ? "no content script in the active tab" : "ok";
      }
    } catch (err) {
      reach = `content script unreachable: ${String((err && err.message) || err)}`;
    }
    send({
      type: "hello",
      protocol_version: PROTOCOL_VERSION,
      browser: navigator.userAgent,
      origins: granted ? granted.origins || [] : null,
      permissions: granted ? granted.permissions || [] : null,
      content_script: reach,
    });
    showState();
  };

  socket.onmessage = async (event) => {
    let message;
    try {
      message = JSON.parse(event.data);
    } catch {
      return;
    }
    if (!message || !message.id) return;
    try {
      send({ id: message.id, ok: true, result: await handleRequest(message) });
    } catch (err) {
      send({ id: message.id, ok: false, error: String((err && err.message) || err) });
    }
  };

  socket.onclose = () => {
    socket = null;
    showState();
    scheduleReconnect();
  };
  socket.onerror = () => {
    // onclose always follows; reconnecting from both would double the backoff.
  };
};

const scheduleReconnect = () => {
  setTimeout(connect, reconnectDelay);
  reconnectDelay = Math.min(reconnectDelay * 2, RECONNECT_MAX_MS);
};

connect();


// --------------------------------------------------------------------------
// The toolbar button
// --------------------------------------------------------------------------
//
// Clicking it is how a person brings the bridge up, and it works because the
// *browser* dispatches this event: Safari has to load this page to deliver it,
// and loading it already ran connect() above.
//
// Two designs that do not work, both measured rather than guessed:
//
//   * A `default_popup`. The popup is its own page, and a message from it to an
//     unloaded background page has no receiver -- Safari will not start this
//     page to deliver one, so the popup reports a bridge that is not there.
//     Declaring a popup is also actively harmful: Safari then shows it INSTEAD
//     of its own per-site permission menu, so the button can no longer be used
//     to grant the extension access to a site.
//   * Waiting for a content script to announce itself. Safari injects no
//     content script until the site is permitted, so injection cannot be the
//     thing that gets the bridge going on a fresh install. It is a useful
//     second path once permission exists, and nothing more.

const showState = async () => {
  const url = await endpoint();
  const live = !!socket && socket.readyState === WebSocket.OPEN;
  try {
    await browser.action.setBadgeText({ text: live ? "on" : "" });
    await browser.action.setTitle({
      title: live
        ? `AnyBrowser Bridge \u2014 connected to ${url}`
        : `AnyBrowser Bridge \u2014 waiting for the engine at ${url}`,
    });
  } catch {
    // The badge is cosmetic. Never let it fail a connection.
  }
};

browser.action.onClicked.addListener(() => {
  if (socket && socket.readyState === WebSocket.OPEN) {
    showState();
    return;
  }
  // This click is the wake. Dial now rather than waiting out the backoff.
  reconnectDelay = RECONNECT_MIN_MS;
  connect();
});

