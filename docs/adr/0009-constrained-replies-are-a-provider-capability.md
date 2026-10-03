# ADR-0009: Constrained replies are a provider capability

**Status:** Accepted · **Date:** 2026-10-03

## Context

The planner asks for one JSON object and parses leniently. A capable model
mostly complies. Small models, the ones this project now targets, do not. In
measured failures:

- gemma3:1b copied the tool's argument *schema* into its arguments
  (`{"handle": {"type": "string", ...}}`) on every attempt.
- qwen3:4b-instruct "clicked" handle `13:12` on a page with no elements.
- qwen2.5vl:3b wrote the whole plan as several JSON objects in one reply, sent
  `x: [999, 284]` three times in a row, and used "done" to mean "this step is
  done" with an action as the answer.

None of these are judgment failures. They are the reply format failing, and
roughly half of qwen2.5vl's first image-only score (−0.200) came from them.
Every training round spent teaching a model the format is a round not spent on
judgment.

Most local and hosted servers can enforce a JSON schema while decoding. Ollama,
vLLM and OpenRouter accept OpenAI's `response_format`. A constrained model
cannot emit two objects, a list where an integer goes, or a tool that does not
exist.

## Decision

**`ModelProvider.structured_output(schema)`**, not abstract, defaulting to
`None`. It means "this provider cannot", and the planner then prompts and parses
as before. A provider that can returns the request options that enforce the
schema. The OpenAI-compatible provider returns `response_format` with a JSON
schema. The Anthropic provider returns `None` for now. This follows the
ADR-0006 rule for anything added to a published interface: a working default,
and an override is an optimisation.

**The planner builds one flat schema per step:** `narrative`, `tool`,
`arguments`, `answer`, with `"done"` as just another tool. An earlier attempt
used a choice between an action shape and a finish shape. Ollama does not keep
key order, so a model that started with `"answer"` locked itself into finishing
and wrote its plan as the answer. A flat object has no branch to fall into.

**Every argument is narrowed to the page.** A handle must be one in the current
listing. A tool whose required argument cannot be satisfied (a click by handle
on a page with no elements) is left out of the enum. The vision planner bounds
`x` and `y` to the screenshot. Each planner narrows what it knows, through one
`_constrain` hook.

**It is opt-in** (`constrained=True`, and `--constrained` on the bench). Hosted
gateways vary in what they honour, and a run that silently changes its request
shape is harder to compare than one that says so.

## Consequences

Good: on qwen2.5vl:3b the format failures disappear. With the format no longer
in the way, the remaining failures are judgment, which is what training is for.
A model can no longer invent a handle or point off the screen.

Bad: the schema is rebuilt every step. It is small, and cheap next to a
screenshot. The arguments object is the union of every tool's parameters, so a
reply can carry fields the chosen tool ignores. The parser keeps only the chosen
tool's.

A constraint is not understanding. A model that cannot read the page still
cannot, and one that never says "done" (qwen3:4b-instruct) still won't. This
removes failures of form only.

## When we would revisit

If tools grow arguments that clash by name with different types, the flat union
stops being sound, and the schema becomes a per-tool choice keyed on `tool`. That
needs a server that honours key order, or the same trap returns.
