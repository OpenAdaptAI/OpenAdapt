"""Interruption classes end to end: a real browser, the real replayer and OCR.

A local synthetic app saves a note with ``POST /save``. Before the save, it
shows one interruption, chosen by the page address. The server counts every
request it receives, so each case can tell a cleared interruption from a
halted run and a blocked write from one that reached the server.

Pre-registered cases (``docs/INTERRUPTION_RECOVERY.md``):

=============  ===============================================  ============
interruption   what its control does                            expected
=============  ===============================================  ============
session        "Stay signed in" removes the dialog (GET ping)   saved
banner         "Close" removes an "Update available" banner     saved
committing     "Stay signed in" also POSTs /logout              halt, no write
disguised      session text + "Apply template to patient?"      halt, no click
steal-hover    a popup covers Save as the pointer arrives       halt, no write
steal-press    a popup covers Save between press and release    halt, no write
unknown        an unlisted dialog                               halt, no click
mislabeled     class says "Close", its selector is "Sign out"   halt, no click
relabel        external label turns "Close" into "Sign out"     halt, no GET
               on pointerover
external       a stable external label "Close"                  saved
cssom-rule     insertRule turns the ::before "Close" into       halt, no GET
               "Sign out" on pointerover
cssom-adopted  an adopted stylesheet does the same              halt, no GET
nodes-65       a 65-node label, relabeled by CSSOM on its last  halt, no click
               node (too large to observe whole)
nodes-64       a stable 64-node label                           saved
grow           a 64-node label grows past the cap on            halt, no GET
               pointerover
getter-*       on pointerover a page getter that throws, plus   halt, no GET
               a CSSOM relabel to "Sign out"
getter-stable  the same throwing getter, no relabel             saved
=============  ===============================================  ============
"""

from __future__ import annotations

import http.server
import threading
from typing import Any, Iterator

import pytest

pytest.importorskip("playwright.sync_api")

from openadapt_flow.interruptions import interruption_from_data  # noqa: E402
from openadapt_flow.ir import (  # noqa: E402
    ActionKind,
    Anchor,
    Step,
    StructuralLocator,
    Workflow,
)

