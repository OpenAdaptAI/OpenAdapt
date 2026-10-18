"""Build a :class:`CopyGuard` from a live backend and taught interstitials.

:mod:`openadapt_flow.runtime.list_source` takes plain callables so it can be
tested without a screen. This module wires those callables to what the
runtime already has:

- focus comes from ``backend.application_identity()`` (the same identity the
  replayer's execution-context check uses);
- known modals come from the workflow's :class:`~openadapt_flow.ir.Interstitial`
  declarations, detected with the model-free predicate evaluator.

An interstitial is dismissed automatically only when it would be on the
replayer's own path: ``risk="reversible"``, ``consequential=False`` and a
declared ``clearance`` predicate, plus a way to dismiss it:

- ``dismiss_key="Escape"``: press Escape;
- ``dismiss_anchor``: click its close control, located on the current frame
  with the same model-free resolution ladder the replayer uses for an
  interstitial (no model grounding). If the control does not resolve, the
  walk halts with ``modal_present``.

After the one action the clearance must hold and the modal must be gone, or
the walk halts with ``modal_not_cleared``. A dialog that is not declared, or
is declared without those three properties, halts the walk by name.
"""

from __future__ import annotations

from typing import Any, Callable, Optional, Sequence

from openadapt_flow.ir import Interstitial, Predicate
from openadapt_flow.runtime.list_source import CopyGuard, KnownModal, ListHalt
from openadapt_flow.runtime.program_predicates import evaluate_program_predicate

PredicateHolds = Callable[[Predicate, bytes], bool]
#: (interstitial, frame) -> click point of its close control, or None.
CloseLocator = Callable[[Interstitial, bytes], Optional[tuple[int, int]]]


def program_predicate_holds(vision: Any) -> PredicateHolds:
    """Evaluate a predicate on a frame with the runtime's model-free rules."""

    def holds(pred: Predicate, frame_png: bytes) -> bool:
        return evaluate_program_predicate(
            pred,
            frame_png,
            {},
            vision=vision,
            viewport=None,
            asset_loader=lambda _rel: None,
        )

    return holds


def close_locator(
    vision: Any, asset_loader: Callable[[str], Optional[bytes]]
) -> CloseLocator:
    """Locate a dismiss control with the model-free resolution ladder."""
    from openadapt_flow.runtime.resolver import resolve

    def locate(item: Interstitial, frame_png: bytes) -> Optional[tuple[int, int]]:
        anchor = item.dismiss_anchor
        if anchor is None:
            return None
        template = asset_loader(anchor.template) if anchor.template.strip() else None
        res = resolve(anchor, frame_png, vision, None, item.name, template_png=template)
        if res is None:
            return None
        x, y = res[0].point
        return int(x), int(y)

    return locate


def _admitted(item: Interstitial) -> bool:
    return (
        item.risk == "reversible"
        and item.consequential is False
        and item.clearance is not None
    )


def _auto_dismissable(item: Interstitial, can_click: bool) -> bool:
    if not _admitted(item):
        return False
    if item.dismiss_key == "Escape":
        return True
    return item.dismiss_anchor is not None and can_click


def modals_from_interstitials(
    backend: Any,
    interstitials: Sequence[Interstitial],
    holds: PredicateHolds,
    locate_close: Optional[CloseLocator] = None,
) -> list[KnownModal]:
    """Turn taught interstitials into :class:`KnownModal` checks."""
    out: list[KnownModal] = []
    for item in interstitials:

        def detect(item: Interstitial = item) -> bool:
            return holds(item.detect, backend.screenshot())

        def verify_cleared(item: Interstitial, how: str) -> None:
            frame = backend.screenshot()
            assert item.clearance is not None
            if not holds(item.clearance, frame) or holds(item.detect, frame):
                # One action, then check; no blind retries.
                raise ListHalt(
                    "modal_not_cleared", f"{item.name!r} did not clear after {how}"
                )

        dismiss: Optional[Callable[[], None]] = None
        if _auto_dismissable(item, locate_close is not None):
            if item.dismiss_key == "Escape":

                def dismiss(item: Interstitial = item) -> None:
                    backend.press("Escape")
                    verify_cleared(item, "Escape")

            else:
                assert locate_close is not None
                locate = locate_close

                def dismiss(item: Interstitial = item) -> None:
                    point = locate(item, backend.screenshot())
                    if point is None:
                        raise ListHalt(
                            "modal_present",
                            f"close control of {item.name!r} was not found",
                        )
                    backend.click(*point)
                    verify_cleared(item, "clicking its close control")

        out.append(
            KnownModal(
                name=item.name,
                detect=detect,
                dismiss=dismiss,
                reversible=dismiss is not None,
            )
        )
    return out


def guard_for_backend(
    backend: Any,
    expected_application: str,
    *,
    interstitials: Sequence[Interstitial] = (),
    holds: Optional[PredicateHolds] = None,
    modal_open: Optional[Callable[[], Optional[str]]] = None,
    max_dismissals: int = 2,
    locate_close: Optional[CloseLocator] = None,
) -> CopyGuard:
    """Focus and no-modal guard for a list walk on ``backend``.

    Args:
        backend: A backend with ``application_identity``, ``screenshot`` and
            ``press``.
        expected_application: The list window's application identity, a
            string or regex.
        interstitials: Taught modals.
        holds: Predicate evaluator; required when ``interstitials`` is set.
        modal_open: Optional generic detector for any modal.
        max_dismissals: Dismissals allowed per check.
        locate_close: Finds a declared dialog's close control on a frame
            (see :func:`close_locator`). Without it, dialogs declared with
            only a ``dismiss_anchor`` halt.
    """
    if interstitials and holds is None:
        raise ValueError("holds is required to detect interstitials")

    def focused() -> Optional[str]:
        identity = getattr(backend, "application_identity", None)
        if identity is None:
            return None
        try:
            value = identity()
        except Exception:  # noqa: BLE001 - unknown focus fails closed
            return None
        return value if isinstance(value, str) else None

    return CopyGuard(
        focused_window=focused,
        expected_window=expected_application,
        known_modals=(
            modals_from_interstitials(backend, interstitials, holds, locate_close)
            if holds is not None
            else []
        ),
        modal_open=modal_open,
        max_dismissals=max_dismissals,
    )
