"""Environment manifests and task packs.

An environment directory holds ``env.yaml`` (this module's
:class:`EnvManifest`), a ``verify.yaml`` and one public task pack. Only the
``public`` split may live in a public repository; any other split is loaded
from a path outside the package (``OPENADAPT_ENV_EXTRA_TASKS``), so held-out
and blind fault sets never ship with the environment.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Literal, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, model_validator

MANIFEST_SCHEMA = "openadapt.rl-env-manifest/v1"
PUBLIC_SPLIT = "public"
#: A JSON Lines file of extra tasks (any split) kept outside the repository.
EXTRA_TASKS_ENV = "OPENADAPT_ENV_EXTRA_TASKS"


class TaskError(ValueError):
    """A manifest or task pack is malformed or breaks the publication rule."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Task(_Strict):
    """One episode the environment can reset to.

    ``params`` are the values the task authorizes (which patient, which
    note). They come from the task, never from the agent, and verify-run
    judges the record against them. ``start_path``, ``setup``, ``fault`` and
    ``solution`` stay on the server: the agent sees only ``instruction``.
    """

    id: str
    split: str = PUBLIC_SPLIT
    instruction: str
    start_path: str = "/"
    params: dict[str, str] = Field(default_factory=dict)
    #: Passed to the app's reset hook (for example ``seed_concurrent``).
    setup: dict[str, Any] = Field(default_factory=dict)
    #: False when the app makes the required effect impossible (a fault that
    #: drops the write). The honest outcome is then a ``failure`` report.
    feasible: bool = True
    fault: Optional[str] = None
    #: The scripted reference: actions that reach the maximum reward.
    solution: list[dict[str, Any]] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    #: This task's own ``openadapt.verify/v1`` contract, as a path relative to
    #: the directory that holds it (the environment directory for the public
    #: pack, the extra file's directory otherwise). Unset: the manifest's.
    verify: Optional[str] = None
    _verify_text: Optional[str] = PrivateAttr(default=None)

    @property
    def verify_text(self) -> Optional[str]:
        return self._verify_text

    def public_view(self) -> dict[str, Any]:
        """What the agent and the task-discovery API may see."""
        return {"id": self.id, "split": self.split, "instruction": self.instruction}


class Licence(_Strict):
    #: SPDX id of the environment code and task pack (this package).
    environment: str
    #: SPDX id of the application the agent drives.
    app: str
    #: ``bundled``: the app ships inside this MIT package (synthetic fixture).
    #: ``upstream_image``: pulled from upstream by digest at build or start
    #: time and never copied into this package.
    app_source: Literal["bundled", "upstream_image"]
    upstream_images: list[str] = Field(default_factory=list)
    notes: str = ""

    @model_validator(mode="after")
    def _copyleft_stays_upstream(self) -> "Licence":
        copyleft = ("GPL", "AGPL", "LGPL", "SSPL")
        if any(token in self.app.upper() for token in copyleft):
            if self.app_source != "upstream_image":
                raise ValueError(
                    f"a {self.app} app must be pulled as an upstream image, "
                    "never bundled into a permissive package"
                )
            if not self.upstream_images:
                raise ValueError("an upstream_image app must name its images")
        return self


class AppSpec(_Strict):
    #: ``inprocess``: ``serve`` is ``module:function`` returning
    #: ``(url, store, stop)``; each environment instance runs its own copy.
    #: ``external``: the app already runs (a fixture stack); its URL comes
    #: from ``url_env`` or ``url_default``.
    kind: Literal["inprocess", "external"]
    serve: Optional[str] = None
    url_env: Optional[str] = None
    url_default: Optional[str] = None
    #: How to restore the app before an episode: ``{"http_post": "/path"}``
    #: posts the task's ``setup`` as JSON; ``{"python": "module:function"}``
    #: calls the function with the task's ``setup`` as keyword arguments.
    reset: dict[str, str] = Field(default_factory=dict)
    #: ``module:function(page, app_url)`` run after the start page loads:
    #: unmeasured setup such as signing in, as the app's benchmark declares it.
    prepare: Optional[str] = None
    #: ``module:function()`` returning the app's records from its own
    #: database. The environment serves them read-only on a loopback port the
    #: agent's browser cannot reach, as ``${RECORDS_URL}/records`` for
    #: ``verify.yaml``; use it when the app has no read API the oracle can use.
    records: Optional[str] = None

    @model_validator(mode="after")
    def _source(self) -> "AppSpec":
        if self.kind == "inprocess" and not self.serve:
            raise ValueError("an inprocess app needs serve: module:function")
        if self.kind == "external" and not (self.url_env or self.url_default):
            raise ValueError("an external app needs url_env or url_default")
        if len(self.reset) > 1 or (
            self.reset and next(iter(self.reset)) not in ("http_post", "python")
        ):
            raise ValueError("reset is one of http_post or python")
        return self


