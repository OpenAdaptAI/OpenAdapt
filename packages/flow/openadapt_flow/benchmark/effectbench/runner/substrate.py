"""The substrate — the arm-facing action + perception channel.

An *arm* (see :mod:`.arms`) drives the GUI/app through a
:class:`SubstrateSession` and reads back only what a real agent could perceive:
the **screen** (a rendered banner / OCR read), never the system of record. The
independent effect oracle the benchmark scores with lives on the HARNESS side
(:mod:`.harness`) and is deliberately NOT reachable from a session — that
isolation is the whole non-gameability contract (README section "The oracle
interface"). A session therefore exposes:

* ``goal`` / ``substrate`` — the task intent (never a step list) and where the
  deception lives;
* :meth:`attempt_intended_action` — perform the task's action ONCE and return a
  :class:`ScreenObservation` (the untrusted witness every arm shares);
* :meth:`product_effect_verifier` — the *app's own* record-readback verifier
  (an EMR REST API the compiler mined at demo time). This is an AGENT-side
  capability an arm MAY choose to use (the compiler arm does; the screen-only
  ablation does not). It reads the app's public API over HTTP — a DIFFERENT
  object and transport from the benchmark's independent in-process oracle, so
  "the arm verified its own effect" and "the benchmark judged the effect" stay
  separate.

The reference substrate is :class:`MockMedSession`, which drives the bundled,
CI-fast, no-Docker MockMed fault server (``openadapt_flow.mockmed.fault_server``)
over real HTTP through its transactional persistence boundary. Every fault the
``fault_model`` study catalogs is reproduced by posting to
``/api/encounter?fault=<mode>``; the screen banner is derived from the REAL HTTP
status(es) exactly as ``mockmed/static/app.js`` ``saveViaBackend`` would paint
it, so the witness is computed, never hardcoded.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Protocol, Sequence, runtime_checkable

from openadapt_flow.benchmark.effectbench.runner.compound import (
    CompoundEffectVerifier,
)
from openadapt_flow.benchmark.effectbench.schema import Substrate
from openadapt_flow.runtime.effects.effect import Effect, EffectVerifier


@dataclass(frozen=True)
class ScreenObservation:
    """What an arm perceives from the SCREEN after acting — the untrusted witness.

    This is exactly the signal a vision/pixel agent reads: did the app paint a
    "saved" banner? EffectBench refuses to trust it for scoring; an arm that
    reports success from ``banner_saved`` alone is the silent-wrong-effect
    ablation.
    """

    #: Whether the app rendered a success banner ("saved") this run.
    banner_saved: bool
    #: Human-readable detail for the audit trail (the derived banner rule + the
    #: raw HTTP statuses that produced it).
    detail: str = ""
    #: The raw per-request outcome(s) behind the banner (for debugging / audit);
    #: e.g. HTTP status ints, ``None`` for a client-aborted request.
    raw: tuple[Any, ...] = ()


@runtime_checkable
class SubstrateSession(Protocol):
    """The narrow action+perception channel handed to an arm for one episode.

    Deliberately does NOT expose the benchmark oracle or the system-of-record
    read path — an arm can paint any screen but cannot reach the independent
    reading that judges it.
    """

    substrate: Substrate
    goal: str

    def attempt_intended_action(self, params: Mapping[str, str]) -> ScreenObservation:
        """Perform the task's action once and return the screen witness."""
        ...

    def product_effect_verifier(self) -> Optional[EffectVerifier]:
        """The app's own record verifier (agent-side), or ``None`` if the app
        exposes no record API to the agent. NOT the benchmark oracle."""
        ...


# ---------------------------------------------------------------------------
# The reference substrate: the bundled MockMed transactional fault server.
# ---------------------------------------------------------------------------


def _ok(status: Optional[int]) -> bool:
    return status is not None and 200 <= status < 300


