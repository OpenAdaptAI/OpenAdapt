"""Hidden token inputs and token meta tags in captured HTML are withheld
whether or not that token was sent."""

from __future__ import annotations

import json

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
