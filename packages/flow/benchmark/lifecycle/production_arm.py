"""The production configuration as a soak arm: proof-15's configuration, at scale.

The configuration is exactly the one `benchmark/lifecycle_proof` proves on the
local OpenEMR referral (its README has the table):

- the Regulated profile, governed authorization from the run gate on the
  certified, encrypted bundle, pinned to the admitted digest (a repaired
  version passes only through signed lineage);
- durable runs with an encrypted checkpoint store;
- the API tier where admissible (the referral's API step is refused by its own
  proposal on OpenEMR, so the GUI save runs);
- pipelined read-back, one window (the harness waits for each item's deferred
  check before it judges the item, so the arm's wall time here does not show
  the pipelining gain; proof-15 measured that);
- repair on: a halt before any effect goes to self-repair (the proof's
  rule-based agent, no model);
- ``problems_only`` approval, with the local stand-in for Cloud's switch
  status.

Every person touch goes through the harness operator, which decides from the
ledger's truth and is charged from the cost model:

- an approval the ``problems_only`` gate asks for (``approval``);
- a stopped item (``stop``, or ``reconcile`` when a save may have landed);
- a resume of a durable pause (``stop``, then the resume);
- a relabel repair to review (``repair_review``), with ``recovery="relabel"``.

Options (``--arm-options`` JSON):

- ``keys``: the key directory outside the repository (bundle, checkpoint,
  status and admission keys), default ``~/oa/scratch/compose-proof/keys``;
- ``source``: the certified referral bundle to admit;
- ``trials``: qualification trials per case (default 1);
- ``recovery``: ``"none"`` (what main does today) or ``"relabel"`` (a halt
  that :mod:`openadapt_flow.repair.relabel` proves is a relabeled control is
  re-qualified, reviewed once, re-admitted with lineage, and the item re-run).
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Optional

from .arm import Env, ItemResult, OperatorRequest, WorkItem

DEFAULT_KEYS = Path.home() / "oa" / "scratch" / "compose-proof" / "keys"
APPROVER_ID = "lifecycle-soak-operator"


class _Ledger:
    """The arm's own detail log (the harness ledger holds the results)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def add(self, kind: str, **fields: Any) -> None:
        row = {"at": round(time.time(), 3), "kind": kind, **fields}
        with self._lock, self.path.open("a") as handle:
            handle.write(json.dumps(row, sort_keys=True, default=str) + "\n")


class OperatorPerson:
    """Answers the approval requests the run writes, through the harness operator.

    Stands where the proof's ``ScriptedPerson`` stands (``sleep`` is the
    gate's and self-repair's wait), but every answer is the operator's
    decision, charged from the cost model.
    """

    def __init__(self, ledger: _Ledger) -> None:
        self.ledger = ledger
        self.stores: list[Any] = []
        self.seen: set[str] = set()
        self.touches = 0
        self.item: Optional[WorkItem] = None
        self.operator: Any = None
        self.kind = "approval"

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
                values = {
                    f.label: f.after or "" for f in (request.fields or ()) if f.label
                }
                decision = self.operator.ask(
                    OperatorRequest(
                        kind=self.kind,  # type: ignore[arg-type]
                        item_id=self.item.item_id if self.item else None,
                        reason=f"{request.mode}: {request.step_intent}"[:300],
                        surface="approval_mode:item-approval (Desktop Run it)",
                        detail={
                            "values": values,
                            "step_id": request.step_id,
                            "probes_passed": self.kind == "repair_review",
                        },
                    )
                )
                approve = decision.action in ("approve", "accept_repair")
                store.write_answer(
                    make_answer(
                        request,
                        decision="approve" if approve else "decline",
                        decline_reason=None if approve else "wrong_record",
                        decided_by="human",
                        route="desktop_run_it",
                        approver_id=APPROVER_ID,
                        auth_method="cloud_aal2",
                    )
                )
                self.touches += 1
                self.ledger.add(
                    "person",
                    action=decision.action,
                    item=self.item.item_id if self.item else None,
                    request_id=request.request_id,
                )


