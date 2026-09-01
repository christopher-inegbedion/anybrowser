"""The value types carry real invariants; these are them."""

from __future__ import annotations

import pytest

from relaykit.core.types import ActionOutcome, Box, Point, Viewport


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
