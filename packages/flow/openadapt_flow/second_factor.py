"""Ask a person for a sign-in's second factor while the run waits.

A program step can declare ``Step.one_time``:

- ``"code"``: a TYPE step into a one-time-code field (a code sent by SMS or
  email, or shown by an authenticator app). The program never holds a value.
  When the step runs, the configured source supplies one code; the runtime
  types it and drops it. It is never stored, logged or written to a report.
- ``"approval"``: a WAIT step at a push-approval prompt. The source tells a
  person to approve the sign-in on their own device, and the run continues
  once they say they did. The next step still checks the screen.

Sources, from the environment, first match wins:

- ``OPENADAPT_SECOND_FACTOR_URL`` with ``OPENADAPT_RUNNER_TOKEN``: the control
  plane of a hosted or customer-run runner. The runtime makes a one-run P-256
  key and sends only its public half. The person's browser seals the code to
  that key, so the control plane stores ciphertext it cannot read and deletes
  it on the first read.
- ``OPENADAPT_SECOND_FACTOR_COMMAND``: a local command (for example a password
  manager's one-time-code command). It is run as
  ``<command> code|approval <step id>`` with no shell. Exit 0 prints the code
  (or confirms the approval), exit 3 means the person declined.

- The terminal, when the run has one (stdin and stderr are both a TTY and
  ``OPENADAPT_SECOND_FACTOR_PROMPT`` is not ``off``): the person types the
  code, which is not echoed, or presses Enter after approving.

A run with no terminal (a service, a hosted sandbox, an MCP server) never
prompts. With no source, or when the source fails, times out or declines, the step
stops before anything is typed, with one closed code from
:data:`REFUSAL_CODES`.
"""

from __future__ import annotations

import base64
import os
import re
import shlex
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal, Optional, Protocol
from urllib.parse import urlsplit

__all__ = [
    "COMMAND_ENV",
    "DEFAULT_TIMEOUT_S",
    "ONE_TIME_CODE_MARKER",
    "ONE_TIME_CODE_PARAM",
    "REFUSAL_CODES",
    "SOURCE_ENV_KEYS",
    "TIMEOUT_ENV",
    "URL_ENV",
    "CommandSource",
    "HttpSource",
    "SecondFactorRefused",
    "TerminalSource",
    "check_step_shape",
    "has_one_time_steps",
    "normalize_code",
    "obtain",
    "open_sealed_code",
    "runtime_value",
    "source_env",
    "source_from_env",
    "timeout_from_env",
]

Kind = Literal["code", "approval"]

URL_ENV = "OPENADAPT_SECOND_FACTOR_URL"
TOKEN_ENV = "OPENADAPT_RUNNER_TOKEN"
COMMAND_ENV = "OPENADAPT_SECOND_FACTOR_COMMAND"
TIMEOUT_ENV = "OPENADAPT_SECOND_FACTOR_TIMEOUT_S"
#: ``off`` stops an interactive run from asking at its terminal.
PROMPT_ENV = "OPENADAPT_SECOND_FACTOR_PROMPT"
#: What a run's child processes need for a source: its configuration and,
#: for the control plane, the run token that authorizes the request.
SOURCE_ENV_KEYS = (URL_ENV, TOKEN_ENV, COMMAND_ENV, TIMEOUT_ENV, PROMPT_ENV)


def source_env(environ: Optional[Mapping[str, str]] = None) -> dict[str, str]:
    """The set :data:`SOURCE_ENV_KEYS` of ``environ``, for a child process."""

    environ = os.environ if environ is None else environ
    return {key: environ[key] for key in SOURCE_ENV_KEYS if environ.get(key)}


#: How long a step waits for a person by default, and the bounds an
#: operator may set with :data:`TIMEOUT_ENV`.
DEFAULT_TIMEOUT_S = 120.0
MIN_TIMEOUT_S = 10.0
MAX_TIMEOUT_S = 600.0
#: Exit status a command source uses when the person declined.
DECLINED_EXIT_CODE = 3
#: Seconds between progress lines on stderr while waiting (a supervisor that
#: ends a silent run must not end one that is waiting for a person).
HEARTBEAT_S = 20.0