PAGE = b"""<!doctype html><html><body style="font: 20px sans-serif">
<h1>Encounter note</h1>
<button id="save" style="font-size:20px">Save note</button>
<div id="status"></div>
<script>
document.getElementById('save').onclick = () =>
  fetch('/save', {method: 'POST', body: 'note'}).then(() => {
    document.getElementById('status').textContent = 'Saved';
  });
const kind = location.hash.slice(1);
function dialog(text, label, onclick) {
  const d = document.createElement('div');
  d.setAttribute('role', 'dialog');
  d.style = 'position:fixed;inset:0;background:#fff;padding:40px';
  d.innerHTML = '<p style="font-size:28px">' + text + '</p>';
  const b = document.createElement('button');
  b.textContent = label; b.style = 'font-size:22px';
  b.onclick = () => { onclick(); d.remove(); };
  d.appendChild(b);
  document.body.appendChild(d);
}
if (kind === 'session') {
  dialog('Your session will expire in 5 minutes', 'Stay signed in',
         () => fetch('/ping'));
} else if (kind === 'committing') {
  dialog('Your session will expire in 5 minutes', 'Stay signed in',
         () => fetch('/logout', {method: 'POST'}));
} else if (kind === 'disguised') {
  dialog('Your session will expire in 5 minutes. Apply template to patient?',
         'Stay signed in', () => fetch('/apply', {method: 'POST'}));
} else if (kind === 'steal-hover' || kind === 'steal-press') {
  // A popup that appears while the click is under way: after every check,
  // as the pointer arrives (hover) or between press and release (press).
  const evt = kind === 'steal-hover' ? 'pointerover' : 'pointerdown';
  document.getElementById('save').addEventListener(evt, () => {
    const p = document.createElement('div');
    p.style = 'position:fixed;inset:0;background:#fee;font-size:28px';
    p.innerHTML = 'New message <button id="accept">Accept</button>';
    p.querySelector('button').onclick = () => fetch('/accept', {method: 'POST'});
    p.onclick = () => fetch('/popup-click', {method: 'POST'});
    document.body.appendChild(p);
  }, {once: true});
} else if (kind === 'unknown') {
  dialog('Unexpected error 42 occurred', 'Close', () => {});
} else if (kind === 'mislabeled') {
  dialog('Update available now', 'Sign out', () => fetch('/logout-everywhere'));
  document.querySelector('[role=dialog] button').id = 'danger';
} else if (kind === 'relabel' || kind === 'external') {
  // The button's name comes from an external aria-labelledby node.
  const d = document.createElement('div');
  d.setAttribute('role', 'dialog');
  d.style = 'position:fixed;inset:0;background:#fff;padding:40px;font-size:28px';
  d.innerHTML = '<p>Update available now</p><span id="lbl">Close</span> '
    + '<button id="x" aria-labelledby="lbl" style="font-size:22px">x</button>';
  document.body.appendChild(d);
  const b = d.querySelector('#x');
  if (kind === 'relabel') {
    b.addEventListener('pointerover', () => {
      document.getElementById('lbl').textContent = 'Sign out';
    });
    b.onclick = () => { fetch('/logout-everywhere'); d.remove(); };
  } else {
    b.onclick = () => d.remove();
  }
} else if (kind === 'cssom-rule' || kind === 'cssom-adopted') {
  // The name is generated CSS content; a CSSOM change relabels it without
  // any DOM mutation.
  const st = document.createElement('style');
  st.textContent = '.l::before { content: "Close"; }';
  document.head.appendChild(st);
  const d = document.createElement('div');
  d.setAttribute('role', 'dialog');
  d.style = 'position:fixed;inset:0;background:#fff;padding:40px;font-size:28px';
  d.innerHTML = '<p>Update available now</p><span id="lbl" class="l"></span> '
    + '<button id="x" aria-labelledby="lbl" style="font-size:22px">x</button>';
  document.body.appendChild(d);
  const b = d.querySelector('#x');
  const rule = '.l::before { content: "Sign out" !important; }';
  b.addEventListener('pointerover', () => {
    if (kind === 'cssom-rule') {
      st.sheet.insertRule(rule, st.sheet.cssRules.length);
    } else {
      const sheet = new CSSStyleSheet();
      sheet.replaceSync(rule);
      document.adoptedStyleSheets = [...document.adoptedStyleSheets, sheet];
    }
  });
  b.onclick = () => { fetch('/logout-everywhere'); d.remove(); };
} else if (kind.startsWith('nodes-') || kind === 'grow') {
  // A label of N nodes (itself and N-1 empty children); the last child's
  // generated content names the button.
  const n = kind === 'nodes-65' ? 65 : 64;
  const st = document.createElement('style');
  st.textContent = '.t::before { content: "Close"; }';
  document.head.appendChild(st);
  const d = document.createElement('div');
  d.setAttribute('role', 'dialog');
  d.style = 'position:fixed;inset:0;background:#fff;padding:40px;font-size:28px';
  d.innerHTML = '<p>Update available now</p><span id="lbl">'
    + '<i></i>'.repeat(n - 2) + '<i class="t"></i></span> '
    + '<button id="x" aria-labelledby="lbl" style="font-size:22px">x</button>';
  document.body.appendChild(d);
  const b = d.querySelector('#x');
  b.addEventListener('pointerover', () => {
    if (kind === 'nodes-65') {
      st.sheet.insertRule('.t::before { content: "Sign out" !important; }', 1);
    } else if (kind === 'grow') {
      document.getElementById('lbl').appendChild(document.createElement('i'));
    }
  });
  b.onclick = () => {
    if (kind !== 'nodes-64') fetch('/logout-everywhere');
    d.remove();
  };
} else if (kind.startsWith('getter-')) {
  // A page getter that throws when read, installed as the pointer arrives,
  // together with a CSSOM relabel; the stable variant only installs it.
  const st = document.createElement('style');
  st.textContent = '.l::before { content: "Close"; }';
  document.head.appendChild(st);
  const d = document.createElement('div');
  d.setAttribute('role', 'dialog');
  d.style = 'position:fixed;inset:0;background:#fff;padding:40px;font-size:28px';
  d.innerHTML = '<p>Update available now</p><span id="lbl" class="l"></span> '
    + '<button id="x" aria-labelledby="lbl" style="font-size:22px">x</button>';
  document.body.appendChild(d);
  const b = d.querySelector('#x');
  const boom = { get() { throw new Error('boom'); }, configurable: true };
  b.addEventListener('pointerover', () => {
    const what = kind.slice('getter-'.length);
    if (what === 'proto') {
      Object.defineProperty(HTMLButtonElement.prototype, 'value', boom);
    } else if (what === 'stable') {
      Object.defineProperty(b, 'value', boom);
    } else {
      Object.defineProperty(b, what, boom);
    }
    if (what !== 'stable') {
      st.sheet.insertRule('.l::before { content: "Sign out" !important; }', 1);
    }
  });
  b.onclick = () => {
    if (kind !== 'getter-stable') fetch('/logout-everywhere');
    d.remove();
  };
} else if (kind === 'banner') {
  const b = document.createElement('div');
  b.style = 'position:fixed;top:0;left:0;right:0;height:120px;background:#ffd;'
          + 'font-size:28px;padding:20px';
  b.innerHTML = 'Update available <button aria-label="Close">x</button>';
  b.querySelector('button').onclick = () => b.remove();
  document.body.appendChild(b);
}
</script></body></html>"""


