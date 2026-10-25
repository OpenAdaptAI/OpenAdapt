"""Generate all four application parts from demonstrations, and verify each.

``openadapt-flow generate-parts build`` reads a folder per demonstration:

- ``params.json``: the values the person typed;
- ``reads/<channel>.before.json`` and ``reads/<channel>.after.json``: each
  candidate channel read just before and just after the save
  (``generate-parts snapshot`` takes them);
- optionally ``source.*``: the document the values came from.

and writes, in the output folder:

==========================  ==================================================
``channel.json``            the admitted read channel and its channel test
``verify.yaml``             the record contract (``openadapt.verify/v1``)
``check_coverage.json``     the fault gate's coverage of that contract
``duplicate_rule.json``     the identity and duplicate rule, with calibration
``inputs.json``             input types, labels and conversions, round trip
``parts.review.md``         every check in plain words, for a person
``parts.manifest.json``     the digest of each part; what an approval binds
==========================  ==================================================

Nothing here is approved. ``generate-parts approve`` records a person's
approval of ``parts.review.md``, bound to the manifest digest. The safety
policy (profiles, approval modes, the switch, which steps are consequential,
the gate order, attribution) is never generated.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from openadapt_flow.generate.channel import (
    ChannelCandidate,
    ChannelReport,
    DemoReads,
    check_channel,
)
from openadapt_flow.generate.contract import (
    ContractError,
    Demonstration,
    GatedContract,
    generate_and_gate,
)

MANIFEST_SCHEMA = "openadapt.generated-parts/v1"
APPROVAL_SCHEMA = "openadapt.generated-parts-approval/v1"
PART_FILES = (
    "channel.json",
    "verify.yaml",
    "check_coverage.json",
    "identity_mapping.json",
    "duplicate_rule.json",
    "inputs.json",
    "parts.review.md",
)


class BuildError(ValueError):
    """The demonstrations can't produce admitted parts. Nothing is written."""


@dataclass
class DemoFolder:
    path: Path
    params: dict[str, str]
    reads: dict[str, tuple[Optional[list[dict]], Optional[list[dict]]]]
    source: Optional[Path] = None


def channel_id(candidate: ChannelCandidate) -> str:
    raw = json.dumps(candidate.section, sort_keys=True, default=str)
    return "ch-" + hashlib.sha256(raw.encode()).hexdigest()[:12]


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text()) if path.is_file() else None


def load_demo(path: Path) -> DemoFolder:
    params = _load_json(path / "params.json")
    if not isinstance(params, dict):
        raise BuildError(f"{path}: params.json is missing or not an object")
    reads: dict[str, tuple[Optional[list[dict]], Optional[list[dict]]]] = {}
    for before in sorted((path / "reads").glob("*.before.json")):
        cid = before.name[: -len(".before.json")]
        after = path / "reads" / f"{cid}.after.json"
        reads[cid] = (_load_json(before), _load_json(after))
    sources = sorted(p for p in path.glob("source.*") if p.is_file())
    return DemoFolder(
        path,
        {k: str(v) for k, v in params.items()},
        reads,
        sources[0] if sources else None,
    )


def snapshot(
    candidates: Sequence[ChannelCandidate],
    params: Mapping[str, object],
    demo: Path,
    label: str,
) -> dict[str, int]:
    """Read every candidate once; write ``reads/<id>.<label>.json``."""

    from openadapt_flow.generate.channel import read_through

    if label not in ("before", "after"):
        raise BuildError("label must be before or after")
    out = demo / "reads"
    out.mkdir(parents=True, exist_ok=True)
    counts = {}
    for cand in candidates:
        cid = channel_id(cand)
        records = read_through(cand.section, params)
        (out / f"{cid}.{label}.json").write_text(
            json.dumps(records, default=str, indent=1)
        )
        counts[cid] = -1 if records is None else len(records)
    return counts


@dataclass
class BuildResult:
    out_dir: Path
    channel: Optional[ChannelReport]
    channel_reports: list[ChannelReport]
    gated: Optional[GatedContract]
    rule: Any
    inputs: list[Any]
    input_round_trip: list[Any]
    manifest: dict[str, Any]
    seconds: dict[str, float] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)
    adoption: Any = None
    identity: Optional[dict[str, Any]] = None

    @property
    def admitted(self) -> bool:
        from openadapt_flow.generate.inputs import admitted as inputs_ok

        return (
            not self.problems
            and self.channel is not None
            and self.channel.admitted
            and self.gated is not None
            and self.gated.admitted
            and (not self.inputs or inputs_ok(self.input_round_trip))
        )


