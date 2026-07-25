"""Live, PHI-free execution-context identity from the Playwright page."""

from __future__ import annotations

from typing import Any, cast

from openadapt_flow.backend import ExecutionContextIdentityBackend
from openadapt_flow.backends.playwright_backend import PlaywrightBackend


class _MetaLocator:
    def __init__(self, page: "_Page", name: str) -> None:
        self._page = page
        self._name = name

    def count(self) -> int:
        value = self._page.markers.get(self._name)
        return len(value) if isinstance(value, list) else int(value is not None)

    def get_attribute(self, name: str) -> str | None:
        assert name == "content"
        value = self._page.markers.get(self._name)
        return value if isinstance(value, str) else None


class _Page:
    def __init__(self) -> None:
        self.url = "https://app.example.test/"
        self.markers: dict[str, str | list[str]] = {}
        self.locator_calls: list[str] = []
        self.locator_error: Exception | None = None

    def locator(self, selector: str) -> _MetaLocator:
        if self.locator_error is not None:
            raise self.locator_error
        self.locator_calls.append(selector)
        name = selector.removeprefix('head > meta[name="').removesuffix('"]')
        return _MetaLocator(self, name)


def _backend(page: _Page) -> PlaywrightBackend:
    return PlaywrightBackend(page)  # type: ignore[arg-type]


def test_playwright_backend_exposes_execution_context_identity() -> None:
    assert isinstance(_backend(_Page()), ExecutionContextIdentityBackend)


def test_application_identity_observes_live_origin_without_sensitive_url_parts() -> (
    None
):
    page = _Page()
    backend = _backend(page)

    page.url = (
        "https://operator:secret@App.Example.test:8443/"
        "patients/record-123?member_id=private#clinical-note"
    )
    assert backend.application_identity() == "https://app.example.test:8443"

    page.url = "https://replacement.example.test/another-sensitive-record"
    assert backend.application_identity() == "https://replacement.example.test"


def test_application_identity_refuses_non_web_malformed_and_unreadable_urls() -> None:
    page = _Page()
    backend = _backend(page)

    for url in (
        "about:blank",
        "data:text/html,private",
        "https://example.test:invalid/path",
        f"https://{'a' * 254}.test/path",
    ):
        page.url = url
        assert backend.application_identity() is None

    class _UnreadablePage:
        @property
        def url(self) -> str:
            raise RuntimeError("page closed")

    assert (
        PlaywrightBackend(cast(Any, _UnreadablePage())).application_identity() is None
    )


def test_session_identity_observes_current_exact_digest_on_every_call() -> None:
    page = _Page()
    backend = _backend(page)
    first = "a" * 64
    second = "b" * 64

    page.markers["openadapt-session-identity"] = first
    assert backend.session_identity() == first

    page.markers["openadapt-session-identity"] = second
    assert backend.session_identity() == second
    assert page.locator_calls == [
        'head > meta[name="openadapt-session-identity"]',
        'head > meta[name="openadapt-session-identity"]',
    ]


def test_session_identity_returns_none_for_missing_duplicate_or_malformed_marker() -> (
    None
):
    page = _Page()
    backend = _backend(page)

    invalid: tuple[str | list[str] | None, ...] = (
        None,
        ["a" * 64, "b" * 64],
        "A" * 64,
        "a" * 63,
        "a" * 65,
        "g" * 64,
        "a" * 32 + " " + "b" * 31,
    )
    for value in invalid:
        if value is None:
            page.markers.pop("openadapt-session-identity", None)
        else:
            page.markers["openadapt-session-identity"] = value
        assert backend.session_identity() is None


def test_workflow_state_identity_observes_live_bounded_machine_token() -> None:
    page = _Page()
    backend = _backend(page)

    page.markers["openadapt-workflow-state"] = "eligibility.review"
    assert backend.workflow_state_identity() == "eligibility.review"

    page.markers["openadapt-workflow-state"] = "claim.submit-confirmation"
    assert backend.workflow_state_identity() == "claim.submit-confirmation"


def test_workflow_state_identity_refuses_unsafe_or_ambiguous_markers() -> None:
    page = _Page()
    backend = _backend(page)

    invalid: tuple[str | list[str] | None, ...] = (
        None,
        ["review", "submit"],
        "",
        "contains patient name",
        "UPPERCASE",
        "state/record-123",
        "-leading",
        "trailing-",
        "a" * 129,
    )
    for value in invalid:
        if value is None:
            page.markers.pop("openadapt-workflow-state", None)
        else:
            page.markers["openadapt-workflow-state"] = value
        assert backend.workflow_state_identity() is None


def test_context_identity_read_errors_fail_closed() -> None:
    page = _Page()
    backend = _backend(page)
    page.locator_error = RuntimeError("page navigated")

    assert backend.session_identity() is None
    assert backend.workflow_state_identity() is None
