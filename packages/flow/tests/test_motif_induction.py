"""Shadow-mined episodes -> the existing induction -> a CANDIDATE, never a run.

The fixtures under ``tests/fixtures/shadow/`` are real
``openadapt.shadow.episodes/v2`` exports, produced by openadapt-capture's
shadow miner from synthetic streams
(``tests/shadow_support/generate_flow_fixtures.py`` in that repository).
"""

from __future__ import annotations

import ast
import copy
import json
import socket
from pathlib import Path

import pytest
from pydantic import ValidationError

from openadapt_flow.compiler import motif_induction
from openadapt_flow.compiler.motif_induction import (
    CANDIDATE_FILENAME,
    DRAFT_FILENAME,
    PLACEHOLDER_TEMPLATE,
    MotifCandidate,
    ShadowEpisodesExport,
    induce_candidate,
    write_candidate,
)
from openadapt_flow.ir import ActionKind, Workflow
from openadapt_flow.traversal import iter_workflow_steps

FIXTURES = Path(__file__).parent / "fixtures" / "shadow"

# The raw values the synthetic streams typed or clicked. None may survive.
RAW_VALUES = (
    "INV-559017",
    "4471.25",
    "jane.doe@example.com",
    "Jane Doe",
    "123-45-6789",
    "Quarterly report",
    "invoices/INV",
    "ssn=",
)


def _load(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.episodes.json").read_text())


def test_fixtures_carry_no_raw_values():
    for path in sorted(FIXTURES.glob("*.json")):
        text = path.read_text()
        for value in RAW_VALUES:
            assert value not in text, (path.name, value)


def test_fixed_control_task_induces_a_candidate_draft_with_parameters():
    candidate = induce_candidate(_load("report_mail"))
    assert candidate.status == "candidate"
    assert candidate.admitted is False and candidate.runnable is False
    assert candidate.induction_certified is True
    assert candidate.n_episodes == 3
    draft = candidate.draft
    assert draft is not None and draft.program is not None
    assert draft.name.startswith("shadow-draft-")
    assert len(draft.param_specs) == 2
    # Parameter examples are export-local references, never values or digests.
    assert all(v.startswith("<") and ":" in v for v in draft.params.values())


def test_record_next_starts_with_a_full_demonstration():
    candidate = induce_candidate(_load("report_mail"))
    first = candidate.record_next[0]
    assert first.code == "shadow_full_demonstration" and first.blocking
    codes = [r.code for r in candidate.record_next]
    assert codes.count("shadow_confirm_check") == len(candidate.proposed_checks)
    assert "shadow_name_value" in codes


def test_checks_are_proposals_only():
    candidate = induce_candidate(_load("report_mail"))
    kinds = sorted(c.kind for c in candidate.proposed_checks)
    assert kinds == ["file_written", "record_written"]
    for check in candidate.proposed_checks:
        assert check.status == "proposed"
        assert check.admitted is False
        assert "can't approve" in check.note
    # No proposal became an effect contract on any step.
    steps = _all_steps(candidate.draft)
    assert len(steps) >= 8 and all(not s.effects for s in steps)
    send = [s for s in steps if "Send" in s.intent]
    assert send and send[0].risk_review_required


def _all_steps(workflow: Workflow | None):
    return [] if workflow is None else list(iter_workflow_steps(workflow))


def test_write_shaped_steps_need_review_and_no_anchor_is_real():
    export = ShadowEpisodesExport.model_validate(_load("report_mail"))
    workflows, _ = motif_induction.episodes_to_workflows(export)
    send = [s for s in workflows[0].steps if "Send" in s.intent]
    assert send and send[0].risk == "irreversible" and send[0].risk_review_required
    for step in workflows[0].steps:
        if step.anchor is not None:
            assert step.anchor.template == PLACEHOLDER_TEMPLATE
            assert step.anchor.click_point == (0, 0)


def test_varying_selection_is_refused_by_existing_induction():
    candidate = induce_candidate(_load("invoice_exact"))
    assert candidate.induction_certified is False
    assert candidate.draft is None
    codes = [r.code for r in candidate.record_next]
    assert codes[0] == "shadow_full_demonstration"
    assert "ambiguous_selection" in codes
    assert {c.kind for c in candidate.proposed_checks} == {
        "page_reached",
        "record_written",
    }


def test_variant_episode_is_read_as_an_optional_step():
    candidate = induce_candidate(_load("invoice_variant"))
    assert any(
        "optional step" in line for line in candidate.induction_report.splitlines()
    )


def test_candidate_cannot_claim_admission():
    candidate = induce_candidate(_load("report_mail"))
    data = candidate.model_dump()
    for field in ("admitted", "runnable"):
        bad = {**data, field: True}
        with pytest.raises(ValidationError):
            MotifCandidate.model_validate(bad)
    with pytest.raises(ValidationError):
        MotifCandidate.model_validate({**data, "status": "admitted"})


@pytest.mark.parametrize(
    ("mutate", "why"),
    [
        (lambda s: s.update(text="jane.doe@example.com"), "unknown field"),
        (lambda s: s.update(display_label="Pay 1200"), "digits in a plain label"),
        (lambda s: s.update(value_ref="not-a-ref"), "malformed reference"),
        (lambda s: s.update(value_digest="0123456789abcdef"), "a keyed digest field"),
        (lambda s: s.update(label_digest="0123456789abcdef"), "a keyed digest field"),
        (lambda s: s.update(token="click|Mail|button|0a1b2c3d"), "a digest in a token"),
        (lambda s: s.update(kind="screenshot"), "unknown kind"),
    ],
)
def test_export_schema_refuses_raw_content(mutate, why):
    data = copy.deepcopy(_load("report_mail"))
    mutate(data["episodes"][0]["steps"][3])
    with pytest.raises(ValidationError):
        induce_candidate(data)


def test_export_schema_refuses_wrong_schema_and_status():
    data = _load("report_mail")
    with pytest.raises(ValidationError):
        induce_candidate({**data, "schema": "openadapt.shadow.episodes/v3"})
    with pytest.raises(ValidationError):
        induce_candidate({**data, "status": "admitted"})


def test_v1_exports_with_keyed_digests_are_refused():
    data = _load("report_mail")
    with pytest.raises(ValueError, match="keyed digests"):
        induce_candidate({**data, "schema": "openadapt.shadow.episodes/v1"})
    with pytest.raises(ValidationError):
        induce_candidate(
            {**data, "tokens": ["focus|Mail", "click|Mail|button|0a1b2c3d"]}
        )


@pytest.mark.parametrize("name", ["report_mail", "invoice_exact", "invoice_variant"])
def test_draft_carries_no_digest(name):
    import re

    candidate = induce_candidate(_load(name))
    blob = candidate.model_dump_json()
    # No 8+ character hex run except the export's own id.
    runs = set(re.findall(r"(?<![0-9a-z])[0-9a-f]{8,}(?![0-9a-z])", blob))
    assert runs <= {_load(name)["motif_id"]}


def test_write_candidate_never_writes_a_runnable_bundle(tmp_path):
    candidate = induce_candidate(_load("report_mail"))
    out = write_candidate(candidate, tmp_path / "cand")
    assert sorted(p.name for p in out.iterdir()) == [CANDIDATE_FILENAME, DRAFT_FILENAME]
    meta = json.loads((out / CANDIDATE_FILENAME).read_text())
    assert meta["status"] == "candidate" and meta["admitted"] is False
    draft = Workflow.model_validate_json((out / DRAFT_FILENAME).read_text())
    assert draft.program is not None
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "workflow.json").write_text("{}")
    with pytest.raises(FileExistsError):
        write_candidate(candidate, bundle)


