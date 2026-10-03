"""The runner's guarantees.

Exercised with a scripted planner and a stub engine, so these run in CI with no
API key and no browser. What they check is the loop's promises, each of which is
a real failure mode: it stops, it does not lie about no-ops, and it survives a
tool that misbehaves.
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from anybrowser.agent import AgentRunner, Decision, Observation, Planner, RunConfig, StopReason
from anybrowser.agent.runner import render_history
from anybrowser.agent.tool import Tool
from anybrowser.core.engine import BrowserEngine, Capabilities, Capability, EngineInfo
from anybrowser.core.errors import StaleHandle
from anybrowser.core.types import (
    ActionOutcome,
    Box,
    Element,
    NavigationResult,
    Screenshot,
    Snapshot,
    Viewport,
)


class _Engine(BrowserEngine):
    name = "stub"

    def __init__(self, *, clicks_work: bool = True) -> None:
        self.clicks_work = clicks_work
        self.clicked: list[str] = []

    @property
    def capabilities(self) -> Capabilities:
        return Capabilities.of(Capability.EVALUATE_JS)

    async def info(self) -> EngineInfo:
        return EngineInfo(name=self.name, browser="stub")

    async def start(self) -> None: ...
    async def close(self) -> None: ...
    async def url(self) -> str:
        return "https://stub.invalid/"

    async def title(self) -> str:
        return "stub"

    async def viewport(self) -> Viewport:
        return Viewport(width=800, height=600)

    async def snapshot(self, *, include_text: bool = True) -> Snapshot:
        return Snapshot(
            url="https://stub.invalid/",
            title="stub",
            viewport=await self.viewport(),
            elements=(Element(handle="1:0", box=Box(0, 0, 80, 20), tag="button", label="Go"),),
        )

    async def screenshot(self, *, full_page: bool = False, clip=None) -> Screenshot:
        return Screenshot(data=b"\x89PNG\r\n\x1a\n", width=800, height=600)

    async def navigate(self, url: str, *, timeout: float = 30.0) -> NavigationResult:
        return NavigationResult(url=url)

    async def reload(self, *, timeout: float = 30.0) -> NavigationResult:
        return NavigationResult(url="https://stub.invalid/")

    async def go_back(self, *, timeout: float = 30.0) -> NavigationResult:
        return NavigationResult(url="https://stub.invalid/")

    async def click(self, target, **kwargs) -> ActionOutcome:
        self.clicked.append(getattr(target, "handle", str(target)))
        if self.clicks_work:
            return ActionOutcome(ok=True, changed=True, detail="clicked")
        return ActionOutcome.no_change("the click hit nothing")

    async def type_text(self, text: str, **kwargs) -> ActionOutcome:
        return ActionOutcome(ok=True, changed=True, detail="typed")

    async def press_key(self, key: str, **kwargs) -> ActionOutcome:
        return ActionOutcome(ok=True, changed=True, detail="pressed")

    async def scroll(self, dx: float, dy: float, **kwargs) -> ActionOutcome:
        return ActionOutcome.no_change("nothing to scroll")


class _Scripted(Planner):
    """Replays a fixed list of decisions, then declares itself done."""

    def __init__(self, decisions: Sequence[Decision]) -> None:
        self._decisions = list(decisions)
        self.seen: list[Observation] = []

    async def decide(self, observation: Observation, tools: Sequence[Tool]) -> Decision:
        self.seen.append(observation)
        if self._decisions:
            return self._decisions.pop(0)
        return Decision(tool="", arguments={}, narrative="finished", done=True, answer="ok")


def _click(handle: str = "1:0") -> Decision:
    return Decision(tool="click", arguments={"handle": handle}, narrative="try the button")


async def test_runs_to_done_and_returns_the_answer():
    runner = AgentRunner(_Engine(), _Scripted([_click()]))
    result = await runner.run("press the button")
    assert result.ok and result.stop_reason == StopReason.DONE
    assert result.answer == "ok"
    assert result.step_count == 2  # the click, then the done marker


async def test_budget_is_a_hard_ceiling():
    """The most important safety valve: a planner that never finishes must stop."""
    forever = _Scripted([_click() for _ in range(100)])
    runner = AgentRunner(_Engine(), forever, config=RunConfig(max_steps=5))
    result = await runner.run("go forever")
    assert result.stop_reason == StopReason.BUDGET
    assert result.step_count == 5


async def test_repeated_no_ops_break_the_loop():
    """Three identical actions that change nothing is stuck, not persistence."""
    engine = _Engine(clicks_work=False)
    runner = AgentRunner(
        engine, _Scripted([_click() for _ in range(20)]), config=RunConfig(max_steps=20)
    )
    result = await runner.run("click a dead button")
    assert result.stop_reason == StopReason.STUCK
    assert result.step_count == 4, "should stop at the limit, not the budget"
    assert "no effect" in result.error


async def test_alternating_actions_that_change_nothing_still_count_as_stuck():
    """The commonest loop is not a repeat: it is act, scroll, act, scroll.

    Measuring repetition misses it entirely -- no two consecutive actions are
    the same, and the run burns its whole budget going nowhere. What matters is
    that nothing changed, whatever shape the actions took.

    Both actions here are real tools that genuinely report no change. This test
    used to alternate with `look`, and when that tool was removed it kept
    passing for the wrong reason: an unknown tool name fails, a failure also
    counts as no change, and the name still appeared in the error string. A test
    that passes whether or not its subject exists is not testing anything.
    """
    engine = _Engine(clicks_work=False)
    scroll = Decision(tool="scroll", arguments={"dy": 400}, narrative="look further down")
    alternating = _Scripted([d for _ in range(10) for d in (_click(), scroll)])
    runner = AgentRunner(engine, alternating, config=RunConfig(max_steps=20, repeat_limit=4))
    result = await runner.run("loop without repeating")
    assert result.stop_reason == StopReason.STUCK
    assert result.step_count == 4
    assert "click" in result.error and "scroll" in result.error


async def test_a_changed_result_resets_the_breaker():
    """Repeating an action that IS working must not be mistaken for being stuck."""
    engine = _Engine(clicks_work=True)
    runner = AgentRunner(
        engine, _Scripted([_click() for _ in range(6)]), config=RunConfig(max_steps=8)
    )
    result = await runner.run("click repeatedly, productively")
    assert result.stop_reason == StopReason.DONE
    assert len(engine.clicked) == 6


async def test_history_marks_no_effect():
    """The marker is the whole mechanism; without it every line reads as progress."""
    engine = _Engine(clicks_work=False)
    runner = AgentRunner(engine, _Scripted([_click(), _click()]), config=RunConfig(max_steps=2))
    result = await runner.run("x")
    rendered = render_history(result.steps)
    assert "[no effect]" in rendered
    assert "why: try the button" in rendered


async def test_unknown_tool_is_reported_not_fatal():
    runner = AgentRunner(
        _Engine(),
        _Scripted([Decision(tool="teleport", arguments={}, narrative="worth a try")]),
    )
    result = await runner.run("x")
    assert result.stop_reason == StopReason.DONE
    failed = result.steps[0].result
    assert not failed.ok and "no tool named" in failed.summary


async def test_stale_handle_becomes_a_result_not_a_crash():
    """A stale handle means re-snapshot, so the planner must get to hear about it."""
    runner = AgentRunner(_Engine(), _Scripted([_click("99:99")]))
    result = await runner.run("x")
    assert result.stop_reason == StopReason.DONE
    assert not result.steps[0].result.ok


async def test_confirm_can_refuse_a_mutating_action():
    """The harness raises the gate, not the agent.

    A prompt-level "ask before destructive actions" rule is advisory and leaks;
    a gate at dispatch cannot be talked out of.
    """
    engine = _Engine()
    runner = AgentRunner(
        engine,
        _Scripted([_click()]),
        config=RunConfig(confirm=lambda decision: False),
    )
    result = await runner.run("x")
    assert engine.clicked == [], "a refused action must not reach the engine"
    assert "not confirmed" in result.steps[0].result.summary


async def test_stop_ends_the_run_cooperatively():
    engine = _Engine()
    runner = AgentRunner(engine, _Scripted([_click() for _ in range(10)]))
    runner.stop()
    result = await runner.run("x")
    assert result.stop_reason == StopReason.STOPPED
    assert result.step_count == 0


async def test_planner_sees_a_fresh_snapshot_each_step():
    """A planner reasoning about a stale page is reasoning about a page that is gone."""
    planner = _Scripted([_click(), _click()])
    runner = AgentRunner(_Engine(), planner, config=RunConfig(max_steps=3))
    await runner.run("x")
    assert len(planner.seen) >= 2
    assert all(o.snapshot is not None for o in planner.seen)
    # Distinct observation objects, not one reused.
    assert planner.seen[0].snapshot is not planner.seen[1].snapshot


async def test_screenshot_failure_degrades_rather_than_ends_the_run():
    class _NoPixels(_Engine):
        async def screenshot(self, *, full_page: bool = False, clip=None):
            raise StaleHandle("no pixels here")

    runner = AgentRunner(_NoPixels(), _Scripted([_click()]))
    result = await runner.run("x")
    assert result.ok


def test_decision_requires_a_narrative():
    """Every action must carry a human explanation, including from silent models."""
    with pytest.raises(ValueError):
        Decision(tool="click", arguments={}, narrative="   ")


class _Loading(_Engine):
    """A page whose content the test controls, standing in for a slow result."""

    def __init__(self) -> None:
        super().__init__()
        self.content = "spinner"

    async def page_signature(self) -> str:
        return self.content


class _LandsWhileDeciding(_Scripted):
    """A planner slow enough that the result arrives while it is thinking."""

    def __init__(self, engine: _Loading, decisions: Sequence[Decision]) -> None:
        super().__init__(decisions)
        self._engine = engine

    async def decide(self, observation: Observation, tools: Sequence[Tool]) -> Decision:
        self._engine.content = "results"
        return await super().decide(observation, tools)


def _wait(seconds: float = 0.5) -> Decision:
    return Decision(tool="wait", arguments={"seconds": seconds}, narrative="still loading")


async def test_wait_counts_a_change_that_landed_while_the_planner_was_deciding():
    """The wait measures from the page the planner saw, not from when it runs.

    Measuring from now, this wait sits out its full 15 seconds, reports that
    nothing changed, and the very next observation shows the results it said
    never came.
    """
    engine = _Loading()
    runner = AgentRunner(engine, _LandsWhileDeciding(engine, [_wait(15)]))
    result = await runner.run("x")
    waited = result.steps[0].result
    assert waited.ok and waited.changed
    assert result.elapsed < 5


async def test_a_wait_that_sees_nothing_says_so_and_counts_toward_stuck():
    """A page that is never coming must not let a run wait out its budget."""
    runner = AgentRunner(
        _Loading(),
        _Scripted([_wait() for _ in range(10)]),
        config=RunConfig(max_steps=10, repeat_limit=2),
    )
    result = await runner.run("x")
    assert result.stop_reason == StopReason.STUCK
    assert not result.steps[0].result.changed
    assert "did not change" in result.steps[0].result.summary


async def test_wait_is_not_gated_and_is_not_marked_as_a_failed_action():
    """Waiting changes nothing, so there is nothing to confirm -- and a wait that
    times out is a read that found nothing, not an action that missed."""
    runner = AgentRunner(
        _Loading(),
        _Scripted([_wait()]),
        config=RunConfig(confirm=lambda decision: False),
    )
    result = await runner.run("x")
    assert "not confirmed" not in result.steps[0].result.summary
    rendered = render_history(result.steps[:1], reads={"wait"})
    assert "[no effect]" not in rendered


async def test_wait_caps_what_a_model_asks_for():
    from anybrowser.agent.tool import ToolContext
    from anybrowser.agent.tools import WaitTool

    class _Recording(_Loading):
        async def await_change(self, *, timeout=10.0, poll=0.25, since=None):
            self.timeout = timeout
            return ActionOutcome.no_change("nothing")

    engine = _Recording()
    await WaitTool().run(ToolContext(engine=engine), seconds=600)
    assert engine.timeout == WaitTool.max_seconds
    bad = await WaitTool().run(ToolContext(engine=engine), seconds="soon")
    assert not bad.ok


async def test_an_unusable_reply_is_recorded_as_the_models_failure():
    from anybrowser.core.errors import ModelOutputError

    class _Garbled(Planner):
        async def decide(self, observation, tools):
            raise ModelOutputError("planner did not return JSON: 'lol'")

    result = await AgentRunner(_Engine(), _Garbled()).run("x")
    assert result.stop_reason == StopReason.ERROR
    assert result.error_type == "ModelOutputError"


def test_an_empty_reply_at_the_token_limit_is_reported_as_cut_off():
    """A reasoning model can spend its whole budget thinking. The reply is then
    empty, and "did not return JSON: ''" blamed the model for our limit."""
    from anybrowser.agent.llm_planner import LLMPlanner
    from anybrowser.agent.tools import default_tools
    from anybrowser.core.errors import ModelOutputError

    planner = LLMPlanner(object(), model="m", max_tokens=2048)  # type: ignore[arg-type]
    with pytest.raises(ModelOutputError, match=r"cut off after 2048 tokens.*reasoning"):
        planner._parse("", default_tools(), stop_reason="length")
    with pytest.raises(ModelOutputError, match="did not return JSON"):
        planner._parse("I refuse", default_tools(), stop_reason="stop")
