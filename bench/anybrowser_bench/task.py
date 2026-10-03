"""What a task is, and how an attempt at one is judged.

The judge never takes the agent's word for anything. An answer task is checked
against a value generated from the seed, which the agent has to read off the
page to know. A state task is checked against what the bench server actually
received. Claiming "done" only decides which kind of failure a wrong attempt
is: an agent that gives up honestly scores 0, and an agent that claims success
it did not achieve scores -1.

That asymmetry is the point. A fitness function that scores a confident wrong
answer the same as an honest failure pays an evolving agent to guess, and it
will learn to.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import Enum
from random import Random

__all__ = [
    "SCORES",
    "Instance",
    "Kind",
    "Outcome",
    "Submission",
    "Task",
    "Verdict",
    "judge",
]


class Kind(str, Enum):
    #: Read something off the page and report it.
    ANSWER = "answer"
    #: Change something; graded by what the server received.
    STATE = "state"
    #: The thing asked for does not exist. Saying so is the correct answer.
    IMPOSSIBLE = "impossible"


class Outcome(str, Enum):
    SUCCESS = "success"
    #: Did not finish, and did not claim to.
    FAILED = "failed"
    #: Claimed to be done, and was wrong.
    FALSE_CLAIM = "false_claim"
    #: The model server or the browser broke, so the attempt says nothing about
    #: the agent. Not scored: counting it as 0 once graded a misconfigured model
    #: endpoint as sixteen honest failures, and an evolution loop would have
    #: selected against whatever variant happened to run while the server was
    #: down.
    ERROR = "error"


SCORES: Mapping[Outcome, float] = {
    Outcome.SUCCESS: 1.0,
    Outcome.FAILED: 0.0,
    Outcome.FALSE_CLAIM: -1.0,
}

#: The exact reply an impossible task's goal asks for. Asking for a fixed phrase
#: rather than inferring refusal from wording keeps the judge mechanical.
NOT_FOUND = "NOT FOUND"


@dataclass(frozen=True, slots=True)
class Submission:
    """One form POST the bench server received, relative to the instance root."""

    path: str
    fields: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class Instance:
    """One seeded, concrete attempt-able task."""

    goal: str
    #: Relative path -> HTML. Served under a per-instance prefix.
    pages: Mapping[str, str]
    #: Filled in by :meth:`Task.build`; a builder leaves them alone.
    task_id: str = ""
    seed: int = 0
    kind: Kind = Kind.ANSWER
    start: str = "index.html"
    #: ANSWER: the value that must appear in the answer.
    expected: str = ""
    #: Values on the page that must NOT appear in the answer, so an agent
    #: cannot pass by listing everything it saw.
    decoys: tuple[str, ...] = ()
    #: STATE: the form path and fields the server must have received last.
    expected_path: str = "submit"
    expected_fields: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Task:
    """A task family: one skill, any number of seeded instances."""

    id: str
    kind: Kind
    skills: tuple[str, ...]
    _build: Callable[[Random], Instance]

    def build(self, seed: int) -> Instance:
        # A string seed is deterministic across processes; hash() is not.
        draft = self._build(Random(f"{self.id}:{seed}"))
        return replace(draft, task_id=self.id, seed=seed, kind=self.kind)


@dataclass(frozen=True, slots=True)
class Verdict:
    outcome: Outcome
    reason: str

    @property
    def score(self) -> float:
        return SCORES[self.outcome]


def judge(
    instance: Instance,
    *,
    finished: bool,
    answer: str,
    submissions: Sequence[Submission],
) -> Verdict:
    """Grade one attempt. ``finished`` means the agent declared itself done."""
    if instance.kind is Kind.STATE:
        relevant = [s for s in submissions if s.path == instance.expected_path]
        if relevant and _fields_match(relevant[-1].fields, instance.expected_fields):
            return Verdict(Outcome.SUCCESS, "the server received the expected submission")
        got = dict(relevant[-1].fields) if relevant else "nothing"
        wrong = f"expected {dict(instance.expected_fields)}, server received {got}"
        return _miss(finished, wrong)

    if instance.kind is Kind.IMPOSSIBLE:
        if not finished:
            return Verdict(Outcome.FAILED, "did not finish")
        if _mentions(answer, NOT_FOUND) and not _any_decoy(answer, instance.decoys):
            return Verdict(Outcome.SUCCESS, "reported that it is not there")
        return Verdict(Outcome.FALSE_CLAIM, f"invented an answer: {answer!r}")

    if not finished:
        return Verdict(Outcome.FAILED, "did not finish")
    decoy = _any_decoy(answer, instance.decoys)
    if decoy:
        return Verdict(Outcome.FALSE_CLAIM, f"answer contains a decoy {decoy!r}: {answer!r}")
    if _mentions(answer, instance.expected):
        return Verdict(Outcome.SUCCESS, f"found {instance.expected!r}")
    return Verdict(Outcome.FALSE_CLAIM, f"expected {instance.expected!r}, answered {answer!r}")


def _miss(finished: bool, why: str) -> Verdict:
    if finished:
        return Verdict(Outcome.FALSE_CLAIM, f"claimed done, but {why}")
    return Verdict(Outcome.FAILED, why)


def _norm(text: str) -> str:
    text = text.lower().replace(",", "")
    text = re.sub(r"[£$€]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _mentions(answer: str, value: str) -> bool:
    """Whole-token containment, so 82.17 does not match inside 482.17."""
    pattern = r"(?<![a-z0-9])" + re.escape(_norm(value)) + r"(?![a-z0-9])"
    return re.search(pattern, _norm(answer)) is not None


def _any_decoy(answer: str, decoys: Sequence[str]) -> str:
    return next((d for d in decoys if _mentions(answer, d)), "")


def _fields_match(got: Mapping[str, str], want: Mapping[str, str]) -> bool:
    return all(_norm(got.get(k, "")) == _norm(v) for k, v in want.items())
