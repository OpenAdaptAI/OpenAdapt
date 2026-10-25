"""One production configuration, its whole lifecycle, on the real local OpenEMR.

The configuration (docs/COMPATIBILITY.md):

- the **Regulated** profile, governed authorization from the run gate on the
  certified, sealed, pinned bundle (``--pin-digest`` semantics: the gate pins
  the admitted digest and accepts a repaired version only through signed
  lineage);
- durable runs with an encrypted checkpoint store;
- the API tier where admissible (the referral's API step is refused by its
  own proposal on OpenEMR, so its GUI save runs and the report says why);
- pipelined read-back (one window), the deferred record check held in the
  durable pause;
- self-repair on, every fix re-qualified on a test copy and re-admitted with
  signed lineage;
- ``problems_only`` approval: a person sees only problems.

Phases (each writes its evidence under ``--out``):

1. ``admit``: seal the certified referral bundle, qualify the sealed copy on
   this fixture (the six cases, certified under clinical-write), check the
   Regulated run gate, and open the problems-only switch record.
2. ``batch``: new referrals, each item starting while the previous item's
   record check is still running.
3. ``crash``: an item is killed while its record check runs; the resume
   re-runs only the check; the oracle counts one chart.
4. ``drift``: the vendor relabels the save button; the item stops before
   saving; self-repair proposes the fix, a person approves it once, the fix
   is re-qualified on the test copy and re-admitted with lineage, and the
   item resumes and saves once.
5. ``continue``: more referrals on the repaired version, pinned to the
   original digest through the lineage.

The person is scripted (``ScriptedPerson``) and counted: every approval it
gives is in the ledger. The repair agent is a rule-based stand-in that calls
no model (``LabelAgent``); ``--repair-agent module:factory`` swaps in another.
The oracle reads the database directly and counts charts per referral.

    LIFECYCLE_PROOF_STATE=... PYTHONPATH=.:../types \\
        python -m benchmark.lifecycle_proof.run all --out <dir> --keys <dir>

``--keys`` is a directory outside the repository for the bundle key, the
checkpoint key, the local status key and the admission key. Nothing secret is
written under ``--out``.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

HERE = Path(__file__).resolve().parent
FLOW = HERE.parents[1]
VERIFY_YAML = HERE / "verify.yaml"
DEFAULT_SOURCE = (
    Path.home() / "oa/scratch/qualify-referral/certified-referrals-qual10/bundle"
)
POLICY = "clinical-write"
RUNNER_ID = "lifecycle-proof-runner-1"
DEDICATED_USER = "openadapt_actor"
STATUS_KEY_ID = "lifecycle-proof-status-1"

#: Synthetic referrals (benchmark/openemr_api_tier/common.py), unique emails.
BATCH = ("r1-nadia-brennan", "r2-marcus-feld", "r3-ines-carvalho")
CRASH = "r4-theo-lindqvist"
DRIFT = "r5-grace-adeyemi"
CONTINUE = ("r7-arlo-whitcombe", "r8-priya-raman")


# -- small helpers -----------------------------------------------------------------


def _write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True, default=str) + "\n")


def _read(path: Path) -> Any:
    return json.loads(path.read_text())


class Ledger:
    """Append-only record of every phase, item, oracle read and person touch."""

    def __init__(self, out: Path) -> None:
        self.path = out / "ledger.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def add(self, kind: str, **fields: Any) -> None:
        row = {"at": time.time(), "kind": kind, **fields}
        with self._lock, self.path.open("a") as handle:
            handle.write(json.dumps(row, sort_keys=True, default=str) + "\n")
        print(json.dumps(row, default=str), flush=True)


def _keys(keys: Path) -> dict[str, Any]:
    """Keys kept outside the repository, created once."""

    keys.mkdir(parents=True, exist_ok=True, mode=0o700)
    secret = keys / "secrets.json"
    if not secret.exists():
        secret.write_text(
            json.dumps(
                {
                    "bundle_key": secrets.token_urlsafe(32),
                    "checkpoint_key": secrets.token_urlsafe(32),
                    "user_hmac_key": secrets.token_hex(32),
                }
            )
        )
        secret.chmod(0o600)
    values = _read(secret)
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        NoEncryption,
        PrivateFormat,
        PublicFormat,
        load_pem_private_key,
    )

    status_pem = keys / "status-key.pem"
    if not status_pem.exists():
        status_pem.write_bytes(
            Ed25519PrivateKey.generate().private_bytes(
                Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()
            )
        )
        status_pem.chmod(0o600)
    status = load_pem_private_key(status_pem.read_bytes(), password=None)
    import base64

    public = base64.b64encode(
        status.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    ).decode("ascii")
    # Durable resume opens the bundle and the checkpoints with one key
    # (OPENADAPT_BUNDLE_KEY), as `openadapt-flow resume` does.
    values["checkpoint_key"] = values["bundle_key"]
    return {
        **values,
        "status_private": status,
        "status_keys": {STATUS_KEY_ID: public},
        "admission_key_dir": keys / "admission",
    }


def _env(app: Any, keys: dict[str, Any]) -> None:
    app.point_hooks_here()
    os.environ["OPENADAPT_BUNDLE_KEY"] = keys["bundle_key"]
    os.environ["OPENADAPT_NO_LAUNCHER_CHECK"] = "1"


def referral_params(key: str) -> dict[str, str]:
    from benchmark.openemr_api_tier.common import REFERRALS, spec_for

    ref = {r.key: r for r in REFERRALS}[key]
    return {k: str(v) for k, v in spec_for(ref).params().items()}


def _config_and_hooks(
    verify_yaml: Path = VERIFY_YAML,
) -> tuple[Any, Any, dict[str, Any]]:
    from openadapt_flow.qualify.campaign import _default_hooks, lookup_plan
    from openadapt_flow.qualify.config import load_verify_config

    config = load_verify_config(verify_yaml)
    surface, structure = lookup_plan(config)
    lookup = {"use_structural": structure, "deployment_surface": surface}
    return config, _default_hooks(config, None), lookup


# -- the person and the stand-in for Cloud's switch status -----------------------------


class ScriptedPerson:
    """Answers every waiting approval request, and counts each answer."""

    APPROVE = dict(
        decision="approve",
        decided_by="human",
        route="desktop_run_it",
        approver_id="lifecycle-proof-operator",
        auth_method="cloud_aal2",
    )

    def __init__(self, ledger: Ledger) -> None:
        self.ledger = ledger
        self.stores: list[Any] = []
        self.seen: set[str] = set()
        self.touches = 0

    def watch(self, store: Any) -> None:
        self.stores.append(store)

    def sleep(self, seconds: float) -> None:
        time.sleep(min(seconds, 0.5))
        from openadapt_flow.approval_mode.item_approval import make_answer

        for store in list(self.stores):
            for request in store.requests():
                if request.request_id in self.seen:
                    continue
                self.seen.add(request.request_id)
                store.write_answer(make_answer(request, **self.APPROVE))
                self.touches += 1
                self.ledger.add(
                    "person",
                    action="approve",
                    request_id=request.request_id,
                    store=str(store.root) if hasattr(store, "root") else "",
                )


class LocalStatusAuthority:
    """Stands in for Cloud's status route: signs ``on`` for the open switch."""

    def __init__(self, private_key: Any) -> None:
        self.private_key = private_key

    def __call__(self, request: Any) -> Any:
        from openadapt_flow.approval_mode.switch import sign_status

        return sign_status(
            {
                "switch_id": request.switch_id,
                "switch_record_sha256": request.switch_record_sha256,
                "runner_id": request.runner_id,
                "nonce": request.nonce,
                "runner_event_seq": request.runner_event_seq,
                "runner_event_sha256": request.runner_event_sha256,
                "state": "on",
                "off_reason": None,
                "issued_at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
                "key_id": STATUS_KEY_ID,
            },
            self.private_key,
        )


