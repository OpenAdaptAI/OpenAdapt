"""Agent Skill export keeps recorded values, step text, and secrets out."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from openadapt_agent.bundles import load_workflow_info
from openadapt_agent.cards import CARD_FILENAME, card_for_bundle
from openadapt_agent.cli import main
from openadapt_agent.copy import OUTCOME_RULES, SKILL_NAME
from openadapt_agent.skill import emit_agent_skill, render_skill

REPO_ROOT = Path(__file__).resolve().parents[1]

SECRET_VALUES = ("Follow-up in 2 weeks", "Open the patient chart", "Type the triage note")


def frontmatter(text: str) -> dict[str, str]:
    block = text.split("---", 2)[1]
    fields = {}
    for line in block.strip().splitlines():
        key, value = line.split(":", 1)
        fields[key.strip()] = value.strip()
    return fields


def test_skill_has_no_recorded_values_intents_or_bundle(bundle_dir, tmp_path):
    skill_dir = emit_agent_skill(bundle_dir, tmp_path / "skills")
    text = (skill_dir / "SKILL.md").read_text()
    for value in SECRET_VALUES:
        assert value not in text
    assert "Demo Triage" not in text
    assert "replay" not in text
    assert str(bundle_dir) not in text
    assert not (skill_dir / "bundle").exists()
    assert "`note`" in text
    assert "run_workflow" in text
    assert "not_sure_if_saved" in text
    assert OUTCOME_RULES in text
    assert "name: computer-use" not in text.lower()


def test_typed_password_step_never_reaches_the_skill(tmp_path):
    from openadapt_flow.ir import ActionKind, Step, Workflow

    bundle = tmp_path / "bundles" / "login"
    Workflow(
        name="Login and note",
        params={"note": "Recorded note"},
        steps=[
            Step(id="s1", intent="type 'mockmed-demo-pass'", action=ActionKind.TYPE,
                 text="mockmed-demo-pass"),
            Step(id="s2", intent="type the note", action=ActionKind.TYPE,
                 text="Recorded note", param="note"),
        ],
    ).save(bundle)
    text = (emit_agent_skill(bundle, tmp_path / "skills") / "SKILL.md").read_text()
    assert "mockmed-demo-pass" not in text
    assert "Recorded note" not in text


def test_each_skill_gets_its_own_description(tmp_path):
    from openadapt_flow.ir import ActionKind, Step, Workflow

    descriptions = set()
    for name, param in (("one", "note"), ("two", "referral_reason")):
        bundle = tmp_path / "bundles" / name
        Workflow(
            name=name,
            params={param: "x"},
            steps=[Step(id="s1", intent="click", action=ActionKind.CLICK)],
        ).save(bundle)
        skill = emit_agent_skill(bundle, tmp_path / "skills")
        descriptions.add(frontmatter((skill / "SKILL.md").read_text())["description"])
    assert len(descriptions) == 2


def test_card_names_and_describes_the_skill(bundle_dir, tmp_path):
    (bundle_dir / CARD_FILENAME).write_text(
        json.dumps(
            {
                "name": "add_triage_note",
                "purpose": "Add a triage note to a patient's chart.",
                "done_means": "The note shows on the chart's encounter list.",
                "inputs": {"note": {"description": "The note text.", "maxLength": 500}},
            }
        )
    )
    skill_dir = emit_agent_skill(bundle_dir, tmp_path / "skills")
    assert skill_dir.name == "add-triage-note"
    text = (skill_dir / "SKILL.md").read_text()
    fields = frontmatter(text)
    assert fields["name"] == "add-triage-note"
    assert "Add a triage note" in fields["description"]
    assert "up to 500 characters" in text
    assert "The note shows on the chart's encounter list." in text


def test_include_bundle_is_explicit_and_labelled(bundle_dir, tmp_path):
    skill_dir = emit_agent_skill(bundle_dir, tmp_path / "skills", include_bundle=True)
    assert (skill_dir / "bundle" / "workflow.json").is_file()
    assert "protected workflow data" in (skill_dir / "SKILL.md").read_text()


def test_cli_emit_skill(bundle_dir, tmp_path, capsys):
    assert main(["emit-skill", str(bundle_dir), "--out", str(tmp_path / "out")]) == 0
    assert "Wrote Agent Skill folder" in capsys.readouterr().out
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "workflow.json").write_text("{not json")
    assert main(["emit-skill", str(broken), "--out", str(tmp_path / "out")]) == 2


def test_skill_folder_is_valid_markdown_with_frontmatter(bundle_dir, tmp_path):
    text = (emit_agent_skill(bundle_dir, tmp_path / "s") / "SKILL.md").read_text()
    assert text.startswith("---\nname: ")
    fields = frontmatter(text)
    assert len(fields["name"]) <= 64
    assert len(json.loads(fields["description"])) <= 1024
    assert Path(tmp_path / "s").is_dir()


def _name_card(bundle_dir, name):
    (bundle_dir / CARD_FILENAME).write_text(
        json.dumps({"name": name, "purpose": "Add a triage note to a chart."})
    )


@pytest.mark.parametrize("reserved", ["computer_use", "openadapt_gui_write"])
def test_reserved_names_fall_back_to_the_workflow_id(bundle_dir, tmp_path, reserved):
    _name_card(bundle_dir, reserved)
    workflow_id = load_workflow_info(bundle_dir).public_id
    skill_dir = emit_agent_skill(bundle_dir, tmp_path / "skills")
    expected = "openadapt-" + workflow_id.replace("_", "-")
    assert skill_dir.name == expected
    text = (skill_dir / "SKILL.md").read_text()
    assert frontmatter(text)["name"] == expected
    assert "name: computer-use" not in text.lower()
    assert f"name: {SKILL_NAME}\n" not in text


def test_emitting_never_replaces_the_shipped_general_skill(bundle_dir, tmp_path):
    shipped = (REPO_ROOT / "skills" / SKILL_NAME / "SKILL.md").read_text()
    skills = tmp_path / "skills"
    (skills / SKILL_NAME).mkdir(parents=True)
    (skills / SKILL_NAME / "SKILL.md").write_text(shipped)
    for name in ("computer_use", "openadapt_gui_write"):
        _name_card(bundle_dir, name)
        skill_dir = emit_agent_skill(bundle_dir, skills, include_bundle=True)
        assert skill_dir.name != SKILL_NAME
    assert (skills / SKILL_NAME / "SKILL.md").read_text() == shipped
    assert not (skills / SKILL_NAME / "bundle").exists()


def test_render_skill_needs_a_workflow_id_for_a_reserved_name(bundle_dir):
    _name_card(bundle_dir, "computer_use")
    info = load_workflow_info(bundle_dir)
    card = card_for_bundle(info, default_name=info.public_id)
    with pytest.raises(ValueError):
        render_skill(card)
    assert "name: openadapt-workflow-" in render_skill(card, workflow_id=info.public_id)
