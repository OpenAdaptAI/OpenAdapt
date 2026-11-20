"""Real headless Frame qualification using an original synthetic loopback app."""

from __future__ import annotations

import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from openadapt_flow.form_tx.playwright_surface import (
    PlaywrightFormSurface,
    SurfaceContextError,
    SurfaceSettleTimeoutError,
)
from openadapt_flow.form_tx.types import ObservedOption, UIProfileSpec


@pytest.fixture(scope="module")
def browser() -> Any:
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="chromium", headless=True)
        yield browser
        browser.close()


@pytest.fixture
def app(browser: Any) -> Any:
    class App:
        pages: dict[str, str] = {}
        delays: dict[str, float] = {}
        posts: list[str] = []

    state = App()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            path = urlsplit(self.path).path
            time.sleep(state.delays.get(path, 0))
            body = state.pages.get(path, "<!doctype html><body>ready</body>").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            state.posts.append(self.path)
            time.sleep(state.delays.get(self.path, 0))
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(
        target=lambda: server.serve_forever(poll_interval=0.02), daemon=True
    )
    thread.start()
    state.origin = f"http://127.0.0.1:{server.server_port}"
    state.context = browser.new_context()
    state.page = state.context.new_page()
    try:
        yield state
    finally:
        state.context.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def profile(max_settle_ms: int = 2500) -> UIProfileSpec:
    return UIProfileSpec(
        profile_id="frame-test",
        debounce_grace_ms=0,
        settle_window_ms=80,
        max_settle_ms=max_settle_ms,
        busy_selectors=("[data-busy='true']",),
        authenticated_landmark_selectors=("[data-authenticated]",),
        auth_wall_selectors=("[data-auth-wall]",),
    )


FORM = """<!doctype html><title>Frame form</title><body data-authenticated>
<label>Name<input id="name" name="name" value="Original"></label>
<button id="save" onclick="fetch('/save', {method:'POST'})">Save</button>
</body>"""


def load(app: Any, content: str = FORM) -> Any:
    app.pages["/shell"] = (
        '<!doctype html><body data-auth-wall><iframe name="work" src="/form?record=one"></iframe>'
    )
    app.pages["/form"] = content
    app.page.goto(app.origin + "/shell")
    return app.page.frame(name="work")


def test_native_frame_waits_for_six_second_network_and_reads_only_its_dom(
    app: Any,
) -> None:
    frame = load(app)
    surface = PlaywrightFormSurface(frame, profile(8500))
    app.delays["/slow"] = 6
    frame.evaluate("""() => { window.pending = fetch('/slow').then(() => {
        document.querySelector('#name').value = 'Complete';
    }); }""")
    start = time.monotonic()
    snap = surface.snapshot()
    assert time.monotonic() - start >= 5.8
    assert snap.title == "Frame form"
    assert snap.authenticated_landmark_present and not snap.auth_wall_present
    assert snap.controls[0].current_value == "Complete"
    assert snap.context_id and "record=" not in snap.model_dump_json()
    assert surface._quiescence._armed


def test_mutations_are_on_owning_page_including_nested_frames_but_not_other_tabs(
    app: Any,
) -> None:
    frame = load(app, FORM + '<iframe name="nested" src="/nested"></iframe>')
    app.pages["/nested"] = FORM
    # Reload now that the nested route is configured.
    frame.goto(app.origin + "/form?record=one")
    surface = PlaywrightFormSurface(frame, profile())
    nested = app.page.frame(name="nested")
    app.delays["/save"] = 0.2
    nested.evaluate("() => { window.pending = fetch('/save', {method:'POST'}); }")
    snap = surface.snapshot()
    assert surface.mutation_count == 1
    assert snap.inflight_mutations == 0
    assert app.posts == ["/save"]
    other = app.context.new_page()
    other.goto(app.origin + "/elsewhere")
    other.evaluate("() => fetch('/save', {method:'POST'})")
    assert surface.mutation_count == 1
    surface.fill_text("#name", "Updated")
    assert frame.locator("#name").input_value() == "Updated"


