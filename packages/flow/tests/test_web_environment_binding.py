"""A project bound to the generic web observer uses it at run time.

A web app with no OpenAdapt markers can't be observed by the backend's own
marker observer, which is what a deployment wires by default for a
qualification case. A project whose boundary names Flow's generic web
observer (or the signed-in session observer) gets that observer instead, and
every id, contract and digest is still compared with the project.
"""

from __future__ import annotations

from typing import Any

from openadapt_flow.qualification import EnvironmentBoundary
from openadapt_flow.qualification_environment import (
    BackendQualificationEnvironmentObserver,
)
from openadapt_flow.qualify.environment import (
    WEB_OBSERVER_CONTRACT_SHA256,
    WEB_OBSERVER_ID,
    WebEnvironmentObserver,
)
from openadapt_flow.runtime.replayer import Replayer

ORIGIN = "http://127.0.0.1:18917"


class _Browser:
    version = "131.0.0.0"


class _Context:
    browser = _Browser()


class _Page:
    context = _Context()

    def __init__(self) -> None:
        self.time_origin = 1000.0

    def evaluate(self, script: str) -> Any:
        if script == "location.origin":
            return ORIGIN
        if script == "navigator.userAgent":
            return "Mozilla/5.0 HeadlessChrome/131.0.0.0"
        if script == "window.devicePixelRatio":
            return 1
        if "timeOrigin" in script:
            return '{"origin":"%s","timeOrigin":%s,"userAgent":"x"}' % (
                ORIGIN,
                self.time_origin,
            )
        raise AssertionError(script)


class _Backend:
    viewport = (1280, 800)
    headless = True

    def __init__(self) -> None:
        self.page = _Page()
        self.guard = None

    def set_qualification_input_guard(self, guard: Any) -> None:
        self.guard = guard


def _boundary(backend: _Backend, **change: Any) -> EnvironmentBoundary:
    observed = WebEnvironmentObserver(application_version="recorded").observe(
        backend, "web"
    )
    fields = dict(
        target_kind="web",
        application="127.0.0.1",
        application_identity=ORIGIN,
        application_version="recorded",
        environment_observer_id=WEB_OBSERVER_ID,
        environment_observer_contract_sha256=WEB_OBSERVER_CONTRACT_SHA256,
        environment_digest=observed.environment_digest,
        runtime_version="test",
    )
    fields.update(change)
    return EnvironmentBoundary(**fields)


def _replayer(backend: _Backend) -> Replayer:
    replayer = Replayer(backend, vision=object())
    # What build_replayer wires for every qualification case.
    replayer.qualification_environment_observer = (
        BackendQualificationEnvironmentObserver(backend)
    )
    return replayer


def test_a_web_observer_project_binds_with_the_web_observer() -> None:
    backend = _Backend()
    refusal, observed, ids = _replayer(backend)._bind_qualification_boundary(
        _boundary(backend)
    )

    assert refusal is None
    assert ids == (WEB_OBSERVER_ID, WEB_OBSERVER_CONTRACT_SHA256)
    assert observed is not None and observed.application_identity == ORIGIN
    assert backend.guard is not None


def test_every_digest_is_still_compared() -> None:
    backend = _Backend()
    other = _boundary(backend, environment_digest="0" * 64)
    refusal, _observed, _ids = _replayer(backend)._bind_qualification_boundary(other)
    assert (
        refusal
        == "observed qualification environment digest does not match the project"
    )

    version = _boundary(backend, application_version="9.9")
    refusal, _observed, _ids = _replayer(backend)._bind_qualification_boundary(version)
    assert refusal is not None


def test_a_reload_mid_run_stops_before_the_next_input() -> None:
    backend = _Backend()
    replayer = _replayer(backend)
    refusal, _observed, _ids = replayer._bind_qualification_boundary(_boundary(backend))
    assert refusal is None

    backend.page.time_origin = 2000.0  # a new page load
    try:
        backend.guard()
    except Exception as exc:  # StructuralResolutionRefused
        assert "changed before input" in str(exc)
    else:  # pragma: no cover - must refuse
        raise AssertionError("a reloaded page was accepted")


def test_a_project_without_an_observer_keeps_the_backend_observer() -> None:
    backend = _Backend()
    plain = _boundary(
        backend,
        environment_observer_id=None,
        environment_observer_contract_sha256=None,
    )
    refusal, _observed, _ids = _replayer(backend)._bind_qualification_boundary(plain)
    assert refusal == "qualification environment observer does not match the project"
