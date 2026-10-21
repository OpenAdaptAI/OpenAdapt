"""The offline Seal page: ``openadapt.seal_view`` and the viewer it ships.

The fixtures are a real quickstart run's files (tests/fixtures/seal_view).
The browser tests open the written page from ``file://`` in Chromium and
check what a reader sees: the signature verifies over the embedded report, a
changed copy does not, and the page asks the network for nothing. They run
where Playwright and its Chromium are installed (the seal-view job in
packages.yml sets OPENADAPT_SEAL_VIEW_BROWSER=1, which makes a missing
browser a failure instead of a skip).
"""

from __future__ import annotations

import base64
import gzip
import json
import os
import shutil
from pathlib import Path

import pytest

from openadapt import seal_view
from openadapt.seal_view import (
    DATA_SLOT,
    SealViewError,
    render_page,
    report_graph,
    viewer_template,
    write_seal_page,
)

FIXTURES = Path(__file__).parent / "fixtures" / "seal_view"
SIZE_SNAPSHOT = Path(__file__).parent / "snapshots" / "seal_viewer_size.json"
KEYS = json.loads((FIXTURES / "keys.json").read_text(encoding="utf-8"))
PROGRAM = json.loads((FIXTURES / "program.json").read_text(encoding="utf-8"))


@pytest.fixture()
def runs(tmp_path) -> Path:
    for name in ("run-1", "run-broken"):
        shutil.copytree(FIXTURES / name, tmp_path / name)
    return tmp_path


def _page(run_dir: Path, **kwargs) -> Path:
    kwargs.setdefault("program", PROGRAM)
    return write_seal_page(
        run_dir,
        keys=KEYS,
        about={"app": "MockMed, a synthetic clinic app", "job": "Add a triage note"},
        **kwargs,
    )


def _data(page: Path) -> dict:
    html = page.read_text(encoding="utf-8")
    start = html.index('<script type="application/json" id="seal-data">')
    start = html.index(">", start) + 1
    return json.loads(html[start : html.index("</script>", start)])


# ---------------------------------------------------------------------------
# The page and its data
# ---------------------------------------------------------------------------


def test_the_viewer_ships_in_the_package_with_a_slot_and_no_network():
    html = viewer_template()
    assert html.count(DATA_SLOT) == 1
    # The page's own policy allows no requests at all.
    assert "default-src 'none'" in html
    for remote in ("<script src", "<link", "@import", "fonts.googleapis", "cdn."):
        assert remote not in html, remote


def test_the_viewer_size_matches_its_snapshot_and_budget():
    """The shipped viewer's size, measured. A change updates the snapshot.

    UPDATE_SNAPSHOTS=1 rewrites it; the budget is packages/seal-view's
    VIEWER_BUDGET, and growing it is a decision for the pull request.
    """

    raw = viewer_template().encode("utf-8")
    measured = {
        "bytes": len(raw),
        "gzip_bytes": len(gzip.compress(raw, compresslevel=9, mtime=0)),
        "budget_bytes": 262_144,
    }
    if os.environ.get("UPDATE_SNAPSHOTS") == "1":
        SIZE_SNAPSHOT.write_text(
            json.dumps(measured, indent=2) + "\n", encoding="utf-8"
        )
    assert measured == json.loads(SIZE_SNAPSHOT.read_text(encoding="utf-8"))
    assert measured["bytes"] <= measured["budget_bytes"]
    build = (
        Path(__file__).parents[1] / "packages" / "seal-view" / "scripts" / "build.mjs"
    )
    assert f"VIEWER_BUDGET = {measured['budget_bytes']:_}" in build.read_text(
        encoding="utf-8"
    )


def test_the_page_embeds_the_exact_files(runs):
    page = _page(runs / "run-1")
    assert page == runs / "run-1" / "seal.html"
    data = _data(page)
    report = (runs / "run-1" / "report.json").read_bytes()
    assert base64.b64decode(data["report_b64"]) == report
    assert data["seal_text"] == (runs / "run-1" / "seal.json").read_text(
        encoding="utf-8"
    )
    assert data["anchor_text"] is None and data["tsa_text"] is None
    assert data["keys"] == KEYS
    assert (
        data["program"]["spec"]["bundle"]["provenance"]["content_digest"]
        == (json.loads(report)["bundle_content_digest"])
    )
    assert data["host"]["files"] == ["report.json", "seal.json"]
    # The bundle's own graph, not the fallback built from the report.
    assert data["program"]["spec"] == PROGRAM