class OpenEMRSignedInUser:
    """P2 for OpenEMR: the signed-in user's login, from the app's own session.

    OpenEMR shows only the display name, and only at wide windows. Its tab
    shell holds the login in ``app_view_model.application_data.user()``.
    This check reads that login from the live page; the frame is not used.
    """

    kind = "login_id"

    def __init__(self, page: Any, expected: str) -> None:
        self._page = page
        self._expected = expected

    def reference(self, key: bytes) -> str:
        from openadapt_flow.approval_mode.gate import dedicated_user_reference

        return dedicated_user_reference(self._expected, key)

    def check(self, frame_png: bytes) -> str:
        try:
            login = self._page.evaluate(
                "() => app_view_model.application_data.user().username()"
            )
        except Exception:  # noqa: BLE001 - an unreadable session is not a match
            return "unreadable"
        if not isinstance(login, str) or not login:
            return "unreadable"
        return "match" if login == self._expected else "mismatch"


@dataclass(frozen=True)
class AppProfile:
    """What the configuration needs to know about one application.

    Everything else (admission, the switch, the gate, durable runs, the
    pipelined record check, repair) is the same for every app.
    """

    name: str
    verify_yaml: Path
    source: Path
    dedicated_user: str
    workflow_id: str
    #: The test copy: ``point_hooks_here()``, ``dump()``, ``restore(path)``
    #: and ``reset_for_admission()`` (or ``fx.reset()``).
    make_app: Callable[[], Any]
    #: The gate's signed-in-user check (P2) for a live page.
    signed_in_user: Callable[[Any, str], Any]
    policy: str = POLICY


def _openemr_app() -> Any:
    from benchmark.lifecycle_proof.fixture import ProofApp

    return ProofApp()


OPENEMR = AppProfile(
    name="openemr",
    verify_yaml=VERIFY_YAML,
    source=DEFAULT_SOURCE,
    dedicated_user=DEDICATED_USER,
    workflow_id="workflow-openemr-referral",
    make_app=_openemr_app,
    signed_in_user=lambda page, user: OpenEMRSignedInUser(page, user),
)