def test_refused_candidate_writes_no_draft(tmp_path):
    candidate = induce_candidate(_load("invoice_exact"))
    out = write_candidate(candidate, tmp_path / "cand")
    assert [p.name for p in out.iterdir()] == [CANDIDATE_FILENAME]


def test_deterministic():
    a = induce_candidate(_load("report_mail")).model_dump(mode="json")
    b = induce_candidate(_load("report_mail")).model_dump(mode="json")
    a["draft"].pop("created_at", None)
    b["draft"].pop("created_at", None)
    assert a == b


def test_bridge_imports_no_network_module():
    banned = {
        "socket",
        "ssl",
        "http",
        "urllib",
        "requests",
        "httpx",
        "aiohttp",
        "smtplib",
    }
    tree = ast.parse(Path(motif_induction.__file__).read_text())
    for node in ast.walk(tree):
        names = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module]
        for name in names:
            assert name.split(".")[0] not in banned, name


def test_bridge_makes_zero_network_calls(tmp_path, monkeypatch):
    import httpx

    attempts: list[str] = []

    def refuse(name):
        def _raise(*args, **kwargs):
            attempts.append(name)
            raise AssertionError(f"network access via {name}")

        return _raise

    for name in ("connect", "connect_ex", "sendto"):
        monkeypatch.setattr(socket.socket, name, refuse(f"socket.{name}"))
    monkeypatch.setattr(socket, "create_connection", refuse("create_connection"))
    monkeypatch.setattr(socket, "getaddrinfo", refuse("getaddrinfo"))
    monkeypatch.setattr(httpx.Client, "send", refuse("httpx.Client.send"))
    for name in ("report_mail", "invoice_exact", "invoice_variant"):
        write_candidate(induce_candidate(_load(name)), tmp_path / name)
    assert attempts == []


def test_hotkey_steps_keep_their_chord():
    export = ShadowEpisodesExport.model_validate(_load("report_mail"))
    workflows, _ = motif_induction.episodes_to_workflows(export)
    save = workflows[0].steps[0]
    assert (
        save.action is ActionKind.HOTKEY
        and save.key == "s"
        and save.modifiers == ["meta"]
    )