def test_witness_files_are_embedded_only_when_the_run_has_them(runs):
    (runs / "run-1" / "seal.anchor.json").write_text("{}", encoding="utf-8")
    data = _data(_page(runs / "run-1"))
    assert data["anchor_text"] == "{}"
    assert data["tsa_text"] is None
    assert "seal.anchor.json" in data["host"]["files"]


def test_no_text_in_the_data_can_end_its_script_element():
    html = render_page(
        {"seal_text": "</script><script>alert(1)</script>"},
        template=f"<body>{DATA_SLOT}</body>",
    )
    assert html.count("</script>") == 1
    assert "\\u003c/script>" in html


def test_a_run_without_a_seal_has_no_page(runs):
    (runs / "run-1" / "seal.json").unlink()
    with pytest.raises(SealViewError, match="no seal.json"):
        _page(runs / "run-1")


def test_a_program_from_another_bundle_is_not_used(runs):
    other = json.loads(json.dumps(PROGRAM))
    other["bundle"]["provenance"]["content_digest"] = "0" * 64
    # No bundle next to the run: the program is the report's own steps.
    data = _data(_page(runs / "run-1", program=other))
    spec = data["program"]["spec"]
    assert [n["title"] for n in spec["nodes"]][:2] == [
        "click 'Open'",
        "click 'New Encounter'",
    ]
    assert (
        spec["bundle"]["provenance"]["content_digest"]
        == PROGRAM["bundle"]["provenance"]["content_digest"]
    )


def test_the_report_graph_follows_the_runs_steps():
    report = json.loads((FIXTURES / "run-broken" / "report.json").read_bytes())
    spec = report_graph(report)
    assert [n["kind"] for n in spec["nodes"]] == ["action"] * 6 + ["terminal"]
    assert [e["source"] for e in spec["edges"]] == [n["id"] for n in spec["nodes"][:-1]]


def test_seal_view_writes_a_page_per_sealed_run(runs, monkeypatch):
    from click.testing import CliRunner

    from openadapt import cli

    opened = []
    monkeypatch.setattr(seal_view, "open_page", lambda page: opened.append(page))
    keys = runs / "keys.json"
    keys.write_text(json.dumps(KEYS), encoding="utf-8")
    monkeypatch.setattr(seal_view, "find_program", lambda *a, **k: PROGRAM)
    result = CliRunner().invoke(
        cli.main, ["seal", "view", str(runs), "--keys", str(keys), "--no-open"]
    )
    assert result.exit_code == 0, result.output
    assert (runs / "run-1" / "seal.html").is_file()
    assert (runs / "run-broken" / "seal.html").is_file()
    assert f"Seal: {runs / 'run-1' / 'seal.html'}" in result.output
    assert opened == []


def test_seal_view_refuses_a_folder_with_no_sealed_run(tmp_path):
    from click.testing import CliRunner

    from openadapt import cli

    result = CliRunner().invoke(cli.main, ["seal", "view", str(tmp_path)])
    assert result.exit_code == 1
    assert "no sealed run" in result.output


# ---------------------------------------------------------------------------
# In a browser, from file://
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def browser():
    required = os.environ.get("OPENADAPT_SEAL_VIEW_BROWSER") == "1"
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        if required:
            raise
        pytest.skip("Playwright is not installed")
    with sync_playwright() as p:
        try:
            chromium = p.chromium.launch()
        except Exception as exc:  # noqa: BLE001 - no browser on this machine
            if required:
                raise
            pytest.skip(f"no Chromium for Playwright: {exc}")
        yield chromium
        chromium.close()


class Reader:
    """One page open from file://, with every request it makes recorded."""

    def __init__(self, browser, page_path: Path, *, reduced: bool = False) -> None:
        self.uri = page_path.resolve().as_uri()
        self.context = browser.new_context(
            viewport={"width": 1280, "height": 900},
            reduced_motion="reduce" if reduced else "no-preference",
        )
        self.page = self.context.new_page()
        self.requests: list[str] = []
        self.errors: list[str] = []
        self.page.on("request", lambda r: self.requests.append(r.url))
        self.page.on("pageerror", lambda e: self.errors.append(str(e)))
        self.page.goto(self.uri)

    def verdict(self, starts: str, timeout: float = 15_000) -> str:
        box = self.page.locator("[data-verdict]")
        box.filter(has_text=starts).wait_for(timeout=timeout)
        return box.inner_text()

    def outside(self) -> list[str]:
        return [url for url in self.requests if url != self.uri]

    def close(self) -> None:
        self.context.close()


