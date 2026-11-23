"""Opt-in fallback end to end on MockMed, headless.

A scripted agent's run 1 is compiled and admitted with a registered key.
Then MockMed drifts (a vendor release relabels and moves a control). The
compiled program halts before the save; with the opt-in policy the task goes
to a scripted fallback agent that drives the drifted screens through the
compile-agent harness, a gate stands in for the person, and the saved record
is read from MockMed's own store. Until #2406 moves the MCP tool onto a
worker thread (needed under mcp 1.30.0), the compiled program runs in
process, on a thread, instead of over MCP.
"""

from __future__ import annotations

import json
import shutil
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator

import pytest

from openadapt_flow.compile_agent import ScriptedAgent
from openadapt_flow.integrations.agents import skills
from openadapt_flow.integrations.agents.skills import SkillCache
from openadapt_flow.jit.keys import CustomerModelKey
from openadapt_flow.tutorial import _http_json, _records
from tests._rest_read_contract_fixtures import mockmed_effects_yaml
from tests._teach_authorization_support import mockmed_note

pytest.importorskip("playwright")

PROMPT = (
    "Open the referral for Jane Sample, start a Triage encounter, and save "
    "this note: {note}"
)
NOTE = "Synthetic follow-up in two weeks"
EXPECT = {"patient_id": "p1", "type": "Triage"}
ON_SCREEN = ["Jane Sample", "MRN P1"]
TEST_KEY = CustomerModelKey("anthropic", "env:TEST_ONLY", "not-a-real-key")
DRIFT_HOOK = "(new URLSearchParams(location.search).get('drift') || '')"


class DriftableMockMed:
    """MockMed's fault server over a copy of its static files.

    The app reads ``?drift=`` from its own URL, and the URL is part of the
    task's key, so the drift is switched in the served script instead.
    """

    def __init__(self, root: Path) -> None:
        from openadapt_flow.mockmed import fault_server
        from openadapt_flow.mockmed.chart_pages import ChartPages

        self.static = root / "static"
        shutil.copytree(fault_server.STATIC_DIR, self.static)
        self.app_js = (self.static / "app.js").read_text(encoding="utf-8")
        assert DRIFT_HOOK in self.app_js
        db = fault_server.FaultDB()
        chart = ChartPages(rows_for=db.rows_for_patient)
        db.chart = chart
        handler = fault_server._make_handler(db, str(self.static), chart)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def drift(self, mode: str) -> None:
        text = self.app_js.replace(DRIFT_HOOK, f"({json.dumps(mode)} || '')")
        (self.static / "app.js").write_text(text, encoding="utf-8")

    def reset(self) -> None:
        _http_json(f"{self.base}/api/reset", method="POST", body={})

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


@pytest.fixture
def app(tmp_path: Path) -> Iterator[DriftableMockMed]:
    server = DriftableMockMed(tmp_path / "app")
    yield server
    server.stop()


def steps(save: str = "Save Encounter", new: str = "New Encounter") -> list:
    return [
        {"click": "Open", "near": "Jane Sample"},
        {"click": new},
        {"click": "Triage"},
        {"type": "{note}", "into": "Note"},
        {"click": save},
    ]


def make_cache(tmp_path: Path, app: DriftableMockMed, env: Any) -> SkillCache:
    from openadapt_flow.jit import authority

    env.setenv(authority.KEY_PASSPHRASE_ENV, "e2e passphrase")
    key_dir = tmp_path / "keys"
    pem = tmp_path / "keyfiles" / "ci.pem"
    authority.register_principal(
        key_dir,
        role="admitter_key",
        principal_id="ci:wrap-fallback",
        public_key_hex=authority.generate_encrypted_key(pem),
    )
    admitter = authority.principal_from_key(authority.load_encrypted_key(pem), key_dir)
    verify = tmp_path / "verify.yaml"
    verify.write_text(
        "verifier:\n"
        f"{mockmed_effects_yaml(app.base)}"
        "  timeout_s: 2.0\n"
        "  poll_interval_s: 0.05\n"
    )
    cache = SkillCache(
        url=f"{app.base}/?fault=ok&idempotency=demo#tasks",
        verify=verify,
        expect=EXPECT,
        on_screen=ON_SCREEN,
        offer=lambda proposal: "dana.reviewer",
        admitter=admitter,
        canary_params=[{"note": "Canary test note"}],
        drop_fields=["key", "source"],
        task_id="wrap.fallback",
        cache_dir=tmp_path / "cache",
        key_dir=key_dir,
        model_key=TEST_KEY,
        reset=app.reset,
        on_warning=lambda text: None,
    )
    task_id = cache.task_id(PROMPT, {"note": NOTE})
    cache.common["teach_authorization"] = mockmed_note(
        key_dir,
        tmp_path / f"teach-authorization-{task_id}",
        task_id,
        cache.common["url"],
        note=NOTE,
        record=EXPECT,
        constants=ON_SCREEN,
    )
    return cache


