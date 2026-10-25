"""Generic fault drivers for the five qualification fault cases.

A fault driver changes the real input at the gate the runtime selects, so
that the engine's ordinary check must refuse it. It never returns a verdict:
the engine takes its own frame after the change, refuses a mutation that did
not change the input, and the case passes only when the detector refused that
exact input before any input was delivered.

:class:`WebFaultDriver` needs nothing from the application beyond the save
step's own structural selector, recorded in the bundle:

==================  =====================  ===================================
Case                Gate                   What changes
==================  =====================  ===================================
ambiguity           target_resolution      the save control is duplicated in
                                           place, so two candidates match
wrong_identity      identity_verification  the save control is relabeled to a
                                           different action and, when the
                                           page shows plain record text around
                                           it, that text is replaced by a
                                           different record of the same shape
stale_identity      actuation_revalidation the document holding the save
                                           control is blanked after the
                                           identity check
weak_effect         effect_strength        the record check is replaced by one
                                           that only reads the screen
missing_effect      effect_verifier        the record check is removed
==================  =====================  ===================================

``weak_effect`` and ``missing_effect`` change only the verifier, so they work
on every surface. The three screen faults need a browser page. A pixel-surface
driver for Windows, RDP and Citrix (changing what the remote frame shows at
the save) is not built yet; on those surfaces the driver declines the screen
faults and the case does not pass. See ``docs/QUALIFY_A_WORKFLOW.md``.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Optional

from openadapt_flow.qualification_faults import (
    FaultMutationReceipt,
    QualificationFaultContext,
    QualificationFaultMutation,
    effect_verifier_input_sha256,
    sha256_bytes,
    sign_fault_mutation_receipt,
)
from openadapt_flow.qualify.keys import LocalTestKey, new_local_test_key
from openadapt_flow.verification import VerificationTier

WEB_FAULT_DRIVER_ID = "openadapt.qualify.web-faults"
_CONTRACT = (
    b"openadapt.qualify.web-faults/v1\n"
    b"ambiguity@target_resolution: clone the save control in place\n"
    b"wrong_identity@identity_verification: relabel the save control; replace "
    b"plain record text around it\n"
    b"stale_identity@actuation_revalidation: blank the save control's document\n"
    b"weak_effect@effect_strength: screen-only record check\n"
    b"missing_effect@effect_verifier: no record check\n"
)
WEB_FAULT_DRIVER_CONTRACT_SHA256 = hashlib.sha256(_CONTRACT).hexdigest()
#: The label a wrong-identity save control is given.
WRONG_ACTION_LABEL = "Merge Into Existing Record"
SCREEN_FAULTS = frozenset({"ambiguity", "wrong_identity", "stale_identity"})

_CLONE_JS = (
    """(el) => { el.parentNode.insertBefore(el.cloneNode(true), el); return 1; }"""
)
_RELABEL_JS = """(el, label) => {
  if (el.tagName === 'INPUT') { el.value = label; }
  else { el.textContent = label; }
  for (const name of ['aria-label', 'title', 'value']) {
    if (el.hasAttribute(name)) { el.setAttribute(name, label); }
  }
  return 1;
}"""
_BLANK_JS = """(el) => {
  const doc = el.ownerDocument;
  doc.documentElement.style.background = '#ffffff';
  doc.body.replaceChildren();
  doc.body.style.background = '#ffffff';
  return 1;
}"""


class ScreenOnlyRecordCheck:
    """The weak-effect stand-in: a verifier that only declares a screen read.

    It has no other method; the engine must refuse it on its declared tier
    before it reads or writes anything.
    """

    verification_tier = VerificationTier.IMMEDIATE_SCREEN


def _rotate(value: str) -> str:
    out = []
    for char in value:
        if "a" <= char <= "z":
            out.append(chr((ord(char) - 97 + 13) % 26 + 97))
        elif "A" <= char <= "Z":
            out.append(chr((ord(char) - 65 + 13) % 26 + 65))
        elif "0" <= char <= "9":
            out.append(chr((ord(char) - 48 + 5) % 10 + 48))
        else:
            out.append(char)
    return "".join(out)


class WebFaultDriver:
    """Apply the five faults at the save step, using the bundle's own selector."""

    driver_id = WEB_FAULT_DRIVER_ID
    contract_sha256 = WEB_FAULT_DRIVER_CONTRACT_SHA256

    def __init__(
        self,
        workflow: Any,
        *,
        key: Optional[LocalTestKey] = None,
        key_id: str = "qualify-web-fault-driver",
    ) -> None:
        from openadapt_flow.traversal import iter_workflow_steps

        self._key = key or new_local_test_key(key_id)
        self.attestation_key_id = self._key.key_id
        self.public_key_base64 = self._key.public_key_base64
        self._steps = {step.id: step for step in iter_workflow_steps(workflow)}
        #: Why the last screen fault could not be staged, as a closed code.
        self.declined: Optional[str] = None

    @staticmethod
    def mutation_bytes(kind: str) -> bytes:
        return f"{WEB_FAULT_DRIVER_ID}/{kind}/v1\n".encode("ascii")

    # -- page helpers ---------------------------------------------------------
    def _save_target(self, context: QualificationFaultContext) -> Any:
        """The save control, found by the bundle's structural locator.

        A CSS selector or, when the control has none (common in React apps,
        which seldom give a save button an ``id``), its ARIA role and
        accessible name. Either must find exactly one element: no match or
        several matches is no target, and the fault declines.
        """

        step = self._steps.get(context.step_id)
        locator = (
            step.anchor.structural
            if step is not None and step.anchor is not None
            else None
        )
        by_role = (
            locator is not None
            and not locator.selector
            and bool(locator.role and locator.name)
        )
        if locator is None or not (locator.selector or by_role):
            return None
        scoped = getattr(context.backend, "_locator_with_scope", None)
        if callable(scoped):
            try:
                resolved = scoped(locator)
                if resolved is not None:
                    _scope, target = resolved
                    if target.count() == 1:
                        return target.first
                    if by_role:
                        return None
            except Exception:  # noqa: BLE001 - fall back to a frame search
                pass
        page = getattr(context.backend, "page", None)
        found = []
        for frame in list(getattr(page, "frames", None) or []):
            try:
                if by_role:
                    loc = frame.get_by_role(locator.role, name=locator.name, exact=True)
                    count = loc.count()
                    if count > 1:
                        return None
                else:
                    loc = frame.locator(locator.selector)
                    count = loc.count()
                if count == 1:
                    found.append(loc.first)
            except Exception:  # noqa: BLE001 - detached frames are skipped
                continue
        return found[0] if len(found) == 1 else None

    def _record_text_pairs(
        self, context: QualificationFaultContext, target: Any
    ) -> list:
        reader = getattr(context.backend, "structured_text_at", None)
        if not callable(reader):
            return []
        try:
            box = target.bounding_box()
            if not box:
                return []
            text = reader(
                int(round(box["x"] + box["width"] / 2)),
                int(round(box["y"] + box["height"] / 2)),
            )
        except Exception:  # noqa: BLE001
            return []
        if not text:
            return []
        try:
            json.loads(text)
            return []  # a structural identity document, not text on the page
        except ValueError:
            pass
        pairs: dict[str, str] = {}
        for word in re.findall(r"[0-9A-Za-z\u00C0-\u024F]{2,}", text):
            altered = _rotate(word)
            if altered != word:
                pairs[word] = altered
        return sorted(pairs.items(), key=lambda item: -len(item[0]))

    def _settled_frame_sha256(
        self, context: QualificationFaultContext
    ) -> Optional[str]:
        from openadapt_flow.qualification_drills import (
            _await_paint,
            _settled_screenshot,
        )

        _await_paint(context.backend.page)
        capture = context.capture_frame or context.backend.screenshot
        frame = _settled_screenshot(capture)
        return None if frame is None else sha256_bytes(frame)

    def _screen_fault(self, context: QualificationFaultContext) -> Optional[str]:
        page = getattr(context.backend, "page", None)
        if context.actuation_path != "gui" or page is None:
            self.declined = "screen_fault_needs_a_browser"
            return None
        target = self._save_target(context)
        if target is None:
            self.declined = "save_target_not_found"
            return None
        kind = context.fault_kind
        try:
            if kind == "ambiguity":
                target.evaluate(_CLONE_JS)
            elif kind == "wrong_identity":
                pairs = self._record_text_pairs(context, target)
                target.evaluate(_RELABEL_JS, WRONG_ACTION_LABEL)
                if pairs:
                    from openadapt_flow.qualification_drills import _SUBSTITUTE_JS

                    for frame in list(page.frames):
                        try:
                            frame.evaluate(_SUBSTITUTE_JS, pairs)
                        except Exception:  # noqa: BLE001
                            continue
            else:
                target.evaluate(_BLANK_JS)
        except Exception:  # noqa: BLE001 - a fault that cannot stage declines
            self.declined = "page_change_failed"
            return None
        after = self._settled_frame_sha256(context)
        if after is None:
            self.declined = "page_did_not_settle"
        return after

    # -- QualificationFaultDriver ------------------------------------------
    def mutate(
        self, context: QualificationFaultContext
    ) -> Optional[QualificationFaultMutation]:
        kind = context.fault_kind
        replacement: Any = context.effect_verifier
        replace_effect_verifier = False
        if kind in {"weak_effect", "missing_effect"}:
            replace_effect_verifier = True
            replacement = ScreenOnlyRecordCheck() if kind == "weak_effect" else None
            after = effect_verifier_input_sha256(replacement, context.effects)
        elif kind in SCREEN_FAULTS:
            screen_after = self._screen_fault(context)
            if screen_after is None:
                return None
            after = screen_after
        else:
            return None
        receipt = FaultMutationReceipt(
            project_id=context.project_id,
            project_revision=context.project_revision,
            project_contract_sha256=context.project_contract_sha256,
            campaign_id_sha256=context.campaign_id_sha256,
            case_id_sha256=context.case_id_sha256,
            case_input_sha256=context.case_input_sha256,
            run_id_sha256=context.run_id_sha256,
            step_id_sha256=sha256_bytes(context.step_id.encode("utf-8")),
            actuation_path=context.actuation_path,
            fault_kind=kind,
            gate=context.gate,
            driver_id=self.driver_id,
            driver_contract_sha256=self.contract_sha256,
            before_input_sha256=context.before_input_sha256,
            after_input_sha256=after,
            mutation_artifact_sha256=sha256_bytes(self.mutation_bytes(kind)),
            attestation_key_id=self.attestation_key_id,
        )
        self.declined = None
        return QualificationFaultMutation(
            receipt=sign_fault_mutation_receipt(
                receipt, private_key=self._key.private_key
            ),
            replace_effect_verifier=replace_effect_verifier,
            effect_verifier=replacement,
        )


__all__ = [
    "SCREEN_FAULTS",
    "WEB_FAULT_DRIVER_CONTRACT_SHA256",
    "WEB_FAULT_DRIVER_ID",
    "WRONG_ACTION_LABEL",
    "ScreenOnlyRecordCheck",
    "WebFaultDriver",
]
