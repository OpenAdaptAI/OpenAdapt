"""compile_agent records an agent's run through the real RDP and Citrix backends.

MockMed runs in a browser that Flow never reads: tests/surface_kiosk presents
the page to ``RemoteDisplayBackend`` (``rdp``, local client window) and
``CitrixWorkspaceBackend`` (``citrix``) as a remote-desktop client window, so
Flow sees only its pixels, as it would over RDP or Citrix. The agent replays
the recorded Anthropic trajectory used by tests/e2e/test_jit_cua_e2e.py.

What it checks:

* a cache miss records through the surface backend and compiles a DRAFT bound
  to that surface, with the typed note lifted to a parameter and the save's
  record-written effect mined from the system of record;
* the canary is refused by the standard run gate before any input reaches the
  window, because a pixel-only click carries no verified record identity.
  ``run --profile standard`` refuses the same bundle the same way; see
  docs/BACKEND_PARITY.md.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator

import pytest

pytest.importorskip("playwright.sync_api")

from openadapt_flow.jit import SurfaceApp, TaskSignature, compile_agent  # noqa: E402
from openadapt_flow.jit.recorded import RecordedTrajectoryAgent  # noqa: E402
from openadapt_flow.mockmed.fault_server import serve  # noqa: E402
from openadapt_flow.runtime.effects import RestRecordVerifier  # noqa: E402
from openadapt_flow.tutorial import _http_json, _records  # noqa: E402
from tests.surface_kiosk import PageWindowClient  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "jit"
TASK = TaskSignature(
    "mockmed.add_note",
    "Open the first referral, start a Triage encounter, and save this note: {note}",
    {"note": str},
)
TITLE = "MockMed kiosk"
SECTIONS = {
    "rdp": {"kind": "rdp", "rdp_window": "mstsc", "rdp_window_title": TITLE},
    "citrix": {
        "kind": "citrix",
        "rdp_window": "Citrix Viewer",
        "rdp_window_title": TITLE,
        "rdp_readiness_text": "MockMed",
    },
}


@pytest.fixture(scope="module")
def mockmed() -> Iterator[str]:
    base, _db, stop = serve()
    yield base.rstrip("/")
    stop()


@pytest.fixture(scope="module")
def browser() -> Iterator[Any]:
    from playwright.sync_api import sync_playwright

    from openadapt_flow._browser_setup import launch_chromium

    with sync_playwright() as pw:
        try:
            chromium = launch_chromium(pw.chromium, headless=True)
        except Exception as exc:  # noqa: BLE001 - no browser on this host
            pytest.skip(f"chromium unavailable: {exc}")
        yield chromium
        chromium.close()


def _opener(browser: Any, base: str, clients: list[PageWindowClient]):
    from openadapt_flow.backends.citrix_workspace import CitrixWorkspaceBackend
    from openadapt_flow.backends.remote_display import RemoteDisplayBackend

    def open_backend(cfg: Any) -> Any:
        page = browser.new_page(
            viewport={"width": 1280, "height": 800}, device_scale_factor=1
        )
        page.goto(f"{base}/?fault=ok&idempotency=demo#tasks")
        page.wait_for_timeout(300)
        client = PageWindowClient(page, owner=cfg.rdp_window, title=TITLE)
        clients.append(client)
        if cfg.kind == "citrix":
            backend = CitrixWorkspaceBackend(
                client,
                owner_substr=cfg.rdp_window,
                window_title=cfg.rdp_window_title,
                readiness_text=cfg.rdp_readiness_text,
                max_frame_age_s=30.0,
            )
        else:
            backend = RemoteDisplayBackend(
                client,
                owner_substr=cfg.rdp_window,
                title_substr=cfg.rdp_window_title,
                max_frame_age_s=30.0,
            )
        backend.close = page.close  # type: ignore[attr-defined]
        return backend

    return open_backend


@pytest.mark.timeout(300)
@pytest.mark.parametrize("kind", ["rdp", "citrix"])
def test_an_agent_run_on_a_remote_window_compiles_and_fails_closed(
    mockmed: str, browser: Any, tmp_path: Path, kind: str
) -> None:
    clients: list[PageWindowClient] = []
    app = SurfaceApp(
        SECTIONS[kind],
        record_reader=lambda: _records(mockmed),
        reset=lambda: _http_json(f"{mockmed}/api/reset", method="POST", body={}),
        open_backend=_opener(browser, mockmed, clients),
    )
    agent = RecordedTrajectoryAgent.from_file(FIXTURES / "anthropic_add_note.json")
    jit = compile_agent(
        agent,
        task=TASK,
        app=app,
        verifier=RestRecordVerifier(
            mockmed,
            records_path="/api/db",
            records_key="records",
            timeout_s=2.0,
            poll_interval_s=0.05,
        ),
        policy="clinical-write",
        cache_dir=tmp_path / "cache",
        key_dir=tmp_path / "keys",
        on_warning=lambda text: None,
    )

    miss = jit.run(note="Synthetic follow-up in two weeks")
    assert miss.status == "needs_human_admit", miss.detail
    assert agent.calls == 1 and len(clients) == 1 and clients[0].inputs > 0
    candidate = miss.candidate
    assert candidate is not None
    assert [p["name"] for p in candidate["params"]["lifted"]] == ["note"]
    assert [e["kind"] for e in candidate["effects"]] == ["record_written"]

    workflow = json.loads((jit.bundle_dir / "workflow.json").read_text())
    assert workflow["surface"] == kind
    assert workflow["backend_hints"]["rdp_window_title"] == TITLE
    meta = json.loads((Path(candidate["recording"]) / "meta.json").read_text())
    assert meta["app_url"] is None and meta["surface"] == kind
    assert meta["demonstrated_by"] == "agent"
    assert meta["authoring"] == {"input": "remote_client_window", "screen_only": True}

    confirmed = jit.confirm_params(
        principal_id="Dana Reviewer", accept_params=["note"], confirm="ok"
    )
    jit.review(
        principal_id="Dana Reviewer", reviewed_steps=confirmed["steps"], confirm="ok"
    )
    saved = _records(mockmed)
    canary = jit.canary(principal_id="Dana Reviewer", note="Canary test note")
    assert canary.status == "refused"
    assert "Identity coverage" in (canary.detail or "")
    assert "Nothing was executed" in (canary.detail or "")
    # The gate refused before a backend opened: no second window, no input.
    assert len(clients) == 1
    assert _records(mockmed) == saved
    assert canary.inbox_item is not None and canary.inbox_item.is_file()