def _replica(
    section: Mapping[str, Any], before: list[dict], target: dict, key: str
) -> Any:
    """A test copy shaped like the channel's records, when no copy of the app
    is given: it tests the contract's judgment, not the app's read."""

    from openadapt_flow.fault_gate import MemoryRestCopy

    copy = MemoryRestCopy(
        before, {k: v for k, v in target.items() if k != key}, key_field=key
    )
    copy.name = "replica"  # type: ignore[misc]
    return copy


def build(
    demo_dirs: Sequence[Path],
    out_dir: Path,
    candidates: Sequence[ChannelCandidate],
    *,
    test_copy: Any = None,
    name: str = "record",
    timeout_s: float = 5.0,
    verify_tls: bool = True,
    expected_records: Optional[int] = None,
    reference_duplicate_check: Any = None,
    false_stop_budget: float = 0.02,
    reference_identity: Any = None,
) -> BuildResult:
    """Generate and verify every part. ``test_copy``: a fault-gate copy of the
    app (``openadapt_flow.fault_gate.TestCopy``); without one, a replica.
    ``reference_duplicate_check`` (``(new, table) -> bool``, True: stop) is the
    system's hand-built duplicate check: the generated rule replaces it only
    if it misses no more on the calibration set
    (:func:`openadapt_flow.generate.identity.adopt`)."""

    from openadapt_flow.generate.identity import generate_rule
    from openadapt_flow.generate.inputs import Sample, generate_inputs, round_trip

    demos = [load_demo(Path(d)) for d in demo_dirs]
    if len(demos) < 2:
        raise BuildError("at least two demonstrations are needed")
    out_dir.mkdir(parents=True, exist_ok=True)
    seconds: dict[str, float] = {}
    problems: list[str] = []

    # 1. The read channel: the first candidate that passes the channel test.
    started = time.perf_counter()
    reports: list[ChannelReport] = []
    chosen: Optional[ChannelReport] = None
    for cand in candidates:
        cid = channel_id(cand)
        reads = [DemoReads(d.params, *d.reads.get(cid, (None, None))) for d in demos]
        report = check_channel(cand, reads, demos[-1].params, verify_tls=verify_tls)
        reports.append(report)
        if report.admitted and chosen is None:
            chosen = report
    seconds["channel"] = round(time.perf_counter() - started, 2)
    (out_dir / "channel.json").write_text(
        json.dumps(
            {
                "admitted": chosen.as_dict() if chosen else None,
                "section": chosen.candidate.section if chosen else None,
                "candidates": [r.as_dict() for r in reports],
            },
            indent=2,
            default=str,
        )
        + "\n"
    )
    if chosen is None:
        problems.append("no candidate read channel passed the channel test")
        return BuildResult(
            out_dir, None, reports, None, None, [], [], {}, seconds, problems
        )

    # 2. The record contract, refined on a test copy by the fault gate.
    started = time.perf_counter()
    cid = channel_id(chosen.candidate)
    demonstrations = [
        Demonstration(
            d.params, list(d.reads[cid][0] or []), list(d.reads[cid][1] or [])
        )
        for d in demos
    ]
    gated: Optional[GatedContract] = None
    try:
        from openadapt_flow.generate.contract import _delta, _target, identity_field_of

        key = identity_field_of(demonstrations)
        last = demonstrations[-1]
        target, _how = _target(last, _delta(last, key), key)
        copy_ = test_copy or _replica(
            chosen.candidate.section, last.before, target, key
        )
        channel_for_gate = (
            chosen.candidate.section
            if test_copy is not None
            else {
                "kind": "rest",
                "base_url": "http://127.0.0.1:1",
                "records_path": "/api/db",
            }
        )
        gated = generate_and_gate(
            demonstrations,
            channel_for_gate,
            copy_,
            last.params,
            name=name,
            timeout_s=timeout_s,
        )
        if test_copy is None:
            # The admitted contract reads the real channel; the replica only
            # stood in for the store while faults were written.
            gated.contract.verify["verifier"] = dict(chosen.candidate.section)
            close = getattr(copy_, "close", None)
            if close:
                close()
    except ContractError as exc:
        problems.append(f"record contract: {exc}")
    seconds["contract"] = round(time.perf_counter() - started, 2)
    if gated is not None:
        import yaml

        (out_dir / "verify.yaml").write_text(
            yaml.safe_dump(gated.contract.verify, sort_keys=False)
        )
        if gated.coverage is not None:
            (out_dir / "check_coverage.json").write_text(
                json.dumps(
                    gated.coverage.model_dump(mode="json"), indent=2, sort_keys=True
                )
                + "\n"
            )

    # 3. The identity and duplicate rule, calibrated on the records already there.
    started = time.perf_counter()
    profile = list(demonstrations[0].before)
    from openadapt_flow.generate.identity import adopt

    rule = generate_rule(
        profile, expected_records=expected_records, false_stop_budget=false_stop_budget
    )
    adoption = adopt(rule, reference_duplicate_check)
    if gated is not None:
        param_of = gated.contract.bound()
        new_roles = {
            role: param_of[fld] for role, fld in rule.roles.items() if fld in param_of
        }
        rule.notes.append(
            "a new record's roles come from the task: "
            + ", ".join(f"{r} = {p}" for r, p in sorted(new_roles.items()))
        )
        rule_doc = {**rule.as_dict(), "new_record_params": new_roles}
    else:
        rule_doc = rule.as_dict()
    rule_doc["adoption"] = {
        "use": adoption.use,
        "why": adoption.why,
        "calibration_generated": adoption.generated,
        "calibration_reference": adoption.reference,
    }
    (out_dir / "duplicate_rule.json").write_text(
        json.dumps(rule_doc, indent=2, default=str) + "\n"
    )
    identity = _identity_mapping(
        demonstrations, gated, list(demonstrations[-1].after), reference_identity
    )
    (out_dir / "identity_mapping.json").write_text(
        json.dumps(identity, indent=2, default=str) + "\n"
    )
    seconds["identity"] = round(time.perf_counter() - started, 2)

    # 4. Input types and conversions, from the source documents.
    started = time.perf_counter()
    samples = []
    for d in demos:
        if d.source is None:
            continue
        from openadapt_flow.intake.document import read_document

        samples.append(Sample(read_document(d.source).lines, dict(d.params)))
    inputs = generate_inputs(samples) if len(samples) == len(demos) else []
    trips = round_trip(inputs, samples) if inputs else []
    (out_dir / "inputs.json").write_text(
        json.dumps(
            {
                "parts": [p.as_dict() for p in inputs],
                "round_trip": [t.__dict__ for t in trips],
                "note": None
                if inputs
                else "no source document for every demonstration",
            },
            indent=2,
        )
        + "\n"
    )
    seconds["inputs"] = round(time.perf_counter() - started, 2)

    result = BuildResult(
        out_dir, chosen, reports, gated, rule, inputs, trips, {}, seconds, problems
    )
    result.adoption = adoption
    result.identity = identity
    (out_dir / "parts.review.md").write_text(render_review(result, name))
    result.manifest = write_manifest(out_dir)
    return result


