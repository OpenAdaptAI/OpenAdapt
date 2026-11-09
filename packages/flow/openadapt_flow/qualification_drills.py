"""Readiness drills from Desktop: a local campaign made from the task itself.

Check it's safety drills are qualification fault cases. A fault case needs a
fault driver: something outside the detector that changes the real input at
one gate, so the engine's ordinary check must refuse the changed input. Until
now ``qualify run-case`` ran only the practice item, because nothing on a
customer's computer could supply a driver ("fault qualification cases require
the configured fault-driver campaign").

This module is that local campaign. Nothing in it is specific to one
application: it uses only the recorded program, the customer's own practice
case and the mechanisms Flow already has.

- :func:`prepare_local_drill_campaign` sets up the five drills for the
  practice case's first save (or a save the caller names). A project starts
  with one required fault case per kind; an unscoped one gets the practice
  case's exact inputs and action scope, so only the fault differs. A kind the
  project lacks is added. A drill already scoped is left as it is. It also
  trusts this computer's drill key in the project, so the project names the
  only key whose fault receipts it accepts.
- :class:`LocalDrillDriver` makes each fault real, at the gate the engine
  names, and signs a receipt for it:

  ===================  ======================  ==================================
  Drill                Gate                    What changes
  ===================  ======================  ==================================
  wrong record         identity_verification   the record identity at the save
                                               (and the case's own values) on the
                                               page is replaced by another
  two matches          target_resolution       the save target is duplicated
                                               on the page
  record changed       actuation_revalidation  the same change, after the
                                               right-record check passed
  save not confirmed   effect_verifier         the record check is removed
  weak record check    effect_strength         the record check is replaced by
                                               one that only reads the screen
  ===================  ======================  ==================================

What a drill proves is unchanged, because none of this touches the detector:

- the driver can only change the real page or the verifier it is handed; it
  never returns a verdict, and the engine takes its own screenshot after the
  change and refuses a mutation that didn't change the input;
- a drill passes only when the engine's ordinary detector refused that exact
  changed input before any input was delivered, with the exact stage and
  code (:func:`openadapt_flow.qualification_faults.fault_detector_contract_error`);
- the receipt binds the project, case, inputs, campaign, run and step, and is
  signed with a key that never leaves this computer;
- at the fault step the engine never asks a person to approve the save: if
  the detector misses the fault, the engine still refuses to cross the input
  boundary, and the drill fails.

A drill the driver can't stage here (a screen drill on a target that isn't a
browser, or a save with no structural locator or record identity on the
page) declines. The
run then ends without the detector refusal, and the drill hasn't passed. Page
changes stay in the browser tab: nothing is saved, and the tab should be
reopened at the task's start screen before the next run.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Optional

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from openadapt_flow.qualification_faults import (
    FaultMutationReceipt,
    QualificationFaultContext,
    QualificationFaultMutation,
    effect_verifier_input_sha256,
    sha256_bytes,
    sign_fault_mutation_receipt,
)
from openadapt_flow.verification import VerificationTier

if TYPE_CHECKING:
    from openadapt_flow.ir import Step, Workflow

LOCAL_DRILL_DRIVER_ID = "openadapt.local-readiness-drills"
_LOCAL_DRILL_CONTRACT = (
    b"openadapt.local-readiness-drills/v1\n"
    b"ambiguity@target_resolution: duplicate the structural save target in the page\n"
    b"wrong_identity@identity_verification: replace the record identity at the "
    b"save target and the case's own values shown on the page\n"
    b"stale_identity@actuation_revalidation: the same replacement after the "
    b"right-record check\n"
    b"missing_effect@effect_verifier: remove the record check\n"
    b"weak_effect@effect_strength: replace the record check with a screen-only "
    b"declaration\n"
)
LOCAL_DRILL_CONTRACT_SHA256 = hashlib.sha256(_LOCAL_DRILL_CONTRACT).hexdigest()
KEY_SCHEMA = "openadapt.local-drill-key/v1"
MUTATION_SCHEMA = "openadapt.local-drill-mutation/v1"
CAMPAIGN_SCHEMA = "openadapt.local-drill-campaign/v1"

#: (fault kind, case-id slug, plain name) in the order Check it shows them.
DRILLS: tuple[tuple[str, str, str], ...] = (
    ("wrong_identity", "wrong-record", "Wrong record"),
    ("ambiguity", "two-matches", "Two matches"),
    ("stale_identity", "record-changed", "Record changed mid-task"),
    ("missing_effect", "save-not-confirmed", "Save didn't land"),
    ("weak_effect", "weak-record-check", "Weak record check"),
)
_SCREEN_KINDS = frozenset({"ambiguity", "wrong_identity", "stale_identity"})


class LocalDrillError(ValueError):
    """The local drill campaign can't be prepared or used safely."""


