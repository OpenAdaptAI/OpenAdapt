"""Check it, pre-filled: what the recording already shows, and three questions.

A person used to answer every part of Check it by hand: which step saves,
which steps only move around, how to read the saved record back, which
values change per item. Most of that is in the recording. This module reads
it and proposes each answer; the person (or their agent, over the local
socket) answers three questions:

1. **Is this the right record check?** The read the app made after the save
   (Flow's record-check synthesis, ``openadapt_flow.record_check_synthesis``)
   becomes the same effect binding a person would type in the plain
   question form, with the read itself as the record-check settings file.
   Its fault test runs on exactly that binding, so the evidence shown is
   for what will be saved, not for a stronger check it doesn't bind.
2. **Which values change per item?** Every value typed during the
   recording is an input. A value the person marks as fixed is filled in
   with its recorded value on every item.
3. **Approve?** Unchanged: certify and seal, in the window, by a person.

Nothing here certifies, approves or loosens a check. A "yes" calls the same
qualification commands the forms call; a proposal that isn't certain says
so and leaves the form open.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from openadapt_desktop.private_fs import ensure_private_dir, make_private_file

SETUP_SCHEMA = "openadapt.desktop.check-setup/v1"

#: The verifier kinds a learned check can bind today. A REST read is what
#: Flow's synthesis builds from a recorded network capture.
_BINDABLE_READS = frozenset({"rest"})

#: Who answered a question. The window passes "person"; the local agent
#: socket passes "agent". Approval is never answered by an agent.
ANSWERERS = frozenset({"person", "agent"})


class CheckSetupError(ValueError):
    """A setup answer the engine refuses."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# What the recording shows
# ---------------------------------------------------------------------------


def _read_label(config: Mapping[str, Any]) -> str:
    path = str(config.get("records_path") or "")
    return f"GET {path}" if path else "a read of the app's records"


def _plain_fault(result: Any) -> str:
    detail = str(getattr(result, "detail", "") or "")
    # Flow names the surface first ("api_db: nothing saved"); a person
    # doesn't need the surface name.
    return detail.split(": ", 1)[1] if ": " in detail else detail


def _is_idempotency_field(name: str) -> bool:
    lowered = name.lower().replace("-", "_")
    return lowered in {"key", "idempotency_key", "idempotencykey"} or "idempot" in lowered