class ProductionArm:
    name = "openadapt-production"

    def __init__(self, options: dict[str, Any]) -> None:
        self.options = options
        self.keys_dir = Path(options.get("keys") or DEFAULT_KEYS).expanduser()
        self.source = options.get("source")
        self.trials = int(options.get("trials", 1))
        #: A prior run's workdir whose admission (the qualified, pinned bundle)
        #: this run reuses, so two runs compare on one admitted version.
        self.admission_from = options.get("admission_from")
        #: Admit a version whose duplicate-check step reads OpenEMR's match
        #: list (no_duplicate_candidate) instead of requiring "No matches
        #: were found.". A program change, so it gets its own admission.
        self.duplicate_check = bool(options.get("duplicate_check", False))
        #: Admit a version whose date of birth is a typed ``date`` parameter,
        #: so a date written another way is refused before any step.
        self.typed_dob = bool(options.get("typed_dob", False))
        #: Convert each item's date parameters to ISO as intake does before
        #: the run (the referral writes dates however its sender writes them).
        self.intake_dates = bool(options.get("intake_dates", False))
        #: Offer the operator a signed re-arm of a switched-off
        #: problems_only switch once the log allows it (10 verified items).
        self.rearm = bool(options.get("rearm", False))
        self.recovery = str(options.get("recovery", "none"))
        if self.recovery not in ("none", "relabel"):
            raise ValueError(f"unknown recovery {self.recovery!r}")
        if self.recovery == "relabel":
            self.name = "openadapt-production-relabel"
        self.ctx: dict[str, Any] = {}
        self.pipeline: Any = None
        self.recoveries: list[dict[str, Any]] = []
        self._runs: dict[str, int] = {}

    # -- describe / setup ---------------------------------------------------------

    def describe(self) -> dict[str, Any]:
        from benchmark.lifecycle_proof import run as proof

        return {
            "configuration": "benchmark/lifecycle_proof (proof-15)",
            "profile": "regulated",
            "authorization": "governed run gate on the certified, encrypted bundle",
            "pin": "admitted digest; repaired versions only through signed lineage",
            "durable": True,
            "checkpoint_store": "encrypted",
            "api_tier": "where admissible (refused on the OpenEMR referral by its "
            "own proposal; the GUI save runs)",
            "readback": "pipelined, window 1; the harness waits for each check",
            "repair": "self-repair on a halt before any effect (rule-based agent, "
            "no model)",
            "approval": "problems_only (local stand-in for Cloud's switch status)",
            "policy": proof.POLICY,
            "recovery": self.recovery,
            "typed_dob": self.typed_dob,
            "intake_dates": self.intake_dates,
            "rearm": "operator signs when offered" if self.rearm else "never",
            "duplicate_check": (
                "no_duplicate_candidate on step_039 (rows tr.oneresult; a row "
                "with the item's last name or date of birth stops the item)"
                if self.duplicate_check
                else "recorded: text_present 'No matches were found.'"
            ),
            "qualification_trials": self.trials,
            "model_calls": "none on any path",
        }

    def setup(self, env: Env, operator: Any) -> list[dict[str, Any]]:
        from benchmark.lifecycle_proof import run as proof

        out = env.workdir
        ledger = _Ledger(out / "arm-ledger.jsonl")
        ctx = proof._context(out, self.keys_dir, ledger)  # type: ignore[arg-type]
        person = OperatorPerson(ledger)
        person.operator = operator
        ctx["person"] = person
        self.ctx = ctx
        source = Path(self.source) if self.source else proof.DEFAULT_SOURCE
        app = ctx["app"]
        # Admission resets the app for its six cases; the harness already
        # seeded the run's existing charts, so put them back afterwards.
        reused = None
        t0 = time.time()
        if self.admission_from:
            reused = self._reuse_admission(Path(self.admission_from), out)
            admit_s = float(reused.get("seconds") or 0.0)
        else:
            if self.duplicate_check:
                source = with_duplicate_list_check(source, out / "program")
            if self.typed_dob:
                source = with_typed_dob(source, out / "program-typed-dob")
            dump = app.dump()
            try:
                proof.phase_admit(ctx, source, self.trials)
            finally:
                app.restore(dump)
            admit_s = time.time() - t0
        t1 = time.time()
        proof.phase_switch(ctx)
        from openadapt_flow.runtime.readback_pipeline import ReadbackPipeline

        self.pipeline = ReadbackPipeline()
        if self.rearm:
            self._approver = self._approver_key()
            ctx["approver_keys"] = self._approver[1]
        return [
            {
                "phase": "admit",
                "wall_s": round(admit_s, 1),
                # Encrypt, qualify (6 cases) and the run-gate check are
                # unattended; the person signs the admission (cost model).
                "attended_s": 0.0,
                "model_calls": 0,
                "cost_usd": 0.0,
                "note": "teach and compile happened before this run (the "
                "certified referral bundle); onboarding effort is measured "
                "in benchmark/onboarding_effort",
                "pinned_digest": ctx["pinned_digest"],
                "reused_from": reused,
            },
            {
                "phase": "switch",
                "wall_s": round(time.time() - t1, 1),
                "attended_s": 0.0,
                "model_calls": 0,
                "cost_usd": 0.0,
                "note": "problems_only switch opened locally, as Cloud would",
            },
        ]

    def _reuse_admission(self, source: Path, out: Path) -> dict[str, Any]:
        import shutil

        doc = json.loads((source / "admission" / "admission.json").read_text())
        target = out / "admission"
        target.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / "admission" / "admission.json", target / "admission.json")
        self.ctx.update(
            pinned_digest=doc["pinned_digest"],
            bundle=Path(doc["bundle"]),
            environment_digest=doc["environment_digest"],
        )
        seconds = None
        ledger = source / "arm-ledger.jsonl"
        if ledger.exists():
            for line in ledger.read_text().splitlines():
                row = json.loads(line)
                if row.get("kind") == "admit":
                    seconds = row.get("seconds")
        return {
            "workdir": str(source),
            "digest": doc["pinned_digest"],
            "seconds": seconds,
        }

    # -- one item -----------------------------------------------------------------

    def _run_dir(self, item: WorkItem, tag: str = "") -> Path:
        n = self._runs.get(item.item_id, 0) + 1
        self._runs[item.item_id] = n
        name = item.item_id + (f"-{tag}" if tag else "") + (f"-{n}" if n > 1 else "")
        return self.ctx["out"] / "items" / name

    def _item(self, item: WorkItem, run_dir: Path, *, retry: bool = False) -> Any:
        from benchmark.lifecycle_proof import run as proof

        return proof.Item(
            self.ctx,
            item.item_id,
            run_dir,
            pipeline=self.pipeline,
            params=item.params,
            is_retry=retry,
        )

    def _execute(self, item: WorkItem, run_dir: Path, *, retry: bool = False) -> Any:
        """Run one item under the configuration; settle its record check."""

        handle = self._item(item, run_dir, retry=retry)
        try:
            report = handle.run(self.ctx["bundle"])
            handoff = self.pipeline.take()
            if handoff is not None:
                handoff.settled.result(timeout=600)
        finally:
            for close in self.ctx.pop("closers", []):
                try:
                    close()
                except Exception:  # noqa: BLE001 - a closed browser is closed
                    pass
        return report

    def run_item(self, item: WorkItem, env: Env, operator: Any) -> ItemResult:
        from benchmark.lifecycle_proof import run as proof
        from openadapt_flow.repair.auto_repair import route_stop

        person: OperatorPerson = self.ctx["person"]
        person.item, person.operator, person.kind = item, operator, "approval"
        t0 = time.time()
        run_dir = self._run_dir(item)
        detail: dict[str, Any] = {"run_dir": str(run_dir.relative_to(env.workdir))}
        if self.intake_dates:
            item = self._intake_dates(item, detail)
        phases: list[dict[str, Any]] = []
        try:
            report = self._execute(item, run_dir)
        except Exception as exc:  # noqa: BLE001 - a refused start is a stop
            detail["error"] = f"{type(exc).__name__}: {exc}"[:400]
            operator.ask(
                OperatorRequest(
                    kind="stop",
                    item_id=item.item_id,
                    reason=detail["error"][:300],
                    surface="openadapt-flow run (refused before the first step)",
                    detail={"resumable": False},
                )
            )
            return ItemResult(
                item_id=item.item_id,
                claimed="error",
                product_status=detail["error"][:200],
                wall_s=time.time() - t0,
                detail=detail,
            )
        summary = proof._summary(report)
        detail["first_run"] = summary
        if summary["transaction_outcome"] == "VERIFIED" and report.success:
            return self._result(item, "done", summary, t0, detail, phases)

        route, why = route_stop(report)
        detail["route"] = {"route": route, "why": why}
        if self.recovery == "relabel":
            recovered = self._try_relabel(item, report, run_dir, operator, detail)
            if recovered is not None:
                phases.extend(recovered.pop("phases", []))
                summary = recovered["summary"]
                claimed = (
                    "done"
                    if summary["transaction_outcome"] == "VERIFIED"
                    and recovered["success"]
                    else "stopped"
                )
                if claimed != "done":
                    self._ask_stop(item, summary, operator, detail, resumable=False)
                return self._result(item, claimed, summary, t0, detail, phases)
        if route == "repair":
            repaired = self._self_repair(item, operator, detail)
            if repaired is not None:
                summary = repaired
                if summary["transaction_outcome"] == "VERIFIED" and summary["success"]:
                    return self._result(item, "done", summary, t0, detail, phases)

        # A stop: a person decides. A durable pause that only holds a record
        # check (a crash) can resume; a halted run re-runs only as a new item.
        resumable = self._resumable(run_dir)
        decision = self._ask_stop(
            item,
            summary,
            operator,
            detail,
            resumable=resumable,
            reconcile=route == "reconciliation",
        )
        if decision.action == "resume" and resumable:
            resumed = proof._resume(self.ctx, item.item_id, run_dir, item.params)
            summary = proof._summary(resumed)
            detail["resumed"] = summary
            if summary["transaction_outcome"] == "VERIFIED" and resumed.success:
                return self._result(item, "done", summary, t0, detail, phases)
        return self._result(item, "stopped", summary, t0, detail, phases)

    def _intake_dates(self, item: WorkItem, detail: dict[str, Any]) -> WorkItem:
        """Each date parameter as intake converts it, with the conversion kept.

        A date whose order can't be told (or that doesn't exist) is left as
        written: the run's own ISO check then refuses it before any step.
        """

        import dataclasses

        from openadapt_flow.intake.extract import normalize_date
        from openadapt_flow.ir import ParamKind, Workflow

        specs = Workflow.load(Path(self.ctx["bundle"])).param_specs
        params = dict(item.params)
        notes = {}
        for name, spec in specs.items():
            if spec.type is not ParamKind.DATE or not params.get(name):
                continue
            iso, error, conversion = normalize_date(params[name])
            if error:
                notes[name] = {"raw": params[name], "error": error}
            elif conversion:
                notes[name] = {
                    "raw": params[name],
                    "value": iso,
                    "conversion": conversion,
                }
                params[name] = str(iso)
        if notes:
            detail["intake_dates"] = notes
        return dataclasses.replace(item, params=params)

    def _approver_key(self) -> tuple[Any, dict[str, str]]:
        """A throwaway approver key for the scripted operator (outside the repo)."""

        import base64

        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
        )
        from cryptography.hazmat.primitives.serialization import (
            Encoding,
            NoEncryption,
            PrivateFormat,
            PublicFormat,
            load_pem_private_key,
        )

        pem = self.keys_dir / "soak-approver.pem"
        if not pem.exists():
            pem.write_bytes(
                Ed25519PrivateKey.generate().private_bytes(
                    Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()
                )
            )
            pem.chmod(0o600)
        private = load_pem_private_key(pem.read_bytes(), password=None)
        public = private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)  # type: ignore[union-attr]
        return private, {"soak-approver": base64.b64encode(public).decode("ascii")}

    def _offer_rearm(self, operator: Any, item: WorkItem) -> Optional[dict[str, Any]]:
        """After an item: if the switch may be re-armed, the operator signs."""

        from openadapt_flow.approval_mode.switch import (
            SwitchLogError,
            rearm,
            rearm_request,
            sign_rearm,
        )

        log = self.ctx.get("switch_log")
        if log is None:
            return None
        keys = self.ctx["approver_keys"]
        try:
            if not log.state(approver_keys=keys).is_off:
                return None
            request = rearm_request(
                log, cause="recorded in the arm ledger", approver_keys=keys
            )
        except SwitchLogError:
            return None
        decision = operator.ask(
            OperatorRequest(
                kind="approval",
                item_id=item.item_id,
                reason=f"re-arm problems_only after {len(request['clean_run_refs'])} "
                f"verified items (off: {request['off_reason']})",
                surface="Desktop: re-arm the switch (one signed tap)",
                detail={"rearm": True, "off_reason": request["off_reason"]},
            )
        )
        if decision.action != "approve":
            return {"rearmed": False}
        evidence = sign_rearm(
            log,
            request,
            approver_id="soak-approver",
            key_id="soak-approver",
            private_key=self._approver[0],
        )
        event = rearm(log, evidence, approver_keys=keys)
        return {"rearmed": True, "seq": event.seq, "off_reason": request["off_reason"]}

    def _resumable(self, run_dir: Path) -> bool:
        from openadapt_flow.runtime.durable import CheckpointStore

        try:
            pending = CheckpointStore(
                run_dir, key=self.ctx["keys"]["checkpoint_key"]
            ).read_pending()
        except Exception:  # noqa: BLE001 - no readable pause, nothing to resume
            return False
        return bool(pending and pending.deferred_readback)

    def _ask_stop(
        self,
        item: WorkItem,
        summary: dict[str, Any],
        operator: Any,
        detail: dict[str, Any],
        *,
        resumable: bool,
        reconcile: bool = False,
    ) -> Any:
        decision = operator.ask(
            OperatorRequest(
                kind="reconcile" if reconcile else "stop",
                item_id=item.item_id,
                reason=(
                    f"{summary.get('transaction_outcome')}: {summary.get('halt') or ''}"
                )[:300],
                surface="openadapt-flow explain / approve / resume",
                detail={
                    "resumable": resumable,
                    "transaction_outcome": summary.get("transaction_outcome"),
                    "approval": summary.get("approval"),
                },
            )
        )
        detail.setdefault("stops", []).append(
            {"decision": decision.action, "rule": decision.rule}
        )
        return decision

    # -- recovery -----------------------------------------------------------------

    def _self_repair(
        self, item: WorkItem, operator: Any, detail: dict[str, Any]
    ) -> Optional[dict[str, Any]]:
        """Repair on: a halt before any effect goes to self-repair, as the
        configuration's ``repair.agent`` does. It re-runs the item itself."""

        from benchmark.lifecycle_proof import run as proof
        from openadapt_flow.approval_mode.item_approval import ItemApprovalStore
        from openadapt_flow.repair.self_repair import ScreenAccess, SelfRepair

        person: OperatorPerson = self.ctx["person"]
        run_dir = self._run_dir(item, "repair")
        handle = proof.Item(self.ctx, item.item_id, run_dir, params=item.params)
        person.kind = "repair_review"
        person.watch(ItemApprovalStore(run_dir))
        try:
            repair = SelfRepair(
                proof.LabelAgent(),
                proof.ProofTarget(self.ctx, handle),
                store=self.ctx["store"],
                screen_access=ScreenAccess(
                    enabled_by="lifecycle soak (synthetic data)"
                ),
                approval_wait_s=600.0,
                requalification=proof._requalification(self.ctx, self.trials),
                admission_key_dir=self.ctx["keys"]["admission_key_dir"],
                sleep=person.sleep,
            )
            result = repair.run(self.ctx["bundle"], run_dir)
        except Exception as exc:  # noqa: BLE001 - a failed repair is a stop
            detail["self_repair"] = {"error": f"{type(exc).__name__}: {exc}"[:300]}
            return None
        finally:
            person.kind = "approval"
        evidence = result.evidence
        detail["self_repair"] = {
            "status": result.status,
            "reason": evidence.reason[:300],
            "readmitted": evidence.readmitted_bundle_digest,
        }
        if evidence.readmitted_bundle_digest:
            self._move_to(
                self.ctx["store"] / "bundles" / evidence.readmitted_bundle_digest,
                evidence.readmitted_bundle_digest,
                sorted((run_dir / "self-repair").glob("requalify-*")),
            )
        if result.report is None or result.status == "not_repairable":
            return None
        summary = proof._summary(result.report)
        summary["success"] = bool(result.report.success)
        return summary

    def _adopt_running_release(self) -> dict[str, Any]:
        """Pin the test copy's environment lock to the release now running.

        The fixture refuses to reset (and so to qualify) when OpenEMR's
        source differs from its lock. After a vendor release the lock must
        name the new release; this records its source proofs and which files
        changed. The bundle's admission and the pin are not touched.
        """

        from benchmark.openemr_local import qualify_hooks

        fx = qualify_hooks.fixture()
        app = self.ctx["app"]
        locked: dict[str, str] = dict(fx.lock["source_proofs"])
        out = app._exec(app.app_container, "sha256sum " + " ".join(sorted(locked)))
        observed: dict[str, str] = {}
        for line in out.splitlines():
            parts = line.split(maxsplit=1)
            if len(parts) == 2:
                observed[parts[1].lstrip("* ")] = parts[0]
        if set(observed) != set(locked):
            raise RuntimeError("could not read every locked OpenEMR source file")
        changed = sorted(k for k in locked if observed[k] != locked[k])
        fx.lock = {**fx.lock, "source_proofs": observed}
        app.fx.lock = {**app.fx.lock, "source_proofs": observed}
        path = (
            app.state_dir / f"environment.release-{len(self.recoveries) + 1}.lock.json"
        )
        path.write_text(json.dumps(fx.lock, indent=2) + "\n")
        os.environ["OPENEMR_FIXTURE_LOCK"] = str(path)
        return {"changed": changed, "lock": path.name}

    def _move_to(self, bundle: Path, digest: str, requalified: list[Path]) -> None:
        """Run the re-admitted version from here on, under a new switch record."""

        from benchmark.lifecycle_proof import run as proof

        proof._write(
            self.ctx["out"] / "current-bundle.json",
            {
                "bundle": str(bundle),
                "digest": digest,
                "qualification_dir": str(requalified[-1]) if requalified else "",
            },
        )
        self.ctx["bundle"] = bundle
        # A switch record names one bundle digest; Cloud issues one for the
        # re-admitted version from its own re-qualification's passing cases.
        proof.phase_switch(self.ctx)

    def _try_relabel(
        self,
        item: WorkItem,
        report: Any,
        run_dir: Path,
        operator: Any,
        detail: dict[str, Any],
    ) -> Optional[dict[str, Any]]:
        from benchmark.lifecycle_proof import run as proof
        from openadapt_flow.ir import Workflow
        from openadapt_flow.repair import relabel
        from openadapt_flow.repair.lifecycle import RepairStore

        bundle = Path(self.ctx["bundle"])
        workflow = Workflow.load(bundle)
        proposal = relabel.propose(report, workflow)
        if isinstance(proposal, str):
            detail["relabel"] = {"proposed": False, "why": proposal}
            return None
        t0 = time.time()
        work = run_dir.parent / f"{run_dir.name}-relabel"
        candidate = relabel.build(
            bundle, proposal, work / "candidate", key=self.ctx["keys"]["bundle_key"]
        )
        review_charge: dict[str, Any] = {}
        # The test copy now runs the vendor's new release; re-qualification
        # pins that release (the person sees the changed files on the review).
        release = self._adopt_running_release()

        def ask_person(review: Any) -> tuple[str, str]:
            rows = review.requalification["rows"]
            decision = operator.ask(
                OperatorRequest(
                    kind="repair_review",
                    item_id=item.item_id,
                    reason=review.proposal.summary()[:300],
                    surface="openadapt-flow repair relabel (review card)",
                    detail={
                        "fresh_admission": review.fresh_admission,
                        "release_changed_files": release["changed"],
                        "probes_passed": bool(rows),
                        "requalified_cases": len(rows),
                        "certified_digest": review.certified_digest,
                    },
                )
            )
            review_charge.update(action=decision.action, charged_s=decision.charged_s)
            approve = decision.action == "accept_repair"
            return ("approve" if approve else "decline", APPROVER_ID)

        result = relabel.readmit(
            parent_bundle=bundle,
            candidate_bundle=candidate,
            proposal=proposal,
            store=RepairStore(self.ctx["store"]),
            requalification=proof._requalification(self.ctx, self.trials),
            ask_person=ask_person,
            work_dir=work,
            evidence_run_dir=run_dir,
            key_dir=self.ctx["keys"]["admission_key_dir"],
        )
        record = {
            "item": item.item_id,
            "proposal": proposal.summary(),
            "status": result.status,
            "reason": result.reason[:300],
            "digest": result.digest,
            "lineage_hop": (result.lineage or {}).get("hop"),
            "timings": result.timings,
            "review": review_charge,
            "release": release,
            "seconds_to_readmitted": round(time.time() - t0, 1),
        }
        detail["relabel"] = record
        self.recoveries.append(record)
        if result.status not in ("readmitted", "fresh_admission"):
            return None
        if result.bundle_dir is None:
            return None
        phases_extra: list[dict[str, Any]] = []
        if result.status == "fresh_admission":
            # The person's approval admits the certified copy afresh: the
            # deployment re-pins to its digest (no lineage), and the person
            # signs that admission (the cost model's admit charge).
            self.ctx["pinned_digest"] = str(result.digest)
            phases_extra.append(
                {
                    "phase": "admit",
                    "wall_s": 0.0,
                    "attended_s": 0.0,
                    "model_calls": 0,
                    "cost_usd": 0.0,
                    "note": "fresh admission of the re-qualified copy: "
                    + result.reason[:200],
                }
            )
        self._move_to(result.bundle_dir, str(result.digest), [work / "requalify"])
        # The stopped item never wrote; it runs again on the re-admitted
        # version, as a retry (problems_only sends a retry to a person).
        retry_dir = self._run_dir(item, "after-relabel")
        rerun = self._execute(item, retry_dir, retry=True)
        summary = proof._summary(rerun)
        record["rerun"] = summary
        record["seconds_to_item_done"] = round(time.time() - t0, 1)
        return {
            "summary": summary,
            "success": bool(rerun.success),
            "phases": phases_extra
            + [
                {
                    "phase": "relabel_requalify",
                    "wall_s": result.timings.get("requalify_s", 0.0),
                    "attended_s": 0.0,
                    "model_calls": 0,
                    "cost_usd": 0.0,
                }
            ],
        }

    # -- result -------------------------------------------------------------------

    def _result(
        self,
        item: WorkItem,
        claimed: str,
        summary: dict[str, Any],
        t0: float,
        detail: dict[str, Any],
        phases: list[dict[str, Any]],
    ) -> ItemResult:
        status = " / ".join(
            str(x)
            for x in (
                summary.get("execution_outcome"),
                summary.get("transaction_outcome"),
            )
            if x
        )
        detail["final"] = summary
        if self.rearm:
            offered = self._offer_rearm(self.ctx["person"].operator, item)
            if offered is not None:
                detail["rearm"] = offered
        return ItemResult(
            item_id=item.item_id,
            claimed=claimed,  # type: ignore[arg-type]
            product_status=status or "no report",
            wall_s=time.time() - t0,
            model_calls=int(summary.get("model_calls") or 0),
            evidence=detail.get("run_dir", ""),
            phases=phases,
            detail=detail,
        )

    def recover_after_timeout(self) -> None:
        """After the runner timed an item out: drop its browser and pipeline."""

        from openadapt_flow.runtime.readback_pipeline import ReadbackPipeline

        for close in self.ctx.pop("closers", []):
            try:
                close()
            except Exception:  # noqa: BLE001
                pass
        if self.pipeline is not None:
            try:
                self.pipeline.close()
            except Exception:  # noqa: BLE001
                pass
        self.pipeline = ReadbackPipeline()

    def after_drift(
        self, event: dict[str, Any], env: Env, operator: Any
    ) -> list[dict[str, Any]]:
        return []  # the product is not told about app changes; it meets them

    def close(self) -> None:
        if self.pipeline is not None:
            self.pipeline.close()
        for close in self.ctx.pop("closers", []):
            try:
                close()
            except Exception:  # noqa: BLE001
                pass