# -- the computer's drill key -------------------------------------------------------


def _state_root() -> Path:
    return Path(os.environ.get("OPENADAPT_HOME", Path.home() / ".openadapt"))


def _private_dir(path: Path) -> Path:
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        pass
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise LocalDrillError("the drill key folder is not a plain folder")
    if os.name != "nt":
        if info.st_uid != os.geteuid():
            raise LocalDrillError("the drill key folder belongs to another user")
        if stat.S_IMODE(info.st_mode) != 0o700:
            os.chmod(path, 0o700)
    return path


@dataclass(frozen=True)
class LocalDrillKey:
    """This computer's Ed25519 drill key. The private half never leaves it."""

    key_id: str
    public_key_base64: str
    private_key: bytes = field(repr=False)


def _key_id(public_key_base64: str) -> str:
    digest = hashlib.sha256(public_key_base64.encode("ascii")).hexdigest()
    return f"local-drill-{digest[:24]}"


def local_drill_key(
    state_root: Optional[Path] = None, *, create: bool = False
) -> Optional[LocalDrillKey]:
    """Load this computer's drill key; create it when ``create`` is set."""

    root = Path(state_root) if state_root is not None else _state_root()
    if create:
        root.mkdir(parents=True, exist_ok=True)
        folder = _private_dir(root / "qualification-drills")
    else:
        folder = root / "qualification-drills"
        if not folder.exists():
            return None
        _private_dir(folder)
    path = folder / "driver-key.json"
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        if not create:
            return None
        return _create_key(path)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > 4096:
            raise LocalDrillError("the drill key is not a regular file")
        if os.name != "nt" and (
            info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077
        ):
            raise LocalDrillError("the drill key file is not private to this user")
        raw = os.read(descriptor, 4097)
    finally:
        os.close(descriptor)
    try:
        payload = json.loads(raw)
        if payload.get("schema") != KEY_SCHEMA:
            raise ValueError("schema")
        private = base64.b64decode(payload["private_key_base64"], validate=True)
        key = Ed25519PrivateKey.from_private_bytes(private)
    except (ValueError, KeyError, TypeError) as exc:
        raise LocalDrillError("the drill key file is unreadable") from exc
    public = _public_b64(key)
    if payload.get("public_key_base64") != public or payload.get("key_id") != _key_id(
        public
    ):
        raise LocalDrillError("the drill key file does not match its key")
    return LocalDrillKey(
        key_id=_key_id(public), public_key_base64=public, private_key=private
    )


def _public_b64(key: Ed25519PrivateKey) -> str:
    return base64.b64encode(
        key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
    ).decode("ascii")


