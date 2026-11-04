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
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Literal, Optional

import yaml
from openenv.core.env_server.interfaces import Environment
from openenv.core.env_server.types import (
    Action,
    EnvironmentMetadata,
    Observation,
    State,
)
from pydantic import BaseModel, ConfigDict, Field

from openadapt_flow.rl_env import CONTRACT_VERSION
from openadapt_flow.rl_env.reward import EpisodeJudge, Judgement, render_verify_config
from openadapt_flow.rl_env.tasks import (
    EXTRA_TASKS_ENV,
    MANIFEST_SCHEMA,
    PUBLIC_SPLIT,
    AgentSpec,
    Licence,
    Task,
    TaskError,
    read_tasks,
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


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RdpSpec(_Strict):
    width: int = 1280
    height: int = 800
    color_depth: Literal[16, 24, 32] = 32
    #: ``aardwolf`` (pure Python, any host, RDP bitmap codecs) or ``xfreerdp``
    #: (FreeRDP 3 under Xvfb, Linux; negotiates the server's H.264/AVC444).
    transport: Literal["aardwolf", "xfreerdp"] = "aardwolf"
    #: ``xfreerdp`` only: ask for H.264 (``avc444`` as on a production RDS
    #: host). The server's policy decides what is actually negotiated.
    gfx: Optional[Literal["avc444", "avc420"]] = None


class DesktopAppSpec(_Strict):
    kind: Literal["desktop"]
    surface: Literal["windows_uia", "rdp_pixels"]
    #: The pack app in the guest (``C:\\oa\\pack\\<guest_app>.ps1``).
    guest_app: str
    window_title: str
    #: An automation id that exists once the app is ready for input.
    ready_automation_id: str
    rdp: RdpSpec = RdpSpec()
    #: Seconds the app may take to come up after a reset.
    ready_timeout_s: float = 60.0


class DesktopEnvManifest(_Strict):
    schema_version: Literal["openadapt.rl-env-manifest/v1"]
    name: str
    title: str
    description: str = ""
    licence: Licence
    app: DesktopAppSpec
    agent: AgentSpec = AgentSpec()
    verify: str = "verify.yaml"
    tasks: str = "tasks/public.jsonl"
    #: Where the Windows machine comes from; never shipped with the pack.
    guest_image: str = "buyer-supplied"


class DesktopEnvDirectory(_Strict):
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    directory: Path
    manifest: DesktopEnvManifest
    verify_text: str
    tasks: list[Task]

    def task(self, task_id: str) -> Task:
        for task in self.tasks:
            if task.id == task_id:
                return task
        raise TaskError(f"no task {task_id!r} in {self.manifest.name}")

    def split(self, name: str) -> list[Task]:
        return [task for task in self.tasks if task.split == name]

    def splits(self) -> list[str]:
        return sorted({task.split for task in self.tasks})


def load_desktop_environment(directory: str | Path) -> DesktopEnvDirectory:
    """Load a desktop ``env.yaml``; the same publication rule as the web loader."""
    root = Path(directory).resolve()
    try:
        raw = yaml.safe_load((root / "env.yaml").read_text(encoding="utf-8"))
        manifest = DesktopEnvManifest.model_validate(raw)
    except (OSError, yaml.YAMLError) as exc:
        raise TaskError(f"cannot read {root / 'env.yaml'}: {exc}") from exc
    if manifest.schema_version != MANIFEST_SCHEMA:
        raise TaskError(f"unsupported manifest {manifest.schema_version}")
    verify_text = (root / manifest.verify).read_text(encoding="utf-8")
    tasks = read_tasks(root / manifest.tasks, public_only=True)
    extra = os.environ.get(EXTRA_TASKS_ENV)
    if extra:
        extra_path = Path(extra).resolve()
        if extra_path.is_relative_to(root):
            raise TaskError(
                f"{EXTRA_TASKS_ENV} must point outside the environment directory"
            )
        known = {task.id for task in tasks}
        for task in read_tasks(extra_path, public_only=False):
            if task.id in known:
                raise TaskError(f"extra task {task.id} repeats a public task id")
            tasks.append(task)
    return DesktopEnvDirectory(
        directory=root, manifest=manifest, verify_text=verify_text, tasks=tasks
    )


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


class RecordShim:
    """Serves ``GET /api/db`` from the guest's database, on loopback only.

    The oracle's read path: verify-run's REST verifier reads it, the agent
    has no network action and never sees its address.
    """

    def __init__(self, read: Any) -> None:
        shim = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                if self.path.split("?")[0] != "/api/db":
                    self.send_error(404)
                    return
                try:
                    body = json.dumps(read()).encode()
                except Exception as exc:  # noqa: BLE001 - an unread record fails closed
                    shim.last_error = f"{type(exc).__name__}: {exc}"
                    self.send_error(503)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_: Any) -> None:
                return

        self.last_error: Optional[str] = None
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


