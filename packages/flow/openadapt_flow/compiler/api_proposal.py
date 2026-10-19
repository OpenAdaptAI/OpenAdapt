"""Propose an API step for a recorded web save (``record --capture-network``).

When a web recording carries ``network.jsonl``, ``compile`` looks for the one
form request that did the save and tries to explain every field it sent:

- ``param``: the value a recorded parameter typed into the form;
- ``select_label``: a select whose option the person chose by its label (the
  value is looked up in the live form each run);
- ``session``: a CSRF or session value the server issued (withheld at
  capture as ``<session:NAME>``; read from the live form each run);
- ``form_default``: the form's own default value.

If any field can't be explained, nothing is proposed and the program stays on
the GUI. Read-only requests that a folded step made, and whose response
carries text the GUI program checks on screen before saving (for example a
duplicate search that must say "No matches were found."), become pre-checks
the API step runs before it sends the save. So the right-record check before
the save and the record check after it both stay.

The proposal is an ``ApiBinding(kind="web_form", on_unavailable="gui")`` on
the save step with no admission. It does nothing until a person admits it
(:func:`admit_api_step`, ``openadapt-flow api admit``), the same rule as a
demonstrated record check. The binding copies the save step's record check
(``effects``); admitting refuses a save that has none.

What is not proposed (the reasons are returned):

- no captured form POST carries the recorded values (a JSON or script-built
  request, a save done over a websocket, a desktop app);
- the form page was not captured (start the recording before opening the
  form);
- the save sends a secret field, or a field the compiler can't explain;
- the save is not the program's last step (steps after it would expect the
  page the save left);
- a folded step checks record identity on a region the person marked
  (``record --identifier``), or has a guard or wait rule;
- a folded step made a read whose response must show text the GUI checks,
  and that text is in no response.

Model-free and deterministic.
"""

from __future__ import annotations

import getpass
import json
import re
import urllib.parse as up
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from openadapt_flow import network_capture as nc
from openadapt_flow.ir import (
    ActionKind,
    ApiAdmission,
    ApiBinding,
    Step,
    WebFormField,
    WebFormPrecheck,
    WebFormSpec,
    Workflow,
)
from openadapt_flow.runtime.actuators.web_form import (
    client_rules,
    folded_checks_digest,
    form_controls,
)


class ApiProposalError(ValueError):
    """The API step can't be admitted (or the proposal input is malformed)."""


def _event_index(step: Step) -> Optional[int]:
    prefix, _, number = step.id.rpartition("_")
    return int(number) if prefix == "step" and number.isdigit() else None


def _events(recording: Path) -> list[dict[str, Any]]:
    path = recording / "events.jsonl"
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _path(url: str) -> str:
    return up.urlsplit(url).path


def _templated(value: str, by_value: dict[str, list[str]]) -> Optional[str]:
    names = by_value.get(value) or []
    return "{" + names[0] + "}" if len(names) == 1 else None


def _success_pattern(line: dict[str, Any]) -> Optional[str]:
    """Where the accepted save sent the browser, with record numbers generalized."""

    target = line.get("location")
    if not target:
        m = re.search(
            r"""(?:window\.)?location(?:\.href)?\s*=\s*['"]([^'"]+)['"]""",
            line.get("response_text") or "",
        )
        target = m.group(1) if m else None
    if not target:
        return None
    path = up.urlsplit(target).path.rsplit("/", 1)[-1]
    query = up.urlsplit(target).query
    pieces = [re.escape(path)]
    for name, value in up.parse_qsl(query, keep_blank_values=True):
        pieces.append(
            re.escape(name) + "=" + (r"\d+" if value.isdigit() else re.escape(value))
        )
    return r".*?".join(pieces)


