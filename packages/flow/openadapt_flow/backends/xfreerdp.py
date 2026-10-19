"""Headless FreeRDP sessions with a fixed session profile, and a session pool.

:class:`XFreeRDPTransport` is an :class:`~openadapt_flow.backends.rdp_backend.RDPTransport`
that runs the FreeRDP 3 X11 client (``xfreerdp3``) inside a private virtual
display (``Xvfb``). The existing
:class:`~openadapt_flow.backends.rdp_backend.FreeRDPBackend` drives it, so
compiled workflows, identity checks and read-back run unchanged.

Every session uses the same :class:`SessionProfile`: resolution, colour depth,
display scaling, font smoothing, wallpaper, themes and codecs. A recording
made under one profile replays under the same pixels, and the profile digest
goes into run evidence so a profile change is visible instead of showing up
as drift.

:class:`SessionPool` runs several sessions in parallel, one job per session
at a time, and reports per-job timing and throughput.

Credentials: the password comes from a caller-supplied callable at connect
time and is written to the client's standard input (``/from-stdin:force``).
It never appears on a command line, in an environment variable, in a log or
on this object.

Linux only. Needs ``Xvfb``, ``xfreerdp3`` and ``xdotool`` on PATH and Pillow
built with XCB; ``openbox`` is used when present (the X11 client needs a
window manager for pointer motion to reach the session).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Literal, Optional, Sequence, TypeVar

from openadapt_flow.backends.rdp_backend import Framebuffer

_SCALES = (100, 140, 180)
_DISPLAY_LOCK = threading.Lock()
_FIRST_DISPLAY = 60
_LAST_DISPLAY = 400

# Normalized key names (rdp_backend convention) -> X keysyms for xdotool.
_KEYSYMS = {
    "enter": "Return",
    "return": "Return",
    "tab": "Tab",
    "esc": "Escape",
    "escape": "Escape",
    "backspace": "BackSpace",
    "delete": "Delete",
    "space": "space",
    "up": "Up",
    "down": "Down",
    "left": "Left",
    "right": "Right",
    "home": "Home",
    "end": "End",
    "pageup": "Prior",
    "pagedown": "Next",
    "insert": "Insert",
    "ctrl": "ctrl",
    "control": "ctrl",
    "shift": "shift",
    "alt": "alt",
    "meta": "super",
    "cmd": "super",
    "win": "super",
    **{f"f{i}": f"F{i}" for i in range(1, 13)},
}
_BUTTONS = {"left": "1", "middle": "2", "right": "3"}
#: Delay between typed characters, in milliseconds.
TYPE_DELAY_MS = 12


class XFreeRDPError(RuntimeError):
    """The headless session could not be started or stopped cleanly."""


@dataclass(frozen=True)
class SessionProfile:
    """Fixed display settings shared by every session of a deployment.

    Attributes:
        width, height: Remote desktop size in pixels.
        color_depth: Bits per pixel (16, 24 or 32).
        scale: FreeRDP display scaling, 100, 140 or 180 percent.
        desktop_scale: Scaling the remote desktop applies to its apps, in
            percent. 100 means no Windows display scaling.
        dpi: Virtual display DPI on the client side.
        lossless: Turn off the GFX, RemoteFX and NSCodec lossy codecs so
            frames are pixel-identical across runs.
        font_smoothing: ClearType on the remote. Off keeps glyph pixels
            stable across sessions.
        wallpaper, themes: Off removes pixels that vary between sessions.
        clipboard: Redirect the clipboard (needed for copy-based reads).
        keyboard_layout: FreeRDP ``/kbd:layout`` value, for example ``0x409``.
    """

    width: int = 1280
    height: int = 800
    color_depth: Literal[16, 24, 32] = 24
    scale: int = 100
    desktop_scale: int = 100
    dpi: int = 96
    lossless: bool = True
    font_smoothing: bool = False
    wallpaper: bool = False
    themes: bool = False
    clipboard: bool = True
    keyboard_layout: str = "0x409"

    def __post_init__(self) -> None:
        if self.scale not in _SCALES:
            raise ValueError(f"scale must be one of {_SCALES}")
        if not (100 <= self.desktop_scale <= 500):
            raise ValueError("desktop_scale must be 100..500 percent")
        if self.width < 200 or self.height < 200:
            raise ValueError("width and height must be at least 200 pixels")

    def client_args(self) -> list[str]:
        """``xfreerdp3`` arguments that pin this profile."""
        args = [
            f"/size:{self.width}x{self.height}",
            f"/bpp:{self.color_depth}",
            f"/scale:{self.scale}",
            f"/scale-desktop:{self.desktop_scale}",
            f"/kbd:layout:{self.keyboard_layout}",
        ]
        if self.lossless:
            args += ["-gfx", "-rfx", "-nsc"]
        args.append("+fonts" if self.font_smoothing else "-fonts")
        args.append("+wallpaper" if self.wallpaper else "-wallpaper")
        args.append("+themes" if self.themes else "-themes")
        args.append("+clipboard" if self.clipboard else "-clipboard")
        return args

    @property
    def sha256(self) -> str:
        """Digest of the profile, recorded with each run."""
        body = json.dumps(asdict(self), sort_keys=True).encode("utf-8")
        return hashlib.sha256(body).hexdigest()


@dataclass(frozen=True)
class RdpTarget:
    """Where to connect. Holds no secret."""

    host: str
    port: int = 3389
    username: str = ""
    domain: Optional[str] = None
    ignore_certificate: bool = False


#: Schema tag of a saved session file.
SESSION_SCHEMA = "openadapt.freerdp-session/v1"


@dataclass(frozen=True)
class RdpGateway:
    """An RD Gateway the client connects through. Holds no secret."""

    host: str
    port: int = 443


def save_session(
    path: Path,
    target: RdpTarget,
    profile: Optional[SessionProfile] = None,
    gateway: Optional[RdpGateway] = None,
) -> Path:
    """Write a session file: where to connect and the fixed display profile.

    The file holds no password. The on-prem installer reads its ``target``
    and ``gateway`` hosts to build the no-outbound allow list.
    """
    profile = profile or SessionProfile()
    body = {
        "schema": SESSION_SCHEMA,
        "target": asdict(target),
        "profile": asdict(profile),
        "profile_sha256": profile.sha256,
        "gateway": asdict(gateway) if gateway is not None else None,
    }
    path = Path(path)
    path.write_text(json.dumps(body, indent=2, sort_keys=True) + "\n")
    return path


def load_session(
    path: Path,
) -> tuple[RdpTarget, SessionProfile, Optional[RdpGateway]]:
    """Read a file written by :func:`save_session`.

    Raises:
        ValueError: The file is not a session file, has unknown fields, or
            its profile no longer matches the recorded digest.
    """
    data = json.loads(Path(path).read_text())
    if not isinstance(data, dict) or data.get("schema") != SESSION_SCHEMA:
        raise ValueError(f"{path}: not a {SESSION_SCHEMA} file")
    try:
        target = RdpTarget(**data["target"])
        profile = SessionProfile(**(data.get("profile") or {}))
        raw_gateway = data.get("gateway")
        gateway = RdpGateway(**raw_gateway) if raw_gateway else None
    except (KeyError, TypeError) as exc:
        raise ValueError(f"{path}: {exc}") from exc
    recorded = data.get("profile_sha256")
    if recorded is not None and recorded != profile.sha256:
        raise ValueError(f"{path}: profile does not match profile_sha256")
    return target, profile, gateway


def _require(tool: str) -> str:
    path = shutil.which(tool)
    if path is None:
        raise XFreeRDPError(f"{tool} not found on PATH")
    return path


def _display_free(n: int) -> bool:
    return not (
        Path(f"/tmp/.X11-unix/X{n}").exists() or Path(f"/tmp/.X{n}-lock").exists()
    )


_CLAIMED: set[int] = set()


def _claim_display() -> int:
    """Reserve a free display number.

    Xvfb writes its own lock file only once it is running, so two sessions
    starting together would otherwise pick the same number. The reservation
    is held in-process and, across processes, by an exclusive marker file.
    """
    with _DISPLAY_LOCK:
        for n in range(_FIRST_DISPLAY, _LAST_DISPLAY):
            if n in _CLAIMED or not _display_free(n):
                continue
            try:
                fd = os.open(_marker(n), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                continue
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            _CLAIMED.add(n)
            return n
    raise XFreeRDPError("no free X display number")


def _marker(n: int) -> str:
    return f"/tmp/.oaflow-xfreerdp-{n}.claim"


def _release_display(n: Optional[int]) -> None:
    if n is None:
        return
    with _DISPLAY_LOCK:
        _CLAIMED.discard(n)
        try:
            os.unlink(_marker(n))
        except FileNotFoundError:
            pass


class XFreeRDPTransport:
    """One headless ``xfreerdp3`` session in a private ``Xvfb`` display.

    Args:
        target: Host, port and user.
        password: Called once at connect; its result goes to the client's
            standard input and is not kept.
        profile: The fixed session profile.
        connect_timeout_s: Seconds to wait for the first non-blank frame.
    """

    supports_hwheel = False

    def __init__(
        self,
        target: RdpTarget,
        password: Callable[[], str],
        *,
        profile: SessionProfile = SessionProfile(),
        connect_timeout_s: float = 30.0,
    ) -> None:
        self.target = target
        self.profile = profile
        self._password = password
        self._connect_timeout_s = connect_timeout_s
        self._display: Optional[int] = None
        self._procs: list[subprocess.Popen[bytes]] = []
        self._session_digest: Optional[str] = None
        self._last_pointer = (0, 0)

    # -- lifecycle ---------------------------------------------------------

    @property
    def display(self) -> str:
        if self._display is None:
            raise XFreeRDPError("session is not connected")
        return f":{self._display}"

    def _env(self) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG")}
        env["DISPLAY"] = self.display
        return env

    def _spawn(self, argv: list[str], **kw: Any) -> subprocess.Popen[bytes]:
        proc = subprocess.Popen(
            argv,
            env=self._env(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **kw,
        )
        self._procs.append(proc)
        return proc

    def connect(self) -> None:
        """Start the virtual display and client; block until a frame shows."""
        xvfb = _require("Xvfb")
        client = _require("xfreerdp3")
        _require("xdotool")
        p = self.profile
        self._display = _claim_display()
        try:
            xvfb_proc = self._spawn(
                [
                    xvfb,
                    self.display,
                    "-screen",
                    "0",
                    f"{p.width}x{p.height}x24",
                    "-dpi",
                    str(p.dpi),
                    "-nolisten",
                    "tcp",
                ]
            )
            self._wait(lambda: Path(f"/tmp/.X11-unix/X{self._display}").exists(), 10)
            if xvfb_proc.poll() is not None:
                raise XFreeRDPError(f"Xvfb exited on display {self.display}")
            t = self.target
            argv = [client, f"/v:{t.host}:{t.port}", "/f", "/from-stdin:force"]
            if t.username:
                argv.append(f"/u:{t.username}")
            if t.domain:
                argv.append(f"/d:{t.domain}")
            if t.ignore_certificate:
                argv.append("/cert:ignore")
            argv += p.client_args()
            argv += ["/log-level:ERROR"]
            proc = self._spawn(argv, stdin=subprocess.PIPE)
            assert proc.stdin is not None
            proc.stdin.write((self._password() + "\n").encode("utf-8"))
            proc.stdin.close()
            started = time.time()
            self._wait(lambda: self._frame_has_content(proc), self._connect_timeout_s)
            # Start the window manager only once the session shows a frame:
            # started first, it races the client's fullscreen window and the
            # session can stay unpainted. It is needed for pointer motion.
            wm = shutil.which("openbox")
            if wm:
                self._spawn([wm])
                time.sleep(0.5)
            self._session_digest = hashlib.sha256(
                f"openadapt.xfreerdp/{self._display}/{proc.pid}/{started}".encode()
            ).hexdigest()
        except BaseException:
            self.disconnect()
            raise

    def _wait(self, ready: Callable[[], bool], timeout_s: float) -> None:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if ready():
                return
            time.sleep(0.2)
        raise XFreeRDPError(f"session on {self.display} not ready in {timeout_s}s")

    def _frame_has_content(self, client: subprocess.Popen[bytes]) -> bool:
        if client.poll() is not None:
            raise XFreeRDPError(f"xfreerdp3 exited with code {client.returncode}")
        img, _, _ = self.framebuffer()
        assert not isinstance(img, bytes)
        # The client paints a uniform window until the first remote frame.
        lo, hi = zip(*img.getextrema())
        return lo != hi

    def disconnect(self) -> None:
        """Stop the client and the virtual display (idempotent)."""
        for proc in reversed(self._procs):
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
        self._procs.clear()
        _release_display(self._display)
        self._display = None
        self._session_digest = None

    def session_identity(self) -> Optional[str]:
        """64-hex digest unique to this live session."""
        return self._session_digest

    # -- frames and input --------------------------------------------------

    def framebuffer(self) -> Framebuffer:
        from PIL import ImageGrab

        img = ImageGrab.grab(xdisplay=self.display).convert("RGB")
        return img, img.width, img.height

    def _xdotool(self, *args: str) -> None:
        subprocess.run(
            ["xdotool", *args],
            env=self._env(),
            check=True,
            timeout=10,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def pointer(self, x: int, y: int, button: str, down: bool) -> None:
        self._last_pointer = (int(x), int(y))
        btn = _BUTTONS[button]
        self._xdotool(
            "mousemove",
            str(int(x)),
            str(int(y)),
            "mousedown" if down else "mouseup",
            btn,
        )

    def move(self, x: int, y: int) -> None:
        """Move the pointer with no button edge (a hover)."""
        self._last_pointer = (int(x), int(y))
        self._xdotool("mousemove", str(int(x)), str(int(y)))

    def key(self, keysym_or_char: str, down: bool) -> None:
        name = _KEYSYMS.get(keysym_or_char.lower()) if len(keysym_or_char) > 1 else None
        if name is not None:
            self._xdotool("keydown" if down else "keyup", name)
            return
        if len(keysym_or_char) != 1:
            raise ValueError(f"unknown key {keysym_or_char!r}")
        if down:
            self._xdotool("type", "--delay", str(TYPE_DELAY_MS), "--", keysym_or_char)

    def supports_bulk_text(self, text: str) -> bool:
        """Printable single-line text goes in one ``xdotool type`` call."""
        return text.isprintable() and "\n" not in text

    def bulk_type_text(self, text: str) -> None:
        # A zero inter-key delay drops keystrokes over the RDP input channel.
        self._xdotool("type", "--delay", str(TYPE_DELAY_MS), "--", text)

    def wheel(self, dx: int, dy: int) -> None:
        if dx:
            raise ValueError("horizontal wheel is not supported")
        clicks = max(1, abs(int(dy)) // 40)
        x, y = self._last_pointer
        self._xdotool(
            "mousemove",
            str(x),
            str(y),
            "click",
            "--repeat",
            str(clicks),
            "5" if dy > 0 else "4",
        )


# -- pool --------------------------------------------------------------------

J = TypeVar("J")
T = TypeVar("T")


@dataclass
class JobResult:
    """Outcome of one job on one session."""

    index: int
    session: int
    ok: bool
    seconds: float
    value: Any = None
    error: Optional[str] = None
    detail: Optional[str] = None


@dataclass
class PoolRun:
    """Throughput of one :meth:`SessionPool.run`."""

    sessions: int
    jobs: int
    ok: int
    wall_s: float
    results: list[JobResult] = field(default_factory=list)

    @property
    def jobs_per_minute(self) -> float:
        return 60.0 * self.ok / self.wall_s if self.wall_s else 0.0

    def summary(self) -> dict[str, Any]:
        times = sorted(r.seconds for r in self.results if r.ok)
        median = times[len(times) // 2] if times else None
        return {
            "sessions": self.sessions,
            "jobs": self.jobs,
            "ok": self.ok,
            "failed": self.jobs - self.ok,
            "wall_s": round(self.wall_s, 3),
            "jobs_per_minute": round(self.jobs_per_minute, 2),
            "median_job_s": round(median, 3) if median is not None else None,
        }


class SessionPool:
    """Several connected sessions; each runs one job at a time.

    Args:
        sessions: Unconnected transports, one per slot (each with its own
            login). All must share one :class:`SessionProfile`.
    """

    def __init__(self, sessions: Sequence[XFreeRDPTransport]) -> None:
        profiles = {s.profile.sha256 for s in sessions}
        if len(profiles) > 1:
            raise ValueError("every session in a pool must use the same profile")
        if not sessions:
            raise ValueError("a pool needs at least one session")
        self.sessions = list(sessions)
        self._free: list[int] = []
        self._cv = threading.Condition()

    @property
    def profile_sha256(self) -> str:
        return self.sessions[0].profile.sha256

    def __enter__(self) -> "SessionPool":
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    def start(self) -> None:
        with ThreadPoolExecutor(max_workers=len(self.sessions)) as ex:
            list(ex.map(lambda s: s.connect(), self.sessions))
        self._free = list(range(len(self.sessions)))

    def stop(self) -> None:
        for s in self.sessions:
            s.disconnect()
        self._free = []

    @contextmanager
    def lease(self) -> Iterator[tuple[int, XFreeRDPTransport]]:
        """Borrow one free session; blocks until one is free."""
        with self._cv:
            while not self._free:
                self._cv.wait()
            slot = self._free.pop(0)
        try:
            yield slot, self.sessions[slot]
        finally:
            with self._cv:
                self._free.append(slot)
                self._cv.notify()

    def run(
        self,
        jobs: Sequence[J],
        work: Callable[[XFreeRDPTransport, J], T],
    ) -> PoolRun:
        """Run ``work(session, job)`` for every job across the pool."""

        def one(i: int, job: J) -> JobResult:
            with self.lease() as (slot, session):
                t0 = time.perf_counter()
                try:
                    value = work(session, job)
                    return JobResult(i, slot, True, time.perf_counter() - t0, value)
                except Exception as exc:  # noqa: BLE001 - reported per job
                    return JobResult(
                        i,
                        slot,
                        False,
                        time.perf_counter() - t0,
                        error=type(exc).__name__,
                        detail=str(exc)[:300],
                    )

        t0 = time.perf_counter()
        with ThreadPoolExecutor(max_workers=len(self.sessions)) as ex:
            results = list(ex.map(lambda p: one(*p), enumerate(jobs)))
        wall = time.perf_counter() - t0
        return PoolRun(
            sessions=len(self.sessions),
            jobs=len(jobs),
            ok=sum(r.ok for r in results),
            wall_s=wall,
            results=results,
        )
