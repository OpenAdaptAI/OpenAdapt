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
import io
import json
import re
from typing import Any, Callable, Optional

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
PIXEL_SURFACE_FAULT_DRIVER_ID = "openadapt.qualify.pixel-surface-faults"
_PIXEL_CONTRACT = (
    b"openadapt.qualify.pixel-surface-faults/v1\n"
    b"ambiguity@target_resolution: duplicate target region crop on pixel surface\n"
    b"wrong_identity@identity_verification: overwrite identifier region pixels\n"
    b"stale_identity@actuation_revalidation: blank captured pixel surface\n"
    b"weak_effect@effect_strength: screen-only record check\n"
    b"missing_effect@effect_verifier: no record check\n"
)
PIXEL_SURFACE_FAULT_DRIVER_CONTRACT_SHA256 = hashlib.sha256(_PIXEL_CONTRACT).hexdigest()
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


def _clamp_box(
    region: tuple[int, int, int, int], width: int, height: int
) -> tuple[int, int, int, int]:
    rx, ry, rw, rh = region
    x0 = max(0, min(rx, width - 1))
    y0 = max(0, min(ry, height - 1))
    x1 = max(x0 + 1, min(rx + max(1, rw), width))
    y1 = max(y0 + 1, min(ry + max(1, rh), height))
    return (x0, y0, x1, y1)