@pytest.mark.parametrize("change", ["remove", "replace", "close"])
def test_native_frame_refuses_detach_replacement_and_closed_owner(
    app: Any, change: str
) -> None:
    frame = load(app)
    surface = PlaywrightFormSurface(frame, profile())
    surface.snapshot()
    if change == "close":
        app.page.close()
    else:
        app.page.evaluate(
            """change => {
            const old = document.querySelector('iframe');
            if (change === 'replace') old.replaceWith(old.cloneNode());
            else old.remove();
        }""",
            change,
        )
    with pytest.raises(SurfaceContextError):
        surface.snapshot()
    with pytest.raises(SurfaceContextError):
        surface.fill_text("#name", "Unsafe")


@pytest.mark.parametrize("change", ["reload", "query", "replacement"])
def test_document_identity_changes_without_exposing_record_query(
    app: Any, change: str
) -> None:
    frame = load(app)
    surface = PlaywrightFormSurface(
        app.page, profile(), base_origin=app.origin, frame_path_patterns=(r"/form",)
    )
    before = surface.snapshot()
    if change == "replacement":
        app.page.evaluate(
            "() => { const f=document.querySelector('iframe'); f.replaceWith(f.cloneNode()); }"
        )
        app.page.wait_for_function(
            "document.querySelector('iframe').contentDocument?.readyState === 'complete' && document.querySelector('iframe').contentDocument?.querySelector('#name')"
        )
    elif change == "reload":
        frame.goto(frame.url)
    else:
        frame.evaluate("() => history.replaceState({}, '', '?record=two')")
    with pytest.raises(SurfaceContextError, match="fresh snapshot"):
        surface.fill_text("#name", "Stale")
    after = surface.snapshot()
    assert after.url_origin_and_path == before.url_origin_and_path
    assert after.context_id != before.context_id
    assert "record=" not in after.model_dump_json()
    surface.fill_text("#name", "Fresh")


def test_ordered_route_selects_nested_target_and_follows_confirm_to_summary(
    app: Any,
) -> None:
    app.pages["/confirm"] = """<!doctype html><title>Confirm</title><body>
        <button id="confirm" onclick="parent.location='/summary'">Confirm</button>"""
    app.pages["/summary"] = (
        "<!doctype html><title>Saved</title><body data-authenticated>saved"
    )
    load(app, FORM + '<iframe src="/confirm"></iframe>')
    surface = PlaywrightFormSurface(
        app.page,
        profile(),
        base_origin=app.origin,
        frame_path_patterns=(r"/confirm", r"/form", r"/summary"),
    )
    assert surface.snapshot().title == "Confirm"
    surface.click("#confirm")
    assert surface.snapshot().title == "Saved"


@pytest.mark.parametrize("case", ["unknown", "ambiguous", "foreign"])
def test_routes_refuse_unknown_ambiguous_and_cross_origin_targets(
    app: Any, case: str
) -> None:
    load(app)
    if case == "ambiguous":
        app.page.evaluate(
            "() => document.body.append(document.querySelector('iframe').cloneNode())"
        )
        app.page.wait_for_function("document.querySelectorAll('iframe').length === 2")
        for frame in app.page.frames:
            frame.wait_for_load_state()
    elif case == "foreign":
        app.page.locator("iframe").evaluate(
            "(el, url) => el.src=url",
            app.origin.replace("127.0.0.1", "localhost") + "/form",
        )
        app.page.frame(name="work").wait_for_load_state()
    surface = PlaywrightFormSurface(
        app.page,
        profile(),
        base_origin=app.origin,
        frame_path_patterns=(r"/unknown" if case == "unknown" else r"/form",),
    )
    with pytest.raises(SurfaceContextError):
        surface.snapshot()


