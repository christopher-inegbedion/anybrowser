from __future__ import annotations

import pytest

from relaykit.core.errors import ProtocolError
from relaykit.daemon.protocol import Event, Request, Response, decode, encode


def test_request_round_trips():
    message = decode(encode(Request(method="engine.click", params={"handle": "e1"})))
    assert isinstance(message, Request)
    assert message.method == "engine.click"
    assert message.params == {"handle": "e1"}


def test_requests_get_an_id_for_free():
    assert Request(method="x").id


def test_response_round_trips_both_ways():
    ok = decode(encode(Response(id="1", result={"changed": True})))
    assert isinstance(ok, Response) and ok.ok and ok.result == {"changed": True}

    bad = decode(encode(Response(id="1", ok=False, error="boom", error_type="ActionFailed")))
    assert isinstance(bad, Response) and not bad.ok and bad.error == "boom"


def test_event_round_trips():
    message = decode(encode(Event(event="tab.navigated", data={"url": "u"})))
    assert isinstance(message, Event) and message.data == {"url": "u"}


def test_encoding_is_one_line():
    """Newline-delimited transports depend on this and cannot check it."""
    assert "\n" not in encode(Event(event="e", data={"text": "a\nb"}))


@pytest.mark.parametrize(
    "raw",
    ["not json", "[]", '"a string"', "{}", '{"unknown": 1}'],
)
def test_malformed_messages_raise_rather_than_half_parse(raw):
    with pytest.raises(ProtocolError):
        decode(raw)
