"""The reference engine: Playwright.

This backend exists to be *read*. It is the shortest complete implementation of
:class:`~relaykit.core.engine.BrowserEngine`, it passes the conformance suite,
and it is the file to copy when writing a backend for a browser we do not ship.

It is not the backend Relay itself uses in anger -- Playwright drives its own
profile, so it cannot satisfy ``ATTACH_TO_USER_SESSION``, which is the whole
reason the Chrome and Safari engines exist. It is however the fastest way to run
an agent against a throwaway browser, which is what you want in CI.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping, Sequence
from typing import Any

from ...core.engine import BrowserEngine, Capabilities, Capability, EngineInfo
from ...core.errors import (
    ActionFailed,
    ElementNotFound,
    EngineNotAvailable,
    EvaluationError,
    NavigationError,
    StaleHandle,
)
from ...core.types import (
    ActionOutcome,
    Box,
    Element,
    FrameInfo,
    KeyModifier,
    MouseButton,
    NavigationResult,
    Point,
    Screenshot,
    Snapshot,
    TabInfo,
    Viewport,
)
from .script import COLLECT_JS, READ_JS

__all__ = ["PlaywrightEngine"]

_CAPABILITIES = Capabilities.of(
    Capability.TRUSTED_INPUT,
    Capability.EVALUATE_JS,
    Capability.CROSS_ORIGIN_FRAMES,
    Capability.OFFSCREEN_SCREENSHOT,
    Capability.FULL_PAGE_SCREENSHOT,
    Capability.POINTER_GESTURES,
    Capability.FILE_UPLOAD,
    Capability.JS_DIALOGS,
    Capability.COOKIES,
    Capability.TAB_MANAGEMENT,
    Capability.INIT_SCRIPTS,
    Capability.NETWORK_INTERCEPTION,
    Capability.PAGE_ZOOM,
)


class PlaywrightEngine(BrowserEngine):
    """Drive a Playwright-managed browser."""

    name = "playwright"

    def __init__(
        self,
        *,
        browser: str = "chromium",
        headless: bool = True,
        viewport_width: int = 1280,
        viewport_height: int = 800,
        slow_mo: float = 0.0,
        **launch_options: Any,
    ) -> None:
        self._browser_name = browser
        self._headless = headless
        self._viewport = {"width": viewport_width, "height": viewport_height}
        self._slow_mo = slow_mo
        self._launch_options = launch_options
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._page: Any = None
        #: Bumped on every navigation; handles carry it so staleness is detectable
        #: without a round trip into a page that may no longer exist.
        self._generation = 0

    # ------------------------------------------------------------------ #
    # Lifecycle                                                           #
    # ------------------------------------------------------------------ #

    @classmethod
    async def probe(cls) -> None:
        try:
            import playwright.async_api  # noqa: F401
        except ImportError as exc:
            raise EngineNotAvailable(
                "playwright is not installed; pip install 'relaykit[playwright]'"
            ) from exc

    @property
    def capabilities(self) -> Capabilities:
        return _CAPABILITIES

    async def info(self) -> EngineInfo:
        import platform

        version = ""
        if self._browser is not None:
            version = self._browser.version
        return EngineInfo(
            name=self.name,
            browser=self._browser_name,
            browser_version=version,
            platform=platform.platform(),
            engine_version="0.1.0",
        )

    async def start(self) -> None:
        if self._page is not None:
            return
        await self.probe()
        from playwright.async_api import async_playwright

        self._playwright = await async_playwright().start()
        launcher = getattr(self._playwright, self._browser_name, None)
        if launcher is None:
            raise EngineNotAvailable(f"unknown playwright browser {self._browser_name!r}")
        self._browser = await launcher.launch(
            headless=self._headless, slow_mo=self._slow_mo, **self._launch_options
        )
        self._context = await self._browser.new_context(viewport=self._viewport)
        self._page = await self._context.new_page()

    async def close(self) -> None:
        for closer in (self._context, self._browser):
            try:
                if closer is not None:
                    await closer.close()
            except Exception:
                pass
        try:
            if self._playwright is not None:
                await self._playwright.stop()
        except Exception:
            pass
        self._page = self._context = self._browser = self._playwright = None

    # ------------------------------------------------------------------ #
    # Internals                                                           #
    # ------------------------------------------------------------------ #

    def _require_page(self) -> Any:
        if self._page is None:
            raise ActionFailed("engine is not started")
        return self._page

    def _encode(self, index: int) -> str:
        return f"{self._generation}:{index}"

    def _decode(self, handle: str) -> int:
        generation, _, index = handle.partition(":")
        if not index.isdigit():
            raise ElementNotFound("malformed handle", handle=handle)
        if int(generation) != self._generation:
            raise StaleHandle(
                "handle belongs to a previous page",
                handle=handle,
                generation=generation,
                current=self._generation,
            )
        return int(index)

    async def _point_for(self, target: Element | Point) -> Point:
        if isinstance(target, Point):
            return target
        index = self._decode(target.handle)
        box = await self._eval(READ_JS, {"op": "box", "index": index})
        if not box:
            raise StaleHandle("element is no longer in the page", handle=target.handle)
        return Point(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)

    async def _eval(self, script: str, arg: Any = None) -> Any:
        page = self._require_page()
        try:
            return await page.evaluate(script, arg)
        except Exception as exc:
            raise EvaluationError(str(exc)) from exc

    async def _page_signature(self) -> str:
        return str(await self._eval(READ_JS, {"op": "signature"}))

    # ------------------------------------------------------------------ #
    # Observation                                                         #
    # ------------------------------------------------------------------ #

    async def url(self) -> str:
        return self._require_page().url

    async def title(self) -> str:
        return await self._require_page().title()

    async def viewport(self) -> Viewport:
        data = await self._eval(READ_JS, {"op": "viewport"})
        return Viewport(
            width=int(data["width"]),
            height=int(data["height"]),
            scroll_x=float(data["scrollX"]),
            scroll_y=float(data["scrollY"]),
            device_pixel_ratio=float(data["dpr"]),
        )

    async def snapshot(self, *, include_text: bool = True) -> Snapshot:
        page = self._require_page()
        raw = await self._eval(COLLECT_JS, {"includeText": include_text})
        elements = [
            Element(
                handle=self._encode(item["index"]),
                box=Box(item["x"], item["y"], item["width"], item["height"]),
                tag=item.get("tag", ""),
                role=item.get("role", ""),
                label=item.get("label", ""),
                value=item.get("value", ""),
                placeholder=item.get("placeholder", ""),
                editable=bool(item.get("editable")),
                disabled=bool(item.get("disabled")),
                attributes=item.get("attributes") or {},
            )
            for item in raw["elements"]
        ]
        view = raw["viewport"]
        return Snapshot(
            url=page.url,
            title=await page.title(),
            viewport=Viewport(
                width=int(view["width"]),
                height=int(view["height"]),
                scroll_x=float(view["scrollX"]),
                scroll_y=float(view["scrollY"]),
                device_pixel_ratio=float(view["dpr"]),
            ),
            elements=elements,
            text=raw.get("text", ""),
            signature=str(raw.get("signature", "")),
            captured_at=time.time(),
            frames=await self.frames(),
        )

    async def screenshot(self, *, full_page: bool = False, clip: Box | None = None) -> Screenshot:
        page = self._require_page()
        options: dict[str, Any] = {"full_page": full_page, "type": "png"}
        if clip is not None:
            options["clip"] = {"x": clip.x, "y": clip.y, "width": clip.width, "height": clip.height}
        data = await page.screenshot(**options)
        view = await self.viewport()
        height = view.height
        if full_page:
            height = int(await self._eval("document.documentElement.scrollHeight"))
        return Screenshot(
            data=data,
            format="png",
            width=int(clip.width) if clip else view.width,
            height=int(clip.height) if clip else height,
            device_pixel_ratio=view.device_pixel_ratio,
            full_page=full_page,
        )

    async def frames(self) -> Sequence[FrameInfo]:
        page = self._require_page()
        out: list[FrameInfo] = []
        for index, frame in enumerate(page.frames):
            out.append(
                FrameInfo(
                    frame_id=str(index),
                    url=frame.url,
                    parent_id="" if frame.parent_frame is None else "0",
                    is_main=frame.parent_frame is None,
                )
            )
        return out

    # ------------------------------------------------------------------ #
    # Navigation                                                          #
    # ------------------------------------------------------------------ #

    async def _navigate(self, action: Any, *, timeout: float) -> NavigationResult:
        page = self._require_page()
        try:
            response = await action(timeout=timeout * 1000)
        except Exception as exc:
            raise NavigationError(str(exc)) from exc
        self._generation += 1
        return NavigationResult(
            url=page.url,
            ok=True,
            status=getattr(response, "status", 0) or 0,
        )

    async def navigate(self, url: str, *, timeout: float = 30.0) -> NavigationResult:
        page = self._require_page()
        return await self._navigate(
            lambda timeout: page.goto(url, wait_until="domcontentloaded", timeout=timeout),
            timeout=timeout,
        )

    async def reload(self, *, timeout: float = 30.0) -> NavigationResult:
        page = self._require_page()
        return await self._navigate(
            lambda timeout: page.reload(wait_until="domcontentloaded", timeout=timeout),
            timeout=timeout,
        )

    async def go_back(self, *, timeout: float = 30.0) -> NavigationResult:
        page = self._require_page()
        return await self._navigate(
            lambda timeout: page.go_back(wait_until="domcontentloaded", timeout=timeout),
            timeout=timeout,
        )

    async def go_forward(self, *, timeout: float = 30.0) -> NavigationResult:
        page = self._require_page()
        return await self._navigate(
            lambda timeout: page.go_forward(wait_until="domcontentloaded", timeout=timeout),
            timeout=timeout,
        )

    # ------------------------------------------------------------------ #
    # Input                                                               #
    # ------------------------------------------------------------------ #

    async def click(
        self,
        target: Element | Point,
        *,
        button: MouseButton = MouseButton.LEFT,
        click_count: int = 1,
        modifiers: Sequence[KeyModifier] = (),
    ) -> ActionOutcome:
        page = self._require_page()
        point = await self._point_for(target)
        before = await self._page_signature()
        # Playwright's mouse API takes no modifiers, so hold them on the keyboard
        # around the click. Doing it here keeps the modifier contract identical
        # across backends that do accept them natively.
        held = [m.value.capitalize() for m in modifiers]
        for key in held:
            await page.keyboard.down(key)
        try:
            await page.mouse.click(point.x, point.y, button=button.value, click_count=click_count)
        finally:
            for key in reversed(held):
                await page.keyboard.up(key)
        return await self._settle(before, "click landed", "click hit nothing")

    async def type_text(
        self,
        text: str,
        *,
        target: Element | Point | None = None,
        clear_first: bool = False,
        delay: float = 0.0,
    ) -> ActionOutcome:
        page = self._require_page()
        if target is not None:
            point = await self._point_for(target)
            await page.mouse.click(point.x, point.y)
        if clear_first:
            await page.keyboard.press("ControlOrMeta+a")
            await page.keyboard.press("Delete")
        await page.keyboard.type(text, delay=delay * 1000)
        # Read back rather than trust: controlled inputs routinely swallow writes.
        landed = await self._eval(READ_JS, {"op": "activeValue"})
        if text and text not in str(landed):
            return ActionOutcome.no_change(
                "typed text did not land in the focused element",
                expected=text,
                actual=landed,
            )
        return ActionOutcome(ok=True, changed=True, detail="text landed", data={"value": landed})

    async def press_key(
        self, key: str, *, modifiers: Sequence[KeyModifier] = (), repeat: int = 1
    ) -> ActionOutcome:
        page = self._require_page()
        combo = "+".join([*(m.value.capitalize() for m in modifiers), key])
        before = await self._page_signature()
        for _ in range(max(1, repeat)):
            await page.keyboard.press(combo)
        return await self._settle(before, f"pressed {combo}", f"{combo} changed nothing")

    async def scroll(
        self, delta_x: float, delta_y: float, *, at: Point | None = None
    ) -> ActionOutcome:
        page = self._require_page()
        if at is not None:
            await page.mouse.move(at.x, at.y)
        before = await self.viewport()
        await page.mouse.wheel(delta_x, delta_y)
        # Scrolling is not synchronous with the wheel event -- smooth-scroll CSS
        # and compositor-driven scrolling both land a frame or more later.
        # Sampling immediately reports "already at the limit" on a page that is
        # in fact moving, which is exactly the lie `changed` exists to prevent.
        after = before
        for _ in range(10):
            await asyncio.sleep(0.03)
            after = await self.viewport()
            if (after.scroll_x, after.scroll_y) != (before.scroll_x, before.scroll_y):
                break
        moved = (after.scroll_x, after.scroll_y) != (before.scroll_x, before.scroll_y)
        if not moved:
            return ActionOutcome.no_change(
                "already at the scroll limit",
                scroll_x=after.scroll_x,
                scroll_y=after.scroll_y,
            )
        return ActionOutcome(
            ok=True,
            changed=True,
            detail="scrolled",
            data={"scroll_x": after.scroll_x, "scroll_y": after.scroll_y},
        )

    async def hover(self, target: Element | Point) -> ActionOutcome:
        page = self._require_page()
        point = await self._point_for(target)
        before = await self._page_signature()
        await page.mouse.move(point.x, point.y)
        return await self._settle(before, "hovered", "hover changed nothing")

    async def drag(
        self,
        path: Sequence[Point],
        *,
        button: MouseButton = MouseButton.LEFT,
        hold: float = 0.0,
    ) -> ActionOutcome:
        if len(path) < 2:
            return ActionOutcome.failure("a drag needs at least two points")
        page = self._require_page()
        before = await self._page_signature()
        await page.mouse.move(path[0].x, path[0].y)
        await page.mouse.down(button=button.value)
        # Playwright keeps the pressed-button state on move for us; a hand-rolled
        # CDP backend must set buttons=1 explicitly or this silently no-ops.
        for point in path[1:]:
            await page.mouse.move(point.x, point.y, steps=8)
        await page.mouse.up(button=button.value)
        return await self._settle(before, "dragged", "the drag moved nothing")

    async def select_option(
        self, target: Element, *, value: str = "", label: str = "", index: int = -1
    ) -> ActionOutcome:
        node = self._decode(target.handle)
        result = await self._eval(
            READ_JS,
            {"op": "select", "index": node, "value": value, "label": label, "optionIndex": index},
        )
        if not result or not result.get("ok"):
            return ActionOutcome.failure(
                "no matching option", detail=str(result), **{"target": target.description}
            )
        return ActionOutcome(ok=True, changed=True, detail=f"selected {result['value']}")

    async def upload_files(self, target: Element, paths: Sequence[str]) -> ActionOutcome:
        page = self._require_page()
        index = self._decode(target.handle)
        handle = await page.evaluate_handle("(i) => window.__relaykit.nodes[i]", index)
        element = handle.as_element()
        if element is None:
            raise StaleHandle("upload target is gone", handle=target.handle)
        await element.set_input_files(list(paths))
        return ActionOutcome(ok=True, changed=True, detail=f"attached {len(paths)} file(s)")

    async def set_zoom(self, factor: float) -> ActionOutcome:
        await self._eval(f"document.body.style.zoom = {float(factor)}")
        return ActionOutcome(ok=True, changed=True, detail=f"zoom {factor}")

    # ------------------------------------------------------------------ #
    # Scripting, tabs, cookies                                            #
    # ------------------------------------------------------------------ #

    async def evaluate(self, script: str, *args: Any, frame_id: str = "") -> Any:
        page = self._require_page()
        target = page
        if frame_id:
            frames = page.frames
            position = int(frame_id)
            if position >= len(frames):
                raise EvaluationError("no such frame", frame_id=frame_id)
            target = frames[position]
        try:
            if args:
                return await target.evaluate(script, args[0] if len(args) == 1 else list(args))
            return await target.evaluate(script)
        except Exception as exc:
            raise EvaluationError(str(exc)) from exc

    async def add_init_script(self, script: str) -> None:
        if self._context is None:
            raise ActionFailed("engine is not started")
        await self._context.add_init_script(script)

    async def tabs(self) -> Sequence[TabInfo]:
        if self._context is None:
            return ()
        out = []
        for index, page in enumerate(self._context.pages):
            out.append(
                TabInfo(
                    tab_id=str(index),
                    url=page.url,
                    title=await page.title(),
                    active=page is self._page,
                    attached=True,
                )
            )
        return out

    async def active_tab(self) -> TabInfo:
        for tab in await self.tabs():
            if tab.active:
                return tab
        raise ActionFailed("no active tab")

    async def switch_tab(self, tab_id: str) -> TabInfo:
        from ...core.errors import TabNotFound

        pages = self._context.pages if self._context else []
        try:
            self._page = pages[int(tab_id)]
        except (ValueError, IndexError) as exc:
            raise TabNotFound("no such tab", tab_id=tab_id) from exc
        self._generation += 1
        await self._page.bring_to_front()
        return await self.active_tab()

    async def open_tab(self, url: str = "") -> TabInfo:
        if self._context is None:
            raise ActionFailed("engine is not started")
        self._page = await self._context.new_page()
        self._generation += 1
        if url:
            await self.navigate(url)
        return await self.active_tab()

    async def close_tab(self, tab_id: str) -> None:
        pages = self._context.pages if self._context else []
        page = pages[int(tab_id)]
        await page.close()
        if page is self._page:
            self._page = self._context.pages[0] if self._context.pages else None
            self._generation += 1

    async def handle_dialog(self, *, accept: bool, prompt_text: str = "") -> ActionOutcome:
        page = self._require_page()
        page.once(
            "dialog",
            lambda dialog: dialog.accept(prompt_text) if accept else dialog.dismiss(),
        )
        return ActionOutcome(ok=True, changed=True, detail="dialog handler armed")

    async def cookies(self, urls: Sequence[str] = ()) -> Sequence[Mapping[str, Any]]:
        if self._context is None:
            return ()
        return await self._context.cookies(list(urls) or None)

    async def set_cookies(self, cookies: Sequence[Mapping[str, Any]]) -> None:
        if self._context is None:
            raise ActionFailed("engine is not started")
        await self._context.add_cookies([dict(c) for c in cookies])

    # ------------------------------------------------------------------ #
    # Change detection                                                    #
    # ------------------------------------------------------------------ #

    async def _settle(self, before: str, changed_detail: str, still_detail: str) -> ActionOutcome:
        """Decide honestly whether the page moved.

        Cheap structural signature rather than pixels: it survives animation and
        caret blink, which a pixel hash does not, and it is what decides whether
        the agent above sees ``changed``.
        """
        import asyncio

        for _ in range(6):
            await asyncio.sleep(0.05)
            try:
                after = await self._page_signature()
            except EvaluationError:
                # A navigation tore the context down -- that is a change.
                return ActionOutcome(ok=True, changed=True, detail="page navigated")
            if after != before:
                return ActionOutcome(ok=True, changed=True, detail=changed_detail)
        return ActionOutcome.no_change(still_detail)
