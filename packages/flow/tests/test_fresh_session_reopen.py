"""Fresh-session reopen (desktop relaunch, second RDP session) on mocks.

The apps here are scripted backends over the MockMed fault server; ground
truth is the server's store. No live desktop app or RDP host runs.
"""

from __future__ import annotations

import contextlib
from types import SimpleNamespace

import pytest

from openadapt_flow.deployment import EffectsConfig, build_effect_verifier
from openadapt_flow.runtime.effects.effect import ReadbackNav, Verdict
from openadapt_flow.runtime.effects.fresh_session import (
    DesktopRelaunch,
    FreshSessionError,
    FreshSessionReadbackVerifier,
    FreshSessionSpec,
    RdpSession,
    desktop_relaunch,
    rdp_session,
)
from openadapt_flow.runtime.effects.onscreen import OnScreenReadbackVerifier
from openadapt_flow.verification import VerificationTier
from tests.test_readback_reopen_faults import (
    FAULTS,
    NOTE,
    PARAMS,
    SAVE_AT,
    SEARCH_PATIENT,
    SEARCH_REFERENCE,
    FaultApp,
    _effect,
    vision_for,
)


@pytest.fixture(scope="module")
def server():
    from openadapt_flow.mockmed.fault_server import serve as fault_serve

    base_url, db, stop = fault_serve()
    yield base_url.rstrip("/"), db
    stop()


class CachingApp(FaultApp):
    """An app that keeps what it sent and shows it on reopen, saved or not."""

    def save(self) -> None:
        super().save()
        self.cached = {"id": 0, "patient_id": "p1", "type": "Triage", "note": NOTE}

    def _open(self) -> None:
        super()._open()
        if self.query.strip() == "p1" and getattr(self, "cached", None):
            self.record = self.cached
            self.screen = "record"


class FreshApp(FaultApp):
    """A new process or session: starts on its home screen, typed nothing."""

    def __init__(self, base: str, fault: str) -> None:
        super().__init__(base, fault)
        self.screen = "home"


class OneVision:
    """OCR that reads whichever app is on screen."""

    def __init__(self) -> None:
        self.app = None

    def ocr(self, png, *, region=None):
        return vision_for(self.app).ocr(png, region=region)


def _fresh(server, acting, *, start=(), ready_text=None, opened=None):
    base, _db = server
    vision = OneVision()
    vision.app = acting

    @contextlib.contextmanager
    def open_session(_acting):
        app = FreshApp(base, acting.fault)
        if opened is not None:
            opened.append(app)
        previous, vision.app = vision.app, app
        try:
            yield app
        finally:
            vision.app = previous

    spec = FreshSessionSpec(
        surface="rdp",
        rdp=RdpSession(host="h", username_env="U", password_env="P"),
        start=list(start),
        ready_text=ready_text,
        ready_timeout_s=1.0,
    )
    clock = iter(range(1000))
    verifier = FreshSessionReadbackVerifier(
        spec,
        open_session=open_session,
        vision=vision,
        clock=lambda: float(next(clock)),
        sleep=lambda _s: None,
    )
    verifier.bind_backend(acting)
    return verifier


def _saved(server, fault, cls=FaultApp):
    base, db = server
    db.reset()
    app = cls(base, fault)
    app.save()
    return app, db


def test_declares_a_separate_session_not_a_system_of_record():
    assert FreshSessionReadbackVerifier.verification_tier is (
        VerificationTier.INDEPENDENT_SESSION
    )
    assert not VerificationTier.INDEPENDENT_SESSION.reads_system_of_record()


def test_a_client_cache_fools_the_acting_session_but_not_a_fresh_one(server):
    # no_persist: the server answers 200 and stores nothing. The app shows its
    # own cached copy when the record is reopened in the same session.
    app, db = _saved(server, "no_persist", CachingApp)
    effect = _effect(SEARCH_PATIENT).resolve(PARAMS)
    same = OnScreenReadbackVerifier(app, vision=vision_for(app))
    assert same.verify(effect, same.capture_pre_state(), PARAMS).verdict is (
        Verdict.CONFIRMED
    )  # the false confirm a fresh session exists to catch
    app, db = _saved(server, "no_persist", CachingApp)
    fresh = _fresh(server, app)
    verdict = fresh.verify(effect, fresh.capture_pre_state(), PARAMS)
    assert verdict.verdict is not Verdict.CONFIRMED
    assert verdict.substrate == "fresh_session"
    assert db.snapshot()["records"] == []