def learn_record_check(
    recording_dir: Path,
    *,
    inputs: Mapping[str, Any],
    secret_inputs: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """Propose the record check from the reads the recording holds.

    Returns ``{"found": False, "reason": ...}`` when there is nothing to
    propose. Otherwise ``found`` is True and ``binding`` holds the exact
    arguments for ``bind_qualification_effect``, ``effects`` the record-check
    settings (a deployment ``effects:`` section), and ``fault_test`` the
    result of judging that binding against the recorded records with each
    bad save injected.
    """

    try:
        from openadapt_flow.record_check_synthesis import (
            SynthesisError,
            prove_against_faults,
            recorded_params,
            synthesize,
        )
        from openadapt_flow.runtime.effects import Effect, EffectKind, ValueExpr
        from openadapt_flow.runtime.effects.surfaces import surface_probe
    except ImportError:
        return {"found": False, "reason": "This Flow can't read a recording's network capture."}

    recording_dir = Path(recording_dir)
    if not (recording_dir / "network.jsonl").is_file():
        return {
            "found": False,
            "reason": (
                "The recording holds no network capture, so OpenAdapt didn't see a read "
                "after the save."
            ),
        }
    params = {
        name: value
        for name, value in recorded_params(recording_dir).items()
        if name in inputs and name not in secret_inputs
    }
    try:
        proposal = synthesize(recording_dir, params=params)
    except SynthesisError as exc:
        return {"found": False, "reason": str(exc)}
    targets = [surface for surface in proposal.search.surfaces if surface.is_target]
    if len(targets) != 1:
        return {
            "found": False,
            "reason": (
                "OpenAdapt didn't find the saved record in any read after the save."
                if not targets
                else "The saved record showed up in more than one read, so OpenAdapt won't "
                "pick one."
            ),
        }
    surface = targets[0]
    config = {key: value for key, value in surface.config.items() if not key.startswith("_")}
    kind = str(config.get("kind"))
    if kind not in _BINDABLE_READS:
        return {
            "found": False,
            "reason": f"OpenAdapt found the read, but Desktop can't run a {kind} read yet.",
        }
    bound = {field: param for field, param in sorted(surface.bound.items()) if param in params}
    if not bound:
        return {
            "found": False,
            "reason": "None of the values you typed shows up in the saved record.",
        }
    # Select the record by an id-like value when there is one, else by the
    # first value the record holds. Every other recorded value is matched too.
    match_field, match_param = next(
        ((field, param) for field, param in bound.items() if " " not in str(params.get(param, ""))),
        next(iter(bound.items())),
    )
    extra_match: dict[str, dict[str, str]] = {
        field: {"param": param} for field, param in bound.items() if field != match_field
    }
    constants = dict(sorted(surface.constants.items()))
    # A fixed key the app sends with the save to stop a double save. Flow's
    # clinical-write policy wants it on the record check; it is the app's
    # own value, so it is checked as sent, never invented.
    key_field = next((field for field in constants if _is_idempotency_field(field)), None)
    idempotency_literal = constants.pop(key_field) if key_field else None
    extra_match.update({field: {"literal": str(value)} for field, value in constants.items()})
    binding: dict[str, Any] = {
        "kind": "record_written",
        "match_field": match_field,
        "match_param": match_param,
        "extra_match": extra_match,
        "expected_count": 1,
        "count_new_only": True,
        "forbid_collateral_loss": True,
        "verification_tier": 1,
    }
    if key_field:
        binding["idempotency_literal"] = idempotency_literal
        binding["key_field"] = key_field

    # Judge exactly this binding, not the fuller check synthesis proposes.
    match = {match_field: ValueExpr(param=match_param)}
    for field, expr in extra_match.items():
        match[field] = (
            ValueExpr(param=expr["param"])
            if "param" in expr
            else ValueExpr(literal=expr["literal"])
        )
    effect = Effect(
        kind=EffectKind.RECORD_WRITTEN,
        match=match,
        expected_count=1,
        count_new_only=True,
        forbid_collateral_loss=True,
        idempotency_key=ValueExpr(literal=idempotency_literal) if key_field else None,
        key_field=key_field or "key",
        probe=surface_probe(surface.name),
    )
    faults = prove_against_faults(
        [surface], [effect], params, closed_world=proposal.search.closed_world
    )
    clean = [result for result in faults if result.correct]
    bad = [result for result in faults if not result.correct]
    caught = [result for result in bad if result.ok]
    missed = [result for result in bad if not result.ok]
    clean_pass = bool(clean) and all(result.ok for result in clean)
    if not clean_pass:
        return {
            "found": False,
            "reason": (
                "The read OpenAdapt found would stop the recorded save itself, so it isn't "
                "proposed."
            ),
        }
    return {
        "found": True,
        "source": "recorded_reads",
        "read": _read_label(config),
        "checked": sorted({match_field, *extra_match}),
        "values_checked": sorted(set(bound.values())),
        "fixed_fields": constants,
        "idempotency_field": key_field,
        "binding": binding,
        "effects": config,
        "fault_test": {
            "bad_saves": len(bad),
            "caught": len(caught),
            "caught_kinds": [_plain_fault(result) for result in caught],
            "missed_kinds": [_plain_fault(result) for result in missed],
        },
    }


def quiet_steps(recording_dir: Path) -> dict[str, Any] | None:
    """Whether every write the recording sent belongs to the save.

    When the network capture shows the app sent a write request only during
    the save, every other step only opened screens or filled in the form.
    Returns ``None`` when that can't be shown.
    """

    try:
        from openadapt_flow.record_check_synthesis import (
            SynthesisError,
            load_network,
            save_window,
        )
    except ImportError:
        return None
    try:
        network = load_network(Path(recording_dir))
    except SynthesisError:
        return None
    if not network:
        return None
    writes = [
        line
        for line in network
        if str(line.get("method") or "").upper() in {"POST", "PUT", "PATCH", "DELETE"}
    ]
    _first, _last, save_writes = save_window(Path(recording_dir), network)
    if not save_writes or len(save_writes) != len(writes):
        return None
    return {
        "write_requests": len(writes),
        "evidence": (
            "In your recording the app sent data to its server only when you saved."
            if len(writes) == 1
            else f"In your recording the app sent its {len(writes)} save requests only when "
            "you saved."
        ),
    }


def _recording_params(recording_dir: Path) -> dict[str, str]:
    try:
        meta = json.loads((Path(recording_dir) / "meta.json").read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    params = meta.get("params") if isinstance(meta, dict) else None
    if not isinstance(params, dict):
        return {}
    return {str(name): str(value) for name, value in params.items() if value is not None}


def value_proposals(inputs: list[dict[str, Any]], recordings: list[Path]) -> list[dict[str, Any]]:
    """Which inputs change per item, from one recording or several.

    With one recording every typed value is proposed as changing per item:
    the person typed it, and nothing shows it stays the same. With several
    recordings of the same workflow, a value that was the same in every one
    is proposed as fixed, and one that differed as changing.
    """

    seen: dict[str, set[str]] = {}
    counted = 0
    for recording in recordings:
        values = _recording_params(recording)
        if not values:
            continue
        counted += 1
        for name, value in values.items():
            seen.setdefault(name, set()).add(value)
    proposals: list[dict[str, Any]] = []
    for item in inputs:
        name = str(item.get("name") or "")
        if not name or item.get("secret"):
            continue
        distinct = seen.get(name, set())
        if counted > 1 and len(distinct) == 1:
            changes = False
            why = f"The same in all {counted} recordings."
        elif counted > 1 and len(distinct) > 1:
            changes = True
            why = f"Different in your {counted} recordings."
        else:
            changes = True
            why = "You typed it while recording."
        proposals.append(
            {
                "name": name,
                "example": item.get("example"),
                "changes": changes,
                "why": why,
            }
        )
    return proposals


# ---------------------------------------------------------------------------
# What was answered
# ---------------------------------------------------------------------------


class SetupStore:
    """Answers per workflow, owner-only, next to the engine's other state."""

    def __init__(self, data_dir: Path) -> None:
        self.root = Path(data_dir) / "check_setup"

    def _path(self, workflow_id: str) -> Path:
        safe = "".join(ch for ch in workflow_id if ch.isalnum() or ch in "-_")
        if not safe or safe != workflow_id:
            raise CheckSetupError("Unknown workflow")
        return self.root / f"{safe}.json"

    def load(self, workflow_id: str) -> dict[str, Any]:
        path = self._path(workflow_id)
        try:
            data = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError):
            return {"schema": SETUP_SCHEMA, "answers": {}, "fixed_values": {}}
        if not isinstance(data, dict) or data.get("schema") != SETUP_SCHEMA:
            return {"schema": SETUP_SCHEMA, "answers": {}, "fixed_values": {}}
        data.setdefault("answers", {})
        data.setdefault("fixed_values", {})
        return data

    def save(self, workflow_id: str, data: dict[str, Any]) -> None:
        ensure_private_dir(self.root)
        path = self._path(workflow_id)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        make_private_file(tmp)
        os.replace(tmp, path)

    def record_check_config(self, workflow_id: str) -> Path | None:
        """The record-check settings a "yes" saved, if the file is intact."""

        data = self.load(workflow_id)
        raw = data.get("record_check_config")
        if not raw:
            return None
        path = Path(str(raw))
        if not path.is_file() or path.parent != self.root:
            return None
        return path

    def write_record_check_config(self, workflow_id: str, effects: Mapping[str, Any]) -> Path:
        """Write the read as a deployment settings file Flow loads for runs."""

        ensure_private_dir(self.root)
        path = self.root / f"{self._path(workflow_id).stem}.record-check.yaml"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            "# Written by OpenAdapt Desktop when a person (or their agent) said\n"
            "# yes to the record check Check it learned from the recording.\n"
            + yaml.safe_dump({"effects": dict(effects)}, sort_keys=False),
            encoding="utf-8",
        )
        make_private_file(tmp)
        os.replace(tmp, path)
        return path


