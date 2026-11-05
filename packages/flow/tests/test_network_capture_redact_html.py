"""Hidden token inputs and token meta tags in captured HTML are withheld
whether or not that token was sent."""

from __future__ import annotations

import html
import json

import pytest

from openadapt_flow.network_capture import SECRET, redact, session_placeholder

SENT = "Sent0tokenAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=="
OTHER_FORM = "Other1tokenBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB=="
PAGE_META = "Meta2tokenCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC=="
LATER_FORM = "Later3tokenDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDD=="

# The shape of a Rails app's (Redmine's) issue pages: the csrf-param meta
# names the token field, the csrf-token meta and every form carry a token.
NEW_ISSUE_PAGE = f"""<html><head>
<meta name="csrf-param" content="authenticity_token" />
<meta name="csrf-token" content="{PAGE_META}" />
</head><body>
<form action="/search"><input type="hidden" name="authenticity_token"
 value="{OTHER_FORM}" autocomplete="off" /></form>
<form action="/projects/helpdesk/issues" method="post">
<input type="hidden" name="authenticity_token" value="{SENT}" autocomplete="off" />
<input type="hidden" name="back_url" value="/projects/helpdesk/issues" />
<input type="text" name="issue[subject]" value="" />
</form></body></html>"""

ISSUE_PAGE = f"""<html><head>
<meta name="csrf-param" content="authenticity_token" />
<meta name="csrf-token" content="{LATER_FORM}" />
</head><body><form action="/issues/1" method="post">
<input name="authenticity_token" type="hidden" value='{LATER_FORM}'>
</form><p>Issue #1 created.</p></body></html>"""


def _capture():
    return [
        {
            "seq": 0,
            "method": "GET",
            "url": "http://app.test/projects/helpdesk/issues/new",
            "response_type": "text/html",
            "response_text": NEW_ISSUE_PAGE,
        },
        {
            "seq": 1,
            "method": "POST",
            "url": "http://app.test/projects/helpdesk/issues",
            "request_type": "application/x-www-form-urlencoded",
            "form": [
                ["authenticity_token", SENT],
                ["issue[subject]", "Printer down"],
            ],
        },
        {
            "seq": 2,
            "method": "GET",
            "url": "http://app.test/issues/1",
            "response_type": "text/html",
            "response_text": ISSUE_PAGE,
        },
    ]


def test_tokens_of_forms_that_were_not_sent_are_withheld():
    text = json.dumps(redact(_capture()))

    for token in (SENT, OTHER_FORM, PAGE_META, LATER_FORM):
        assert token not in text
    assert session_placeholder("authenticity_token") in text
    assert session_placeholder("csrf-token") in text


def test_the_field_name_and_ordinary_fields_are_kept():
    out = redact(_capture())

    page = out[0]["response_text"]
    # The csrf-param meta names the field; redacting it would rename it.
    assert 'content="authenticity_token"' in page
    assert 'name="authenticity_token"' in page
    assert 'value="/projects/helpdesk/issues"' in page
    assert out[1]["form"][1] == ["issue[subject]", "Printer down"]
    assert "Issue #1 created." in out[2]["response_text"]


def test_a_hidden_secret_input_is_withheld_whatever_its_length():
    page = '<form><input type="hidden" name="password" value="hunter"></form>'
    out = redact(
        [{"seq": 0, "method": "GET", "url": "http://app.test/", "response_text": page}]
    )

    assert "hunter" not in out[0]["response_text"]
    assert SECRET in out[0]["response_text"]


def test_an_entity_escaped_token_in_the_markup_is_withheld():
    token = "Esc4token&EEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEE"
    page = (
        f'<input type="hidden" name="csrf_token" value="{token.replace("&", "&amp;")}">'
    )
    out = redact(
        [{"seq": 0, "method": "GET", "url": "http://app.test/", "response_text": page}]
    )

    assert "EEEEEEEE" not in out[0]["response_text"]


def test_unparseable_markup_does_not_stop_redaction():
    page = f'<meta name="csrf-token" content="{PAGE_META}"><<!-- <input ="'
    out = redact(
        [{"seq": 0, "method": "GET", "url": "http://app.test/", "response_text": page}]
    )

    assert PAGE_META not in out[0]["response_text"]


def _page(markup, *more):
    lines = [
        {"seq": 0, "method": "GET", "url": "http://app.test/", "response_text": markup}
    ]
    for i, extra in enumerate(more, 1):
        lines.append({"seq": i, **extra})
    return redact(lines)


NUMERIC = "Num5tokenFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF"


