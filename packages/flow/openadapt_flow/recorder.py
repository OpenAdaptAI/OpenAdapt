"""Recorder: wraps a Backend and captures frames + events for each action.

Recording format (DESIGN.md):

    <recording>/
      meta.json          # {"id", "created_at", "viewport": [w,h],
                         #  "coordinate_space": "page", "app_url",
                         #  "params": {"<param_name>": "<value typed>"}}
      events.jsonl       # {"i":0,"kind":"click","x":123,"y":45,"t":1.20}
                         # x/y (and every recorded rect) are top-document
                         # page-viewport coordinates, matching the frames.
                         # right_click uses the same point; drag also carries
                         # end_x/end_y for its independently resolved endpoint.
                         # {"i":1,"kind":"type","text":"...","param":"note",...}
                         # TYPE events also carry "field_label" (the receiving
                         # field's DOM/a11y label, captured passively when the
                         # backend exposes it) and, from the interactive
                         # recorder, "field_rect" (the field's viewport rect,
                         # for the compiler's nearby-OCR label fallback).
                         # {"i":2,"kind":"key","key":"Enter","t":3.10}
                         # hotkey carries explicit modifiers + trigger key.
                         # {"i":3,"kind":"scroll","dx":0,"dy":400,"t":4.02}
                         # Events additionally carry url/title/pages
                         # _before/_after keys when the backend exposes
                         # structural observations (StructuralBackend), and
                         # sor_before/sor_after (a system-of-record snapshot)
                         # when a recorder-only observer is configured.
                         # Every frame-backed event also carries
                         # viewport_before/viewport_after from the actual PNGs.
                         # A click/double_click on a control with no
                         # structural locator also carries "submit_binding":
                         # how that control relates to the fields typed into
                         # so far (structure only; openadapt_flow.
                         # submit_binding), when the backend exposes it.
      frames/{i:04d}_before.png
      frames/{i:04d}_after.png   # captured after the action settled

Multi-surface recordings (docs/MULTI_SURFACE.md) add, only once a second
surface exists: ``"opens": [...]`` on an event whose action opened browser
pages, a ``{"kind": "switch_surface", "surface": "<id>"}`` event for every
surface change, ``"surface"`` on every later event, and ``"surfaces"`` in
``meta.json`` (each page's opening event, opener relation, URL, and title, from
which the compiler derives the replay-time patterns). A recording that never
opens a page or switches is written exactly as before.

The settle wait is implemented inline here (perceptual-hash polling) on purpose:
this module must not depend on `openadapt_flow.vision`.
"""

from __future__ import annotations

import io
import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from PIL import Image, ImageDraw

from openadapt_flow.backend import Backend, RichPointerActionBackend
from openadapt_flow.image_hash import perceptual_hash
from openadapt_flow.ir import (
    MAX_PAGES_OPENED_PER_STEP,
    PRIMARY_SURFACE_ID,
    StructuralLocator,
    structural_locator_is_locatable,
)

#: Bound on waiting for a newly opened page's first navigation.
_NEW_PAGE_LOAD_TIMEOUT_S = 10.0
# How long a recorded action that raises a browser dialog waits for it.
_DIALOG_WAIT_S = 5.0


def _has_window_opener(page: Any) -> bool:
    """Whether the page's own ``window.opener`` is set (False for noopener)."""

    from openadapt_flow.runtime.surfaces import page_has_window_opener

    return bool(page_has_window_opener(page))


def _url_without_query(url: Any) -> str:
    """``url`` with its query and fragment removed."""

    from urllib.parse import urlsplit, urlunsplit

    if not isinstance(url, str) or not url:
        return ""
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _phash(png: bytes) -> str:
    """Perceptual hash of a PNG frame."""
    with Image.open(io.BytesIO(png)) as img:
        return perceptual_hash(img)