@dataclass
class MockMedFault:
    """The transactional fault a MockMed reference task injects (from a task's
    ``initial_state``). Mirrors ``benchmark/silent_wrong_action`` scenarios."""

    #: ``fault_server`` ``?fault=`` mode (ok/partial/duplicate/timeout/…).
    mode: str
    #: How ``app.js`` issues the write: ``"single"`` (one POST) or ``"double"``
    #: (double-submit / double-delivered click, two POSTs).
    delivery: str = "single"
    #: Whether the write carries the idempotency key (only the ``idempotent``
    #: recommended-fix scenario does).
    keyed: bool = False
    #: Seed a concurrent actor's row before the run (the ``stale`` lost-update
    #: scenario overwrites it — collateral loss).
    seed_concurrent: bool = False


class MockMedSession:
    """Live MockMed session over one in-process fault server (real HTTP).

    Constructed per-episode by :func:`mockmed_session` (which owns bring-up /
    teardown). The arm sees only this object; the harness keeps the ground-truth
    ``FaultDB`` for the independent oracle to itself.

    Args:
        base_url: The running fault server's base URL (trailing slash).
        goal: The task's natural-language intent.
        fault: The transactional fault this episode injects.
        idempotency_key: Key posted when ``fault.keyed`` (idempotent scenario).
        extra_effects: The compound consequential-save contract's sub-effects
            BEYOND the primary ``record_written`` (e.g. the ``field_equals``
            note read-back), already resolved for this trial. The compiler-style
            arm's own product verifier checks these; the screen-only ablation
            ignores them. Kept on the session (not the arm) so an arm stays
            substrate-agnostic — the app's mined contract belongs to the app.
        client_abort_s: The ``AbortController`` window ``app.js`` arms for the
            write. The ``timeout`` fault commits then hangs past it, so the
            client sees an aborted request (no banner) while the row persisted.
    """

    #: The idempotency-key record field MockMed uses.
    KEY_FIELD = "key"

    def __init__(
        self,
        *,
        base_url: str,
        goal: str,
        fault: MockMedFault,
        idempotency_key: str = "effectbench-idem-key",
        extra_effects: Sequence[Effect] = (),
        client_abort_s: float = 0.3,
    ) -> None:
        self.substrate = Substrate.WEB
        self.goal = goal
        self._base = base_url.rstrip("/")
        self._fault = fault
        self._idem = idempotency_key
        self._extra: tuple[Effect, ...] = tuple(extra_effects)
        self._abort_s = client_abort_s

    # -- action channel -----------------------------------------------------

    def _post(self, params: Mapping[str, str], *, timed: bool) -> Optional[int]:
        """Issue one write POST to the fault backend; return its HTTP status
        (``None`` when the request aborted before a response)."""
        import requests  # lazy: keep module import light

        payload: dict[str, str] = {
            "patient_id": params.get("patient_id", ""),
            "type": params.get("type", "Triage"),
            "note": params.get("note", ""),
        }
        if self._fault.keyed:
            payload[self.KEY_FIELD] = self._idem
        url = f"{self._base}/api/encounter?fault={self._fault.mode}"
        try:
            resp = requests.post(
                url, json=payload, timeout=self._abort_s if timed else 5.0
            )
        except requests.exceptions.RequestException:
            return None
        return int(resp.status_code)

    def _drive(self, params: Mapping[str, str]) -> list[Optional[int]]:
        """Reproduce the write(s) ``app.js`` issues under this fault."""
        if self._fault.mode == "timeout":
            # Armed abort controller; server commits then hangs past it.
            return [self._post(params, timed=True)]
        if self._fault.delivery == "double":
            return [self._post(params, timed=False), self._post(params, timed=False)]
        return [self._post(params, timed=False)]

    def attempt_intended_action(self, params: Mapping[str, str]) -> ScreenObservation:
        """Drive the demonstrated write and derive the screen banner.

        The SAME action every arm performs — the arms differ only in how they
        decide success (screen banner vs effect verification), which is the
        whole ablation. The banner rule encodes ``app.js`` ``saveViaBackend``
        applied to the real server response(s)."""
        statuses = self._drive(params)
        banner = self._banner(statuses)
        return ScreenObservation(
            banner_saved=banner,
            detail=f"fault={self._fault.mode} statuses={statuses} banner={banner}",
            raw=tuple(statuses),
        )

    def _banner(self, statuses: list[Optional[int]]) -> bool:
        """Does ``app.js`` paint the "saved" banner for these real statuses?

        - ``optimistic`` paints success BEFORE the write resolves, then ignores
          the (rejecting) result: banner regardless of status.
        - ``timeout`` / double-delivery: banner if ANY response was 2xx.
        - single delivery: a 401 bounces to ``#login`` (no banner); a 2xx
          paints it; anything else is an error (no banner).
        """
        mode = self._fault.mode
        if mode == "optimistic":
            return True
        if mode == "timeout" or self._fault.delivery == "double":
            return any(_ok(s) for s in statuses)
        status = statuses[0] if statuses else None
        if status == 401:
            return False
        return _ok(status)

    # -- agent-side (product) record capability -----------------------------

    def product_effect_verifier(self) -> Optional[EffectVerifier]:
        """The app's own REST record readback (``GET /api/db``) an OpenAdapt
        compiler mines at demo time and an arm MAY use to gate on the effect.

        A DIFFERENT object and transport from the benchmark oracle (which reads
        the in-process ``FaultDB`` directly, harness-side): using this is the
        agent choosing to verify its own effect through the app's public API.

        Returns the app's REST readback wrapped as a compound verifier over the
        mined save contract (``record_written`` + the note ``field_equals``), so
        the arm gates on the WHOLE effect a partial save would silently break."""
        from openadapt_flow.runtime.effects.rest import RestRecordVerifier

        base = RestRecordVerifier(
            self._base,
            records_path="/api/db",
            records_key="records",
            poll_interval_s=0.02,
        )
        return CompoundEffectVerifier(base, extra_effects=self._extra)


