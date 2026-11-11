"""The write leaderboard's scorer and browser boundary, offline.

benchmark/fhir_medplum/leaderboard: the scored board must reproduce from the
committed rows, a bootstrap draw must be its own case unit, and the agent's
browser must not navigate off the app.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from benchmark.fhir_medplum.leaderboard import score
from benchmark.fhir_medplum.leaderboard.browser import off_app, origin

RESULTS = (
    Path(__file__).resolve().parents[1]
    / "benchmark"
    / "fhir_medplum"
    / "leaderboard"
    / "results"
)


def _row(case: str, ok: bool) -> dict[str, Any]:
    return {
        "id": case,
        "class": "clean",
        "type": "patient",
        "expect": "create",
        "claim": {"status": "done"},
        "oracle": {"verdict": "correct" if ok else "wrong", "changes": {}},
    }


# -- bootstrap: each draw is its own case ---------------------------------------------


def test_pass_hat_k_interval_keeps_drawn_cases_apart() -> None:
    # The reviewer's control: two cases, each with two passes and one failure.
    # Neither case passes all three runs, so pass^3 is 0 in every resample.
    # Pooling a case drawn twice made it 4 of 6 and gave an upper bound of 0.2.
    rows = [_row(c, ok) for c in ("A", "B") for ok in (True, True, False)]
    assert score.pass_hat_k(rows, 3) == 0.0
    assert score.bootstrap(rows, lambda s: score.pass_hat_k(s, 3)) == [0.0, 0.0]


def test_pass_hat_k_interval_all_pass_and_all_fail() -> None:
    passing = [_row(c, True) for c in ("A", "B") for _ in range(3)]
    failing = [_row(c, False) for c in ("A", "B") for _ in range(3)]
    assert score.bootstrap(passing, lambda s: score.pass_hat_k(s, 3)) == [1.0, 1.0]
    assert score.bootstrap(failing, lambda s: score.pass_hat_k(s, 3)) == [0.0, 0.0]


def test_bootstrap_leaves_the_rows_unchanged() -> None:
    rows = [_row(c, ok) for c in ("A", "B") for ok in (True, False, True)]
    before = json.dumps(rows, sort_keys=True)
    score.bootstrap(rows, lambda s: score.pass_hat_k(s, 3))
    assert json.dumps(rows, sort_keys=True) == before


@pytest.mark.skipif(
    not (RESULTS / "LEADERBOARD.json").exists(), reason="results not in this checkout"
)
def test_committed_board_reproduces_from_the_rows() -> None:
    committed = json.loads((RESULTS / "LEADERBOARD.json").read_text())
    rebuilt = json.loads(json.dumps(score.board(RESULTS)))
    assert rebuilt == committed
    assert (RESULTS / "LEADERBOARD.md").read_text() == score.render(committed) + "\n"


# -- browser boundary ------------------------------------------------------------------


def test_off_app_matcher() -> None:
    match = off_app(["http://127.0.0.1:9801/", "http://127.0.0.1:9800"])
    assert origin("HTTP://LocalHost:9801/x?y") == "http://localhost:9801"
    assert not match("http://127.0.0.1:9801/Patient/1")
    assert not match("http://127.0.0.1:9800/fhir/R4/Patient")
    assert match("http://127.0.0.1:98010/")
    assert match("http://127.0.0.1:9802/")
    assert match("https://127.0.0.1:9801/")
    assert match("http://127.0.0.1:9801@evil.example/")
    assert match("https://www.example.org/")
    assert not match("about:blank")
    assert not match("data:text/html,hi")


class _Site:
    """A loopback origin that counts the requests it serves."""

    def __init__(self, body: str) -> None:
        self.hits: list[str] = []
        site = self

        class H(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                site.hits.append(self.path)
                data = site.body.encode()
                self.send_response(200)
                self.send_header("content-type", "text/html")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a: Any) -> None:
                pass

        self.body = body
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def sites():
    pytest.importorskip("playwright.sync_api")
    other = _Site("<p>other origin</p>")
    app = _Site("")
    app.body = (
        f'<a id="out" href="{other.url}leak" style="display:block;width:300px;height:40px">out</a>'
        '<a id="in" href="/inside" style="display:block;width:300px;height:40px">in</a>'
        f'<button id="pop" onclick="window.open(\'{other.url}popup\')" '
        'style="display:block;width:300px;height:40px">pop</button>'
    )
    yield app, other
    app.close()
    other.close()


@pytest.fixture
def screen_for(sites):
    from benchmark.fhir_medplum.leaderboard.browser import MedplumBrowser

    app, _ = sites
    b = MedplumBrowser(app.url, app.url.rstrip("/"))
    try:
        b.start()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"chromium unavailable: {exc}")
    b.state = {"cookies": [], "origins": []}
    ctx, screen = b.open(app.url + "fhir/R4")
    yield screen
    ctx.close()
    b.close()


def _center(screen: Any, sel: str) -> tuple[float, float]:
    box = screen.page.locator(sel).bounding_box()
    return box["x"] + box["width"] / 2, box["y"] + box["height"] / 2


def test_click_on_an_off_app_link_never_leaves_the_browser(sites, screen_for) -> None:
    app, other = sites
    screen = screen_for
    screen.click(*_center(screen, "#out"))
    screen.page.wait_for_timeout(500)
    assert other.hits == []
    assert any(e["a"] == "blocked_navigation" for e in screen.log)
    assert origin(screen.url()) != origin(other.url)


def test_keyboard_navigation_off_app_is_blocked(sites, screen_for) -> None:
    _, other = sites
    screen = screen_for
    screen.page.focus("#out")
    screen.key(["Enter"])
    screen.page.wait_for_timeout(500)
    assert other.hits == []
    assert any(e["a"] == "blocked_navigation" for e in screen.log)


def test_popup_to_another_origin_is_blocked(sites, screen_for) -> None:
    _, other = sites
    screen = screen_for
    screen.click(*_center(screen, "#pop"))
    screen.page.wait_for_timeout(800)
    assert other.hits == []


def test_goto_another_origin_is_refused(sites, screen_for) -> None:
    _, other = sites
    screen = screen_for
    with pytest.raises(ValueError):
        screen.goto(other.url + "direct")
    screen.page.evaluate(f"location.href = {json.dumps(other.url + 'script')}")
    screen.page.wait_for_timeout(500)
    assert other.hits == []


def test_same_app_navigation_still_works(sites, screen_for) -> None:
    app, other = sites
    screen = screen_for
    screen.click(*_center(screen, "#in"))
    screen.page.wait_for_timeout(300)
    assert screen.url() == app.url + "inside"
    screen.goto(app.url + "again")
    assert "/again" in app.hits
    assert other.hits == []