class Recorder:
    """Records a demonstration by mirroring backend actions to disk.

    Each action captures a before frame, performs the action on the wrapped
    backend, waits for the screen to settle (polling screenshots until two
    consecutive frames have identical perceptual hashes, with a timeout),
    captures the after frame, and appends one event line to ``events.jsonl``.

    Args:
        backend: The `Backend` to act through and screenshot from.
        out_dir: Recording directory; created if missing. `finish()` returns
            this path.
        app_url: Optional app URL stored in ``meta.json``.
        settle_interval_s: Poll interval for the settle wait.
        settle_stable_frames: Consecutive identical frames required.
        settle_timeout_s: Max seconds to wait for the screen to settle.
    """

    def __init__(
        self,
        backend: Backend,
        out_dir: Path | str,
        *,
        app_url: Optional[str] = None,
        system_of_record_reader: Optional[
            Callable[[], Optional[list[dict[str, Any]]]]
        ] = None,
        settle_interval_s: float = 0.1,
        settle_stable_frames: int = 2,
        settle_timeout_s: float = 3.0,
        network_capture: Optional[Any] = None,
        surface_id: str = PRIMARY_SURFACE_ID,
        surface_kind: Optional[str] = None,
        page_backend_factory: Optional[Callable[[Any], Backend]] = None,
    ) -> None:
        # ``_backend`` is always the ACTIVE surface's backend; on a
        # single-surface recording it never changes.
        self._backend = backend
        # Opt-in ``record --capture-network``: a started
        # :class:`openadapt_flow.network_capture.NetworkCapture`, written beside
        # the events by :meth:`finish`.
        self._network_capture = network_capture
        # Builds the backend for a browser page an action opened. The
        # interactive recorder passes one that applies its privacy masking
        # and scrubbed structural reads to every page it follows.
        self._page_backend_factory = page_backend_factory
        self._primary_backend = backend
        self._primary_surface_id = surface_id
        self._primary_surface_kind = surface_kind
        self._active_surface = surface_id
        self._surface_backends: dict[str, Backend] = {surface_id: backend}
        self._surface_meta: list[dict[str, Any]] = []
        self._multi_surface = False
        # Set by expect_dialog(): how the NEXT action answers the dialog it
        # raises (docs/MULTI_SURFACE.md, "Dialogs").
        self._expected_dialog: Optional[str] = None
        self._page_counters = {"popup": 0, "tab": 0}
        self._known_pages: list[Any] = []
        self._remember_pages(backend)
        self._dir = Path(out_dir)
        self._frames_dir = self._dir / "frames"
        self._frames_dir.mkdir(parents=True, exist_ok=True)
        self._events_path = self._dir / "events.jsonl"
        self._events_path.write_text("")  # truncate any stale file
        self._app_url = app_url
        # This observer belongs to recording, not the execution backend. A GUI
        # backend must never advertise an authoritative business-effect oracle
        # merely because the recorder can query a separate read-only source.
        self._system_of_record_reader = system_of_record_reader
        self._settle_interval_s = settle_interval_s
        self._settle_stable_frames = settle_stable_frames
        self._settle_timeout_s = settle_timeout_s
        self._params: dict[str, str] = {}
        self._secret_params: set[str] = set()
        self._i = 0
        self._t0 = time.monotonic()

    # -- action API (mirrors Backend, plus param-aware type_text) -----------

    def click(self, x: int, y: int) -> None:
        """Click at pixel coordinates, recording the event and frames."""
        self._record(
            {"kind": "click", "x": int(x), "y": int(y)},
            lambda: self._backend.click(int(x), int(y)),
        )

    def double_click(self, x: int, y: int) -> None:
        """Double-click at pixel coordinates, recording the event."""
        self._record(
            {"kind": "double_click", "x": int(x), "y": int(y)},
            lambda: self._backend.click(int(x), int(y), double=True),
        )

    def right_click(self, x: int, y: int) -> None:
        """Right-click at pixel coordinates, recording the event."""

        if not isinstance(self._backend, RichPointerActionBackend):
            raise RuntimeError("recording backend does not support right-click input")
        backend = self._backend
        self._record(
            {"kind": "right_click", "x": int(x), "y": int(y)},
            lambda: backend.right_click(int(x), int(y)),
        )

    def drag(self, x: int, y: int, end_x: int, end_y: int) -> None:
        """Drag between two points, retaining both endpoints as evidence."""

        if not isinstance(self._backend, RichPointerActionBackend):
            raise RuntimeError("recording backend does not support drag input")
        backend = self._backend
        self._record(
            {
                "kind": "drag",
                "x": int(x),
                "y": int(y),
                "end_x": int(end_x),
                "end_y": int(end_y),
            },
            lambda: backend.drag(int(x), int(y), int(end_x), int(end_y)),
        )

    def type_text(self, text: str, param: Optional[str] = None) -> None:
        """Type text into the focused element, recording the event.

        The receiving field's best available label is captured PASSIVELY when
        the backend exposes the ``focused_field_label`` seam
        (:class:`openadapt_flow.backend.FieldLabelBackend`) and stored as
        ``field_label`` on the event -- pure evidence for the compile-time
        parameter-proposal pass (no behavior change, no prompts, no model
        calls; see ``compiler.annotate.FieldLabelAnnotator``).

        Args:
            text: The literal text typed during the demonstration.
            param: If set, the typed value is a workflow parameter with this
                name; it is included on the event and in ``meta.json`` params.
        """
        event: dict[str, Any] = {"kind": "type", "text": text}
        # Read the label BEFORE acting: focus is already on the receiving
        # field (the demonstrator clicked it), and typing may navigate.
        field_label = self._focused_field_label()
        if field_label:
            event["field_label"] = field_label
        if param is not None:
            event["param"] = param
            self._params[param] = text
        self._record(event, lambda: self._backend.type_text(text))

    def press(self, key: str) -> None:
        """Press a key or chord (e.g. ``'Enter'``), recording the event."""
        self._record({"kind": "key", "key": key}, lambda: self._backend.press(key))

    def hotkey(self, modifiers: list[str], key: str) -> None:
        """Press and record an explicit modifier chord."""

        chord = "+".join([*modifiers, key])
        self._record(
            {"kind": "hotkey", "modifiers": list(modifiers), "key": key},
            lambda: self._backend.press(chord),
        )

    def scroll(self, dx: int, dy: int) -> None:
        """Scroll by (dx, dy) pixels via the wheel, recording the event."""
        self._record(
            {"kind": "scroll", "dx": int(dx), "dy": int(dy)},
            lambda: self._backend.scroll(int(dx), int(dy)),
        )

    # -- lifecycle -----------------------------------------------------------

    def finish(self) -> Path:
        """Write ``meta.json`` and return the recording directory."""
        viewport = self._primary_backend.viewport
        meta: dict[str, Any] = {
            "id": uuid.uuid4().hex,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "viewport": [int(viewport[0]), int(viewport[1])],
            # Every recorded x/y/rect is top-document (page viewport) space,
            # the space page.screenshot() captures. Events that happen inside
            # an iframe are composed into this space at capture time or
            # refused. A recording without this marker does not declare its
            # coordinate space: a framed target's point in one may be
            # frame-local.
            "coordinate_space": "page",
            "app_url": self._app_url,
            "params": dict(self._params),
            "secret_params": sorted(self._secret_params),
        }
        if self._network_capture is not None:
            from openadapt_flow.network_capture import NETWORK_FILE

            self._network_capture.stop()
            self._network_capture.write(self._dir, self._t0)
            meta["network_capture"] = {
                "file": NETWORK_FILE,
                "exchanges": self._network_capture.count,
            }
        if self._multi_surface:
            meta["surfaces"] = [
                {
                    "id": self._primary_surface_id,
                    "kind": self._primary_kind(),
                },
                *self._surface_meta,
            ]
        (self._dir / "meta.json").write_text(json.dumps(meta, indent=2))
        return self._dir

    # -- multi-surface (docs/MULTI_SURFACE.md) ---------------------------------

    @property
    def active_surface(self) -> str:
        """Id of the surface the next action is recorded on."""

        return self._active_surface

    @property
    def surface_ids(self) -> tuple[str, ...]:
        """Every surface recorded so far, primary first."""

        return tuple(self._surface_backends)

    def surface_backend(self, surface_id: str) -> Backend:
        """The backend recorded for ``surface_id`` (a page opened by a step)."""

        return self._surface_backends[surface_id]

    def surface_for_page(self, page: Any) -> Optional[str]:
        """The surface id recorded for browser ``page``, or None."""

        for surface_id, backend in self._surface_backends.items():
            if self._browser_page(backend) is page:
                return surface_id
        return None

    def knows_page(self, page: Any) -> bool:
        """Whether ``page`` was open at the start or claimed by an action."""

        return any(page is known for known in self._known_pages)

    def expect_dialog(self, response: str = "accept") -> None:
        """The next action raises a browser dialog; answer it with ``response``.

        Browser backend only. The dialog's message is recorded as its identity
        and the compiled step answers only a dialog that shows that message.
        """

        if response not in ("accept", "dismiss"):
            raise ValueError("a dialog response is 'accept' or 'dismiss'")
        self._expected_dialog = response

    def switch_surface(
        self,
        surface_id: str,
        *,
        backend: Optional[Backend] = None,
        kind: Optional[str] = None,
        before_png: Optional[bytes] = None,
        structural_before: Optional[dict[str, Any]] = None,
    ) -> None:
        """Record moving to another surface (a tab/popup, or another backend).

        A page some earlier action opened is switched to by its id (see
        ``surface_ids``). Another backend (a native app, an RDP/Citrix window)
        is passed in with ``backend``; its kind is read from the backend class
        or given as ``kind``. The event's before frame is the old surface and
        its after frame the new one, settled.
        """

        import re as _re

        from openadapt_flow.ir import SURFACE_ID_PATTERN

        if surface_id == self._active_surface:
            raise ValueError(f"surface {surface_id!r} is already active")
        if backend is None:
            if surface_id not in self._surface_backends:
                raise ValueError(
                    f"surface {surface_id!r} was not opened in this recording; "
                    "pass backend= to record a switch to another backend"
                )
            target = self._surface_backends[surface_id]
        else:
            if surface_id in self._surface_backends:
                if self._surface_backends[surface_id] is not backend:
                    raise ValueError(f"surface {surface_id!r} is already recorded")
            elif not _re.fullmatch(SURFACE_ID_PATTERN, surface_id):
                raise ValueError(f"invalid surface id {surface_id!r}")
            else:
                self._surface_meta.append(
                    {"id": surface_id, "kind": self._kind_of(backend, kind)}
                )
                self._surface_backends[surface_id] = backend
                self._remember_pages(backend)
            target = backend
        # A caller that observed the switch (the interactive recorder) captured
        # the old surface already; a driven recording captures it now.
        before = before_png if before_png is not None else self._backend.screenshot()
        if structural_before is None:
            structural_before = self._structural_state()
        self._multi_surface = True
        self._backend = target
        self._active_surface = surface_id
        page = self._browser_page(target)
        if page is not None:
            try:
                page.bring_to_front()
            except Exception:
                pass
        after = self._wait_settled()
        self._commit(
            {"kind": "switch_surface", "surface": surface_id},
            before,
            after,
            structural_before,
        )

    def _primary_kind(self) -> str:
        return self._kind_of(self._primary_backend, self._primary_surface_kind)

    @staticmethod
    def _kind_of(backend: Backend, kind: Optional[str]) -> str:
        from openadapt_flow.runtime.surfaces import backend_surface_kinds

        if kind is not None:
            return kind
        kinds = backend_surface_kinds(backend)
        if len(kinds) != 1:
            raise ValueError(
                "cannot tell this backend's surface kind; pass kind= "
                "(web/windows/macos/linux/rdp/citrix)"
            )
        return next(iter(kinds))

    @staticmethod
    def _browser_page(backend: Any) -> Any:
        page = getattr(backend, "page", None)
        if page is None or not hasattr(page, "context"):
            return None
        return page

    def _context_pages(self, backend: Any) -> list[Any]:
        page = self._browser_page(backend)
        if page is None:
            return []
        try:
            return list(page.context.pages)
        except Exception:
            return [page]

    def _remember_pages(self, backend: Any) -> None:
        for page in self._context_pages(backend):
            if not any(page is known for known in self._known_pages):
                self._known_pages.append(page)

    def _claim_new_pages(self, event_index: int) -> list[str]:
        """Record every page opened since the last check, in creation order."""

        new_pages = [
            page
            for page in self._context_pages(self._backend)
            if not any(page is known for known in self._known_pages)
        ]
        opener_page = self._browser_page(self._backend)

        def _opened_by_another_known_page(page: Any) -> bool:
            # A page another recorded page opened belongs to THAT page's
            # action; it is claimed when that action is recorded. (The
            # interactive recorder can see a new page before it has written
            # the pending action of the surface the user just left.)
            try:
                live_opener = page.opener()
            except Exception:
                return False
            return (
                live_opener is not None
                and live_opener is not opener_page
                and any(live_opener is known for known in self._known_pages)
            )

        new_pages = [p for p in new_pages if not _opened_by_another_known_page(p)]
        if not new_pages:
            return []
        if len(new_pages) > MAX_PAGES_OPENED_PER_STEP:
            raise RuntimeError(
                f"one action opened {len(new_pages)} browser pages; at most "
                f"{MAX_PAGES_OPENED_PER_STEP} can be recorded per step"
            )
        from openadapt_flow.backends.playwright_backend import PlaywrightBackend

        ids: list[str] = []
        for order, page in enumerate(new_pages):
            self._known_pages.append(page)
            try:
                page.wait_for_load_state(
                    "domcontentloaded", timeout=_NEW_PAGE_LOAD_TIMEOUT_S * 1000
                )
            except Exception:
                pass
            try:
                live_opener = page.opener()
            except Exception:
                live_opener = None
            link = "window_opener" if _has_window_opener(page) else "none"
            if live_opener is not None and live_opener is not opener_page:
                raise RuntimeError(
                    "a recorded page was opened by a page that is not the "
                    "active surface; refusing to guess its opener"
                )
            prefix = "popup" if link == "window_opener" else "tab"
            self._page_counters[prefix] += 1
            surface_id = f"{prefix}_{self._page_counters[prefix]}"
            try:
                url = _url_without_query(page.url)
            except Exception:
                url = ""
            # meta.json keeps only what identifies the page at replay: the
            # URL without its query or fragment (OAuth code/state, search
            # terms), and no title (titles carry record names).
            self._surface_meta.append(
                {
                    "id": surface_id,
                    "kind": "web",
                    "opened_by_event": event_index,
                    "opener": self._active_surface,
                    "open_order": order,
                    "opener_link": link,
                    "url": url,
                }
            )
            self._surface_backends[surface_id] = (
                self._page_backend_factory(page)
                if self._page_backend_factory is not None
                else PlaywrightBackend(page, closed_page_frame=True)
            )
            ids.append(surface_id)
        self._multi_surface = True
        return ids

    @property
    def event_count(self) -> int:
        """Number of complete events already persisted."""

        return self._i

    # -- internals -----------------------------------------------------------

    def _record(self, event: dict[str, Any], act: Callable[[], None]) -> None:
        """Capture before frame, act, wait settle, capture after, log event."""
        before = self._backend.screenshot()
        structural_before = self._structural_state()
        # Structured identity of the clicked target (DOM / a11y text), when
        # the backend exposes it: captured on the BEFORE frame, before the
        # action, so the compiler can store it on the anchor and the replayer
        # can verify identity against the highest-fidelity signal (no OCR
        # ambiguity). Pointer events only (they carry x/y); absent otherwise.
        if event.get("kind") in (
            "click",
            "double_click",
            "right_click",
            "drag",
        ):
            # Structural locator (DOM selector / role+name, or UIA identifiers)
            # for the clicked element, when the backend exposes it
            # (StructuralActionBackend). Stored on the event so the compiler can
            # put it on the anchor and the replayer's structural ACTION rung can
            # re-find the SAME element deterministically (no pixel match). Absent
            # on pixel-only backends; resolution then uses the visual anchor.
            locator = self._structural_locator_at(int(event["x"]), int(event["y"]))
            if locator is not None:
                event = {
                    **event,
                    "structural": locator.model_dump(exclude_none=True),
                }
            structured = self._structured_identity_at(int(event["x"]), int(event["y"]))
            if structured:
                event = {**event, "structured_identity": structured}
            # An unlabeled control has no structural locator. Observe how it
            # relates to the fields typed into so far (form ownership, submit
            # role, position, uniqueness) so the compiler can bind it to the
            # form it submits. Before the action: the click may submit it.
            if not structural_locator_is_locatable(locator) and event.get("kind") in (
                "click",
                "double_click",
            ):
                binding = self._submit_binding_at(int(event["x"]), int(event["y"]))
                if binding:
                    event = {**event, "submit_binding": binding}
        dialog_response, self._expected_dialog = self._expected_dialog, None
        if dialog_response is not None:
            arm = getattr(self._backend, "arm_dialog", None)
            if not callable(arm):
                raise RuntimeError(
                    "this backend cannot see browser dialogs; on a screen-driven "
                    "backend record the clicks that answer the dialog instead"
                )
            arm(dialog_response, None)
        act()
        if dialog_response is not None:
            seen = self._backend.take_dialogs(_DIALOG_WAIT_S)  # type: ignore[attr-defined]
            if len(seen) != 1:
                raise RuntimeError(
                    f"the action raised {len(seen)} browser dialogs; exactly one "
                    "was expected"
                )
            event = {
                **event,
                "dialog": {
                    "response": dialog_response,
                    "message": str(seen[0]["message"]),
                },
            }
        after = self._wait_settled()
        opened = self._claim_new_pages(self._i)
        if opened:
            event = {**event, "opens": opened}
        self._commit(event, before, after, structural_before)

    def record_observed(
        self,
        event: dict[str, Any],
        *,
        before_png: bytes,
        structural_before: dict[str, Any],
        structured_identity: Optional[str] = None,
        param: Optional[str] = None,
        secret: bool = False,
        redact_region: Optional[tuple[int, int, int, int]] = None,
        after_png: Optional[bytes] = None,
        structural_after: Optional[dict[str, Any]] = None,
    ) -> None:
        """Persist an event the USER already performed (no backend action).

        The driving methods (``click`` / ``type_text`` / ...) perform the
        action and screenshot around it. ``record_observed`` instead records
        an action the caller OBSERVED in a live session the user is driving:
        the action already happened, so nothing is performed on the backend.

        Frame chaining mirrors a driven demonstration: ``before_png`` is the
        pre-action frame the caller supplies (the previous step's settled
        frame — the screen the user saw before acting), and the after frame is
        captured now, once the screen settles.

        Args:
            event: The event dict (``{"kind": ..., ...}``) without ``i``/``t``.
            before_png: Pre-action frame (the previous settled frame).
            structural_before: URL/title/page-count observed before the action
                (captured in-page at action time, pre-navigation).
            structured_identity: DOM/a11y identity of the clicked row, captured
                in-page at click time; stored on click/double_click events.
            param: Parameter name for a TYPE event, if any.
            secret: TYPE only. When True the value is a SECRET: the event
                carries NO ``text`` (never persisted), ``param`` is registered
                as a secret parameter, and the value is injected from the
                environment at replay (see ir.Step.secret).
            redact_region: (x, y, w, h) blacked out in BOTH the before and
                after frames before they are written — a secret field's pixels
                must never persist to disk.
            after_png: Pre-captured settled after-frame. When None, the screen
                is settled and captured now.
        """
        event = dict(event)
        if (
            event.get("kind")
            in (
                "click",
                "double_click",
                "right_click",
                "drag",
            )
            and structured_identity
        ):
            event["structured_identity"] = structured_identity
        if param is not None:
            event["param"] = param
            if secret:
                event["secret"] = True
                event.pop("text", None)
                self._secret_params.add(param)
            else:
                self._params[param] = str(event.get("text", ""))
        # A caller that already captured the settled after-frame at the right
        # moment (e.g. a typed field's value BEFORE a following navigating
        # click) passes it in; otherwise we settle and capture now.
        if after_png is None:
            after_png = self._wait_settled()
        # The observed action may have opened browser pages (a popup, a
        # target=_blank tab): claim them for this event, exactly as a driven
        # action does. A non-browser backend has no pages, so nothing changes.
        opened = self._claim_new_pages(self._i)
        if opened:
            event = {**event, "opens": opened}
        self._commit(
            event,
            before_png,
            after_png,
            structural_before,
            redact_region=redact_region,
            structural_after=structural_after,
        )

    def _commit(
        self,
        event: dict[str, Any],
        before_png: bytes,
        after_png: bytes,
        structural_before: dict[str, Any],
        *,
        redact_region: Optional[tuple[int, int, int, int]] = None,
        structural_after: Optional[dict[str, Any]] = None,
    ) -> None:
        """Write this step's before/after frames and its events.jsonl line.

        ``redact_region`` (when given) is blacked out in both frames before
        they hit disk — the single choke point that keeps a secret field's
        pixels out of every persisted frame. ``structural_after`` lets a
        caller supply the post-action URL/title/page-count captured at the
        right moment (a deferred type-run flush must not read a URL a LATER
        navigating click produced); when None it is read now.
        """
        i = self._i
        if redact_region is not None:
            before_png = self._redact(before_png, redact_region)
            after_png = self._redact(after_png, redact_region)
        (self._frames_dir / f"{i:04d}_before.png").write_bytes(before_png)
        (self._frames_dir / f"{i:04d}_after.png").write_bytes(after_png)
        with Image.open(io.BytesIO(before_png)) as before_image:
            before_viewport = [int(before_image.width), int(before_image.height)]
        with Image.open(io.BytesIO(after_png)) as after_image:
            after_viewport = [int(after_image.width), int(after_image.height)]
        line: dict[str, Any] = {
            "i": i,
            **event,
            "viewport_before": before_viewport,
            "viewport_after": after_viewport,
        }
        if self._multi_surface and event.get("kind") != "switch_surface":
            line["surface"] = self._active_surface
        for key, value in structural_before.items():
            line[f"{key}_before"] = value
        if structural_after is None:
            structural_after = self._structural_state()
        for key, value in structural_after.items():
            line[f"{key}_after"] = value
        line["t"] = round(time.monotonic() - self._t0, 3)
        with self._events_path.open("a") as f:
            f.write(json.dumps(line) + "\n")
        self._i += 1

    def _structural_locator_at(self, x: int, y: int) -> Optional[StructuralLocator]:
        """Stable structural locator for the element under (x, y), if any.

        Backends MAY expose ``structural_locator_at``
        (:class:`openadapt_flow.backend.StructuralActionBackend`): a stable DOM
        selector / role+name (browser) or UIA AutomationId / role+name (native)
        the replayer can re-resolve to act on the SAME element deterministically.
        Pixel-only backends (no such method) or a momentary failure yield None,
        and the step relies on the visual anchor alone.
        """
        getter = getattr(self._backend, "structural_locator_at", None)
        if getter is None:
            return None
        try:
            return getter(int(x), int(y))
        except Exception:
            return None

    def _submit_binding_at(self, x: int, y: int) -> Optional[dict[str, Any]]:
        """Submit-binding observation for the control under (x, y), if any.

        Backends MAY expose ``submit_binding_at``
        (:class:`openadapt_flow.backend.SubmitBindingBackend`). Pixel-only
        backends or a momentary failure yield None; the compiler then records
        no binding and the runtime keeps refusing a consequential click on an
        unlabeled control it cannot tie to a record.
        """
        getter = getattr(self._backend, "submit_binding_at", None)
        if getter is None:
            return None
        try:
            observation = getter(int(x), int(y))
        except Exception:
            return None
        return observation if isinstance(observation, dict) else None

    @staticmethod
    def _redact(png: bytes, region: tuple[int, int, int, int]) -> bytes:
        """Return ``png`` with ``region`` (x, y, w, h) filled solid black."""
        x, y, w, h = (int(v) for v in region)
        with Image.open(io.BytesIO(png)) as img:
            frame = img.convert("RGB")
        ImageDraw.Draw(frame).rectangle(
            [max(0, x), max(0, y), x + w, y + h], fill=(0, 0, 0)
        )
        out = io.BytesIO()
        frame.save(out, format="PNG")
        return out.getvalue()

    def _focused_field_label(self) -> Optional[str]:
        """Best available label of the focused field, if the backend can say.

        Backends MAY expose ``focused_field_label``
        (:class:`openadapt_flow.backend.FieldLabelBackend`): the focused
        field's DOM/accessibility label, captured as passive TYPE-event
        evidence so the compiler can PROPOSE (never silently apply) a
        human-named parameter for the typed value. Pixel-only backends (no
        such method) or a momentary failure yield None; recording proceeds
        unchanged either way.
        """
        getter = getattr(self._backend, "focused_field_label", None)
        if getter is None:
            return None
        try:
            label = getter()
        except Exception:
            return None
        if not isinstance(label, str):
            return None
        label = " ".join(label.split())
        return label or None

    def _structured_identity_at(self, x: int, y: int) -> Optional[str]:
        """Structured (DOM / a11y) identity text under (x, y), if available.

        Backends MAY expose ``structured_text_at``
        (``openadapt_flow.backend.IdentityBackend``): the REAL characters of
        the target's row from the DOM / accessibility tree, captured so replay
        verifies identity by exact/normalized compare with no OCR ambiguity.
        Pixel-only backends (no such method) or a momentary failure yield
        None, and identity falls back to the OCR context band.
        """
        getter = getattr(self._backend, "structured_text_at", None)
        if getter is None:
            return None
        try:
            return getter(int(x), int(y))
        except Exception:
            return None

    def _structural_state(self) -> dict[str, Any]:
        """Structural observations the backend can provide right now.

        Backends MAY expose ``url`` / ``page_title`` / ``page_count`` (see
        ``openadapt_flow.backend.StructuralBackend``); whatever is available
        is captured per event so the compiler can mine structural
        postconditions (URL/title change, new tab) for steps whose action
        changed nothing visible in the frame. Missing observations are
        simply absent from the event.
        """
        state: dict[str, Any] = {}
        for attr, key in (
            ("url", "url"),
            ("page_title", "title"),
            ("page_count", "pages"),
        ):
            try:
                value = getattr(self._backend, attr, None)
            except Exception:
                value = None
            if value is not None:
                state[key] = value
        if self._system_of_record_reader is not None:
            try:
                records = self._system_of_record_reader()
            except Exception:
                records = None
            if records is not None:
                state["sor"] = records
        return state

    def _wait_settled(self) -> bytes:
        """Poll screenshots until N consecutive identical hashes or timeout.

        Returns:
            The last PNG frame captured (settled if achieved before timeout).
        """
        deadline = time.monotonic() + self._settle_timeout_s
        png = self._backend.screenshot()
        prev = _phash(png)
        consecutive = 1
        while consecutive < self._settle_stable_frames and time.monotonic() < deadline:
            time.sleep(self._settle_interval_s)
            png = self._backend.screenshot()
            cur = _phash(png)
            if cur == prev:
                consecutive += 1
            else:
                consecutive = 1
            prev = cur
        return png