def profile(name: str) -> AppProfile:
    """The profile for ``name`` (``openemr``, ``openimis`` or ``fhir``)."""

    if name == "openemr":
        return OPENEMR
    if name == "openimis":
        from benchmark.second_installation.openimis_profile import OPENIMIS

        return OPENIMIS
    if name == "fhir":
        from benchmark.second_installation.fhir_profile import FHIR

        return FHIR
    raise ValueError(f"unknown app profile {name!r}")


class LabelAgent:
    """A rule-based stand-in for the repair model; it calls no model.

    It reads the live screen and proposes the button whose words look like a
    save (``save``, ``create``, ``confirm``) nearest to where the recorded
    control was. Self-repair's probes, the person and re-qualification decide
    whether the fix is right.
    """

    dialect = "anthropic"
    agent_id = "lifecycle-proof-label-agent"
    requires_model_key = False
    WORDS = ("save", "create", "confirm")

    def propose(self, request: Any) -> Any:
        from openadapt_flow import vision
        from openadapt_flow.repair.self_repair import RepairProposal

        lines = vision.ocr(request.live_png)
        rx, ry = request.recorded_click_point
        best = None
        for line in lines or []:
            text = str(getattr(line, "text", "") or "")
            words = text.casefold().split()
            if not any(w in words for w in self.WORDS):
                continue
            if request.recorded_label and text.strip() == request.recorded_label:
                continue
            x, y, w, h = line.region
            cx, cy = x + w // 2, y + h // 2
            distance = (cx - rx) ** 2 + (cy - ry) ** 2
            if best is None or distance < best[0]:
                best = (distance, cx, cy, text)
        if best is None:
            return None
        _distance, cx, cy, text = best
        return RepairProposal(
            action={"action": "left_click", "coordinate": [int(cx), int(cy)]},
            model_calls=0,
            note=f"the recorded control is gone; '{text}' reads as the save",
        )


# -- one item --------------------------------------------------------------------------


class Item:
    """One referral under the configuration, in this process."""

    def __init__(
        self,
        ctx: dict[str, Any],
        key: str,
        run_dir: Path,
        *,
        pipeline: Any = None,
        verifier_wrapper: Any = None,
        params: Optional[dict[str, str]] = None,
        is_retry: bool = False,
    ) -> None:
        self.ctx = ctx
        self.key = key
        self.run_dir = run_dir
        # A soak item brings its own values; the proof's referrals are named.
        self.params = dict(params) if params is not None else referral_params(key)
        self.pipeline = pipeline
        self.verifier_wrapper = verifier_wrapper
        self.is_retry = is_retry

    def approval_gate(self, bundle: Path, page: Any) -> Any:
        from openadapt_flow.approval_mode.gate import ApprovalGate
        from openadapt_flow.approval_mode.item_approval import ItemApprovalStore
        from openadapt_flow.approval_mode.model import (
            DESKTOP_LIST_WITH_HEADER,
            ItemContext,
        )

        ctx = self.ctx
        store = ItemApprovalStore(self.run_dir)
        ctx["person"].watch(store)

        return ApprovalGate(
            "problems_only",
            record=ctx["switch_record"],
            log=ctx["switch_log"],
            pinned_status_keys=ctx["keys"]["status_keys"],
            fetch_status=LocalStatusAuthority(ctx["keys"]["status_private"]),
            environment_digest=lambda: ctx["environment_digest"],
            runner_id=RUNNER_ID,
            signed_in_user=ctx["profile"].signed_in_user(
                page, ctx["profile"].dedicated_user
            ),
            dedicated_user_key=bytes.fromhex(ctx["keys"]["user_hmac_key"]),
            item=ItemContext(
                channel=DESKTOP_LIST_WITH_HEADER,
                header_maps_every_input=True,
                rows_well_formed=True,
                values_pass_types=True,
                is_retry=self.is_retry,
                allow_repeat=False,
                record_key_seen_recently=False,
                caller_supplied_configuration=False,
                holds_switch_lease=True,
            ),
            approval_store=store,
            approval_wait_s=600.0,
            sleep=ctx["person"].sleep,
            policy_name=ctx["profile"].policy,
        )

    def authorization(self, workflow: Any, bundle: Path, verifier: Any) -> Any:
        from openadapt_flow.deployment import DeploymentConfig, PolicySection
        from openadapt_flow.execution_profiles import (
            ExecutionProfile,
            execution_profile_contract,
        )
        from openadapt_flow.repair.readmission import lineage_check
        from openadapt_flow.run_gate import (
            build_runtime_authorization,
            evaluate_run_gate,
        )

        gate = evaluate_run_gate(
            workflow,
            bundle_dir=bundle,
            deployment=DeploymentConfig(
                policy=PolicySection(policy=self.ctx["profile"].policy)
            ),
            effect_verifier=verifier,
            profile_contract=execution_profile_contract(ExecutionProfile.REGULATED),
            effective_durable=True,
            effective_require_settled=True,
            pinned_content_digest=self.ctx["pinned_digest"],
            pinned_lineage=lineage_check(
                self.ctx["store"], key_dir=self.ctx["keys"]["admission_key_dir"]
            ),
        )
        _write(
            self.run_dir.parent / f"{self.run_dir.name}-gate.json",
            {
                "passed": gate.passed,
                "failed": [
                    {"gate": g.gate, "detail": g.detail}
                    for g in gate.gates
                    if not g.passed
                ],
                "manifest": next(
                    (g.detail for g in gate.gates if g.gate == "manifest"), None
                ),
            },
        )
        if not gate.passed:
            raise RuntimeError("run gate refused: " + gate.render()[-600:])
        return build_runtime_authorization(workflow, gate, params=self.params)

    def replayer_kwargs(self, workflow: Any) -> dict[str, Any]:
        return dict(
            durable=True,
            checkpoint_key=self.ctx["keys"]["checkpoint_key"],
            require_settled=True,
            pixel_verify_enabled=any(
                s.anchor is not None and s.anchor.identifier_crop
                for s in workflow.steps
            ),
            **self.ctx["lookup"],
        )

    def verifier(self) -> Any:
        verifier = self.ctx["hooks"].make_verifier(self.params)
        return self.verifier_wrapper(verifier) if self.verifier_wrapper else verifier

    def run(self, bundle: Path) -> Any:
        from openadapt_flow.ir import Workflow
        from openadapt_flow.runtime import Replayer

        workflow = Workflow.load(bundle)
        backend, close = self.ctx["hooks"].open_session()
        try:
            verifier = self.verifier()
            replayer = Replayer(
                backend,
                effect_verifier=verifier,
                governed_authorization=self.authorization(workflow, bundle, verifier),
                approval_gate=self.approval_gate(bundle, backend.page),
                readback_pipeline=self.pipeline,
                **self.replayer_kwargs(workflow),
            )
            return replayer.run(
                workflow.model_copy(deep=True),
                params=dict(self.params),
                bundle_dir=bundle,
                run_dir=self.run_dir,
                execution_target_kind="web",
                execution_origin=self.ctx["config"].app.origin,
                execution_entry_url=backend.page.url,
                run_id=f"lifecycle-proof-{self.key}-{int(time.time())}",
            )
        finally:
            if self.pipeline is None:
                close()
            else:
                self.ctx.setdefault("closers", []).append(close)