# -- review and approval ---------------------------------------------------------


def _identity_mapping(
    demonstrations: Sequence[Demonstration],
    gated: Optional[GatedContract],
    rows: list[dict[str, Any]],
    reference: Any,
) -> dict[str, Any]:
    """The template plus the generated per-site mapping, verified.

    ``reference(rows) -> template records`` is the site's hand-written source
    adapter, when there is one; the generated mapping is used only if it does
    no worse on the perturbation suite.
    """

    from openadapt_flow.generate.contract import _delta, _target, identity_field_of
    from openadapt_flow.generate.identity_template import (
        WANT_TO_RECORD,
        Template,
        TemplateUnavailable,
        adopt_mapping,
        check_demos,
        generate_mapping,
        perturbation_suite,
        run_suite,
    )

    key = identity_field_of(demonstrations)
    demos = [(d.params, _target(d, _delta(d, key), key)[0]) for d in demonstrations]
    mapping = generate_mapping(
        rows, demos, bound=gated.contract.bound() if gated is not None else None
    )
    out: dict[str, Any] = {
        "mapping": mapping.as_dict(),
        "demos_agree": check_demos(mapping, demos),
        "demos": len(demos),
    }
    try:
        template = Template(mapping.template)
    except TemplateUnavailable as exc:
        out["suite"] = None
        out["note"] = f"{exc}: the perturbation suite was not run"
        return out

    def person(row: Mapping[str, Any]) -> dict[str, str]:
        slots = mapping.record_slots(row)
        who = {w: slots[r] for w, r in WANT_TO_RECORD.items() if slots.get(r)}
        if slots.get("prior_lname"):
            who["prior_lname"] = slots["prior_lname"]
        return who

    from openadapt_flow.generate.name_variants import load

    nicknames = {k: sorted(v) for k, v in load().items()}
    cases = perturbation_suite(rows, person, nicknames=nicknames)

    def decide(want: Mapping[str, str], records: Sequence[Any]) -> str:
        return template.decide_records(
            {k: v for k, v in want.items() if k != "prior_lname"}, records
        )

    out["suite"] = run_suite(
        cases, [template.record(mapping.record_slots(r)) for r in rows], decide
    )
    ref = run_suite(cases, reference(rows), decide) if reference is not None else None
    adoption = adopt_mapping(out["suite"], ref)
    out["suite_reference"] = ref
    out["adoption"] = {"use": adoption.use, "why": adoption.why}
    return out