@dataclass
class MockMedProvisioned:
    """A brought-up MockMed episode: the arm-facing session, the ground-truth
    store, and the teardown callable (harness-owned)."""

    session: MockMedSession
    read_records: "Any"  # zero-arg callable -> the FaultDB records (dicts)
    close: "Any"  # zero-arg teardown
    fingerprint: dict[str, Any] = field(default_factory=dict)


def mockmed_session(
    *,
    goal: str,
    fault: MockMedFault,
    extra_effects: Sequence[Effect] = (),
    client_abort_s: float = 0.3,
) -> MockMedProvisioned:
    """Bring up one isolated MockMed fault server for a single episode.

    Returns the arm-facing :class:`MockMedSession`, a zero-arg ``read_records``
    the harness hands to the INDEPENDENT in-process oracle (the ground-truth
    ``FaultDB`` snapshot — a read path the agent never touches), and a ``close``
    teardown. Seeds a concurrent-actor row first when the fault needs it.

    ``extra_effects`` are the resolved compound sub-effects the compiler-style
    arm's product verifier checks (the note ``field_equals`` read-back).
    """
    from openadapt_flow.mockmed.fault_server import serve

    url, db, stop = serve(port=0)
    db.reset(seed_concurrent=fault.seed_concurrent)

    def read_records() -> Optional[list[dict[str, Any]]]:
        snap = db.snapshot()
        recs = snap.get("records")
        return list(recs) if isinstance(recs, list) else None

    session = MockMedSession(
        base_url=url,
        goal=goal,
        fault=fault,
        extra_effects=extra_effects,
        client_abort_s=client_abort_s,
    )
    return MockMedProvisioned(
        session=session,
        read_records=read_records,
        close=stop,
        fingerprint={
            "substrate": "web",
            "app": "mockmed.fault_server",
            "fault": fault.mode,
            "delivery": fault.delivery,
            "keyed": fault.keyed,
            "seed_concurrent": fault.seed_concurrent,
        },
    )
