// RelayKit's Chrome bridge.
//
// The engine cannot attach to a browser the user started themselves: CDP over
// the DevTools port needs --remote-debugging-port present from launch, and by
// then it is too late. An extension can, because `chrome.debugger` is a
// debugger session the *browser* hands out.
//
// So this is a relay, and deliberately nothing more. It holds no policy and
// makes no decisions: it opens a WebSocket to a RelayKit engine, forwards CDP
// commands to chrome.debugger, and forwards events back. Everything about what
// to do with a page lives in Python.
//
//   engine -> here   {"id":"7","type":"cdp","tabId":42,"method":"Page.navigate",
//                     "params":{...},"sessionId":""}
//   here -> engine   {"id":"7","ok":true,"result":{...}}
//   here -> engine   {"type":"event","method":"Page.loadEventFired",
//                     "params":{},"tabId":42,"sessionId":"..."}
//
// Two Chrome facts shape everything below, and both are easy to learn the hard
// way:
//
//   * A Manifest V3 service worker is terminated whenever Chrome feels like it.
//     In-memory state does not survive. `chrome.debugger` attachments DO. So on
//     every wake we reconcile what we think we have against what Chrome says we
//     have, or we end up attached to tabs we have forgotten and unable to
//     re-attach to them.
//   * Only one debugger client may attach to a tab at a time. If DevTools is
//     open on a tab, attaching fails — and the honest thing is to say so rather
//     than retry forever.

const DEFAULT_ENDPOINT = "ws://127.0.0.1:8787";
const PROTOCOL_VERSION = 1;
const RECONNECT_MIN_MS = 500;
const RECONNECT_MAX_MS = 15000;

let socket = null;
let reconnectDelay = RECONNECT_MIN_MS;
let attached = new Set();

// --------------------------------------------------------------------------
// Small helpers
// --------------------------------------------------------------------------

const callChrome = (fn) =>
  new Promise((resolve, reject) => {
    try {
      fn((result) => {
        const error = chrome.runtime.lastError;
        if (error) reject(new Error(error.message));
        else resolve(result);
      });
    } catch (err) {
      reject(err);
    }
  });

const send = (message) => {
  if (socket && socket.readyState === WebSocket.OPEN) {
    socket.send(JSON.stringify(message));
  }
};

const endpoint = async () => {
  const stored = await chrome.storage.local.get("endpoint");
  return stored.endpoint || DEFAULT_ENDPOINT;
};

// --------------------------------------------------------------------------
// Debugger attachment
// --------------------------------------------------------------------------

// The service worker's Set is a cache of Chrome's truth, not the truth itself.
// After a termination the two disagree, and every command on a "forgotten" tab
// fails with "Debugger is already attached" until they are reconciled.
const reconcileAttachments = async () => {
  let targets = [];
  try {
    targets = await callChrome((done) => chrome.debugger.getTargets(done));
  } catch {
    targets = [];
  }
  attached = new Set(
    targets.filter((t) => t && t.attached && t.type === "page" && t.tabId > 0).map((t) => t.tabId)
  );
};

const attach = async (tabId) => {
  if (attached.has(tabId)) return;
  try {
    await callChrome((done) => chrome.debugger.attach({ tabId }, "1.3", done));
    attached.add(tabId);
  } catch (err) {
    const message = String(err && err.message);
    if (message.includes("already attached")) {
      // Either us before a restart, or DevTools. If it is us, recording it is
      // enough; if it is DevTools, the next command fails with a clear message,
      // which is better than silently pretending to be attached.
      attached.add(tabId);
      return;
    }
    throw err;
  }
  await chrome.debugger
    .sendCommand({ tabId }, "Page.enable", {})
    .catch(() => null);
  await chrome.debugger
    .sendCommand({ tabId }, "Runtime.enable", {})
    .catch(() => null);
};

const detach = async (tabId) => {
  if (!attached.has(tabId)) return;
  attached.delete(tabId);
  await callChrome((done) => chrome.debugger.detach({ tabId }, done)).catch(() => null);
};

