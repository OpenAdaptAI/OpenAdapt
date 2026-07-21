"""Secret-typed parameters: never persisted, injected from the environment.

Fast unit tests (no browser) covering the secret contract end to end:

* the Recorder never writes a secret's literal (meta/events) and redacts its
  field region from the persisted frames;
* the compiler carries the secret through to ``Step.secret`` /
  ``Workflow.secret_params`` with ``text=None`` and no leak;
* the Replayer injects the value from ``OPENADAPT_FLOW_SECRET_<PARAM>`` and
  fails fast with an actionable message when it is missing.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from openadapt_flow.compiler import compile_recording
from openadapt_flow.ir import ActionKind, Step, Workflow
from openadapt_flow.recorder import Recorder
from openadapt_flow.runtime.replayer import Replayer, secret_env_var

SECRET = "hunter2-SUPER-secret-value"


def _png(size=(1280, 800), color=(250, 250, 250)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


class FakeBackend:
    def __init__(self) -> None:
        self._png = _png()
        self.actions: list = []

    @property
    def viewport(self):
        return (1280, 800)

    def screenshot(self):
        return self._png

    def click(self, x, y, *, double=False):
        self.actions.append(("click", x, y, double))

    def type_text(self, text):
        self.actions.append(("type", text))

    def press(self, key):
        self.actions.append(("press", key))

    def scroll(self, dx, dy):
        self.actions.append(("scroll", dx, dy))


def _events(rec_dir: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in (rec_dir / "events.jsonl").read_text().splitlines()
        if line.strip()
    ]


# -- Recorder ---------------------------------------------------------------


def test_recorder_secret_not_persisted_and_region_redacted(tmp_path: Path) -> None:
    rec = Recorder(FakeBackend(), tmp_path / "rec", app_url="http://app/")
    before = _png()
    rec.record_observed(
        {"kind": "type"},
        before_png=before,
        structural_before={},
        param="password",
        secret=True,
        redact_region=(100, 200, 300, 40),
    )
    rec_dir = rec.finish()

    # meta: the param is listed as secret, its value is NOWHERE.
    meta = json.loads((rec_dir / "meta.json").read_text())
    assert meta["secret_params"] == ["password"]
    assert "password" not in meta["params"]
    blob = (rec_dir / "meta.json").read_text() + (rec_dir / "events.jsonl").read_text()
    assert SECRET not in blob

    # events: the type event carries the param + secret flag but NO text.
    (event,) = _events(rec_dir)
    assert event["kind"] == "type"
    assert event["param"] == "password"
    assert event["secret"] is True
    assert "text" not in event

    # frames: the redacted region is solid black in both frames.
    for suffix in ("before", "after"):
        arr = np.asarray(
            Image.open(rec_dir / "frames" / f"0000_{suffix}.png").convert("RGB")
        )
        region = arr[200:240, 100:400]
        assert region.sum() == 0, f"{suffix} region not redacted"


def test_recorder_non_secret_param_keeps_value(tmp_path: Path) -> None:
    rec = Recorder(FakeBackend(), tmp_path / "rec")
    rec.record_observed(
        {"kind": "type", "text": "visible note"},
        before_png=_png(),
        structural_before={},
        param="note",
    )
    rec_dir = rec.finish()
    meta = json.loads((rec_dir / "meta.json").read_text())
    assert meta["params"] == {"note": "visible note"}
    assert meta["secret_params"] == []
    (event,) = _events(rec_dir)
    assert event["text"] == "visible note" and event["param"] == "note"


# -- compiler ---------------------------------------------------------------


def _write_secret_recording(rec_dir: Path) -> None:
    (rec_dir / "frames").mkdir(parents=True)
    (rec_dir / "frames" / "0000_before.png").write_bytes(_png())
    (rec_dir / "frames" / "0000_after.png").write_bytes(_png())
    (rec_dir / "meta.json").write_text(
        json.dumps(
            {
                "id": "abc",
                "created_at": "2026-07-13T00:00:00+00:00",
                "viewport": [1280, 800],
                "app_url": "http://app/",
                "params": {},
                "secret_params": ["password"],
            }
        )
    )
    (rec_dir / "events.jsonl").write_text(
        json.dumps({"i": 0, "kind": "type", "param": "password", "secret": True}) + "\n"
    )


def test_compiler_carries_secret_without_value(tmp_path: Path) -> None:
    rec_dir = tmp_path / "rec"
    _write_secret_recording(rec_dir)
    bundle = tmp_path / "bundle"
    workflow = compile_recording(rec_dir, bundle, name="secret-wf")

    assert workflow.secret_params == ["password"]
    (step,) = workflow.steps
    assert step.action is ActionKind.TYPE
    assert step.secret is True
    assert step.param == "password"
    assert step.text is None
    # Nothing in the persisted bundle carries a value for the secret.
    assert "password" not in workflow.params
    assert SECRET not in (bundle / "workflow.json").read_text()


# -- replayer ---------------------------------------------------------------


def test_secret_env_var_mapping() -> None:
    assert secret_env_var("password") == "OPENADAPT_FLOW_SECRET_PASSWORD"
    assert secret_env_var("api-key") == "OPENADAPT_FLOW_SECRET_API_KEY"


def _secret_workflow() -> Workflow:
    return Workflow(
        name="secret-only",
        params={},
        secret_params=["password"],
        steps=[
            Step(
                id="step_000",
                intent="type <password> (secret)",
                action=ActionKind.TYPE,
                param="password",
                secret=True,
            )
        ],
    )


def test_replayer_injects_secret_from_env(tmp_path, monkeypatch) -> None:
    from tests.test_replayer import FakeBackend as RBackend, FakeVision

    monkeypatch.setenv("OPENADAPT_FLOW_SECRET_PASSWORD", SECRET)
    backend = RBackend()
    bundle = tmp_path / "bundle"
    (bundle / "templates").mkdir(parents=True)
    report = Replayer(backend, vision=FakeVision()).run(
        _secret_workflow(), params={}, bundle_dir=bundle, run_dir=tmp_path / "run"
    )
    assert report.success, [r.error for r in report.results]
    assert ("type", SECRET) in backend.actions


def test_replayer_missing_secret_errors_clearly(tmp_path, monkeypatch) -> None:
    from tests.test_replayer import FakeBackend as RBackend, FakeVision

    monkeypatch.delenv("OPENADAPT_FLOW_SECRET_PASSWORD", raising=False)
    backend = RBackend()
    bundle = tmp_path / "bundle"
    (bundle / "templates").mkdir(parents=True)
    report = Replayer(backend, vision=FakeVision()).run(
        _secret_workflow(), params={}, bundle_dir=bundle, run_dir=tmp_path / "run"
    )
    assert not report.success
    (result,) = report.results
    assert "OPENADAPT_FLOW_SECRET_PASSWORD" in (result.error or "")
    # Nothing was typed: the secret never reached the backend.
    assert not any(a[0] == "type" for a in backend.actions)