def _summary(report: Any) -> dict[str, Any]:
    save = report.results[-1] if report.results else None
    return {
        "execution_outcome": report.execution_outcome,
        "transaction_outcome": report.transaction_outcome,
        "execution_profile": report.execution_profile,
        "success": report.success,
        "model_calls": report.model_calls,
        "bundle_content_digest": report.bundle_content_digest,
        "approval": (
            report.approval.model_dump(
                mode="json",
                include={"mode", "decision", "decision_reason", "needs_person"},
            )
            if getattr(report, "approval", None) is not None
            else None
        ),
        "save_step": save.step_id if save else None,
        "save_effect_verified": save.effect_verified if save else None,
        "save_api_fallback": save.api_fallback if save else None,
        "readback": [
            r for r in (save.effect_results if save else []) if "[pipelined]" in r
        ][:2],
        "halt": report.halt.reason[:300] if report.halt else None,
    }


# -- the phases ------------------------------------------------------------------------


def _context(
    out: Path, keys_dir: Path, ledger: Ledger, app_profile: Optional[AppProfile] = None
) -> dict[str, Any]:
    app_profile = app_profile or OPENEMR
    app = app_profile.make_app()
    keys = _keys(keys_dir)
    _env(app, keys)
    config, hooks, lookup = _config_and_hooks(app_profile.verify_yaml)
    ctx: dict[str, Any] = {
        "profile": app_profile,
        "app": app,
        "keys": keys,
        "config": config,
        "hooks": hooks,
        "lookup": lookup,
        "ledger": ledger,
        "person": ScriptedPerson(ledger),
        "store": out / "repair-store",
        "out": out,
    }
    admission = out / "admission" / "admission.json"
    if admission.exists():
        from openadapt_flow.approval_mode.switch import SwitchLog, SwitchRecord

        doc = _read(admission)
        ctx["pinned_digest"] = doc["pinned_digest"]
        ctx["bundle"] = Path(doc["bundle"])
        ctx["environment_digest"] = doc["environment_digest"]
    switch = out / "switch" / "switch.json"
    if switch.exists():
        from openadapt_flow.approval_mode.switch import SwitchLog, SwitchRecord

        state = _read(switch)
        ctx["switch_record"] = SwitchRecord.model_validate(state["record"])
        ctx["switch_log"] = SwitchLog(Path(state["log"]))
    current = out / "current-bundle.json"
    if current.exists():
        ctx["bundle"] = Path(_read(current)["bundle"])
    return ctx