def mutate_pixel_frame(
    frame_png: bytes,
    *,
    fault_kind: str,
    region: tuple[int, int, int, int],
    identifier_region: Optional[tuple[int, int, int, int]] = None,
) -> bytes:
    """Apply a deterministic pixel-surface fault mutation to a PNG frame.

    - ``ambiguity``: duplicates the target ``region`` crop onto an adjacent
      non-overlapping location in the frame so visual resolution sees two
      identical targets.
    - ``wrong_identity``: overwrites ``identifier_region`` (or ``region`` when
      absent) with a high-contrast mismatched banner and rotated label so
      pixel and OCR identity checks detect a mismatch.
    - ``stale_identity``: blanks the frame to solid white so final
      actuation revalidation observes a changed screen.
    """
    from PIL import Image, ImageDraw

    with Image.open(io.BytesIO(frame_png)) as src:
        img = src.convert("RGBA")
    width, height = img.size

    if fault_kind == "ambiguity":
        x0, y0, x1, y1 = _clamp_box(region, width, height)
        crop = img.crop((x0, y0, x1, y1))
        cw, ch = crop.size
        pad = 8
        # Place the duplicate to the right, left, below, or above the target.
        if x1 + pad + cw <= width:
            dx, dy = x1 + pad, y0
        elif x0 - pad - cw >= 0:
            dx, dy = x0 - pad - cw, y0
        elif y1 + pad + ch <= height:
            dx, dy = x0, y1 + pad
        elif y0 - pad - ch >= 0:
            dx, dy = x0, y0 - pad - ch
        else:
            dx = max(0, min(width - cw, (x0 + max(4, cw // 2)) % max(1, width - cw + 1)))
            dy = max(0, min(height - ch, (y0 + max(4, ch // 2)) % max(1, height - ch + 1)))
        img.paste(crop, (dx, dy))
        # Ensure the destination region visibly differs even on a uniform canvas.
        if img.tobytes() == Image.open(io.BytesIO(frame_png)).convert("RGBA").tobytes():
            draw = ImageDraw.Draw(img)
            draw.rectangle((dx, dy, dx + cw - 1, dy + ch - 1), outline=(220, 38, 38, 255))
    elif fault_kind == "wrong_identity":
        target_box = identifier_region if identifier_region is not None else region
        x0, y0, x1, y1 = _clamp_box(target_box, width, height)
        draw = ImageDraw.Draw(img)
        draw.rectangle((x0, y0, x1 - 1, y1 - 1), fill=(254, 242, 242, 255))
        draw.rectangle((x0, y0, x1 - 1, y1 - 1), outline=(185, 28, 28, 255), width=2)
        draw.text((x0 + 4, y0 + 2), WRONG_ACTION_LABEL, fill=(127, 29, 29, 255))
    elif fault_kind == "stale_identity":
        # Blank to white; if the source frame was already pure white, fill light gray.
        blank = Image.new("RGBA", (width, height), (255, 255, 255, 255))
        if blank.tobytes() == img.tobytes():
            blank = Image.new("RGBA", (width, height), (241, 245, 249, 255))
        img = blank
    else:
        raise ValueError(f"unsupported pixel screen fault kind: {fault_kind!r}")

    out = io.BytesIO()
    img.convert("RGB").save(out, format="PNG")
    return out.getvalue()


class PixelSurfaceFaultDriver:
    """Apply the five qualification faults on pixel surfaces (Windows/RDP/Citrix).

    Handles ``ambiguity``, ``wrong_identity``, and ``stale_identity`` directly on
    captured PNG frames, plus ``weak_effect`` and ``missing_effect`` verifier
    mutations, producing Ed25519-signed :class:`FaultMutationReceipt` objects
    identical in structure to :class:`WebFaultDriver`.
    """

    driver_id = PIXEL_SURFACE_FAULT_DRIVER_ID
    contract_sha256 = PIXEL_SURFACE_FAULT_DRIVER_CONTRACT_SHA256

    def __init__(
        self,
        workflow: Any,
        *,
        key: Optional[LocalTestKey] = None,
        key_id: str = "qualify-pixel-surface-fault-driver",
        apply_frame: Optional[
            Callable[[bytes, QualificationFaultContext], Optional[bytes]]
        ] = None,
    ) -> None:
        from openadapt_flow.traversal import iter_workflow_steps

        self._key = key or new_local_test_key(key_id)
        self.attestation_key_id = self._key.key_id
        self.public_key_base64 = self._key.public_key_base64
        self._steps = {step.id: step for step in iter_workflow_steps(workflow)}
        self._apply_frame = apply_frame
        self._patched_backends: dict[int, tuple[Any, Any]] = {}
        self.last_mutated_frame: Optional[bytes] = None
        #: Why the last screen fault could not be staged, as a closed code.
        self.declined: Optional[str] = None

    @staticmethod
    def mutation_bytes(kind: str) -> bytes:
        return f"{PIXEL_SURFACE_FAULT_DRIVER_ID}/{kind}/v1\n".encode("ascii")

    def _capture_current_frame(
        self, context: QualificationFaultContext
    ) -> Optional[bytes]:
        capture = context.capture_frame or getattr(context.backend, "screenshot", None)
        if not callable(capture):
            return None
        try:
            frame = capture()
        except Exception:  # noqa: BLE001
            return None
        return frame if isinstance(frame, (bytes, bytearray)) and frame else None

    def _publish_mutated_frame(
        self,
        mutated_png: bytes,
        context: QualificationFaultContext,
    ) -> bytes:
        if self._apply_frame is not None:
            returned = self._apply_frame(mutated_png, context)
            if isinstance(returned, (bytes, bytearray)) and returned:
                return bytes(returned)
            recaptured = self._capture_current_frame(context)
            return bytes(recaptured) if recaptured is not None else mutated_png

        for attr in ("set_pixel_fault_frame", "inject_frame"):
            hook = getattr(context.backend, attr, None)
            if callable(hook):
                hook(mutated_png)
                recaptured = self._capture_current_frame(context)
                return bytes(recaptured) if recaptured is not None else mutated_png

        backend = context.backend
        if backend is not None and hasattr(backend, "screenshot"):
            backend_key = id(backend)
            if backend_key not in self._patched_backends:
                self._patched_backends[backend_key] = (backend, backend.screenshot)
            backend.screenshot = lambda _png=mutated_png: _png
        return mutated_png

    def cleanup(self, backend: Any) -> None:
        """Restore any patched screenshot hook or clear backend pixel fault state."""
        clear_hook = getattr(backend, "clear_pixel_fault_frame", None)
        if callable(clear_hook):
            clear_hook()
        saved = self._patched_backends.pop(id(backend), None)
        if saved is not None:
            saved_backend, orig_screenshot = saved
            saved_backend.screenshot = orig_screenshot

    def _screen_fault(self, context: QualificationFaultContext) -> Optional[str]:
        if context.actuation_path != "gui":
            self.declined = "pixel_fault_needs_gui"
            return None
        step = self._steps.get(context.step_id)
        anchor = step.anchor if step is not None else None
        if anchor is None:
            self.declined = "step_anchor_unavailable"
            return None
        current = self._capture_current_frame(context)
        if current is None:
            self.declined = "frame_capture_unavailable"
            return None
        try:
            mutated = mutate_pixel_frame(
                bytes(current),
                fault_kind=context.fault_kind,
                region=anchor.region,
                identifier_region=anchor.identifier_region,
            )
            published = self._publish_mutated_frame(mutated, context)
        except Exception:  # noqa: BLE001
            self.declined = "pixel_frame_mutation_failed"
            return None
        after = sha256_bytes(published)
        if after == context.before_input_sha256:
            self.declined = "frame_mutation_unchanged"
            return None
        self.last_mutated_frame = published
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
            if after == context.before_input_sha256:
                self.declined = "effect_verifier_unchanged"
                return None
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
    "PIXEL_SURFACE_FAULT_DRIVER_CONTRACT_SHA256",
    "PIXEL_SURFACE_FAULT_DRIVER_ID",
    "SCREEN_FAULTS",
    "WEB_FAULT_DRIVER_CONTRACT_SHA256",
    "WEB_FAULT_DRIVER_ID",
    "WRONG_ACTION_LABEL",
    "PixelSurfaceFaultDriver",
    "ScreenOnlyRecordCheck",
    "WebFaultDriver",
    "mutate_pixel_frame",
]