def render_review(result: BuildResult, name: str) -> str:
    """Every generated check as one plain sentence, and what it can't catch."""

    lines = [f"# Generated parts for the {name} save", ""]
    lines.append(
        "Approve these sentences, not the files. Nothing here runs until you do."
    )
    lines.append("")
    lines.append("## How the system of record is read")
    if result.channel is not None:
        section = result.channel.candidate.section
        lines.append(
            f"- Through {result.channel.candidate.origin} ({section.get('kind')})."
        )
        lines.append(
            "- It showed each demonstration's record, read every page, and read as "
            "unreadable when the credentials, a page or the response were broken "
            f"({', '.join(sorted(result.channel.probes))})."
        )
    else:
        lines.append("- No channel passed the channel test. Nothing below can be used.")
    lines.append("")
    lines.append("## What the save must do, and must not do")
    if result.gated is not None:
        c = result.gated.contract
        match = ", ".join(c.match)
        lines.append(f"- Exactly one new {name} record appears, found by {match}.")
        for role in c.roles:
            if role.role == "param" and role.field not in c.match:
                lines.append(f"- Its {role.field} is the {role.param} the task names.")
            elif role.role == "literal":
                lines.append(
                    f"- Its {role.field} is {role.literal!r} (the same in every demonstration)."
                )
        lines.append(
            f"- No other {name} record is added, removed or changed"
            + (
                f" (except {', '.join(c.volatile)}, which changed on every record by itself)"
                if c.volatile
                else ""
            )
            + "."
        )
        for note in c.notes:
            lines.append(f"- Not checked: {note}.")
        cov = result.gated.coverage
        if cov is not None:
            survived = [
                k
                for k, v in cov.classes.items()
                if v.applicable and v.killed < v.applicable
            ]
            lines.append(
                f"- Shown to catch {cov.killed} of {cov.applicable} bad saves written to a "
                f"{cov.test_copy} test copy"
                + (f"; it misses: {', '.join(survived)}" if survived else "")
                + "."
            )
        for item in result.gated.unresolved:
            lines.append(f"- Unresolved: {item}.")
    lines.append("")
    lines.append("## When a new record is already there")
    identity = result.identity
    if identity is not None:
        mapping = identity["mapping"]
        lines.append(
            f"- The reviewed template {mapping['template']} decides; this system's "
            "fields feed it as follows:"
        )
        for slot, path in sorted(mapping["record"].items()):
            lines.append(f"  - {slot} from {path!r}")
        if mapping.get("dob_format"):
            lines.append(f"  - dates of birth are stored {mapping['dob_format']}")
        agree = identity["demos_agree"]
        lines.append(
            f"- Every demonstration's saved record read back as typed for "
            f"{sum(1 for n in agree.values() if n == identity['demos'])} of "
            f"{len(agree)} inputs."
        )
        suite = identity.get("suite")
        if suite:
            lines.append(
                f"- On {suite['duplicates']} made-up duplicates of this system's records "
                f"it missed {suite['missed']}; it stopped {suite['false_stops']} of "
                f"{suite['distinct']} existing records posed as new."
            )
        if identity.get("adoption"):
            lines.append(
                f"- Mapping: {identity['adoption']['use']} ({identity['adoption']['why']})."
            )
        if identity.get("note"):
            lines.append(f"- {identity['note']}.")
        for note in mapping.get("notes") or []:
            lines.append(f"- {note}.")
        lines.append("")
        lines.append(
            "A rule calibrated on this system's records alone, for comparison:"
        )
    adoption = result.adoption
    if adoption is not None and adoption.use == "reference":
        lines.append(f"- The hand-built duplicate check stays: {adoption.why}.")
        lines.append("- The generated rule below is shown for comparison only.")
    elif adoption is not None and adoption.reference is not None:
        lines.append(
            f"- The generated rule replaces the hand-built check: {adoption.why}."
        )
    if result.rule is not None:
        lines += [f"- {text}" for text in result.rule.describe()]
        cal = result.rule.calibration
        if cal is not None:
            rates = cal.rates()
            lines.append(
                f"- On this system's own {rates['distinct_queries']} pairs of existing "
                f"records it fired {cal.false_matches} times; on "
                f"{rates['calibration_duplicates']} made-up duplicates it missed "
                f"{round(100 * rates['miss_rate'])}%."
            )
            missed = [k for k, v in rates["miss_by_error"].items() if v >= 0.5]
            if missed:
                lines.append(f"- It mostly misses: {', '.join(missed)}.")
    lines.append("")
    lines.append("## Where the typed values come from")
    if result.inputs:
        lines += [f"- {p.describe()}" for p in result.inputs]
    else:
        lines.append(
            "- No source documents were given; the task's values are checked as typed."
        )
    lines.append("")
    lines.append("## Never generated")
    lines.append(
        "- The execution profile, approval modes, the go-live switch, which steps are "
        "consequential, the order of the gates, and attribution of changes to other users."
    )
    return "\n".join(lines) + "\n"