@pytest.mark.parametrize("fault", sorted(FAULTS))
def test_fresh_session_search_never_confirms_a_save_that_did_not_land(server, fault):
    app, db = _saved(server, fault, CachingApp)
    rows = len(db.snapshot()["records"])
    fresh = _fresh(server, app)
    verdict = fresh.verify(
        _effect(SEARCH_PATIENT).resolve(PARAMS), fresh.capture_pre_state(), PARAMS
    ).verdict
    assert (verdict is Verdict.CONFIRMED) is FAULTS[fault]
    assert len(db.snapshot()["records"]) == rows


def test_the_reference_number_is_read_in_the_acting_session(server):
    app, _db = _saved(server, "ok")
    fresh = _fresh(server, app)
    effect = _effect(SEARCH_REFERENCE, reference=True).resolve(PARAMS)
    verdict = fresh.verify(effect, fresh.capture_pre_state(), PARAMS)
    assert verdict.verdict is Verdict.CONFIRMED


def test_start_steps_run_behind_the_read_only_guard(server):
    app, db = _saved(server, "ok")
    opened: list = []
    fresh = _fresh(
        server, app, start=[ReadbackNav(action="click", point=SAVE_AT)], opened=opened
    )
    verdict = fresh.verify(
        _effect(SEARCH_PATIENT).resolve(PARAMS), fresh.capture_pre_state(), PARAMS
    )
    assert verdict.verdict is Verdict.INDETERMINATE
    assert "read-only guard" in verdict.reason
    assert opened[0].writes == 0
    assert len(db.snapshot()["records"]) == 1


def test_ready_text_that_never_shows_halts(server):
    app, _db = _saved(server, "ok")
    fresh = _fresh(server, app, ready_text="Patient search")
    verdict = fresh.verify(
        _effect(SEARCH_PATIENT).resolve(PARAMS), fresh.capture_pre_state(), PARAMS
    )
    assert verdict.verdict is Verdict.INDETERMINATE
    assert "ready_text" in verdict.reason


def test_ready_text_that_shows_lets_the_reopen_run(server):
    app, _db = _saved(server, "ok")
    fresh = _fresh(server, app, ready_text="Search")
    verdict = fresh.verify(
        _effect(SEARCH_PATIENT).resolve(PARAMS), fresh.capture_pre_state(), PARAMS
    )
    assert verdict.verdict is Verdict.CONFIRMED


def test_an_unadmitted_learned_check_opens_no_session(server):
    app, _db = _saved(server, "ok")
    opened: list = []
    fresh = _fresh(server, app, opened=opened)
    verdict = fresh.verify(
        _effect(SEARCH_PATIENT, admitted=False).resolve(PARAMS),
        fresh.capture_pre_state(),
        PARAMS,
    )
    assert verdict.verdict is Verdict.INDETERMINATE
    assert opened == []


def test_a_session_that_fails_to_open_halts(server):
    app, _db = _saved(server, "ok")
    spec = FreshSessionSpec(
        surface="rdp", rdp=RdpSession(host="h", username_env="U", password_env="P")
    )

    def broken(_acting):
        raise FreshSessionError("could not open the RDP session (TimeoutError)")

    verifier = FreshSessionReadbackVerifier(spec, open_session=broken)
    verdict = verifier.verify(
        _effect(SEARCH_PATIENT).resolve(PARAMS), verifier.capture_pre_state(), PARAMS
    )
    assert verdict.verdict is Verdict.INDETERMINATE
    assert "could not open the RDP session" in verdict.reason
    assert verifier.test_connection().ok is False


# -- desktop relaunch -----------------------------------------------------------


class FakeProc:
    def __init__(self, exit_at_once: bool) -> None:
        self.returncode = 0 if exit_at_once else None
        self.terminated = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.returncode = -9


