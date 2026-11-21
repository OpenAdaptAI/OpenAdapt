"""Real headless Chromium checks: forms in open shadow roots and routed iframes.

The pages are original synthetic loopback fixtures. Nothing here names an
application; routing and anchors are data, as in a form pack.
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import sync_playwright

from openadapt_flow.form_tx.playwright_surface import PlaywrightFormSurface
from openadapt_flow.form_tx.surface import match_anchor_in_snapshot
from openadapt_flow.form_tx.types import FieldAnchorSpec, ObservedOption, UIProfileSpec

SHADOW_PAGE = r"""<!doctype html>
<html><body>
<h1>Quote</h1>
<label for="light_name">Light name</label><input id="light_name" name="light_name">
<x-panel id="panel"></x-panel>
<x-hidden-panel id="hidden_panel" hidden></x-hidden-panel>
<x-closed id="closed_panel"></x-closed>
<script>
class XField extends HTMLElement {
  connectedCallback() {
    const root = this.attachShadow({mode: 'open'});
    root.innerHTML = `
      <fieldset><legend>Dwelling</legend>
        <label for="year">Year built</label><input id="year" name="year" required>
        <label for="roof">Roof material</label>
        <select id="roof"><option value="">-- Select --</option>
          <option value="asphalt">Asphalt shingle</option><option value="metal">Metal</option></select>
        <label><input type="checkbox" id="alarm"> Monitored alarm</label>
        <span id="ownlbl">Pool on premises</span>
        <input id="pool" aria-labelledby="ownlbl">
        <div role="combobox" id="city" aria-controls="city_list" aria-expanded="false"
             tabindex="0">Choose...</div>
        <ul role="listbox" id="city_list" hidden>
          <li role="option" data-value="SPR">Springfield</li>
          <li role="option" data-value="SHB">Shelbyville</li>
        </ul>
      </fieldset>`;
    const cb = root.getElementById('city');
    const lb = root.getElementById('city_list');
    cb.addEventListener('click', () => { lb.hidden = false; cb.setAttribute('aria-expanded', 'true'); });
    for (const li of lb.querySelectorAll('li')) {
      li.addEventListener('click', () => {
        cb.textContent = li.textContent; cb.setAttribute('data-selected-value', li.dataset.value);
        lb.hidden = true; cb.setAttribute('aria-expanded', 'false');
      });
    }
  }
}
class XPanel extends HTMLElement {
  connectedCallback() {
    const root = this.attachShadow({mode: 'open'});
    root.innerHTML = `<section><h2>Property</h2><x-field id="inner"></x-field>
      <div role="alert" id="banner"></div></section>`;
  }
}
class XHiddenPanel extends HTMLElement {
  connectedCallback() {
    const root = this.attachShadow({mode: 'open'});
    root.innerHTML = `<label for="h">Hidden field</label><input id="h">`;
  }
}
class XClosed extends HTMLElement {
  connectedCallback() {
    const root = this.attachShadow({mode: 'closed'});
    root.innerHTML = `<label for="c">Closed field</label><input id="c">`;
  }
}
customElements.define('x-field', XField);
customElements.define('x-panel', XPanel);
customElements.define('x-hidden-panel', XHiddenPanel);
customElements.define('x-closed', XClosed);
</script>
</body></html>"""

SHELL_PAGE = r"""<!doctype html><html><body><h1>Shell</h1>
<label for="shell_q">Shell search</label><input id="shell_q">
<iframe src="/embed/app" title="quote"></iframe></body></html>"""

EMBED_PAGE = r"""<!doctype html><html><body><h2>Embedded quote</h2>
<label for="zip">ZIP code</label><input id="zip" name="zip">
<x-sub></x-sub>
<script>
customElements.define('x-sub', class extends HTMLElement {
  connectedCallback() {
    this.attachShadow({mode: 'open'}).innerHTML =
      '<label for="sq">Square feet</label><input id="sq">';
  }
});
</script></body></html>"""


@pytest.fixture(scope="module")
def browser() -> Any:
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        yield browser
        browser.close()


@pytest.fixture
def app(browser: Any) -> Any:
    pages = {"/shadow": SHADOW_PAGE, "/shell": SHELL_PAGE, "/embed/app": EMBED_PAGE}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body = pages.get(urlsplit(self.path).path, "<body>ready</body>").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    context = browser.new_context()
    page = context.new_page()

    class App:
        origin = f"http://127.0.0.1:{server.server_port}"

    App.page = page
    try:
        yield App
    finally:
        context.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _profile(**kw: Any) -> UIProfileSpec:
    return UIProfileSpec(
        profile_id="shadow-test",
        debounce_grace_ms=0,
        settle_window_ms=60,
        max_settle_ms=4000,
        **kw,
    )


def _by_label(snap: Any, pattern: str) -> Any:
    ctrl = match_anchor_in_snapshot(
        FieldAnchorSpec(label_patterns=(pattern,)), snap, include_readonly=True
    )
    assert ctrl is not None, pattern
    return ctrl


def test_nested_open_shadow_controls_are_observed_with_labels(app: Any) -> None:
    app.page.goto(app.origin + "/shadow")
    surface = PlaywrightFormSurface(app.page, _profile(), base_origin=app.origin)
    snap = surface.snapshot()
    labels = {c.label_text for c in snap.controls}
    assert {"Light name", "Year built", "Roof material", "Monitored alarm"} <= labels
    assert "Pool on premises" in labels  # aria-labelledby resolved in its root
    assert "Hidden field" not in labels  # host is hidden
    assert "Closed field" not in labels  # closed roots stay invisible

    year = _by_label(snap, "^Year built$")
    assert year.dom_selector == "#panel >> #inner >> #year"
    assert year.required is True
    assert year.section_text == "Dwelling"
    roof = _by_label(snap, "^Roof material$")
    assert roof.current_value == ""
    assert [o.label for o in roof.options][1:] == ["Asphalt shingle", "Metal"]


def test_actions_reach_controls_inside_nested_shadow_roots(app: Any) -> None:
    app.page.goto(app.origin + "/shadow")
    surface = PlaywrightFormSurface(app.page, _profile(), base_origin=app.origin)
    snap = surface.snapshot()
    surface.fill_text(_by_label(snap, "^Year built$").dom_selector, "1961")
    surface.select_native_option(
        _by_label(snap, "^Roof material$").dom_selector, "metal"
    )
    surface.set_checkbox_or_radio(_by_label(snap, "Monitored alarm").dom_selector, True)
    surface.fill_text(_by_label(snap, "Pool on premises").dom_selector, "No")

    after = surface.snapshot()
    assert _by_label(after, "^Year built$").current_value == "1961"
    assert _by_label(after, "^Roof material$").current_value == "metal"
    assert _by_label(after, "Monitored alarm").checked is True
    assert _by_label(after, "Pool on premises").current_value == "No"


def test_portal_combobox_inside_shadow_root(app: Any) -> None:
    app.page.goto(app.origin + "/shadow")
    surface = PlaywrightFormSurface(app.page, _profile(), base_origin=app.origin)
    snap = surface.snapshot()
    combo = next(c for c in snap.controls if c.role == "combobox")
    assert combo.dom_selector == "#panel >> #inner >> #city"
    opts = surface.open_portal_combobox_options(combo.dom_selector)
    assert [(o.value, o.label) for o in opts] == [
        ("SPR", "Springfield"),
        ("SHB", "Shelbyville"),
    ]
    surface.choose_portal_combobox_option(
        combo.dom_selector, ObservedOption(value="SHB", label="Shelbyville")
    )
    after = next(c for c in surface.snapshot().controls if c.role == "combobox")
    assert after.current_value == "SHB"


def test_banners_and_auth_walls_inside_shadow_roots_are_seen(app: Any) -> None:
    app.page.goto(app.origin + "/shadow")
    surface = PlaywrightFormSurface(
        app.page,
        _profile(auth_wall_selectors=("[data-auth-wall='true']",)),
        base_origin=app.origin,
    )
    assert surface.snapshot().banner_errors == ()
    assert surface.snapshot().auth_wall_present is False
    app.page.evaluate(
        """() => {
            const root = document.getElementById('panel').shadowRoot;
            root.getElementById('banner').textContent = 'Your session has expired.';
            const wall = document.createElement('div');
            wall.setAttribute('data-auth-wall', 'true');
            wall.textContent = 'Sign in';
            root.querySelector('section').appendChild(wall);
        }"""
    )
    snap = surface.snapshot()
    assert snap.banner_errors == ("Your session has expired.",)
    assert snap.auth_wall_present is True


def test_shadow_root_changes_count_as_dom_activity(app: Any) -> None:
    app.page.goto(app.origin + "/shadow")
    surface = PlaywrightFormSurface(app.page, _profile(), base_origin=app.origin)
    first = surface._read_dom_state([])["domSig"]
    app.page.evaluate(
        """() => {
            const inner = document.getElementById('panel').shadowRoot
                .getElementById('inner').shadowRoot;
            inner.getElementById('year').value = '2001';
        }"""
    )
    assert surface._read_dom_state([])["domSig"] != first


def test_ui_profile_routes_into_iframe_and_its_shadow_root(app: Any) -> None:
    app.page.goto(app.origin + "/shell")
    app.page.wait_for_load_state()
    surface = PlaywrightFormSurface(
        app.page,
        _profile(frame_path_patterns=(r"/embed/.*",)),
        base_origin=app.origin,
    )
    snap = surface.snapshot()
    labels = {c.label_text for c in snap.controls}
    assert labels == {"ZIP code", "Square feet"}  # never the shell's search box
    assert snap.url_origin_and_path.endswith("/embed/app")
    surface.fill_text(_by_label(snap, "ZIP code").dom_selector, "98006")
    surface.fill_text(_by_label(snap, "Square feet").dom_selector, "2860")
    after = surface.snapshot()
    assert _by_label(after, "ZIP code").current_value == "98006"
    assert _by_label(after, "Square feet").current_value == "2860"


def test_ui_profile_rejects_invalid_frame_pattern() -> None:
    with pytest.raises(ValueError):
        _profile(frame_path_patterns=("(",))