REFUSAL_CODES = (
    "second_factor_unavailable",
    "second_factor_timeout",
    "second_factor_declined",
    "second_factor_invalid",
)

#: The param a one-time code step names. It is not a task input or a secret
#: parameter, so no call and no vault supplies it.
ONE_TIME_CODE_PARAM = "one_time_code"
#: What a capture agent types into a one-time-code field; the recorder
#: discards it and types the code a person supplies.
ONE_TIME_CODE_MARKER = "\u27e6one-time-code\u27e7"

REQUEST_SCHEMA = "openadapt.second-factor-request/v1"
SEAL_INFO = b"openadapt.second-factor/v1:"

# Codes people get are short runs of digits or letters; some are shown in
# groups ("123 456", "ABC-123"). Spaces and hyphens are dropped.
_CODE = re.compile(r"[A-Za-z0-9]{4,16}\Z")
_STEP_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{7,63}\Z")
_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


class SecondFactorRefused(RuntimeError):
    """The step cannot get its second factor; nothing was typed.

    ``code`` is one of :data:`REFUSAL_CODES`. The message never carries a
    code value or a source's output.
    """

    def __init__(self, code: str, detail: str) -> None:
        if code not in REFUSAL_CODES:
            raise ValueError(f"unknown second-factor refusal {code!r}")
        self.code = code
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True)
class Request:
    kind: Kind
    step_id: str
    timeout_s: float


class Source(Protocol):
    """Where a step's second factor comes from."""

    def obtain(
        self, request: Request, *, heartbeat: Callable[[], None]
    ) -> Optional[str]:
        """The code for ``code`` requests, None once an approval is confirmed.

        Raises :class:`SecondFactorRefused` otherwise.
        """
        ...


def normalize_code(raw: object) -> str:
    """One code with group separators removed, or a refusal."""

    if not isinstance(raw, str):
        raise SecondFactorRefused("second_factor_invalid", "the code is not text")
    code = re.sub(r"[\s-]", "", raw)
    if not _CODE.fullmatch(code):
        raise SecondFactorRefused(
            "second_factor_invalid",
            "the code must be 4 to 16 letters or digits",
        )
    return code


def timeout_from_env(env: Optional[Mapping[str, str]] = None) -> float:
    env = os.environ if env is None else env
    raw = (env.get(TIMEOUT_ENV) or "").strip()
    if not raw:
        return DEFAULT_TIMEOUT_S
    try:
        value = float(raw)
    except ValueError:
        raise SecondFactorRefused(
            "second_factor_unavailable", f"{TIMEOUT_ENV} is not a number"
        ) from None
    if not MIN_TIMEOUT_S <= value <= MAX_TIMEOUT_S:
        raise SecondFactorRefused(
            "second_factor_unavailable",
            f"{TIMEOUT_ENV} must be between {MIN_TIMEOUT_S:g} and "
            f"{MAX_TIMEOUT_S:g} seconds",
        )
    return value


def source_from_env(env: Optional[Mapping[str, str]] = None) -> Optional[Source]:
    """The configured source, or None when none is configured."""

    env = os.environ if env is None else env
    url = (env.get(URL_ENV) or "").strip()
    if url:
        return HttpSource(url, (env.get(TOKEN_ENV) or "").strip())
    command = (env.get(COMMAND_ENV) or "").strip()
    if command:
        return CommandSource(command)
    if (env.get(PROMPT_ENV) or "").strip().lower() not in ("off", "0", "false"):
        terminal = TerminalSource.open()
        if terminal is not None:
            return terminal
    return None


