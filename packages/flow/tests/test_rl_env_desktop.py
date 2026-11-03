"""The Windows desktop environments, end to end without Windows.

A simulated guest stands in for WinRM and a simulated app for the window: it
lays out the same automation ids as the pack's apps and writes through the
real ``pack_db.py`` (in a temp directory), so the reset, the planted faults,
the REST shim and verify-run's verdict are all the real code. The real-guest
run is ``python -m openadapt_flow.rl_env.validate_desktop``; its results are
committed under ``environments/openenv/_windows/results``.
"""

from __future__ import annotations

import base64
import importlib.util
import io
import json
from pathlib import Path
from typing import Any, Optional

import pytest

pytest.importorskip("openenv")

from openadapt_flow.ir import StructuralHandle, StructuralLocator  # noqa: E402
from openadapt_flow.rl_env import desktop  # noqa: E402
from openadapt_flow.rl_env.desktop import (  # noqa: E402
    DesktopAction,
    OpenAdaptDesktopEnvironment,
    load_desktop_environment,
)
from openadapt_flow.rl_env.tasks import TaskError  # noqa: E402
from openadapt_flow.rl_env.validate_desktop import leak_check, run_episode  # noqa: E402

ROOT = Path(__file__).resolve().parents[1] / "environments" / "openenv"
ENVS = [
    "win-patient-chart",
    "win-patient-chart-rdp",
    "win-claims-desk",
    "win-claims-desk-rdp",
]


def _pack_db(tmp: Path) -> Any:
    spec = importlib.util.spec_from_file_location("pack_db_sim", desktop.PACK_DB)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    module.ROOT = str(tmp)
    return module


def _png() -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (1280, 800), "white").save(buf, format="PNG")
    return buf.getvalue()


LAYOUT = {
    "patient_chart": {
        "searchBox": (20, 44, 500, 28),
        "searchButton": (530, 42, 100, 30),
        "patientGrid": (20, 82, 720, 230),
        "noteBox": (20, 350, 720, 110),
        "saveButton": (630, 494, 110, 34),
    },
    "claims_desk": {
        "searchBox": (20, 44, 520, 28),
        "searchButton": (550, 42, 100, 30),
        "claimsGrid": (20, 82, 900, 250),
        "statusApproved": (110, 395, 90, 20),
        "statusDenied": (220, 395, 90, 20),
        "statusPended": (330, 395, 90, 20),
        "amountBox": (110, 430, 160, 28),
        "noteBox": (20, 470, 900, 100),
        "saveButton": (790, 580, 130, 34),
    },
}