class DesktopAction(Action):
    """One desktop action. Coordinates are session pixels."""

    kind: DesktopActionKind
    x: Optional[int] = None
    y: Optional[int] = None
    #: An automation id instead of x/y: allowed on the UIA surface (the
    #: element list carries the ids); on the pixels-only surface only in a
    #: reference run.
    target: Optional[str] = None
    #: With ``target``: offset from the element's top-left corner (a grid's
    #: first row, say) instead of its centre.
    at: Optional[list[int]] = None
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

    if spec.transport == "aardwolf":
        transport = AardwolfTransport.from_credentials(
            guest.host,
            guest.user,
            password,
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
            RdpTarget(host=guest.host, username=guest.user, ignore_certificate=True),
            SessionProfile(
                width=spec.width,
                height=spec.height,
                color_depth=spec.color_depth,
                lossless=False,
                gfx=spec.gfx,
            ),
            password=lambda: password,
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
        self.env = load_desktop_environment(env_dir)
        self.spec = self.env.manifest.app
        self.reference = os.environ.get(REFERENCE_ENV) == "1"
        self._guest = guest
        self._state = DesktopState(
            env_name=self.env.manifest.name, surface=self.spec.surface
        )
        self._task: Optional[Task] = None
        self._judge: Optional[EpisodeJudge] = None
        self._judgement: Optional[Judgement] = None
        self._shim: Optional[RecordShim] = None
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
        if self._shim is None:
            self._guest.deploy()
            app = self.spec.guest_app
            self._shim = RecordShim(lambda: self._guest.dump(app))
        return self._guest

    def _ensure_surface(self) -> None:
        guest = self._ensure_guest()
        if self.spec.surface == "rdp_pixels" and self._surface is None:
            # Connect first: the session moves from the console to RDP, and
            # the in-guest agent must start inside that session.
            self._surface = _rdp_backend(self.spec.rdp, guest)
            self._surface.screenshot()
            time.sleep(6)
            self._uia = None
        if self._uia is None:
            endpoint = guest.launch_agent(token=secrets.token_urlsafe(24))
            self._uia = endpoint.backend()
        if self.spec.surface == "windows_uia":
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
            env_name=self.env.manifest.name, surface=self.spec.surface, done=True
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
        assert self._shim is not None
        cfg = render_verify_config(self.env.verify_text, self._shim.url)
        episode = episode_id or uuid.uuid4().hex
        self._judge = EpisodeJudge(cfg, task.params, episode_id=episode)
        self._task = task
        self._state = DesktopState(
            env_name=self.env.manifest.name,
            surface=self.spec.surface,
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
        for closer in (
            lambda: self._judge and self._judge.close(),
            lambda: self._shim and self._shim.close(),
            lambda: self._guest and self._guest.stop_app(),
            lambda: (
                self.spec.surface == "rdp_pixels"
                and self._surface
                and self._surface.close()
            ),
        ):
            try:
                closer()
            except Exception:  # noqa: BLE001 - best-effort teardown
                pass
        self._judge = self._shim = self._surface = self._uia = None

    # -- internals ----------------------------------------------------------
    def _point(self, action: DesktopAction) -> tuple[int, int]:
        if action.target is None:
            if action.x is None or action.y is None:
                raise ValueError(f"{action.kind} needs x and y")
            return int(action.x), int(action.y)
        if self.spec.surface == "rdp_pixels" and not self.reference:
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
            return int(handle.region[0] + action.at[0]), int(
                handle.region[1] + action.at[1]
            )
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

    def _finish(
        self, claim: Optional[str], error: Optional[str] = None
    ) -> DesktopObservation:
        assert self._task is not None and self._judge is not None
        # The app writes through a child process; let it land first.
        time.sleep(max(self.env.manifest.agent.settle_s, self.finish_settle_s))
        judgement = self._judge.judge(claim, self._task.feasible)
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
            if self.spec.surface == "windows_uia":
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
            surface=self.spec.surface,
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