def obtain(
    kind: Kind,
    step_id: str,
    *,
    source: Optional[Source] = None,
    env: Optional[Mapping[str, str]] = None,
    heartbeat: Optional[Callable[[], None]] = None,
) -> Optional[str]:
    """Get one second factor for ``step_id``. See the module docstring."""

    if kind not in ("code", "approval"):
        raise ValueError(f"unknown second-factor kind {kind!r}")
    if not _STEP_ID.fullmatch(step_id or ""):
        raise SecondFactorRefused("second_factor_unavailable", "invalid step id")
    timeout_s = timeout_from_env(env)
    chosen = source if source is not None else source_from_env(env)
    if chosen is None:
        raise SecondFactorRefused(
            "second_factor_unavailable",
            "this sign-in asks for a second factor and no source is set "
            f"({URL_ENV} or {COMMAND_ENV}); nothing was typed",
        )
    beat = heartbeat or _stderr_heartbeat()
    value = chosen.obtain(Request(kind, step_id, timeout_s), heartbeat=beat)
    if kind == "approval":
        return None
    return normalize_code(value)


def check_step_shape(step: Any) -> Any:
    """``ir.Step``'s validator for ``one_time``; returns the step or raises."""

    if step.one_time == "code" and not (
        step.action.value == "type" and step.secret and step.param and step.text is None
    ):
        raise ValueError(
            "a one-time code step is a secret TYPE step that names its param "
            "and carries no text"
        )
    if step.one_time == "approval" and (
        step.action.value != "wait"
        or step.secret
        or step.param is not None
        or step.text is not None
    ):
        raise ValueError("a one-time approval step is a WAIT with no input")
    return step


def has_one_time_steps(workflow: Any) -> bool:
    """Whether any step of ``workflow`` (linear or program graph) is one-time."""

    steps = list(getattr(workflow, "steps", None) or [])
    graphs = [getattr(workflow, "program", None)]
    graphs.extend((getattr(workflow, "subflows", None) or {}).values())
    for graph in graphs:
        for state in (getattr(graph, "states", None) or {}).values():
            if getattr(state, "step", None) is not None:
                steps.append(state.step)
    return any(getattr(step, "one_time", None) for step in steps)


def _stderr_heartbeat() -> Callable[[], None]:
    last = [time.monotonic()]

    def beat() -> None:
        now = time.monotonic()
        if now - last[0] >= HEARTBEAT_S:
            last[0] = now
            print(
                "openadapt-flow: waiting for a person (second factor)",
                file=sys.stderr,
                flush=True,
            )

    return beat


# -- terminal source ---------------------------------------------------------


class TerminalSource:
    """Ask the person at the run's own terminal (no echo for a code)."""

    def __init__(self, tty_in: Any, tty_out: Any) -> None:
        self._in = tty_in
        self._out = tty_out

    @classmethod
    def open(cls) -> Optional["TerminalSource"]:
        try:
            if not (sys.stdin.isatty() and sys.stderr.isatty()):
                return None
        except (AttributeError, ValueError):
            return None
        return cls(sys.stdin, sys.stderr)

    def _ask(self, prompt: str, *, hidden: bool, timeout_s: float) -> Optional[str]:
        import getpass
        import threading

        answer: list[Optional[str]] = [None]

        def read() -> None:
            try:
                if hidden:
                    answer[0] = getpass.getpass(prompt, stream=self._out)
                else:
                    self._out.write(prompt)
                    self._out.flush()
                    answer[0] = self._in.readline()
            except (EOFError, OSError, KeyboardInterrupt):
                answer[0] = None

        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        reader.join(timeout_s)
        if reader.is_alive():
            self._out.write("\n")
            raise SecondFactorRefused(
                "second_factor_timeout",
                f"no second factor within {timeout_s:g} s; nothing was typed",
            )
        if answer[0] is None:
            raise SecondFactorRefused(
                "second_factor_unavailable", "the terminal closed; nothing was typed"
            )
        return answer[0]

    def obtain(
        self, request: Request, *, heartbeat: Callable[[], None]
    ) -> Optional[str]:
        if request.kind == "approval":
            reply = self._ask(
                "Approve the sign-in on your device, then press Enter "
                "(type n and Enter to decline): ",
                hidden=False,
                timeout_s=request.timeout_s,
            )
            if (reply or "").strip().lower() in ("n", "no"):
                raise SecondFactorRefused(
                    "second_factor_declined", "the person declined; nothing was typed"
                )
            return None
        reply = self._ask(
            "Sign-in code (not shown; leave empty to decline): ",
            hidden=True,
            timeout_s=request.timeout_s,
        )
        if not (reply or "").strip():
            raise SecondFactorRefused(
                "second_factor_declined", "the person declined; nothing was typed"
            )
        return reply