def test_busy_scope_excludes_siblings_but_includes_nested_documents(app: Any) -> None:
    app.pages["/nested"] = '<!doctype html><body data-busy="true">busy'
    frame = load(app)
    app.page.evaluate("""() => {
        const noise = document.createElement('iframe');
        noise.src='/nested'; document.body.append(noise);
    }""")
    surface = PlaywrightFormSurface(frame, profile(400))
    surface.snapshot()  # Sibling's static busy marker is outside this surface.
    frame.evaluate("""() => {
        const nested = document.createElement('iframe');
        nested.src='/nested'; document.body.append(nested);
    }""")
    with pytest.raises(SurfaceSettleTimeoutError, match="busy:selector"):
        surface.snapshot()


def test_frame_popup_uses_owning_page_lifecycle(app: Any) -> None:
    frame = load(
        app,
        FORM + '<button id="popup" onclick="window.open(\'/popup\')">Popup</button>',
    )
    app.pages["/popup"] = "<!doctype html><title>Child</title><body>child"
    surface = PlaywrightFormSurface(frame, profile())
    surface.snapshot()
    with surface.open_popup_surface(
        "#popup", url_pattern=r"/popup$", title_pattern="Child"
    ) as child:
        assert child.snapshot().title == "Child"
    assert len(app.context.pages) == 1
    assert surface.snapshot().title == "Frame form"


@pytest.mark.parametrize("routed", [False, True])
@pytest.mark.parametrize(
    "method,args",
    [
        ("fill_text", ("#name", "Unsafe")),
        ("select_native_option", ("#name", "Unsafe")),
        ("set_checkbox_or_radio", ("#name", True)),
        ("click", ("#save",)),
        ("open_portal_combobox_options", ("#name",)),
        (
            "choose_portal_combobox_option",
            ("#name", ObservedOption(value="one", label="One")),
        ),
        ("open_popup_surface", ("#save",)),
    ],
)
def test_all_actuation_entrypoints_refuse_replaced_record_context(
    app: Any, routed: bool, method: str, args: tuple[Any, ...]
) -> None:
    frame = load(app)
    surface = PlaywrightFormSurface(
        app.page if routed else frame,
        profile(),
        base_origin=app.origin,
        frame_path_patterns=(r"/form",) if routed else (),
    )
    surface.snapshot()
    frame.evaluate("() => history.replaceState({}, '', '?record=replacement')")
    with pytest.raises(SurfaceContextError, match="fresh snapshot"):
        result = getattr(surface, method)(*args)
        if method == "open_popup_surface":
            with result:
                pytest.fail("A stale popup trigger must not run")
    assert app.posts == []
    assert frame.locator("#name").input_value() == "Original"


def test_native_frame_navigation_and_keepalive_use_owner_events(app: Any) -> None:
    frame = load(app)
    surface = PlaywrightFormSurface(frame, profile(), base_origin=app.origin)
    surface.goto("/form?record=two")
    assert surface.snapshot().url_origin_and_path == app.origin + "/form"
    assert surface.ping_keepalive("/heartbeat") == 200
    assert surface.mutation_count == 0


def test_route_waits_for_delayed_allowed_replacement_after_action(app: Any) -> None:
    app.pages["/summary"] = (
        "<!doctype html><title>Saved</title><body data-authenticated>Saved"
    )
    frame = load(
        app, FORM + '<button id="next" onclick="parent.finish()">Finish</button>'
    )
    # The shell owns the replacement timer, just as it owns iframe lifetimes.
    # A callback owned by a removed Frame's realm is discarded by Chromium.
    app.page.evaluate("""() => { window.finish = () => {
        fetch('/save', {method:'POST'});
        setTimeout(() => {
            const next = document.createElement('iframe');
            next.src='/summary'; document.body.append(next);
        }, 600);
        document.querySelector('iframe').remove();
    }; }""")
    surface = PlaywrightFormSurface(
        app.page,
        profile(),
        base_origin=app.origin,
        frame_path_patterns=(r"/form", r"/summary"),
    )
    first = surface.snapshot()
    start = time.monotonic()
    surface.click("#next")
    saved = surface.snapshot()
    assert time.monotonic() - start >= 0.55
    assert frame.is_detached()
    assert saved.title == "Saved"
    assert saved.context_id != first.context_id
    assert surface.mutation_count == 1


