"""Agent Skill export keeps recorded values, step text, and secrets out."""

from __future__ import annotations

import json
from pathlib import Path

from openadapt_agent.cards import CARD_FILENAME
from openadapt_agent.cli import main
from openadapt_agent.copy import OUTCOME_RULES
from openadapt_agent.skill import emit_agent_skill

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