# -- command source ----------------------------------------------------------


class CommandSource:
    """Run a local command for each request (no shell)."""

    def __init__(self, command: str) -> None:
        try:
            self._argv = shlex.split(command)
        except ValueError:
            self._argv = []
        if not self._argv:
            raise SecondFactorRefused(
                "second_factor_unavailable", f"{COMMAND_ENV} is not a command"
            )

    def obtain(
        self, request: Request, *, heartbeat: Callable[[], None]
    ) -> Optional[str]:
        heartbeat()
        try:
            done = subprocess.run(
                [*self._argv, request.kind, request.step_id],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=request.timeout_s,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise SecondFactorRefused(
                "second_factor_timeout",
                f"no second factor within {request.timeout_s:g} s; nothing was typed",
            ) from None
        except OSError:
            raise SecondFactorRefused(
                "second_factor_unavailable", "the second-factor command could not start"
            ) from None
        if done.returncode == DECLINED_EXIT_CODE:
            raise SecondFactorRefused(
                "second_factor_declined", "the person declined; nothing was typed"
            )
        if done.returncode != 0:
            raise SecondFactorRefused(
                "second_factor_unavailable",
                f"the second-factor command exited {done.returncode}",
            )
        if request.kind == "approval":
            return None
        try:
            return done.stdout.decode("utf-8").strip()
        except UnicodeDecodeError:
            raise SecondFactorRefused(
                "second_factor_invalid", "the code is not text"
            ) from None


# -- control-plane source ------------------------------------------------------


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64url(text: object, *, size: Optional[int] = None) -> bytes:
    if not isinstance(text, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,4096}", text):
        raise ValueError("not base64url")
    raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    if size is not None and len(raw) != size:
        raise ValueError("wrong length")
    return raw


def _sealing_key(private_key: Any, epk: bytes, request_id: str) -> bytes:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    peer = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), epk)
    shared = private_key.exchange(ec.ECDH(), peer)
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=SEAL_INFO + request_id.encode("ascii"),
    ).derive(shared)


def open_sealed_code(private_key: Any, request_id: str, sealed: object) -> str:
    """Decrypt a code the person's browser sealed to this run's key.

    ``sealed`` is ``{"epk", "iv", "ciphertext"}``: the browser's ephemeral
    P-256 public key (uncompressed point), a 12-byte nonce and the AES-256-GCM
    ciphertext, each base64url. The request id is the associated data.
    """

    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    try:
        if not isinstance(sealed, dict) or set(sealed) != {"epk", "iv", "ciphertext"}:
            raise ValueError("shape")
        epk = _unb64url(sealed["epk"], size=65)
        iv = _unb64url(sealed["iv"], size=12)
        ciphertext = _unb64url(sealed["ciphertext"])
        key = _sealing_key(private_key, epk, request_id)
        plain = AESGCM(key).decrypt(iv, ciphertext, request_id.encode("ascii"))
        return plain.decode("utf-8")
    except Exception:
        raise SecondFactorRefused(
            "second_factor_invalid", "the sealed code could not be opened"
        ) from None


