// RelayKit's Safari bridge — the background half.
//
// Safari exposes no CDP: the Web Inspector protocol needs private Apple
// entitlements. So the Safari engine is split. The native helper
// (relaykit.engines.safari) owns the things only the system can do — trusted
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

  socket.onopen = () => {
    reconnectDelay = RECONNECT_MIN_MS;
    send({ type: "hello", protocol_version: PROTOCOL_VERSION, browser: navigator.userAgent });
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