class _Server(http.server.BaseHTTPRequestHandler):
    seen: list[str] = []

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        type(self).seen.append(f"GET {self.path}")
        self.send_response(200)
        self.send_header("content-type", "text/html")
        self.end_headers()
        self.wfile.write(PAGE)

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        type(self).seen.append(f"POST {self.path}")
        self.send_response(204)
        self.end_headers()

    def log_message(self, *_: Any) -> None:
        pass


CLASSES = [
    interruption_from_data(
        {
            "name": "session expiry warning",
            "texts": ["Your session will expire"],
            "dismiss": {"control": {"role": "button", "name": "Stay signed in"}},
        }
    ),
    interruption_from_data(
        {
            "name": "update banner",
            "texts": ["Update available"],
            "dismiss": {"control": {"role": "button", "name": "Close"}},
        }
    ),
]


@pytest.fixture()
def server() -> Iterator[str]:
    _Server.seen = []
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Server)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}/"
    finally:
        srv.shutdown()


def _run(url: str, kind: str, tmp_path: Any, classes: Any = None) -> Any:
    from openadapt_flow.backends.playwright_backend import PlaywrightBackend
    from openadapt_flow.runtime.replayer import Replayer

    try:
        backend, close = PlaywrightBackend.launch(url + "#" + kind, headless=True)
    except Exception as exc:  # pragma: no cover - no browser installed
        pytest.skip(f"no Playwright browser: {exc}")
    workflow = Workflow(
        name="save note",
        steps=[
            Step(
                id="save",
                intent="click 'Save note'",
                action=ActionKind.CLICK,
                anchor=Anchor(
                    template="",
                    structural=StructuralLocator(selector="#save"),
                    region=(8, 80, 120, 30),
                    click_point=(60, 95),
                    ocr_text="Save note",
                ),
                risk="reversible",
            )
        ],
        interstitials=CLASSES if classes is None else classes,
    )
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    try:
        report = Replayer(backend).run(
            workflow, bundle_dir=bundle, run_dir=tmp_path / "run"
        )
        backend.page.wait_for_timeout(300)
    finally:
        close()
    return report


@pytest.mark.parametrize("kind", ["session", "banner"])
def test_a_benign_interruption_is_cleared_and_the_save_lands(
    server: str, kind: str, tmp_path: Any
) -> None:
    report = _run(server, kind, tmp_path)
    assert report.success is True, report.results[0].error
    event = report.results[0].interstitial_actions[0]
    assert event.ok and event.effect_watchers == ["network"]
    assert event.write_attempts == []
    assert "POST /save" in _Server.seen


def test_a_dismissal_that_commits_halts_and_nothing_is_written(
    server: str, tmp_path: Any
) -> None:
    report = _run(server, "committing", tmp_path)
    assert report.success is False
    result = report.results[0]
    assert result.safety_halt is True
    assert any("/logout" in a for a in result.interstitial_actions[0].write_attempts)
    # The fence stopped the logout in the browser, and the save never ran.
    assert not any(seen.startswith("POST") for seen in _Server.seen)


def test_an_unknown_dialog_halts_without_a_click(server: str, tmp_path: Any) -> None:
    report = _run(server, "unknown", tmp_path)
    assert report.success is False
    assert report.results[0].interstitial_actions == []
    assert not any(seen.startswith("POST") for seen in _Server.seen)


def test_a_business_question_in_disguise_halts_without_a_click(
    server: str, tmp_path: Any
) -> None:
    report = _run(server, "disguised", tmp_path)
    assert report.success is False
    assert "business question" in (report.results[0].error or "")
    assert report.results[0].interstitial_actions == []
    assert not any(seen.startswith("POST") for seen in _Server.seen)


