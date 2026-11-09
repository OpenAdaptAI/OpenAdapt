"""Reopen the saved record in a fresh session: desktop relaunch or a second RDP session.

The acting session can show a value it never persisted: a form buffer, a
client cache, a view that was not refetched. This verifier reads the record
somewhere that never held the typed value:

- ``desktop``: a new process of the application, started after the save
  (``command``, run on the machine Flow runs on, or ``start`` steps typed
  through the run's own desktop, for a remote or guest desktop).
- ``rdp``: a second RDP session to the same host, signed in with separate
  credentials taken from environment variables.

In that session it runs the optional ``start`` steps (open the app, sign
in), waits for ``ready_text``, then replays the effect's learned reopen (a
search by a run input or by the reference number the save showed) and reads
the saved value with OCR. Every step runs behind the read-only guard.

It declares :attr:`VerificationTier.INDEPENDENT_SESSION` (tier 2, check level
``separate_session``), so a run it confirms can be VERIFIED.
It is not a system-of-record read: both sessions go through the same
application. Any doubt (no session, a launch the running instance absorbed,
the same account as the run, ``ready_text`` never shown) is INDETERMINATE.

The drivers are tested against scripted backends and the MockMed fault
server only; no live desktop app or RDP host is exercised in CI.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import time
from typing import Any, Callable, ContextManager, Iterator, Literal, Mapping, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from openadapt_flow.assurance import UNCHECKED
from openadapt_flow.runtime.effects.adapter import ConnectionProbe, VerifierAdapterBase
from openadapt_flow.runtime.effects.effect import (
    Effect,
    EffectState,
    EffectVerdict,
    ReadbackNav,
    Verdict,
)
from openadapt_flow.runtime.effects.onscreen import (
    READABLE_CONF,
    OnScreenReadbackVerifier,
    _is_persisted_reacquisition,
    _normalize,
)
from openadapt_flow.verification import VerificationTier

__all__ = [
    "DesktopRelaunch",
    "FreshSessionError",
    "FreshSessionReadbackVerifier",
    "FreshSessionSpec",
    "RdpSession",
    "build_fresh_session_verifier",
]


class FreshSessionError(RuntimeError):
    """A fresh session could not be opened. The message never holds a secret."""


class DesktopRelaunch(BaseModel):
    """Start a new process of the application for the check."""

    model_config = ConfigDict(extra="forbid")

    #: argv to start the app on the machine Flow runs on (no shell). Omit to
    #: start it with ``start`` steps through the run's own desktop instead.
    command: Optional[list[str]] = Field(default=None, min_length=1)
    #: A launch that exits within this many seconds handed itself to the
    #: running instance (a single-instance app): the check halts.
    handoff_grace_s: float = Field(default=2.0, ge=0)


class RdpSession(BaseModel):
    """A second RDP session, signed in as a separate account."""

    model_config = ConfigDict(extra="forbid")

    host: str = Field(min_length=1)
    port: int = Field(default=3389, ge=1, le=65535)
    domain: Optional[str] = None
    #: Environment variables holding the check's account. Never the values.
    username_env: str = Field(min_length=1)
    password_env: str = Field(min_length=1)
    #: Environment variable holding the run's own account, when known; the
    #: check refuses to sign in as the same account.
    acting_username_env: Optional[str] = None
    width: int = Field(default=1280, ge=320)
    height: int = Field(default=800, ge=240)


class FreshSessionSpec(BaseModel):
    """``effects.fresh_session`` in ``deployment.yaml``."""

    model_config = ConfigDict(extra="forbid")

    surface: Literal["desktop", "rdp"]
    desktop: Optional[DesktopRelaunch] = None
    rdp: Optional[RdpSession] = None
    #: Steps run in the new session before the reopen (open the app, sign in).
    start: list[ReadbackNav] = Field(default_factory=list)
    #: Text that shows the app is ready; the reopen waits for it.
    ready_text: Optional[str] = None
    ready_timeout_s: float = Field(default=30.0, gt=0)

    @model_validator(mode="after")
    def _surface_section(self) -> "FreshSessionSpec":
        if self.surface == "desktop":
            if self.desktop is None or self.rdp is not None:
                raise ValueError(
                    "fresh_session.surface desktop needs a desktop section, not rdp"
                )
            if self.desktop.command is None and not self.start:
                raise ValueError(
                    "fresh_session.desktop needs a command or start steps that "
                    "open the app"
                )
        else:
            if self.rdp is None or self.desktop is not None:
                raise ValueError(
                    "fresh_session.surface rdp needs an rdp section, not desktop"
                )
        return self


SessionOpener = Callable[[Any], ContextManager[Any]]


@contextlib.contextmanager
def desktop_relaunch(
    spec: DesktopRelaunch,
    acting_backend: Any,
    *,
    popen: Callable[..., Any] = subprocess.Popen,
    sleep: Callable[[float], None] = time.sleep,
) -> Iterator[Any]:
    """A new app process on the run's desktop; stopped when the check ends."""

    if acting_backend is None:
        raise FreshSessionError("no desktop is bound to relaunch the app on")
    if spec.command is None:
        yield acting_backend  # the start steps open the app
        return
    try:
        proc = popen(
            list(spec.command),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        raise FreshSessionError(
            f"could not start the app ({type(exc).__name__})"
        ) from exc
    try:
        sleep(spec.handoff_grace_s)
        if proc.poll() is not None:
            raise FreshSessionError(
                "the new app process exited at once; a single-instance app "
                "hands the launch to the running window, which is not a fresh "
                "session"
            )
        yield acting_backend
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except Exception:  # noqa: BLE001 - make sure it is gone
                proc.kill()


def _default_rdp_backend(spec: RdpSession, username: str, password: str) -> Any:
    from openadapt_flow.backends.rdp_backend import AardwolfTransport, FreeRDPBackend

    transport = AardwolfTransport.from_credentials(
        spec.host,
        username,
        password,
        domain=spec.domain,
        port=spec.port,
        width=spec.width,
        height=spec.height,
    )
    return FreeRDPBackend(transport, viewport=(spec.width, spec.height))


@contextlib.contextmanager
def rdp_session(
    spec: RdpSession,
    acting_backend: Any = None,
    *,
    environ: Optional[Mapping[str, str]] = None,
    backend_factory: Callable[[RdpSession, str, str], Any] = _default_rdp_backend,
) -> Iterator[Any]:
    """A second RDP session as the check's own account; closed afterwards."""

    env = os.environ if environ is None else environ
    username = env.get(spec.username_env) or ""
    password = env.get(spec.password_env) or ""
    if not username or not password:
        raise FreshSessionError(
            f"set {spec.username_env} and {spec.password_env} for the check's "
            "RDP account"
        )
    if spec.acting_username_env is not None:
        acting = env.get(spec.acting_username_env) or ""
        if acting and acting.strip().lower() == username.strip().lower():
            raise FreshSessionError(
                "the check's RDP account is the run's own account; use a "
                "separate, read-only account"
            )
    try:
        backend = backend_factory(spec, username, password)
    except Exception as exc:  # noqa: BLE001 - never echo connection details
        raise FreshSessionError(
            f"could not open the RDP session ({type(exc).__name__})"
        ) from None
    try:
        yield backend
    finally:
        close = getattr(backend, "close", None)
        if callable(close):
            with contextlib.suppress(Exception):
                close()


class FreshSessionReadbackVerifier(VerifierAdapterBase):
    """Read the saved record back in a fresh desktop process or RDP session."""

    substrate = "fresh_session"
    verification_tier = VerificationTier.INDEPENDENT_SESSION
    independent_system_of_record = False

    def __init__(
        self,
        spec: FreshSessionSpec,
        *,
        open_session: SessionOpener,
        vision: Any = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._spec = spec
        self._open_session = open_session
        if vision is None:
            from openadapt_flow import vision as _vision

            vision = _vision
        self._vision = vision
        self._clock = clock
        self._sleep = sleep
        self._acting: Any = None

    def bind_backend(self, backend: Any) -> None:
        """The run's own backend: read only for a reference number, and the
        desktop a relaunched app opens on."""
        self._acting = backend

    @staticmethod
    def verification_tier_for(effect: Effect) -> VerificationTier:
        return VerificationTier.INDEPENDENT_SESSION

    @staticmethod
    def requires_readable_pre_state_for(effect: Effect) -> bool:
        """The fresh session opens after the save; there is no baseline."""
        return False

    def capture_pre_state(self, context: Any = None) -> EffectState:
        return EffectState(
            substrate=self.substrate,
            reachable=False,
            records=[],
            detail={
                "reason": "the fresh session opens after the save",
                "surface": self._spec.surface,
            },
        )

    def test_connection(self, context: Any = None) -> ConnectionProbe:
        """Open a fresh session, take one frame, close it. Runs no steps."""

        try:
            with self._open_session(self._acting) as fresh:
                frame = fresh.screenshot()
        except Exception as exc:  # noqa: BLE001 - a probe never escapes
            return ConnectionProbe(ok=False, substrate=self.substrate, reason=_why(exc))
        ok = isinstance(frame, bytes) and bool(frame)
        return ConnectionProbe(
            ok=ok,
            substrate=self.substrate,
            reason=(
                f"opened a fresh {self._spec.surface} session"
                if ok
                else "the fresh session returned no frame"
            ),
        )

    def _halt(self, expected: Effect, reason: str) -> EffectVerdict:
        return EffectVerdict(
            verdict=Verdict.INDETERMINATE,
            kind=expected.kind,
            substrate=self.substrate,
            reason=f"{reason}; cannot certify — HALT",
            assurance=UNCHECKED,
        )

    def _wait_ready(self, backend: Any) -> bool:
        from openadapt_flow.runtime.effects.readback_values import token_occurrences

        want = _normalize(self._spec.ready_text or "")
        if not want:
            return True
        deadline = self._clock() + self._spec.ready_timeout_s
        while True:
            lines = self._vision.ocr(backend.screenshot(), region=None) or []
            text = _normalize(
                " ".join(
                    line.text for line in lines if line.confidence >= READABLE_CONF
                )
            )
            if token_occurrences(text, want) >= 1:  # whole tokens
                return True
            if self._clock() >= deadline:
                return False
            self._sleep(0.5)

    def verify(
        self, expected: Effect, before: EffectState, context: Any = None
    ) -> EffectVerdict:
        params = context if isinstance(context, dict) else {}
        spec = expected.readback
        if not _is_persisted_reacquisition(spec):
            return self._halt(
                expected,
                "the effect has no learned reopen to replay in a fresh session",
            )
        assert spec is not None
        if spec.demonstrated and spec.admission is None:
            return self._halt(
                expected,
                "the read-back was taught by demonstration and no person has "
                "admitted it",
            )
        reference: Optional[str] = None
        if spec.reference is not None:
            acting = OnScreenReadbackVerifier(self._acting, vision=self._vision)
            reference, why = acting._capture_reference(spec.reference)
            if reference is None:
                return self._halt(expected, why)
        try:
            with self._open_session(self._acting) as fresh:
                inner = OnScreenReadbackVerifier(
                    fresh, vision=self._vision, reference_value=reference
                )
                if self._spec.start:
                    failed = inner._renavigate(
                        self._spec.start, params, guard=spec.guard
                    )
                    if failed is not None:
                        return self._halt(expected, f"fresh session start: {failed}")
                if not self._wait_ready(fresh):
                    return self._halt(
                        expected, "the fresh session never showed ready_text"
                    )
                verdict = inner.verify(expected, before, params)
        except Exception as exc:  # noqa: BLE001 - any doubt halts
            return self._halt(expected, _why(exc))
        return verdict.model_copy(update={"substrate": self.substrate})


def _why(exc: BaseException) -> str:
    if isinstance(exc, FreshSessionError):
        return str(exc)
    return f"the fresh session failed ({type(exc).__name__})"


def build_fresh_session_verifier(
    spec: FreshSessionSpec,
    *,
    environ: Optional[Mapping[str, str]] = None,
    vision: Any = None,
) -> FreshSessionReadbackVerifier:
    """The verifier ``effects.kind: fresh_session`` builds."""

    def open_session(acting: Any) -> ContextManager[Any]:
        if spec.surface == "rdp":
            assert spec.rdp is not None
            return rdp_session(spec.rdp, acting, environ=environ)
        assert spec.desktop is not None
        return desktop_relaunch(spec.desktop, acting)

    if spec.surface == "rdp":
        assert spec.rdp is not None
        env = os.environ if environ is None else environ
        missing = [
            name
            for name in (spec.rdp.username_env, spec.rdp.password_env)
            if not env.get(name)
        ]
        if missing:
            raise ValueError(
                "effects.fresh_session.rdp needs environment variable(s) "
                + ", ".join(missing)
            )
    return FreshSessionReadbackVerifier(spec, open_session=open_session, vision=vision)