def _sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def write_manifest(out_dir: Path) -> dict[str, Any]:
    parts = {n: _sha256(out_dir / n) for n in PART_FILES if (out_dir / n).is_file()}
    body = {"schema_version": MANIFEST_SCHEMA, "parts": parts}
    digest = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )
    manifest = {**body, "manifest_sha256": digest}
    (out_dir / "parts.manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def verify_manifest(out_dir: Path) -> Optional[str]:
    """None when every part still matches the manifest."""

    manifest = _load_json(out_dir / "parts.manifest.json")
    if not isinstance(manifest, dict):
        return "no manifest"
    for name, digest in (manifest.get("parts") or {}).items():
        path = out_dir / name
        if not path.is_file() or _sha256(path) != digest:
            return f"{name} changed after it was generated"
    return None


def approve(
    out_dir: Path, approved_by: str, *, at: Optional[str] = None
) -> dict[str, Any]:
    """Record a person's approval of the review text, bound to the manifest."""

    from datetime import datetime, timezone

    from openadapt_flow.runtime.effects.effect import is_agent_identity

    if is_agent_identity(approved_by):
        raise BuildError("only a person can approve generated parts")
    problem = verify_manifest(out_dir)
    if problem:
        raise BuildError(f"refused: {problem}")
    manifest = _load_json(out_dir / "parts.manifest.json")
    approval = {
        "schema_version": APPROVAL_SCHEMA,
        "approved_by": approved_by,
        "role": "person",
        "approved_at": at
        or datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "manifest_sha256": manifest["manifest_sha256"],
        "review_sha256": manifest["parts"].get("parts.review.md"),
    }
    (out_dir / "parts.approval.json").write_text(json.dumps(approval, indent=2) + "\n")
    return approval


def approved(out_dir: Path) -> bool:
    """True when a person approved exactly the parts that are there now."""

    approval = _load_json(out_dir / "parts.approval.json")
    manifest = _load_json(out_dir / "parts.manifest.json")
    from openadapt_flow.runtime.effects.effect import is_agent_identity

    return (
        isinstance(approval, dict)
        and isinstance(manifest, dict)
        and verify_manifest(out_dir) is None
        and approval.get("role") == "person"
        and not is_agent_identity(approval.get("approved_by"))
        and approval.get("manifest_sha256") == manifest.get("manifest_sha256")
    )
