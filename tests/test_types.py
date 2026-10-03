"""The value types carry real invariants; these are them."""

from __future__ import annotations

import pytest

from anybrowser.core.types import ActionOutcome, Box, Point, Viewport


def test_normalized_maps_into_the_surface():
    surface = Box(100, 50, 200, 100)
    assert Point.from_normalized(0, 0, surface) == Point(100, 50)
    assert Point.from_normalized(1000, 1000, surface) == Point(300, 150)
    assert Point.from_normalized(500, 500, surface) == surface.center


@pytest.mark.parametrize("nx, ny", [(-1, 0), (0, 1001), (1500, 1500)])
def test_normalized_rejects_out_of_range(nx, ny):
    """Out-of-range silently clamped is a wrong click nobody notices."""
    with pytest.raises(ValueError):
        Point.from_normalized(nx, ny, Box(0, 0, 10, 10))


def test_box_geometry():
    box = Box(10, 20, 100, 50)
    assert box.center == Point(60, 45)
    assert box.contains(Point(60, 45))
    assert not box.contains(Point(5, 45))
    assert box.intersects(Box(50, 40, 10, 10))
    assert not box.intersects(Box(500, 500, 10, 10))


def test_viewport_box_is_origin_relative():
    """Scroll offset must not leak into the viewport box.

    Elements are reported in viewport coordinates, so a viewport box that
    started at scrollY would double-count the offset on every point.
    """
    view = Viewport(width=800, height=600, scroll_y=1200)
    assert view.box == Box(0, 0, 800, 600)


def test_no_change_is_ok_but_not_changed():
    outcome = ActionOutcome.no_change("already at the scroll limit")
    assert outcome.ok and not outcome.changed
    assert "scroll limit" in outcome.detail


def test_failure_is_neither():
    outcome = ActionOutcome.failure("element is gone")
    assert not outcome.ok and not outcome.changed


def test_outcome_constructors_reject_a_detail_kwarg():
    """`detail` is the first positional argument, so passing it again collides.

    This has now been written twice, in two engines, and both times mypy caught
    it rather than a test -- meaning it would have reached anyone not running a
    type checker. The extra data belongs under its own key.
    """
    with pytest.raises(TypeError):
        ActionOutcome.no_change("nothing happened", detail="why")
    with pytest.raises(TypeError):
        ActionOutcome.failure("it broke", detail="why")

    # The intended shape: context travels in `data`.
    outcome = ActionOutcome.no_change("nothing happened", reason="already at the limit")
    assert outcome.detail == "nothing happened"
    assert outcome.data["reason"] == "already at the limit"


def _page(**kwargs):
    from anybrowser.core.types import Snapshot

    return Snapshot(url="https://x.invalid/", title="x", viewport=Viewport(800, 600), **kwargs)


def test_the_old_signature_name_still_works_and_says_why_it_moved():
    """A third-party engine written against 0.3 must keep running, and be told.

    Silently aliasing would leave it reaching for the element fingerprint when
    it meant "did the page change", which is the mistake the rename exists to
    stop -- so the warning names the method it probably wanted.
    """
    with pytest.warns(DeprecationWarning, match="page_signature"):
        page = _page(signature="3:abc")
    assert page.element_signature == "3:abc"
    with pytest.warns(DeprecationWarning, match="element_signature"):
        assert page.signature == "3:abc"


def test_the_new_name_does_not_warn():
    # filterwarnings turns any DeprecationWarning into an error, so this would
    # fail if the shim fired on the new name too.
    assert _page(element_signature="3:abc").element_signature == "3:abc"


def test_passing_both_names_is_refused_rather_than_guessed():
    with pytest.warns(DeprecationWarning), pytest.raises(TypeError):
        _page(signature="a", element_signature="b")


def test_the_wire_carries_both_keys_and_reads_either():
    """Mixed versions across the daemon keep the field, without a protocol bump."""
    from anybrowser.daemon.codec import dump_snapshot, load_snapshot

    wire = dump_snapshot(_page(element_signature="3:abc"))
    assert wire["element_signature"] == wire["signature"] == "3:abc"
    old_peer = {k: v for k, v in wire.items() if k != "element_signature"}
    assert load_snapshot(old_peer).element_signature == "3:abc"
    assert load_snapshot(wire).element_signature == "3:abc"
