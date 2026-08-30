"""Execute the qualify-proposal effect oracle. Do not only list the fault.

Before ``qualify accept`` can succeed, the proposed oracle is run against a
``--break-it`` world: the success banner says saved, the store did not change.
MockMed is the default fixture because it has a real persistence boundary.

A model must not invent an endpoint. Actor bytes and oracle bytes must be
disjoint. Re-reading the acting screen or the same-session banner HALTs.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Literal, Optional
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from openadapt_flow.ir import PostconditionKind, Step, Workflow
from openadapt_flow.runtime.effects._common import judge_records
from openadapt_flow.runtime.effects.effect import (
    Effect,
    EffectState,
    EffectVerdict,
    Verdict,
)
from openadapt_flow.runtime.effects.onscreen import OnScreenReadbackVerifier
from openadapt_flow.runtime.effects.rest import RestRecordVerifier
from openadapt_flow.traversal import iter_workflow_steps

ORACLE_GATE_SCHEMA: Literal["openadapt.qualification-oracle-gate/v1"] = (
    "openadapt.qualification-oracle-gate/v1"
)
DEFAULT_BREAK_IT_FIXTURE = "mockmed"
MISSING_SECOND_READ = "do not automate until a second read exists"
BROKEN_CASE_ACCEPTED = (
    "proposed oracle would have accepted the broken case: "
    "banner/success UI says saved, store unchanged"
)

CHANNEL_ACTING_SCREEN = "acting_screen"
CHANNEL_ACTING_SESSION = "acting_session"
CHANNEL_ACTING_SESSION_BANNER = "acting_session_banner"
CHANNEL_REST = "rest"
CHANNEL_SQL = "sql"
CHANNEL_FILE = "file"
CHANNEL_SECOND_SESSION = "second_session"
CHANNEL_SYSTEM_OF_RECORD = "system_of_record"

ACTOR_CHANNELS = frozenset({CHANNEL_ACTING_SCREEN, CHANNEL_ACTING_SESSION})
DISJOINT_ORACLE_CHANNELS = frozenset(
    {
        CHANNEL_REST,
        CHANNEL_SQL,
        CHANNEL_FILE,
        CHANNEL_SECOND_SESSION,
        CHANNEL_SYSTEM_OF_RECORD,
    }
)
_SCREEN_CHANNELS = frozenset(
    {
        CHANNEL_ACTING_SCREEN,
        CHANNEL_ACTING_SESSION,
        CHANNEL_ACTING_SESSION_BANNER,
    }
)
_BANNER_TOKENS = ("saved", "success", "banner")
_MOCKMED_BANNER_PREFIX = "Encounter saved — "


class BoundEffect(BaseModel):
    """One proposed effect plus the channel it reads."""

    model_config = ConfigDict(extra="forbid")

    step_id: str
    effect_index: int
    actuation_path: str
    oracle_channels: list[str]
    kind: str
    has_readback: bool


class OracleGateResult(BaseModel):
    """Audit of channel disjointness plus the executed --break-it fault."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["openadapt.qualification-oracle-gate/v1"] = (
        ORACLE_GATE_SCHEMA
    )
    fixture: str = DEFAULT_BREAK_IT_FIXTURE
    actor_channels: list[str] = Field(default_factory=list)
    oracle_channels: list[str] = Field(default_factory=list)
    shared_channels: list[str] = Field(default_factory=list)
    effects: list[BoundEffect] = Field(default_factory=list)
    break_it_executed: bool = False
    break_it_verdicts: list[dict[str, Any]] = Field(default_factory=list)
    screen_claimed_success: bool = False
    store_unchanged: bool = False
    system_of_record_records: int = 0
    passed: bool = False
    halt_reason: Optional[str] = None


def _write_steps(workflow: Workflow) -> list[Step]:
    return [
        step
        for step in iter_workflow_steps(workflow)
        if step.risk == "irreversible" or list(step.effects)
    ]


def _effects_on_step(step: Step) -> list[tuple[str, int, Effect]]:
    found: list[tuple[str, int, Effect]] = []
    for index, effect in enumerate(step.effects):
        if not effect.needs_operator_confirmation:
            found.append(("gui", index, effect))
    if step.api_binding is not None:
        for index, effect in enumerate(step.api_binding.effects):
            if not effect.needs_operator_confirmation:
                found.append(("api", index, effect))
    return found


def bound_effects(workflow: Workflow) -> list[BoundEffect]:
    """Name each proposed effect's oracle channel. Do not invent an endpoint."""

    bound: list[BoundEffect] = []
    for step in iter_workflow_steps(workflow):
        for path, index, effect in _effects_on_step(step):
            bound.append(
                BoundEffect(
                    step_id=step.id,
                    effect_index=index,
                    actuation_path=path,
                    oracle_channels=sorted(_oracle_channels(effect, path)),
                    kind=effect.kind.value,
                    has_readback=effect.readback is not None,
                )
            )
    return bound