def phase_admit(ctx: dict[str, Any], source: Path, trials: int) -> None:
    from openadapt_flow.ir import Workflow
    from openadapt_flow.qualify.campaign import qualify

    out, ledger = ctx["out"], ctx["ledger"]
    t0 = time.time()
    adm = out / "admission"
    adm.mkdir(parents=True, exist_ok=True)
    sealed = adm / "encrypted"
    if not sealed.exists():
        # Encrypted at rest for the Regulated profile: workflow.json and every
        # template are encrypted with the bundle key. A whole-bundle sealed set
        # (``openadapt-flow seal``) can't run with an approval gate yet, since
        # the approval requests the person sees are written for the approval
        # surface to read; see this directory's README. A benchmark harness's
        # notes next to the program (``benchmark-contract.json``) stay out.
        from openadapt_flow.bundle_sealing import _invalidate_prior_certification

        shutil.copytree(
            source, sealed, ignore=shutil.ignore_patterns("benchmark-contract.json")
        )
        workflow = Workflow.load(sealed)
        invalidated = _invalidate_prior_certification(
            workflow, reason="encrypted at rest for the Regulated profile"
        )
        workflow.save(sealed, encrypt=True, key=ctx["keys"]["bundle_key"])
        encrypted = Workflow.load(sealed, key=ctx["keys"]["bundle_key"])
        ledger.add(
            "admit.encrypt",
            source_digest=Workflow.load(source).manifest.content_digest,
            encrypted_digest=encrypted.manifest.content_digest,
            encrypted=encrypted.encrypted,
            certification_invalidated=invalidated,
        )
    reset = getattr(ctx["app"], "reset_for_admission", None)
    (reset or ctx["app"].fx.reset)()
    qual = qualify(
        sealed,
        ctx["config"],
        out=adm / "qualification",
        trials=trials,
        certify=ctx["profile"].policy,
        hooks=ctx["hooks"],
        log=lambda line: ledger.add("admit.qualify.log", line=line[:300]),
    )
    bundle = Path(qual.bundle_dir)
    workflow = Workflow.load(bundle)
    digest = workflow.manifest.content_digest
    ledger.add(
        "admit.qualify",
        passed=qual.passed,
        certified=qual.certified,
        digest=digest,
        rows=[
            {
                k: r.get(k)
                for k in ("case_id", "trial", "expected", "transaction_outcome")
            }
            for r in qual.rows
        ],
        seconds=round(time.time() - t0, 1),
    )
    if not (qual.passed and qual.certified):
        raise SystemExit("qualification did not certify the sealed bundle")
    environment = qual.environment_digest or ""
    if not environment.startswith("sha256:"):
        environment = "sha256:" + environment
    _write(
        adm / "admission.json",
        {
            "pinned_digest": digest,
            "bundle": str(bundle),
            "qualification_dir": str(adm / "qualification"),
            "policy": ctx["profile"].policy,
            "profile": "regulated",
            "environment_digest": environment,
            "qualification_trials": trials,
            "qualification_result": qual.result,
        },
    )
    ctx.update(pinned_digest=digest, bundle=bundle, environment_digest=environment)
    ledger.add("admit", pinned_digest=digest, seconds=round(time.time() - t0, 1))


def _passing_screen_facts(qualification_dir: Path) -> tuple[dict, dict]:
    """The rung floor and dialog dismissals of the passing cases.

    What Cloud records on a switch: per step, the weakest resolution rung a
    VERIFIED representative case used, and the dialogs it dismissed.
    """

    from openadapt_flow.approval_mode.switch import RUNG_ORDER
    from openadapt_flow.ir import RunReport

    floor: dict[str, str] = {}
    dialogs: dict[str, set[str]] = {}
    pattern = "cases/representative/trial-*/run/report.json"
    for path in sorted(qualification_dir.glob(pattern)):
        report = RunReport.model_validate_json(path.read_text())
        if report.execution_outcome != "VERIFIED":
            continue
        for result in report.results:
            for resolution in (result.resolution, result.drag_end_resolution):
                if resolution is None or resolution.rung == "grounder":
                    continue
                prior = floor.get(result.step_id)
                if prior is None or RUNG_ORDER.index(
                    resolution.rung
                ) > RUNG_ORDER.index(prior):
                    floor[result.step_id] = resolution.rung
            for action in result.interstitial_actions:
                dialogs.setdefault(result.step_id, set()).add(action.interstitial)
    return floor, {k: tuple(sorted(v)) for k, v in dialogs.items()}


