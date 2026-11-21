"""Wrap a task that starts with a sign-in, in headless Chromium.

The target is a synthetic notes app behind a sign-in form
(:mod:`tests.e2e._sign_in_app`). A scripted agent signs in and saves a note
through the real ``openadapt wrap`` path: the task loader, SkillCache, the
first-run compiler, a person's check confirmation, the canary, a registered
admitter key, and then a cached run with no model calls. It asserts:

* the password field and the username typed before it become secret
  parameters of the program, found from the page alone (field type and form
  order), with no task-file hint;
* the program, the recording, the runs, the receipts and the local ledgers
  hold neither value, as text or in any file; the frames of both fields are
  blacked out;
* the canary and the cached run sign in with values supplied only in memory
  and still read their note back VERIFIED.
"""

from __future__ import annotations

import io
import json
import re
from pathlib import Path
from typing import Any, Iterator

import pytest

from openadapt_flow.integrations.agents.recording import load_task
from openadapt_flow.integrations.agents.skills import FrameworkAgent
from openadapt_flow.ir import Workflow
from openadapt_flow.jit.keys import CustomerModelKey
from openadapt_flow.wrap import WrappedTask
from tests.e2e._sign_in_app import serve

pytest.importorskip("playwright")

#: High-entropy synthetic values, so a match anywhere is a real leak.
USERNAME = "synthuser-7q3k9w2m"
PASSWORD = "Synth-Pw-x4K9qZ7mT2vR8nB5"
TEST_KEY = CustomerModelKey("anthropic", "env:TEST_ONLY", "not-a-real-key")
INSTRUCTION = (
    "Sign in as {username} with password {password}, then add a note with "
    "the text {note}"
)


@pytest.fixture
def app() -> Iterator[tuple[str, Any]]:
    base, state, stop = serve(USERNAME, PASSWORD)
    yield base, state
    stop()


def _ref(listing: str, name: str) -> str:
    for line in listing.splitlines():
        found = re.search(r"\[(e\d+)\] \S+ \"" + re.escape(name) + '"', line)
        if found:
            return found.group(1)
    raise AssertionError(f"no {name!r} in listing:\n{listing}")


_TASK = re.compile(
    r"Sign in as (?P<username>\S+) with password (?P<password>\S+), then add a "
    r"note with the text (?P<note>.+)\Z"
)


def _inputs(task: str) -> dict[str, str]:
    """What the model was told for each input: its placeholder when text
    scrubbing works, otherwise the value itself."""

    found = _TASK.search(task)
    assert found, task
    return found.groupdict()


def _agent(seen: list[str]) -> FrameworkAgent:
    def run(task: str, tools: Any) -> dict[str, Any]:
        seen.append(task)
        told = _inputs(task)
        listing = tools.observe()
        seen.append(listing)
        listing = tools.type_text(told["username"], _ref(listing, "Username"))
        listing = tools.type_text(told["password"], _ref(listing, "Password"))
        listing = tools.click(_ref(listing, "Sign in"))
        seen.append(listing)
        listing = tools.type_text(told["note"], _ref(listing, "Note"))
        listing = tools.click(_ref(listing, "Save"))
        seen.append(listing)
        tools.finish("saved")
        return {
            "calls": [
                {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "total_tokens": 0,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                }
            ]
        }

    return FrameworkAgent(run, principal_id="agent:scripted-sign-in", model="scripted")


def _admitter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Path]:
    from openadapt_flow.jit import authority

    monkeypatch.setenv(authority.KEY_PASSPHRASE_ENV, "e2e passphrase")
    key_dir = tmp_path / "keys"
    pem = tmp_path / "keyfiles" / "ci.pem"
    authority.register_principal(
        key_dir,
        role="admitter_key",
        principal_id="ci:sign-in",
        public_key_hex=authority.generate_encrypted_key(pem),
    )
    return authority.principal_from_key(
        authority.load_encrypted_key(pem), key_dir
    ), key_dir


