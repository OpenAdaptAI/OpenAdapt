"""Backend protocols: the runtime boundary for observing and acting on a GUI.

Every backend provides fresh frames and bounded input. Browser and native
backends may additionally expose structural candidates and native operations;
opaque RDP/Citrix surfaces retain the visual/OCR/relational ladder. The
governed runtime owns uniqueness, identity, authorization, effect verification,
and refusal regardless of which observation or actuation rung is available.
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


class ActionDeliveryUncertain(RuntimeError):
    """An action API failed after delivery may already have begun.

    This is deliberately distinct from both a pre-delivery safety refusal and
    a successful delivery receipt.  The runtime must not retry the action.  It
    may report success only after the configured postcondition and independent
    effect contracts fully confirm the intended business outcome.

    The exception retains only bounded, non-payload metadata.  Backend error
    messages can contain page text, URLs, or identifiers and therefore are not
    copied into the run report.
    """

    def __init__(
        self,
        *,
        operation: str,
        native: bool,
        target_fingerprint: Optional[str] = None,
        cause_type: str = "BackendError",
    ) -> None:
        super().__init__(
            "action delivery may have occurred; independent verification required"
        )
        self.operation = operation
        self.native = native
        self.target_fingerprint = target_fingerprint
        self.cause_type = cause_type


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
class FieldLabelBackend(Protocol):
    """Optional RECORD-TIME field-label capability a backend MAY expose.

    When a demonstrator types into a field, the RECORDER (not the replayer)
    asks the backend for the focused field's best available human label so a
    non-engineer never has to name parameters in code: the label becomes
    passive evidence (``field_label`` on the TYPE event and
    ``ir.Step.field_label``) that the compile-time parameter-proposal pass
    turns into a slugified, OPERATOR-CONFIRMED parameter name (see
    ``compiler.annotate.FieldLabelAnnotator``).

    Implementations read whatever structured layer they own:

    - a browser backend (Playwright) reads the focused element's associated
      DOM ``<label>``, ``aria-label`` / ``aria-labelledby``, ``placeholder``,
      ``name`` attribute, or ``title`` -- in that order;
    - a native desktop backend reads the accessibility label of the focused
      control (UIA ``Name``, macOS ``AXTitle``/``AXDescription``, AT-SPI
      accessible name) where that seam exists.

    This is PASSIVE metadata capture only: querying the label must never
    change focus, field contents, or timing beyond a cheap read, and a
    pixel-only substrate simply returns None (the compiler may then fall back
    to nearby-OCR evidence where the recording carries a field rectangle).
    Never called at replay.
    """

    def focused_field_label(self) -> Optional[str]:
        """Return the focused field's best available label, or None.

        Whitespace-collapsed; None when no field is focused, the backend has
        no structured layer, or on any momentary failure (never raises).
        """
        ...


@runtime_checkable
class ExecutionContextIdentityBackend(Protocol):
    """Optional live identities that cannot be inferred from record-row text."""

    def application_identity(self) -> Optional[str]:
        """Return the current application/window identity."""
        ...

    def session_identity(self) -> Optional[str]:
        """Return the current runner/remote-session identity digest."""
        ...

    def workflow_state_identity(self) -> Optional[str]:
        """Return the current application workflow-state identity."""
        ...


@runtime_checkable
class BrowserPresentationGeometryBackend(Protocol):
    """Optional exact browser geometry for an out-of-page presentation layer.

    This capability does not inject markup, expose a selector, or participate
    in target resolution.  It lets the presentation-only control-overlay rail
    normalize an already-resolved rectangle to the top-level browser CSS
    viewport.  Native desktop and pixel-only RDP/Citrix backends deliberately
    do not implement it; those coordinate spaces require their own contracts.
    """

    def browser_presentation_viewport(self) -> Optional[tuple[int, int, float]]:
        """Return ``(CSS width, CSS height, device pixel ratio)`` or ``None``."""
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
class GuardedCoordinateActionBackend(Protocol):
    """Optional atomic local coordinate actuation.

    A backend implementing this seam must bind the identity-verified target to
    delivery and reject a changed frame/element/record before the first input
    edge. A pixel backend can compare the expected frame under its input lock; a
    DOM backend can use an opaque element token plus a mutation guard and retain
    its native pointer-event semantics. This is the local counterpart to a
    remote backend's one-shot actuation lease.
    """

    def arm_guarded_coordinate(self, x: int, y: int) -> None:
        """Bind one identity-bearing actionable target before identity readback."""
        ...

    def cancel_guarded_coordinate(self) -> None:
        """Cancel and clean any unconsumed guarded-coordinate binding."""
        ...

    def act_guarded_coordinate(
        self,
        x: int,
        y: int,
        *,
        expected_frame_sha256: str,
        double: bool = False,
        button: str = "left",
    ) -> "ActionDeliveryReceipt":
        """Verify the exact frame and deliver one coordinate action atomically."""
        ...


@runtime_checkable
class FocusedElementActuationLeaseBackend(Protocol):
    """Optional focused-control binding layered onto a remote frame lease.

    Native backends that already use :class:`RemoteActuationBackend` for an
    exact frame/window lease can additionally bind the freshly resolved target
    point to one accessibility-focused element before final identity readback.
    Delivery must refuse if that opaque element changes while the pixels remain
    indistinguishable.
    """

    def arm_focused_element_lease(self, x: int, y: int) -> None:
        """Bind the current focused element to the resolved target point."""
        ...

    def cancel_focused_element_lease(self) -> None:
        """Cancel an unconsumed focused-element binding."""
        ...


@runtime_checkable
class GuardedKeyboardActionBackend(Protocol):
    """Optional browser-local focus/record lease for consequential keyboard I/O.

    The backend arms the exact focused element and its identity-bearing record
    before the runtime's final identity observation. Focus changes or any
    target/record mutation invalidate the one-shot lease before a key or text
    input can be delivered.
    """

    def guarded_keyboard_frame(self) -> bytes:
        """Capture the exact frame without mutating focused-element caret state."""
        ...

    def arm_guarded_keyboard(self, x: int, y: int) -> None:
        """Bind the focused element at the freshly resolved identity point."""
        ...

    def cancel_guarded_keyboard(self) -> None:
        """Cancel and clean any unconsumed guarded-keyboard binding."""
        ...

    def press_guarded(
        self,
        key: str,
        *,
        expected_frame_sha256: str,
    ) -> "ActionDeliveryReceipt":
        """Deliver one key/chord to the pre-identity focused-element lease."""
        ...

    def type_text_guarded(
        self,
        text: str,
        *,
        expected_frame_sha256: str,
    ) -> "ActionDeliveryReceipt":
        """Type into the pre-identity focused-element lease."""
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
class SelectOptionBackend(Protocol):
    """Atomic parameterized option selection for opaque/native controls.

    Implementations keep the type-ahead text and Enter/Tab commit under one
    input/focus lease. This protocol reports delivery only; the runtime still
    requires the intended committed value inside a compiler-qualified readback
    band mapped onto the freshly re-resolved live field before continuing.
    """

    def select_option(self, text: str, commit_key: str) -> None:
        """Type ``text`` and commit it with Enter/Tab as one input operation."""
        ...


@runtime_checkable
class RemoteActuationBackend(Protocol):
    """Optional two-phase actuation seam for opaque remote surfaces.

    A remote backend implements this when input crosses an RDP/Citrix/VDI
    boundary whose guest-side focus and contents can change after ordinary
    target resolution.  The method acquires the exact remote client/session,
    validates readiness, and captures the frame that the runtime must
    re-resolve immediately before a consequential action.

    The backend owns a one-shot content lease for the returned frame.  Its next
    input method captures once more under the backend input lock and refuses
    before the first input edge if the window/session, dimensions, readiness,
    or exact frame content changed.  The lease is consumed once so a
    multi-character type or double-click gesture cannot invalidate itself.
    """

    def acquire_actuation_frame(self) -> bytes:
        """Acquire focus/readiness and return the freshly leased PNG frame."""
        ...


@runtime_checkable
class PreparedPointerActuationBackend(Protocol):
    """Optional pointer pre-positioning for opaque remote-display clients.

    Moving a remote cursor can legitimately repaint hover, caret, or remote
    cursor pixels.  A backend implementing this seam positions the pointer
    *before* the runtime acquires its final consequential actuation frame.  The
    runtime then re-resolves the target and identity on that post-hover frame;
    delivery must use the same prepared point without another pointer move.
    """

    def prepare_pointer_actuation(self, x: int, y: int) -> None:
        """Position the pointer without pressing a button or claiming success."""
        ...


@runtime_checkable
class RichPointerActionBackend(Protocol):
    """Pointer actions beyond the primary/double click contract."""

    def right_click(self, x: int, y: int) -> None: ...

    def drag(self, x: int, y: int, end_x: int, end_y: int) -> None: ...


@runtime_checkable
class GuardedDragActionBackend(Protocol):
    """Consume a pre-identity coordinate lease for one exact drag."""

    def drag_guarded(
        self,
        x: int,
        y: int,
        end_x: int,
        end_y: int,
        *,
        expected_frame_sha256: str,
    ) -> "ActionDeliveryReceipt": ...


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
