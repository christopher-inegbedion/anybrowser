"""The contract every engine must satisfy.

Read this file as the specification -- the prose in ``core/engine.py`` explains
intent, but these assertions are what "conformant" means.
"""

from __future__ import annotations

import pytest

from relaykit.core import Capability, Point
from relaykit.core.errors import CapabilityNotSupported, StaleHandle

req = pytest.mark.requires


# --------------------------------------------------------------------------- #
# Identity and lifecycle                                                       #
# --------------------------------------------------------------------------- #


def test_declares_a_name(engine):
    assert engine.name, "engine.name must be set and match the entry-point key"


def test_capabilities_are_answerable(engine):
    caps = engine.capabilities
    assert all(isinstance(c, Capability) for c in caps.supported)


def test_info_identifies_the_browser(engine, run):
    info = run(engine.info())
    assert info.name == engine.name
    assert info.browser, "info().browser must name the browser being driven"


def test_start_is_idempotent(engine, run):
    run(engine.start())
    run(engine.start())
    assert run(engine.url()) is not None


# --------------------------------------------------------------------------- #
# Navigation                                                                   #
# --------------------------------------------------------------------------- #


def test_navigate_reports_the_landing_url(engine, run, base_url):
    result = run(engine.navigate(f"{base_url}/index.html"))
    assert result.ok
    assert result.url.endswith("/index.html")
    assert run(engine.url()).endswith("/index.html")


def test_title_reflects_the_page(engine, run, base_url):
    run(engine.navigate(f"{base_url}/index.html"))
    assert "conformance" in run(engine.title()).lower()


def test_go_back_returns_to_the_previous_page(engine, run, base_url):
    run(engine.navigate(f"{base_url}/index.html"))
    run(engine.navigate(f"{base_url}/second.html"))
    run(engine.go_back())
    assert run(engine.url()).endswith("/index.html")


# --------------------------------------------------------------------------- #
# Observation                                                                  #
# --------------------------------------------------------------------------- #


def test_snapshot_finds_interactive_elements(engine, run, base_url):
    run(engine.navigate(f"{base_url}/index.html"))
    page = run(engine.snapshot())
    labels = " ".join(el.description for el in page.elements).lower()
    assert "plain button" in labels, f"button missing from snapshot: {labels[:400]}"
    assert page.url.endswith("/index.html")
    assert page.viewport.width > 0 and page.viewport.height > 0


def test_snapshot_boxes_are_viewport_pixels(engine, run, base_url):
    run(engine.navigate(f"{base_url}/index.html"))
    page = run(engine.snapshot())
    visible = [el for el in page.elements if el.box.area > 0]
    assert visible, "no element carried geometry"
    for el in visible:
        assert el.box.width < page.viewport.width * 4, (
            f"{el.description} box looks like device pixels, not CSS pixels: {el.box}"
        )


def test_snapshot_keeps_hidden_file_inputs(engine, run, base_url):
    """The one documented exception to the visibility filter.

    Every real upload flow hides its ``<input type=file>`` behind a styled
    button. An engine that filters it out cannot upload anywhere that matters.
    """
    run(engine.navigate(f"{base_url}/index.html"))
    page = run(engine.snapshot())
    assert any(
        el.tag.lower() == "input" and el.attributes.get("type") == "file" for el in page.elements
    ), "hidden file input was filtered out"


def test_handles_are_addressable(engine, run, base_url):
    run(engine.navigate(f"{base_url}/index.html"))
    page = run(engine.snapshot())
    target = _find(page, "plain button")
    outcome = run(engine.click(target))
    assert outcome.ok


def test_stale_handles_raise_stale_handle(engine, run, base_url):
    """Not ``ElementNotFound`` -- the caller's correct response differs."""
    run(engine.navigate(f"{base_url}/index.html"))
    page = run(engine.snapshot())
    target = _find(page, "plain button")
    run(engine.navigate(f"{base_url}/second.html"))
    with pytest.raises(StaleHandle):
        run(engine.click(target))


# --------------------------------------------------------------------------- #
# Input                                                                        #
# --------------------------------------------------------------------------- #


