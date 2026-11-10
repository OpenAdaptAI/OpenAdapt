"""Real-browser failed/absent submit captures never become admitted bindings."""

from __future__ import annotations

import json

import pytest

from openadapt_flow.network_capture import load
from tests import test_api_shape_boundaries as boundaries
from tests.test_api_shape_boundaries import browser as browser
from tests.test_api_shape_boundaries import site_page as site_page


def _observe_admission(monkeypatch):
    calls = []
    original = boundaries.admit_api_step

    def observed(*args, **kwargs):
        calls.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(boundaries, "admit_api_step", observed)
    return calls


def _assert_proposal_refused(failure, site, admissions):
    diagnostic = json.loads(str(failure.value).splitlines()[0])
    assert diagnostic["proposal_reason_codes"] == ["no_form_request"]
    assert diagnostic["capture"]["available"] is True
    assert site.last_capture_diagnostics == diagnostic["capture"]
    assert admissions == []
    assert site.writes() == []
    assert diagnostic["capture"]["server_post_count"] == 0
    assert diagnostic["capture"]["page_pending_write_count"] == 0
    return diagnostic["capture"]


def test_required_file_blocks_request_without_binding_or_extra_write(
    tmp_path, site_page, monkeypatch
):
    site, page = site_page
    site.html = boundaries.FORM.replace(
        'method="post"', 'method="post" enctype="multipart/form-data"'
    ).replace(
        "<button", '<input type="file" name="bundle" id="bundle" required><button'
    )
    admissions = _observe_admission(monkeypatch)

    with pytest.raises(AssertionError) as failure:
        boundaries.binding_from_browser(tmp_path, site, page)

    diagnostic = _assert_proposal_refused(failure, site, admissions)
    assert (
        page.locator("#bundle").evaluate("element => element.validity.valueMissing")
        is True
    )
    assert diagnostic["captured_post_count"] == 0
    assert diagnostic["captured_post_failed_count"] == 0
    assert not any(
        line.get("method") == "POST" for line in load(tmp_path / "recording")
    )
    assert (tmp_path / "recording/events.jsonl").is_file()


def test_aborted_post_is_captured_as_failure_without_admission_or_retry(
    tmp_path, site_page, monkeypatch
):
    site, page = site_page
    # Keep the real submit event and HTTP request, without navigating to a
    # browser error page when the explicitly intercepted request is aborted.
    site.html = boundaries.FORM.replace(
        "</body>",
        """<script>
        document.querySelector('#entry').addEventListener('submit', event => {
          event.preventDefault();
          fetch(event.target.action, {
            method: 'POST', body: new URLSearchParams(new FormData(event.target))
          }).catch(() => {});
        });
        </script></body>""",
    )
    aborted = []

    def abort_post(route):
        assert route.request.method == "POST"
        aborted.append(True)
        route.abort("failed")

    page.route(site.base + "/publish", abort_post)
    admissions = _observe_admission(monkeypatch)

    with pytest.raises(AssertionError) as failure:
        boundaries.binding_from_browser(tmp_path, site, page)

    diagnostic = _assert_proposal_refused(failure, site, admissions)
    posts = [
        line for line in load(tmp_path / "recording") if line.get("method") == "POST"
    ]
    assert aborted == [True]
    assert len(posts) == 1
    assert posts[0].get("failed") is True
    assert posts[0].get("status") is None
    assert diagnostic["captured_post_count"] == 1
    assert diagnostic["captured_post_failed_count"] == 1
    assert (tmp_path / "recording/events.jsonl").is_file()
