"""Windows desktop environments over the OpenEnv protocol.

The desktop counterpart of :mod:`openadapt_flow.rl_env.environment`. The
task pack, ``verify.yaml`` and reward are the same contract (an episode earns
1 only when verify-run's read of the system of record agrees with the
agent's report); what changes is the surface the agent acts on:

* ``windows_uia``: the agent sees the screen and the app window's UI
  Automation elements (role, name, automation id, bounds) and acts with
  pointer and keyboard input through the in-guest agent (``win_agent``).
* ``rdp_pixels``: the agent sees only the pixels of a Remote Desktop session
  and acts with RDP pointer and keyboard input. No accessibility tree, no
  element ids.

The Windows machine is the buyer's (their own image, or an Azure or AWS
Windows template); nothing here ships Windows. :mod:`.windows_guest` resets
it between episodes over WinRM: the app's database is restored from the
pristine file, the planted fault and UI knobs are set, and the app is
restarted. The restored database's digest must equal the digest of the same
seed built on the server, so an episode that would start from leftover state
is refused instead of run.

The oracle reads the database through a read-only HTTP shim on the server's
loopback interface, which reads the guest over WinRM. The agent has no
network action; its keys are an allowlist with no Windows key, Alt or
function keys; and the apps run full screen over the taskbar.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Literal, Optional

from openenv.core.env_server.interfaces import Environment
from openenv.core.env_server.types import (
    Action,
    EnvironmentMetadata,
    Observation,
    State,
)
from pydantic import Field

from openadapt_flow.rl_env import CONTRACT_VERSION
from openadapt_flow.rl_env.environment import _serve_records
from openadapt_flow.rl_env.reward import EpisodeJudge, Judgement, render_verify_config
from openadapt_flow.rl_env.tasks import (
    PUBLIC_SPLIT,
    DesktopSpec,
    EnvDirectory,
    RdpSpec,
    Task,
    TaskError,
    load_environment,
)

DesktopActionKind = Literal[
    "click", "double_click", "right_click", "type", "press", "scroll", "wait", "done"
]
MAX_WAIT_S = 5.0
#: Server-side switch for reference runs (validation): lets a step name an
#: element by automation id on the pixels-only surface, and lets reset plant
#: a fault other than the task's. An agent's server never sets it.
REFERENCE_ENV = "OPENADAPT_ENV_REFERENCE"
#: Keys an agent may press, after normalization (``rdp_backend.normalize_chord``):
#: editing and navigation keys, Shift with any of them, and Ctrl only with
#: the editing shortcuts. The Windows key, Alt (window switching, the system
#: menu) and the function keys are refused, so key input cannot leave the app.
ALLOWED_KEYS = frozenset(
    {
        "enter",
        "tab",
        "escape",
        "backspace",
        "delete",
        "space",
        "home",
        "end",
        "pageup",
        "pagedown",
        "up",
        "down",
        "left",
        "right",
    }
)
CTRL_KEYS = frozenset({"a", "c", "v", "x", "z", "y", "home", "end"})


def allowed_chord(text: str) -> Optional[str]:
    """The normalized chord when an agent may press it, else None."""
    from openadapt_flow.backends.rdp_backend import normalize_chord

    try:
        parts = normalize_chord(text.strip().replace(" ", ""))
    except ValueError:
        return None
    *mods, base = parts
    if len(set(mods)) != len(mods) or not set(mods) <= {"ctrl", "shift"}:
        return None
    lowered = base.lower()
    single = len(base) == 1 and base.isprintable()
    if not (lowered in ALLOWED_KEYS or single):
        return None
    if "ctrl" in mods and lowered not in CTRL_KEYS:
        return None
    return "+".join([*mods, base])


PACK_DB = (
    Path(__file__).resolve().parents[2]
    / "environments"
    / "openenv"
    / "_windows"
    / "apps"
    / "pack_db.py"
)


def load_desktop_environment(directory: str | Path) -> EnvDirectory:
    """Load an environment directory and check it is a desktop one."""
    env = load_environment(directory)
    if env.manifest.app.kind != "desktop":
        raise TaskError(f"{env.manifest.name} is not a desktop environment")
    return env


def expected_digest(app: str, variant: str = "none") -> str:
    """The digest of a freshly seeded database, built here from the pack's seed.

    The guest's restored database must match it byte for byte, whatever the
    previous episode wrote.
    """
    with tempfile.TemporaryDirectory(prefix="openadapt-winpack-") as tmp:
        env = {**os.environ, "OA_PACK_DIR": tmp}
        out = subprocess.run(
            [sys.executable, str(PACK_DB), "seed", app, "--variant", variant],
            capture_output=True,
            text=True,
            env=env,
            check=True,
        ).stdout
    return str(json.loads(out.strip().splitlines()[-1])["digest"])


class DesktopAction(Action):
    """One desktop action. Coordinates are session pixels."""

    kind: DesktopActionKind
    x: Optional[int] = None
    y: Optional[int] = None
    #: An automation id instead of x/y: allowed on the UIA surface (the
    #: element list carries the ids); on the pixels-only surface only in a
    #: reference run.
    target: Optional[str] = None
    #: With ``target``: a point inside the element as fractions of its width
    #: and height from its top-left corner (a grid's first row, say) instead
    #: of its centre. Fractions hold at any display scale.
    at: Optional[list[float]] = None
    text: Optional[str] = Field(
        default=None, description="type: text; press: key or chord"
    )
    dy: Optional[int] = Field(
        default=None, description="scroll: wheel notches, + is down"
    )
    seconds: Optional[float] = Field(default=None, description="wait: <= 5")
    claim: Optional[Literal["success", "failure"]] = None


class DesktopObservation(Observation):
    contract: str = CONTRACT_VERSION
    surface: str = ""
    task_id: str = ""
    instruction: str = ""
    width: int = 0
    height: int = 0
    screenshot_png_b64: Optional[str] = None
    #: UIA surface only: the app window's elements. Empty on ``rdp_pixels``.
    elements: list[dict[str, Any]] = Field(default_factory=list)
    step: int = 0
    max_steps: int = 0
    last_action_error: Optional[str] = None
    verdict: Optional[str] = None


class DesktopState(State):
    env_name: str = ""
    surface: str = ""
    task_id: Optional[str] = None
    split: Optional[str] = None
    done: bool = False
    claim: Optional[str] = None
    verdict: Optional[str] = None
    reward: Optional[float] = None
    reset_s: Optional[float] = None


def _rdp_backend(spec: RdpSpec, guest: Any) -> Any:
    from openadapt_flow.backends.rdp_backend import AardwolfTransport, FreeRDPBackend

    password = guest.rdp_password()

    transport_kind = os.environ.get("OPENADAPT_WINENV_RDP_TRANSPORT", spec.transport)
    if transport_kind not in ("aardwolf", "xfreerdp"):
        raise TaskError("OPENADAPT_WINENV_RDP_TRANSPORT is aardwolf or xfreerdp")
    transport: Any
    if transport_kind == "aardwolf":
        host, port = guest.rdp_address()
        transport = AardwolfTransport.from_credentials(
            host,
            guest.desktop_user,
            password,
            port=port,
            width=spec.width,
            height=spec.height,
        )
    else:
        from openadapt_flow.backends.xfreerdp import (
            RdpTarget,
            SessionProfile,
            XFreeRDPTransport,
        )

        transport = XFreeRDPTransport(
            RdpTarget(
                host=guest.rdp_address()[0],
                port=guest.rdp_address()[1],
                username=guest.desktop_user,
                ignore_certificate=True,
            ),
            lambda: password,
            profile=SessionProfile(
                width=spec.width,
                height=spec.height,
                color_depth=spec.color_depth,
                lossless=False,
                avc444=spec.avc444,
            ),
        )
    return FreeRDPBackend(transport, max_frame_age_s=5.0)


class OpenAdaptDesktopEnvironment(
    Environment[DesktopAction, DesktopObservation, DesktopState]
):
    """A record-checked Windows desktop task, served over the OpenEnv protocol."""

    SUPPORTS_CONCURRENT_SESSIONS = False
    REQUIRES_SINGLE_THREAD_EXECUTOR = True

    def __init__(self, env_dir: str | Path, *, guest: Any = None) -> None:
        super().__init__()
        self.env: EnvDirectory = load_desktop_environment(env_dir)
        app = self.env.manifest.app
        assert app.surface is not None and app.desktop is not None
        self.surface: str = app.surface
        self.spec: DesktopSpec = app.desktop
        self.reference = os.environ.get(REFERENCE_ENV) == "1"
        self._guest = guest
        self._state = DesktopState(
            env_name=self.env.manifest.name, surface=self.surface
        )
        self._task: Optional[Task] = None
        self._judge: Optional[EpisodeJudge] = None
        self._judgement: Optional[Judgement] = None
        self._records_url: Optional[str] = None
        self._records_stop: Optional[Callable[[], None]] = None
        self._uia: Any = None  # the in-guest agent's backend (privileged)
        self._surface: Any = None  # what the agent acts through
        self._digests: dict[str, str] = {}
        #: Seconds to let the app's write land before the record is read.
        self.finish_settle_s = 1.0

    # -- task discovery -----------------------------------------------------
    def list_splits(self) -> list[str]:
        return self.env.splits()

    def num_tasks(self, split: str = PUBLIC_SPLIT) -> int:
        return len(self.env.split(split))

    def get_task(self, split: str, index: int) -> dict[str, Any]:
        return {"index": index, **self.env.split(split)[index].public_view()}

    def list_tasks(self, split: str = PUBLIC_SPLIT) -> list[dict[str, Any]]:
        return [
            {"index": i, **t.public_view()} for i, t in enumerate(self.env.split(split))
        ]

    def get_metadata(self) -> EnvironmentMetadata:
        m = self.env.manifest
        return EnvironmentMetadata(
            name=m.name, description=m.description or m.title, version=CONTRACT_VERSION
        )

    # -- guest, oracle and surface ------------------------------------------
    def _ensure_guest(self) -> Any:
        if self._guest is None:
            from openadapt_flow.rl_env.windows_guest import WindowsGuest

            self._guest = WindowsGuest()
        if self._records_url is None:
            self._guest.deploy()
            app = self.spec.guest_app
            # The oracle's read path: the guest's database over WinRM or SSH,
            # served on loopback as ${RECORDS_URL}/records (the agent has no
            # network action and never sees the address).
            self._records_url, self._records_stop = _serve_records(
                lambda: self._guest.dump(app)["records"]
            )
        return self._guest

    def _ensure_surface(self) -> None:
        guest = self._ensure_guest()
        if self.surface == "rdp_pixels" and self._surface is None:
            # Connect first: the session moves from the console to RDP, and
            # the in-guest agent must start inside that session.
            self._surface = _rdp_backend(self.spec.rdp, guest)
            self._surface.screenshot()
            time.sleep(6)
            self._uia = None
        if self._uia is None:
            endpoint = guest.launch_agent(token=secrets.token_urlsafe(24))
            self._uia = endpoint.backend()
        if self.surface == "windows_uia":
            self._surface = self._uia

    def _wait_ready(self, timeout_s: float) -> None:
        from openadapt_flow.ir import StructuralLocator

        locator = StructuralLocator(automation_id=self.spec.ready_automation_id)
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                if self._uia.locate_structural(locator) is not None:
                    return
            except Exception:  # noqa: BLE001 - not up yet, or two windows
                pass
            time.sleep(0.25)
        raise TaskError(
            f"{self.spec.window_title} did not come up in {timeout_s:.0f} s"
        )

    # -- OpenEnv API --------------------------------------------------------
    def reset(
        self,
        seed: Optional[int] = None,
        episode_id: Optional[str] = None,
        task_id: Optional[str] = None,
        split: str = PUBLIC_SPLIT,
        index: Optional[int] = None,
        fault: Optional[str] = None,
        **_: Any,
    ) -> DesktopObservation:
        import random

        if task_id is not None:
            task = self.env.task(task_id)
        else:
            pool = self.env.split(split)
            if not pool:
                raise TaskError(f"no tasks in split {split!r}")
            task = pool[
                index if index is not None else random.Random(seed).randrange(len(pool))
            ]
        if fault is not None and not self.reference:
            raise TaskError("planting another fault needs a reference run")
        if self._judge is not None:
            self._judge.close()
        # Until this reset succeeds there is no episode: a failure below
        # leaves nothing a step could act on or be judged against.
        self._judge = None
        self._task, self._judgement = None, None
        self._state = DesktopState(
            env_name=self.env.manifest.name, surface=self.surface, done=True
        )
        started = time.monotonic()
        self._ensure_surface()
        guest = self._guest
        variant = str(task.setup.get("variant", "none"))
        planted = fault if fault is not None else (task.fault or "ok")
        restored = guest.reset(
            self.spec.guest_app,
            fault=planted,
            variant=variant,
            ui=dict(task.setup.get("ui", {})),
        )
        key = f"{self.spec.guest_app}:{variant}"
        if key not in self._digests:
            self._digests[key] = expected_digest(self.spec.guest_app, variant)
        if restored.get("digest") != self._digests[key]:
            raise TaskError(
                "the restored database differs from the pristine seed; refusing "
                "an episode that would start from leftover state"
            )
        self._wait_ready(self.spec.ready_timeout_s)
        reset_s = round(time.monotonic() - started, 2)
        assert self._records_url is not None
        cfg = render_verify_config(
            task.verify_text or self.env.verify_text,
            self._records_url,
            self._records_url,
        )
        episode = episode_id or uuid.uuid4().hex
        self._judge = EpisodeJudge(cfg, task.params, episode_id=episode)
        self._task = task
        self._state = DesktopState(
            env_name=self.env.manifest.name,
            surface=self.surface,
            episode_id=episode,
            step_count=0,
            task_id=task.id,
            split=task.split,
            reset_s=reset_s,
        )
        return self._observe(reward=None, done=False, extra={"reset_s": reset_s})

    def step(
        self, action: DesktopAction, timeout_s: Optional[float] = None, **_: Any
    ) -> DesktopObservation:
        if self._task is None or self._judge is None or self._surface is None:
            raise RuntimeError(
                "call reset() before step(); the last reset did not finish"
            )
        if self._state.done:
            return self._observe(reward=0.0, done=True, error="episode is over")
        self._state.step_count += 1
        if action.kind == "done":
            return self._finish(action.claim)
        error = self._act(action)
        if self._state.step_count >= self.env.manifest.agent.max_steps:
            return self._finish(None, error=error)
        return self._observe(reward=0.0, done=False, error=error)

    @property
    def state(self) -> DesktopState:
        return self._state

    def close(self) -> None:
        closers: list[Callable[[], Any]] = []
        if self._judge is not None:
            closers.append(self._judge.close)
        if self._records_stop is not None:
            closers.append(self._records_stop)
        if self._guest is not None:
            closers.append(self._guest.stop_app)
        if self.surface == "rdp_pixels" and self._surface is not None:
            closers.append(self._surface.close)
        for closer in closers:
            try:
                closer()
            except Exception:  # noqa: BLE001 - best-effort teardown
                pass
        self._judge = self._surface = self._uia = None
        self._records_url = self._records_stop = None

    # -- internals ----------------------------------------------------------
    def _point(self, action: DesktopAction) -> tuple[int, int]:
        if action.target is None:
            if action.x is None or action.y is None:
                raise ValueError(f"{action.kind} needs x and y")
            return int(action.x), int(action.y)
        if self.surface == "rdp_pixels" and not self.reference:
            raise ValueError("the pixels-only surface takes x and y, not element ids")
        from openadapt_flow.ir import StructuralLocator

        handle = self._uia.locate_structural(
            StructuralLocator(automation_id=action.target)
        )
        if handle is None:
            raise ValueError(f"no element {action.target!r}")
        if action.at:
            if handle.region is None:
                raise ValueError(f"element {action.target!r} has no bounds")
            fx, fy = action.at
            if not (0.0 <= fx <= 1.0 and 0.0 <= fy <= 1.0):
                raise ValueError("at is a pair of fractions in [0, 1]")
            x, y, w, h = handle.region
            return int(x + fx * w), int(y + fy * h)
        return int(handle.point[0]), int(handle.point[1])

    def _act(self, action: DesktopAction) -> Optional[str]:
        surface = self._surface
        try:
            if action.kind in ("click", "double_click", "right_click"):
                x, y = self._point(action)
                if action.kind == "right_click":
                    surface.right_click(x, y)
                else:
                    surface.click(x, y, double=action.kind == "double_click")
            elif action.kind == "type":
                surface.type_text(action.text or "")
            elif action.kind == "press":
                key = allowed_chord(action.text or "enter")
                if key is None:
                    return f"key {action.text!r} is not allowed in this environment"
                surface.press(key)
            elif action.kind == "scroll":
                surface.scroll(0, int(action.dy or 3))
            elif action.kind == "wait":
                time.sleep(min(max(action.seconds or 1.0, 0.0), MAX_WAIT_S))
        except Exception as exc:  # noqa: BLE001 - the agent sees the failure
            return f"{type(exc).__name__}: {str(exc).splitlines()[0][:200] if str(exc) else ''}"
        time.sleep(self.env.manifest.agent.settle_s)
        return None

    def _attempted(self) -> bool:
        """Whether the app was asked to save the task's values this episode.

        An infeasible task pays a failure report only after a real try, as on
        the web environments; an unreadable log counts as no try.
        """
        assert self._task is not None
        try:
            asked = self._guest.attempts(self.spec.guest_app)
        except Exception:  # noqa: BLE001 - unread stays unattempted
            return False
        want = {k: str(v) for k, v in self._task.params.items()}
        return any(all(str(row.get(k)) == v for k, v in want.items()) for row in asked)

    def _finish(
        self, claim: Optional[str], error: Optional[str] = None
    ) -> DesktopObservation:
        assert self._task is not None and self._judge is not None
        # The app writes through a child process; let it land first.
        time.sleep(max(self.env.manifest.agent.settle_s, self.finish_settle_s))
        judgement = self._judge.judge(claim, self._task.feasible, self._attempted())
        self._judgement = judgement
        self._state.done = True
        self._state.claim = claim
        self._state.verdict = judgement.verdict
        self._state.reward = judgement.reward
        return self._observe(
            reward=judgement.reward,
            done=True,
            error=error,
            verdict=judgement.verdict,
            extra={"judgement": judgement.as_dict(), "fault": self._task.fault},
        )

    def _observe(
        self,
        *,
        reward: Optional[float],
        done: bool,
        error: Optional[str] = None,
        verdict: Optional[str] = None,
        extra: Optional[dict[str, Any]] = None,
    ) -> DesktopObservation:
        assert self._task is not None
        shot, width, height, elements = None, 0, 0, []
        try:
            png = self._surface.screenshot()
            shot = base64.b64encode(png).decode("ascii")
            width, height = self._surface.viewport
            if self.surface == "windows_uia":
                tree = self._uia.window_tree(self.spec.window_title)
                if tree is not None:
                    elements = [
                        {
                            k: n.get(k)
                            for k in (
                                "role",
                                "name",
                                "automation_id",
                                "bounds",
                                "depth",
                            )
                        }
                        for n in tree[0]
                    ]
        except Exception as exc:  # noqa: BLE001
            error = error or f"observation incomplete: {type(exc).__name__}"
        return DesktopObservation(
            done=done,
            reward=reward,
            metadata={"env": self.env.manifest.name, **(extra or {})},
            surface=self.surface,
            task_id=self._task.id,
            instruction=self._task.instruction,
            width=int(width),
            height=int(height),
            screenshot_png_b64=shot,
            elements=elements,
            step=self._state.step_count,
            max_steps=self.env.manifest.agent.max_steps,
            last_action_error=error,
            verdict=verdict,
        )


def build_app(env_dir: str | Path) -> Any:
    """The OpenEnv FastAPI app for one desktop environment (one guest)."""
    import functools

    from openenv.core.env_server.http_server import create_app

    loaded = load_desktop_environment(env_dir)
    return create_app(
        functools.partial(OpenAdaptDesktopEnvironment, str(loaded.directory)),
        DesktopAction,
        DesktopObservation,
        env_name=loaded.manifest.name,
        max_concurrent_envs=1,
        state_cls=DesktopState,
    )


def main(argv: Optional[list[str]] = None) -> int:
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(description="Serve a Windows desktop environment")
    parser.add_argument("env_dir")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)
    uvicorn.run(build_app(args.env_dir), host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