def test_missing_routed_successor_times_out_without_reading_shell(app: Any) -> None:
    load(app)
    surface = PlaywrightFormSurface(
        app.page, profile(250), base_origin=app.origin, frame_path_patterns=(r"/form",)
    )
    surface.snapshot()
    app.page.locator("iframe").evaluate("el => el.remove()")
    start = time.monotonic()
    with pytest.raises(SurfaceContextError, match="settle deadline"):
        surface.snapshot()
    assert 0.2 <= time.monotonic() - start < 1
    assert surface.mutation_count == 0


def test_foreign_priority_match_cannot_fall_back_to_lower_route(app: Any) -> None:
    load(app)
    foreign = app.origin.replace("127.0.0.1", "localhost") + "/confirm"
    with app.page.expect_event(
        "framenavigated", predicate=lambda frame: frame.url == foreign
    ):
        app.page.evaluate(
            """url => {
            const frame=document.createElement('iframe'); frame.src=url;
            document.body.append(frame);
        }""",
            foreign,
        )
    surface = PlaywrightFormSurface(
        app.page,
        profile(),
        base_origin=app.origin,
        frame_path_patterns=(r"/confirm", r"/form"),
    )
    with pytest.raises(SurfaceContextError, match="untrusted origin"):
        surface.snapshot()
    assert app.posts == []


_ACTION_TARGETS = {
    "fill": "#name",
    "select": "#choice",
    "check": "#check",
    "click": "#write",
    "open_combo": "#combo",
    "choose_combo": "#option",
    "popup": "#popup",
}


def _action_form(action: str) -> str:
    visible = "" if action == "choose_combo" else "hidden"
    return f"""<!doctype html><title>Action form</title><body data-authenticated>
    <script>window.write = () => fetch('/save', {{method:'POST'}});</script>
    <label>Name<input id="name" value="Original" onchange="window.write()"></label>
    <label>Choice<select id="choice" onchange="window.write()"><option value="a">A</option><option value="b">B</option></select></label>
    <label>Check<input type="checkbox" id="check" onchange="window.write()"></label>
    <button id="write" onclick="window.write()">Write</button>
    <button id="combo" role="combobox" aria-controls="options" aria-expanded="false"
      onclick="document.querySelector('#options').hidden=false;this.setAttribute('aria-expanded','true');window.write()">Choices</button>
    <div id="options" role="listbox" {visible}>
      <button role="option" id="option" data-value="b" onclick="window.write()">B</button>
    </div>
    <button id="popup" onclick="window.write();window.open('/popup')">Popup</button>
    """


def _run_action(surface: PlaywrightFormSurface, action: str) -> None:
    if action == "fill":
        surface.fill_text("#name", "Changed")
    elif action == "select":
        surface.select_native_option("#choice", "b")
    elif action == "check":
        surface.set_checkbox_or_radio("#check")
    elif action == "click":
        surface.click("#write")
    elif action == "open_combo":
        surface.open_portal_combobox_options("#combo")
    elif action == "choose_combo":
        surface.choose_portal_combobox_option(
            "#combo", ObservedOption(value="b", label="B")
        )
    else:
        with surface.open_popup_surface("#popup") as child:
            child.snapshot()


def _action_surface(
    app: Any, action: str, routed: bool, max_settle_ms: int = 2500
) -> Any:
    frame = load(app, _action_form(action))
    app.pages["/popup"] = "<!doctype html><title>Child</title><body>child"
    surface = PlaywrightFormSurface(
        app.page if routed else frame,
        profile(max_settle_ms),
        base_origin=app.origin,
        frame_path_patterns=(r"/form",) if routed else (),
    )
    snap = surface.snapshot()
    return frame, surface, snap