def propose_api_step(
    workflow: Workflow, recording_dir: Path | str, *, replace: bool = False
) -> tuple[Workflow, Optional[Step], list[str]]:
    """Attach a proposed ``web_form`` binding to the save step, or say why not.

    Returns the workflow (unchanged when nothing is proposed), the save step
    that got the proposal, and the reasons when there is none. ``replace``
    proposes again over an earlier ``web_form`` proposal (its admission is
    dropped), for example after a check was added to a folded step.
    """

    recording = Path(recording_dir)
    lines = nc.load(recording)
    if not lines:
        return workflow, None, ["the recording has no network capture"]
    if workflow.program is not None:
        return (
            workflow,
            None,
            ["the program has branches; API steps cover linear programs only"],
        )
    meta = json.loads((recording / "meta.json").read_text(encoding="utf-8"))
    params = {k: str(v) for k, v in (meta.get("params") or {}).items()}
    if not params:
        return workflow, None, ["the recording has no parameters to map into a request"]
    events = _events(recording)
    by_value: dict[str, list[str]] = {}
    for name, value in params.items():
        if value:
            by_value.setdefault(value, []).append(name)

    steps = list(workflow.steps)
    save = steps[-1]
    save_i = _event_index(save)
    if (
        save_i is None
        or save_i >= len(events)
        or save.action
        not in (ActionKind.CLICK, ActionKind.DOUBLE_CLICK, ActionKind.KEY)
    ):
        return (
            workflow,
            None,
            ["the last step is not a recorded click or key that could be the save"],
        )
    if save.api_binding is not None and not (
        replace and save.api_binding.kind == "web_form"
    ):
        return workflow, None, ["the save step already has an API binding"]
    # The save is the last step, so its request may finish after the
    # recorder stamped the event: take anything after the previous event.
    t_end = float("inf")
    t_prev = float(events[save_i - 1]["t"]) if save_i > 0 else float("-inf")

    def carried(line: dict[str, Any]) -> int:
        return sum(1 for _, v in line.get("form") or [] if v in by_value)

    posts = [
        ln
        for ln in lines
        if ln["method"] == "POST"
        and ln.get("form") is not None
        and t_prev < float(ln["t"]) <= t_end
        and not ln.get("failed")
    ]
    if not posts:
        return workflow, None, ["no form request was sent by the save step"]
    post = max(posts, key=carried)
    if carried(post) < max(1, len(by_value) // 2):
        return workflow, None, ["the save's request does not carry the recorded values"]
    action = _path(post["url"])
    if any(v == nc.SECRET for _, v in post["form"]):
        return workflow, None, ["the save sends a secret field; it stays on the GUI"]

    # The page that served the form: the last captured document with that form.
    form_line, controls = None, None
    for ln in lines:
        if float(ln["t"]) >= float(post["t"]):
            break
        if (
            ln["method"] == "GET"
            and ln.get("status") == 200
            and "<form" in (ln.get("response_text") or "")
        ):
            found = form_controls(ln["response_text"], action)
            if found is not None:
                form_line, controls = ln, found
    if form_line is None or controls is None:
        return (
            workflow,
            None,
            [
                "the page that serves the form is not in the capture; start recording before opening it"
            ],
        )
    form_t = float(form_line["t"])

    fields: list[WebFormField] = []
    for name, value in post["form"]:
        ctl = controls.get(name)
        template = _templated(value, by_value) if value else None
        if template is not None:
            fields.append(WebFormField(name=name, source="param", param=template[1:-1]))
            continue
        if (
            value.startswith("<session:")
            and ctl is not None
            and ctl.get("default") == value
        ):
            fields.append(WebFormField(name=name, source="session"))
            continue
        if value and ctl is not None and ctl.get("options"):
            label = next(
                (o["label"] for o in ctl["options"] if o["value"] == value), None
            )
            names = by_value.get(label or "") or []
            if len(names) == 1:
                fields.append(
                    WebFormField(name=name, source="select_label", param=names[0])
                )
                continue
        if ctl is not None and (ctl.get("default") or "") == value:
            fields.append(WebFormField(name=name, source="form_default", value=value))
            continue
        return (
            workflow,
            None,
            [
                f"field {name!r} of the save is not a recorded value, a form default or a session value"
            ],
        )

    # Steps the API step stands in for: those recorded on the form page.
    folded: list[Step] = []
    for step in steps:
        i = _event_index(step)
        if i is not None and i < len(events) and float(events[i]["t"]) > form_t:
            folded.append(step)
    if not folded or folded[-1] is not save:
        return (
            workflow,
            None,
            ["the save is not part of the steps recorded on the form page"],
        )
    for step in folded:
        if step.guard is not None or step.wait_until is not None:
            return (
                workflow,
                None,
                [f"{step.id} has a guard or wait rule; the API step can't carry it"],
            )
        i = _event_index(step)
        if i is not None and events[i].get("identifier_region"):
            # The person marked a record-identifying region (record
            # --identifier): the GUI checks which record it is on screen.
            return (
                workflow,
                None,
                [f"{step.id} checks record identity on screen; keep the GUI program"],
            )

    # Checks the GUI program makes on screen before saving, carried as reads.
    prechecks: list[WebFormPrecheck] = []
    screen_only: list[str] = []
    for step in folded[:-1]:
        kinds = {getattr(p.kind, "value", p.kind) for p in step.expect}
        texts = [
            p.text
            for p in step.expect
            if getattr(p.kind, "value", p.kind) == "text_present" and p.text
        ]
        i = _event_index(step)
        if i is None or not step.expect:
            continue
        lo = float(events[i - 1]["t"]) if i > 0 else form_t
        # A read the step caused can finish just after its event was stamped.
        hi = float(events[i + 1]["t"]) if i + 1 < len(events) else float("inf")
        reads = [
            ln
            for ln in lines
            if ln["method"] == "GET"
            and lo < float(ln["t"]) <= hi
            and ln is not post
            and float(ln["t"]) < float(post["t"])
        ]
        if not reads or not texts:
            if reads and "text_absent" in kinds:
                return (
                    workflow,
                    None,
                    [
                        f"{step.id} checks that text is absent after a request; the API step can't carry it"
                    ],
                )
            screen_only.append(
                f"{step.id}: " + ", ".join(sorted(str(k) for k in kinds))
            )
            continue  # a screen-only change; nothing the server said
        hit = next(
            (
                ln
                for ln in reads
                if all(t in (ln.get("response_text") or "") for t in texts)
            ),
            None,
        )
        if hit is None:
            return (
                workflow,
                None,
                [
                    f"{step.id} checks {texts!r} on screen and no response it caused carries it"
                ],
            )
        query = []
        for k, v in up.parse_qsl(up.urlsplit(hit["url"]).query, keep_blank_values=True):
            if v.startswith("<session:"):
                return (
                    workflow,
                    None,
                    [f"the check at {step.id} sends a session value in its URL"],
                )
            query.append((k, _templated(v, by_value) or v))
        prechecks.append(
            WebFormPrecheck(
                path=_path(hit["url"]),
                query=query,
                require_text=texts,
                from_step=step.id,
            )
        )

    others = sorted(
        {
            f"{ln['method']} {_path(ln['url'])}"
            for ln in lines
            if ln["method"] not in ("GET", "HEAD", "OPTIONS")
            and ln is not post
            and form_t < float(ln["t"]) < float(post["t"])
        }
    )
    spec = WebFormSpec(
        form_path=_path(form_line["url"])
        + (
            f"?{up.urlsplit(form_line['url']).query}"
            if up.urlsplit(form_line["url"]).query
            else ""
        ),
        fields=fields,
        controls=sorted(controls),
        client_rules=client_rules(form_line["response_text"]),
        prechecks=prechecks,
        success_pattern=_success_pattern(post),
        replaces_steps=[s.id for s in folded],
        other_writes_seen=others,
        screen_only_checks=screen_only,
        folded_checks_digest=folded_checks_digest(folded[:-1]),
    )
    binding = ApiBinding(
        kind="web_form",
        on_unavailable="gui",
        method="POST",
        url_template=action,
        expected_status=[int(post["status"])] if post.get("status") else [],
        timeout_s=30.0,
        effects=list(save.effects),
        web_form=spec,
    )
    new_steps = [
        s.model_copy(update={"api_binding": binding}) if s is save else s for s in steps
    ]
    workflow = workflow.model_copy(update={"steps": new_steps})
    return workflow, new_steps[-1], []


def proposed_api_steps(workflow: Workflow) -> list[Step]:
    return [
        s
        for s in workflow.steps
        if s.api_binding is not None and s.api_binding.kind == "web_form"
    ]


def describe_api_step(step: Step) -> str:
    b = step.api_binding
    assert b is not None and b.web_form is not None
    spec = b.web_form
    counts: dict[str, int] = {}
    for f in spec.fields:
        counts[f.source] = counts.get(f.source, 0) + 1
    lines = [
        f"{step.id}: save with one request, {b.method} {b.url_template}, in place of "
        f"{len(spec.replaces_steps)} GUI steps ({spec.replaces_steps[0]} to {spec.replaces_steps[-1]}).",
        f"  sign-in: the replaying browser's own session; form read again from {spec.form_path}",
        "  fields: " + ", ".join(f"{n} {k}" for k, n in sorted(counts.items())),
        "  run values: "
        + ", ".join(f"{f.name}={{{f.param}}}" for f in spec.fields if f.param),
    ]
    for check in spec.prechecks:
        lines.append(
            f"  before saving: GET {check.path} must show {check.require_text!r} (from {check.from_step})"
        )
    if spec.client_rules:
        lines.append(
            f"  required by the form: {sorted(k for k, r in spec.client_rules.items() if isinstance(r, dict) and r.get('presence'))}"
        )
    lines.append(
        f"  accepted when: HTTP {b.expected_status or '2xx/3xx'}"
        + (
            f" and the response matches {spec.success_pattern!r}"
            if spec.success_pattern
            else ""
        )
    )
    lines.append(
        f"  record check after: {len(b.effects)} effect(s) copied from the save step"
    )
    if spec.screen_only_checks:
        lines.append(f"  screen-only checks, not repeated: {spec.screen_only_checks}")
    if spec.other_writes_seen:
        lines.append(
            f"  NOT repeated (other writes seen during these steps): {spec.other_writes_seen}"
        )
    lines.append(
        "  if the form or a check differs at run time, the GUI steps run instead"
    )
    return "\n".join(lines)


def admit_api_step(
    workflow: Workflow,
    *,
    admitted_by: Optional[str] = None,
    step_id: Optional[str] = None,
    now: Optional[datetime] = None,
) -> tuple[Workflow, list[str]]:
    """Record a person's admission of the proposed API steps.

    Copies the save step's current record check into the binding first (so a
    check added after compile is the one admitted), then binds the admission
    to the exact binding. The caller saves the workflow and asks the person.
    """

    from openadapt_flow.runtime.effects.effect import is_agent_identity

    who = (admitted_by or getpass.getuser() or "").strip()
    if not who:
        raise ApiProposalError("an admission needs the name of the person admitting it")
    if is_agent_identity(who):
        raise ApiProposalError(
            f"{who!r} is not a person; an agent or model can't admit an API step"
        )
    stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    when = stamp.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    admitted: list[str] = []
    steps = []
    for step in workflow.steps:
        b = step.api_binding
        if (
            b is not None
            and b.kind == "web_form"
            and (step_id is None or step.id == step_id)
        ):
            if not step.effects:
                raise ApiProposalError(
                    f"{step.id} has no record check; an API step must be confirmed by one "
                    "(add it, for example with record --verify or check mark, then admit)"
                )
            clean = b.model_copy(
                update={"effects": list(step.effects), "admission": None}
            )
            b = clean.model_copy(
                update={
                    "admission": ApiAdmission(
                        admitted_by=who,
                        admitted_at=when,
                        spec_digest=clean.admission_digest(),
                    )
                }
            )
            step = step.model_copy(update={"api_binding": b})
            admitted.append(step.id)
        steps.append(step)
    if not admitted:
        raise ApiProposalError(
            "no proposed API step to admit" + (f" on {step_id}" if step_id else "")
        )
    return workflow.model_copy(update={"steps": steps}), admitted
