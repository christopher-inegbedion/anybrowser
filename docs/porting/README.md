# Porting

The Chrome and Safari engines are being lifted out of
[Relay](https://relaythis.com)'s daemon, where they ran in production, onto the
`BrowserEngine` interface. This directory tracks what that involves.

The order is deliberate: **the interface and the conformance suite were written
first**, so the port is graded by a suite that predates it rather than described
by whatever got built.

Until an engine is done its `probe()` refuses with `EngineNotAvailable`. That is
not a placeholder for politeness — it means the registry falls through to
another engine instead of failing three actions into someone's run.

- [Chrome](chrome.md) — the DevTools pipe is done and passes conformance; the
  extension-owned pipe, which is the one that reaches the user's own window, is
  outstanding.
- [Safari](safari.md) — the native half (trusted background input, occlusion-proof
  capture) is done and verified against a real Safari; perception needs the Web
  Extension.

Both carry hard-won details that are invisible in the source and expensive to
rediscover. They are written down here so the port does not lose them.
