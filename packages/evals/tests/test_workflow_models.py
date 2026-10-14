"""Tests for workflow extraction models, WAA adapter, and matching pipeline.

Implements Priority 1 tests from docs/design/workflow_extraction_pipeline.md
Section 7, using synthetic data families A/B/C/D.

Test organization:
    - TestRecordingSession: RecordingSession creation and computed fields
    - TestWorkflowDemoText: Workflow.to_demo_text() format verification
    - TestContentHash: Content hash determinism
    - TestWAARecordingAdapter: WAA meta.json parsing
    - TestWorkflowMatching: Cosine similarity matching (Pass 3)
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from openadapt_evals.workflow.adapters.waa import WAARecordingAdapter
from openadapt_evals.workflow.models import (
    ActionType,
    CanonicalWorkflow,
    NormalizedAction,
    RecordingSession,
    RecordingSource,
    Workflow,
    WorkflowLibrary,
    WorkflowStep,
)
from openadapt_evals.workflow.pipeline.match import (
    add_instance_to_canonical,
    create_canonical_from_workflow,
    match_workflow_to_canonical,
)

# ---------------------------------------------------------------------------
# Synthetic data factories
# ---------------------------------------------------------------------------


def _make_step(
    index: int,
    description: str,
    think: str,
    action: str,
    expect: str,
    action_type: ActionType = ActionType.CLICK,
    app_name: str = "System Settings",
) -> WorkflowStep:
    """Helper to build a WorkflowStep with minimal boilerplate."""
    return WorkflowStep(
        step_index=index,
        timestamp_start=float(index),
        timestamp_end=float(index + 1),
        description=description,
        think=think,
        action=action,
        expect=expect,
        action_type=action_type,
        app_name=app_name,
        ui_element=description,
    )


def _make_workflow(
    name: str,
    goal: str,
    description: str,
    steps: list[WorkflowStep],
    domain: str = "system_settings",
    app_names: list[str] | None = None,
    tags: list[str] | None = None,
    embedding: list[float] | None = None,
) -> Workflow:
    """Helper to build a Workflow with minimal boilerplate."""
    return Workflow(
        name=name,
        description=description,
        goal=goal,
        app_names=app_names or ["System Settings"],
        domain=domain,
        tags=tags or [],
        steps=steps,
        total_duration_seconds=float(len(steps)),
        session_id="test_session",
        transcript_id="test_transcript",
        recording_source=RecordingSource.WAA_VNC,
        embedding=embedding,
        embedding_model="test" if embedding else None,
        embedding_dim=len(embedding) if embedding else None,
    )


# ---------------------------------------------------------------------------
# Family A: Settings Toggle (3 variants, expected to match same canonical)
# ---------------------------------------------------------------------------

def _family_a_wifi() -> Workflow:
    """A1: Turn off Wi-Fi in System Settings (5 steps)."""
    base_emb = np.random.RandomState(42).randn(64)
    # Add small noise for variant similarity
    emb = (base_emb + np.random.RandomState(100).randn(64) * 0.05).tolist()
    return _make_workflow(
        name="Toggle Wi-Fi off",
        goal="Turn off Wi-Fi in System Settings",
        description="Open system settings, navigate to network, toggle Wi-Fi off",
        domain="system_settings",
        tags=["settings", "toggle", "network"],
        embedding=emb,
        steps=[
            _make_step(0, "Open System Settings", "Need to access settings",
                        "Click System Settings icon", "Settings app opens"),
            _make_step(1, "Click Network section", "Navigate to network settings",
                        "Click Network in sidebar", "Network settings displayed"),
            _make_step(2, "Locate Wi-Fi toggle", "Find the Wi-Fi control",
                        "Scroll to Wi-Fi section", "Wi-Fi toggle visible"),
            _make_step(3, "Click Wi-Fi toggle to off", "Turn off Wi-Fi",
                        "Click the toggle switch", "Wi-Fi toggle switches to off"),
            _make_step(4, "Confirm Wi-Fi is disabled", "Verify the change",
                        "Check status indicator", "Wi-Fi shows as disabled"),
        ],
    )


def _family_a_bluetooth() -> Workflow:
    """A2: Disable Bluetooth in Settings (5 steps)."""
    base_emb = np.random.RandomState(42).randn(64)
    emb = (base_emb + np.random.RandomState(101).randn(64) * 0.05).tolist()
    return _make_workflow(
        name="Toggle Bluetooth off",
        goal="Disable Bluetooth in Settings",
        description="Open system settings, navigate to bluetooth, toggle off",
        domain="system_settings",
        tags=["settings", "toggle", "bluetooth"],
        embedding=emb,
        steps=[
            _make_step(0, "Open System Settings", "Need to access settings",
                        "Click System Settings icon", "Settings app opens"),
            _make_step(1, "Click Bluetooth section", "Navigate to bluetooth",
                        "Click Bluetooth in sidebar", "Bluetooth settings shown"),
            _make_step(2, "Locate Bluetooth toggle", "Find the control",
                        "Scroll to toggle area", "Bluetooth toggle visible"),
            _make_step(3, "Click Bluetooth toggle to off", "Turn off Bluetooth",
                        "Click the toggle switch", "Bluetooth toggle switches off"),
            _make_step(4, "Confirm Bluetooth disabled", "Verify the change",
                        "Check status indicator", "Bluetooth shows disabled"),
        ],
    )


def _family_a_night_shift() -> Workflow:
    """A3: Turn off Night Shift in Display Settings (6 steps)."""
    base_emb = np.random.RandomState(42).randn(64)
    emb = (base_emb + np.random.RandomState(102).randn(64) * 0.05).tolist()
    return _make_workflow(
        name="Toggle Night Shift off",
        goal="Turn off Night Shift in Display Settings",
        description="Open settings, navigate to display, toggle night shift off",
        domain="system_settings",
        tags=["settings", "toggle", "display"],
        embedding=emb,
        steps=[
            _make_step(0, "Open System Settings", "Need to access settings",
                        "Click System Settings icon", "Settings app opens"),
            _make_step(1, "Click Display section", "Navigate to display",
                        "Click Display in sidebar", "Display settings shown"),
            _make_step(2, "Click Night Shift tab", "Find night shift controls",
                        "Click Night Shift tab", "Night Shift options visible"),
            _make_step(3, "Locate Night Shift toggle", "Find the control",
                        "Scroll to toggle", "Night Shift toggle visible"),
            _make_step(4, "Click toggle to off", "Turn off Night Shift",
                        "Click the toggle switch", "Toggle switches off"),
            _make_step(5, "Confirm Night Shift disabled", "Verify change",
                        "Check display appearance", "Night Shift is off"),
        ],
    )


# ---------------------------------------------------------------------------
# Family B: Spreadsheet Data Entry (3 variants, expected to match)
# ---------------------------------------------------------------------------

def _family_b_sales() -> Workflow:
    """B1: Enter quarterly sales data (8 steps)."""
    base_emb = np.random.RandomState(200).randn(64)
    emb = (base_emb + np.random.RandomState(300).randn(64) * 0.05).tolist()
    return _make_workflow(
        name="Enter quarterly sales data",
        goal="Enter quarterly sales data in spreadsheet",
        description="Create headers and enter sales figures for each quarter",
        domain="spreadsheet",
        app_names=["LibreOffice Calc"],
        tags=["data-entry", "spreadsheet", "sales"],
        embedding=emb,
        steps=[
            _make_step(i, desc, "Enter data", "Type in cell", "Data entered",
                        ActionType.TYPE, "LibreOffice Calc")
            for i, desc in enumerate([
                "Click cell A1 and type 'Quarter'",
                "Tab to B1 and type 'Revenue'",
                "Tab to C1 and type 'Expenses'",
                "Click A2 and type 'Q1'",
                "Tab to B2 and type '50000'",
                "Tab to C2 and type '30000'",
                "Click A3 and type 'Q2'",
                "Tab to B3 and type '55000'",
            ])
        ],
    )


def _family_b_budget() -> Workflow:
    """B2: Create budget spreadsheet (10 steps)."""
    base_emb = np.random.RandomState(200).randn(64)
    emb = (base_emb + np.random.RandomState(301).randn(64) * 0.05).tolist()
    return _make_workflow(
        name="Create budget spreadsheet",
        goal="Create a monthly budget spreadsheet",
        description="Set up budget categories and enter monthly amounts",
        domain="spreadsheet",
        app_names=["LibreOffice Calc"],
        tags=["data-entry", "spreadsheet", "budget"],
        embedding=emb,
        steps=[
            _make_step(i, desc, "Enter data", "Type in cell", "Data entered",
                        ActionType.TYPE, "LibreOffice Calc")
            for i, desc in enumerate([
                "Click cell A1 and type 'Category'",
                "Tab to B1 and type 'January'",
                "Tab to C1 and type 'February'",
                "Click A2 and type 'Rent'",
                "Tab to B2 and type '1500'",
                "Tab to C2 and type '1500'",
                "Click A3 and type 'Utilities'",
                "Tab to B3 and type '200'",
                "Tab to C3 and type '210'",
                "Click A4 and type 'Food'",
            ])
        ],
    )


def _family_b_grades() -> Workflow:
    """B3: Enter student grades (7 steps)."""
    base_emb = np.random.RandomState(200).randn(64)
    emb = (base_emb + np.random.RandomState(302).randn(64) * 0.05).tolist()
    return _make_workflow(
        name="Enter student grades",
        goal="Enter student grades in a spreadsheet",
        description="Create a grade sheet with student names and scores",
        domain="spreadsheet",
        app_names=["LibreOffice Calc"],
        tags=["data-entry", "spreadsheet", "grades"],
        embedding=emb,
        steps=[
            _make_step(i, desc, "Enter data", "Type in cell", "Data entered",
                        ActionType.TYPE, "LibreOffice Calc")
            for i, desc in enumerate([
                "Click cell A1 and type 'Student'",
                "Tab to B1 and type 'Score'",
                "Tab to C1 and type 'Grade'",
                "Click A2 and type 'Student 1'",
                "Tab to B2 and type '95'",
                "Click A3 and type 'Student 2'",
                "Tab to B3 and type '87'",
            ])
        ],
    )


# ---------------------------------------------------------------------------
# Family C: Document Formatting (2 variants, expected to match)
# ---------------------------------------------------------------------------

def _family_c_arial() -> Workflow:
    """C1: Change document font to Arial 14pt (4 steps)."""
    base_emb = np.random.RandomState(400).randn(64)
    emb = (base_emb + np.random.RandomState(500).randn(64) * 0.05).tolist()
    return _make_workflow(
        name="Change font to Arial 14pt",
        goal="Change document font to Arial 14pt",
        description="Select all text and change font family and size",
        domain="document",
        app_names=["LibreOffice Writer"],
        tags=["formatting", "font", "document"],
        embedding=emb,
        steps=[
            _make_step(0, "Select all text with Ctrl+A", "Select everything",
                        "Press Ctrl+A", "All text selected",
                        ActionType.KEY_COMBO, "LibreOffice Writer"),
            _make_step(1, "Click font dropdown", "Change font family",
                        "Click font name dropdown", "Font list appears",
                        ActionType.CLICK, "LibreOffice Writer"),
            _make_step(2, "Type 'Arial' and press Enter", "Set font",
                        "Type Arial, press Enter", "Font changed to Arial",
                        ActionType.TYPE, "LibreOffice Writer"),
            _make_step(3, "Set font size to 14", "Set size",
                        "Click size box, type 14, press Enter", "Size set to 14",
                        ActionType.TYPE, "LibreOffice Writer"),
        ],
    )


def _family_c_helvetica() -> Workflow:
    """C2: Set heading to bold Helvetica (5 steps)."""
    base_emb = np.random.RandomState(400).randn(64)
    emb = (base_emb + np.random.RandomState(501).randn(64) * 0.05).tolist()
    return _make_workflow(
        name="Set heading to bold Helvetica",
        goal="Set heading to bold Helvetica",
        description="Select heading text and apply bold Helvetica formatting",
        domain="document",
        app_names=["LibreOffice Writer"],
        tags=["formatting", "font", "document", "bold"],
        embedding=emb,
        steps=[
            _make_step(0, "Select heading text", "Select the heading",
                        "Click and drag over heading", "Heading selected",
                        ActionType.CLICK, "LibreOffice Writer"),
            _make_step(1, "Click font dropdown", "Change font",
                        "Click font name dropdown", "Font list appears",
                        ActionType.CLICK, "LibreOffice Writer"),
            _make_step(2, "Type 'Helvetica' and press Enter", "Set font",
                        "Type Helvetica, press Enter", "Font changed",
                        ActionType.TYPE, "LibreOffice Writer"),
            _make_step(3, "Click bold button", "Make text bold",
                        "Click B button in toolbar", "Text becomes bold",
                        ActionType.CLICK, "LibreOffice Writer"),
            _make_step(4, "Verify formatting", "Check result",
                        "Look at heading appearance", "Heading is bold Helvetica",
                        ActionType.CLICK, "LibreOffice Writer"),
        ],
    )


# ---------------------------------------------------------------------------
# Singleton D: Archive files (no match expected)
# ---------------------------------------------------------------------------

def _singleton_d_archive() -> Workflow:
    """D1: Create zip archive of project folder (3 steps)."""
    emb = np.random.RandomState(999).randn(64).tolist()
    return _make_workflow(
        name="Create zip archive",
        goal="Create zip archive of project folder",
        description="Select folder, right-click, compress to zip",
        domain="file_management",
        app_names=["File Explorer"],
        tags=["archive", "zip", "files"],
        embedding=emb,
        steps=[
            _make_step(0, "Navigate to project folder", "Find the folder",
                        "Open file explorer, browse to folder", "Folder visible",
                        ActionType.CLICK, "File Explorer"),
            _make_step(1, "Right-click the folder", "Open context menu",
                        "Right-click on folder", "Context menu appears",
                        ActionType.RIGHT_CLICK, "File Explorer"),
            _make_step(2, "Select 'Compress to ZIP'", "Create archive",
                        "Click compress option", "ZIP file created",
                        ActionType.CLICK, "File Explorer"),
        ],
    )


# ---------------------------------------------------------------------------
# Pytest fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def settings_toggle_workflows() -> list[Workflow]:
    """Family A: three settings toggle workflows that should match."""
    return [_family_a_wifi(), _family_a_bluetooth(), _family_a_night_shift()]


@pytest.fixture
def spreadsheet_workflows() -> list[Workflow]:
    """Family B: three spreadsheet data entry workflows that should match."""
    return [_family_b_sales(), _family_b_budget(), _family_b_grades()]


@pytest.fixture
def document_workflows() -> list[Workflow]:
    """Family C: two document formatting workflows that should match."""
    return [_family_c_arial(), _family_c_helvetica()]


@pytest.fixture
def singleton_workflow() -> Workflow:
    """Singleton D: unique archive workflow with no match."""
    return _singleton_d_archive()


@pytest.fixture
def mixed_workflows() -> list[Workflow]:
    """Mix of settings + spreadsheet workflows (should form 2 canonicals)."""
    return [_family_a_wifi(), _family_a_bluetooth(), _family_b_sales()]


@pytest.fixture
def synthetic_waa_recording(tmp_path: Path) -> str:
    """Create a synthetic WAA recording directory with meta.json and PNGs."""
    recording_dir = tmp_path / "test-task-id-WOS"
    recording_dir.mkdir()

    meta = {
        "task_id": "test-task-id-WOS",
        "instruction": "Change the font to Times New Roman throughout the text.",
        "num_steps": 3,
        "steps": [
            {
                "action_hint": None,
                "suggested_step": "Press Ctrl+A to select all text in the document.",
                "step_was_refined": False,
            },
            {
                "action_hint": None,
                "suggested_step": 'Click the font dropdown in the toolbar.',
                "step_was_refined": False,
            },
            {
                "action_hint": None,
                "suggested_step": 'Type "Times New Roman" and press Enter.',
                "step_was_refined": False,
            },
        ],
        "step_plans": [
            {
                "at_step_idx": 0,
                "trigger": "initial",
                "steps": [
                    "Press Ctrl+A to select all text in the document.",
                    "Click the font dropdown in the toolbar.",
                    'Type "Times New Roman" and press Enter.',
                ],
            }
        ],
        "server_url": "http://localhost:5001",
        "recorded_at": "2026-03-04T21:34:43.107090+00:00",
        "recording_complete": True,
    }

    meta_path = recording_dir / "meta.json"
    meta_path.write_text(json.dumps(meta))

    # Create dummy PNG files
    for i in range(3):
        (recording_dir / f"step_{i:02d}_before.png").write_bytes(b"PNG")
        (recording_dir / f"step_{i:02d}_after.png").write_bytes(b"PNG")

    return str(meta_path)


# ---------------------------------------------------------------------------
# Test classes
# ---------------------------------------------------------------------------


class TestRecordingSession:
    """Verify RecordingSession creation and computed fields."""

    def test_content_hash_deterministic(self):
        """Same session data produces same content hash."""
        session1 = RecordingSession(
            source=RecordingSource.WAA_VNC,
            task_description="Test task",
            duration_seconds=10.0,
        )
        session2 = RecordingSession(
            source=RecordingSource.WAA_VNC,
            task_description="Test task",
            duration_seconds=10.0,
        )
        assert session1.content_hash == session2.content_hash

    def test_content_hash_differs_for_different_tasks(self):
        """Different task descriptions produce different hashes."""
        session1 = RecordingSession(
            source=RecordingSource.WAA_VNC,
            task_description="Task A",
            duration_seconds=10.0,
        )
        session2 = RecordingSession(
            source=RecordingSource.WAA_VNC,
            task_description="Task B",
            duration_seconds=10.0,
        )
        assert session1.content_hash != session2.content_hash

    def test_app_names_extracted(self):
        """app_names computed field returns unique sorted app names."""
        session = RecordingSession(
            source=RecordingSource.WAA_VNC,
            task_description="Test",
            actions=[
                NormalizedAction(
                    timestamp=0,
                    action_type=ActionType.CLICK,
                    description="Click",
                    app_name="Calc",
                ),
                NormalizedAction(
                    timestamp=1,
                    action_type=ActionType.CLICK,
                    description="Click",
                    app_name="Writer",
                ),
                NormalizedAction(
                    timestamp=2,
                    action_type=ActionType.CLICK,
                    description="Click",
                    app_name="Calc",
                ),
            ],
        )
        assert session.app_names == ["Calc", "Writer"]

    def test_action_count(self):
        """action_count computed field returns len(actions)."""
        session = RecordingSession(
            source=RecordingSource.WAA_VNC,
            task_description="Test",
            actions=[
                NormalizedAction(
                    timestamp=0,
                    action_type=ActionType.CLICK,
                    description="Click A",
                ),
                NormalizedAction(
                    timestamp=1,
                    action_type=ActionType.TYPE,
                    description="Type B",
                ),
            ],
        )
        assert session.action_count == 2

    def test_empty_session(self):
        """Empty session has zero action count and empty app_names."""
        session = RecordingSession(
            source=RecordingSource.IMPORTED,
            task_description="Empty",
        )
        assert session.action_count == 0
        assert session.app_names == []


class TestWorkflowDemoText:
    """Verify Workflow.to_demo_text() format."""

    def test_to_demo_text_format(self):
        """Workflow.to_demo_text() produces parseable DemoController format."""
        workflow = Workflow(
            name="Test workflow",
            description="A test",
            goal="Do the test",
            app_names=["TestApp"],
            domain="test",
            steps=[
                WorkflowStep(
                    step_index=0,
                    timestamp_start=0,
                    timestamp_end=1,
                    description="Click button",
                    think="Need to click",
                    action="Click the button",
                    expect="Button clicked",
                    action_type=ActionType.CLICK,
                    app_name="TestApp",
                    ui_element="Button",
                ),
            ],
            total_duration_seconds=1.0,
            session_id="test",
            transcript_id="test",
            recording_source=RecordingSource.WAA_VNC,
        )
        demo_text = workflow.to_demo_text()
        assert "GOAL:" in demo_text
        assert "PLAN:" in demo_text
        assert "REFERENCE TRAJECTORY:" in demo_text
        assert "Think:" in demo_text
        assert "Action:" in demo_text
        assert "Expect:" in demo_text

    def test_to_demo_text_contains_goal(self):
        """Demo text includes the workflow goal."""
        workflow = _family_a_wifi()
        demo_text = workflow.to_demo_text()
        assert "Turn off Wi-Fi in System Settings" in demo_text

    def test_to_demo_text_step_numbering(self):
        """Demo text numbers steps sequentially starting at 1."""
        workflow = _family_a_wifi()
        demo_text = workflow.to_demo_text()
        assert "Step 1:" in demo_text
        assert "Step 5:" in demo_text

    def test_canonical_to_demo_text_has_note(self):
        """CanonicalWorkflow.to_demo_text() includes adaptation note."""
        canonical = CanonicalWorkflow(
            name="Test",
            description="Test",
            goal="Test goal",
            app_names=["TestApp"],
            domain="test",
            steps=[
                WorkflowStep(
                    step_index=0,
                    timestamp_start=0,
                    timestamp_end=1,
                    description="Do thing",
                    think="Need to",
                    action="Do it",
                    expect="Done",
                    action_type=ActionType.CLICK,
                    app_name="TestApp",
                    ui_element="Thing",
                ),
            ],
            instance_count=1,
        )
        demo_text = canonical.to_demo_text()
        assert "NOTE:" in demo_text
        assert "Adapt steps as needed" in demo_text


class TestContentHash:
    """Verify content hash determinism for workflows."""

    def test_workflow_content_hash_deterministic(self):
        """Same workflow data produces same content hash."""
        steps = [
            WorkflowStep(
                step_index=0,
                timestamp_start=0,
                timestamp_end=1,
                description="Click button",
                think="Need to click",
                action="Click",
                expect="Clicked",
                action_type=ActionType.CLICK,
                app_name="App",
                ui_element="Button",
            ),
        ]
        wf1 = Workflow(
            name="Test",
            description="Test",
            goal="Test",
            app_names=["App"],
            domain="test",
            steps=steps,
            total_duration_seconds=1.0,
            session_id="s1",
            transcript_id="t1",
            recording_source=RecordingSource.WAA_VNC,
        )
        wf2 = Workflow(
            name="Test",
            description="Test",
            goal="Test",
            app_names=["App"],
            domain="test",
            steps=steps,
            total_duration_seconds=1.0,
            session_id="s2",  # Different session_id
            transcript_id="t2",
            recording_source=RecordingSource.WAA_VNC,
        )
        # Content hash depends on name + step descriptions, not session_id
        assert wf1.content_hash == wf2.content_hash

    def test_workflow_content_hash_differs(self):
        """Different step descriptions produce different hashes."""
        step_a = WorkflowStep(
            step_index=0,
            timestamp_start=0,
            timestamp_end=1,
            description="Click button A",
            think="t",
            action="a",
            expect="e",
            action_type=ActionType.CLICK,
            app_name="App",
            ui_element="A",
        )
        step_b = WorkflowStep(
            step_index=0,
            timestamp_start=0,
            timestamp_end=1,
            description="Click button B",
            think="t",
            action="a",
            expect="e",
            action_type=ActionType.CLICK,
            app_name="App",
            ui_element="B",
        )
        wf1 = Workflow(
            name="Test",
            description="Test",
            goal="Test",
            app_names=["App"],
            domain="test",
            steps=[step_a],
            total_duration_seconds=1.0,
            session_id="s",
            transcript_id="t",
            recording_source=RecordingSource.WAA_VNC,
        )
        wf2 = Workflow(
            name="Test",
            description="Test",
            goal="Test",
            app_names=["App"],
            domain="test",
            steps=[step_b],
            total_duration_seconds=1.0,
            session_id="s",
            transcript_id="t",
            recording_source=RecordingSource.WAA_VNC,
        )
        assert wf1.content_hash != wf2.content_hash


class TestWAARecordingAdapter:
    """Verify WAA meta.json parsing into RecordingSession."""

    def test_from_meta_json(self, synthetic_waa_recording):
        """meta.json with steps and PNG paths -> RecordingSession."""
        session = WAARecordingAdapter.from_meta_json(synthetic_waa_recording)
        assert session.action_count > 0
        assert session.source == RecordingSource.WAA_VNC

    def test_action_count_matches_steps(self, synthetic_waa_recording):
        """Number of actions matches num_steps in meta.json."""
        session = WAARecordingAdapter.from_meta_json(synthetic_waa_recording)
        assert session.action_count == 3

    def test_task_description_populated(self, synthetic_waa_recording):
        """task_description comes from instruction field."""
        session = WAARecordingAdapter.from_meta_json(synthetic_waa_recording)
        assert "Times New Roman" in session.task_description

    def test_platform_is_windows(self, synthetic_waa_recording):
        """WAA recordings are always from Windows VMs."""
        session = WAARecordingAdapter.from_meta_json(synthetic_waa_recording)
        assert session.platform == "windows"

    def test_screenshot_paths_populated(self, synthetic_waa_recording):
        """Screenshot paths are set for steps with existing PNGs."""
        session = WAARecordingAdapter.from_meta_json(synthetic_waa_recording)
        for action in session.actions:
            assert action.screenshot_before_path is not None
            assert action.screenshot_after_path is not None

    def test_action_types_classified(self, synthetic_waa_recording):
        """Actions are classified from suggested_step text."""
        session = WAARecordingAdapter.from_meta_json(synthetic_waa_recording)
        # Step 0: "Press Ctrl+A" -> KEY_COMBO
        assert session.actions[0].action_type == ActionType.KEY_COMBO
        # Step 1: "Click the font dropdown" -> CLICK
        assert session.actions[1].action_type == ActionType.CLICK
        # Step 2: 'Type "Times New Roman"' -> TYPE
        assert session.actions[2].action_type == ActionType.TYPE

    def test_recorded_at_parsed(self, synthetic_waa_recording):
        """recorded_at timestamp is parsed from ISO format."""
        session = WAARecordingAdapter.from_meta_json(synthetic_waa_recording)
        assert session.recorded_at.year == 2026
        assert session.recorded_at.month == 3

    def test_source_metadata_preserved(self, synthetic_waa_recording):
        """Source metadata includes task_id and step_plans."""
        session = WAARecordingAdapter.from_meta_json(synthetic_waa_recording)
        assert session.source_metadata["task_id"] == "test-task-id-WOS"
        assert session.source_metadata["recording_complete"] is True

    def test_missing_pngs_handled(self, tmp_path):
        """Missing PNG files result in None screenshot paths."""
        recording_dir = tmp_path / "no-pngs-WOS"
        recording_dir.mkdir()

        meta = {
            "task_id": "no-pngs-WOS",
            "instruction": "Test instruction",
            "num_steps": 1,
            "steps": [
                {
                    "action_hint": None,
                    "suggested_step": "Click something",
                    "step_was_refined": False,
                }
            ],
            "recorded_at": "2026-01-01T00:00:00+00:00",
        }
        meta_path = recording_dir / "meta.json"
        meta_path.write_text(json.dumps(meta))

        session = WAARecordingAdapter.from_meta_json(str(meta_path))
        assert session.actions[0].screenshot_before_path is None
        assert session.actions[0].screenshot_after_path is None

    def test_real_waa_recording(self):
        """Parse a real WAA recording if available (skipped if not found)."""
        real_meta = Path(
            "/Users/abrichr/oa/src/openadapt-evals/waa_recordings/"
            "0e763496-b6bb-4508-a427-fad0b6c3e195-WOS/meta.json"
        )
        if not real_meta.exists():
            pytest.skip("Real WAA recording not available")
        session = WAARecordingAdapter.from_meta_json(str(real_meta))
        assert session.action_count == 3
        assert session.source == RecordingSource.WAA_VNC
        assert "Times New Roman" in session.task_description


class TestWorkflowMatching:
    """Test Pass 3: cosine similarity matching."""

    def test_similar_workflows_match(self, settings_toggle_workflows):
        """Three 'toggle setting' workflows -> same canonical."""
        library = WorkflowLibrary()
        for wf in settings_toggle_workflows:
            canonical_id = match_workflow_to_canonical(wf, library)
            if canonical_id is None:
                create_canonical_from_workflow(wf, library)
            else:
                add_instance_to_canonical(wf, canonical_id, library)

        # All three should end up in the same canonical
        assert library.canonical_count == 1
        assert library.canonical_workflows[0].instance_count == 3

    def test_different_workflows_separate(self, mixed_workflows):
        """Settings toggles + spreadsheet entry -> separate canonicals."""
        library = WorkflowLibrary()
        for wf in mixed_workflows:
            canonical_id = match_workflow_to_canonical(wf, library)
            if canonical_id is None:
                create_canonical_from_workflow(wf, library)
            else:
                add_instance_to_canonical(wf, canonical_id, library)

        # Settings (A1, A2) should match, spreadsheet (B1) should be separate
        assert library.canonical_count >= 2

    def test_singleton_becomes_own_canonical(self, singleton_workflow):
        """A unique workflow with no matches -> singleton canonical."""
        library = WorkflowLibrary()
        canonical_id = match_workflow_to_canonical(
            singleton_workflow, library
        )
        assert canonical_id is None
        create_canonical_from_workflow(singleton_workflow, library)
        assert library.canonical_count == 1
        assert library.canonical_workflows[0].instance_count == 1

    def test_similarity_threshold_respected(self):
        """Workflows with similarity < 0.85 do NOT match."""
        emb_a = np.random.randn(3072).tolist()
        # Create a very different embedding (negated)
        emb_b = (-np.array(emb_a)).tolist()

        wf_a = Workflow(
            name="A",
            description="A",
            goal="A",
            app_names=["A"],
            domain="a",
            steps=[],
            total_duration_seconds=1.0,
            session_id="a",
            transcript_id="a",
            recording_source=RecordingSource.WAA_VNC,
            embedding=emb_a,
            embedding_model="test",
            embedding_dim=3072,
        )
        wf_b = Workflow(
            name="B",
            description="B",
            goal="B",
            app_names=["B"],
            domain="b",
            steps=[],
            total_duration_seconds=1.0,
            session_id="b",
            transcript_id="b",
            recording_source=RecordingSource.WAA_VNC,
            embedding=emb_b,
            embedding_model="test",
            embedding_dim=3072,
        )

        library = WorkflowLibrary()
        create_canonical_from_workflow(wf_a, library)

        match = match_workflow_to_canonical(wf_b, library)
        assert match is None  # Should NOT match

    def test_empty_library_returns_none(self):
        """Matching against an empty library returns None."""
        wf = _family_a_wifi()
        library = WorkflowLibrary()
        assert match_workflow_to_canonical(wf, library) is None

    def test_no_embedding_returns_none(self):
        """Workflow without embedding cannot match."""
        wf = Workflow(
            name="No embedding",
            description="Test",
            goal="Test",
            app_names=["App"],
            domain="test",
            steps=[],
            total_duration_seconds=1.0,
            session_id="s",
            transcript_id="t",
            recording_source=RecordingSource.WAA_VNC,
            embedding=None,
        )
        library = WorkflowLibrary()
        create_canonical_from_workflow(_family_a_wifi(), library)
        assert match_workflow_to_canonical(wf, library) is None

    def test_confidence_grows_with_instances(self, settings_toggle_workflows):
        """Confidence increases as more instances are added."""
        library = WorkflowLibrary()
        create_canonical_from_workflow(
            settings_toggle_workflows[0], library
        )
        initial_confidence = library.canonical_workflows[0].confidence
        assert initial_confidence == pytest.approx(0.3)

        add_instance_to_canonical(
            settings_toggle_workflows[1],
            library.canonical_workflows[0].canonical_id,
            library,
        )
        assert library.canonical_workflows[0].confidence > initial_confidence

    def test_version_increments_on_add(self, settings_toggle_workflows):
        """Version bumps when adding instances."""
        library = WorkflowLibrary()
        create_canonical_from_workflow(
            settings_toggle_workflows[0], library
        )
        assert library.canonical_workflows[0].version == 1

        add_instance_to_canonical(
            settings_toggle_workflows[1],
            library.canonical_workflows[0].canonical_id,
            library,
        )
        assert library.canonical_workflows[0].version == 2

    def test_all_four_families_separate(
        self,
        settings_toggle_workflows,
        spreadsheet_workflows,
        document_workflows,
        singleton_workflow,
    ):
        """All four families form separate canonical workflows."""
        all_workflows = (
            settings_toggle_workflows
            + spreadsheet_workflows
            + document_workflows
            + [singleton_workflow]
        )
        library = WorkflowLibrary()
        for wf in all_workflows:
            canonical_id = match_workflow_to_canonical(wf, library)
            if canonical_id is None:
                create_canonical_from_workflow(wf, library)
            else:
                add_instance_to_canonical(wf, canonical_id, library)

        # Should form 4 canonical groups:
        # A (3 settings), B (3 spreadsheet), C (2 document), D (1 archive)
        assert library.canonical_count == 4

        # Verify instance counts
        counts = sorted(
            cw.instance_count
            for cw in library.canonical_workflows
        )
        assert counts == [1, 2, 3, 3]

    def test_library_computed_fields(
        self,
        settings_toggle_workflows,
        spreadsheet_workflows,
    ):
        """WorkflowLibrary computed fields work correctly."""
        library = WorkflowLibrary()

        # Build library with two families
        for wf in settings_toggle_workflows:
            canonical_id = match_workflow_to_canonical(wf, library)
            if canonical_id is None:
                create_canonical_from_workflow(wf, library)
            else:
                add_instance_to_canonical(wf, canonical_id, library)

        for wf in spreadsheet_workflows:
            canonical_id = match_workflow_to_canonical(wf, library)
            if canonical_id is None:
                create_canonical_from_workflow(wf, library)
            else:
                add_instance_to_canonical(wf, canonical_id, library)

        assert library.canonical_count == 2
        assert set(library.domains) == {"system_settings", "spreadsheet"}
        assert "System Settings" in library.app_coverage
        assert "LibreOffice Calc" in library.app_coverage