def answer_entry(answered_by: str, **extra: Any) -> dict[str, Any]:
    if answered_by not in ANSWERERS:
        raise CheckSetupError("answered_by must be person or agent")
    return {"by": answered_by, "at": _now(), **extra}


# ---------------------------------------------------------------------------
# The engine commands (thin wrappers in dispatch.py call these)
# ---------------------------------------------------------------------------

_WRITE_CLASSES = frozenset({"state_changing", "consequential", "irreversible"})
POLICY = "clinical-write"


def _plan(qualification: Mapping[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Saving steps and unproven steps, the way Check it's plan sorts them."""

    controls = (qualification.get("controls") or {}).get("actions") or {}
    writes: list[dict[str, Any]] = []
    unproven: list[dict[str, Any]] = []
    for node in (qualification.get("graph") or {}).get("nodes") or []:
        if node.get("kind") != "action":
            continue
        control = controls.get(node["id"]) or {}
        classification = (control.get("classification") or {}).get("classification")
        confirmed = bool((control.get("classification") or {}).get("operator_confirmed"))
        is_write = (
            node.get("risk") == "irreversible"
            or bool(node.get("effects"))
            or bool(control.get("effects"))
            or classification in _WRITE_CLASSES
        )
        entry = {"id": node["id"], "node": node, "control": control}
        if is_write:
            writes.append(entry)
        elif not (classification == "read_only" and confirmed):
            unproven.append(entry)
    return {"writes": writes, "unproven": unproven}


def _recording_dir(dispatcher: Any, workflow_id: str) -> Path | None:
    row = dispatcher.services.db.get_bundle(workflow_id) or {}
    capture_id = row.get("capture_id")
    capture = dispatcher.services.db.get_capture(str(capture_id)) if capture_id else None
    raw = capture and (capture.get("capture_path") or capture.get("capture_dir"))
    return Path(str(raw)) if raw else None


def _sibling_recordings(dispatcher: Any, workflow_id: str) -> list[Path]:
    """Recordings of the same workflow: same description as this one's."""

    own = _recording_dir(dispatcher, workflow_id)
    row = dispatcher.services.db.get_bundle(workflow_id) or {}
    capture = (
        dispatcher.services.db.get_capture(str(row["capture_id"]))
        if row.get("capture_id")
        else None
    )
    description = str((capture or {}).get("task_description") or "").strip().lower()
    found = [own] if own else []
    if not description:
        return found
    for other in dispatcher.services.db.list_captures(limit=200):
        if str(other.get("task_description") or "").strip().lower() != description:
            continue
        raw = other.get("capture_path") or other.get("capture_dir")
        if raw and Path(str(raw)) not in found and Path(str(raw)).is_dir():
            found.append(Path(str(raw)))
    return found


def get_check_setup(dispatcher: Any, workflow_id: str) -> dict[str, Any]:
    """Everything Check it can fill in, and what is still a question."""

    store = SetupStore(dispatcher.config.data_dir)
    saved = store.load(workflow_id)
    inputs = dispatcher._task_inputs(workflow_id)
    secrets = frozenset(str(item["name"]) for item in inputs if item.get("secret"))
    recording = _recording_dir(dispatcher, workflow_id)
    qualification = dispatcher.get_qualification(workflow_id=workflow_id, policy=POLICY)
    plan = _plan(qualification) if qualification.get("ok") else {"writes": [], "unproven": []}

    if recording is None:
        learned: dict[str, Any] = {
            "found": False,
            "reason": "OpenAdapt can't find this workflow's recording.",
        }
    else:
        learned = learn_record_check(
            recording,
            inputs={str(item["name"]): item for item in inputs},
            secret_inputs=secrets,
        )
    if learned.get("found") and len(plan["writes"]) != 1:
        learned = {
            "found": False,
            "reason": (
                "This workflow saves in more than one step, so OpenAdapt won't guess which "
                "step the read belongs to."
            ),
        }
    quiet = quiet_steps(recording) if recording is not None else None
    recordings = _sibling_recordings(dispatcher, workflow_id)
    values = value_proposals(inputs, recordings)
    answered_values = saved["answers"].get("values")
    if answered_values:
        changing = set(answered_values.get("changes") or [])
        for item in values:
            item["changes"] = item["name"] in changing
    return {
        "ok": True,
        "schema": SETUP_SCHEMA,
        "workflow_id": workflow_id,
        "record_check": learned,
        "saving_steps": [entry["id"] for entry in plan["writes"]],
        "quiet_steps": (
            {**quiet, "step_ids": [entry["id"] for entry in plan["unproven"]]}
            if quiet and plan["unproven"]
            else None
        ),
        "unproven_steps": [entry["id"] for entry in plan["unproven"]],
        "values": values,
        "recordings": len(recordings),
        "answers": saved["answers"],
        "fixed_values": saved["fixed_values"],
        # Said here so an agent reading this knows its limit.
        "approval": "A person approves in OpenAdapt Desktop. An agent can't.",
    }


def _ok(result: Mapping[str, Any], what: str) -> Mapping[str, Any]:
    if not result.get("ok"):
        raise CheckSetupError(f"{what}: {result.get('error') or 'refused'}")
    return result


def _start_project(dispatcher: Any, workflow_id: str) -> None:
    """Start the check with the app and environment from the recording."""

    draft = dispatcher.propose_qualification_from_demo(
        workflow_id=workflow_id, policy_pack="community", accept=False
    )
    pins = {pin.get("kind"): pin for pin in ((draft.get("proposal") or {}).get("pins") or [])}
    application = pins.get("application") or {}
    environment = pins.get("environment") or {}
    if application.get("status") == "missing" or environment.get("status") == "missing":
        raise CheckSetupError(
            "OpenAdapt couldn't tell which app this is from the recording. "
            "Say which app in Check it."
        )
    app = application.get("payload") or {}
    env = environment.get("payload") or {}
    target_kind = str(app.get("target_kind") or "web")
    version = str(app.get("application_version") or "recorded")
    digest = str(env.get("environment_digest") or "")
    observer: dict[str, str] | None = None
    target = dispatcher._task_target(workflow_id) or {}
    recorded_origin = str(app.get("application_identity") or "")
    if target_kind == "web" and target.get("browser_cdp_endpoint") and recorded_origin:
        # The person's signed-in browser: the app is known by its address
        # and signed-in session, at the version recorded. A sign-out or a
        # move to another address stops the run before the next input.
        from openadapt_flow.qualification_web_session import (
            SIGNED_IN_WEB_SESSION_OBSERVER_ID,
            signed_in_web_session_contract_sha256,
            signed_in_web_session_environment_digest,
        )

        digest = signed_in_web_session_environment_digest(recorded_origin, version)
        observer = {
            "environment_observer_id": SIGNED_IN_WEB_SESSION_OBSERVER_ID,
            "environment_observer_contract_sha256": signed_in_web_session_contract_sha256(version),
            "application_identity": recorded_origin,
        }
    elif target_kind == "web" and target.get("url"):
        # A browser OpenAdapt opens, on an app without OpenAdapt's markers:
        # bind Flow's generic web observer (origin, browser, viewport,
        # platform, runtime, declared version), read once from the recorded
        # address. Its session part is the page load, so an app that reloads
        # the whole page mid-run stops at the next input; that fails closed.
        from openadapt_flow.qualify.environment import observe_web_environment

        observer = observe_web_environment(
            str(target["url"]),
            application_version=version,
            headless=not bool(target.get("headed")),
        )
        digest = observer["environment_digest"]
    _ok(
        dispatcher.initialize_qualification(
            workflow_id=workflow_id,
            policy=POLICY,
            target_kind=target_kind,
            application=str(app.get("application") or ""),
            application_version=version,
            environment_digest=digest,
            required_capabilities=list(env.get("required_capabilities") or []),
            **({"environment_observer": observer} if observer else {}),
        ),
        "Starting the check",
    )


def accept_record_check(dispatcher: Any, workflow_id: str, *, answered_by: str) -> dict[str, Any]:
    """Yes to "Is this the right record check?".

    Each part is the command the matching form sends: start the check with
    the recorded app, mark the steps the capture shows sent nothing as only
    reading, use the record details seen before the save, confirm what the
    save step changes, and bind the learned record check.
    """

    setup = get_check_setup(dispatcher, workflow_id)
    learned = setup["record_check"]
    if not learned.get("found"):
        raise CheckSetupError(
            "OpenAdapt has no record check to propose for this workflow. "
            "Answer the questions in Check it."
        )
    current = dispatcher.get_qualification(workflow_id=workflow_id, policy=POLICY)
    if not current.get("project") or current.get("draft_environment"):
        _start_project(dispatcher, workflow_id)
        current = dispatcher.get_qualification(workflow_id=workflow_id, policy=POLICY)
    plan = _plan(current)
    quiet = setup.get("quiet_steps")
    if quiet:
        for entry in plan["unproven"]:
            _ok(
                dispatcher.set_qualification_risk(
                    workflow_id=workflow_id,
                    policy=POLICY,
                    step_id=entry["id"],
                    risk="read_only",
                    explanation=(
                        f"{quiet['evidence']} Proposed from the recording's network capture; "
                        f"confirmed in Check it by the {answered_by}."
                    ),
                ),
                "Marking a step as only reading",
            )
    for entry in plan["writes"]:
        control = entry["control"]
        identity = control.get("identity") or {}
        # The project needs its own identity policy for the save, even when
        # the compiled step already carries the recorded identity gate.
        if not identity.get("policy"):
            if identity.get("can_arm"):
                _ok(
                    dispatcher.arm_qualification_identity(
                        workflow_id=workflow_id, policy=POLICY, step_id=entry["id"]
                    ),
                    "Using the record details from the recording",
                )
        current_class = (control.get("classification") or {}).get("classification")
        risk = (
            "irreversible"
            if entry["node"].get("risk") == "irreversible"
            else current_class
            if current_class in _WRITE_CLASSES
            else "consequential"
        )
        _ok(
            dispatcher.set_qualification_risk(
                workflow_id=workflow_id,
                policy=POLICY,
                step_id=entry["id"],
                risk=risk,
                explanation=f"Confirmed in Check it by the {answered_by}.",
            ),
            "Confirming what the save changes",
        )
        has_checks = bool(control.get("effects")) and all(
            isinstance(effect.get("verification_tier"), int) for effect in control["effects"]
        )
        if not has_checks:
            _ok(
                dispatcher.bind_qualification_effect(
                    workflow_id=workflow_id,
                    policy=POLICY,
                    step_id=entry["id"],
                    **learned["binding"],
                ),
                "Saving the record check",
            )
    store = SetupStore(dispatcher.config.data_dir)
    config = store.write_record_check_config(workflow_id, learned["effects"])
    saved = store.load(workflow_id)
    saved["record_check_config"] = str(config)
    saved["answers"]["record_check"] = answer_entry(
        answered_by,
        answer="yes",
        read=learned.get("read"),
        quiet_steps=len(plan["unproven"]) if quiet else 0,
    )
    store.save(workflow_id, saved)
    return get_check_setup(dispatcher, workflow_id)


def answer_values(
    dispatcher: Any, workflow_id: str, changes: list[str], *, answered_by: str
) -> dict[str, Any]:
    """Answer "Which values change per item?".

    A value that doesn't change is filled in with its recorded value on
    every item. Nothing about the workflow itself changes.
    """

    inputs = [item for item in dispatcher._task_inputs(workflow_id) if not item.get("secret")]
    names = {str(item["name"]) for item in inputs}
    unknown = sorted(set(changes) - names)
    if unknown:
        raise CheckSetupError("Unknown value(s): " + ", ".join(unknown))
    fixed: dict[str, str] = {}
    for item in inputs:
        name = str(item["name"])
        if name in changes:
            continue
        if item.get("example") is None or str(item["example"]) == "":
            raise CheckSetupError(
                f"OpenAdapt has no recorded value for {name}, so it must change per item."
            )
        fixed[name] = str(item["example"])
    store = SetupStore(dispatcher.config.data_dir)
    saved = store.load(workflow_id)
    saved["fixed_values"] = fixed
    saved["answers"]["values"] = answer_entry(answered_by, changes=sorted(changes))
    store.save(workflow_id, saved)
    return get_check_setup(dispatcher, workflow_id)


def carry_to_sealed(data_dir: Path, workflow_id: str, sealed_id: str) -> None:
    """Approving seals a new version; its answers come with it."""

    store = SetupStore(data_dir)
    saved = store.load(workflow_id)
    if not saved["answers"] and not saved["fixed_values"]:
        return
    config = store.record_check_config(workflow_id)
    if config is not None:
        effects = (yaml.safe_load(config.read_text("utf-8")) or {}).get("effects") or {}
        saved["record_check_config"] = str(store.write_record_check_config(sealed_id, effects))
    saved["sealed_from"] = workflow_id
    store.save(sealed_id, saved)


def run_checks(dispatcher: Any, workflow_id: str) -> dict[str, Any]:
    """The practice item on the recorded values, then the safety drills.

    The practice item saves for real: Flow stops before its save until a
    person approves it in the window (or it times out and saves nothing). A
    drill never saves. Nothing here approves the workflow.
    """

    setup = get_check_setup(dispatcher, workflow_id)
    current = dispatcher.get_qualification(workflow_id=workflow_id, policy=POLICY)
    if "record_check" not in setup["answers"] and not current.get("project"):
        raise CheckSetupError('Answer "Is this the right record check?" first.')
    if not ((current.get("graph") or {}).get("bundle") or {}).get("encrypted"):
        # Flow binds each case result to the exact executable workflow, so
        # the cases run on the sealed version that will be approved and run.
        sealed = dispatcher.seal_qualification_workflow(workflow_id=workflow_id)
        if not sealed.get("ok") or not sealed.get("workflow_id"):
            raise CheckSetupError(f"Locking this version: {sealed.get('error') or 'refused'}")
        dispatcher.emit(
            "check_setup",
            {
                "workflow_id": workflow_id,
                "sealed_id": sealed["workflow_id"],
                "answered_by": "agent",
            },
        )
        workflow_id = str(sealed["workflow_id"])
    values = {
        str(item["name"]): str(item["example"])
        for item in dispatcher._task_inputs(workflow_id)
        if not item.get("secret") and item.get("example") is not None
    }
    parameters = json.dumps(values, sort_keys=True)
    target = dispatcher._task_target(workflow_id)
    common: dict[str, Any] = {
        "workflow_id": workflow_id,
        "policy": POLICY,
        "parameters_json": parameters,
    }
    if target:
        common["target"] = target

    def cases() -> list[dict[str, Any]]:
        current = dispatcher.get_qualification(workflow_id=workflow_id, policy=POLICY)
        return list((current.get("project") or {}).get("cases") or [])

    def passed(case: Mapping[str, Any], revision: Any) -> bool:
        return any(
            result.get("status") == "passed" and result.get("project_revision") == revision
            for result in case.get("results") or []
        )

    practice_id = "practice-1"
    existing = [case for case in cases() if case.get("kind") in {"representative", "nominal"}]
    if existing:
        practice_id = str(existing[0]["id"])
    else:
        _ok(
            dispatcher.add_qualification_case(
                **common,
                case_id=practice_id,
                kind="representative",
                description="Practice run on a test item",
            ),
            "Setting up the practice item",
        )
    revision = (
        dispatcher.get_qualification(workflow_id=workflow_id, policy=POLICY).get("project") or {}
    ).get("revision")
    practice = next(case for case in cases() if case["id"] == practice_id)
    summary: dict[str, Any] = {"practice": None, "drills": []}
    if not passed(practice, revision):
        result = dispatcher.run_qualification_case(**common, case_id=practice_id)
        if result.get("not_approved"):
            summary["practice"] = "not approved: nothing was saved"
            return {
                "ok": False,
                "workflow_id": workflow_id,
                **summary,
                "error": result.get("error"),
            }
        if not result.get("ok"):
            summary["practice"] = "did not pass"
            return {
                "ok": False,
                "workflow_id": workflow_id,
                **summary,
                "error": result.get("error"),
            }
    summary["practice"] = "passed"
    for case in cases():
        if case.get("kind") in {"representative", "nominal"}:
            continue
        revision = (
            dispatcher.get_qualification(workflow_id=workflow_id, policy=POLICY).get("project")
            or {}
        ).get("revision")
        if passed(case, revision):
            summary["drills"].append({"kind": case["kind"], "result": "passed"})
            continue
        step = (case.get("fault_target") or {}).get("step_id")
        result = dispatcher.run_qualification_case(
            **common,
            case_id=case["id"],
            **({"fault_target": {"step_id": step, "actuation_path": "gui"}} if step else {}),
        )
        summary["drills"].append(
            {"kind": case["kind"], "result": "passed" if result.get("ok") else "did not pass"}
        )
        if not result.get("ok"):
            return {
                "ok": False,
                "workflow_id": workflow_id,
                **summary,
                "error": result.get("error"),
            }
    return {
        "ok": True,
        "workflow_id": workflow_id,
        **summary,
        "next": "A person approves the workflow in OpenAdapt Desktop.",
    }
