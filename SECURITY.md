# Security Policy

## Reporting a vulnerability

**Do not open a public issue.** Report privately through
[GitHub Security Advisories](https://github.com/christopher-inegbedion/relaykit/security/advisories/new),
which needs no email address and keeps the report private until a fix ships.

Include what you can: affected version, the engine or transport involved, a
reproduction, and what an attacker gains. We aim to acknowledge within 3 working
days and to ship a fix or a mitigation within 30 days for anything we rate high
or critical. We will credit you in the advisory unless you'd rather we didn't.

## Supported versions

Pre-1.0: the latest minor release only. After 1.0 this table gets real.

## What is in scope

RelayKit drives a browser that is often *the user's own*, holding their logged-in
sessions. That makes a few things security-relevant that would be ordinary bugs
elsewhere:

- **Daemon authentication.** The daemon exposes full control of a browser. An
  unauthenticated path to it, a transport that accepts a connection it should
  have rejected, or a token that leaks into a log is a vulnerability.
- **Sandbox and confirmation bypass.** Anything that lets an action run without
  the confirmation policy it was gated behind.
- **Credential exposure.** Cookies, tokens or page content reaching a log, a
  crash report, a model prompt, or an artifact that outlives the session, when
  the configuration said they should not.
- **Prompt injection with real consequences.** Page content is untrusted input.
  A page that can make the agent exfiltrate data or take a destructive action
  it was not authorised to take is in scope.
- **Engine plugin loading.** A path where a hostile entry point or config value
  gets code executed without the user installing it.

## What is not in scope

- The browser's own vulnerabilities. Report those to the browser vendor.
- An agent doing something unhelpful, wrong or expensive. That is a bug.
- A model producing bad output. That is a model problem.
- Anything requiring an attacker to already have local code execution as the
  user — at that point they can drive the browser directly.

## For people running RelayKit

The daemon should never be bound to a public interface. Default to the Unix
socket transport; if you use WebSocket, bind loopback and set a token. Treat
every page as hostile input, because it is.
