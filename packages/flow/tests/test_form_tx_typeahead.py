"""Headless Chromium tests: type-ahead comboboxes and surface errors as stops.

A type-ahead that fetches suggestions only after typed text, a unique
suggestion chosen, two equal suggestions and no suggestion stopping for a
person, and a click-only combobox whose list never opens stopping instead of
raising. Pages are served through ``context.route``; no sockets, no app names.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

sync_api = pytest.importorskip("playwright.sync_api")

from openadapt_flow.form_tx.playwright_surface import PlaywrightFormSurface  # noqa: E402
from openadapt_flow.form_tx.reconciler import FormReconciler  # noqa: E402
from openadapt_flow.form_tx.types import (  # noqa: E402
    ControlKind,
    DossierFact,
    FieldAnchorSpec,
    FieldBindingSpec,
    FieldCodecSpec,
    FormCodecKind,
    FormHaltKind,
    FormPack,
    FormTxOutcome,
    InputDossier,
    ProvenanceKind,
    UIProfileSpec,
    WizardScreenSpec,
)

SUGGESTIONS = [
    {"value": "slm-or", "label": "Salem, OR"},
    {"value": "spr-or", "label": "Springfield"},
    {"value": "spr-il", "label": "Springfield"},
    {"value": "prt-or", "label": "Portland, OR"},
]

PAGE = """<!doctype html><html><body>
<h1>Quote</h1>
<form id="f" onsubmit="return false">
  <label for="city">City</label>
  <input id="city" name="city" role="combobox" aria-autocomplete="list"
         aria-controls="city-list" aria-expanded="false" autocomplete="off">
  <ul id="city-list" role="listbox" hidden></ul>
  <span id="region-label">Region</span>
  <div id="region" role="combobox" tabindex="0" aria-labelledby="region-label"
       aria-controls="region-list"
       style="display:inline-block;min-width:8em;min-height:1.2em;border:1px solid"></div>
  <button id="submit" type="button">Submit quote</button>
</form>
<script>
  const city = document.getElementById('city');
  const list = document.getElementById('city-list');
  let timer = null;
  city.addEventListener('input', () => {
    clearTimeout(timer);
    list.hidden = true;
    city.setAttribute('aria-expanded', 'false');
    const q = city.value;
    if (q.length < 2) return;
    timer = setTimeout(async () => {
      const res = await fetch('/api/suggest?q=' + encodeURIComponent(q));
      const items = await res.json();
      list.innerHTML = '';
      for (const it of items) {
        const li = document.createElement('li');
        li.setAttribute('role', 'option');
        li.setAttribute('data-option-value', it.value);
        li.id = 'opt-' + it.value;
        li.textContent = it.label;
        li.addEventListener('click', () => {
          city.value = it.label;
          city.dataset.code = it.value;
          list.hidden = true;
          city.setAttribute('aria-expanded', 'false');
        });
        list.appendChild(li);
      }
      if (items.length) {
        list.hidden = false;
        city.setAttribute('aria-expanded', 'true');
      }
    }, 150);
  });
  document.getElementById('submit').addEventListener('click', async () => {
    await fetch('/api/submit', {method: 'POST',
      body: JSON.stringify({city: city.value, code: city.dataset.code || ''})});
    document.body.innerHTML = '<h1>Thank you</h1><p>Quote stored.</p>';
  });
