// Only setting worth exposing: where the engine is listening.
const field = document.getElementById("endpoint");
const status = document.getElementById("status");

chrome.storage.local.get("endpoint").then(({ endpoint }) => {
  field.value = endpoint || "ws://127.0.0.1:8787";
});

const refresh = async () => {
  const targets = await chrome.debugger.getTargets().catch(() => []);
  const count = targets.filter((t) => t.attached && t.type === "page").length;
  status.textContent = count ? `driving ${count} tab(s)` : "idle";
};
refresh();

document.getElementById("save").addEventListener("click", async () => {
  await chrome.storage.local.set({ endpoint: field.value.trim() });
  status.textContent = "saved — reconnecting";
  chrome.runtime.reload();
});