def in_process(patch: Any, cache: SkillCache) -> None:
    from openadapt_flow.compile_agent import run_compiled_params

    async def call(_python, _server, params):
        import asyncio

        task_dir = cache.receipt_root(PROMPT, params)
        # Playwright's sync API refuses a running event loop (#2406 moves the
        # MCP tool to a worker thread the same way).
        result = await asyncio.to_thread(run_compiled_params, task_dir, params)
        return result.to_dict()

    patch.setattr(skills, "_call_skill", call)


#: What the scripted fallback agent clicks on each drifted release.
DRIFTED_STEPS = {
    "redesign": steps(save="Submit to chart"),
    "relink": steps(new="Start visit"),
}


class Gate:
    """Stands in for the person at the pre-commit gate."""

    def __init__(self, approve: bool) -> None:
        self.approve = approve
        self.requests: list = []

    def __call__(self, request: Any) -> bool:
        self.requests.append(request)
        return self.approve


def admitted(tmp_path: Path, app: DriftableMockMed, patch: Any) -> SkillCache:
    cache = make_cache(tmp_path, app, patch)
    in_process(patch, cache)
    app.reset()
    first = cache.run(PROMPT, {"note": NOTE}, ScriptedAgent(steps=steps()))
    assert first.status == "admitted", first.to_dict()
    app.reset()
    return cache


def saved(app: DriftableMockMed) -> list[tuple[str, str, str]]:
    return [(r["patient_id"], r["type"], r["note"]) for r in _records(app.base)]


@pytest.mark.parametrize("mode", sorted(DRIFTED_STEPS))
def test_drift_falls_back_and_the_save_is_read_back(
    app, tmp_path, monkeypatch, mode
) -> None:
    from openadapt_flow.agent.repair import RepairAccessPolicy, RepairDesk
    from openadapt_flow.wrap.fallback import FALLBACK_PATH, FallbackPolicy

    cache = admitted(tmp_path, app, monkeypatch)
    app.drift(mode)
    gate = Gate(approve=True)
    agent = ScriptedAgent(steps=DRIFTED_STEPS[mode])
    policy = FallbackPolicy(
        cache,
        max_cost_usd=0.5,
        gate=gate,
        request_id=f"e2e-{mode}.fallback",
        repair_desk=RepairDesk(tmp_path / "repairs", RepairAccessPolicy(None)),
    )
    run = policy.run(PROMPT, {"note": NOTE}, agent)
    assert run.path == FALLBACK_PATH and run.verified, run.to_dict()
    assert run.status == "VERIFIED" and run.model_calls == 0
    # The program halted before the save; the agent saved once, approved once.
    assert run.replay["program"]["status"] == "HALTED"
    assert len(gate.requests) == 1 and policy.last["approvals"] == 1
    assert saved(app) == [("p1", "Triage", NOTE)]
    # Nothing was admitted: the task is still held and serves no program.
    assert policy.last["admitted"] is False
    assert cache.held(PROMPT, {"note": ""})
    assert cache.server(PROMPT, {"note": ""}) is None
    repair = policy.last["repair"]
    assert repair["opened"] is True and Path(repair["file"]).is_file(), repair


def test_gate_refusal_saves_nothing(app, tmp_path, monkeypatch) -> None:
    from openadapt_flow.wrap.fallback import FallbackPolicy

    cache = admitted(tmp_path, app, monkeypatch)
    app.drift("redesign")
    gate = Gate(approve=False)
    policy = FallbackPolicy(cache, max_cost_usd=0.5, gate=gate)
    run = policy.run(
        PROMPT, {"note": NOTE}, ScriptedAgent(steps=DRIFTED_STEPS["redesign"])
    )
    assert run.status == "GATE_REFUSED" and not run.verified
    assert len(gate.requests) == 1
    assert saved(app) == []  # read from MockMed's store


def test_without_the_policy_the_drift_is_held(app, tmp_path, monkeypatch) -> None:
    cache = admitted(tmp_path, app, monkeypatch)
    app.drift("redesign")
    agent = ScriptedAgent(steps=DRIFTED_STEPS["redesign"])
    first = cache.run(PROMPT, {"note": NOTE}, agent)
    again = cache.run(PROMPT, {"note": NOTE}, agent)
    assert first.status == "HALTED" and again.status == "CACHE_HALTED"
    assert not first.verified and not again.verified
    assert saved(app) == []
