"""Compile a computer-use agent's first verified run into a program.

Run 1 lets a computer-use agent do the task inside the harness
(:mod:`.harness`): structural observations first, a screenshot only when the
agent asks or at a gate, and the right-record check (plus an optional hold)
before every save. The run is recorded and compiled by ``openadapt_flow.jit``.

Then, in order:

1. **Record check.** The system of record is read when the run starts and
   after it ends, through the ``verifier:`` section of ``verify.yaml``. Run 1
   passes only when exactly one record appeared, nothing else changed, the
   record carries every field the operator expected (``expect``, such as the
   patient id), and every input value landed in it. Otherwise the draft is
   set aside and nothing can be admitted.
2. **Proposed checks.** The change becomes proposed record checks
   (``record --verify``'s proposer). A person confirms them: they become the
   program's ``verify.yaml``, and the same person confirms the parameters.
3. **Admission** under the JIT lifecycle: canaries, then a registered admitter
   key, or a registered agent under a signed admission policy (self-admission
   only when that policy allows it). Without either, a person admits at a
   terminal.
4. **Emit.** An Agent Skill, an MCP server with one tool, and a one-line CLI
   command, all calling :func:`run_compiled`.

Every later run executes the admitted program with no model calls, reads each
save back through the verifier, and gets a ``verify-run`` receipt against the
confirmed checks.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence, Union

from openadapt_flow.compile_agent.harness import GateRequest, Harness, SaveGate
from openadapt_flow.jit import lifecycle
from openadapt_flow.jit.compile_agent import (
    CompiledAgent,
    JitResult,
    TaskRequest,
    WebApp,
    compile_agent,
)
from openadapt_flow.jit.keys import CustomerModelKey
from openadapt_flow.jit.signature import TaskSignature

__all__ = [
    "BACKENDS",
    "UnsupportedTarget",
    "CompileResult",
    "FirstRunCompiler",
    "RecordCheck",
    "ReplayResult",
    "SPEC_SCHEMA",
    "check_run_record",
    "compile_first_run",
    "run_compiled",
]

SPEC_SCHEMA = "openadapt.compile-agent/v1"
SPEC_FILE = "compile_agent.json"
OUT_DIR = "compiled"
PROPOSED_FILE = "verify.proposed.yaml"
CONFIRMED_FILE = "verify.yaml"
CHECKS_FILE = "record_checks.json"
TRACE_FILE = "code_action_trace.json"
AGENT_READS_FILE = "agent_reads.json"

_PLACEHOLDER_RE = re.compile(r"{([A-Za-z_][A-Za-z0-9_]*)}")

#: The surfaces ``record --backend`` drives. compile-agent runs on ``web`` only
#: so far: the JIT captures and runs through a Playwright browser, and the
#: harness reads the page's DOM.
BACKENDS = ("web", "windows", "macos", "linux", "rdp", "citrix")


class UnsupportedTarget(ValueError):
    """The app is not a web app. compile-agent does not drive it yet, and never
    falls back to a browser for it."""


def check_target(url: str, backend: str = "web") -> None:
    """Refuse any target that is not a web app at an http(s) URL.

    Raises:
        UnsupportedTarget: a non-web backend, or a URL that is not http(s).
    """

    from urllib.parse import urlsplit

    if backend not in BACKENDS:
        raise UnsupportedTarget(f"unknown backend {backend!r} (one of {BACKENDS})")
    if backend != "web":
        raise UnsupportedTarget(
            f"compile-agent runs web apps only so far; the {backend!r} backend "
            "(which record --backend drives) is not supported yet. Nothing ran."
        )
    if urlsplit(url).scheme not in ("http", "https"):
        raise UnsupportedTarget(
            f"{url!r} is not an http(s) URL. compile-agent runs web apps only so "
            "far; a desktop, RDP or Citrix target is not supported yet. Nothing ran."
        )


PersonOrCallback = Union[str, Callable[[dict[str, Any]], Optional[str]], None]


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _stamp() -> str:
    return time.strftime("%Y%m%dT%H%M%S", time.gmtime()) + f"-{uuid.uuid4().hex[:6]}"


def _write_json(path: Path, data: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True, default=str) + "\n")
    return path


def _slug(text: str) -> str:
    words = re.findall(r"[a-z0-9]+", _PLACEHOLDER_RE.sub("", text.lower()))
    return "compile-agent." + ("-".join(words[:6]) or "task")


# -- the run-1 record check ------------------------------------------------------


@dataclass
class RecordCheck:
    """Whether run 1 changed exactly the record the task asked for."""

    passed: bool
    reasons: list[str]
    created: int
    changed: int
    removed: int
    #: The new record's field names (values stay in the private snapshots).
    fields: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def check_run_record(
    before: Optional[list[dict[str, Any]]],
    after: Optional[list[dict[str, Any]]],
    *,
    expect: Mapping[str, Any],
    values: Mapping[str, str],
) -> RecordCheck:
    """Run 1's change in the system of record, against what the task named.

    Passes only when one record appeared and nothing else changed, the record
    has every ``expect`` field with that value, and every input value is the
    value of one of its fields.
    """

    from openadapt_flow.record_sor import diff_snapshots

    if before is None or after is None:
        return RecordCheck(False, ["the system of record could not be read"], 0, 0, 0)
    diff = diff_snapshots(before, after)
    reasons: list[str] = []
    if len(diff.created) != 1:
        reasons.append(f"{len(diff.created)} records appeared; the task makes one")
    if diff.changed:
        reasons.append(f"{len(diff.changed)} existing records changed")
    if diff.removed:
        reasons.append(f"{len(diff.removed)} records disappeared")
    record = diff.created[0] if len(diff.created) == 1 else {}
    if record:
        for name, wanted in expect.items():
            if str(record.get(name)) != str(wanted):
                reasons.append(
                    f"the new record's {name!r} is not the one the task named "
                    "(wrong record)"
                )
        field_values = {str(v) for v in record.values() if v is not None}
        for name, value in values.items():
            if value not in field_values:
                reasons.append(f"input {name!r} is not in the new record")
    return RecordCheck(
        passed=not reasons,
        reasons=reasons,
        created=len(diff.created),
        changed=len(diff.changed),
        removed=len(diff.removed),
        fields=sorted(record),
    )


# -- results --------------------------------------------------------------------


@dataclass
class CompileResult:
    """What :func:`compile_first_run` did.

    ``status`` is ``admitted`` (later runs make no model calls), ``draft``
    (run 1 passed; checks or admission wait for a person), ``needs_person``
    (outside the agent's admission policy), or why nothing can be admitted:
    ``refused_wrong_record``, ``refused_by_hold``, ``record_check_failed``,
    ``canary_failed``, ``capture_failed``, ``unsupported``.
    """

    status: str
    dir: Path
    task_key: str
    detail: Optional[str] = None
    run1: dict[str, Any] = field(default_factory=dict)
    record_check: Optional[dict[str, Any]] = None
    proposed_checks: Optional[Path] = None
    confirmed_checks: Optional[Path] = None
    admission: Optional[dict[str, Any]] = None
    emitted: dict[str, Any] = field(default_factory=dict)

    @property
    def admitted(self) -> bool:
        return self.status == "admitted"

    def to_dict(self) -> dict[str, Any]:
        out = dict(self.__dict__)
        for key in ("dir", "proposed_checks", "confirmed_checks"):
            out[key] = str(out[key]) if out[key] is not None else None
        return out


@dataclass
class ReplayResult:
    """One run of an admitted program."""

    status: str
    verified: bool
    model_calls: Optional[int]
    seconds: float
    run_dir: Optional[Path]
    receipt_outcome: Optional[str] = None
    receipt: Optional[Path] = None
    halt_reason: Optional[str] = None
    detail: Optional[str] = None
    inbox_item: Optional[Path] = None

    def to_dict(self) -> dict[str, Any]:
        out = dict(self.__dict__)
        for key in ("run_dir", "receipt", "inbox_item"):
            out[key] = str(out[key]) if out[key] is not None else None
        return out


# -- the agent the JIT records ----------------------------------------------------


class _HarnessedAgent:
    """What ``compile_agent`` records: the inner agent, driving the harness."""

    def __init__(
        self,
        inner: Any,
        *,
        gate: SaveGate,
        keep: Sequence[str],
        evidence_dir: Path,
        principal_id: Optional[str],
    ) -> None:
        self.inner = inner
        self.gate = gate
        self.keep = list(keep)
        self.evidence_dir = evidence_dir
        self.principal_id = principal_id
        self.harness: Optional[Harness] = None
        self.ledger: Any = None
        self.error: Optional[str] = None

    @property
    def dialect(self) -> str:
        return str(self.inner.dialect)

    @property
    def requires_model_key(self) -> bool:
        return bool(getattr(self.inner, "requires_model_key", True))

    def run(self, request: TaskRequest, computer: Any) -> None:
        self.harness = Harness(
            computer, self.gate, keep=self.keep, evidence_dir=self.evidence_dir
        )
        try:
            self.ledger = self.inner.drive(request, self.harness)
        except BaseException as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            self.ledger = getattr(self.inner, "last_ledger", None)
            raise


# -- the pipeline -------------------------------------------------------------------


class FirstRunCompiler:
    """One task's compile-agent state, on disk under ``<cache_dir>/<task key>``.

    :func:`compile_first_run` runs every step. Use the steps when a person confirms
    later: :meth:`record`, :meth:`confirm_checks`, :meth:`admit`, :meth:`emit`,
    then :meth:`run` for every later run.
    """

    def __init__(
        self,
        prompt: str,
        *,
        params: Sequence[str],
        url: str,
        verify: Union[str, Path],
        expect: Mapping[str, Any],
        on_screen: Sequence[str],
        task_id: Optional[str] = None,
        policy: str = "clinical-write",
        cache_dir: Union[str, Path] = ".openadapt-compile-agent",
        key_dir: Optional[Union[str, Path]] = None,
        reset: Optional[Callable[[], None]] = None,
        headless: bool = True,
        on_warning: Optional[Callable[[str], None]] = None,
        model_key: Optional[CustomerModelKey] = None,
        backend: str = "web",
    ) -> None:
        from openadapt_flow.record_sor import load_observer

        check_target(url, backend)

        reserved = sorted(set(params) & {"reset", "task_dir"})
        if reserved:
            raise ValueError(f"parameter names {reserved} are reserved")
        placeholders = set(_PLACEHOLDER_RE.findall(prompt))
        if placeholders != set(params):
            raise ValueError(
                f"the prompt's placeholders {sorted(placeholders)} must be the "
                f"parameters {sorted(params)}"
            )
        if not [s for s in on_screen if str(s).strip()]:
            raise ValueError(
                "on_screen must name the record's identity as shown on screen "
                "(for example a name and an MRN); a save with nothing to check "
                "is refused"
            )
        if not expect:
            raise ValueError(
                "expect must name at least one field of the record the task "
                "writes (for example its patient id)"
            )
        self.task = TaskSignature(
            task_id or _slug(prompt), prompt, {name: str for name in params}
        )
        self.url = url
        self.verify_path = Path(verify).resolve()
        self.expect = {str(k): str(v) for k, v in expect.items()}
        self.on_screen = [str(s) for s in on_screen if str(s).strip()]
        self.policy = policy
        self.cache_dir = Path(cache_dir).resolve()
        self.key_dir = Path(key_dir).resolve() if key_dir is not None else None
        self.user_reset = reset
        self.headless = headless
        self.on_warning = on_warning
        self.model_key = model_key
        self.observer, self.verifier_section = load_observer(self.verify_path)
        self._baseline: Optional[list[dict[str, Any]]] = None
        self._pending: Optional[tuple[Any, dict[str, str], Path]] = None
        self._receipt_baseline: Optional[Path] = None

    # -- layout ------------------------------------------------------------------

    @property
    def dir(self) -> Path:
        return self.cache_dir / self.task.key

    @property
    def out_dir(self) -> Path:
        return self.dir / OUT_DIR

    def _jit(self, agent: Any = None) -> CompiledAgent:
        return compile_agent(
            agent,
            task=self.task,
            app=WebApp(
                url=self.url,
                record_reader=lambda: self.observer.system_of_record,
                reset=self._on_reset,
                headless=self.headless,
            ),
            verifier=self.observer.verifier,
            policy=self.policy,
            cache_dir=self.cache_dir,
            dialect=None if agent is not None else "generic",
            model_key=self.model_key,
            key_dir=self.key_dir,
            on_warning=self.on_warning,
        )

    def _on_reset(self) -> None:
        """Called by the JIT before every capture, canary and run, just
        before the browser opens: the baseline read for this run."""

        from openadapt_flow.verify_run import capture_baseline

        if self.user_reset is not None:
            self.user_reset()
        self._baseline = self.observer.system_of_record
        self._receipt_baseline = None
        if self._pending is not None:
            cfg, values, path = self._pending
            capture_baseline(cfg, values, path)
            self._receipt_baseline = path

    def save_spec(self) -> Path:
        return _write_json(
            self.dir / SPEC_FILE,
            {
                "schema": SPEC_SCHEMA,
                "task": {
                    "task_id": self.task.task_id,
                    "instruction": self.task.instruction,
                    "params": sorted(self.task.params),
                    "key": self.task.key,
                },
                "url": self.url,
                "verify": str(self.verify_path),
                "expect": self.expect,
                "on_screen": self.on_screen,
                "policy": self.policy,
                "cache_dir": str(self.cache_dir),
                "key_dir": str(self.key_dir) if self.key_dir else None,
                "headless": self.headless,
            },
        )

    @classmethod
    def load(
        cls, task_dir: Union[str, Path], *, reset: Optional[Callable[[], None]] = None
    ) -> "FirstRunCompiler":
        spec = json.loads((Path(task_dir) / SPEC_FILE).read_text())
        if spec.get("schema") != SPEC_SCHEMA:
            raise ValueError(f"{task_dir} is not a compiled agent task")
        task = spec["task"]
        return cls(
            task["instruction"],
            params=task["params"],
            url=spec["url"],
            verify=spec["verify"],
            expect=spec["expect"],
            on_screen=spec["on_screen"],
            task_id=task["task_id"],
            policy=spec["policy"],
            cache_dir=spec["cache_dir"],
            key_dir=spec.get("key_dir"),
            reset=reset,
            headless=bool(spec.get("headless", True)),
        )

    # -- step 1: run 1 and its record check --------------------------------------

    def record(
        self,
        agent: Any,
        params: Mapping[str, Any],
        *,
        hold: Optional[Callable[[GateRequest], bool]] = None,
        principal_id: Optional[str] = None,
    ) -> CompileResult:
        """Run 1: the agent does the task in the harness; Flow records it.

        ``principal_id`` is the identity the recording is signed with (the
        registered agent, when it will admit under a policy). Default: the
        agent's own.
        """

        self.save_spec()
        jit = self._jit()
        values = self.task.validate(params)
        if jit.admitted or jit.candidate() is not None:
            raise lifecycle.AdmissionError(
                "this task already has a draft or an admitted program; run 1 runs "
                "only once (reject the draft to record again)"
            )
        # A new run 1 makes a new draft: checks a person confirmed for an
        # earlier draft must not carry over to it.
        for stale in (
            AGENT_READS_FILE,
            PROPOSED_FILE,
            CONFIRMED_FILE,
            CHECKS_FILE,
            "admission.json",
            "emitted.json",
        ):
            (self.out_dir / stale).unlink(missing_ok=True)
        attempt = self.dir / "attempts" / _stamp()
        harnessed = _HarnessedAgent(
            agent,
            gate=SaveGate(self.on_screen, hold),
            keep=[*self.on_screen, *values.values(), *self.expect.values()],
            evidence_dir=attempt / "gates",
            principal_id=principal_id or getattr(agent, "principal_id", None),
        )
        jit.agent = harnessed
        jit.dialect = harnessed.dialect
        self._baseline = None
        started = time.monotonic()
        result: JitResult = jit.run(**params)
        seconds = time.monotonic() - started
        after = self.observer.system_of_record
        harness = harnessed.harness
        run1: dict[str, Any] = {
            "attempt": str(attempt),
            "jit_status": result.status,
            "seconds": round(seconds, 3),
            "agent": harnessed.ledger.to_dict() if harnessed.ledger else None,
            "harness": harness.stats.to_dict() if harness else None,
            "gates": harness.gate_log if harness else [],
            "agent_error": harnessed.error,
        }
        trace_data: Optional[dict[str, Any]] = None
        trace = getattr(agent, "last_trace", None)
        if trace is not None:
            # Code as action: the structured steps and what the agent read.
            trace_data = trace.to_dict()
            _write_json(attempt / TRACE_FILE, trace_data)
            run1["code_action"] = trace_data["summary"]
        _write_json(attempt / "run1.json", run1)

        def done(status: str, detail: Optional[str], **kw: Any) -> CompileResult:
            out = CompileResult(
                status=status,
                dir=self.dir,
                task_key=self.task.key,
                detail=detail,
                run1=run1,
                **kw,
            )
            _write_json(attempt / "result.json", out.to_dict())
            return out

        if harness is not None and harness.refusal is not None:
            decision = (harness.gate_log[-1] if harness.gate_log else {}).get(
                "decision", "refused_wrong_record"
            )
            status = (
                "refused_by_hold"
                if decision == "refused_by_hold"
                else "refused_wrong_record"
            )
            if jit.candidate() is not None:
                # An agent that caught the refusal and went on still made a
                # draft; it must never be admitted.
                self._set_aside(jit, status, [harness.refusal])
            return done(status, harness.refusal)
        if result.status != lifecycle_status_draft():
            return done(result.status, result.detail)
        check = check_run_record(
            self._baseline, after, expect=self.expect, values=values
        )
        run1["record_check"] = check.to_dict()
        _write_json(attempt / "run1.json", run1)
        if not check.passed:
            kept = self._set_aside(jit, "record_check_failed", check.reasons)
            return done(
                "record_check_failed",
                "; ".join(check.reasons) + f" (draft set aside at {kept})",
                record_check=check.to_dict(),
            )
        proposed = self._propose(self._baseline or [], after or [], values)
        if trace_data is not None:
            run1["code_action"]["program"] = self._check_trace(jit, trace_data)
            _write_json(attempt / "run1.json", run1)
            _write_json(
                self.out_dir / AGENT_READS_FILE,
                {
                    "identity_read_before_commit": run1["code_action"][
                        "identity_read_before_commit"
                    ],
                    "read_back_after_commit": run1["code_action"][
                        "read_back_after_commit"
                    ],
                    "program": run1["code_action"]["program"],
                },
            )
        return done(
            "draft",
            "run 1 passed its record check; a person confirms the proposed checks",
            record_check=check.to_dict(),
            proposed_checks=proposed,
        )

    @staticmethod
    def _check_trace(jit: CompiledAgent, trace: Mapping[str, Any]) -> dict[str, Any]:
        """Whether the draft program carries the locator of every element
        the agent acted on by structure."""

        from openadapt_flow.compile_agent.code_action import check_trace_against_ir
        from openadapt_flow.ir import Workflow

        try:
            workflow = Workflow.load(jit.bundle_dir)
        except Exception as exc:  # noqa: BLE001 - reported, never admitted on
            return {"passed": False, "error": f"{type(exc).__name__}: {exc}"}
        return check_trace_against_ir(trace, workflow)

    def _set_aside(self, jit: CompiledAgent, status: str, reasons: list[str]) -> Path:
        program = jit.program_dir
        _write_json(program / "refused.json", {"status": status, "reasons": reasons})
        kept = self.dir / f"refused-{status}-{_stamp()}"
        program.rename(kept)
        return kept

    def _propose(
        self,
        before: list[dict[str, Any]],
        after: list[dict[str, Any]],
        values: Mapping[str, str],
    ) -> Path:
        from openadapt_flow.record_sor import (
            _write_private,
            diff_snapshots,
            propose_effects,
            render_contract,
        )

        proposal = propose_effects(diff_snapshots(before, after), values)
        path = self.out_dir / PROPOSED_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_private(path, render_contract(self.verifier_section, proposal.effects))
        return path

    def proposed(self) -> dict[str, Any]:
        """The proposed checks a person reviews: each effect's kind and the
        fields it matches, with parameters named and literals shown."""

        import yaml

        doc = yaml.safe_load((self.out_dir / PROPOSED_FILE).read_text())
        effects = doc.get("effects") or []
        reads = self.out_dir / AGENT_READS_FILE
        extra: dict[str, Any] = {}
        if reads.is_file():
            # What a code-as-action agent read: the identity before the save,
            # and its inputs read back after it. Evidence for the person.
            extra["agent_read"] = json.loads(reads.read_text())
        return {
            **extra,
            "file": str(self.out_dir / PROPOSED_FILE),
            # Confirming the checks also confirms these as the parameters.
            "params": sorted(self.task.params),
            "effects": [
                {
                    "kind": e.get("kind"),
                    "match": {
                        k: (
                            f"{{param: {v['param']}}}"
                            if "param" in v
                            else v.get("literal")
                        )
                        for k, v in (e.get("match") or {}).items()
                    },
                }
                for e in effects
            ],
        }

    # -- step 2: a person confirms the checks and the parameters ------------------

    def confirm_checks(
        self, *, principal_id: str, drop_fields: Sequence[str] = ()
    ) -> Path:
        """A person confirms the proposed record checks.

        ``drop_fields`` are match fields to leave out (values that change on
        every save). A field the task's ``expect`` names can't be dropped.
        The result is the program's ``verify.yaml``. The same person confirms
        the task's parameters, which recompiles the draft with them.

        Raises:
            AdmissionError: not a person, or the identity that recorded.
            ValueError: the checks would no longer pin the record or an input.
        """

        import yaml

        from openadapt_flow.record_sor import _write_private
        from openadapt_flow.verify_run import parse_verify_config, sha256_tagged

        jit = self._jit()
        name = lifecycle.check_principal(
            principal_id, jit.program_dir, key_dir=self.key_dir
        )
        pinned = sorted(set(drop_fields) & set(self.expect))
        if pinned:
            raise ValueError(f"{pinned} identify the record; they can't be dropped")
        proposed_path = self.out_dir / PROPOSED_FILE
        doc = yaml.safe_load(proposed_path.read_text())
        effects = []
        for effect in doc.get("effects") or []:
            effect = dict(effect)
            effect.pop("needs_operator_confirmation", None)
            effect["match"] = {
                k: v
                for k, v in (effect.get("match") or {}).items()
                if k not in drop_fields
            }
            if effect.get("kind") == "record_written":
                effect["probe"] = (
                    "exactly one new record with "
                    + ", ".join(
                        f"{k}="
                        + (
                            f"parameter {v['param']!r}"
                            if v.get("param")
                            else repr(v.get("literal"))
                        )
                        for k, v in effect["match"].items()
                    )
                    + f" (confirmed by {name})"
                )
            effects.append(effect)
        written = [
            e
            for e in effects
            if e.get("kind") == "record_written" and e.get("count_new_only")
        ]
        if len(written) != 1:
            raise ValueError("the checks must claim exactly one new record")
        match = written[0]["match"]
        for key, wanted in self.expect.items():
            if (match.get(key) or {}).get("literal") != wanted:
                raise ValueError(f"the check does not pin {key!r} to {wanted!r}")
        bound = {v.get("param") for v in match.values() if isinstance(v, dict)}
        unbound = sorted(set(self.task.params) - bound)
        if unbound:
            raise ValueError(f"no check reads back input(s) {unbound}")
        config = {
            "schema_version": "openadapt.verify/v1",
            "verifier": self.verifier_section,
            "effects": effects,
        }
        parse_verify_config(config, source="confirmed checks")
        if self.task.params:
            jit.confirm_params(
                principal_id=name, accept_params=sorted(self.task.params), confirm="ok"
            )
        path = self.out_dir / CONFIRMED_FILE
        _write_private(
            path,
            f"# Confirmed by {name} at {_now()} from {PROPOSED_FILE}.\n"
            + yaml.safe_dump(config, sort_keys=False, allow_unicode=True),
        )
        _write_json(
            self.out_dir / CHECKS_FILE,
            {
                "confirmed_by": name,
                "confirmed_at": _now(),
                "proposed_sha256": hashlib.sha256(
                    proposed_path.read_bytes()
                ).hexdigest(),
                "contract_sha256": sha256_tagged(config),
                "dropped_fields": sorted(drop_fields),
                "params_confirmed": sorted(self.task.params),
            },
        )
        lifecycle._event(jit.program_dir, "record_checks_confirmed", principal_id=name)
        return path

    # -- step 3: admission ----------------------------------------------------------

    def _confirmed_config(self) -> Any:
        from openadapt_flow.verify_run import load_verify_config

        path = self.out_dir / CONFIRMED_FILE
        if not path.is_file():
            raise lifecycle.AdmissionError("confirm the record checks first")
        return load_verify_config(path)

    def _governed(
        self,
        kind: str,
        values: dict[str, str],
        run: Callable[[], JitResult],
    ) -> tuple[JitResult, Optional[dict[str, Any]], Optional[Path]]:
        """Run ``run`` with a verify-run baseline taken at its reset, then
        judge the confirmed checks against the system of record."""

        from openadapt_flow import __version__
        from openadapt_flow.verify_run import verify_run, write_result

        cfg = self._confirmed_config()
        work = self.dir / "receipts" / f"{kind}-{_stamp()}"
        work.mkdir(parents=True, exist_ok=True)
        self._pending = (cfg, values, work / "baseline.json")
        self._receipt_baseline = None
        try:
            result = run()
        finally:
            self._pending = None
        if self._receipt_baseline is None:
            return result, None, None
        receipt = verify_run(
            cfg,
            params=values,
            run_record={
                "executor_kind": "flow_replay",
                "executor_id": self.task.key,
                "executor_build": {"openadapt-flow": __version__},
            },
            authorization_ref=f"compile-agent:{self.task.key}:{kind}",
            baseline_path=self._receipt_baseline,
        )
        return result, receipt, write_result(receipt, work)

    def admit(
        self,
        *,
        admitter: Any = None,
        person: Optional[str] = None,
        review: Optional[Callable[[str, dict[str, Any]], bool]] = None,
        canary_params: Optional[Sequence[Mapping[str, Any]]] = None,
    ) -> CompileResult:
        """Canary the draft, then admit it.

        ``admitter`` is a registered :class:`~openadapt_flow.jit.authority.Principal`:
        an admitter key, or an agent under a signed admission policy (which
        needs ``min_clean_canaries`` canaries). Without one, ``person`` admits
        at an interactive terminal: ``review`` shows them the steps and the
        parameters (``"review"``), then the canary results (``"admit"``), and
        returns True only when they confirm each.

        ``canary_params`` are the values each canary saves, one mapping per
        canary (the last is reused). A canary is a real save on the record the
        task names, so the operator chooses values that are safe there.

        Each canary also gets a verify-run receipt against the confirmed
        checks; a canary passes only when both end VERIFIED.
        """

        jit = self._jit()
        self._confirmed_config()
        if admitter is None and person is None:
            return self._result(
                "draft",
                "no admitter: pass a registered key or agent principal, or a person "
                "at a terminal",
            )
        rows = [dict(row) for row in canary_params or []]
        if not rows:
            return self._result(
                "draft",
                "pass canary values: each canary saves them on the record "
                f"{self.expect}, so choose values that are safe to write there",
            )
        if admitter is not None:
            policy = getattr(admitter, "policy", None)
            count = policy.policy.min_clean_canaries if policy is not None else 1
        else:
            if review is None:
                return self._result(
                    "draft",
                    f"{person} reviews the steps at a terminal: openadapt-flow jit "
                    f"review {self.dir}, then pass review= to show them here",
                )
            status = jit.status()
            shown = {"steps": status["steps"], "params": sorted(self.task.params)}
            if not review("review", shown):
                return self._result("draft", f"{person} did not confirm the review")
            jit.review(
                principal_id=str(person),
                reviewed_steps=[s["id"] for s in status["steps"]],
                confirm="ok",
            )
            count = 1
        canaries: list[dict[str, Any]] = []
        for i in range(count):
            params = rows[min(i, len(rows) - 1)]
            values = self.task.validate(params)
            try:
                result, receipt, path = self._governed(
                    f"canary-{i + 1}",
                    values,
                    lambda params=params: jit.canary(
                        principal_id=admitter if admitter is not None else person,
                        **params,
                    ),
                )
            except lifecycle.AdmissionError as exc:
                # Outside the agent's policy: the JIT queued it for a person.
                return self._result(
                    "needs_person", str(exc), admission={"canaries": canaries}
                )
            outcome = _receipt_outcome(receipt)
            canaries.append(
                {
                    "status": result.status,
                    "model_calls": result.model_calls,
                    "receipt_outcome": outcome,
                    "receipt": str(path) if path else None,
                    "halt_reason": result.halt_reason,
                }
            )
            if result.status != "VERIFIED" or outcome != "VERIFIED":
                return self._result(
                    "canary_failed",
                    result.halt_reason or result.detail or f"receipt {outcome}",
                    admission={"canaries": canaries},
                )
        if admitter is not None:
            out = jit.admit_with_key(admitter)
            if out["outcome"] != "admitted":
                return self._result(
                    "needs_person",
                    "; ".join(out.get("reasons") or []),
                    admission={"canaries": canaries, **out},
                )
            manifest = out["manifest"]
        else:
            assert review is not None
            if not review("admit", {"canaries": canaries}):
                return self._result(
                    "draft",
                    f"{person} did not admit the program",
                    admission={"canaries": canaries},
                )
            manifest = jit.admit(principal_id=str(person), confirm="ok")
        admission = {
            "canaries": canaries,
            "admitted_by": manifest.get("admitted_by_principal_id"),
            "mode": manifest.get("admission_mode"),
            "digest": manifest.get("digest"),
            "policy_id": manifest.get("policy_id"),
        }
        _write_json(self.out_dir / "admission.json", admission)
        return self._result("admitted", None, admission=admission)

    def _result(self, status: str, detail: Optional[str], **kw: Any) -> CompileResult:
        confirmed = self.out_dir / CONFIRMED_FILE
        return CompileResult(
            status=status,
            dir=self.dir,
            task_key=self.task.key,
            detail=detail,
            proposed_checks=self.out_dir / PROPOSED_FILE,
            confirmed_checks=confirmed if confirmed.is_file() else None,
            **kw,
        )

    # -- step 4: emit -------------------------------------------------------------

    def cli_line(self) -> str:
        flags = " ".join(
            f"--param {name}=<{name}>" for name in sorted(self.task.params)
        )
        line = f"openadapt-flow compile-agent run {self.dir}"
        return f"{line} {flags}".rstrip()

    def emit(self) -> dict[str, Any]:
        """Write the Agent Skill, the MCP server, and the CLI line."""

        from openadapt_flow.compile_agent.emit import emit_mcp, emit_skill_for

        jit = self._jit()
        if not jit.admitted:
            raise lifecycle.AdmissionError("emit an admitted program only")
        cli = self.cli_line()
        skill = emit_skill_for(jit.bundle_dir, self.out_dir / "skill", cli)
        mcp = emit_mcp(self, self.out_dir / "mcp" / "server.py")
        emitted = {"skill": str(skill), "mcp_server": str(mcp), "cli": cli}
        _write_json(self.out_dir / "emitted.json", emitted)
        return emitted

    # -- every later run -------------------------------------------------------------

    def run(self, **params: Any) -> ReplayResult:
        """Run the admitted program: no model calls, every save read back,
        and a verify-run receipt against the confirmed checks."""

        jit = self._jit()
        values = self.task.validate(params)
        started = time.monotonic()
        if not jit.admitted:
            return ReplayResult(
                status="not_admitted",
                verified=False,
                model_calls=None,
                seconds=0.0,
                run_dir=None,
                detail="this task has no admitted program; compile an agent run first",
            )
        result, receipt, path = self._governed("run", values, lambda: jit.run(**params))
        outcome = _receipt_outcome(receipt)
        verified = result.status == "VERIFIED" and outcome == "VERIFIED"
        status = result.status
        if result.status == "VERIFIED" and outcome != "VERIFIED":
            status = "RECEIPT_NOT_VERIFIED"
        return ReplayResult(
            status=status,
            verified=verified,
            model_calls=result.model_calls,
            seconds=round(time.monotonic() - started, 3),
            run_dir=result.run_dir,
            receipt_outcome=outcome,
            receipt=path,
            halt_reason=result.halt_reason,
            detail=result.detail,
            inbox_item=result.inbox_item,
        )


def _receipt_outcome(receipt: Optional[Mapping[str, Any]]) -> Optional[str]:
    """The verify-run receipt's outcome, upper-cased like run outcomes."""

    outcome = ((receipt or {}).get("receipt") or {}).get("outcome")
    return str(outcome).upper() if outcome else None


def lifecycle_status_draft() -> str:
    from openadapt_flow.jit.compile_agent import NEEDS_HUMAN_ADMIT

    return NEEDS_HUMAN_ADMIT


def _confirmer(confirm: PersonOrCallback, compiler: FirstRunCompiler) -> Optional[str]:
    if confirm is None or isinstance(confirm, str):
        return confirm
    return confirm(compiler.proposed())


def compile_first_run(
    prompt: str,
    *,
    params: Mapping[str, Any],
    url: str,
    verify: Union[str, Path],
    expect: Mapping[str, Any],
    on_screen: Sequence[str],
    agent: Any,
    confirm_checks: PersonOrCallback = None,
    drop_fields: Sequence[str] = (),
    admitter: Any = None,
    person: Optional[str] = None,
    review: Optional[Callable[[str, dict[str, Any]], bool]] = None,
    hold: Optional[Callable[[GateRequest], bool]] = None,
    canary_params: Optional[Sequence[Mapping[str, Any]]] = None,
    task_id: Optional[str] = None,
    policy: str = "clinical-write",
    cache_dir: Union[str, Path] = ".openadapt-compile-agent",
    key_dir: Optional[Union[str, Path]] = None,
    model_key: Optional[CustomerModelKey] = None,
    reset: Optional[Callable[[], None]] = None,
    headless: bool = True,
    on_warning: Optional[Callable[[str], None]] = None,
) -> CompileResult:
    """Turn an agent's first verified run of ``prompt`` into an admitted program.

    Args:
        prompt: The task, with ``{name}`` for each parameter.
        params: The values for run 1.
        url: The app's entry URL.
        verify: A ``verify.yaml`` whose ``verifier:`` section reads the system
            of record (rest, sql, fhir, graphql or file).
        expect: Fields the new record must have, such as ``{"patient_id": "p1"}``.
        on_screen: The record's identity as the screen shows it, such as
            ``["Jane Sample", "MRN P1"]``. Checked before every save.
        agent: :class:`~openadapt_flow.compile_agent.agents.ClaudeAgent`,
            :class:`~openadapt_flow.compile_agent.agents.ScriptedAgent`, or any
            object with ``dialect`` and ``drive(request, harness)``.
        confirm_checks: The person who confirms the proposed checks, or a
            callback that gets them and returns that person's name (None to
            leave them waiting).
        drop_fields: Match fields that person leaves out of the checks.
        admitter: A registered admitter key or policy agent
            (:func:`~openadapt_flow.jit.authority.principal_from_key`).
        person: Without an admitter, the person who reviews, attends the
            canary and admits at a terminal.
        review: With ``person``: shows them the steps, then the canary
            results, and returns True only when they confirm each.
        hold: Asked before every save in run 1; True lets it through.
        canary_params: The values each canary saves, one mapping per canary
            (the last is reused). A canary is a real save on the record the
            task names, so choose values that are safe there. Required.
        model_key: The customer's own model key, for a model-driven agent.
        reset: Called before every run to reset a test app. Tests only.

    The agent is called once, only when the task has no draft and no
    admitted program.
    """

    compiler = FirstRunCompiler(
        prompt,
        params=list(params),
        url=url,
        verify=verify,
        expect=expect,
        on_screen=on_screen,
        task_id=task_id,
        policy=policy,
        cache_dir=cache_dir,
        key_dir=key_dir,
        reset=reset,
        headless=headless,
        on_warning=on_warning,
        model_key=model_key,
    )
    jit = compiler._jit()
    first: Optional[CompileResult] = None
    if not jit.admitted:
        if jit.candidate() is None:
            first = compiler.record(agent, params, hold=hold)
            if first.status != "draft":
                return first
        if not (compiler.out_dir / CONFIRMED_FILE).is_file():
            name = _confirmer(confirm_checks, compiler)
            if name is None:
                return first or compiler._result(
                    "draft", "the proposed checks wait for a person"
                )
            compiler.confirm_checks(principal_id=name, drop_fields=drop_fields)
        admitted = compiler.admit(
            admitter=admitter,
            person=person,
            review=review,
            canary_params=canary_params,
        )
        if first is not None:
            admitted.run1 = first.run1
            admitted.record_check = first.record_check
        if not admitted.admitted:
            return admitted
        result = admitted
    else:
        result = compiler._result(
            "admitted", "already admitted; the agent was not called"
        )
    result.emitted = compiler.emit()
    _write_json(compiler.out_dir / "result.json", result.to_dict())
    return result


def run_compiled(
    task_dir: Union[str, Path],
    *,
    reset: Optional[Callable[[], None]] = None,
    **params: Any,
) -> ReplayResult:
    """Run a compiled task's admitted program with ``params``.

    No model calls. Every save is read back through the verifier, and the run
    gets a verify-run receipt against the person-confirmed checks. A run that
    doesn't end VERIFIED goes to the exception inbox.
    """

    return FirstRunCompiler.load(task_dir, reset=reset).run(**params)
