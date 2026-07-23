#!/usr/bin/env python3
"""Real-RDP vision-ladder qualification: record -> compile -> replay over a
genuine RDP round-trip, asserting the validation contract.

This is the missing live analog of the desktop structural proof (#102) and the
RDP transport/input proof (#142) for the VISION-ONLY resolution ladder: it drives
the production Recorder -> compiler -> governed Replayer classes over a real
RDP pixel surface with NO structural (a11y/UIA) backend, so resolution can only
go through the visual rungs (template -> template_global -> ocr -> geometry).
The harness binds explicit pixel identities, policy, encryption, and a typed
document effect contract before replay. It asserts:

  * record -> compile -> replay succeeds on a healthy run,
  * ZERO model calls on the healthy run (the $0 deterministic guarantee),
  * resolution used the VISUAL rungs and NEVER the structural rung,
  * the write EFFECT is confirmed both by the runtime's exactly-one-new-document
    verifier and by an independent exact-content host read,
  * the ladder HALTS (never silently mis-clicks) when the frame is degraded by
    injected DPI/theme/JPEG-compression drift (simulated drift on a real
    session).

The RDP surface is the `benchmark/rdp_ladder/fixture` container: a deterministic
Tk kiosk app served by a FreeRDP *server* and rendered back by a FreeRDP
*client*, so the pixels this harness reads and the input it injects both cross a
genuine RDP protocol exchange. See that directory's README for why FreeRDP (not
the product's aardwolf client) is used for the Linux CI surface, and how this
complements the aardwolf-over-Windows transport qualification in `benchmark/rdp`.

Honest scope: this qualifies the VISUAL RESOLUTION LADDER + CONTRACT over real
RDP-transported pixels. It is NOT a Citrix ICA/HDX proof, NOT an aardwolf
transport proof (that is `benchmark/rdp`), and the injected drift is
simulated-drift-on-a-real-session, not WAN capture.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import secrets
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from PIL import Image, ImageOps

# -- fixture geometry (kiosk_app.py renders these at fixed positions) ----------
VIEWPORT = (1280, 800)
ADA_ROW = (347, 192)  # "Ada Lovelace   MRN A1001" roster row
GRACE_ROW = (347, 262)  # "Grace Hopper   MRN B2002" roster row
# Click close enough to the Entry's left edge that the 160px recorded target
# crop includes its stable border. The former interior point produced a nearly
# blank/generic template that could resolve with a small offset on the RDP
# surface; translating the patient-identifier crop by that offset correctly
# made identity abstain. A distinctive target crop keeps resolution and the
# separately verified active-patient region in the same coordinate frame.
NOTE_FIELD = (120, 588)  # clinical-note entry, left-border-bearing crop
SAVE_BUTTON = (910, 588)  # "Save Note" button
ADA_IDENTIFIER_REGION = (60, 168, 500, 48)
# After Ada is selected, both the field-focus click and the consequential Save
# click bind to the live selected-record banner, not merely to a blank field or
# generic button. This is the pixel-only equivalent of verifying the active
# patient context before continuing and before writing.
ACTIVE_PATIENT_IDENTIFIER_REGION = (55, 458, 620, 40)
NOTE_PARAM = "note"
NOTE_VALUE = "followup in two weeks"
EXPECTED_MRN = "MRN A1001"  # Ada's MRN, written by the kiosk on save
ORACLE_FILENAME = "saved_note.txt"
RESET_ACK_FILENAME = "reset_ack.txt"
TRIALS_PER_CONDITION = 3
RESULT_SCHEMA = "openadapt.rdp-ladder-qualification.v2"
POLICY_PATH = Path(__file__).with_name("policy.yaml")
REPO_ROOT = Path(__file__).resolve().parents[2]

# xdotool keysym map for the transport (named keys + space).
_XDOTOOL_KEYS = {
    "enter": "Return",
    "tab": "Tab",
    "escape": "Escape",
    "backspace": "BackSpace",
    "delete": "Delete",
    "space": "space",
    "home": "Home",
    "end": "End",
    "pageup": "Prior",
    "pagedown": "Next",
    "up": "Up",
    "down": "Down",
    "left": "Left",
    "right": "Right",
    "ctrl": "ctrl",
    "shift": "shift",
    "alt": "alt",
    "meta": "super",
    # xdotool parses punctuation tokens as key-sequence syntax (and a leading
    # "-" as an option-like invalid sequence), so use the unambiguous X11
    # keysym names for punctuation present in parameterized fixture values.
    "-": "minus",
    "/": "slash",
}


class DockerX11RdpTransport:
    """`RDPTransport` over the FreeRDP client display inside the fixture
    container: `framebuffer()` screenshots the RDP-decoded client display and
    `pointer/key/wheel` inject X events that the FreeRDP client forwards over
    the RDP wire to the kiosk. Both directions traverse real RDP.

    Uses `docker exec` so the flow stack runs on the host (where it is
    installed) while the RDP round-trip stays in the container.
    """

    def __init__(
        self,
        container: str,
        *,
        display: str = ":1",
        width: int = 1280,
        height: int = 800,
    ) -> None:
        self._c = container
        self._display = display
        self._w, self._h = width, height
        self._last_pointer: Optional[tuple[int, int]] = None

    def _exec(self, args: list[str], *, binary: bool = False):
        cmd = ["docker", "exec", "-e", f"DISPLAY={self._display}", self._c, *args]
        res = subprocess.run(cmd, capture_output=True, timeout=30, check=False)
        if res.returncode != 0 and not binary:
            raise RuntimeError(
                f"docker exec {args!r} failed rc={res.returncode}: "
                f"{res.stderr.decode(errors='replace')[:200]}"
            )
        return res.stdout

    # -- RDPTransport --------------------------------------------------------
    def connect(self) -> None:
        # Wait for a non-blank RDP frame (the kiosk painted through the wire).
        for _ in range(40):
            _, w, h = self.framebuffer()
            if (w, h) == (self._w, self._h):
                img = self._grab()
                if len(img.getcolors(maxcolors=1 << 24) or []) > 1:
                    self._focus_client()
                    # FreeRDP establishes its X11 input grab asynchronously.
                    # Focus once per connection, then leave it alone: repeated
                    # focus calls immediately before XTest motion can suppress
                    # that motion while still allowing the later button edge.
                    time.sleep(3.0)
                    return
            time.sleep(0.5)
        raise RuntimeError("no painted RDP frame within timeout")

    def disconnect(self) -> None:
        pass

    def _grab(self) -> Image.Image:
        out = self._exec(["import", "-window", "root", "png:-"], binary=True)
        if not out:
            raise RuntimeError("empty framebuffer capture")
        return Image.open(io.BytesIO(out)).convert("RGB")

    def framebuffer(self):
        img = self._grab()
        return img, img.width, img.height

    def _focus_client(self) -> None:
        """Focus the isolated FreeRDP window before injecting XTest input.

        The fixture runs a minimal Openbox session. Focusing its only visible
        FreeRDP window once is deterministic and remains entirely inside
        display ``:1`` in the container.
        """
        self._exec(
            [
                "xdotool",
                "search",
                "--onlyvisible",
                "--name",
                "^FreeRDP:",
                "windowfocus",
                "%@",
            ]
        )

    def _remote_pointer(self) -> Optional[tuple[int, int]]:
        """Return the fixture server's cursor as a delivery acknowledgement.

        This is deliberately fixture-only.  Input is still injected into the
        FreeRDP *client* on display ``:1`` and crosses the RDP wire; polling
        display ``:0`` merely prevents a button edge from racing an
        asynchronous MotionNotify in this two-Xvfb qualification laboratory.
        """
        cmd = [
            "docker",
            "exec",
            "-e",
            "DISPLAY=:0",
            self._c,
            "xdotool",
            "getmouselocation",
            "--shell",
        ]
        res = subprocess.run(cmd, capture_output=True, timeout=10, check=False)
        if res.returncode != 0:
            return None
        fields = {}
        for line in res.stdout.decode(errors="replace").splitlines():
            key, sep, value = line.partition("=")
            if sep:
                fields[key] = value
        try:
            return int(fields["X"]), int(fields["Y"])
        except (KeyError, ValueError):
            return None

    def pointer(self, x: int, y: int, button: str, down: bool) -> None:
        btn = {"left": "1", "right": "3", "middle": "2"}.get(button, "1")
        self._last_pointer = (int(x), int(y))
        # Deliver the complete gesture in one xdotool/X11 client on the DOWN
        # edge. Separate docker-exec clients for mousedown and mouseup can lose
        # the held-button state before FreeRDP forwards it. Keeping both edges
        # in one invocation, with a conservative hold, makes the virtual RDP
        # wire deterministic. The backend's matching UP edge is then a no-op.
        if not down:
            return
        target = (int(x), int(y))
        delivered = False
        for _attempt in range(3):
            self._exec(
                [
                    "xdotool",
                    "mousemove",
                    str(target[0]),
                    str(target[1]),
                ]
            )
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                if self._remote_pointer() == target:
                    delivered = True
                    break
                time.sleep(0.1)
            if delivered:
                break
        if not delivered:
            raise RuntimeError(
                f"RDP fixture did not acknowledge pointer motion to {target}"
            )
        self._exec(["xdotool", "click", btn])

    def key(self, keysym_or_char: str, down: bool) -> None:
        keysym = _XDOTOOL_KEYS.get(keysym_or_char, keysym_or_char)
        if keysym == " ":
            keysym = "space"
        # A single printable character is delivered as one atomic press+release
        # on the DOWN edge (xdotool `key`), which is far more reliable through
        # the RDP client than separate synthetic keydown/keyup scancodes; the
        # UP edge is then a no-op. Named keys / modifiers keep true down/up so
        # chords still nest correctly.
        if len(keysym) == 1 or keysym == "space":
            if down:
                self._exec(["xdotool", "key", "--clearmodifiers", keysym])
            return
        verb = "keydown" if down else "keyup"
        self._exec(["xdotool", verb, "--clearmodifiers", keysym])

    def wheel(self, dx: int, dy: int) -> None:
        if not dy:
            return
        btn = "5" if dy > 0 else "4"
        for _ in range(max(1, abs(dy) // 100)):
            self._exec(["xdotool", "click", btn])


# -- injected-drift wrapper (simulated drift on a real RDP session) ------------
class _DriftBackend:
    """Wraps a Backend and degrades every screenshot with DPI + theme + JPEG
    drift, so the resolver sees a realistically-degraded remote frame while the
    REAL RDP session is unchanged. Used only for the halt-under-drift case."""

    def __init__(
        self,
        backend,
        *,
        downscale: float = 0.4,
        invert: bool = True,
        jpeg_quality: int = 8,
    ) -> None:
        self._b = backend
        self._ds, self._invert, self._q = downscale, invert, jpeg_quality

    def __getattr__(self, name):
        return getattr(self._b, name)

    def screenshot(self) -> bytes:
        img = Image.open(io.BytesIO(self._b.screenshot())).convert("RGB")
        w, h = img.size
        # Severe DPI/scale drift: downsample hard then back up -- destroys the
        # fine glyph/edge detail the template AND OCR rungs rely on (a laggy,
        # low-bandwidth ICA/HDX frame), so a conservative ladder must halt
        # rather than resolve on degraded pixels.
        img = img.resize(
            (max(1, int(w * self._ds)), max(1, int(h * self._ds))), Image.BILINEAR
        ).resize((w, h), Image.BILINEAR)
        if self._invert:  # theme inversion
            img = ImageOps.invert(img)
        buf = io.BytesIO()  # heavy ICA/HDX-like JPEG blocking
        img.save(buf, format="JPEG", quality=self._q)
        out = Image.open(io.BytesIO(buf.getvalue())).convert("RGB")
        png = io.BytesIO()
        out.save(png, format="PNG")
        return png.getvalue()


def _read_saved_note(oracle_root: Path) -> Optional[str]:
    """Read the bind-mounted document oracle without using the GUI/RDP path."""
    try:
        return (oracle_root / ORACLE_FILENAME).read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _read_reset_ack(oracle_root: Path) -> Optional[int]:
    try:
        return int((oracle_root / RESET_ACK_FILENAME).read_text().strip())
    except (OSError, ValueError):
        return None


def _reset_kiosk(container: str, oracle_root: Path) -> None:
    """Restore the kiosk to its initial state between trials via an IN-PLACE
    reset (SIGUSR1): the kiosk clears the form and deletes the saved note
    without destroying its window, so the RDP display never blanks and
    keyboard-focus continuity is preserved (see kiosk_app.py). The reset must
    be acknowledged by the Tk thread and the external oracle must be empty;
    otherwise the trial refuses to start instead of reusing stale state."""
    before = _read_reset_ack(oracle_root)
    res = subprocess.run(
        ["docker", "exec", container, "pkill", "-USR1", "-f", "kiosk_app.py"],
        capture_output=True,
        timeout=30,
        check=False,
    )
    if res.returncode != 0:
        raise RuntimeError(
            "fixture reset signal failed: " + res.stderr.decode(errors="replace")[:200]
        )
    deadline = time.monotonic() + 8.0
    while time.monotonic() < deadline:
        after = _read_reset_ack(oracle_root)
        advanced = after is not None and (before is None or after > before)
        if advanced and _read_saved_note(oracle_root) is None:
            return
        time.sleep(0.1)
    raise RuntimeError("fixture reset was not acknowledged with an empty oracle")


def _arm_recorded_identifiers(recording_dir: Path) -> None:
    """Bind the entity and write clicks to explicit recorded pixel identities."""
    path = recording_dir / "events.jsonl"
    events = [json.loads(line) for line in path.read_text().splitlines() if line]
    regions = {
        0: ADA_IDENTIFIER_REGION,
        1: ACTIVE_PATIENT_IDENTIFIER_REGION,
        3: ACTIVE_PATIENT_IDENTIFIER_REGION,
    }
    for index, region in regions.items():
        if index >= len(events) or events[index].get("kind") != "click":
            raise RuntimeError(f"recording event {index} is not the expected click")
        events[index]["identifier_region"] = list(region)
    path.write_text(
        "".join(json.dumps(event, sort_keys=True) + "\n" for event in events),
        encoding="utf-8",
    )


def _fixture_versions(container: str) -> dict[str, str]:
    """Capture exact installed fixture package versions for the evidence."""
    packages = (
        "freerdp3-shadow-x11",
        "freerdp3-x11",
        "imagemagick",
        "openbox",
        "xdotool",
    )
    res = subprocess.run(
        [
            "docker",
            "exec",
            container,
            "dpkg-query",
            "-W",
            "-f=${Package}=${Version}\\n",
            *packages,
        ],
        capture_output=True,
        timeout=30,
        check=False,
    )
    if res.returncode != 0:
        raise RuntimeError(
            "could not inventory fixture packages: "
            + res.stderr.decode(errors="replace")[:200]
        )
    versions: dict[str, str] = {}
    for line in res.stdout.decode(errors="replace").splitlines():
        name, sep, version = line.partition("=")
        if sep:
            versions[name] = version
    if set(versions) != set(packages):
        raise RuntimeError(f"incomplete fixture package inventory: {versions}")
    return versions


def _git_output(*args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(REPO_ROOT), *args],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed: {result.stderr.strip()[:200]}"
        )
    return result.stdout.strip()


def _validate_source_provenance(candidate_commit: str, base_commit: str) -> None:
    """Refuse evidence that is not bound to the exact clean source checkout."""
    for label, commit in (("candidate", candidate_commit), ("base", base_commit)):
        if len(commit) != 40 or any(char not in "0123456789abcdef" for char in commit):
            raise RuntimeError(f"{label} commit must be a full lowercase git SHA")

    head = _git_output("rev-parse", "HEAD")
    if candidate_commit != head:
        raise RuntimeError(
            f"candidate commit {candidate_commit} does not match checkout HEAD {head}"
        )
    merge_base = _git_output("merge-base", candidate_commit, "origin/main")
    if base_commit != merge_base:
        raise RuntimeError(
            f"base commit {base_commit} does not match origin/main merge-base "
            f"{merge_base}"
        )
    dirty = _git_output("status", "--porcelain", "--untracked-files=no")
    if dirty:
        raise RuntimeError(
            "qualification source checkout has tracked modifications; commit "
            "them before producing candidate-bound evidence"
        )


def _accepted_contract(healthy: list[dict], drift: list[dict]) -> bool:
    """Return true only for the exact fail-closed 3+3 qualification matrix."""
    return (
        len(healthy) == TRIALS_PER_CONDITION
        and len(drift) == TRIALS_PER_CONDITION
        and all(t["passed"] for t in [*healthy, *drift])
        and all(t["model_calls"] == 0 for t in [*healthy, *drift])
        and all(t["structural_rung_used"] == 0 for t in healthy)
        and all(bool(t["visual_rungs_used"]) for t in healthy)
        and all(t["effect_confirmed"] for t in healthy)
        and all(t["runtime_effect_verified"] for t in healthy)
        and all(t["policy_admitted"] for t in healthy)
        and all(t["identity_required"] for t in healthy)
        and all(t["identity_verified"] for t in healthy)
        and not any(t["silent_incorrect_success"] for t in healthy)
        and not any(t["over_halt"] for t in healthy)
        and all(t["halted"] for t in drift)
        and not any(t["silent_write"] for t in drift)
        and not any(t["false_completion"] for t in drift)
        and all(t["policy_bound"] for t in drift)
    )


def _seal_and_admit_workflow(workflow, bundle_dir: Path, oracle_root: Path):
    """Bind the qualification policy/effect and return a governed bundle."""
    from openadapt_flow.deployment import DeploymentConfig, EffectsConfig, PolicySection
    from openadapt_flow.ir import (
        ActionKind,
        Postcondition,
        PostconditionKind,
        Workflow,
    )
    from openadapt_flow.run_gate import evaluate_run_gate
    from openadapt_flow.runtime.effects import (
        DocumentHashVerifier,
        Effect,
        EffectKind,
    )

    if not workflow.steps:
        raise RuntimeError("compiled qualification workflow has no steps")
    select_step = workflow.steps[0]
    if select_step.action is not ActionKind.CLICK:
        raise RuntimeError(
            "first qualification step is not the patient-selection click"
        )
    # The RDP input edge is asynchronous: a visually stable frame can still
    # precede the remote Tk repaint by one protocol round trip. Make this state
    # dependency explicit so replay cannot advance to the note field until the
    # selected-record banner is actually observable. This is a semantic
    # postcondition evaluated on the real RDP-decoded frame, not a fixed sleep.
    select_step.expect = [
        Postcondition(
            kind=PostconditionKind.TEXT_PRESENT,
            text="Active: Ada Lovelace",
        )
    ]
    save_step = workflow.steps[-1]
    if save_step.action is not ActionKind.CLICK or save_step.risk != "irreversible":
        raise RuntimeError(
            "final qualification step is not the irreversible save click"
        )
    save_step.expect = [
        Postcondition(
            kind=PostconditionKind.TEXT_PRESENT,
            text="Saved note for Ada Lovelace",
        )
    ]
    save_step.effects = [
        Effect(
            kind=EffectKind.RECORD_WRITTEN,
            match={"name": ORACLE_FILENAME},
            expected_count=1,
            count_new_only=True,
            # The filesystem substrate's stable operation key is the output
            # path, not the note contents. DocumentHashVerifier exposes that
            # path as ``name``; binding the key to a nonexistent generic
            # ``key`` field would correctly filter every record out and halt
            # even after the exact document landed.
            idempotency_key=ORACLE_FILENAME,
            key_field="name",
        )
    ]

    # A governed run must bind policy/identity/effect decisions to an encrypted,
    # integrity-sealed bundle. The random key protects this ephemeral synthetic
    # bundle without publishing or persisting a credential.
    bundle_key = secrets.token_urlsafe(32)
    workflow.save(bundle_dir, encrypt=True, key=bundle_key)
    workflow = Workflow.load(bundle_dir, key=bundle_key)
    verifier = DocumentHashVerifier(oracle_root, glob=ORACLE_FILENAME)
    deployment = DeploymentConfig(
        effects=EffectsConfig(
            kind="document-hash",
            root=str(oracle_root),
            glob=ORACLE_FILENAME,
        ),
        policy=PolicySection(policy=str(POLICY_PATH)),
    )
    gate_report = evaluate_run_gate(
        workflow,
        bundle_dir=bundle_dir,
        deployment=deployment,
        effect_verifier=verifier,
        policy_source=str(POLICY_PATH),
        strict_templates=True,
        require_encryption=True,
    )
    if not gate_report.passed:
        raise RuntimeError(gate_report.render())
    return workflow, save_step.id, verifier, gate_report


def run_qualification(
    container: str,
    *,
    out_dir: Path,
    oracle_root: Path,
    candidate_commit: str,
    base_commit: str,
    work_dir: Optional[Path] = None,
) -> dict:
    from openadapt_flow.backends.rdp_backend import FreeRDPBackend
    from openadapt_flow.compiler import compile_recording
    from openadapt_flow.recorder import Recorder
    from openadapt_flow.run_gate import build_runtime_authorization
    from openadapt_flow.runtime.replayer import Replayer

    _validate_source_provenance(candidate_commit, base_commit)
    out_dir.mkdir(parents=True, exist_ok=True)
    oracle_root = oracle_root.resolve()
    oracle_root.mkdir(parents=True, exist_ok=True)
    work = work_dir or Path(tempfile.mkdtemp(prefix="oaflow-rdp-ladder-"))
    work.mkdir(parents=True, exist_ok=True)
    trials: list[dict] = []
    t_start = time.monotonic()
    fixture_versions = _fixture_versions(container)

    # ---- Record once, then replay each condition independently --------------
    _reset_kiosk(container, oracle_root)
    transport = DockerX11RdpTransport(container)
    backend = FreeRDPBackend(transport, connect=True)

    rec_dir = work / "recording"
    bundle_dir = work / "bundle"
    rec = Recorder(
        backend,
        rec_dir,
        settle_interval_s=0.3,
        settle_stable_frames=2,
        settle_timeout_s=6.0,
    )
    rec.click(*ADA_ROW)  # select patient (visual rung)
    rec.click(*NOTE_FIELD)  # focus note field
    rec.type_text(NOTE_VALUE, param=NOTE_PARAM)
    rec.click(*SAVE_BUTTON)  # write (irreversible)
    rec.finish()
    _arm_recorded_identifiers(rec_dir)

    workflow = compile_recording(rec_dir, bundle_dir, name="rdp-vision-ladder")
    workflow, save_step_id, verifier, gate_report = _seal_and_admit_workflow(
        workflow, bundle_dir, oracle_root
    )

    healthy_trials: list[dict] = []
    for condition_trial in range(1, TRIALS_PER_CONDITION + 1):
        _reset_kiosk(container, oracle_root)
        note_value = f"{NOTE_VALUE} / healthy-{condition_trial}"
        params = {NOTE_PARAM: note_value}
        authorization = build_runtime_authorization(
            workflow,
            gate_report,
            approval_source="rdp-ladder-qualification",
            params=params,
        )
        replay_backend = FreeRDPBackend(DockerX11RdpTransport(container), connect=True)
        report = Replayer(
            replay_backend,
            poll_interval_s=0.3,
            effect_verifier=verifier,
            governed_authorization=authorization,
            pixel_verify_enabled=True,
        ).run(
            workflow,
            params=params,
            bundle_dir=bundle_dir,
            run_dir=work / f"run_healthy_{condition_trial}",
        )
        expected_saved = f"{EXPECTED_MRN}\t{note_value}"
        saved = _read_saved_note(oracle_root)
        effect_confirmed = saved == expected_saved
        rung_counts = dict(report.rung_counts)
        structural_used = rung_counts.get("structural", 0)
        visual_rungs = {
            k: v
            for k, v in rung_counts.items()
            if k in ("template", "template_global", "ocr", "geometry")
        }
        identity_statuses = {
            result.step_id: (
                result.identity.status if result.identity is not None else None
            )
            for result in report.results
            if result.step_id in report.required_identity_step_ids
        }
        step_by_id = {step.id: step for step in workflow.steps}
        identity_diagnostics = {
            result.step_id: {
                "mode": result.identity.mode if result.identity is not None else None,
                "status": (
                    result.identity.status if result.identity is not None else None
                ),
                # This fixture is synthetic. Retain enough evidence to
                # distinguish a transport/state race from a true identity
                # mismatch without publishing screenshots or tuned thresholds.
                "observed": (
                    result.identity.observed if result.identity is not None else None
                ),
                "error": result.error,
                "resolution": (
                    {
                        "point": list(result.resolution.point),
                        "rung": result.resolution.rung,
                        "confidence": result.resolution.confidence,
                    }
                    if result.resolution is not None
                    else None
                ),
                "recorded_click_point": (
                    list(step_by_id[result.step_id].anchor.click_point)
                    if step_by_id[result.step_id].anchor is not None
                    else None
                ),
            }
            for result in report.results
            if result.step_id in report.required_identity_step_ids
        }
        identity_required = bool(report.required_identity_step_ids)
        identity_verified = identity_required and all(
            identity_statuses.get(step_id) == "verified"
            for step_id in report.required_identity_step_ids
        )
        step_diagnostics = [
            {
                "step_id": result.step_id,
                "ok": result.ok,
                "error": result.error,
                "input_verified": result.input_verified,
                "input_retried": result.input_retried,
                "postconditions_ok": result.postconditions_ok,
                "effect_verified": result.effect_verified,
                "safety_halt": result.safety_halt,
                "resolution_rung": (
                    result.resolution.rung if result.resolution is not None else None
                ),
            }
            for result in report.results
        ]
        runtime_effect_verified = any(
            result.step_id == save_step_id and result.effect_verified is True
            for result in report.results
        )
        policy_admitted = report.governed_policy_name == str(POLICY_PATH)
        silent_incorrect_success = bool(report.success and not effect_confirmed)
        over_halt = bool(not report.success)
        healthy_ok = (
            report.success
            and report.model_calls == 0
            and structural_used == 0
            and bool(visual_rungs)
            and effect_confirmed
            and runtime_effect_verified
            and policy_admitted
            and identity_verified
        )
        trial = {
            "trial": len(trials) + 1,
            "condition_trial": condition_trial,
            "kind": "healthy_record_compile_replay",
            "success": bool(report.success),
            "model_calls": int(report.model_calls),
            "rung_counts": rung_counts,
            "structural_rung_used": int(structural_used),
            "visual_rungs_used": visual_rungs,
            "required_identity_step_ids": report.required_identity_step_ids,
            "identity_statuses": identity_statuses,
            "identity_diagnostics": identity_diagnostics,
            "step_diagnostics": step_diagnostics,
            "identity_required": identity_required,
            "identity_verified": identity_verified,
            "runtime_effect_verified": runtime_effect_verified,
            "policy_admitted": policy_admitted,
            "effect_confirmed": effect_confirmed,
            "effect_expected": expected_saved,
            "effect_observed": saved,
            "silent_incorrect_success": silent_incorrect_success,
            "over_halt": over_halt,
            "passed": bool(healthy_ok),
            "failure_class": (
                None
                if healthy_ok
                else "silent_incorrect_success"
                if silent_incorrect_success
                else "healthy_over_halt"
                if over_halt
                else "healthy_contract_violation"
            ),
        }
        healthy_trials.append(trial)
        trials.append(trial)

    drift_trials: list[dict] = []
    for condition_trial in range(1, TRIALS_PER_CONDITION + 1):
        _reset_kiosk(container, oracle_root)
        note_value = f"{NOTE_VALUE} / drift-{condition_trial}"
        params = {NOTE_PARAM: note_value}
        authorization = build_runtime_authorization(
            workflow,
            gate_report,
            approval_source="rdp-ladder-qualification",
            params=params,
        )
        drift_backend = _DriftBackend(
            FreeRDPBackend(DockerX11RdpTransport(container), connect=True)
        )
        drift_report = Replayer(
            drift_backend,
            poll_interval_s=0.3,
            effect_verifier=verifier,
            governed_authorization=authorization,
            pixel_verify_enabled=True,
        ).run(
            workflow,
            params=params,
            bundle_dir=bundle_dir,
            run_dir=work / f"run_drift_{condition_trial}",
        )
        saved_after_drift = _read_saved_note(oracle_root)
        # Under heavy drift the ladder must halt and must not have silently
        # written the expected or any partial/wrong effect.
        drift_halted = not drift_report.success
        silent_write = saved_after_drift is not None
        drift_no_model = drift_report.model_calls == 0
        policy_bound = drift_report.governed_policy_name == str(POLICY_PATH)
        safety_halt = any(result.safety_halt for result in drift_report.results)
        drift_ok = drift_halted and not silent_write and drift_no_model and policy_bound
        trial = {
            "trial": len(trials) + 1,
            "condition_trial": condition_trial,
            "kind": "halt_under_injected_drift",
            "drift": (
                "downscale_0.4x_blur + theme_invert + jpeg_q8 "
                "(simulated on real session)"
            ),
            "halted": bool(drift_halted),
            "model_calls": int(drift_report.model_calls),
            "silent_write": bool(silent_write),
            "false_completion": bool(drift_report.success),
            "policy_bound": policy_bound,
            "safety_halt": safety_halt,
            "effect_after_drift": saved_after_drift,
            "passed": bool(drift_ok),
            "failure_class": None if drift_ok else "drift_contract_violation",
        }
        drift_trials.append(trial)
        trials.append(trial)

    _reset_kiosk(container, oracle_root)

    successes = sum(1 for t in trials if t["passed"])
    accepted = _accepted_contract(healthy_trials, drift_trials)

    evidence = {
        "schema_version": RESULT_SCHEMA,
        "substrate": "real-rdp-freerdp3-roundtrip-linux-kiosk",
        "candidate_commit": candidate_commit,
        "base_commit": base_commit,
        "task": (
            "record->compile->replay a patient-note write through the "
            "vision-only resolver ladder over a real RDP round-trip, with "
            "no structural backend; confirm the write via an independent "
            "document oracle; and halt under injected DPI/theme/JPEG drift"
        ),
        "contract": {
            "healthy_trials": len(healthy_trials),
            "healthy_zero_model_calls": all(
                t["model_calls"] == 0 for t in healthy_trials
            ),
            "healthy_structural_rung_used": sum(
                t["structural_rung_used"] for t in healthy_trials
            ),
            "healthy_visual_rungs_used": {
                rung: sum(t["visual_rungs_used"].get(rung, 0) for t in healthy_trials)
                for rung in ("template", "template_global", "ocr", "geometry")
                if any(t["visual_rungs_used"].get(rung, 0) for t in healthy_trials)
            },
            "healthy_effects_confirmed": sum(
                bool(t["effect_confirmed"]) for t in healthy_trials
            ),
            "healthy_runtime_effects_verified": sum(
                bool(t["runtime_effect_verified"]) for t in healthy_trials
            ),
            "healthy_identity_verified": sum(
                bool(t["identity_verified"]) for t in healthy_trials
            ),
            "healthy_policy_admitted": sum(
                bool(t["policy_admitted"]) for t in healthy_trials
            ),
            "healthy_silent_incorrect_successes": sum(
                bool(t["silent_incorrect_success"]) for t in healthy_trials
            ),
            "healthy_over_halts": sum(bool(t["over_halt"]) for t in healthy_trials),
            "drift_trials": len(drift_trials),
            "drift_safely_halted": sum(bool(t["halted"]) for t in drift_trials),
            "drift_silent_writes": sum(bool(t["silent_write"]) for t in drift_trials),
            "drift_false_completions": sum(
                bool(t["false_completion"]) for t in drift_trials
            ),
        },
        "environment": {
            "rdp_server": "freerdp-shadow-cli3 (FreeRDP3 server)",
            "rdp_client": "xfreerdp3 (FreeRDP3 client, fullscreen)",
            "surface": "Tk kiosk app served over RDP; observed on client display",
            "backend": "openadapt_flow FreeRDPBackend (pixel-only, use_structural off)",
            "transport": "DockerX11RdpTransport (screenshot+xdotool over the RDP client)",
            "fixture_package_versions": fixture_versions,
            "governed_policy": POLICY_PATH.name,
            "runtime_effect_verifier": "DocumentHashVerifier over bind-mounted oracle",
            "pixel_identity_verify": "enabled for this synthetic qualification only",
            "viewport": list(VIEWPORT),
            "note": (
                "aardwolf (the product's Windows-RDP transport) cannot "
                "connect to Linux RDP servers; see fixture README. The "
                "aardwolf-over-Windows transport is qualified separately in "
                "benchmark/rdp (PR #142)."
            ),
        },
        "oracle": (
            "host read of the bind-mounted kiosk document, plus the runtime "
            "DocumentHashVerifier exactly-one-new-document gate"
        ),
        "failure_taxonomy": [
            "connect_or_frame_failure",
            "healthy_contract_violation",
            "silent_incorrect_success",
            "healthy_over_halt",
            "effect_not_confirmed",
            "identity_not_verified",
            "effect_not_runtime_verified",
            "policy_not_admitted",
            "drift_contract_violation",
            "reset_not_acknowledged",
        ],
        "caveat": (
            "Synthetic Tk task over real FreeRDP-transported pixels + input, "
            "but NOT Citrix ICA/HDX, NOT the aardwolf transport, NOT a Windows "
            "application qualification, and the drift is simulated on the "
            "real protocol session (not WAN capture)."
        ),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "trials": trials,
        "run_count": len(trials),
        "successes": successes,
        "model_calls": sum(t["model_calls"] for t in trials),
        "total_s": round(time.monotonic() - t_start, 3),
        "accepted": bool(accepted),
    }
    return evidence


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--container",
        default=os.environ.get("OAFLOW_RDP_LADDER_CONTAINER", "oaflow-rdp-ladder"),
    )
    ap.add_argument("--oracle-root", type=Path, required=True)
    ap.add_argument("--work-dir", type=Path)
    ap.add_argument(
        "--output", type=Path, default=Path("benchmark/rdp_ladder/results.json")
    )
    ap.add_argument("--candidate-commit", required=True)
    ap.add_argument("--base-commit", required=True)
    args = ap.parse_args()

    out_dir = args.output.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    evidence = run_qualification(
        args.container,
        out_dir=out_dir,
        oracle_root=args.oracle_root,
        work_dir=args.work_dir,
        candidate_commit=args.candidate_commit,
        base_commit=args.base_commit,
    )
    args.output.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")
    payload = json.dumps(evidence, sort_keys=True).encode()
    print(f"evidence sha256: {hashlib.sha256(payload).hexdigest()}")
    print(f"accepted: {evidence['accepted']}  wrote: {args.output}")
    return 0 if evidence["accepted"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