def actor_channel_ids(workflow: Workflow) -> frozenset[str]:
    """Bytes the actor used: the acting screen and its session."""

    if not _write_steps(workflow):
        return frozenset()
    return frozenset(ACTOR_CHANNELS)


def _oracle_channels(effect: Effect, actuation_path: str) -> frozenset[str]:
    if effect.readback is not None:
        channels = {CHANNEL_ACTING_SCREEN, CHANNEL_ACTING_SESSION}
        value = "" if effect.value is None else str(effect.value).lower()
        probe = (effect.probe or "").lower()
        if any(token in value or token in probe for token in _BANNER_TOKENS):
            channels.add(CHANNEL_ACTING_SESSION_BANNER)
        return frozenset(channels)
    if actuation_path == "api":
        return frozenset({CHANNEL_REST})
    return frozenset({CHANNEL_SYSTEM_OF_RECORD})


def oracle_channel_ids(workflow: Workflow) -> frozenset[str]:
    channels: set[str] = set()
    for item in bound_effects(workflow):
        channels.update(item.oracle_channels)
    return frozenset(channels)


def shared_channel_ids(workflow: Workflow) -> frozenset[str]:
    return actor_channel_ids(workflow) & oracle_channel_ids(workflow)


def _shared_channel_reason(shared: frozenset[str]) -> str:
    named: list[str] = []
    if CHANNEL_ACTING_SCREEN in shared:
        named.append(f"{CHANNEL_ACTING_SCREEN} (re-read the same acting screen)")
    if CHANNEL_ACTING_SESSION_BANNER in shared or CHANNEL_ACTING_SESSION in shared:
        named.append(f"{CHANNEL_ACTING_SESSION} (same session banner)")
    if not named:
        named = sorted(shared)
    return "proposed oracle shares the acting channel: " + "; ".join(named)


def _has_second_read(workflow: Workflow) -> bool:
    return bool(oracle_channel_ids(workflow) & DISJOINT_ORACLE_CHANNELS)


def _only_screen_postcondition(workflow: Workflow) -> bool:
    writes = _write_steps(workflow)
    if not writes:
        return False
    if bound_effects(workflow):
        return False
    for step in writes:
        texts = [
            (pc.text or "").lower()
            for pc in step.expect
            if pc.kind is PostconditionKind.TEXT_PRESENT and pc.text
        ]
        if any("saved" in text or "success" in text for text in texts):
            return True
    return False


def _step_effect(workflow: Workflow, item: BoundEffect) -> Effect:
    for step in iter_workflow_steps(workflow):
        if step.id != item.step_id:
            continue
        if item.actuation_path == "api":
            if step.api_binding is None:
                break
            return step.api_binding.effects[item.effect_index]
        return step.effects[item.effect_index]
    raise LookupError(f"proposal effect {item.step_id!r}[{item.effect_index}] is gone")


def _banner_text(workflow: Workflow) -> str:
    note = str(workflow.params.get("note") or "example")
    return _MOCKMED_BANNER_PREFIX + note[:40]


def _png() -> bytes:
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (40, 20), "white").save(buffer, format="PNG")
    return buffer.getvalue()


class _BreakItBannerBackend:
    def screenshot(self) -> bytes:
        return _png()


class _BannerLine:
    def __init__(self, text: str) -> None:
        self.text = text
        self.confidence = 0.99
        self.region = (0, 0, 40, 20)


class _BreakItBannerVision:
    def __init__(self, text: str) -> None:
        self._text = text

    def ocr(self, png: bytes, *, region: Any = None) -> list[_BannerLine]:
        del png, region
        return [_BannerLine(self._text)]


def _http_json(
    url: str, *, method: str = "GET", body: Optional[dict[str, Any]] = None
) -> tuple[int, dict[str, Any]]:
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=2) as response:
            payload = json.loads(response.read().decode("utf-8"))
            status = int(response.status)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8") if exc.fp is not None else ""
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            payload = {}
        return int(exc.code), payload if isinstance(payload, dict) else {}
    return status, payload if isinstance(payload, dict) else {}


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def _resolved_effect(workflow: Workflow, effect: Effect) -> Effect:
    try:
        resolved = effect.resolve(dict(workflow.params))
    except (TypeError, ValueError, ValidationError):
        resolved = effect
    return resolved.model_copy(update={"timeout_s": 0.0})


def _execute_screen_oracle(
    workflow: Workflow, effect: Effect, banner_text: str
) -> EffectVerdict:
    verifier = OnScreenReadbackVerifier(
        _BreakItBannerBackend(),
        vision=_BreakItBannerVision(banner_text),
    )
    return verifier.verify(
        _resolved_effect(workflow, effect), verifier.capture_pre_state()
    )


def _execute_sor_oracle(
    workflow: Workflow,
    effect: Effect,
    *,
    verifier: RestRecordVerifier,
    before: EffectState,
) -> EffectVerdict:
    resolved = _resolved_effect(workflow, effect)
    if resolved.readback is not None:
        # A screen contract must not be judged against the store.
        records = before.records
        return judge_records(resolved, before, records, substrate=verifier.substrate)
    return verifier.verify(resolved, before)