def test_click_an_element_actually_fires(engine, run, base_url):
    run(engine.navigate(f"{base_url}/index.html"))
    page = run(engine.snapshot())
    outcome = run(engine.click(_find(page, "plain button")))
    assert outcome.ok and outcome.changed
    assert _log(engine, run) == "clicked:plain"


def test_click_a_point_actually_fires(engine, run, base_url):
    run(engine.navigate(f"{base_url}/index.html"))
    page = run(engine.snapshot())
    box = _find(page, "div with a handler").box
    outcome = run(engine.click(box.center))
    assert outcome.ok
    assert _log(engine, run) == "clicked:div"


def test_click_on_nothing_reports_no_change(engine, run, base_url):
    """The single most important honesty check in the suite.

    A click that hit dead space must not come back as a plain success. Agents
    read ``changed`` to decide whether to try something else; an engine that
    always says ``changed=True`` turns every miss into an infinite loop.
    """
    run(engine.navigate(f"{base_url}/index.html"))
    page = run(engine.snapshot())
    dead = Point(page.viewport.width - 3, page.viewport.height - 3)
    outcome = run(engine.click(dead))
    assert not outcome.changed, "clicking dead space was reported as a change"


@req(Capability.TRUSTED_INPUT.value)
def test_clicks_carry_user_activation(engine, run, base_url):
    run(engine.navigate(f"{base_url}/index.html"))
    page = run(engine.snapshot())
    run(engine.click(_find(page, "plain button")))
    state = run(engine.evaluate("document.getElementById('activation').textContent"))
    assert "true" in str(state).lower(), f"input was not trusted: {state}"


def test_type_text_lands_and_is_verified(engine, run, base_url):
    run(engine.navigate(f"{base_url}/index.html"))
    page = run(engine.snapshot())
    outcome = run(engine.type_text("hello world", target=_find(page, "type here")))
    assert outcome.ok and outcome.changed
    assert _log(engine, run) == "typed:hello world"


def test_clear_first_replaces_existing_text(engine, run, base_url):
    run(engine.navigate(f"{base_url}/index.html"))
    page = run(engine.snapshot())
    field = _find(page, "existing value")
    run(engine.type_text("replaced", target=field, clear_first=True))
    value = run(engine.snapshot()).element(field.handle)
    assert value is not None and value.value == "replaced"


def test_press_key_reaches_the_page(engine, run, base_url):
    run(engine.navigate(f"{base_url}/index.html"))
    run(engine.press_key("Enter"))
    assert _log(engine, run) == "key:Enter"


def test_scroll_moves_and_reports_it(engine, run, base_url):
    run(engine.navigate(f"{base_url}/index.html"))
    before = run(engine.viewport()).scroll_y
    outcome = run(engine.scroll(0, 400))
    assert outcome.ok and outcome.changed
    assert run(engine.viewport()).scroll_y > before


def test_scroll_at_the_bottom_reports_no_change(engine, run, base_url):
    """An agent at the end of a page has to be able to learn that."""
    run(engine.navigate(f"{base_url}/index.html"))
    for _ in range(40):
        run(engine.scroll(0, 2000))
    outcome = run(engine.scroll(0, 2000))
    assert not outcome.changed, "scrolling past the bottom was reported as a change"


@req(Capability.POINTER_GESTURES.value)
def test_drag_carries_the_pressed_button(engine, run, base_url):
    """Move events must set the pressed-button state.

    Without it a drag looks perfect from the engine's side and moves nothing on
    any page that checks ``event.buttons`` -- which is most of them, and all
    HTML5 drag-and-drop.
    """
    run(engine.navigate(f"{base_url}/drag.html"))
    start = Point(20, 20)
    outcome = run(engine.drag([start, Point(150, 20), Point(300, 20)]))
    assert outcome.ok
    assert _log(engine, run).startswith("knob:"), "the knob never moved"