DUPLICATE_STEP = "step_039"
NO_MATCHES = "No matches were found."


def with_duplicate_list_check(source: Path, out: Path) -> Path:
    """The referral program with its duplicate check reading the match list.

    The recorded program stops unless OpenEMR shows "No matches were found.",
    so any listed chart stops the item, even one that shares only a first
    name. This version replaces that one postcondition with
    ``no_duplicate_candidate``: the item continues only when the list has
    loaded and no listed row carries the item's last name or date of birth.
    Every other step is unchanged. The result is uncertified; admission
    qualifies it afresh.
    """

    import shutil

    from openadapt_flow.ir import Postcondition, PostconditionKind, Workflow
    from openadapt_flow.repair.self_repair import _uncertify_patch

    workflow = Workflow.load(source)
    steps = list(workflow.steps)
    index = next(i for i, step in enumerate(steps) if step.id == DUPLICATE_STEP)
    step = steps[index]
    expect = [
        pc
        if not (pc.kind is PostconditionKind.TEXT_PRESENT and pc.text == NO_MATCHES)
        else Postcondition(
            kind=PostconditionKind.NO_DUPLICATE_CANDIDATE,
            text=NO_MATCHES,
            timeout_s=pc.timeout_s,
            candidate_rows="tr.oneresult",
            candidate_frame_path=("#modalframe",),
            candidate_params=("lname", "DOB"),
            # Qualified with the record check (OpenEMR's patient API): a run
            # without that channel fails the step instead of reading the list.
            candidate_source="record",
            # OpenEMR's REST search can't page (its next link names the
            # configured site address); its FHIR Patient search returns every
            # match in one Bundle (#1516's live check).
            candidate_channel="fhir",
        )
        for pc in step.expect
    ]
    if expect == list(step.expect):
        raise RuntimeError(f"{DUPLICATE_STEP} has no '{NO_MATCHES}' check to replace")
    steps[index] = step.model_copy(update={"expect": expect})
    workflow.steps = steps
    _uncertify_patch(workflow)
    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(source, out, ignore=shutil.ignore_patterns("manifest.json"))
    workflow.save(out)
    return out


def with_typed_dob(source: Path, out: Path) -> Path:
    """The program with its date of birth declared as a ``date`` parameter.

    The recorded program types the date of birth as a plain string, so a
    referral that writes it as MM/DD/YYYY was typed into the form and saved
    as whatever OpenEMR made of it. Declared as a date, a value that is not a
    canonical ISO date refuses the run before any step. Nothing else changes;
    the result is uncertified, so admission qualifies it afresh.
    """

    import shutil

    from openadapt_flow.ir import ParamKind, ParamSpec, Workflow
    from openadapt_flow.repair.self_repair import _uncertify_patch

    workflow = Workflow.load(source)
    specs = dict(workflow.param_specs)
    current = specs.get("DOB") or ParamSpec(
        name="DOB", example=workflow.params.get("DOB")
    )
    specs["DOB"] = current.model_copy(update={"type": ParamKind.DATE})
    workflow.param_specs = specs
    _uncertify_patch(workflow)
    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(source, out, ignore=shutil.ignore_patterns("manifest.json"))
    workflow.save(out)
    return out


def make(options: dict[str, Any]) -> ProductionArm:
    os.environ.setdefault("OPENADAPT_NO_LAUNCHER_CHECK", "1")
    return ProductionArm(options)
