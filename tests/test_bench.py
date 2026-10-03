"""The bench's judge, its server, and the one property every task must have.

That property is determinism: a seed names one task instance, in every process.
Fitness numbers from two runs are only comparable if the same seeds produced
the same pages, and an evolution loop compares nothing else.
"""

from __future__ import annotations

import os
import subprocess
import sys
import urllib.request
from pathlib import Path
from urllib.parse import urlencode

import pytest
from anybrowser_bench import ALL_TASKS, BenchServer, Kind, Outcome, Submission, judge, task


def test_a_seed_builds_the_same_instance_every_time():
    for t in ALL_TASKS:
        a, b = t.build(7), t.build(7)
        assert a.pages == b.pages and a.expected == b.expected, t.id


def test_a_seed_builds_the_same_instance_in_another_process():
    """Set iteration order changes with PYTHONHASHSEED. A task that draws from a
    set gives a different page per process for the same seed, and two runs of
    the suite silently grade different tests."""
    # Everything about the instance, hashed: an earlier version compared the
    # answer and page sizes, and missed a set-order bug that changed only which
    # order IDs appeared -- the goal and the page text, not the answer.
    code = (
        "import hashlib;from anybrowser_bench import ALL_TASKS;"
        "print(hashlib.sha256(repr([(i.goal, i.expected, sorted(i.expected_fields.items()),"
        " sorted(i.pages.items())) for t in ALL_TASKS for i in [t.build(3)]]).encode())"
        ".hexdigest())"
    )
    outputs = {
        subprocess.run(
            [sys.executable, "-c", code],
            env={**os.environ, "PYTHONHASHSEED": str(hash_seed)},
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        for hash_seed in (0, 1, 2)
    }
    assert len(outputs) == 1


def test_different_seeds_build_different_instances():
    """A run must not be able to memorise a (goal, page) -> action mapping.
    Either may repeat on its own -- form-fill's page is fixed and its goal
    varies; canvas-menu may ask for the same button, somewhere else -- but
    never both."""
    for t in ALL_TASKS:
        seen = {(i.goal, tuple(sorted(i.pages.items()))) for i in map(t.build, range(5))}
        assert len(seen) == 5, f"{t.id} repeats an instance across seeds"


def test_answers_that_must_be_read_differ_across_seeds():
    for t in ALL_TASKS:
        if t.kind is Kind.ANSWER:
            answers = {t.build(seed).expected for seed in range(5)}
            assert len(answers) == 5, f"{t.id} repeats its answer across seeds"


def test_every_answer_is_actually_on_its_pages():
    """A task whose answer is not on the page cannot be solved, only guessed."""
    for t in ALL_TASKS:
        for seed in range(5):
            i = t.build(seed)
            if i.kind is Kind.ANSWER:
                # The async and search tasks write their answer from a script.
                assert any(i.expected in html for html in i.pages.values()), (t.id, seed)
            assert i.expected not in i.decoys


def test_the_missing_item_is_really_missing():
    for seed in range(20):
        i = task("missing-item").build(seed)
        absent = i.goal.split("price of the ", 1)[1].split("?", 1)[0]
        assert absent not in i.pages["index.html"]


# -- the judge -------------------------------------------------------------- #

_answer = task("long-list").build(0)


def test_the_right_answer_succeeds():
    v = judge(_answer, finished=True, answer=f"It costs {_answer.expected}.", submissions=[])
    assert v.outcome is Outcome.SUCCESS and v.score == 1


def test_the_answer_may_drop_the_currency_symbol():
    bare = _answer.expected.lstrip("£")
    assert judge(_answer, finished=True, answer=bare, submissions=[]).outcome is Outcome.SUCCESS


def test_a_wrong_answer_is_a_false_claim_and_costs_more_than_giving_up():
    wrong = judge(_answer, finished=True, answer="£1.11", submissions=[])
    gave_up = judge(_answer, finished=False, answer="", submissions=[])
    assert wrong.outcome is Outcome.FALSE_CLAIM
    assert gave_up.outcome is Outcome.FAILED
    assert wrong.score < gave_up.score


def test_listing_every_price_does_not_pass():
    everything = " ".join([_answer.expected, *_answer.decoys])
    assert judge(_answer, finished=True, answer=everything, submissions=[]).outcome is (
        Outcome.FALSE_CLAIM
    )


def test_a_partial_number_does_not_match():
    expected = _answer.expected  # e.g. £123.45
    padded = "9" + expected.lstrip("£")
    assert judge(_answer, finished=True, answer=padded, submissions=[]).outcome is (
        Outcome.FALSE_CLAIM
    )


_state = task("form-fill").build(0)


def test_state_is_graded_by_what_the_server_received_not_by_the_claim():
    right = [Submission("submit", dict(_state.expected_fields))]
    assert judge(_state, finished=False, answer="", submissions=right).outcome is (Outcome.SUCCESS)
    assert judge(_state, finished=True, answer="Done!", submissions=[]).outcome is (
        Outcome.FALSE_CLAIM
    )


def test_the_last_submission_counts():
    wrong = Submission("submit", {**_state.expected_fields, "plan": "nonsense"})
    right = Submission("submit", dict(_state.expected_fields))
    assert judge(_state, finished=True, answer="", submissions=[wrong, right]).outcome is (
        Outcome.SUCCESS
    )
    assert judge(_state, finished=True, answer="", submissions=[right, wrong]).outcome is (
        Outcome.FALSE_CLAIM
    )


_missing = task("missing-item").build(0)


@pytest.mark.parametrize(
    ("answer", "outcome"),
    [
        ("NOT FOUND", Outcome.SUCCESS),
        ("It is not found in the catalogue.", Outcome.SUCCESS),
        ("£12.34", Outcome.FALSE_CLAIM),
        ("I think it is about ten pounds", Outcome.FALSE_CLAIM),
    ],
)
def test_an_impossible_task_rewards_saying_so(answer, outcome):
    assert judge(_missing, finished=True, answer=answer, submissions=[]).outcome is outcome


def test_not_found_with_a_price_attached_is_still_a_guess():
    hedge = f"NOT FOUND, but similar items cost {_missing.decoys[0]}"
    assert judge(_missing, finished=True, answer=hedge, submissions=[]).outcome is (
        Outcome.FALSE_CLAIM
    )


# -- the server ------------------------------------------------------------- #


def test_the_server_serves_pages_and_records_posts_per_instance():
    a, b = task("form-fill").build(0), task("form-fill").build(1)
    with BenchServer() as server:
        root_a, root_b = server.mount(a), server.mount(b)
        page = urllib.request.urlopen(root_a + "index.html").read().decode()
        assert "Create account" in page
        body = urlencode({"name": "X Y", "email": "x@y.z", "plan": "Team"}).encode()
        urllib.request.urlopen(root_a + "submit", data=body)
        assert server.submissions(root_a) == [
            Submission("submit", {"name": "X Y", "email": "x@y.z", "plan": "Team"})
        ]
        assert server.submissions(root_b) == []


# -- infrastructure errors -------------------------------------------------- #


class _Page:
    """Just enough engine for run_suite: it only navigates before each attempt."""

    async def navigate(self, url, **_):
        return None


async def test_a_broken_model_is_an_error_not_an_honest_failure():
    """The first baseline run graded a misconfigured endpoint as sixteen honest
    failures, fitness 0.000. An evolution loop would have ranked it."""
    from anybrowser_bench import run_suite

    from anybrowser.agent import RunResult, StopReason

    async def broken(engine, goal):
        return RunResult(goal=goal, stop_reason=StopReason.ERROR, error="model unreachable")

    report = await run_suite(broken, _Page(), seeds=(0, 1))
    assert {a.outcome for a in report.attempts} == {"error"}
    assert not report.trustworthy
    assert len(report.attempts) == 3, "should stop after three errors in a row"
    assert "model unreachable" in report.aborted


async def test_errors_do_not_count_toward_fitness():
    from anybrowser_bench import run_suite

    from anybrowser.agent import RunResult, StopReason

    calls = 0

    async def flaky(engine, goal):
        nonlocal calls
        calls += 1
        if calls == 1:
            return RunResult(goal=goal, stop_reason=StopReason.ERROR, error="blip")
        return RunResult(goal=goal, stop_reason="budget_exhausted")

    report = await run_suite(flaky, _Page(), tasks=[task("long-list")], seeds=(0, 1))
    assert [a.outcome for a in report.attempts] == ["error", "failed"]
    assert report.fitness == 0.0 and not report.trustworthy


async def test_an_agent_that_raises_is_failing_not_erroring():
    """An evolved agent's own crash is its fault, and must cost it."""
    from anybrowser_bench import run_suite

    async def crashes(engine, goal):
        raise RuntimeError("bad mutation")

    report = await run_suite(crashes, _Page(), tasks=[task("long-list")], seeds=(0,))
    assert report.attempts[0].outcome == "failed"
    assert "bad mutation" in report.attempts[0].reason
    assert report.trustworthy


def test_a_provider_timeout_given_as_text_is_coerced_or_named():
    from anybrowser.core.errors import AnyBrowserError
    from anybrowser.models.openai_compatible import OpenAICompatibleProvider

    assert OpenAICompatibleProvider(api_key="x", timeout="300")._timeout == 300.0
    with pytest.raises(AnyBrowserError, match="timeout must be a number"):
        OpenAICompatibleProvider(api_key="x", timeout="soon")


async def test_a_model_that_cannot_produce_usable_output_is_failing_not_erroring():
    """Gemma 1B copied tool schemas into its arguments. That is the model
    failing the task, and excluding it scored a weak model as untestable."""
    from anybrowser_bench import run_suite

    from anybrowser.agent import RunResult, StopReason

    async def garbled(engine, goal):
        return RunResult(
            goal=goal,
            stop_reason=StopReason.ERROR,
            error="planner did not return JSON",
            error_type="ModelOutputError",
        )

    report = await run_suite(garbled, _Page(), tasks=[task("long-list")], seeds=(0, 1))
    assert [a.outcome for a in report.attempts] == ["failed", "failed"]
    assert report.trustworthy


async def test_grounding_scores_a_perfect_pointer_perfect_and_a_shifted_one_not():
    from anybrowser_bench.grounding import Target, score

    from anybrowser.core.types import Box

    targets = [
        Target(b"", 100, 100, "a", Box(40, 40, 20, 10), "dom"),
        Target(b"", 100, 100, "b", Box(50, 50, 20, 10), "canvas"),
    ]

    async def perfect(png, description, w, h):
        box = next(t.box for t in targets if t.description == description)
        return box.center.as_tuple()

    async def shifted(png, description, w, h):
        x, y = await perfect(png, description, w, h)
        return x * 1.5, y * 1.5  # the scaling bug a too-small screenshot caused

    assert (await score(perfect, targets)).hit_rate == 1.0
    assert (await score(shifted, targets)).hit_rate == 0.0


@pytest.mark.parametrize(
    ("text", "point"),
    [
        ('{"x": 412, "y": 230}', (412, 230)),
        ('```json\n{"x": 139, "y": 166}}\n```', (139, 166)),
        ("[100, 200, 140, 220]", (120, 210)),
        ("I cannot find it", None),
    ],
)
def test_grounding_reads_the_point_out_of_a_reply(text, point):
    from anybrowser_bench.grounding import parse_point

    assert parse_point(text) == point


# -- recording and datasets -------------------------------------------------- #


def _recorded(tmp_path, outcome: str, steps: list[dict]):
    """One recorded attempt on disk, the way run_suite writes it."""
    from anybrowser_bench.record import Exchange, write_attempt
    from anybrowser_bench.runner import Attempt

    from anybrowser.models.provider import ImagePart, Message, Role

    trace = tuple(s["step"] for s in steps if s["step"] is not None)
    attempt = Attempt(
        task_id="canvas-menu",
        seed=0,
        kind="state",
        outcome=outcome,
        score=1.0 if outcome == "success" else 0.0,
        reason="",
        stop_reason="done",
        steps=len(trace),
        seconds=1.0,
        answer="",
        tools=tuple(s["tool"] for s in trace),
        trace=trace,
    )
    exchanges = [
        Exchange(
            messages=(
                Message(role=Role.SYSTEM, text="sys"),
                Message(role=Role.USER, text=f"step {i}", images=[ImagePart(data=b"png%d" % i)]),
            ),
            options={"response_format": {"type": "json_schema"}},
            reply=s["reply"],
        )
        for i, s in enumerate(steps)
    ]
    return write_attempt(tmp_path, attempt, exchanges)


def _step(tool, changed=True, reply=None):
    return {
        "reply": reply or f'{{"tool": "{tool}"}}',
        "step": {
            "tool": tool,
            "arguments": {},
            "narrative": "",
            "ok": True,
            "changed": changed,
            "summary": "",
        },
    }


def test_a_recorded_attempt_keeps_exactly_what_the_model_saw(tmp_path):
    import json

    where = _recorded(tmp_path, "success", [_step("click"), _step("done")])
    step = json.loads((where / "step-00.json").read_text())
    assert [m["role"] for m in step["messages"]] == ["system", "user"]
    assert (where / step["messages"][1]["images"][0]).read_bytes() == b"png0"
    assert step["options"] == {"response_format": {"type": "json_schema"}}
    assert json.loads((where / "attempt.json").read_text())["outcome"] == "success"


def test_the_dataset_keeps_successes_and_drops_steps_that_did_nothing(tmp_path):
    import json

    from anybrowser_bench.dataset import build_sft

    rec = tmp_path / "rec"
    _recorded(rec, "success", [_step("click"), _step("click", changed=False), _step("done")])
    _recorded(rec, "false_claim", [_step("click"), _step("done")])
    stats = build_sft([rec], tmp_path / "ds")
    rows = [json.loads(line) for line in (tmp_path / "ds" / "train.jsonl").open()]
    assert stats.successes == 1 and stats.dropped_no_effect == 1
    assert [r["meta"]["tool"] for r in rows] == ["click", "done"]
    assert [m["role"] for m in rows[0]["messages"]] == ["system", "user", "assistant"]
    assert rows[0]["messages"][-1]["content"] == '{"tool": "click"}'
    assert Path(rows[0]["images"][0]).read_bytes() == b"png0"


def test_the_same_step_recorded_twice_is_one_example(tmp_path):
    from anybrowser_bench.dataset import build_sft

    rec = tmp_path / "rec"
    for _ in range(2):
        _recorded(rec, "success", [_step("click"), _step("done")])
    stats = build_sft([rec], tmp_path / "ds")
    assert stats.rows == 2 and stats.dropped_duplicate == 2