@pytest.mark.parametrize("kind", ["steal-hover", "steal-press"])
def test_a_popup_that_steals_the_click_halts_and_nothing_is_written(
    server: str, kind: str, tmp_path: Any
) -> None:
    report = _run(server, kind, tmp_path)
    assert report.success is False, _Server.seen
    result = report.results[0]
    assert result.safety_halt is True
    if kind == "steal-press":
        # Pressed on Save, released on the popup: the click's witness saw
        # the input land elsewhere. Reconcile, never retry.
        assert result.delivery_uncertainty is not None
        assert result.delivery_uncertainty.cause_type == "InputLandedElsewhere"
    else:
        # Covered as the pointer arrived: refused before any input.
        assert "pre-dispatch" in (result.error or "")
    # Neither the save nor anything on the popup went through.
    assert not any(seen.startswith("POST") for seen in _Server.seen), _Server.seen


def test_a_selector_that_points_at_another_control_halts_without_a_click(
    server: str, tmp_path: Any
) -> None:
    # The class declares a "Close" button, but its selector resolves to a
    # "Sign out" button whose handler signs out with a GET the fence passes.
    mislabeled = interruption_from_data(
        {
            "name": "update dialog",
            "texts": ["Update available"],
            "dismiss": {
                "control": {"selector": "#danger", "role": "button", "name": "Close"}
            },
        }
    )
    report = _run(server, "mislabeled", tmp_path, classes=[mislabeled])
    assert report.success is False
    assert "not the button 'Close'" in (report.results[0].error or "")
    assert report.results[0].interstitial_actions == []
    assert not any("/logout-everywhere" in seen for seen in _Server.seen)
    assert not any(seen.startswith("POST") for seen in _Server.seen)


LABELLED = interruption_from_data(
    {
        "name": "update dialog",
        "texts": ["Update available"],
        "dismiss": {"control": {"selector": "#x", "role": "button", "name": "Close"}},
    }
)


def test_a_label_that_changes_as_the_pointer_arrives_halts_with_no_request(
    server: str, tmp_path: Any
) -> None:
    report = _run(server, "relabel", tmp_path, classes=[LABELLED])
    assert report.success is False
    result = report.results[0]
    assert result.safety_halt is True
    assert result.delivery_uncertainty is not None
    assert result.delivery_uncertainty.cause_type == "LabelChangedDuringClick"
    event = result.interstitial_actions[0]
    assert event.delivered is True and event.label_violations
    assert not any("/logout-everywhere" in seen for seen in _Server.seen)
    assert not any(seen.startswith("POST") for seen in _Server.seen)


def test_a_stable_external_label_is_cleared_and_the_save_lands(
    server: str, tmp_path: Any
) -> None:
    report = _run(server, "external", tmp_path, classes=[LABELLED])
    assert report.success is True, report.results[0].error
    event = report.results[0].interstitial_actions[0]
    assert event.ok and event.label_violations == []
    assert "POST /save" in _Server.seen


def _learn(url: str, kind: str) -> Any:
    from openadapt_flow.backends.playwright_backend import PlaywrightBackend
    from openadapt_flow.interruptions import learn_interruption

    try:
        backend, close = PlaywrightBackend.launch(url + "#" + kind, headless=True)
    except Exception as exc:  # pragma: no cover - no browser installed
        pytest.skip(f"no Playwright browser: {exc}")

    def shows(text: str) -> bool:
        return bool(
            backend.page.evaluate("t => document.body.innerText.includes(t)", text)
        )

    try:
        return learn_interruption(
            name="update dialog",
            texts=["Update available"],
            control={"selector": "#x", "role": "button", "name": "Close"},
            backend=backend,
            shows=shows,
            settle=lambda: backend.page.wait_for_timeout(200),
        )
    finally:
        backend.page.wait_for_timeout(300)
        close()


def test_learning_refuses_a_label_that_changes_as_the_pointer_arrives(
    server: str,
) -> None:
    from openadapt_flow.interruptions import LearnRefused

    with pytest.raises(LearnRefused, match="label changed during the click"):
        _learn(server, "relabel")
    assert not any("/logout-everywhere" in seen for seen in _Server.seen)
    assert not any(seen.startswith("POST") for seen in _Server.seen)


def test_learning_keeps_a_stable_external_label(server: str) -> None:
    learned = _learn(server, "external")
    assert learned.watchers == ("network",)


CSSOM = ["cssom-rule", "cssom-adopted"]


