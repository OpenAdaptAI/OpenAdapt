"""Halted runs the calling agent can help repair, under a person's control.

When a run stops at a step it can't do, ``run_workflow`` adds a
``needs_repair`` block to the result: the closed halt reason, the step id, and
a one-time repair token. It never carries a screen.

What the calling agent may do with the token depends on an operator setting
that only a person changes, with ``openadapt-agent repair access`` on this
computer. Nothing on the MCP surface reads or writes it. The setting is per
workflow, with an optional organization-wide default, and is ``off`` unless a
person turned it on:

``off``
    No screens, and ``teach_repair`` refuses. The run stays halted for a
    person.
``scrubbed_only``
    ``get_repair_screen`` returns the current screen after openadapt-privacy
    removed names, dates of birth, record numbers, and other personal data.
    If scrubbing fails or isn't installed, no screen is shared.
``ask_each_time``
    Like ``scrubbed_only``, but a person must first approve sharing for that
    one token with ``openadapt-agent repair approve-screen``. The command
    shows them the exact scrubbed image the agent will receive, and the agent
    then receives that image and no other.
``allow_unscrubbed``
    Screens are shared without scrubbing. Only the CLI can set it, and it
    asks the person to confirm at a terminal.

``teach_repair`` records one click the agent asks for, performs it through
the operator's demonstrator, and hands the recording to Flow
(``openadapt_flow.repair.agent_teach``). Flow returns a repair candidate that
waits for a person: review, replay and fault campaigns, approval, staging,
and canary, through ``openadapt-flow repair``. The agent's id is on the
candidate, and Flow refuses review or approval by that id. Until a person
approves a repair, the workflow keeps halting at that step. Healthy runs never
touch this module.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import os
import secrets
import shutil
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional, Protocol

from openadapt_flow.agent.runner import HOLD_FILE

__all__ = [
    "SCREEN_MODES",
    "AccessSetting",
    "Demonstrator",
    "HeldRunDemonstrator",
    "LazyBackendDemonstrator",
    "RepairAccessPolicy",
    "RepairDesk",
    "RepairError",
    "default_scrubber",
    "set_access",
    "token_id",
]

_LOG = logging.getLogger(__name__)

SCREEN_MODES = ("off", "scrubbed_only", "ask_each_time", "allow_unscrubbed")
ACCESS_SCHEMA = "openadapt.agent-repair-access/v1"
#: Outcomes a repair may follow: the run stopped before it saved anything.
REPAIRABLE_OUTCOMES = frozenset({"needs_review", "did_not_run"})
_TOKEN_ID_LEN = 12


class RepairError(Exception):
    """A repair request this server refuses. Nothing was shared or changed."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def token_id(token_sha256: str) -> str:
    """The short id a person types to approve a token. Not the token itself."""
    return token_sha256[:_TOKEN_ID_LEN]