def test_the_page_verifies_the_quickstarts_real_seal(browser, runs):
    reader = Reader(browser, _page(runs / "run-1"))
    try:
        text = reader.verdict("Valid")
        assert "signed by this computer's Seal key" not in text  # keys were given
        assert reader.page.locator("h1").inner_text() == "Saved and checked"
        stats = reader.page.locator("dl").inner_text()
        assert "6/6" in stats and "2/2" in stats
        # The iris and the rings are drawn from the report.
        assert reader.page.locator("[data-step]").count() >= 6
        # The full program map is on the page.
        assert reader.page.locator(".opg-node").count() >= 6
        assert reader.outside() == []
        assert reader.errors == []
    finally:
        reader.close()


def test_a_tampered_report_shows_not_valid(browser, runs):
    reader = Reader(browser, _page(runs / "run-1"))
    try:
        reader.verdict("Valid")
        reader.page.get_by_role("button", name="Tamper with it").click()
        reader.page.get_by_role("button", name="Add one AI model call").click()
        assert "subject_mismatch" in reader.verdict("Fails")
        reader.page.get_by_role("button", name="Change the outcome in the Seal").click()
        assert "bad_signature" in reader.verdict("Fails. The Ed25519")
        reader.page.get_by_role("button", name="Restore and verify").click()
        reader.verdict("Valid")
        assert reader.outside() == []
    finally:
        reader.close()


def test_a_report_changed_on_disk_fails_when_the_page_opens(browser, runs):
    report = runs / "run-1" / "report.json"
    report.write_bytes(
        report.read_bytes().replace(b'"model_calls": 0', b'"model_calls": 1', 1)
    )
    reader = Reader(browser, _page(runs / "run-1"))
    try:
        assert "subject_mismatch" in reader.verdict("Fails")
        assert reader.outside() == []
    finally:
        reader.close()


def test_the_stopped_run_verifies_and_replays_to_the_stop(browser, runs):
    reader = Reader(browser, _page(runs / "run-broken"), reduced=True)
    try:
        reader.verdict("Valid")
        assert reader.page.locator("h1").inner_text() == "Stopped for a person to check"
        assert "1 failed" in reader.page.locator("dl").inner_text()
        reader.page.get_by_role("button", name="Next highlight").click()
        line = reader.page.locator("p[data-step]").first
        # The stop is the last highlight; step through to it.
        for _ in range(6):
            if line.inner_text().upper().startswith("STEP 6 OF 6"):
                break
            reader.page.get_by_role("button", name="Next highlight").click()
        assert line.inner_text().upper().startswith("STEP 6 OF 6")
        # The map follows the replay.
        selected = reader.page.locator(".opg-node[data-selected]").first
        assert "Click Save Encounter" in (selected.get_attribute("aria-label") or "")
        assert reader.outside() == []
    finally:
        reader.close()


def test_the_map_shows_the_sealed_step_and_goes_back_to_the_seal(browser, runs):
    reader = Reader(browser, _page(runs / "run-1"))
    try:
        reader.verdict("Valid")
        reader.page.locator("#map-h").scroll_into_view_if_needed()
        reader.page.locator(".opg-node", has_text="Click Save Encounter").first.click()
        card = reader.page.locator("[data-seal-card]")
        card.wait_for()
        assert "This is the step the Seal certifies" in card.text_content()
        assert "2 of 2 read back · signature valid" in card.inner_text()
        reader.page.get_by_role("button", name="Back to the Seal").click()
        stage = reader.page.locator('section[aria-label="The Seal"]')
        assert stage.get_attribute("data-pulse") in ("1", "2")
        assert reader.outside() == []
    finally:
        reader.close()


def test_a_page_with_no_run_says_how_to_make_one(browser, tmp_path):
    empty = tmp_path / "seal.html"
    empty.write_text(viewer_template(), encoding="utf-8")
    reader = Reader(browser, empty)
    try:
        reader.page.get_by_text("openadapt seal view RUN_FOLDER").first.wait_for()
        assert reader.outside() == []
    finally:
        reader.close()