@pytest.mark.parametrize("kind", CSSOM)
def test_a_cssom_relabel_as_the_pointer_arrives_halts_with_no_request(
    server: str, kind: str, tmp_path: Any
) -> None:
    report = _run(server, kind, tmp_path, classes=[LABELLED])
    assert report.success is False
    result = report.results[0]
    assert result.safety_halt is True, result.error
    assert result.delivery_uncertainty is not None
    assert result.delivery_uncertainty.cause_type == "LabelChangedDuringClick"
    assert result.interstitial_actions[0].label_violations
    assert not any("/logout-everywhere" in seen for seen in _Server.seen)
    assert not any(seen.startswith("POST") for seen in _Server.seen)


@pytest.mark.parametrize("kind", CSSOM)
def test_learning_refuses_a_cssom_relabel_as_the_pointer_arrives(
    server: str, kind: str
) -> None:
    from openadapt_flow.interruptions import LearnRefused

    with pytest.raises(LearnRefused, match="label changed during the click"):
        _learn(server, kind)
    assert not any("/logout-everywhere" in seen for seen in _Server.seen)
    assert not any(seen.startswith("POST") for seen in _Server.seen)


def test_a_label_too_large_to_observe_whole_is_never_clicked(
    server: str, tmp_path: Any
) -> None:
    report = _run(server, "nodes-65", tmp_path, classes=[LABELLED])
    assert report.success is False
    event = report.results[0].interstitial_actions[0]
    assert event.delivered is False
    assert "observed" in (event.error or ""), event.error
    assert report.results[0].delivery_uncertainty is None
    assert not any("/logout-everywhere" in seen for seen in _Server.seen)
    assert not any(seen.startswith("POST") for seen in _Server.seen)


def test_learning_refuses_a_label_too_large_to_observe_whole(server: str) -> None:
    from openadapt_flow.interruptions import LearnRefused

    with pytest.raises(LearnRefused, match="observed"):
        _learn(server, "nodes-65")
    assert not any("/logout-everywhere" in seen for seen in _Server.seen)
    assert not any(seen.startswith("POST") for seen in _Server.seen)


def test_a_stable_label_at_the_cap_is_cleared_and_the_save_lands(
    server: str, tmp_path: Any
) -> None:
    report = _run(server, "nodes-64", tmp_path, classes=[LABELLED])
    assert report.success is True, report.results[0].error
    assert report.results[0].interstitial_actions[0].label_violations == []
    assert "POST /save" in _Server.seen


def test_learning_keeps_a_stable_label_at_the_cap(server: str) -> None:
    learned = _learn(server, "nodes-64")
    assert learned.watchers == ("network",)


def test_a_label_that_grows_past_the_cap_during_the_click_is_blocked(
    server: str, tmp_path: Any
) -> None:
    report = _run(server, "grow", tmp_path, classes=[LABELLED])
    assert report.success is False
    result = report.results[0]
    assert result.safety_halt is True
    assert result.delivery_uncertainty is not None
    assert result.delivery_uncertainty.cause_type == "LabelChangedDuringClick"
    assert not any("/logout-everywhere" in seen for seen in _Server.seen)
    assert not any(seen.startswith("POST") for seen in _Server.seen)


GETTERS = [
    "getter-value",
    "getter-labels",
    "getter-textContent",
    "getter-tagName",
    "getter-proto",
]


@pytest.mark.parametrize("kind", GETTERS)
def test_a_throwing_page_getter_cannot_let_a_relabeled_click_through(
    server: str, kind: str, tmp_path: Any
) -> None:
    report = _run(server, kind, tmp_path, classes=[LABELLED])
    assert report.success is False
    result = report.results[0]
    assert result.safety_halt is True, result.error
    assert result.delivery_uncertainty is not None
    assert result.delivery_uncertainty.cause_type == "LabelChangedDuringClick"
    assert result.interstitial_actions[0].label_violations
    assert not any("/logout-everywhere" in seen for seen in _Server.seen)
    assert not any(seen.startswith("POST") for seen in _Server.seen)


@pytest.mark.parametrize("kind", GETTERS)
def test_learning_refuses_a_relabeled_click_behind_a_throwing_getter(
    server: str, kind: str
) -> None:
    from openadapt_flow.interruptions import LearnRefused

    with pytest.raises(LearnRefused, match="label changed during the click"):
        _learn(server, kind)
    assert not any("/logout-everywhere" in seen for seen in _Server.seen)
    assert not any(seen.startswith("POST") for seen in _Server.seen)


def test_a_throwing_page_getter_alone_does_not_block_a_stable_label(
    server: str, tmp_path: Any
) -> None:
    report = _run(server, "getter-stable", tmp_path, classes=[LABELLED])
    assert report.success is True, report.results[0].error
    assert "POST /save" in _Server.seen