def _create_key(path: Path) -> LocalDrillKey:
    key = Ed25519PrivateKey.generate()
    private = key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public = _public_b64(key)
    payload = json.dumps(
        {
            "schema": KEY_SCHEMA,
            "key_id": _key_id(public),
            "public_key_base64": public,
            "private_key_base64": base64.b64encode(private).decode("ascii"),
        },
        sort_keys=True,
    ).encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        loaded = local_drill_key(path.parent.parent, create=False)
        if loaded is None:
            raise LocalDrillError("the drill key appeared but is unreadable") from None
        return loaded
    try:
        os.write(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return LocalDrillKey(
        key_id=_key_id(public), public_key_base64=public, private_key=private
    )


def project_trusts_local_key(workflow: "Workflow", key: LocalDrillKey) -> bool:
    project = workflow.qualification
    return bool(
        project is not None
        and project.trusted_fault_driver_keys.get(key.key_id) == key.public_key_base64
    )


# -- the campaign ---------------------------------------------------------------------


@dataclass(frozen=True)
class DrillCase:
    case_id: str
    kind: str
    name: str
    fault_step_id: str
    actuation_path: Literal["gui", "api"]
    #: ``added`` (a new drill case), ``scoped`` (an existing, unscoped drill
    #: such as a project's default fault case now runs the practice case's
    #: inputs), or ``kept`` (already scoped; left exactly as it was).
    state: Literal["added", "scoped", "kept"]


@dataclass(frozen=True)
class LocalDrillCampaign:
    """What :func:`prepare_local_drill_campaign` set up (PHI-free)."""

    from_case_id: str
    save_step_id: str
    driver_id: str
    driver_key_id: str
    runtime_input_sha256: str
    drills: tuple[DrillCase, ...]
    skipped: tuple[tuple[str, str], ...]

    def as_json(self) -> dict[str, Any]:
        return {
            "schema": CAMPAIGN_SCHEMA,
            "from_case_id": self.from_case_id,
            "save_step_id": self.save_step_id,
            "driver_id": self.driver_id,
            "driver_contract_sha256": LOCAL_DRILL_CONTRACT_SHA256,
            "driver_key_id": self.driver_key_id,
            "runtime_input_sha256": self.runtime_input_sha256,
            "drills": [
                {
                    "case_id": drill.case_id,
                    "kind": drill.kind,
                    "name": drill.name,
                    "fault_step_id": drill.fault_step_id,
                    "actuation_path": drill.actuation_path,
                    "state": drill.state,
                }
                for drill in self.drills
            ],
            "skipped": [
                {"kind": kind, "reason": reason} for kind, reason in self.skipped
            ],
        }


def _drill_case_id(slug: str, step_id: str) -> str:
    import re

    safe = re.sub(r"[^A-Za-z0-9._:-]", "-", step_id)
    candidate = f"drill-{slug}-{safe}"
    if len(candidate) > 128:
        digest = hashlib.sha256(step_id.encode("utf-8")).hexdigest()[:16]
        candidate = f"drill-{slug}-{digest}"
    return candidate


def prepare_local_drill_campaign(
    workflow: "Workflow",
    *,
    key: LocalDrillKey,
    from_case_id: Optional[str] = None,
    save_step_id: Optional[str] = None,
) -> LocalDrillCampaign:
    """Set up the five drills for one save of the practice case; trust the key.

    Idempotent: a drill already scoped is kept exactly as it is.
    Changing the project invalidates earlier case results and certification,
    as any project change does, so prepare drills before running cases.
    """

    from openadapt_flow.qualification import (
        QualificationActionTarget,
        QualificationCase,
        QualificationCaseKind,
        QualificationOutcome,
        add_case,
        set_case_scope,
        set_trusted_fault_driver_key,
    )
    from openadapt_flow.run_gate import is_consequential
    from openadapt_flow.traversal import iter_workflow_steps

    project = workflow.qualification
    if project is None:
        raise LocalDrillError("set up Check it (the qualification project) first")
    representatives = [
        case
        for case in project.cases
        if case.kind is QualificationCaseKind.REPRESENTATIVE
        and (from_case_id is None or case.id == from_case_id)
    ]
    if from_case_id is not None and not representatives:
        raise LocalDrillError("the named practice case isn't in this project")
    if len(representatives) != 1:
        raise LocalDrillError(
            "name the practice case to drill from (--from-case); this project "
            f"has {len(representatives)}"
        )
    practice = representatives[0]
    if practice.runtime_input_sha256 is None or not practice.action_targets:
        raise LocalDrillError(
            "the practice case has no inputs and action scope yet; run the "
            "practice item's setup first"
        )
    steps = {step.id: step for step in iter_workflow_steps(workflow)}
    order = {step_id: index for index, step_id in enumerate(steps)}
    saves = sorted(
        (
            target
            for target in practice.action_targets
            if target.step_id in steps
            and is_consequential(steps[target.step_id], workflow)
        ),
        key=lambda target: order.get(target.step_id, len(order)),
    )
    if save_step_id is not None:
        saves = [target for target in saves if target.step_id == save_step_id]
        if not saves:
            raise LocalDrillError(
                "the named step isn't a save the practice case reaches"
            )
    if not saves:
        raise LocalDrillError(
            "the practice case reaches no save, so there's nothing to drill"
        )
    save = saves[0]
    set_trusted_fault_driver_key(
        workflow, key_id=key.key_id, public_key_base64=key.public_key_base64
    )
    drills: list[DrillCase] = []
    skipped: list[tuple[str, str]] = []
    fault_target = QualificationActionTarget(
        step_id=save.step_id, actuation_path=save.actuation_path
    )
    for kind, slug, name in DRILLS:
        if save.actuation_path == "api" and kind in _SCREEN_KINDS:
            skipped.append((kind, "api_save_has_no_screen_target"))
            continue
        existing = [case for case in project.cases if case.kind.value == kind]
        if existing:
            # A project starts with one required case per fault kind. Those
            # are the drills Check it shows: give an unscoped one the practice
            # case's inputs and save, and never change one already scoped.
            for case in existing:
                if case.runtime_input_sha256 is None or not case.action_targets:
                    set_case_scope(
                        workflow,
                        case_id=case.id,
                        runtime_input_sha256=practice.runtime_input_sha256,
                        action_targets=list(practice.action_targets),
                        fault_target=fault_target,
                    )
                    drills.append(
                        DrillCase(
                            case.id,
                            kind,
                            name,
                            save.step_id,
                            save.actuation_path,
                            "scoped",
                        )
                    )
                    continue
                target = case.resolved_fault_target()
                drills.append(
                    DrillCase(
                        case.id,
                        kind,
                        name,
                        target.step_id if target is not None else "",
                        target.actuation_path if target is not None else "gui",
                        "kept",
                    )
                )
            continue
        case_id = _drill_case_id(slug, save.step_id)
        if any(case.id == case_id for case in project.cases):
            raise LocalDrillError(
                f"case {case_id!r} already exists as another kind of case; "
                "remove it or choose another save"
            )
        add_case(
            workflow,
            QualificationCase(
                id=case_id,
                kind=QualificationCaseKind(kind),
                description=f"Readiness drill: {name}. Must stop before saving.",
                input_ref=practice.input_ref,
                expected_outcome=QualificationOutcome.HALTED,
            ),
        )
        set_case_scope(
            workflow,
            case_id=case_id,
            runtime_input_sha256=practice.runtime_input_sha256,
            action_targets=list(practice.action_targets),
            fault_target=fault_target,
        )
        drills.append(
            DrillCase(case_id, kind, name, save.step_id, save.actuation_path, "added")
        )
    return LocalDrillCampaign(
        from_case_id=practice.id,
        save_step_id=save.step_id,
        driver_id=LOCAL_DRILL_DRIVER_ID,
        driver_key_id=key.key_id,
        runtime_input_sha256=practice.runtime_input_sha256,
        drills=tuple(drills),
        skipped=tuple(skipped),
    )


# -- the driver ---------------------------------------------------------------------


class _ScreenOnlyRecordCheck:
    """The weak-record-check drill's stand-in: it declares a screen read.

    It has no other method. The engine must refuse it on its declared tier
    before it reads or writes anything.
    """

    verification_tier = VerificationTier.IMMEDIATE_SCREEN


def _rotate(value: str) -> str:
    """A different value of the same shape: letters shift by 13, digits by 5."""

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


_SUBSTITUTE_JS = """(pairs) => {
  const skip = new Set(['INPUT', 'TEXTAREA', 'SELECT', 'OPTION', 'SCRIPT',
                        'STYLE', 'NOSCRIPT', 'TEMPLATE']);
  const swap = (text) => {
    let next = text;
    for (const [from, to] of pairs) {
      if (next.includes(from)) { next = next.split(from).join(to); }
    }
    return next;
  };
  const inside = (el) => {
    for (let p = el; p; p = p.parentElement) {
      if (skip.has(p.tagName) || p.isContentEditable) { return true; }
    }
    return false;
  };
  let changed = 0;
  if (!document.body) { return 0; }
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  const nodes = [];
  while (walker.nextNode()) { nodes.push(walker.currentNode); }
  for (const node of nodes) {
    if (!node.parentElement || inside(node.parentElement)) { continue; }
    const next = swap(node.nodeValue || '');
    if (next !== node.nodeValue) { node.nodeValue = next; changed += 1; }
  }
  const named = '[aria-label], [title], [alt], [data-openadapt-identity]';
  for (const el of document.querySelectorAll(named)) {
    if (inside(el)) { continue; }
    for (const name of ['aria-label', 'title', 'alt', 'data-openadapt-identity']) {
      const value = el.getAttribute(name);
      if (value === null) { continue; }
      const next = swap(value);
      if (next !== value) { el.setAttribute(name, next); changed += 1; }
    }
  }
  return changed;
}"""

_DUPLICATE_JS = """(el) => {
  const copy = el.cloneNode(true);
  el.parentNode.insertBefore(copy, el.nextSibling);
  return 1;
}"""


_AWAIT_PAINT_JS = """() => new Promise((done) => {
  requestAnimationFrame(() => requestAnimationFrame(() => done(true)));
})"""


def _await_paint(page: Any) -> None:
    """Let the browser paint a page change before any frame is taken.

    Two animation frames: the first runs before the change is painted, the
    second after. A page that can't answer is left to the frame check below.
    """

    for frame in list(getattr(page, "frames", None) or [page]):
        try:
            frame.evaluate(_AWAIT_PAINT_JS)
        except Exception:  # noqa: BLE001 - the stability check still applies
            continue


def _settled_screenshot(
    capture: Any,
    *,
    attempts: int = 40,
    interval_s: float = 0.1,
    stable_frames: int = 3,
) -> Optional[bytes]:
    """A frame taken, with the detector's own capture, once the page settled.

    The receipt names the exact frame the engine's detector then reads, so the
    change must have finished rendering: ``stable_frames`` consecutive
    captures must be byte-identical. Right after a text change a browser can
    still re-rasterize an edge for a few hundred milliseconds, and a receipt
    for such a frame would name bytes the detector never sees (the drill then
    fails, safely, but for the wrong reason). ``None`` if it never settles.
    """

    import time

    previous: Optional[bytes] = None
    same = 0
    for _ in range(attempts):
        try:
            frame = capture()
        except Exception:  # noqa: BLE001
            return None
        if previous is not None and frame == previous:
            same += 1
            if same >= stable_frames - 1:
                return frame
        else:
            same = 0
        previous = frame
        time.sleep(interval_s)
    return None


class LocalDrillDriver:
    """The local campaign's fault driver (see the module docstring).

    Holds the case's input values in memory only, to find them on the page.
    The receipt and the mutation record carry counts and digests, never a
    value.
    """

    driver_id = LOCAL_DRILL_DRIVER_ID
    contract_sha256 = LOCAL_DRILL_CONTRACT_SHA256

    def __init__(
        self,
        key: LocalDrillKey,
        *,
        workflow: "Workflow",
        params: Optional[dict[str, Any]] = None,
    ) -> None:
        from openadapt_flow.traversal import iter_workflow_steps

        self._key = key
        self.attestation_key_id = key.key_id
        self._steps: dict[str, "Step"] = {
            step.id: step for step in iter_workflow_steps(workflow)
        }
        secret_names = set(getattr(workflow, "secret_params", None) or ())
        pairs: dict[str, str] = {}
        for name, value in (params or {}).items():
            if name in secret_names or not isinstance(value, str):
                continue
            text = value.strip()
            if sum(char.isalnum() for char in text) < 2:
                continue
            for variant in {text, text.upper(), text.lower(), text.title()}:
                altered = _rotate(variant)
                if altered != variant:
                    pairs[variant] = altered
        # Longest first, so a value inside another value can't pre-empt it.
        self._pairs = sorted(pairs.items(), key=lambda item: -len(item[0]))
        self._artifacts: dict[str, bytes] = {}
        #: Why the last drill could not be staged here, as a closed code.
        self.declined: Optional[str] = None

    # -- QualificationFaultDriver -------------------------------------------------
    def mutate(
        self, context: QualificationFaultContext
    ) -> Optional[QualificationFaultMutation]:
        kind = context.fault_kind
        replacement: Any = context.effect_verifier
        replace_effect_verifier = False
        if kind == "missing_effect":
            replacement = None
            replace_effect_verifier = True
            method, changed = "record_check_removed", 1
            after = effect_verifier_input_sha256(replacement, context.effects)
        elif kind == "weak_effect":
            replacement = _ScreenOnlyRecordCheck()
            replace_effect_verifier = True
            method, changed = "record_check_screen_only", 1
            after = effect_verifier_input_sha256(replacement, context.effects)
        else:
            page = getattr(context.backend, "page", None)
            if context.actuation_path != "gui" or page is None:
                self.declined = "screen_drill_needs_a_browser"
                return None
            if kind == "ambiguity":
                method = "save_target_duplicated"
                changed = self._duplicate_target(context)
            else:
                method = "record_identity_replaced"
                changed = self._replace_identity(context, page)
            if changed <= 0:
                self.declined = (
                    "save_target_not_found"
                    if kind == "ambiguity"
                    else "record_identity_not_on_page"
                )
                return None
            capture = (
                getattr(context, "capture_frame", None) or context.backend.screenshot
            )
            _await_paint(page)
            frame = _settled_screenshot(capture)
            if frame is None:
                self.declined = "page_did_not_settle"
                return None
            after = sha256_bytes(frame)
        artifact = json.dumps(
            {
                "schema": MUTATION_SCHEMA,
                "driver_id": self.driver_id,
                "fault_kind": kind,
                "gate": context.gate,
                "method": method,
                "changed": int(changed),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
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
            mutation_artifact_sha256=sha256_bytes(artifact),
            attestation_key_id=self.attestation_key_id,
        )
        signed = sign_fault_mutation_receipt(receipt, private_key=self._key.private_key)
        self._artifacts[signed.receipt_sha256()] = artifact
        self.declined = None
        return QualificationFaultMutation(
            receipt=signed,
            replace_effect_verifier=replace_effect_verifier,
            effect_verifier=replacement,
        )

    def mutation_bytes(self, receipt: FaultMutationReceipt) -> Optional[bytes]:
        """The exact mutation record a signed receipt names, if this driver made it."""

        return self._artifacts.get(receipt.receipt_sha256())

    # -- page changes -------------------------------------------------------------
    def _save_target(self, context: QualificationFaultContext) -> Any:
        """The save's own element, found by its recorded structural locator."""

        step = self._steps.get(context.step_id)
        locator = (
            step.anchor.structural
            if step is not None and step.anchor is not None
            else None
        )
        scoped = getattr(context.backend, "_locator_with_scope", None)
        if locator is None or not callable(scoped):
            return None
        try:
            resolved = scoped(locator)
            if resolved is None:
                return None
            _scope, target = resolved
            if target.count() != 1:
                return None
            return target.first
        except Exception:  # noqa: BLE001 - a drill that can't stage declines
            return None

    def _duplicate_target(self, context: QualificationFaultContext) -> int:
        target = self._save_target(context)
        if target is None:
            return 0
        try:
            return int(target.evaluate(_DUPLICATE_JS))
        except Exception:  # noqa: BLE001 - a drill that can't stage declines
            return 0

    def _record_identity_pairs(
        self, context: QualificationFaultContext
    ) -> list[tuple[str, str]]:
        """Replacements for the record identity the engine reads at the save.

        The engine's structured identity is the text of the record row around
        the save target (``structured_text_at``). Each word of it is replaced
        with a different word of the same shape, so the page shows another
        record. The text is held in memory only.
        """

        target = self._save_target(context)
        reader = getattr(context.backend, "structured_text_at", None)
        if target is None or not callable(reader):
            return []
        try:
            box = target.bounding_box()
            if not box:
                return []
            text = reader(
                int(round(float(box["x"]) + float(box["width"]) / 2)),
                int(round(float(box["y"]) + float(box["height"]) / 2)),
            )
        except Exception:  # noqa: BLE001
            return []
        pairs: dict[str, str] = {}
        for word in re.findall(r"[0-9A-Za-z\u00C0-\u024F]{2,}", text or ""):
            altered = _rotate(word)
            if altered != word:
                pairs[word] = altered
        return list(pairs.items())

    def _replace_identity(self, context: QualificationFaultContext, page: Any) -> int:
        pairs = dict(self._pairs)
        pairs.update(self._record_identity_pairs(context))
        if not pairs:
            return 0
        ordered = sorted(pairs.items(), key=lambda item: -len(item[0]))
        changed = 0
        try:
            frames = list(getattr(page, "frames", None) or [page])
            for frame in frames:
                changed += int(frame.evaluate(_SUBSTITUTE_JS, ordered))
        except Exception:  # noqa: BLE001 - a drill that can't stage declines
            return 0
        return changed


def write_drill_evidence(run_dir: Path, driver: Any, report: Any) -> dict[str, Any]:
    """Keep a drill's signed receipt and mutation record next to its report.

    Desktop records them as the ``fault_receipt`` and ``fault_mutation``
    evidence of the case result. Returns a PHI-free summary. Sealed in memory
    when the run directory is encrypted (the run of a sealed bundle).
    """
    from openadapt_flow import sealed_set as _sealed

    folder = Path(run_dir) / "qualification-fault"
    receipts = list(getattr(report, "qualification_fault_mutations", None) or [])
    summary: dict[str, Any] = {
        "schema": "openadapt.local-drill-evidence/v1",
        "staged": bool(receipts),
        "declined": getattr(driver, "declined", None) if not receipts else None,
    }
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    if receipts:
        receipt = receipts[0]
        _sealed.write_bytes(folder / "receipt.json", receipt.artifact_bytes())
        summary["receipt"] = "qualification-fault/receipt.json"
        summary["receipt_sha256"] = receipt.receipt_sha256()
        mutation_bytes = getattr(driver, "mutation_bytes", None)
        artifact = mutation_bytes(receipt) if callable(mutation_bytes) else None
        if artifact is not None:
            _sealed.write_bytes(folder / "mutation.json", artifact)
            summary["mutation"] = "qualification-fault/mutation.json"
            summary["mutation_sha256"] = hashlib.sha256(artifact).hexdigest()
    _sealed.write_text(
        folder / "status.json", json.dumps(summary, sort_keys=True, indent=2) + "\n"
    )
    return summary


__all__ = [
    "DRILLS",
    "LOCAL_DRILL_CONTRACT_SHA256",
    "LOCAL_DRILL_DRIVER_ID",
    "DrillCase",
    "LocalDrillCampaign",
    "LocalDrillDriver",
    "LocalDrillError",
    "LocalDrillKey",
    "local_drill_key",
    "prepare_local_drill_campaign",
    "project_trusts_local_key",
    "write_drill_evidence",
]