// --------------------------------------------------------------------------
// Requests from the engine
// --------------------------------------------------------------------------

const listTabs = async () => {
  const tabs = await callChrome((done) => chrome.tabs.query({}, done));
  return tabs.map((tab) => ({
    tab_id: String(tab.id),
    url: tab.url || "",
    title: tab.title || "",
    active: !!tab.active,
    window_id: String(tab.windowId),
    attached: attached.has(tab.id),
  }));
};

const handleRequest = async (message) => {
  const { type } = message;

  if (type === "cdp") {
    const tabId = Number(message.tabId);
    if (!Number.isFinite(tabId) || tabId <= 0) throw new Error("cdp needs a tabId");
    await attach(tabId);
    const target = message.sessionId
      ? { tabId, sessionId: message.sessionId }
      : { tabId };
    const result = await callChrome((done) =>
      chrome.debugger.sendCommand(target, message.method, message.params || {}, done)
    );
    return result === undefined ? {} : result;
  }

  if (type === "tabs") return { tabs: await listTabs() };

  if (type === "attach") {
    await attach(Number(message.tabId));
    return { attached: true };
  }

  if (type === "detach") {
    await detach(Number(message.tabId));
    return { attached: false };
  }

  if (type === "create_tab") {
    const tab = await callChrome((done) =>
      chrome.tabs.create({ url: message.url || "about:blank", active: true }, done)
    );
    return { tab_id: String(tab.id) };
  }

  if (type === "close_tab") {
    await callChrome((done) => chrome.tabs.remove(Number(message.tabId), done));
    return {};
  }

  if (type === "activate_tab") {
    const tabId = Number(message.tabId);
    const tab = await callChrome((done) => chrome.tabs.update(tabId, { active: true }, done));
    await callChrome((done) => chrome.windows.update(tab.windowId, { focused: true }, done)).catch(
      () => null
    );
    return { tab_id: String(tabId) };
  }

  if (type === "ping") return { ok: true };

  throw new Error(`unknown request type: ${type}`);
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
    await reconcileAttachments();
    const info = await chrome.runtime.getPlatformInfo().catch(() => ({}));
    send({
      type: "hello",
      protocol_version: PROTOCOL_VERSION,
      browser: navigator.userAgent,
      platform: info.os || "",
      attached: Array.from(attached).map(String),
    });
  };

  socket.onmessage = async (event) => {
    let message;
    try {
      message = JSON.parse(event.data);
    } catch {
      return; // a frame we cannot parse is not ours to answer
    }
    if (!message || !message.id) return;
    try {
      const result = await handleRequest(message);
      send({ id: message.id, ok: true, result });
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
  // Backoff, because the engine is usually simply not running yet and a tight
  // retry loop would keep the service worker alive doing nothing.
  reconnectDelay = Math.min(reconnectDelay * 2, RECONNECT_MAX_MS);
};

// --------------------------------------------------------------------------
// Events out
// --------------------------------------------------------------------------

chrome.debugger.onEvent.addListener((source, method, params) => {
  send({
    type: "event",
    method,
    params: params || {},
    tabId: source.tabId ? String(source.tabId) : "",
    sessionId: source.sessionId || "",
  });
});

chrome.debugger.onDetach.addListener((source, reason) => {
  if (source.tabId) attached.delete(source.tabId);
  send({ type: "detached", tabId: String(source.tabId || ""), reason });
});

chrome.tabs.onRemoved.addListener((tabId) => {
  attached.delete(tabId);
  send({ type: "tab_removed", tabId: String(tabId) });
});

// A terminated service worker takes the socket with it. The alarm wakes us so
// the engine is not left waiting on a bridge that quietly went away.
chrome.alarms.create("relaykit-keepalive", { periodInMinutes: 0.5 });
chrome.alarms.onAlarm.addListener(() => {
  if (!socket || socket.readyState > WebSocket.OPEN) connect();
});

chrome.runtime.onStartup.addListener(connect);
chrome.runtime.onInstalled.addListener(connect);
connect();