def _write_private(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    _write_private(path, json.dumps(payload, indent=2, sort_keys=True).encode())


def _read_json(path: Path) -> Optional[dict[str, Any]]:
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


# -- operator setting --------------------------------------------------------


@dataclass(frozen=True)
class AccessSetting:
    mode: str = "off"
    enabled_by: Optional[str] = None
    scope: str = "default"


class RepairAccessPolicy:
    """The person-set screen access setting. Unreadable means off."""

    def __init__(self, path: Optional[Path | str]) -> None:
        self.path = Path(path) if path is not None else None

    def _load(self) -> dict[str, Any]:
        if self.path is None or not self.path.is_file():
            return {}
        data = _read_json(self.path)
        if data is None or data.get("schema") != ACCESS_SCHEMA:
            _LOG.warning("repair access file is unreadable; screen access stays off")
            return {}
        return data

    @staticmethod
    def _setting(entry: Any, scope: str) -> Optional[AccessSetting]:
        if not isinstance(entry, dict):
            return None
        mode = entry.get("screens")
        enabled_by = entry.get("enabled_by")
        if mode not in SCREEN_MODES:
            return AccessSetting()
        if mode != "off" and not (isinstance(enabled_by, str) and enabled_by):
            return AccessSetting()
        return AccessSetting(mode=mode, enabled_by=enabled_by, scope=scope)

    def for_workflow(self, workflow: str) -> AccessSetting:
        data = self._load()
        workflows = data.get("workflows")
        if isinstance(workflows, dict) and workflow in workflows:
            setting = self._setting(workflows[workflow], "workflow")
            if setting is not None:
                return setting
        return self._setting(data.get("org"), "org") or AccessSetting()

    def any_enabled(self) -> bool:
        data = self._load()
        entries = [data.get("org")]
        workflows = data.get("workflows")
        if isinstance(workflows, dict):
            entries.extend(workflows.values())
        return any(
            (setting := self._setting(entry, "x")) is not None and setting.mode != "off"
            for entry in entries
        )


def set_access(
    path: Path | str, *, workflow: Optional[str], mode: str, enabled_by: str
) -> dict[str, Any]:
    """Write one setting. Called only from the local CLI, never over MCP."""
    if mode not in SCREEN_MODES:
        raise RepairError(f"screens must be one of {', '.join(SCREEN_MODES)}")
    if not enabled_by.strip():
        raise RepairError("name the person turning this on with --by")
    path = Path(path)
    data = _read_json(path) if path.is_file() else None
    if data is None or data.get("schema") != ACCESS_SCHEMA:
        data = {"schema": ACCESS_SCHEMA, "org": {"screens": "off"}, "workflows": {}}
    entry = {"screens": mode, "enabled_by": enabled_by, "changed_at": _now()}
    if workflow is None:
        data["org"] = entry
    else:
        data.setdefault("workflows", {})[workflow] = entry
    _write_json(path, data)
    return data


# -- screens and demonstrations ---------------------------------------------


class Demonstrator(Protocol):
    """The operator's screen: what the agent's demonstration acts on."""

    def screenshot(self) -> bytes: ...

    def click(self, x: int, y: int) -> None: ...

    def viewport(self) -> tuple[int, int]: ...


class LazyBackendDemonstrator:
    """The operator's pinned local backend, opened on first use.

    Uses the same pinning as ``--authoring``: Playwright Chromium for ``url``,
    otherwise the unique frontmost window on macOS or Linux.
    """

    def __init__(self, url: Optional[str] = None, headed: bool = False) -> None:
        self.url = url
        self.headed = headed
        self._backend: Any = None

    def _get(self) -> Any:
        if self._backend is None:
            from openadapt_flow.agent.authoring import pin_local_backend

            try:
                backend, _, _ = pin_local_backend(url=self.url, headed=self.headed)
            except Exception as exc:
                raise RepairError("no screen could be opened for this repair") from exc
            self._backend = backend
        return self._backend

    def screenshot(self) -> bytes:
        return self._get().screenshot()

    def click(self, x: int, y: int) -> None:
        self._get().click(x, y)

    def viewport(self) -> tuple[int, int]:
        viewport = self._get().viewport
        width, height = viewport() if callable(viewport) else viewport
        return int(width), int(height)


class HeldRunDemonstrator:
    """The halted run's own page, which Flow held open (``--hold-on-halt``).

    Talks to the private socket named in ``<run_dir>/repair-hold.json``.
    """

    def __init__(self, hold: dict[str, Any]) -> None:
        self.hold = hold

    @classmethod
    def for_run(cls, run_dir: Path | str) -> Optional["HeldRunDemonstrator"]:
        hold = _read_json(Path(run_dir) / HOLD_FILE)
        if (
            not hold
            or not isinstance(hold.get("socket"), str)
            or not hold.get("secret")
        ):
            return None
        return cls(hold)

    def _call(self, payload: dict[str, Any]) -> dict[str, Any]:
        import socket

        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(30)
                client.connect(self.hold["socket"])
                message = {**payload, "secret": self.hold["secret"]}
                client.sendall(json.dumps(message).encode() + b"\n")
                data = b""
                while not data.endswith(b"\n"):
                    chunk = client.recv(1 << 20)
                    if not chunk:
                        break
                    data += chunk
            reply = json.loads(data.decode())
        except (OSError, ValueError) as exc:
            raise RepairError("the halted run's screen is no longer open") from exc
        if not isinstance(reply, dict) or not reply.get("ok"):
            raise RepairError("the halted run's screen refused the request")
        return reply

    def screenshot(self) -> bytes:
        return base64.b64decode(self._call({"op": "screenshot"})["png_base64"])

    def click(self, x: int, y: int) -> None:
        self._call({"op": "click", "x": x, "y": y})

    def viewport(self) -> tuple[int, int]:
        width, height = self._call({"op": "viewport"})["viewport"]
        return int(width), int(height)

    def release(self) -> None:
        try:
            self._call({"op": "release"})
        except RepairError:
            pass  # already gone; Flow's own timeout cleans up


IMAGE_MODALITY = "PIL_IMAGE"


def default_scrubber() -> Any:
    """openadapt-privacy's Presidio provider. Raises when it isn't usable."""
    from openadapt_privacy import PresidioScrubbingProvider

    return PresidioScrubbingProvider()


def scrub_png(
    png: bytes, scrubber_factory: Callable[[], Any]
) -> tuple[bytes, dict[str, Any]]:
    """Scrub one screen. Any failure raises; the caller then shares nothing."""
    from PIL import Image

    provider = scrubber_factory()
    # openadapt-privacy names this modality PIL_IMAGE (Modality.PIL_IMAGE).
    provider.validate_ready([IMAGE_MODALITY])
    image = Image.open(io.BytesIO(png))
    image.load()
    scrubbed = provider.scrub_image(image)
    if scrubbed is None:
        raise RuntimeError("the scrubber returned no image")
    out = io.BytesIO()
    scrubbed.save(out, format="PNG")
    evidence = provider.evidence([IMAGE_MODALITY])
    if evidence.get("status") != "completed":
        raise RuntimeError("the scrubber did not report a completed scrub")
    return out.getvalue(), {
        "provider": str(evidence.get("provider") or "unknown"),
        "package_version": str(evidence.get("package_version") or "unknown"),
        "policy_sha256": evidence.get("policy_sha256"),
    }


def _flow_agent_teach() -> Any:
    try:
        from openadapt_flow.repair import agent_teach
    except ImportError:
        return None
    return agent_teach


def teach_supported() -> bool:
    return _flow_agent_teach() is not None


# -- the desk ----------------------------------------------------------------


class RepairDesk:
    """Issue repair tokens, share permitted screens, and turn demonstrations
    into Flow repair candidates."""

    def __init__(
        self,
        root: Path | str,
        policy: RepairAccessPolicy,
        *,
        repair_store_root: Optional[Path | str] = None,
        demonstrator: Optional[Demonstrator] = None,
        scrubber_factory: Callable[[], Any] = default_scrubber,
        agent_id: str = "calling-agent",
    ) -> None:
        self.root = Path(root)
        self.policy = policy
        self.repair_store_root = Path(repair_store_root or self.root / "repair-store")
        self.demonstrator = demonstrator
        self.scrubber_factory = scrubber_factory
        self.agent_id = agent_id

    # paths
    def _token_path(self, sha: str) -> Path:
        return self.root / "tokens" / f"{sha}.json"

    def _approval_path(self, sha: str) -> Path:
        return self.root / "approvals" / f"{sha}.json"

    def _preview_path(self, sha: str) -> Path:
        return self.root / "approvals" / f"{sha}.png"

    def _screen_for(self, record: dict[str, Any]) -> Optional[Demonstrator]:
        """Prefer the halted run's held page; else the configured screen."""
        held = HeldRunDemonstrator.for_run(record.get("run_dir") or "")
        return held if held is not None else self.demonstrator

    def store_for(self, workflow: str) -> Path:
        return self.repair_store_root / _sha(workflow)[:16]

    # tokens
    def open(
        self,
        *,
        run_id: str,
        workflow: str,
        step_id: str,
        halt_reason: str,
        bundle_dir: Path | str,
        run_dir: Path | str,
    ) -> dict[str, Any]:
        token = secrets.token_urlsafe(32)
        sha = _sha(token)
        _write_json(
            self._token_path(sha),
            {
                "run_id": run_id,
                "workflow": workflow,
                "step_id": step_id,
                "halt_reason": halt_reason,
                "bundle_dir": str(bundle_dir),
                "run_dir": str(run_dir),
                "created_at": _now(),
                "state": "open",
                "screens_shared": 0,
                "screens_scrubbed": True,
                "scrub_evidence": None,
            },
        )
        return self.needs_repair(token)

    def _load(self, token: Any) -> tuple[str, dict[str, Any]]:
        if not isinstance(token, str) or not 16 <= len(token) <= 128:
            raise RepairError("repair_token is not a token this server issued")
        sha = _sha(token)
        record = _read_json(self._token_path(sha))
        if record is None:
            raise RepairError("repair_token is not a token this server issued")
        return sha, record

    def find(self, short_id: str) -> tuple[str, dict[str, Any]]:
        """Look a token up by the short id a person types."""
        matches = [
            path
            for path in (self.root / "tokens").glob(f"{short_id}*.json")
            if len(short_id) >= 8
        ]
        if len(matches) != 1:
            raise RepairError("no single open repair matches that id")
        record = _read_json(matches[0])
        if record is None:
            raise RepairError("that repair record is unreadable")
        return matches[0].stem, record

    def _approval(self, sha: str) -> Optional[dict[str, Any]]:
        approval = _read_json(self._approval_path(sha))
        if approval is None or approval.get("token_sha256") != sha:
            return None
        preview = self._preview_path(sha)
        if not preview.is_file():
            return None
        if hashlib.sha256(preview.read_bytes()).hexdigest() != approval.get(
            "image_sha256"
        ):
            return None
        return approval

    def needs_repair(self, token: str) -> dict[str, Any]:
        sha, record = self._load(token)
        setting = self.policy.for_workflow(record["workflow"])
        enabled = (
            setting.mode != "off"
            and teach_supported()
            and self._screen_for(record) is not None
        )
        approval_needed = (
            enabled and setting.mode == "ask_each_time" and self._approval(sha) is None
        )
        block: dict[str, Any] = {
            "halt_reason": record["halt_reason"],
            "step_id": record["step_id"],
            "repair_token": token,
            "token_id": token_id(sha),
            "screen_access": setting.mode if enabled else "off",
            "teach_repair_available": enabled and not approval_needed,
            "human_approval_required": approval_needed,
        }
        if not enabled:
            block["next_action"] = (
                "A person must handle this halt. Screen access for repairs is "
                "off for this workflow."
            )
        elif approval_needed:
            block["next_action"] = (
                "Ask a person at this computer to run `openadapt-agent repair "
                f"approve-screen {token_id(sha)}`. It shows them the exact "
                "screen you would see. You can't approve this yourself. Then "
                "call get_repair_screen and teach_repair."
            )
        else:
            block["next_action"] = (
                "Call get_repair_screen to see the screen, then teach_repair "
                "with the one click that gets past this step. A person still "
                "approves the fix before any run uses it."
            )
        return block

    def _require_open(self, sha: str, record: dict[str, Any]) -> AccessSetting:
        if record.get("state") != "open":
            raise RepairError("this repair token was already used")
        setting = self.policy.for_workflow(record["workflow"])
        if setting.mode == "off":
            raise RepairError(
                "screen access for repairs is off for this workflow; a person must handle this halt"
            )
        return setting

    # screens
    def prepare_preview(
        self, sha: str, record: dict[str, Any]
    ) -> tuple[bytes, Optional[dict]]:
        """Capture and scrub the screen a person is asked to approve."""
        setting = self._require_open(sha, record)
        screen = self._screen_for(record)
        if screen is None:
            raise RepairError("no screen is configured for repairs on this server")
        raw = screen.screenshot()
        if setting.mode == "allow_unscrubbed":
            return raw, None
        try:
            return scrub_png(raw, self.scrubber_factory)
        except Exception:
            _LOG.exception("screen scrubbing failed; nothing shared")
            raise RepairError(
                "the screen could not be scrubbed, so no screen was shared"
            ) from None

    def approve_screen(
        self,
        sha: str,
        *,
        approved_by: str,
        image: bytes,
        evidence: Optional[dict[str, Any]],
    ) -> None:
        """Record a person's approval for one token. Local CLI only."""
        record = _read_json(self._token_path(sha))
        if record is None:
            raise RepairError("no such repair")
        if not approved_by.strip() or approved_by == self.agent_id:
            raise RepairError(
                "approval needs a named person other than the calling agent"
            )
        _write_private(self._preview_path(sha), image)
        _write_json(
            self._approval_path(sha),
            {
                "token_sha256": sha,
                "approved_by": approved_by,
                "approved_at": _now(),
                "image_sha256": hashlib.sha256(image).hexdigest(),
                "scrubbed": evidence is not None,
                "scrub_evidence": evidence,
                "mode": self.policy.for_workflow(record["workflow"]).mode,
            },
        )

    def screen(self, token: Any) -> dict[str, Any]:
        sha, record = self._load(token)
        setting = self._require_open(sha, record)
        evidence: Optional[dict[str, Any]] = None
        if setting.mode == "ask_each_time":
            approval = self._approval(sha)
            if approval is None:
                raise RepairError(
                    "a person must approve sharing this screen first: "
                    f"`openadapt-agent repair approve-screen {token_id(sha)}`"
                )
            image = self._preview_path(sha).read_bytes()
            scrubbed = bool(approval.get("scrubbed"))
            evidence = approval.get("scrub_evidence")
        else:
            image, evidence = self.prepare_preview(sha, record)
            scrubbed = evidence is not None
        record["screens_shared"] = int(record.get("screens_shared", 0)) + 1
        record["screens_scrubbed"] = (
            bool(record.get("screens_scrubbed", True)) and scrubbed
        )
        if evidence is not None:
            record["scrub_evidence"] = evidence
        _write_json(self._token_path(sha), record)
        return {
            "token_id": token_id(sha),
            "scrubbed": scrubbed,
            "screen_png_base64": base64.b64encode(image).decode(),
        }

    # teaching
    def _record_click(
        self, screen: Demonstrator, directory: Path, x: int, y: int
    ) -> None:
        before = screen.screenshot()
        screen.click(x, y)
        after = screen.screenshot()
        frames = directory / "frames"
        frames.mkdir(parents=True)
        (frames / "0000_before.png").write_bytes(before)
        (frames / "0000_after.png").write_bytes(after)
        (directory / "events.jsonl").write_text(
            json.dumps({"i": 0, "kind": "click", "x": x, "y": y, "t": 1.0}) + "\n"
        )
        width, height = screen.viewport()
        (directory / "meta.json").write_text(
            json.dumps(
                {
                    "id": f"agent-repair-{directory.name}",
                    "created_at": _now(),
                    "viewport": [width, height],
                    "app_url": "",
                    "params": {},
                }
            )
        )

    def teach(self, token: Any, demonstration: Any) -> dict[str, Any]:
        sha, record = self._load(token)
        setting = self._require_open(sha, record)
        approval = None
        if setting.mode == "ask_each_time":
            approval = self._approval(sha)
            if approval is None:
                raise RepairError(
                    "a person must approve this repair first: "
                    f"`openadapt-agent repair approve-screen {token_id(sha)}`"
                )
        agent_teach = _flow_agent_teach()
        if agent_teach is None:
            raise RepairError("the installed openadapt-flow can't take agent repairs")
        screen = self._screen_for(record)
        if screen is None:
            raise RepairError("no screen is configured for repairs on this server")
        try:
            return self._teach(
                sha, record, setting, approval, agent_teach, screen, demonstration
            )
        finally:
            if isinstance(screen, HeldRunDemonstrator):
                screen.release()

    def _teach(
        self,
        sha: str,
        record: dict[str, Any],
        setting: Any,
        approval: Optional[dict[str, Any]],
        agent_teach: Any,
        screen: Demonstrator,
        demonstration: Any,
    ) -> dict[str, Any]:
        if (
            not isinstance(demonstration, dict)
            or demonstration.get("action") != "click"
        ):
            raise RepairError("demonstration must be {action: click, x, y}")
        x, y = demonstration.get("x"), demonstration.get("y")
        width, height = screen.viewport()
        if not all(isinstance(v, int) and not isinstance(v, bool) for v in (x, y)):
            raise RepairError("x and y must be whole pixel coordinates")
        if not (0 <= x < width and 0 <= y < height):
            raise RepairError("the click is outside the screen")
        # Use the token once, before anything acts, so a retry can't click twice.
        record["state"] = "used"
        _write_json(self._token_path(sha), record)

        from openadapt_flow.repair import RepairStore
        from openadapt_flow.repair.candidate import (
            RepairProvenance,
            ScreenScrubEvidence,
        )

        work = self.root / "teach" / token_id(sha)
        if work.exists():
            shutil.rmtree(work)
        recording = work / "recording"
        self._record_click(screen, recording, x, y)
        evidence = record.get("scrub_evidence")
        provenance = RepairProvenance(
            agent_id=self.agent_id,
            halted_run_id=record["run_id"],
            halted_step_id=record["step_id"],
            repair_token_sha256=sha,
            screen_access_enabled_by=setting.enabled_by or "unknown",
            screen_share_mode=setting.mode,
            screens_shared=int(record.get("screens_shared", 0)),
            screens_scrubbed=bool(record.get("screens_scrubbed", True)),
            scrub_evidence=ScreenScrubEvidence(**evidence) if evidence else None,
            screen_share_approved_by=approval.get("approved_by") if approval else None,
        )
        try:
            result = agent_teach.teach_from_calling_agent(
                record["run_dir"],
                recording,
                work / "proposed",
                bundle=record["bundle_dir"],
                provenance=provenance,
            )
        except Exception as exc:
            _LOG.info("agent repair refused: %s", exc)
            return {
                "status": "refused",
                "what_happened": (
                    "OpenAdapt couldn't turn that click into a repair. The run "
                    "stays halted and a person handles it."
                ),
            }
        store = RepairStore(self.store_for(record["workflow"]))
        candidate = store.add_candidate(result.candidate)
        response: dict[str, Any] = {
            "status": "pending_approval",
            "candidate_id": candidate.candidate_id,
            "what_happened": (
                "OpenAdapt recorded your click as a proposed repair. A person "
                "must review and approve it before any run uses it. Until "
                "then, this workflow still stops at this step."
            ),
        }
        branch_id = self._teach_branch(record, recording, work, store, agent_teach, provenance)
        if branch_id is not None:
            response["branch_candidate_id"] = branch_id
            response["what_happened"] += (
                " This step has now been repaired more than once, so OpenAdapt "
                "also proposed one branch that covers these cases. It needs "
                "the same review and approval."
            )
        return response

    def _demos_path(self, record: dict[str, Any]) -> Path:
        key = _sha(f"{record['workflow']}\0{record['step_id']}")[:16]
        return self.root / "demos" / f"{key}.json"

    def _teach_branch(
        self,
        record: dict[str, Any],
        recording: Path,
        work: Path,
        store: Any,
        agent_teach: Any,
        provenance: Any,
    ) -> Optional[str]:
        """Remember this demonstration; with two or more, propose a branch.

        The branch is a separate repair candidate. If Flow refuses it, the
        single-step candidate stands on its own.
        """
        path = self._demos_path(record)
        demos = (_read_json(path) or {}).get("demos", [])
        demos = [d for d in demos if isinstance(d, dict) and d.get("run_dir") != record["run_dir"]]
        demos.append({"run_dir": record["run_dir"], "recording": str(recording)})
        _write_json(path, {"demos": demos})
        usable = [
            (d["run_dir"], d["recording"])
            for d in demos
            if Path(d["run_dir"]).is_dir() and Path(d["recording"]).is_dir()
        ]
        teach_branch = getattr(agent_teach, "teach_branch_from_calling_agent", None)
        if len(usable) < 2 or teach_branch is None:
            return None
        try:
            result = teach_branch(
                usable,
                work / "branch",
                bundle=record["bundle_dir"],
                provenance=provenance,
            )
        except Exception as exc:
            _LOG.info("branch from several demonstrations refused: %s", exc)
            return None
        return str(store.add_candidate(result.candidate).candidate_id)

    def active_bundle(self, workflow: str, default: Path) -> Path:
        """The approved repaired bundle, when a person activated one."""
        try:
            from openadapt_flow.repair import RepairStore
        except ImportError:
            return default
        store_dir = self.store_for(workflow)
        if not store_dir.is_dir():
            return default
        try:
            pointer = RepairStore(store_dir).active_pointer()
        except Exception:
            _LOG.exception("unreadable repair pointer; using the original bundle")
            return default
        if pointer is None:
            return default
        path = RepairStore(store_dir).bundle_path(pointer.active_digest)
        return path if path.is_dir() else default


def confirm_at_terminal(prompt: str, expected: str) -> bool:
    """A person types ``expected`` at a real terminal. Pipes are refused."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return False
    try:
        answer = input(prompt)
    except EOFError:
        return False
    return answer.strip() == expected
