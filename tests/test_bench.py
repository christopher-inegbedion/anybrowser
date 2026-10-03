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


def test_different_seeds_ask_for_different_answers():
    for t in ALL_TASKS:
        if t.kind is Kind.IMPOSSIBLE:
            continue
        answers = {(i.expected, tuple(i.expected_fields.items())) for i in map(t.build, range(5))}
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