def phase_switch(ctx: dict[str, Any]) -> None:
    """Open the problems-only switch record for the admitted bundle."""

    from openadapt_flow.approval_mode.gate import dedicated_user_reference
    from openadapt_flow.approval_mode.switch import SwitchLog, SwitchRecord
    from openadapt_flow.qualify.result import verifier_identity_of

    out = ctx["out"]
    doc = _read(out / "admission" / "admission.json")
    current = out / "current-bundle.json"
    if current.exists():
        # A re-admitted repair: the switch Cloud issues for the new version,
        # from the passing cases of its own re-qualification.
        doc = {**doc, **_read(current)}
        digest = doc["digest"]
    else:
        digest = doc["pinned_digest"]
    floor, dialogs = _passing_screen_facts(Path(doc["qualification_dir"]))
    verifier = ctx["hooks"].make_verifier({})
    opened = int(time.time())
    record = SwitchRecord(
        switch_id=f"switch-lifecycle-proof-{digest[:12]}-{opened}",
        org_id="org-lifecycle-proof",
        workflow_id=ctx["profile"].workflow_id,
        runner_id=RUNNER_ID,
        bundle_content_digest=digest,
        environment_digest=doc["environment_digest"],
        verifier_identities=(
            verifier_identity_of(ctx["config"].verifier.verifier, verifier),
        ),
        dedicated_user_ref=dedicated_user_reference(
            ctx["profile"].dedicated_user, bytes.fromhex(ctx["keys"]["user_hmac_key"])
        ),
        rung_floor=floor,
        allowed_interstitials=dialogs,
    )
    log_path = out / "switch" / f"log-{opened}.jsonl"
    log = SwitchLog.open_new(log_path, record)
    _write(
        out / "switch" / "switch.json",
        {"record": record.model_dump(mode="json"), "log": str(log_path)},
    )
    ctx.update(switch_record=record, switch_log=log)
    ctx["ledger"].add(
        "switch.open",
        switch_id=record.switch_id,
        bundle=digest,
        rung_floor=floor,
        dialogs=dialogs,
    )


def _oracle(ctx: dict[str, Any], key: str) -> int:
    email = referral_params(key)["email"]
    count = ctx["app"].charts_for(email)
    ctx["ledger"].add("oracle", item=key, charts=count)
    return count


def phase_batch(ctx: dict[str, Any], keys: tuple[str, ...], name: str) -> list[Any]:
    from openadapt_flow.runtime.readback_pipeline import ReadbackPipeline

    out, ledger = ctx["out"], ctx["ledger"]
    base, attempt = out / name, 1
    while base.exists():
        attempt += 1
        base = out / f"{name}-{attempt}"
    pipeline = ReadbackPipeline()
    handoffs = []
    reports = []
    try:
        for key in keys:
            item = Item(ctx, key, base / key, pipeline=pipeline)
            t0 = time.time()
            report = item.run(ctx["bundle"])
            handoff = pipeline.take()
            ledger.add(
                f"{name}.item",
                item=key,
                returned_s=round(time.time() - t0, 1),
                deferred=handoff is not None,
                inline_reason=pipeline.inline_reason,
            )
            if handoff is not None:
                handoffs.append((key, handoff))
            reports.append((key, report))
        for key, handoff in handoffs:
            handoff.settled.result(timeout=600)
    finally:
        pipeline.close()
        for close in ctx.pop("closers", []):
            close()
    results = []
    for key, report in reports:
        summary = _summary(report)
        summary["charts"] = _oracle(ctx, key)
        ledger.add(f"{name}.result", item=key, **summary)
        results.append(summary)
    return results


def _slow_check(verifier: Any, marker: Path) -> Any:
    """The same record check, made slow, so the kill lands inside its window.

    The verifier object itself is kept: a wrapper would change its identity,
    and the problems-only gate refuses a record check that isn't the one its
    switch record names.
    """

    check = verifier.verify

    def verify(*args: Any, **kwargs: Any) -> Any:
        marker.write_text(str(time.time()))
        time.sleep(120)
        return check(*args, **kwargs)

    verifier.verify = verify
    return verifier


def phase_crash(ctx: dict[str, Any], keys_dir: Path) -> dict[str, Any]:
    """Run one item in a child process and kill it inside the read-back."""

    out, ledger = ctx["out"], ctx["ledger"]
    # A durable run path is claimed once, so a second attempt needs its own.
    base, attempt = out / "crash", 1
    while base.exists():
        attempt += 1
        base = out / f"crash-{attempt}"
    run_dir = base / CRASH
    marker = base / "readback-started"
    marker.parent.mkdir(parents=True)
    child = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "benchmark.lifecycle_proof.run",
            "_crash_child",
            "--out",
            str(out),
            "--keys",
            str(keys_dir),
        ],
        cwd=str(FLOW),
        env={**os.environ, "LIFECYCLE_PROOF_CRASH_DIR": str(base)},
    )
    deadline = time.time() + 900
    while not marker.exists() and child.poll() is None and time.time() < deadline:
        time.sleep(0.5)
    if not marker.exists():
        child.kill()
        raise SystemExit("the crash item never reached its record check")
    time.sleep(1.0)
    os.kill(child.pid, signal.SIGKILL)
    child.wait()
    charts_at_kill = _oracle(ctx, CRASH)
    from openadapt_flow.runtime.durable import CheckpointStore

    store = CheckpointStore(run_dir, key=ctx["keys"]["checkpoint_key"])
    pending = store.read_pending()
    ledger.add(
        "crash.killed",
        item=CRASH,
        charts=charts_at_kill,
        pause_category=pending.category if pending else None,
        deferred_check_held=bool(pending and pending.deferred_readback),
        delivery_uncertainty=bool(pending and pending.delivery_uncertainty),
    )
    report = _resume(ctx, CRASH, run_dir)
    summary = _summary(report)
    summary["charts"] = _oracle(ctx, CRASH)
    summary["charts_at_kill"] = charts_at_kill
    ledger.add("crash.resumed", item=CRASH, **summary)
    return summary