def _numeric(text):
    return "".join(f"&#{ord(c)};" for c in text)


@pytest.mark.parametrize(
    "spelled",
    [
        "Amp6token&#38;GGGGGGGGGGGGGGGGGGGGGGGG",  # a numeric reference inside
        "Amp6token&#x26;GGGGGGGGGGGGGGGGGGGGGGGG",  # a hex reference
        _numeric(NUMERIC),  # every character a numeric reference
    ],
)
def test_a_token_in_any_markup_spelling_is_withheld(spelled):
    out = _page(f'<input type="hidden" name="csrf_token" value="{spelled}">')

    text = out[0]["response_text"]
    assert spelled not in text
    assert html.unescape(spelled) not in html.unescape(text)
    assert session_placeholder("csrf_token") in text


@pytest.mark.parametrize(
    "markup, value",
    [
        ('<input type="hidden" name="password" value="abc">', "abc"),
        ('<input type="hidden" name="csrf_token" value="Seven77">', "Seven77"),
        ("<meta name='csrf-token' content=Seven77>", "Seven77"),
        ('<input type=hidden name=api_key value="k1">', "k1"),
    ],
)
def test_a_short_token_or_secret_attribute_is_withheld(markup, value):
    out = _page(markup)

    assert f"={value}" not in out[0]["response_text"].replace('"', "").replace("'", "")


def test_a_token_equal_to_a_field_name_on_the_page_is_withheld():
    token = "SameAsAFieldName0123"
    markup = (
        f'<input name="{token}" value="x">'
        f'<input type="hidden" name="authenticity_token" value="{token}">'
    )
    out = _page(markup)

    assert f'value="{token}"' not in out[0]["response_text"]


def test_a_token_equal_to_a_field_name_in_another_request_is_withheld():
    token = "SameAsAFieldName0123"
    out = _page(
        f'<input type="hidden" name="authenticity_token" value="{token}">',
        {
            "method": "POST",
            "url": "http://app.test/other",
            "form": [[token, "1"]],
        },
    )

    assert token not in out[0]["response_text"]


def test_every_copy_of_a_repeated_value_attribute_is_withheld():
    out = _page('<input type="hidden" name="csrf" value="First111" value="Second22">')

    text = out[0]["response_text"]
    assert "First111" not in text and "Second22" not in text


def test_a_meta_that_names_a_field_or_header_keeps_its_name():
    markup = (
        '<meta name="_csrf_parameter" content="_csrf">'
        '<meta name="_csrf_header" content="X-CSRF-TOKEN">'
        '<meta name="csrf-param" content="authenticity_token">'
    )
    out = _page(markup)

    assert out[0]["response_text"] == markup


def test_a_value_inside_another_attribute_is_not_mistaken_for_the_token():
    markup = (
        '<input data-note=\'value="keep"\' type="hidden" name="csrf_token"'
        ' value="Hidden999token">'
    )
    out = _page(markup)

    text = out[0]["response_text"]
    assert "data-note='value=\"keep\"'" in text
    assert "Hidden999token" not in text


def test_chromium_recovers_no_token_from_the_saved_markup():
    sync_api = pytest.importorskip("playwright.sync_api")
    tokens = {
        "csrf_token": "Amp6token&#38;GGGGGGGGGGGGGGGGGGGGGGGG",
        "authenticity_token": _numeric(NUMERIC),
        "password": "abc",
        "xsrf": "Seven77",
    }
    markup = (
        "<form>"
        + "".join(
            f'<input type="hidden" name="{n}" value="{v}">' for n, v in tokens.items()
        )
        + "</form>"
    )
    saved = _page(markup)[0]["response_text"]
    with sync_api.sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(headless=True)
        except Exception as exc:  # noqa: BLE001 - no browser installed
            pytest.skip(f"chromium unavailable: {exc}")
        try:
            page = browser.new_page()
            page.set_content(saved)
            values = page.eval_on_selector_all(
                "input", "els => els.map((e) => [e.name, e.value])"
            )
        finally:
            browser.close()
    recovered = dict(values)
    for name, spelled in tokens.items():
        assert recovered[name] != html.unescape(spelled)
        assert recovered[name] in (SECRET, session_placeholder(name))


def test_a_token_the_walk_cannot_place_withholds_the_whole_page(monkeypatch):
    import re

    from openadapt_flow import network_capture

    monkeypatch.setattr(network_capture, "_ATTRIBUTE", re.compile(r"(?!)"))
    out = _page('<input type="hidden" name="csrf_token" value="Unplaced0token">')

    assert "Unplaced0token" not in out[0]["response_text"]
