"""Skill emission: wraps flow's emitter, appends MCP + halt guidance."""

from __future__ import annotations

from pathlib import Path

from openadapt_agent.skill import emit_agent_skill

GOLDEN = Path(__file__).parent / "golden" / "skill_appendix.md"


def test_emit_skill_wraps_flow_and_appends_guidance(bundle_dir, tmp_path):
    skill_dir = emit_agent_skill(bundle_dir, tmp_path / "skills")
    assert skill_dir.name == "demo-triage"  # flow's slug convention
    text = (skill_dir / "SKILL.md").read_text()

    # flow-emitted content is intact (frontmatter + CLI usage).
    assert text.startswith("---\nname: demo-triage\n")
    assert "openadapt-flow replay bundle" in text
    # Bundle copied alongside (flow's portability contract).
    assert (skill_dir / "bundle" / "workflow.json").is_file()

    # Our appendix is present, exactly as the golden file specifies.
    golden = GOLDEN.read_text()
    assert text.endswith(golden)


def test_appendix_names_the_mcp_tool_and_params(bundle_dir, tmp_path):
    skill_dir = emit_agent_skill(bundle_dir, tmp_path / "skills")
    text = (skill_dir / "SKILL.md").read_text()
    assert "run_demo_triage" in text
    assert "`note`" in text
    # Honesty requirements: halt is never success; refusal executes nothing.
    assert "NOT a success" in text
    assert "NOTHING was executed" in text
    assert "Experimental" in text