def _resume(
    ctx: dict[str, Any],
    key: str,
    run_dir: Path,
    params: Optional[dict[str, str]] = None,
) -> Any:
    from openadapt_flow.runtime import Replayer
    from openadapt_flow.runtime.durable import CheckpointStore, resume
    from openadapt_flow.runtime.durable.approval import issue_resume_approval
    from openadapt_flow.runtime.durable.program_checkpoint import bundle_version

    store = CheckpointStore(run_dir, key=ctx["keys"]["checkpoint_key"])
    manifest, pending = store.read_manifest(), store.read_pending()
    assert manifest is not None and pending is not None
    # Resuming a stopped item is a problem a person decides.
    approval = issue_resume_approval(
        pending,
        approver="lifecycle-proof-operator",
        resolution="the runner stopped while the record check ran",
        bundle_version=bundle_version(Path(manifest.bundle_dir)),
        run_id=manifest.run_id,
        workflow_name=manifest.workflow_name,
        run_dir=run_dir,
    )
    ctx["person"].touches += 1
    ctx["ledger"].add("person", action="resume", item=key)
    item = Item(ctx, key, run_dir, params=params)
    backend, close = ctx["hooks"].open_session()
    try:
        replayer = Replayer(
            backend,
            effect_verifier=item.verifier(),
            **item.replayer_kwargs(_load(Path(manifest.bundle_dir))),
        )
        return resume(
            run_dir, replayer, approval=approval, key=ctx["keys"]["checkpoint_key"]
        )
    finally:
        close()


def _load(bundle: Path) -> Any:
    from openadapt_flow.ir import Workflow

    return Workflow.load(bundle)


def _crash_child(ctx: dict[str, Any]) -> None:
    from openadapt_flow.runtime.readback_pipeline import ReadbackPipeline

    base = Path(os.environ["LIFECYCLE_PROOF_CRASH_DIR"])
    marker = base / "readback-started"
    pipeline = ReadbackPipeline()
    item = Item(
        ctx,
        CRASH,
        base / CRASH,
        pipeline=pipeline,
        verifier_wrapper=lambda v: _slow_check(v, marker),
    )
    item.run(ctx["bundle"])
    time.sleep(600)  # the parent kills this process inside the check


class ProofTarget:
    """Self-repair's target: the item's first run under the configuration."""

    def __init__(self, ctx: dict[str, Any], item: Item) -> None:
        from openadapt_flow.repair.self_repair import ReplayTarget

        self.ctx, self.item = ctx, item
        self._base = ReplayTarget(
            self._open,
            effect_verifier=item.verifier(),
            policy=ctx["profile"].policy,
            profile="regulated",
            params=item.params,
            execution_origin=ctx["config"].app.origin,
            replayer_kwargs=item.replayer_kwargs(_load(ctx["bundle"])),
        )
        self.effect_verifier = self._base.effect_verifier

    def _open(self, fault: Optional[str]) -> Any:
        from openadapt_flow.repair.self_repair import AppSession

        backend, close = self.ctx["hooks"].open_session()
        return AppSession(backend=backend, close=close)

    def open(self, fault: Optional[str] = None) -> Any:
        return self._open(fault)

    def execute(
        self,
        workflow,
        bundle_dir,
        session,
        run_dir,
        *,
        approval_gate=None,
        gui_only=False,
    ):  # type: ignore[no-untyped-def]
        if approval_gate is None and not gui_only:
            # The item's own first run: the problems-only gate and the pin.
            from openadapt_flow.runtime import Replayer

            item = Item(self.ctx, self.item.key, Path(run_dir), params=self.item.params)
            verifier = item.verifier()
            replayer = Replayer(
                session.backend,
                effect_verifier=verifier,
                governed_authorization=item.authorization(
                    workflow, bundle_dir, verifier
                ),
                approval_gate=item.approval_gate(bundle_dir, session.backend.page),
                **item.replayer_kwargs(workflow),
            )
            return replayer.run(
                workflow.model_copy(deep=True),
                params=dict(item.params),
                bundle_dir=bundle_dir,
                run_dir=run_dir,
                execution_target_kind="web",
                execution_origin=self.ctx["config"].app.origin,
                execution_entry_url=session.backend.page.url,
            )
        return self._base.execute(
            workflow,
            bundle_dir,
            session,
            run_dir,
            approval_gate=approval_gate,
            gui_only=gui_only,
        )