class SimApp:
    """The pack app's behaviour, driven by points like the real window."""

    viewport = (1280, 800)

    def __init__(self, guest: "SimGuest") -> None:
        self.guest = guest
        self.restart()

    def restart(self) -> None:
        self.fields = {"searchBox": "", "noteBox": "", "amountBox": ""}
        self.focus: Optional[str] = None
        self.rows: list[str] = []
        self.selected: Optional[str] = None
        self.decision: Optional[str] = None

    @property
    def layout(self) -> dict[str, tuple]:
        return LAYOUT[self.guest.app]

    def probe(self) -> bool:
        return True

    def screenshot(self) -> bytes:
        return _png()

    def locate_structural(
        self, locator: StructuralLocator
    ) -> Optional[StructuralHandle]:
        box = self.layout.get(locator.automation_id or "")
        if box is None:
            return None
        return StructuralHandle(
            point=(box[0] + box[2] // 2, box[1] + box[3] // 2), region=box
        )

    def read_structural(self, locator: StructuralLocator) -> Optional[dict]:
        return {"match": "unique", "text": self.fields.get(locator.automation_id or "")}

    def window_tree(self, title: str):
        return (
            [
                {
                    "role": "edit",
                    "name": k,
                    "automation_id": k,
                    "bounds": [x, y, x + w, y + h],
                    "depth": 1,
                }
                for k, (x, y, w, h) in self.layout.items()
            ],
            False,
        )

    def _hit(self, x: int, y: int) -> Optional[str]:
        for key, (bx, by, bw, bh) in self.layout.items():
            if bx <= x < bx + bw and by <= y < by + bh:
                return key
        return None

    def click(self, x: int, y: int, *, double: bool = False) -> None:
        hit = self._hit(x, y)
        db = self.guest.db
        if hit in self.fields:
            self.focus = hit
        elif hit == "searchButton":
            self._search()
        elif hit in ("patientGrid", "claimsGrid"):
            top = self.layout[hit][1]
            header = 34 if hit == "patientGrid" else 32
            row = 36 if hit == "patientGrid" else 34
            index = (y - top - header) // row
            if 0 <= index < len(self.rows):
                self.selected = self.rows[index]
                self.fields["noteBox"] = ""
        elif hit and hit.startswith("status"):
            self.decision = hit[len("status") :]
        elif hit == "saveButton" and self.selected:
            note = self.fields["noteBox"]
            if self.guest.app == "patient_chart":
                if note.strip():
                    db.pc_save(self.selected, note)
            elif self.decision:
                db.cd_save(self.selected, self.decision, self.fields["amountBox"], note)

    def right_click(self, x: int, y: int) -> None:
        return

    def _search(self) -> None:
        con = self.guest.db._conn(self.guest.app)
        like = f"%{self.fields['searchBox']}%"
        if self.guest.app == "patient_chart":
            sql = "SELECT mrn FROM patients WHERE first LIKE ? OR last LIKE ? OR mrn LIKE ? ORDER BY sort"
        else:
            sql = "SELECT claim_no FROM claims WHERE claim_no LIKE ? OR member LIKE ? OR member_id LIKE ? ORDER BY sort"
        self.rows = [r[0] for r in con.execute(sql, (like, like, like))]
        self.selected = None

    def type_text(self, text: str) -> None:
        if self.focus:
            self.fields[self.focus] += text

    def press(self, key: str) -> None:
        if key == "enter" and self.focus == "searchBox":
            self._search()

    def scroll(self, dx: int, dy: int) -> None:
        return


class SimEndpoint:
    def __init__(self, app: SimApp) -> None:
        self._app = app

    def backend(self) -> SimApp:
        return self._app


class SimGuest:
    host = "sim-guest"
    user = "lab"

    def __init__(self, tmp: Path, *, leak: bool = False) -> None:
        self.db = _pack_db(tmp)
        self.app = "patient_chart"
        self.ui: SimApp = SimApp(self)
        self.leak = leak
        self.resets = 0

    def deploy(self) -> None:
        return

    def reset(
        self, app: str, *, fault: str = "ok", variant: str = "none", ui=None
    ) -> dict:
        self.app = app
        self.resets += 1
        if not (self.leak and self.resets > 1):
            self.db.seed(app, fault, variant)
        self.ui.restart()
        return {"seeded": app, "digest": self.db.digest(app)}

    def dump(self, app: str) -> dict:
        return self.db.dump(app)

    def digest(self, app: str) -> str:
        return self.db.digest(app)

    def app_processes(self) -> int:
        return 1

    def stop_app(self) -> None:
        return

    def launch_agent(self, *, token: str, **_: Any) -> SimEndpoint:
        return SimEndpoint(self.ui)


@pytest.fixture
def reference(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(desktop.REFERENCE_ENV, "1")


@pytest.mark.parametrize("name", ENVS)
def test_every_environment_loads_with_24_public_tasks(name: str) -> None:
    env = load_desktop_environment(ROOT / name)
    assert len(env.split("public")) == 24
    assert env.splits() == ["public"]
    assert env.manifest.guest_image == "buyer-supplied"
    manifest = json.loads((ROOT / name / "tasks" / "MANIFEST.json").read_text())
    import hashlib

    public = (ROOT / name / "tasks" / "public.jsonl").read_bytes()
    assert manifest["public"]["sha256"] == hashlib.sha256(public).hexdigest()
    assert manifest["heldout"]["tasks"] == 24 and "sha256" in manifest["heldout"]


def test_uia_and_pixels_variants_share_one_task_set() -> None:
    for app in ("win-patient-chart", "win-claims-desk"):
        a = (ROOT / app / "tasks" / "public.jsonl").read_bytes()
        b = (ROOT / f"{app}-rdp" / "tasks" / "public.jsonl").read_bytes()
        assert a == b


@pytest.mark.parametrize("name", ["win-patient-chart", "win-claims-desk"])
def test_solutions_earn_one_and_planted_faults_are_refuted(
    name: str, tmp_path: Path, reference: None
) -> None:
    env = OpenAdaptDesktopEnvironment(ROOT / name, guest=SimGuest(tmp_path))
    env.env.manifest.agent.settle_s = 0.0
    env.finish_settle_s = 0.0
    try:
        for i, task in enumerate(env.env.tasks):
            fast = [s for s in task.solution if s["kind"] != "wait"]
            task.solution[:] = fast
            row = run_episode(env, task, "scripted")
            assert row["reward"] == 1.0, row
            trusting = run_episode(env, task, "screen_trusting")
            assert trusting["reward"] == (1.0 if task.feasible else 0.0), trusting
            if task.feasible:
                fault = ("drop", "wrong_row", "duplicate", "truncate", "stale")[i % 5]
                planted = run_episode(env, task, "planted_fault", fault=fault)
                assert planted["verdict"] == "REFUTED" and planted["reward"] == 0.0, (
                    planted
                )
                assert planted["silent_wrong_effect"] is True
    finally:
        env.close()


def test_a_restore_that_keeps_old_rows_is_refused(
    tmp_path: Path, reference: None
) -> None:
    env = OpenAdaptDesktopEnvironment(
        ROOT / "win-patient-chart", guest=SimGuest(tmp_path, leak=True)
    )
    env.env.manifest.agent.settle_s = 0.0
    env.finish_settle_s = 0.0
    task = next(
        t for t in env.env.tasks if t.feasible and t.setup.get("variant") == "none"
    )
    try:
        task.solution[:] = [s for s in task.solution if s["kind"] != "wait"]
        run_episode(env, task, "scripted")
        with pytest.raises(TaskError, match="leftover state"):
            env.reset(task_id=task.id)
    finally:
        env.close()


def test_no_state_leaks_between_episodes(tmp_path: Path, reference: None) -> None:
    env = OpenAdaptDesktopEnvironment(
        ROOT / "win-claims-desk", guest=SimGuest(tmp_path)
    )
    env.env.manifest.agent.settle_s = 0.0
    env.finish_settle_s = 0.0
    feasible = [t for t in env.env.tasks if t.feasible]
    for task in feasible[:2]:
        task.solution[:] = [s for s in task.solution if s["kind"] != "wait"]
    try:
        result = leak_check(env, feasible[0], feasible[1])
    finally:
        env.close()
    assert result["digest_after_write_differs"]
    assert result["passed"], result


def test_pixels_surface_refuses_element_ids_outside_a_reference_run(
    tmp_path: Path,
) -> None:
    env = OpenAdaptDesktopEnvironment(
        ROOT / "win-patient-chart-rdp", guest=SimGuest(tmp_path)
    )
    assert env.reference is False
    with pytest.raises(ValueError, match="x and y"):
        env._point(DesktopAction(kind="click", target="saveButton"))
    with pytest.raises(TaskError, match="reference run"):
        env.reset(task_id=env.env.tasks[0].id, fault="drop")


def test_uia_surface_lists_elements_and_refuses_the_windows_key(tmp_path: Path) -> None:
    env = OpenAdaptDesktopEnvironment(
        ROOT / "win-patient-chart", guest=SimGuest(tmp_path)
    )
    try:
        obs = env.reset(task_id=env.env.tasks[0].id)
        ids = {e["automation_id"] for e in obs.elements}
        assert {"searchBox", "noteBox", "saveButton"} <= ids
        assert base64.b64decode(obs.screenshot_png_b64)[:4] == b"\x89PNG"
        assert "fault" not in obs.instruction.lower()
        for chord in ("win", "win+r", "ctrl+esc", "ctrl+shift+esc"):
            out = env.step(DesktopAction(kind="press", text=chord))
            assert "not allowed" in (out.last_action_error or "")
    finally:
        env.close()