class AgentSpec(_Strict):
    #: App paths the agent's browser may not request: the record read paths
    #: the oracle uses, reset routes, and anything else that would let the
    #: agent read or rewrite the answer.
    blocked_paths: list[str] = Field(default_factory=list)
    #: The requests that carry the task's write (``"POST /api/encounter"``).
    #: An infeasible task pays a failure report only if the agent's browser
    #: sent one whose body holds every value in the task's ``params``.
    write_requests: list[str] = Field(default_factory=list)
    max_steps: int = 40
    #: Seconds to wait after an action for the page to settle.
    settle_s: float = 0.3


class EnvManifest(_Strict):
    schema_version: Literal["openadapt.rl-env-manifest/v1"]
    name: str
    title: str
    description: str = ""
    #: What the reward verifies, stated positively, one item per check.
    checks: list[str] = Field(min_length=1)
    licence: Licence
    app: AppSpec
    agent: AgentSpec = AgentSpec()
    verify: str = "verify.yaml"
    tasks: str = "tasks/public.jsonl"


class EnvDirectory(_Strict):
    """A loaded environment directory."""

    directory: Path
    manifest: EnvManifest
    verify_text: str
    tasks: list[Task]
    #: SHA-256 of every task pack loaded, by file name. A buyer compares the
    #: held-out pack's hash with the one delivered to show it was never public.
    pack_sha256: dict[str, str] = Field(default_factory=dict)

    def task(self, task_id: str) -> Task:
        for task in self.tasks:
            if task.id == task_id:
                return task
        raise TaskError(f"no task {task_id!r} in {self.manifest.name}")

    def split(self, name: str) -> list[Task]:
        return [task for task in self.tasks if task.split == name]

    def splits(self) -> list[str]:
        return sorted({task.split for task in self.tasks})


def read_tasks(
    path: Path, *, public_only: bool, base: Optional[Path] = None
) -> list[Task]:
    base = (base or path.parent).resolve()
    tasks: list[Task] = []
    seen: set[str] = set()
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            task = Task.model_validate(json.loads(line))
        except Exception as exc:  # noqa: BLE001 - reported with its location
            raise TaskError(f"{path}:{number}: {exc}") from exc
        if public_only and task.split != PUBLIC_SPLIT:
            raise TaskError(
                f"{path}:{number}: task {task.id} is split {task.split!r}; only "
                f"the {PUBLIC_SPLIT!r} split may ship with an environment. Keep "
                f"other splits outside the package and load them with "
                f"{EXTRA_TASKS_ENV}."
            )
        if task.id in seen:
            raise TaskError(f"{path}:{number}: duplicate task id {task.id}")
        if not task.solution or task.solution[-1].get("kind") != "done":
            raise TaskError(
                f"{path}:{number}: task {task.id} needs a scripted solution "
                "that ends with a done action"
            )
        if task.verify:
            contract = (base / task.verify).resolve()
            if not contract.is_relative_to(base) or not contract.is_file():
                raise TaskError(
                    f"{path}:{number}: task {task.id} verify {task.verify!r} is "
                    f"not a file under {base}"
                )
            task._verify_text = contract.read_text(encoding="utf-8")
        seen.add(task.id)
        tasks.append(task)
    return tasks


def load_environment(directory: str | Path) -> EnvDirectory:
    """Load ``env.yaml``, its ``verify.yaml`` and its tasks.

    Tasks from ``OPENADAPT_ENV_EXTRA_TASKS`` are added when that file exists;
    it must sit outside ``directory`` so the extra splits never ship with it.
    """
    root = Path(directory).resolve()
    try:
        raw = yaml.safe_load((root / "env.yaml").read_text(encoding="utf-8"))
        manifest = EnvManifest.model_validate(raw)
        verify_text = (root / manifest.verify).read_text(encoding="utf-8")
        pack = root / manifest.tasks
        tasks = read_tasks(pack, public_only=True, base=root)
    except (OSError, yaml.YAMLError) as exc:
        raise TaskError(f"cannot load the environment in {root}: {exc}") from exc
    hashes = {pack.name: _sha256(pack)}
    extra = os.environ.get(EXTRA_TASKS_ENV)
    if extra:
        extra_path = Path(extra).resolve()
        if extra_path.is_relative_to(root):
            raise TaskError(
                f"{EXTRA_TASKS_ENV} must point outside the environment directory"
            )
        known = {task.id for task in tasks}
        hashes[extra_path.name] = _sha256(extra_path)
        for task in read_tasks(extra_path, public_only=False):
            if task.id in known:
                raise TaskError(f"extra task {task.id} repeats a public task id")
            tasks.append(task)
    if any(not t.feasible for t in tasks) and not manifest.agent.write_requests:
        raise TaskError(
            f"{manifest.name} has infeasible tasks but no agent.write_requests, "
            "so a failure report could not be told from not trying"
        )
    return EnvDirectory(
        directory=root,
        manifest=manifest,
        verify_text=verify_text,
        tasks=tasks,
        pack_sha256=hashes,
    )


def _sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