class HttpSource:
    """Ask the run's control plane; a person answers on the dashboard."""

    poll_interval_s = 2.0

    def __init__(
        self,
        url: str,
        token: str,
        *,
        client: Any = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        parts = urlsplit(url)
        local = parts.scheme == "http" and (parts.hostname or "") in _LOCAL_HOSTS
        if (
            url != url.strip()
            or not (parts.scheme == "https" or local)
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or parts.query
            or parts.fragment
        ):
            raise SecondFactorRefused(
                "second_factor_unavailable", f"{URL_ENV} must be an HTTPS URL"
            )
        if not token:
            raise SecondFactorRefused(
                "second_factor_unavailable", f"{TOKEN_ENV} is not set"
            )
        self._url = url.rstrip("/")
        self._token = token
        self._client = client
        self._clock = clock
        self._sleep = sleep

    def _http(self) -> Any:
        if self._client is None:
            import httpx

            self._client = httpx.Client(timeout=15.0, follow_redirects=False)
        return self._client

    def _headers(self) -> dict[str, str]:
        return {
            "authorization": f"Bearer {self._token}",
            "content-type": "application/json",
            "cache-control": "no-store",
        }

    def obtain(
        self, request: Request, *, heartbeat: Callable[[], None]
    ) -> Optional[str]:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec

        private_key = ec.generate_private_key(ec.SECP256R1())
        public = private_key.public_key().public_bytes(
            serialization.Encoding.X962,
            serialization.PublicFormat.UncompressedPoint,
        )
        deadline = self._clock() + request.timeout_s
        request_id = self._create(request, _b64url(public))
        while True:
            heartbeat()
            state, sealed = self._poll(request_id)
            if state == "answered":
                if request.kind != "code":
                    raise SecondFactorRefused(
                        "second_factor_invalid", "a code answered an approval request"
                    )
                return open_sealed_code(private_key, request_id, sealed)
            if state == "approved":
                if request.kind != "approval":
                    raise SecondFactorRefused(
                        "second_factor_invalid", "an approval answered a code request"
                    )
                return None
            if state == "declined":
                raise SecondFactorRefused(
                    "second_factor_declined", "the person declined; nothing was typed"
                )
            if state in ("expired", "consumed") or self._clock() >= deadline:
                raise SecondFactorRefused(
                    "second_factor_timeout",
                    f"no second factor within {request.timeout_s:g} s; "
                    "nothing was typed",
                )
            self._sleep(self.poll_interval_s)

    def _create(self, request: Request, public_key: str) -> str:
        body = {
            "schema": REQUEST_SCHEMA,
            "kind": request.kind,
            "step_id": request.step_id,
            "public_key": public_key,
            "timeout_s": int(request.timeout_s),
        }
        try:
            response = self._http().post(self._url, json=body, headers=self._headers())
            data = response.json() if response.status_code == 201 else None
        except Exception:
            data = None
        request_id = data.get("request_id") if isinstance(data, dict) else None
        if not isinstance(request_id, str) or not _REQUEST_ID.fullmatch(request_id):
            raise SecondFactorRefused(
                "second_factor_unavailable",
                "the control plane did not open a second-factor request",
            )
        return request_id

    def _poll(self, request_id: str) -> tuple[str, object]:
        try:
            response = self._http().get(
                f"{self._url}/{request_id}", headers=self._headers()
            )
        except Exception:
            return "pending", None  # transient; the deadline still applies
        if response.status_code >= 500:
            return "pending", None
        if response.status_code != 200:
            raise SecondFactorRefused(
                "second_factor_unavailable",
                f"the control plane refused the second-factor request "
                f"({response.status_code})",
            )
        try:
            data = response.json()
        except Exception:
            return "pending", None
        state = data.get("state") if isinstance(data, dict) else None
        if state not in (
            "pending",
            "answered",
            "approved",
            "declined",
            "expired",
            "consumed",
        ):
            return "pending", None
        return str(state), data.get("sealed")


def runtime_value(step: Any, result: Any = None) -> tuple[str, Optional[str]]:
    """The replayer's hook: ``(code, None)``, or ``("", refusal message)``.

    An approval returns ``("", None)`` once confirmed. A refusal marks the
    step result a governed refusal; its message starts with the closed code.
    """

    try:
        value = obtain(step.one_time, step.id)
    except SecondFactorRefused as refused:
        if result is not None:
            result.failure_category = "governed_refusal"
        return "", f"Step '{step.id}' ({step.intent}) {refused}"
    return value or "", None