def test_select_option_or_says_it_cannot(engine, run, base_url):
    run(engine.navigate(f"{base_url}/index.html"))
    page = run(engine.snapshot())
    target = _find(page, "alpha", required=False) or _find(page, "select", required=False)
    if target is None:
        pytest.skip("engine's snapshot did not expose the <select>")
    try:
        outcome = run(engine.select_option(target, value="b"))
    except CapabilityNotSupported:
        pytest.skip("engine declares no option selection")
    assert outcome.ok
    assert _log(engine, run) == "selected:b"


@req(Capability.FILE_UPLOAD.value)
def test_upload_sets_files_without_a_picker(engine, run, base_url, tmp_path):
    path = tmp_path / "upload.txt"
    path.write_text("relaykit")
    run(engine.navigate(f"{base_url}/index.html"))
    page = run(engine.snapshot())
    file_input = next(
        el
        for el in page.elements
        if el.tag.lower() == "input" and el.attributes.get("type") == "file"
    )
    outcome = run(engine.upload_files(file_input, [str(path)]))
    assert outcome.ok
    count = run(engine.evaluate("document.getElementById('file').files.length"))
    assert int(count) == 1


# --------------------------------------------------------------------------- #
# Pixels and scripting                                                         #
# --------------------------------------------------------------------------- #


def test_screenshot_returns_real_image_bytes(engine, run, base_url):
    run(engine.navigate(f"{base_url}/index.html"))
    shot = run(engine.screenshot())
    assert shot.data, "screenshot was empty"
    assert shot.data[:8] == b"\x89PNG\r\n\x1a\n" or shot.format != "png"
    assert shot.width > 0 and shot.height > 0


@req(Capability.FULL_PAGE_SCREENSHOT.value)
def test_full_page_screenshot_is_taller(engine, run, base_url):
    run(engine.navigate(f"{base_url}/index.html"))
    viewport_shot = run(engine.screenshot())
    full = run(engine.screenshot(full_page=True))
    assert full.full_page
    assert full.height > viewport_shot.height


@req(Capability.EVALUATE_JS.value)
def test_evaluate_round_trips_json(engine, run, base_url):
    run(engine.navigate(f"{base_url}/index.html"))
    assert run(engine.evaluate("1 + 1")) == 2
    assert run(engine.evaluate("document.title")) == "RelayKit conformance"


@req(Capability.CROSS_ORIGIN_FRAMES.value)
def test_frames_are_enumerated(engine, run, base_url):
    run(engine.navigate(f"{base_url}/frames.html"))
    frames = run(engine.frames())
    assert any(f.is_main for f in frames)
    assert len(frames) >= 2, "the child frame was not reported"


# --------------------------------------------------------------------------- #
# Honesty about what is missing                                                #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "capability, call",
    [
        (Capability.EVALUATE_JS, lambda e: e.evaluate("1")),
        (Capability.TAB_MANAGEMENT, lambda e: e.tabs()),
        (Capability.COOKIES, lambda e: e.cookies()),
        (Capability.PAGE_ZOOM, lambda e: e.set_zoom(1.5)),
    ],
)
def test_undeclared_capabilities_raise_the_right_error(engine, run, capability, call):
    """An unsupported call raises ``CapabilityNotSupported`` -- not ``NotImplementedError``,
    not ``AttributeError``, not a backend-specific exception. Callers route on it."""
    if capability in engine.capabilities:
        pytest.skip(f"engine declares {capability.value}")
    with pytest.raises(CapabilityNotSupported):
        run(call(engine))


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #


def _find(page, needle: str, *, required: bool = True):
    needle = needle.lower()
    for el in page.elements:
        haystack = " ".join([el.description, el.label, el.placeholder, el.value, el.tag]).lower()
        if needle in haystack:
            return el
    if required:
        raise AssertionError(
            f"no element matching {needle!r}; snapshot had: "
            + ", ".join(el.description for el in page.elements)[:500]
        )
    return None


def _log(engine, run) -> str:
    """Read the fixture page's status line, however this engine can."""
    if Capability.EVALUATE_JS in engine.capabilities:
        return str(run(engine.evaluate("document.getElementById('log').textContent")))
    page = run(engine.snapshot())
    for el in page.elements:
        if el.attributes.get("id") == "log":
            return el.value or el.label
    raise AssertionError("cannot read the fixture log on this engine")