def _requalification(ctx: dict[str, Any], trials: int) -> Any:
    """Re-qualify on the test copy: the same fixture, its data put back after."""

    from openadapt_flow.qualify.campaign import qualify
    from openadapt_flow.repair.readmission import Requalification

    app = ctx["app"]

    def run(*args: Any, **kwargs: Any) -> Any:
        dump = app.dump()
        ctx["ledger"].add("drift.requalify.start", candidate=str(args[0]))
        try:
            return qualify(*args, **kwargs)
        finally:
            app.restore(dump)
            ctx["ledger"].add("drift.requalify.end")

    return Requalification(
        config=ctx["config"], hooks=ctx["hooks"], trials=trials, run=run
    )


def phase_drift(ctx: dict[str, Any], trials: int, agent: Any) -> dict[str, Any]:
    from openadapt_flow.repair.self_repair import ScreenAccess, SelfRepair

    out, ledger = ctx["out"], ctx["ledger"]
    change = ctx["app"].relabel_save()
    ledger.add("drift.app_change", change=change)
    run_dir = out / "drift" / DRIFT
    item = Item(ctx, DRIFT, run_dir)
    repair = SelfRepair(
        agent,
        ProofTarget(ctx, item),
        store=ctx["store"],
        screen_access=ScreenAccess(enabled_by="lifecycle-proof (synthetic data only)"),
        approval_wait_s=600.0,
        requalification=_requalification(ctx, trials),
        admission_key_dir=ctx["keys"]["admission_key_dir"],
        sleep=ctx["person"].sleep,
    )
    from openadapt_flow.approval_mode.item_approval import ItemApprovalStore

    ctx["person"].watch(ItemApprovalStore(run_dir))
    t0 = time.time()
    result = repair.run(ctx["bundle"], run_dir)
    evidence = result.evidence
    summary = {
        "status": result.status,
        "reason": evidence.reason,
        "halted_step": evidence.halted_step_id,
        "readmitted_digest": evidence.readmitted_bundle_digest,
        "lineage_hop": evidence.lineage_hop,
        "approved_by": evidence.approved_by,
        "stages": [(s.stage, s.passed, s.detail[:200]) for s in evidence.steps],
        "resumed": _summary(result.report) if result.report is not None else None,
        "seconds": round(time.time() - t0, 1),
    }
    summary["charts"] = _oracle(ctx, DRIFT)
    ledger.add("drift.result", item=DRIFT, **summary)
    if evidence.readmitted_bundle_digest:
        bundle = ctx["store"] / "bundles" / evidence.readmitted_bundle_digest
        requalified = sorted((run_dir / "self-repair").glob("requalify-*"))
        _write(
            out / "current-bundle.json",
            {
                "bundle": str(bundle),
                "digest": evidence.readmitted_bundle_digest,
                "qualification_dir": str(requalified[-1]) if requalified else "",
            },
        )
        ctx["bundle"] = bundle
    return summary


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "phase",
        choices=(
            "up",
            "admit",
            "switch",
            "batch",
            "crash",
            "drift",
            "continue",
            "all",
            "_crash_child",
        ),
    )
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--keys", type=Path, required=True)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--trials", type=int, default=1)
    parser.add_argument("--repair-agent", default=None)
    args = parser.parse_args(argv)

    out = args.out.resolve()
    ledger = Ledger(out)
    if args.phase == "up":
        from benchmark.lifecycle_proof.fixture import ProofApp

        app = ProofApp()
        how = app.bring_up(say=lambda m: ledger.add("up", line=m))
        ledger.add("up.done", how=how)
        return 0
    ctx = _context(out, args.keys, ledger)
    if args.phase == "_crash_child":
        _crash_child(ctx)
        return 0
    agent: Any = LabelAgent()
    if args.repair_agent:
        from openadapt_flow.cli_self_repair import load_agent

        agent = load_agent(args.repair_agent)
    phases = (
        [args.phase]
        if args.phase != "all"
        else ["admit", "switch", "batch", "crash", "drift", "switch", "continue"]
    )
    for phase in phases:
        ledger.add("phase.start", phase=phase)
        if phase == "admit":
            phase_admit(ctx, args.source, args.trials)
        elif phase == "switch":
            phase_switch(ctx)
        elif phase == "batch":
            phase_batch(ctx, BATCH, "batch")
        elif phase == "crash":
            phase_crash(ctx, args.keys)
        elif phase == "drift":
            phase_drift(ctx, args.trials, agent)
        elif phase == "continue":
            phase_batch(ctx, CONTINUE, "continue")
        ledger.add("phase.end", phase=phase, person_touches=ctx["person"].touches)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