def _authorize(
    cache: Any,
    instruction: str,
    values: dict[str, str],
    base: str,
    key_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A person signs what run 1 may do. The sign-in inputs are named by
    digest: the statement never spells a credential out."""

    from openadapt_flow import teach_authorization as ta
    from openadapt_flow.approval_mode.approver_roster import ROOT_ENV
    from openadapt_flow.teach_authorization_fixture import LocalAuthorizer
    from openadapt_flow.teach_scope import origin_of

    task_id = cache.task_id(instruction, values)
    # Machine handles, as an unattended publish needs (no person identifier
    # to replace in the hosted copy of the admission).
    root = LocalAuthorizer(key_dir, "ci:sign-in-roster-admin")
    signer = LocalAuthorizer(key_dir, "ci:sign-in-teach-lead", added_by=root)
    monkeypatch.setenv(ROOT_ENV, signer.root)
    statement = signer.issue(
        key_dir.parent / f"teach-authorization-{task_id}",
        {
            "task": {"task_id": task_id, "summary": "Sign in and add a note"},
            "purposes": ["teach", "repair"],
            "origins": [str(origin_of(base))],
            "inputs": {
                "username": {"sha256": ta.value_sha256(values["username"])},
                "password": {"sha256": ta.value_sha256(values["password"])},
                "note": {"value": values["note"]},
            },
            "constants": [{"value": "Notes"}],
            "writes": {
                "record_kind": "note",
                "identity": {"list": {"value": "demo"}},
                "fields": {"note": {"input": "note"}},
            },
        },
    )
    cache.common["teach_authorization"] = signer.authority(statement)


def _why(root: Path) -> str:
    """The first run's own result files, for a failure message."""

    return "\n".join(
        path.read_text()[:2000] for path in sorted(root.rglob("result.json"))
    )


def _leaks(root: Path, values: list[str]) -> list[str]:
    """Every file under ``root`` that holds a value, in any common encoding."""

    import base64
    import hashlib

    from openadapt_flow import teach_authorization as ta

    needles: list[bytes] = []
    for value in values:
        raw = value.encode()
        needles += [
            raw,
            raw.hex().encode(),
            base64.b64encode(raw).rstrip(b"="),
            hashlib.sha256(raw).hexdigest().encode(),
            ta.value_sha256(value).encode(),
            json.dumps(value).encode()[1:-1],
        ]
    hits = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink():
            data = path.read_bytes()
            if any(needle in data for needle in needles):
                hits.append(str(path.relative_to(root)))
    return hits


def test_a_sign_in_becomes_secret_parameters_and_never_persists(
    app: tuple[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    base, state = app
    admitter, key_dir = _admitter(tmp_path, monkeypatch)
    task_file = tmp_path / "task.json"
    task_file.write_text(
        json.dumps(
            {
                "schema": "openadapt.wrap-task/v1",
                "instruction": INSTRUCTION,
                "parameters": ["username", "password", "note"],
                "url": base,
                "verifier": {
                    "kind": "rest",
                    "base_url": base.rstrip("/"),
                    "records_path": "/api/notes",
                    "records_key": "records",
                    "records_complete": True,
                },
                "expect": {"list": "demo"},
                "on_screen": ["Notes"],
                "canary_params": [
                    {
                        "username": USERNAME,
                        "password": PASSWORD,
                        "note": "sign-in canary note",
                    }
                ],
                "cache_dir": str(tmp_path / "cache"),
            }
        )
    )
    setup = load_task(
        task_file,
        offer=lambda proposal: "ci:sign-in-reviewer",
        admitter=admitter,
        key_dir=key_dir,
        model_key=TEST_KEY,
    )
    first_values = {
        "username": USERNAME,
        "password": PASSWORD,
        "note": "first sign-in note",
    }
    _authorize(setup.cache, setup.instruction, first_values, base, key_dir, monkeypatch)
    seen: list[str] = []
    task = WrappedTask(
        cache=setup.cache,
        agent=_agent(seen),
        instruction=setup.instruction,
        parameters=setup.parameters,
        runs_dir=tmp_path / "runs",
    )
    first = task.call("first-run", first_values)
    assert first["status"] == "admitted" and first["verified"] is True, _why(tmp_path)
    from openadapt_flow.privacy import text_scrubbing_works

    if text_scrubbing_works():
        # The model never saw either value: it typed placeholders.
        assert not any(USERNAME in text or PASSWORD in text for text in seen)
    # The password field's value never reaches a listing either way.
    assert not any(PASSWORD in text for text in seen[1:])

    program = setup.cache.admitted_program(
        setup.instruction, dict.fromkeys(setup.parameters, "")
    )
    assert program is not None
    workflow = Workflow.load(Path(program["bundle"]))
    assert sorted(workflow.secret_params) == ["password", "username"]
    assert sorted(workflow.params) == ["note"]
    secret_steps = [s for s in workflow.steps if s.secret]
    assert sorted(s.param for s in secret_steps) == ["password", "username"]
    assert all(s.text is None for s in secret_steps)

    _assert_nothing_holds_a_credential(tmp_path, capfd)
    _assert_fields_blacked_out(Path(program["task_dir"]))
    _assert_no_pixel_check_froze_a_black_box(Path(program["bundle"]), workflow)

    # What `openadapt wrap publish` would upload holds no credential either,
    # and names the sign-in inputs as secret parameters, not call inputs.
    from openadapt_flow.wrap.publish import stage

    staged = stage(setup.cache, setup.instruction, setup.parameters, key_dir=key_dir)
    assert staged.manifest["parameters"] == ["note"]
    assert staged.manifest["secret_parameters"] == ["password", "username"]
    assert staged.manifest["input_schema"]["required"] == ["note"]
    assert _leaks(staged.staged, [USERNAME, PASSWORD]) == []
    # The local, person-signed teach statement stays local. The independently
    # checked hosted admission is the authority exported with the program.
    assert not (staged.staged / "teach-authorization").exists()

    # A cached run: no model, the values only in memory, read back VERIFIED.
    # mcp 1.x runs the emitted server's sync tool on its event loop, where
    # Playwright's sync API refuses to start (a limit of every cached web
    # task, not of sign-in); mcp 2 runs it in a worker thread.
    from importlib.metadata import version

    if int(version("mcp").split(".")[0]) < 2:
        pytest.skip("the cached web run needs mcp 2 (sync tools run in a thread)")
    second = task.call(
        "second-run",
        {"username": USERNAME, "password": PASSWORD, "note": "second sign-in note"},
    )
    assert second.get("path") == "skill", second
    assert second["verified"] is True and second["model_calls"] == 0, json.dumps(second)
    assert second["receipt_outcome"] == "VERIFIED", second
    assert [r["note"] for r in state.records][-1] == "second sign-in note"
    assert state.sign_ins >= 3  # run 1, the canary and the cached run

    _assert_nothing_holds_a_credential(tmp_path, capfd)


def _assert_no_pixel_check_froze_a_black_box(bundle: Path, workflow) -> None:
    """A REGION_STABLE crop over a withheld field would hold the recorder's
    black box, which no live screen shows, and refute every hosted run."""
    import numpy as np
    from PIL import Image

    for step in workflow.steps:
        for pc in step.expect:
            if pc.kind.value != "region_stable" or not pc.template:
                continue
            pixels = np.asarray(Image.open(bundle / pc.template).convert("L"))
            assert (pixels <= 8).mean() < 0.05, (step.id, pc.template)


def _assert_nothing_holds_a_credential(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    out, err = capfd.readouterr()
    assert USERNAME not in out + err and PASSWORD not in out + err
    from PIL import Image

    for path in tmp_path.rglob("steps/*.png"):
        with Image.open(path) as frame:
            assert frame.convert("RGB").getextrema() == ((0, 0), (0, 0), (0, 0)), path
    # What may hold a credential: the operator's own task file (its canary
    # values), and the person-signed teach authorization, which names the
    # sign-in inputs by digest and never leaves this machine.
    allowed = {"task.json"}
    leaks = set(_leaks(tmp_path, [USERNAME, PASSWORD])) - allowed
    assert leaks and all(
        Path(path).name == "teach-authorization.json" for path in leaks
    ), sorted(leaks)


def _assert_fields_blacked_out(task_dir: Path) -> None:
    """Each sign-in field is black in every saved frame of the sign-in page
    from the moment it was typed into."""

    from PIL import Image

    events = [
        json.loads(line)
        for path in task_dir.rglob("recording/events.jsonl")
        for line in path.read_text().splitlines()
        if line.strip()
    ]
    secret = [e for e in events if e.get("secret")]
    assert sorted(e["credential"] for e in secret) == ["password", "username"]
    assert all("text" not in e for e in secret)
    checked = 0
    for frames in task_dir.rglob("recording/frames"):
        for event in events:
            for side in ("before", "after"):
                if event.get(f"title_{side}") != "Notes sign in":
                    continue
                path = frames / f"{event['i']:04d}_{side}.png"
                with Image.open(io.BytesIO(path.read_bytes())) as image:
                    rgb = image.convert("RGB")
                    # Once a field holds a credential, every frame of the
                    # sign-in page has it blacked out.
                    for typed in secret:
                        if typed["i"] > event["i"]:
                            continue
                        x, y, w, h = typed["redacted_region"]
                        crop = rgb.crop((x, y, x + w, y + h))
                        assert crop.getextrema() == ((0, 0), (0, 0), (0, 0)), path
                checked += 1
    # Run 1 shows the form in the clicks and typing before the sign-in.
    assert checked >= 8