def test_desktop_relaunch_starts_and_stops_a_new_process():
    procs: list[FakeProc] = []

    def popen(argv, **kw):
        assert argv == ["/opt/app/bin/app", "--new-window"]
        procs.append(FakeProc(exit_at_once=False))
        return procs[-1]

    acting = object()
    spec = DesktopRelaunch(command=["/opt/app/bin/app", "--new-window"])
    with desktop_relaunch(spec, acting, popen=popen, sleep=lambda _s: None) as got:
        assert got is acting
        assert procs[0].terminated is False
    assert procs[0].terminated is True


def test_a_launch_absorbed_by_the_running_instance_is_refused():
    spec = DesktopRelaunch(command=["app"])
    with pytest.raises(FreshSessionError, match="single-instance"):
        with desktop_relaunch(
            spec,
            object(),
            popen=lambda argv, **kw: FakeProc(exit_at_once=True),
            sleep=lambda _s: None,
        ):
            pass


def test_desktop_needs_a_command_or_start_steps():
    with pytest.raises(ValueError, match="command or start steps"):
        FreshSessionSpec(surface="desktop", desktop=DesktopRelaunch())
    FreshSessionSpec(
        surface="desktop",
        desktop=DesktopRelaunch(),
        start=[ReadbackNav(action="key", key="win+r")],
    )


# -- second RDP session -----------------------------------------------------------

RDP = RdpSession(
    host="10.0.0.5",
    username_env="CHECK_USER",
    password_env="CHECK_PASS",
    acting_username_env="RUN_USER",
)


def test_rdp_signs_in_with_the_checks_own_account_and_closes():
    seen = {}
    closed = []

    def factory(spec, user, password):
        seen.update(user=user, password=password)
        return SimpleNamespace(close=lambda: closed.append(True))

    env = {"CHECK_USER": "reader", "CHECK_PASS": "s3cret", "RUN_USER": "clerk"}
    with rdp_session(RDP, environ=env, backend_factory=factory):
        pass
    assert seen == {"user": "reader", "password": "s3cret"}
    assert closed == [True]


def test_rdp_refuses_the_runs_own_account():
    env = {"CHECK_USER": "Clerk", "CHECK_PASS": "x", "RUN_USER": "clerk"}
    with pytest.raises(FreshSessionError, match="run's own account"):
        with rdp_session(RDP, environ=env, backend_factory=lambda *a: None):
            pass


def test_rdp_errors_never_echo_credentials():
    env = {"CHECK_USER": "reader", "CHECK_PASS": "s3cret-value"}

    def factory(spec, user, password):
        raise ConnectionError(f"login failed for {user}:{password}")

    with pytest.raises(FreshSessionError) as info:
        with rdp_session(RDP, environ=env, backend_factory=factory):
            pass
    assert "s3cret-value" not in str(info.value)
    assert info.value.__cause__ is None
    with pytest.raises(FreshSessionError, match="CHECK_USER"):
        with rdp_session(RDP, environ={}, backend_factory=factory):
            pass


# -- deployment.yaml --------------------------------------------------------------


def test_effects_kind_fresh_session_builds_the_verifier(monkeypatch):
    monkeypatch.setenv("CHECK_USER", "reader")
    monkeypatch.setenv("CHECK_PASS", "x")
    cfg = EffectsConfig.model_validate(
        {
            "kind": "fresh_session",
            "fresh_session": {
                "surface": "rdp",
                "rdp": {
                    "host": "10.0.0.5",
                    "username_env": "CHECK_USER",
                    "password_env": "CHECK_PASS",
                },
                "ready_text": "Patient search",
            },
        }
    )
    verifier = build_effect_verifier(cfg)
    assert isinstance(verifier, FreshSessionReadbackVerifier)
    monkeypatch.delenv("CHECK_PASS")
    with pytest.raises(ValueError, match="CHECK_PASS"):
        build_effect_verifier(cfg)
    with pytest.raises(ValueError, match="fresh_session section"):
        build_effect_verifier(EffectsConfig(kind="fresh_session"))
