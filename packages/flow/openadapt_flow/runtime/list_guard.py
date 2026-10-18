"""Build a :class:`CopyGuard` from a live backend and taught interstitials.

:mod:`openadapt_flow.runtime.list_source` takes plain callables so it can be
tested without a screen. This module wires those callables to what the
runtime already has:

- focus comes from ``backend.application_identity()`` (the same identity the
  replayer's execution-context check uses);
- known modals come from the workflow's :class:`~openadapt_flow.ir.Interstitial`
  declarations, detected with the model-free predicate evaluator.

An interstitial is dismissed automatically only when it would be on the
replayer's own path: ``risk="reversible"``, ``consequential=False``, a
declared ``clearance`` predicate and ``dismiss_key="Escape"``. After the key
press the clearance must hold and the modal must be gone, or the walk halts
with ``modal_not_cleared``. Every other interstitial halts the walk by name.
Click-to-dismiss (``dismiss_anchor``) is not done here because it needs the
resolution ladder; such modals halt.
"""

from __future__ import annotations

from typing import Any, Callable, Optional, Sequence

from openadapt_flow.ir import Interstitial, Predicate
from openadapt_flow.runtime.list_source import CopyGuard, KnownModal, ListHalt
from openadapt_flow.runtime.program_predicates import evaluate_program_predicate

PredicateHolds = Callable[[Predicate, bytes], bool]


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


def _auto_dismissable(item: Interstitial) -> bool:
    return (
        item.risk == "reversible"
        and item.consequential is False
        and item.clearance is not None
        and item.dismiss_key == "Escape"
    )


def modals_from_interstitials(
    backend: Any,
    interstitials: Sequence[Interstitial],
    holds: PredicateHolds,
) -> list[KnownModal]:
    """Turn taught interstitials into :class:`KnownModal` checks."""
    out: list[KnownModal] = []
    for item in interstitials:

        def detect(item: Interstitial = item) -> bool:
            return holds(item.detect, backend.screenshot())

        dismiss: Optional[Callable[[], None]] = None
        if _auto_dismissable(item):

            def dismiss(item: Interstitial = item) -> None:
                backend.press("Escape")
                frame = backend.screenshot()
                assert item.clearance is not None
                if not holds(item.clearance, frame) or holds(item.detect, frame):
                    # One action, then check; no blind retries.
                    raise ListHalt(
                        "modal_not_cleared",
                        f"{item.name!r} did not clear after Escape",
                    )

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
            modals_from_interstitials(backend, interstitials, holds)
            if holds is not None
            else []
        ),
        modal_open=modal_open,
        max_dismissals=max_dismissals,
    )
