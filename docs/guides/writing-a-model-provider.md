# Writing a model provider

One required method:

```python
from relaykit.models.provider import ModelProvider, Completion, Usage


class BedrockProvider(ModelProvider):
    name = "bedrock"

    async def complete(
        self, messages, *, model, temperature=0.0, max_tokens=4096, stop=(), **options
    ) -> Completion: ...
```

```toml
[project.entry-points."relaykit.models"]
bedrock = "my_package.model:BedrockProvider"
```

## Three things that are not optional

**Report cost honestly.** `Usage.cost_usd` comes from *your* price table, because
only you know whether a cache read was billed at a discount. If you genuinely
cannot price a call, return `0.0` and say why in `Usage.notes`. Do not guess —
something upstream is metering against it.

**Answer `supports_images` from a table.** Several text-only models reject an
image-bearing request with a 4xx that reads exactly like a transport error, so a
wrong answer here surfaces as a mysterious intermittent failure hours later.
Relay lost an afternoon to precisely this with a model that was 405-on-images.

**Implement `stream` if your API can.** Streaming is what makes a running agent
interruptible mid-decision: a blocking completion call cannot be steered or
stopped, and the user pressing stop waits for the whole response. The default
implementation yields the finished text once, which is correct but gives up that
property.

## Role handling is your problem

Providers disagree about roles in ways no common wrapper survives. Anthropic
takes a top-level `system`; OpenAI accepts a `developer` role; several
OpenAI-compatible endpoints reject `developer` *and* reject a second `system`
message, returning a 400 that names neither. The interface gives you
`system`/`user`/`assistant` and expects you to fold them into whatever your
endpoint actually accepts.