</script>
</body></html>"""


@pytest.fixture(scope="module")
def chromium_browser() -> Any:
    with sync_api.sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(channel="chromium", headless=True)
        except Exception:
            browser = pw.chromium.launch(headless=True)
        try:
            yield browser
        finally:
            browser.close()


@pytest.fixture
def portal(chromium_browser: Any) -> Any:
    ctx = chromium_browser.new_context()
    submissions: list[dict[str, Any]] = []
    queries: list[str] = []

    def handle(route: Any) -> None:
        parts = urlsplit(route.request.url)
        if parts.path == "/quote":
            route.fulfill(status=200, content_type="text/html", body=PAGE)
        elif parts.path == "/quote-covered":
            covered = PAGE.replace(
                '<button id="submit" type="button">Submit quote</button>',
                '<span style="position:relative;display:inline-block">'
                '<button id="submit" type="button">Submit quote</button>'
                '<span style="position:absolute;inset:0;z-index:5"></span></span>',
            )
            route.fulfill(status=200, content_type="text/html", body=covered)
        elif parts.path == "/quote-redraw":
            # After a pick the page redraws the input with a new id.
            redraw = PAGE.replace(
                "          city.setAttribute('aria-expanded', 'false');\n        });",
                "          city.setAttribute('aria-expanded', 'false');\n"
                "          const fresh = city.cloneNode();\n"
                "          fresh.id = 'c' + Math.random().toString(36).slice(2, 8);\n"
                "          fresh.value = it.label;\n"
                "          fresh.dataset.code = it.value;\n"
                "          document.querySelector('label[for=city]').htmlFor = fresh.id;\n"
                "          city.replaceWith(fresh);\n"
                "        });",
            ).replace(
                "body: JSON.stringify({city: city.value, code: city.dataset.code || ''})",
                "body: JSON.stringify({city: document.querySelector('form input').value, "
                "code: document.querySelector('form input').dataset.code || ''})",
            )
            assert redraw != PAGE
            route.fulfill(status=200, content_type="text/html", body=redraw)
        elif parts.path == "/api/suggest":
            q = parse_qs(parts.query).get("q", [""])[0]
            queries.append(q)
            hits = [s for s in SUGGESTIONS if s["label"].lower().startswith(q.lower())]
            route.fulfill(
                status=200, content_type="application/json", body=json.dumps(hits)
            )
        elif parts.path == "/api/submit":
            submissions.append(json.loads(route.request.post_data or "{}"))
            route.fulfill(status=200, content_type="application/json", body="{}")
        else:
            route.fulfill(status=404, body="not found")

    ctx.route("http://portal.local/**", handle)
    page = ctx.new_page()
    profile = UIProfileSpec(
        profile_id="typeahead_test", max_settle_ms=2500, settle_window_ms=60
    )
    surface = PlaywrightFormSurface(page, profile)
    surface.goto("http://portal.local/quote")
    try:
        yield surface, submissions, queries
    finally:
        ctx.close()


def _pack(*bindings: FieldBindingSpec) -> FormPack:
    return FormPack(
        pack_id="typeahead_pack",
        domain_schema="test.quote/v1",
        ui_profile_id="typeahead_test",
        bindings=bindings,
        screens=(
            WizardScreenSpec(
                screen_id="quote",
                landmark_selector="#submit",
                is_commit_screen=True,
                commit_button_anchor=FieldAnchorSpec(
                    label_patterns=(r"^Submit quote$",)
                ),
            ),
        ),
    )


def _city_binding(**kw: Any) -> FieldBindingSpec:
    return FieldBindingSpec(
        binding_id="city",
        dossier_key="address.city",
        anchor=FieldAnchorSpec(label_patterns=(r"^City$",)),
        control_kind=ControlKind.PORTAL_COMBOBOX,
        codec=FieldCodecSpec(kind=FormCodecKind.CASEFOLD),
        **kw,
    )


def _dossier(**facts: Any) -> InputDossier:
    return InputDossier(
        dossier_id="d1",
        request_id="r1",
        facts={
            k: DossierFact(
                key=k, value=v, provenance=ProvenanceKind.INTAKE_DOCUMENT, source_id="t"
            )
            for k, v in facts.items()
        },
    )


def _run(surface: Any, pack: FormPack, dossier: InputDossier) -> Any:
    reconciler = FormReconciler(surface)
    session = reconciler.create_session(pack, dossier)
    return reconciler.run_until_halt_or_done(session)


def test_typed_query_fetches_suggestions_and_unique_match_is_chosen(
    portal: Any,
) -> None:
    surface, submissions, queries = portal
    res = _run(
        surface, _pack(_city_binding()), _dossier(**{"address.city": "Salem, OR"})
    )
    assert queries, "suggestions are fetched only after typed text"
    assert submissions == [{"city": "Salem, OR", "code": "slm-or"}]
    assert not any(h.kind == FormHaltKind.AMBIGUOUS_OPTION_OR_TARGET for h in res.halts)
    assert res.outcome != FormTxOutcome.VERIFIED_DONE  # no read-back contract


def test_prefix_query_types_only_the_declared_characters(portal: Any) -> None:
    surface, submissions, queries = portal
    _run(
        surface,
        _pack(_city_binding(typeahead_query_chars=4)),
        _dossier(**{"address.city": "Portland, OR"}),
    )
    assert queries[-1] == "Port"
    assert submissions == [{"city": "Portland, OR", "code": "prt-or"}]


def test_two_equal_suggestions_stop_with_the_options_and_submit_nothing(
    portal: Any,
) -> None:
    surface, submissions, _ = portal
    res = _run(
        surface, _pack(_city_binding()), _dossier(**{"address.city": "Springfield"})
    )
    assert res.outcome == FormTxOutcome.HALTED_NEEDS_HUMAN
    halt = res.halts[0]
    assert halt.kind == FormHaltKind.AMBIGUOUS_OPTION_OR_TARGET
    assert halt.field_label == "City"
    assert sorted(o.value for o in halt.available_options) == ["spr-il", "spr-or"]
    assert submissions == []


def test_no_suggestion_is_a_stop_not_an_exception(portal: Any) -> None:
    surface, submissions, _ = portal
    res = _run(
        surface, _pack(_city_binding()), _dossier(**{"address.city": "Atlantis"})
    )
    assert res.outcome == FormTxOutcome.HALTED_NEEDS_HUMAN
    halt = res.halts[0]
    assert halt.kind == FormHaltKind.AMBIGUOUS_OPTION_OR_TARGET
    assert halt.field_label == "City"
    assert "No suggestions appeared" in halt.message
    assert res.safe_to_retry is True
    assert submissions == []


def test_click_only_combobox_without_a_list_stops_instead_of_raising(
    portal: Any,
) -> None:
    surface, submissions, _ = portal
    region = FieldBindingSpec(
        binding_id="region",
        dossier_key="address.region",
        anchor=FieldAnchorSpec(label_patterns=(r"^Region$",)),
        control_kind=ControlKind.PORTAL_COMBOBOX,
    )
    res = _run(surface, _pack(region), _dossier(**{"address.region": "West"}))
    assert res.outcome == FormTxOutcome.HALTED_NEEDS_HUMAN
    halt = res.halts[0]
    assert halt.kind == FormHaltKind.AMBIGUOUS_OPTION_OR_TARGET
    assert halt.field_label == "Region"
    assert "MissingAnchorError" in halt.message
    assert submissions == []


def test_error_on_the_submit_click_is_reported_as_delivery_uncertain(
    portal: Any,
) -> None:
    surface, submissions, _ = portal
    surface.goto("http://portal.local/quote-covered")
    res = _run(surface, _pack(), _dossier())
    assert res.outcome == FormTxOutcome.DELIVERY_UNCERTAIN
    assert res.safe_to_retry is False
    assert res.halts[0].message.startswith("After the submit click")
    assert submissions == []


def test_pick_that_redraws_the_input_is_checked_on_the_redrawn_control(
    portal: Any,
) -> None:
    surface, submissions, _ = portal
    surface.goto("http://portal.local/quote-redraw")
    res = _run(
        surface, _pack(_city_binding()), _dossier(**{"address.city": "Salem, OR"})
    )
    assert not res.halts, [h.message for h in res.halts]
    assert submissions == [{"city": "Salem, OR", "code": "slm-or"}]
