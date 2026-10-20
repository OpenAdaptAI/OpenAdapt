"""Propose identity evidence for clicks a bundle runs without a right-record check.

docs/LIMITS.md: identity protection applies only where it is armed, and a live
OpenEMR bundle armed 4 to 7 of its 12 clicks. ``scaffold-verifier --bundle``
reports identity coverage before and after evidence taken from the recording's
DOM / accessibility context near each unarmed click.

Evidence is proposed only where the shipped runtime checks it on the bundle's
surface: the structured-text tier, which compares the recorded text with the
live DOM / accessibility text at the click point. A record named elsewhere on
the page (the title, the URL) is listed but not counted, because the runtime
does not check identity there. Nothing is applied to the bundle; a person
reviews ``identity.proposed.json`` and re-compiles.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Mapping, Optional

from openadapt_flow import sealed_set as _sealed
from openadapt_flow.record_check_synthesis import param_lookup

#: Accessibility roles whose accessible name is the record row itself; the
#: browser and desktop backends read it as the row's structured identity.
ROW_ROLES = frozenset({"row", "listitem", "option", "treeitem"})

IDENTITY_PROPOSED_NAME = "identity.proposed.json"


def _events(recording_dir: Path) -> list[dict[str, Any]]:
    path = Path(recording_dir) / "events.jsonl"
    if not _sealed.is_file(path):
        return []
    out: list[dict[str, Any]] = []
    for line in _sealed.read_text(path).splitlines():
        try:
            out.append(json.loads(line) if line.strip() else {})
        except ValueError:
            out.append({})
    return out


def identity_coverage(
    bundle_dir: Path,
    recording_dir: Path,
    params: Mapping[str, str],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Identity-armed clicks now, and with evidence from the recording's
    DOM / accessibility context.

    Returns ``(report, proposals)``. The report holds counts and parameter
    names only; ``proposals`` hold the evidence text for the private
    ``identity.proposed.json``. Evidence is proposed only where the shipped
    runtime checks it on this bundle's surface (the structured-text tier, at
    the click point); context it can't check is listed, not counted.
    """

    from openadapt_flow.ir import Workflow
    from openadapt_flow.policy import is_identity_applicable, is_identity_armed
    from openadapt_flow.runtime.identity import verifiable_identity_evidence

    workflow = Workflow.load(Path(bundle_dir))
    surface = getattr(workflow.surface, "value", workflow.surface)
    checkable = "structured_text" in verifiable_identity_evidence(surface)
    events = _events(recording_dir)
    lookup, _ = param_lookup(params)

    applicable = [s for s in workflow.steps if is_identity_applicable(s)]
    armed = [s for s in applicable if is_identity_armed(s)]
    proposals: list[dict[str, Any]] = []
    unchecked: list[dict[str, Any]] = []
    still_unarmed: list[dict[str, Any]] = []
    for step in applicable:
        if is_identity_armed(step):
            continue
        match = re.fullmatch(r"step_(\d+)", step.id)
        event = (
            events[int(match.group(1))]
            if match and int(match.group(1)) < len(events)
            else {}
        )
        text: Optional[str] = None
        basis = ""
        recorded = event.get("structured_identity")
        if isinstance(recorded, str) and recorded.strip():
            text, basis = (
                recorded.strip(),
                "DOM / accessibility text of the clicked row",
            )
        raw_structural = event.get("structural")
        structural: dict[str, Any] = (
            raw_structural if isinstance(raw_structural, dict) else {}
        )
        name = str(structural.get("name") or "").strip()
        role = str(structural.get("role") or "").strip().lower()
        if text is None and name and role in ROW_ROLES:
            text, basis = name, f"accessible name of the clicked {role}"
        names_params = sorted(
            {p for value, p in lookup.items() if text and value in text}
        )
        if text is not None and checkable:
            proposals.append(
                {
                    "step_id": step.id,
                    "anchor_field": "structured_identity",
                    "value": text,
                    "basis": basis,
                    "contains_params": names_params,
                }
            )
            continue
        context = [
            key
            for key in ("title_before", "url_before")
            if isinstance(event.get(key), str)
            and any(value in event[key] for value in lookup)
        ]
        if text is not None or context:
            unchecked.append(
                {
                    "step_id": step.id,
                    "seen": basis or ", ".join(context),
                    "why_not_proposed": (
                        "this surface has no structured text at the click"
                        if text is not None
                        else "the record is named on the page, not at the click; "
                        "the runtime checks identity at the click point"
                    ),
                }
            )
        still_unarmed.append(
            {
                "step_id": step.id,
                "reason": step.identity_unarmed_reason or "no evidence",
            }
        )
    report = {
        "surface": surface or "web",
        "applicable": len(applicable),
        "armed_before": len(armed),
        "armed_after": len(armed) + len(proposals),
        "proposed": [{k: v for k, v in p.items() if k != "value"} for p in proposals],
        "seen_not_checkable": unchecked,
        "still_unarmed": still_unarmed,
        "approved": False,
    }
    return report, proposals


def write_identity_proposals(
    proposals: list[dict[str, Any]], out_dir: Path
) -> Optional[Path]:
    """Write the proposed evidence (it holds record text) with mode 0600."""

    import os

    if not proposals:
        return None
    path = out_dir / IDENTITY_PROPOSED_NAME
    if _sealed.is_sealed_path(path):
        _sealed.write_text(
            path,
            json.dumps(
                {
                    "schema": "openadapt.identity-proposal/v1",
                    "approved": False,
                    "proposals": proposals,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
        )
        return path
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(
            {
                "schema": "openadapt.identity-proposal/v1",
                "approved": False,
                "proposals": proposals,
            },
            fh,
            indent=2,
            sort_keys=True,
        )
        fh.write("\n")
    os.chmod(path, 0o600)
    return path