@pytest.mark.parametrize("routed", [False, True])
@pytest.mark.parametrize("action", _ACTION_TARGETS)
@pytest.mark.parametrize("change", ["navigation", "query", "replacement"])
def test_waiting_action_never_retargets_changed_document(
    app: Any,
    routed: bool,
    action: str,
    change: str,
) -> None:
    frame, surface, before = _action_surface(app, action, routed)
    selector = _ACTION_TARGETS[action]
    # Mutate after the structural observation. The action must wait on this
    # element; the replacement document deliberately exposes the same selectors.
    frame.evaluate(
        """selector => {
        document.querySelector(selector).disabled=true;
    }""",
        selector,
    )
    if change == "replacement":
        app.page.evaluate("""() => setTimeout(() => {
            const old=document.querySelector('iframe');old.replaceWith(old.cloneNode());
        }, 300)""")
    else:
        frame.evaluate(
            """({selector, change}) => {
            setTimeout(() => {
                if(change==='navigation') location.href='/form?record=two';
                else {
                    history.replaceState({}, '', '?record=two');
                    document.querySelector(selector).disabled=false;
                }
            }, 300);
        }""",
            {"selector": selector, "change": change},
        )
    with pytest.raises(SurfaceContextError):
        _run_action(surface, action)
    app.page.wait_for_function(
        "document.querySelector('iframe').contentDocument?.readyState === 'complete' && document.querySelector('iframe').contentDocument?.querySelector('#name')"
    )
    live = app.page.frame(name="work")
    live.wait_for_load_state()
    assert live.locator("#name").input_value() == "Original"
    assert live.locator("#choice").input_value() == "a"
    assert not live.locator("#check").is_checked()
    assert app.posts == []
    assert len(app.context.pages) == 1
    assert surface.mutation_count == 0
    if routed or change != "replacement":
        assert surface.snapshot().context_id != before.context_id


@pytest.mark.parametrize("routed", [False, True])
@pytest.mark.parametrize("action", _ACTION_TARGETS)
def test_waiting_action_succeeds_when_observed_document_is_unchanged(
    app: Any,
    routed: bool,
    action: str,
) -> None:
    frame, surface, before = _action_surface(app, action, routed)
    frame.evaluate(
        """selector => {
        const target=document.querySelector(selector);target.disabled=true;
        setTimeout(() => target.disabled=false, 200);
    }""",
        _ACTION_TARGETS[action],
    )
    _run_action(surface, action)
    assert surface.snapshot().context_id == before.context_id
    assert app.posts == ["/save"]
    assert surface.mutation_count == 1
    assert len(app.context.pages) == 1


@pytest.mark.parametrize("routed", [False, True])
def test_bound_action_timeout_does_not_mutate_or_retarget(
    app: Any, routed: bool
) -> None:
    frame, surface, before = _action_surface(app, "fill", routed, 300)
    frame.locator("#name").evaluate("el => el.disabled=true")
    with pytest.raises(PlaywrightTimeoutError):
        surface.fill_text("#name", "Unsafe")
    assert surface.snapshot().context_id == before.context_id
    assert frame.locator("#name").input_value() == "Original"
    assert app.posts == []
    frame.locator("#name").evaluate("el => el.disabled=false")
    surface.snapshot()
    surface.fill_text("#name", "After timeout")
    assert frame.locator("#name").input_value() == "After timeout"
    assert app.posts == ["/save"]


@pytest.mark.parametrize("routed", [False, True])
def test_bound_action_refuses_detachment_while_waiting(app: Any, routed: bool) -> None:
    frame, surface, _ = _action_surface(app, "fill", routed)
    frame.locator("#name").evaluate("el => el.disabled=true")
    app.page.evaluate(
        "() => setTimeout(() => document.querySelector('iframe').remove(), 200)"
    )
    with pytest.raises(SurfaceContextError):
        surface.fill_text("#name", "Unsafe")
    assert frame.is_detached()
    assert app.posts == []
