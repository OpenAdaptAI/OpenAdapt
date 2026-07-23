"""Recorder: wraps a Backend and captures frames + events for each action.

Recording format (DESIGN.md):

    <recording>/
      meta.json          # {"id", "created_at", "viewport": [w,h], "app_url",
                         #  "params": {"<param_name>": "<value typed>"}}
      events.jsonl       # {"i":0,"kind":"click","x":123,"y":45,"t":1.20}
                         # {"i":1,"kind":"type","text":"...","param":"note",...}
                         # {"i":2,"kind":"key","key":"Enter","t":3.10}
                         # {"i":3,"kind":"scroll","dx":0,"dy":400,"t":4.02}
                         # Events additionally carry url/title/pages
                         # _before/_after keys when the backend exposes
                         # structural observations (StructuralBackend), and
                         # sor_before/sor_after (a system-of-record snapshot)
                         # when it exposes SystemOfRecordBackend.
      frames/{i:04d}_before.png
      frames/{i:04d}_after.png   # captured after the action settled

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

from openadapt_flow.backend import Backend
from openadapt_flow.image_hash import perceptual_hash
from openadapt_flow.ir import StructuralLocator


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
        settle_interval_s: float = 0.1,
        settle_stable_frames: int = 2,
        settle_timeout_s: float = 3.0,
    ) -> None:
        self._backend = backend
        self._dir = Path(out_dir)
        self._frames_dir = self._dir / "frames"
        self._frames_dir.mkdir(parents=True, exist_ok=True)
        self._events_path = self._dir / "events.jsonl"
        self._events_path.write_text("")  # truncate any stale file
        self._app_url = app_url
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

    def type_text(self, text: str, param: Optional[str] = None) -> None:
        """Type text into the focused element, recording the event.

        Args:
            text: The literal text typed during the demonstration.
            param: If set, the typed value is a workflow parameter with this
                name; it is included on the event and in ``meta.json`` params.
        """
        event: dict[str, Any] = {"kind": "type", "text": text}
        if param is not None:
            event["param"] = param
            self._params[param] = text
        self._record(event, lambda: self._backend.type_text(text))

    def press(self, key: str) -> None:
        """Press a key or chord (e.g. ``'Enter'``), recording the event."""
        self._record({"kind": "key", "key": key}, lambda: self._backend.press(key))

    def scroll(self, dx: int, dy: int) -> None:
        """Scroll by (dx, dy) pixels via the wheel, recording the event."""
        self._record(
            {"kind": "scroll", "dx": int(dx), "dy": int(dy)},
            lambda: self._backend.scroll(int(dx), int(dy)),
        )

    # -- lifecycle -----------------------------------------------------------

    def finish(self) -> Path:
        """Write ``meta.json`` and return the recording directory."""
        viewport = self._backend.viewport
        meta = {
            "id": uuid.uuid4().hex,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "viewport": [int(viewport[0]), int(viewport[1])],
            "app_url": self._app_url,
            "params": dict(self._params),
            "secret_params": sorted(self._secret_params),
        }
        (self._dir / "meta.json").write_text(json.dumps(meta, indent=2))
        return self._dir

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
        if event.get("kind") in ("click", "double_click"):
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
        act()
        after = self._wait_settled()
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
        if event.get("kind") in ("click", "double_click") and structured_identity:
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
        line: dict[str, Any] = {"i": i, **event}
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
            # System-of-record snapshot (openadapt_flow.backend.
            # SystemOfRecordBackend): the app's authoritative records right
            # now, captured before/after each event so the compiler's effect
            # miner can derive record_written/field_equals from the delta. A
            # list value flows through unchanged (a list is not None).
            ("system_of_record", "sor"),
        ):
            try:
                value = getattr(self._backend, attr, None)
            except Exception:
                value = None
            if value is not None:
                state[key] = value
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