def evaluate_oracle_gate(
    workflow: Workflow,
    *,
    fixture: str = DEFAULT_BREAK_IT_FIXTURE,
) -> OracleGateResult:
    """Run channel disjointness and the MockMed --break-it fault.

    Status stays draft/halted when the proposed oracle would have accepted
    the lie, when actor and oracle share a channel, or when no second read
    exists. Nothing is guessed. The MockMed ``/api/db`` URL is the fixture's
    persistence boundary, not a production endpoint invented for the pin.
    """

    actor = actor_channel_ids(workflow)
    oracle = oracle_channel_ids(workflow)
    shared = actor & oracle
    effects = bound_effects(workflow)
    writes = _write_steps(workflow)
    reasons: list[str] = []

    if writes and not effects:
        reasons.append(MISSING_SECOND_READ)
        if _only_screen_postcondition(workflow):
            reasons.append(
                "proposed oracle shares the acting channel: "
                f"{CHANNEL_ACTING_SESSION} (same session banner)"
            )
        return OracleGateResult(
            fixture=fixture,
            actor_channels=sorted(actor),
            oracle_channels=sorted(oracle),
            shared_channels=sorted(shared),
            effects=effects,
            passed=False,
            halt_reason="; ".join(reasons),
        )
    if not writes:
        return OracleGateResult(
            fixture=fixture,
            actor_channels=sorted(actor),
            oracle_channels=sorted(oracle),
            shared_channels=sorted(shared),
            effects=effects,
            passed=True,
        )
    if shared:
        reasons.append(_shared_channel_reason(shared))
    if not _has_second_read(workflow):
        reasons.append(MISSING_SECOND_READ)

    if fixture != DEFAULT_BREAK_IT_FIXTURE:
        return OracleGateResult(
            fixture=fixture,
            actor_channels=sorted(actor),
            oracle_channels=sorted(oracle),
            shared_channels=sorted(shared),
            effects=effects,
            passed=False,
            halt_reason=(
                f"unknown break-it fixture {fixture!r}; "
                f"default is {DEFAULT_BREAK_IT_FIXTURE!r}"
            ),
        )

    from openadapt_flow.mockmed.fault_server import serve

    banner_text = _banner_text(workflow)
    records: list[dict[str, Any]] = []
    store_unchanged = False
    screen_claimed_success = False
    verdicts: list[EffectVerdict] = []
    status = 0
    base_url, db, stop = serve()
    try:
        verifier = RestRecordVerifier(
            _origin(base_url),
            records_path="/api/db",
            records_key="records",
            timeout_s=2.0,
            poll_interval_s=0.05,
        )
        before = verifier.capture_pre_state()
        encounter_url = f"{base_url.rstrip('/')}/api/encounter?fault=optimistic"
        status, _payload = _http_json(
            encounter_url,
            method="POST",
            body={
                "patient_id": str(workflow.params.get("record_id") or "p1"),
                "type": "Triage",
                "note": str(workflow.params.get("note") or "example"),
            },
        )
        snapshot = db.snapshot()
        records = list(snapshot.get("records") or [])
        store_unchanged = len(records) == 0
        screen_claimed_success = True
        for item in effects:
            effect = _step_effect(workflow, item)
            if set(item.oracle_channels) & _SCREEN_CHANNELS:
                verdicts.append(_execute_screen_oracle(workflow, effect, banner_text))
            else:
                verdicts.append(
                    _execute_sor_oracle(
                        workflow, effect, verifier=verifier, before=before
                    )
                )
    finally:
        stop()

    dumped = [
        {
            "step_id": item.step_id,
            "kind": item.kind,
            "verdict": verdict.verdict.value,
            "substrate": verdict.substrate,
            "reason": verdict.reason,
        }
        for item, verdict in zip(effects, verdicts)
    ]
    would_accept = bool(verdicts) and all(
        verdict.verdict is Verdict.CONFIRMED for verdict in verdicts
    )
    caught = any(verdict.verdict is Verdict.REFUTED for verdict in verdicts)
    if would_accept:
        reasons.append(BROKEN_CASE_ACCEPTED)
    elif not caught:
        reasons.append(
            "proposed oracle did not refute the lying backend; "
            "refusing to guess that it would catch --break-it"
        )
    if status not in {409, 200} and not store_unchanged:
        reasons.append("MockMed optimistic write did not leave the store unchanged")
    halt_reason = "; ".join(reasons) if reasons else None
    return OracleGateResult(
        fixture=fixture,
        actor_channels=sorted(actor),
        oracle_channels=sorted(oracle),
        shared_channels=sorted(shared),
        effects=effects,
        break_it_executed=True,
        break_it_verdicts=dumped,
        screen_claimed_success=screen_claimed_success,
        store_unchanged=store_unchanged,
        system_of_record_records=len(records),
        passed=halt_reason is None,
        halt_reason=halt_reason,
    )
