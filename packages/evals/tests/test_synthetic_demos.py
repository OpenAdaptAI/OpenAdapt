"""Test synthetic demo compatibility with ApiAgent."""

import json
from pathlib import Path

import pytest

from openadapt_evals.agents.api_agent import ApiAgent


def test_synthetic_demos_exist():
    """Test that all 154 synthetic demos have been generated."""
    demo_dir = Path(__file__).parent.parent / "demo_library" / "synthetic_demos"
    demo_files = list(demo_dir.glob("*.txt"))

    assert len(demo_files) == 154, f"Expected 154 demos, found {len(demo_files)}"


def test_synthetic_demos_index():
    """Test that the demo index is complete and valid."""
    index_path = Path(__file__).parent.parent / "demo_library" / "synthetic_demos" / "demos.json"

    assert index_path.exists(), "Demo index (demos.json) not found"

    with open(index_path) as f:
        index = json.load(f)

    assert index["version"] == "2.0.0"
    assert index["total_demos"] == 154
    assert len(index["demos"]) == 154

    # Check all domains are represented
    domains = {demo["domain"] for demo in index["demos"]}
    expected_domains = {
        "notepad", "browser", "office", "coding", "media",
        "paint", "file_explorer", "clock", "settings", "edge", "vscode"
    }
    assert domains == expected_domains


def test_demo_format():
    """Test that demos follow the expected format."""
    demo_dir = Path(__file__).parent.parent / "demo_library" / "synthetic_demos"
    demo_file = demo_dir / "notepad_1.txt"

    assert demo_file.exists(), "Sample demo file notepad_1.txt not found"

    content = demo_file.read_text()

    # Check required sections
    assert "TASK:" in content
    assert "DOMAIN:" in content
    assert "STEPS:" in content
    assert "EXPECTED_OUTCOME:" in content or "DONE()" in content

    # Check for numbered steps
    assert "1." in content
    assert "REASONING:" in content
    assert "ACTION:" in content


def test_demo_with_api_agent():
    """Test that synthetic demos can be loaded by ApiAgent."""
    demo_dir = Path(__file__).parent.parent / "demo_library" / "synthetic_demos"
    demo_file = demo_dir / "notepad_1.txt"

    # Load demo
    demo_text = demo_file.read_text()

    # Create agent with demo (no API key needed for this test)
    # We're just testing that the agent accepts the demo format
    try:
        agent = ApiAgent(
            provider="anthropic",
            demo=demo_text,
            api_key="test-key"  # Dummy key for format testing
        )

        # Verify demo was stored
        assert agent.demo == demo_text

    except Exception as e:
        pytest.fail(f"Failed to create ApiAgent with synthetic demo: {e}")


def test_demo_loading_all_domains():
    """Test that demos from all domains can be loaded."""
    demo_dir = Path(__file__).parent.parent / "demo_library" / "synthetic_demos"

    # Test one demo from each domain
    test_demos = {
        "notepad": "notepad_1.txt",
        "browser": "browser_1.txt",
        "office": "office_1.txt",
        "coding": "coding_1.txt",
        "media": "media_1.txt",
        "paint": "paint_1.txt",
        "file_explorer": "file_explorer_1.txt",
        "clock": "clock_1.txt",
        "settings": "settings_1.txt",
        "edge": "edge_1.txt",
        "vscode": "vscode_1.txt",
    }

    for domain, filename in test_demos.items():
        demo_file = demo_dir / filename
        assert demo_file.exists(), f"Demo file {filename} not found"

        content = demo_file.read_text()
        assert f"DOMAIN: {domain}" in content, f"Domain mismatch in {filename}"


def test_demo_action_formats():
    """Test that demos use valid action formats."""
    demo_dir = Path(__file__).parent.parent / "demo_library" / "synthetic_demos"
    demo_file = demo_dir / "notepad_1.txt"

    content = demo_file.read_text()

    # Check for action format patterns
    import re

    # Should have at least one of these action types
    has_click = bool(re.search(r'CLICK\(x=[\d.]+,\s*y=[\d.]+\)', content))
    has_type = bool(re.search(r'TYPE\(".*"\)', content))
    has_done = "DONE()" in content

    assert has_done, "Demo should end with DONE()"
    assert has_click or has_type, "Demo should have at least one CLICK or TYPE action"


def test_demo_coordinates_normalized():
    """Test that demos use normalized coordinates (0.0-1.0)."""
    demo_dir = Path(__file__).parent.parent / "demo_library" / "synthetic_demos"

    import re

    # Check a few demos
    for demo_file in list(demo_dir.glob("*.txt"))[:10]:
        content = demo_file.read_text()

        # Find all CLICK coordinates
        clicks = re.findall(r'CLICK\(x=([\d.]+),\s*y=([\d.]+)\)', content)

        for x, y in clicks:
            x_val = float(x)
            y_val = float(y)

            assert 0.0 <= x_val <= 1.0, f"x coordinate {x_val} out of range in {demo_file.name}"
            assert 0.0 <= y_val <= 1.0, f"y coordinate {y_val} out of range in {demo_file.name}"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
