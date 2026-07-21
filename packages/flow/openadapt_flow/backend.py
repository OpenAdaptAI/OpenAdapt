"""Backend protocol: the only interface the runtime uses to touch a GUI.

The runtime is vision-only by construction — it sees PNG bytes and emits
clicks/keys at pixel coordinates. Anything that can screenshot and inject
input can be a backend: a Playwright page (reference/test backend), a native
OS layer (pyautogui/Quartz), or an RDP session.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional, Protocol, runtime_checkable

if TYPE_CHECKING:  # pragma: no cover
    from openadapt_flow.ir import (
        ActionDeliveryReceipt,
        StructuralHandle,
        StructuralLocator,
    )


class StructuralResolutionRefused(RuntimeError):
    """A structural backend found candidates but could not prove uniqueness.

    This is a safety refusal, not an ordinary miss. The resolver must not hide
    it by falling through to a weaker visual rung that could choose one of the
    same ambiguous controls.
    """


@runtime_checkable
class SystemOfRecordBackend(Protocol):
    """Optional system-of-record observation a backend MAY expose.

    Vision (and even structural URL/title) cannot see whether a consequential
    write actually reached the system of record — a partial save, a phantom
    optimistic-UI success, a duplicate submission all look identical on screen
    (``docs/LIMITS.md`` "5 of 7 write faults silent"). A backend that can read
    the app's authoritative store (a JSON ``/api/db`` endpoint, an EMR's own
    API) exposes it here; the recorder snapshots it before and after each event
    (``sor_before`` / ``sor_after`` on the event, exactly as it already records
    ``url_before`` / ``url_after``), and the compiler's effect miner
    (``compiler.effect_mining``) derives typed ``record_written`` /
    ``field_equals`` effects from the observed delta.

    Backends without a readable system of record (pixel-only substrates) simply
    do not implement this; the miner then falls back to a flagged placeholder
    or an honest "no verifiable effect derivable" (never a fabricated binding).
    """

    @property
    def system_of_record(self) -> Optional[list[dict[str, Any]]]:
        """Current system-of-record records, or None if unobservable.

        None (not ``[]``) when the store cannot be read right now — the miner
        distinguishes "not observed" from "observed empty" (a legitimate
        baseline for a first write).
        """
        ...


@runtime_checkable
class StructuralBackend(Protocol):
    """Optional structural observations a backend MAY expose.

    Vision alone cannot see effects that never render in the frame — a
    new-tab click, an SPA route change below the fold. Backends that can
    cheaply observe URL / title / page count expose these read-only
    properties; the recorder captures them per event and the compiler mines
    *structural* postconditions (URL_CHANGED, TITLE_CHANGED, NEW_TAB_OPENED)
    as a fallback for steps that would otherwise assert nothing. Backends
    without these observations (native OS, RDP) simply don't implement them;
    such steps stay honestly unverified (docs/LIMITS.md).

    Each property returns None when the observation is momentarily
    unavailable (e.g. mid-navigation).
    """

    @property
    def url(self) -> Optional[str]:
        """Current page URL, or None if unobservable."""
        ...

    @property
    def page_title(self) -> Optional[str]:
        """Current page title, or None if unobservable."""
        ...

    @property
    def page_count(self) -> Optional[int]:
        """Number of open pages/tabs, or None if unobservable."""
        ...


@runtime_checkable
class IdentityBackend(Protocol):
    """Optional STRUCTURED-TEXT identity capability a backend MAY expose.

    The runtime resolves targets by VISION alone (screenshot in, clicks out);
    that never changes. But *identity verification* -- proving the resolved
    target is the recorded entity, not a look-alike sibling -- does not have
    to be OCR-based when the backend can hand back the real, structured
    characters under a point.

    An adversarial review proved the OCR-only identity path cannot close the
    same-name / same-DOB glyph-collapse case: two DIFFERENT patients whose MRN
    differs only by an O/0 or l/1 glyph ("MG4408" vs "MG44O8") render to a
    byte-identical OCR band -- the same input a legit re-read produces -- so
    no function downstream of OCR can distinguish them (see docs/LIMITS.md and
    benchmark/dense_surface/DENSE_SURFACE.md). The escape is to stop relying
    on OCR for identity where a higher-fidelity signal exists.

    ``structured_text_at`` returns that higher-fidelity signal: the accessible
    / DOM text at (or around) a coordinate, in the REAL characters --
    "MG4408" with a genuine digit 0, not an OCR guess. Backends implement it
    from whatever structured layer they own:

    - a browser backend (Playwright) reads the DOM element under the point
      (``elementFromPoint`` -> row/cell ``textContent`` + ``aria-label``);
    - a native desktop backend reads the accessibility tree -- Windows UI
      Automation ``Name``/``Value``/text, macOS AX attributes, or Linux AT-SPI
      accessible text. Crucially,
      an element lacking a stable ``AutomationId`` usually STILL exposes
      Name/Value text, so UIA/AX identity is viable on most native apps even
      where an AutomationId-keyed selector is not.

    The identity ladder treats DOM and UIA/AX text identically -- both are
    "structured text". A pure-pixel substrate (Citrix/RDP/VDI, or a backend
    with no a11y tree) returns None from every point, and identity falls back
    to the OCR name+DOB-primary tier (docs/LIMITS.md). This is an ADDITIVE
    identity capability: the 4-method vision resolution protocol
    (:class:`Backend`) is unchanged.
    """

    def structured_text_at(self, x: int, y: int) -> Optional[str]:
        """Return the structured (DOM / a11y) text at/around pixel (x, y).

        The coordinate space matches :meth:`Backend.click` -- the same pixels
        the resolver emits. Returns the target's row/element text in its REAL
        characters, or None when the backend cannot observe structured text at
        that point (pixel-only substrate, no a11y node, or a momentary
        failure -- never raises).
        """
        ...


@runtime_checkable
class StructuralActionBackend(Protocol):
    """Optional STRUCTURAL action capability a backend MAY expose.

    The runtime resolves targets by a LADDER (see
    :mod:`openadapt_flow.runtime.resolver`). Its top rung is structural: where
    the backend owns a structured layer (a browser's DOM, a native app's UIA/AX
    tree) the runtime re-finds the recorded target as an ELEMENT and acts on its
    center DETERMINISTICALLY, instead of pixel-matching a template that render
    drift (relabel, theme, zoom, layout shift) can defeat. The desktop benchmark
    measured UIA execution 21/21 vs compiled visual replay 6/21 under drift.

    This is the thesis shift from "vision-only" to "deterministic compiled
    automation with visual FALLBACK". It is ADDITIVE and backend-optional: a
    pixel-only substrate (RDP/Citrix/canvas, or a backend with no structured
    layer) simply does not implement it, and resolution falls through to the
    visual rungs (template/ocr/geometry) UNCHANGED -- the healthcare/Citrix
    floor is never removed.

    Crucially, the structurally-resolved point flows through the IDENTICAL click
    path as any visual resolution, so the pre-click identity gate and the
    irreversible risk gate still fire on it: structure makes identity STRONGER
    (an exact element), it never bypasses it.

    Two methods, mirroring the record/replay split of the identity capability
    (:class:`IdentityBackend`):

    - ``structural_locator_at`` runs at RECORD time: given the demonstrated
      click point, return a STABLE structural locator the runtime can re-resolve
      later (a DOM ``#id`` / role+name, a UIA ``AutomationId`` / role+name,
      or an AT-SPI accessible ID / role+name).
    - ``locate_structural`` runs at REPLAY time: given that recorded locator,
      find the element on the LIVE surface and return its center point.

    Each returns None when the capability is momentarily unavailable, the
    element is absent/ambiguous, or the substrate has no structured layer at
    that point (never raises) -- resolution then uses the visual ladder.
    """

    def structural_locator_at(self, x: int, y: int) -> Optional["StructuralLocator"]:
        """Return a stable structural locator for the element at pixel (x, y).

        The coordinate space matches :meth:`Backend.click`. Returns None when
        the backend cannot derive a stable locator (no structured node under the
        point, or nothing that identifies the element durably) -- the step then
        relies on the visual anchor alone.
        """
        ...

    def locate_structural(
        self, locator: "StructuralLocator"
    ) -> Optional["StructuralHandle"]:
        """Locate ``locator``'s unique element on the live surface.

        A backend MUST raise :class:`StructuralResolutionRefused` when one or
        more candidates exist but uniqueness cannot be established. ``None``
        is reserved for an ordinary miss/unavailable structural substrate, so
        the runtime cannot hide ambiguity by falling through to visual match.
        """
        ...


@runtime_checkable
class NativeStructuralActionBackend(Protocol):
    """Optional native element actuation after governed structural resolution.

    ``act_structural`` re-resolves the exact locator, requires the candidate
    fingerprint returned by ``locate_structural``, and uses the strongest UIA
    pattern available. Its receipt proves input delivery only; postconditions
    and independent effects remain authoritative for outcome verification.
    """

    def act_structural(
        self,
        locator: "StructuralLocator",
        handle: "StructuralHandle",
        *,
        double: bool = False,
    ) -> "ActionDeliveryReceipt":
        """Deliver a native action to the same unique structural candidate."""
        ...


@runtime_checkable
class TextValueBackend(Protocol):
    """Optional exact readback for the editable control under a point.

    A screenshot/OCR verifier cannot reliably distinguish a long unrendered
    value from password dots or platform-specific glyph noise. Backends with a
    structural accessibility surface can return the control's current value so
    the runtime can confirm text delivery without weakening to pixel change.
    The value is compared in memory only and must never be logged.
    """

    def text_value_at(self, x: int, y: int) -> Optional[str]:
        """Return the exact editable value, or None when unavailable."""
        ...

    def focused_text_value(self) -> Optional[str]:
        """Return the exact focused editable value, or None."""
        ...


@runtime_checkable
class Backend(Protocol):
    @property
    def viewport(self) -> tuple[int, int]:
        """(width, height) of the screen surface in pixels."""
        ...

    def screenshot(self) -> bytes:
        """Return the current frame as PNG bytes."""
        ...

    def click(self, x: int, y: int, *, double: bool = False) -> None: ...

    def type_text(self, text: str) -> None:
        """Type text into the currently focused element."""
        ...

    def press(self, key: str) -> None:
        """Press a key or chord, e.g. 'Enter', 'Tab', 'Meta+a'."""
        ...

    def scroll(self, dx: int, dy: int) -> None:
        """Scroll by (dx, dy) pixels — a wheel gesture at the current
        pointer position (positive dy scrolls content up / view down)."""
        ...
