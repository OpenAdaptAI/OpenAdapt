"""Evidence-driven headed complex visual workflow campaign."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import statistics
import subprocess
import sys
import time
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

import numpy as np
from PIL import Image

from benchmark.complex_visual.observer import Snapshot, verify_observation
from benchmark.complex_visual.protocol import CARD_COLUMNS, canonical_json, decode_task_card
from benchmark.complex_visual.x11 import X11Session

ROOT = Path(__file__).parent
REPOSITORY_ROOT = ROOT.parents[1]
CAMPAIGN_PATH = ROOT / "campaign.json"
CASES_PATH = ROOT / "cases.json"
WINDOWS = {"inbox": (20, 30), "worklist": (440, 30), "editor": (860, 30)}
WINDOW_SIZE = (390, 560)


@dataclass(frozen=True)
class RuntimeEvidence:
    status: str
    reason: str
    delivery_uncertain: bool
    action_attempts: int
    reconnects: int
    runtime_s: float


@dataclass(frozen=True)
class ActorIdentity:
    username: str
    uid: int
    gid: int


@dataclass(frozen=True)
class Classification:
    outcome: str
    silent_incorrect_successes: int
    over_halts: int
    under_halts: int
    wrong_entity_writes: int
    duplicate_effects: int
    collateral_writes: int
    reconciled_uncertain_deliveries: int


def load_campaign() -> dict:
    return json.loads(CAMPAIGN_PATH.read_text(encoding="utf-8"))


def load_cases() -> dict[str, dict]:
    return json.loads(CASES_PATH.read_text(encoding="utf-8"))["cases"]


def make_truth(case_id: str, condition: str, trial: int) -> dict:
    task = dict(load_cases()[case_id])
    action_id = f"ACT-{condition}-{trial:02d}"
    task["expected_action_id"] = action_id
    task["schema_version"] = "openadapt.visual-task-card.v1"
    return {
        "schema_version": "openadapt.complex-visual-truth.v1",
        "case_id": case_id,
        "expected_action_id": action_id,
        "expected_mail_text": (
            f"Action: {action_id}\nRecord: {task['target_record_id']}\nRoute: {task['route']}\n"
        ),
        "task_card": task,
    }


def _python_environment() -> dict[str, str]:
    return {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(
            filter(None, (str(REPOSITORY_ROOT), os.environ.get("PYTHONPATH", "")))
        ),
    }


def resolve_actor_identity(username: str | None = None) -> ActorIdentity:
    """Require a configured actor account with a different effective UID."""
    if os.name != "posix" or not hasattr(os, "geteuid"):
        raise RuntimeError("the headed authority campaign requires a POSIX host")
    selected = username or os.environ.get("COMPLEX_VISUAL_ACTOR_USER")
    if not selected:
        raise RuntimeError("COMPLEX_VISUAL_ACTOR_USER must name a dedicated OS account")
    import pwd

    try:
        account = pwd.getpwnam(selected)
    except KeyError as exc:
        raise RuntimeError(f"actor OS account {selected!r} does not exist") from exc
    if account.pw_uid == os.geteuid():
        raise RuntimeError("actor OS account must have a distinct effective UID")
    return ActorIdentity(selected, account.pw_uid, account.pw_gid)


def _sudo_path() -> str:
    sudo = shutil.which("sudo")
    if not sudo:
        raise RuntimeError("sudo is required to enter the dedicated actor identity")
    return sudo


def _actor_command(identity: ActorIdentity, command: list[str]) -> list[str]:
    """Build a clean-environment command for the dedicated actor identity."""
    environment = shutil.which("env") or "/usr/bin/env"
    actor_source = os.environ.get("COMPLEX_VISUAL_ACTOR_SOURCE", str(REPOSITORY_ROOT))
    return [
        _sudo_path(),
        "--non-interactive",
        "--user",
        identity.username,
        "--",
        environment,
        "-i",
        f"DISPLAY={os.environ.get('DISPLAY', '')}",
        f"PATH={os.environ.get('PATH', '')}",
        f"PYTHONPATH={actor_source}",
        "PYTHONDONTWRITEBYTECODE=1",
        *command,
    ]


def _actor_python() -> str:
    """Return the Python executable in the actor's read-only runtime."""
    return os.environ.get("COMPLEX_VISUAL_ACTOR_PYTHON", sys.executable)


def _prepare_actor_root(actor_root: Path, identity: ActorIdentity) -> Path:
    """Give the actor one writable directory that the coordinator can read."""
    stderr_path = actor_root / "actor_stderr.log"
    stderr_path.touch(mode=0o640)
    coordinator_gid = os.getegid()
    subprocess.run(
        [
            _sudo_path(),
            "--non-interactive",
            "chown",
            f"{identity.uid}:{coordinator_gid}",
            str(actor_root),
        ],
        check=True,
    )
    subprocess.run(
        [_sudo_path(), "--non-interactive", "chmod", "2750", str(actor_root)],
        check=True,
    )
    return stderr_path


class FixtureProcess:
    def __init__(self, root: Path, condition: str, truth_path: Path) -> None:
        self.ready_path = root / "lifecycle.ready"
        self.stderr_path = root / "fixture_stderr.log"
        self.stderr_stream = self.stderr_path.open("w", encoding="utf-8")
        self.process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "benchmark.complex_visual.fixture_service",
                "--root",
                str(root),
                "--condition",
                condition,
                "--ready",
                str(self.ready_path),
                "--truth",
                str(truth_path),
            ],
            stdout=subprocess.DEVNULL,
            stderr=self.stderr_stream,
            env=_python_environment(),
        )

    def wait_ready(self, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                self.stderr_stream.flush()
                raise RuntimeError(self.stderr_path.read_text(encoding="utf-8"))
            if self.ready_path.is_file() and self.ready_path.read_text() == "READY\n":
                return
            time.sleep(0.03)
        raise TimeoutError("headed fixture did not become ready")

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            self.process.wait(timeout=5)
        self.stderr_stream.close()


def _threshold(image: Image.Image) -> int:
    values = np.asarray(image.convert("L"), dtype=np.uint8)
    minimum, maximum = int(values.min()), int(values.max())
    if minimum == maximum:
        raise RuntimeError("retained target crop has no visual contrast")
    return (minimum + maximum) // 2


def _window_region(name: str) -> list[int]:
    x, y = WINDOWS[name]
    return [x, y, x + WINDOW_SIZE[0], y + WINDOW_SIZE[1]]


def _marker_region(window: str, x: int, y: int, scale: float, unit: int) -> list[int]:
    wx, wy = WINDOWS[window]
    x, y = round(x * scale), round(y * scale)
    return [wx + x - 2, wy + y - 2, wx + x + 7 * unit + 2, wy + y + 7 * unit + 2]


def _control_geometry(
    window: str, x: int, y: int, scale: float, unit: int
) -> tuple[list[int], tuple[int, int]]:
    wx, wy = WINDOWS[window]
    x, y = round(x * scale), round(y * scale)
    width, height = round(150 * scale), round(42 * scale)
    region = [
        wx + x + 8,
        wy + y + 10,
        wx + x + 10 + 7 * unit + 2,
        wy + y + 12 + 7 * unit + 2,
    ]
    return region, (wx + x + width // 2, wy + y + height // 2)


def _capture_source(
    x11: X11Session, evidence_dir: Path, index: int, label: str
) -> tuple[Image.Image, Path]:
    path = evidence_dir / "source_frames" / f"{index:03d}_{label}.png"
    return x11.capture(path), path


def _capture_source_when_visible(
    x11: X11Session,
    evidence_dir: Path,
    index: int,
    label: str,
    region: list[int],
    timeout: float = 2.0,
) -> tuple[Image.Image, Path]:
    """Retain the first presented frame with contrast at the observed region."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        frame, path = _capture_source(x11, evidence_dir, index, label)
        values = np.asarray(frame.convert("L").crop(tuple(region)))
        if int(values.min()) != int(values.max()):
            return frame, path
        time.sleep(0.04)
    raise TimeoutError(f"demonstration target {label!r} did not become visible")


def _retain_target(
    manifest: dict,
    trace: list[dict],
    evidence_dir: Path,
    evidence_id: str,
    variant: str,
    frame: Image.Image,
    frame_path: Path,
    region: list[int],
    window: str,
    **metadata: object,
) -> None:
    crop = frame.crop(tuple(region))
    crop_path = evidence_dir / "templates" / f"{evidence_id}_{variant}.png"
    crop_path.parent.mkdir(parents=True, exist_ok=True)
    crop.save(crop_path, format="PNG")
    item = {
        "path": crop_path.relative_to(evidence_dir).as_posix(),
        "crop_sha256": hashlib.sha256(crop_path.read_bytes()).hexdigest(),
        "source_frame": frame_path.relative_to(evidence_dir).as_posix(),
        "source_frame_sha256": hashlib.sha256(frame_path.read_bytes()).hexdigest(),
        "source_region": region,
        "luminance_threshold": _threshold(crop),
        "variant": variant,
        **metadata,
    }
    entry = manifest["templates"].setdefault(
        evidence_id, {"search_region": _window_region(window), "variants": []}
    )
    entry["variants"].append(item)
    trace.append(
        {
            "operation": "observe_target",
            "evidence_id": evidence_id,
            "variant": variant,
            "source_frame": item["source_frame"],
            "source_frame_sha256": item["source_frame_sha256"],
            "source_region": region,
        }
    )


def _record_variant(
    evidence_dir: Path,
    manifest: dict,
    trace: list[dict],
    variant: str,
    condition: str,
    frame_index: int,
) -> int:
    scale, unit = (1.08, 3) if condition == "display_drift" else (1.0, 2)
    author_root = evidence_dir / "authoring" / variant
    oracle = author_root / "oracle"
    fixture_root = author_root / "fixture"
    oracle.mkdir(parents=True)
    fixture_root.mkdir()
    truth_path = oracle / "truth.json"
    truth = make_truth("urgent", f"author-{variant}", 1)
    truth_path.write_text(json.dumps(truth, sort_keys=True) + "\n", encoding="utf-8")
    fixture = FixtureProcess(fixture_root, condition, truth_path)
    x11 = X11Session()
    try:
        fixture.wait_ready()
        frame_index += 1
        initial, initial_path = _capture_source(
            x11, evidence_dir, frame_index, f"{variant}_initial"
        )
        marker_targets = {
            "inbox_identity": ("inbox", 24, 94),
            "task_card_anchor": ("inbox", 72, 94),
            "worklist_identity": ("worklist", 24, 94),
            "editor_identity": ("editor", 24, 94),
        }
        for evidence_id, (window, x, y) in marker_targets.items():
            region = _marker_region(window, x, y, scale, unit)
            metadata: dict[str, object] = {}
            if evidence_id == "task_card_anchor":
                card_origin = (
                    WINDOWS["inbox"][0] + round(120 * scale),
                    WINDOWS["inbox"][1] + round(94 * scale),
                )
                metadata = {
                    "card_columns": CARD_COLUMNS,
                    "card_cell_unit": unit,
                    "card_sample_offset": (unit - 1) // 2,
                    "card_origin_offset": [
                        card_origin[0] - region[0],
                        card_origin[1] - region[1],
                    ],
                }
            _retain_target(
                manifest,
                trace,
                evidence_dir,
                evidence_id,
                variant,
                initial,
                initial_path,
                region,
                window,
                **metadata,
            )
        control_targets = {
            "attachment": ("inbox", 35, 240),
            "row_task": ("worklist", 35, 145),
            "field": ("editor", 30, 135),
            "save": ("editor", 35, 240),
        }
        for evidence_id, (window, x, y) in control_targets.items():
            region, _ = _control_geometry(window, x, y, scale, unit)
            _retain_target(
                manifest,
                trace,
                evidence_dir,
                evidence_id,
                variant,
                initial,
                initial_path,
                region,
                window,
            )
        for index in range(len(truth["task_card"]["attachments"])):
            _, point = _control_geometry("inbox", 35, 240 + index * 58, scale, unit)
            x11.click(*point)
            trace.append(
                {"operation": "click", "evidence_id": "attachment", "x": point[0], "y": point[1]}
            )
        for index in range(len(truth["task_card"]["worklist_rows"])):
            _, point = _control_geometry("worklist", 35, 145 + index * 65, scale, unit)
            x11.click(*point)
            trace.append(
                {"operation": "click", "evidence_id": "row_task", "x": point[0], "y": point[1]}
            )
        frame_index += 1
        branch, branch_path = _capture_source(x11, evidence_dir, frame_index, f"{variant}_branch")
        for evidence_id, x in (("urgent", 35), ("normal", 200)):
            region, point = _control_geometry("worklist", x, 370, scale, unit)
            _retain_target(
                manifest,
                trace,
                evidence_dir,
                evidence_id,
                variant,
                branch,
                branch_path,
                region,
                "worklist",
            )
            if evidence_id == "urgent":
                x11.click(*point)
                trace.append(
                    {"operation": "click", "evidence_id": evidence_id, "x": point[0], "y": point[1]}
                )
        _, field_point = _control_geometry("editor", 30, 135, scale, unit)
        x11.click(*field_point)
        trace.append(
            {"operation": "click", "evidence_id": "field", "x": field_point[0], "y": field_point[1]}
        )
        text = truth["task_card"]["expected_document_text"].removesuffix("\n")
        x11.type_text(text)
        trace.append({"operation": "type", "character_count": len(text)})
        frame_index += 1
        focused, focused_path = _capture_source(
            x11, evidence_dir, frame_index, f"{variant}_focused"
        )
        focus_region = _marker_region("editor", 72, 94, scale, unit)
        _retain_target(
            manifest,
            trace,
            evidence_dir,
            "focus",
            variant,
            focused,
            focused_path,
            focus_region,
            "editor",
        )
        _, save_point = _control_geometry("editor", 35, 240, scale, unit)
        x11.click(*save_point)
        trace.append(
            {"operation": "click", "evidence_id": "save", "x": save_point[0], "y": save_point[1]}
        )
        frame_index += 1
        commit_region, commit_point = _control_geometry("editor", 35, 330, scale, unit)
        commit_frame, commit_path = _capture_source_when_visible(
            x11, evidence_dir, frame_index, f"{variant}_commit", commit_region
        )
        _retain_target(
            manifest,
            trace,
            evidence_dir,
            "commit",
            variant,
            commit_frame,
            commit_path,
            commit_region,
            "editor",
        )
        x11.click(*commit_point)
        trace.append(
            {
                "operation": "click",
                "evidence_id": "commit",
                "x": commit_point[0],
                "y": commit_point[1],
            }
        )
        frame_index += 1
        receipt_region = _marker_region("editor", 200, 345, scale, unit)
        receipt, receipt_path = _capture_source_when_visible(
            x11, evidence_dir, frame_index, f"{variant}_receipt", receipt_region
        )
        _retain_target(
            manifest,
            trace,
            evidence_dir,
            "receipt",
            variant,
            receipt,
            receipt_path,
            receipt_region,
            "editor",
        )
    finally:
        fixture.close()
    return frame_index


def record_demonstration(evidence_dir: Path) -> dict:
    """Retain real frames, clicks, typing, and observation geometry."""
    evidence_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema_version": "openadapt.visual-evidence.v2",
        "templates": {},
        "matching": {"method": "exact_retained_binary_crop"},
    }
    trace: list[dict] = []
    frame_index = _record_variant(evidence_dir, manifest, trace, "native", "healthy", 0)
    _record_variant(
        evidence_dir,
        manifest,
        trace,
        "display_drift",
        "display_drift",
        frame_index,
    )
    (evidence_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (evidence_dir / "event_trace.json").write_text(
        json.dumps(trace, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest


class EvidenceResolver:
    """Resolve targets only from retained bitmap crops and source geometry."""

    def __init__(self, evidence_dir: Path) -> None:
        self.evidence_dir = evidence_dir
        self.manifest = json.loads((evidence_dir / "manifest.json").read_text(encoding="utf-8"))
        self.targets: dict[str, list[tuple[dict, np.ndarray]]] = {}
        for evidence_id, entry in self.manifest["templates"].items():
            variants = []
            for item in entry["variants"]:
                crop_path = evidence_dir / item["path"]
                source_path = evidence_dir / item["source_frame"]
                if hashlib.sha256(crop_path.read_bytes()).hexdigest() != item["crop_sha256"]:
                    raise ValueError(f"retained crop hash is invalid for {evidence_id}")
                if (
                    hashlib.sha256(source_path.read_bytes()).hexdigest()
                    != item["source_frame_sha256"]
                ):
                    raise ValueError(f"retained source-frame hash is invalid for {evidence_id}")
                crop_rgb = Image.open(crop_path).convert("RGB")
                source_crop = (
                    Image.open(source_path).convert("RGB").crop(tuple(item["source_region"]))
                )
                if not np.array_equal(np.asarray(crop_rgb), np.asarray(source_crop)):
                    raise ValueError(
                        f"retained crop is not bound to its source frame for {evidence_id}"
                    )
                crop = crop_rgb.convert("L")
                variants.append((item, np.asarray(crop) <= item["luminance_threshold"]))
            self.targets[evidence_id] = variants

    @staticmethod
    def _find_exact(region: np.ndarray, target: np.ndarray) -> list[tuple[int, int]]:
        target_points = [(int(x), int(y)) for y, x in np.argwhere(target)]
        if not target_points:
            return []
        region_points = {(int(x), int(y)) for y, x in np.argwhere(region)}
        anchors = (target_points[0], target_points[len(target_points) // 2], target_points[-1])
        candidates: set[tuple[int, int]] | None = None
        for anchor_x, anchor_y in anchors:
            origins = {(x - anchor_x, y - anchor_y) for x, y in region_points}
            candidates = origins if candidates is None else candidates & origins
        matches = []
        height, width = target.shape
        for x, y in candidates or set():
            if x < 0 or y < 0 or x + width > region.shape[1] or y + height > region.shape[0]:
                continue
            if np.array_equal(region[y : y + height, x : x + width], target):
                matches.append((x, y))
        return matches

    def resolve_details(self, image: Image.Image, evidence_id: str) -> list[dict]:
        entry = self.manifest["templates"][evidence_id]
        left, top, right, bottom = entry["search_region"]
        luminance = np.asarray(image.convert("L"))
        matches: list[dict] = []
        for item, target in self.targets[evidence_id]:
            screen = luminance <= item["luminance_threshold"]
            region = screen[top:bottom, left:right]
            for x, y in self._find_exact(region, target):
                height, width = target.shape
                matches.append(
                    {
                        "box": (left + x, top + y, left + x + width, top + y + height),
                        "variant": item,
                    }
                )
        unique = {(match["box"], match["variant"]["variant"]): match for match in matches}
        return [unique[key] for key in sorted(unique)]

    def resolve(self, image: Image.Image, evidence_id: str) -> list[tuple[int, int, int, int]]:
        return sorted({match["box"] for match in self.resolve_details(image, evidence_id)})

    def read_task_card(self, image: Image.Image) -> dict:
        luminance = np.asarray(image.convert("L"))
        failures: list[str] = []
        for match in self.resolve_details(image, "task_card_anchor"):
            item = match["variant"]
            unit = item["card_cell_unit"]
            sample_offset = item["card_sample_offset"]
            columns = item["card_columns"]
            origin_x = match["box"][0] + item["card_origin_offset"][0]
            origin_y = match["box"][1] + item["card_origin_offset"][1]
            search_bottom = self.manifest["templates"]["task_card_anchor"]["search_region"][3]
            rows = (search_bottom - origin_y) // unit
            bits = []
            for index in range(rows * columns):
                row, column = divmod(index, columns)
                x = origin_x + column * unit + sample_offset
                y = origin_y + row * unit + sample_offset
                if y >= luminance.shape[0] or x >= luminance.shape[1]:
                    break
                bits.append(int(luminance[y, x] < 128))
            try:
                task = decode_task_card(bits)
                _validate_visible_task(task)
                return task
            except (KeyError, TypeError, ValueError) as exc:
                failures.append(str(exc))
        raise ValueError(f"no valid hash-checked visual task card: {failures}")


def _validate_visible_task(task: dict) -> None:
    required_strings = (
        "schema_version",
        "target_record_id",
        "priority",
        "route",
        "expected_action_id",
        "expected_document_text",
    )
    if any(not isinstance(task.get(key), str) or not task[key] for key in required_strings):
        raise ValueError("visual task card has an invalid required string")
    if task["priority"] not in {"urgent", "normal"} or task["route"] != task["priority"]:
        raise ValueError("visual task card has an invalid route branch")
    for key in ("attachments", "worklist_rows"):
        if not isinstance(task.get(key), list) or not task[key]:
            raise ValueError(f"visual task card has an invalid {key} loop")
        if any(not isinstance(value, str) or not value for value in task[key]):
            raise ValueError(f"visual task card has an invalid {key} item")


class PixelActor:
    def __init__(self, root: Path, evidence_dir: Path) -> None:
        self.root = root
        self.frames = root / "frames"
        self.frames.mkdir()
        self.x11 = X11Session()
        self.resolver = EvidenceResolver(evidence_dir)
        self.events: list[dict] = []
        self.capture_index = 0
        self.action_attempts = 0
        self.reconnects = 0

    def capture(self, label: str) -> tuple[Image.Image, str]:
        self.capture_index += 1
        path = self.frames / f"{self.capture_index:03d}_{label}.png"
        image = self.x11.capture(path)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        self.events.append(
            {
                "operation": "capture",
                "label": label,
                "path": path.relative_to(self.root).as_posix(),
                "frame_sha256": digest,
            }
        )
        return image, digest

    @staticmethod
    def _center(box: tuple[int, int, int, int]) -> tuple[int, int]:
        return ((box[0] + box[2]) // 2, (box[1] + box[3]) // 2)

    def _click_match(self, evidence_id: str, box: tuple[int, int, int, int]) -> tuple[bool, str]:
        x, y = self._center(box)
        self.x11.move(x, y)
        self.action_attempts += 1
        self.events.append({"operation": "move", "evidence_id": evidence_id, "x": x, "y": y})
        fresh, _ = self.capture(f"fresh_{evidence_id}")
        matches = self.resolver.resolve(fresh, evidence_id)
        if not matches:
            return False, "fresh_visual_target_missing"
        nearest = min(
            matches,
            key=lambda match: abs(self._center(match)[0] - x) + abs(self._center(match)[1] - y),
        )
        x, y = self._center(nearest)
        self.x11.click(x, y)
        self.action_attempts += 1
        self.events.append({"operation": "click", "evidence_id": evidence_id, "x": x, "y": y})
        return True, ""

    def loop(self, evidence_id: str, expected_count: int) -> tuple[bool, str]:
        processed = 0
        absent_since: float | None = None
        while processed < expected_count:
            frame, _ = self.capture(f"loop_{evidence_id}_{processed}")
            matches = self.resolver.resolve(frame, evidence_id)
            if not matches:
                absent_since = absent_since or time.monotonic()
                if time.monotonic() - absent_since > 1.5:
                    return False, "loop_target_missing"
                time.sleep(0.08)
                continue
            if absent_since is not None:
                self.reconnects += 1
                self.events.append({"operation": "reconnect", "evidence_id": evidence_id})
            absent_since = None
            ok, reason = self._click_match(evidence_id, matches[0])
            if not ok:
                return False, reason
            processed += 1
        final, _ = self.capture(f"loop_{evidence_id}_complete")
        if self.resolver.resolve(final, evidence_id):
            return False, "loop_did_not_reach_zero"
        return True, ""

    def click_unique(self, evidence_id: str) -> tuple[bool, str]:
        frame, _ = self.capture(f"resolve_{evidence_id}")
        matches = self.resolver.resolve(frame, evidence_id)
        if len(matches) != 1:
            return False, "visual_target_missing_or_ambiguous"
        return self._click_match(evidence_id, matches[0])

    def commit(self) -> tuple[str, str, bool]:
        resolved, _ = self.capture("commit_resolved")
        commits = self.resolver.resolve(resolved, "commit")
        if len(commits) != 1:
            return "halted", "commit_missing_or_ambiguous", False
        x, y = self._center(commits[0])
        self.x11.move(x, y)
        self.action_attempts += 1
        self.events.append({"operation": "move", "evidence_id": "commit", "x": x, "y": y})
        time.sleep(0.12)
        fresh, fresh_digest = self.capture("commit_fresh")
        if len(self.resolver.resolve(fresh, "editor_identity")) != 1:
            return "halted", "fresh_identity_mismatch", False
        if len(self.resolver.resolve(fresh, "focus")) != 1:
            return "halted", "fresh_focus_mismatch", False
        commits = self.resolver.resolve(fresh, "commit")
        if len(commits) != 1:
            return "halted", "fresh_commit_missing_or_ambiguous", False
        time.sleep(0.12)
        guarded, guarded_digest = self.capture("commit_guard")
        if fresh_digest != guarded_digest:
            return "halted", "fresh_frame_mismatch", False
        if len(self.resolver.resolve(guarded, "commit")) != 1:
            return "halted", "guarded_commit_missing_or_ambiguous", False
        x, y = self._center(commits[0])
        self.x11.click(x, y)
        self.action_attempts += 1
        self.events.append({"operation": "click", "evidence_id": "commit", "x": x, "y": y})
        time.sleep(0.2)
        receipt, _ = self.capture("post_dispatch_receipt")
        if len(self.resolver.resolve(receipt, "receipt")) == 1:
            return "completed", "", False
        return "delivery_uncertain", "post_dispatch_receipt_missing", True

    def run(self) -> RuntimeEvidence:
        started = time.monotonic()
        status, reason, uncertain = "halted", "uninitialized", False
        try:
            initial, frame_hash = self.capture("initial")
            task = self.resolver.read_task_card(initial)
            self.events.append(
                {
                    "operation": "read_visual_task_card",
                    "frame_sha256": frame_hash,
                    "task_sha256": hashlib.sha256(canonical_json(task)).hexdigest(),
                    "priority": task["priority"],
                    "attachment_count": len(task["attachments"]),
                    "worklist_row_count": len(task["worklist_rows"]),
                }
            )
            if len(self.resolver.resolve(initial, "inbox_identity")) != 1:
                reason = "inbox_identity_mismatch"
            else:
                ok, reason = self.loop("attachment", len(task["attachments"]))
                if ok:
                    worklist, _ = self.capture("worklist_identity")
                    ok = len(self.resolver.resolve(worklist, "worklist_identity")) == 1
                    reason = "worklist_identity_mismatch" if not ok else ""
                if ok:
                    ok, reason = self.loop("row_task", len(task["worklist_rows"]))
                if ok:
                    ok, reason = self.click_unique(task["priority"])
                if ok:
                    editor, _ = self.capture("editor_identity")
                    ok = len(self.resolver.resolve(editor, "editor_identity")) == 1
                    reason = "editor_identity_mismatch" if not ok else ""
                if ok:
                    ok, reason = self.click_unique("field")
                if ok:
                    text = task["expected_document_text"].removesuffix("\n")
                    self.x11.type_text(text)
                    self.action_attempts += 1
                    self.events.append({"operation": "type", "character_count": len(text)})
                    ok, reason = self.click_unique("save")
                if ok:
                    status, reason, uncertain = self.commit()
        except (KeyError, TypeError, ValueError) as exc:
            reason = f"visual_task_card_invalid:{exc}"
        finally:
            self.save_trace()
        return RuntimeEvidence(
            status,
            reason,
            uncertain,
            self.action_attempts,
            self.reconnects,
            time.monotonic() - started,
        )

    def save_trace(self) -> None:
        (self.root / "event_trace.json").write_text(
            json.dumps(self.events, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )


def _run_observer(fixture_root: Path, oracle_root: Path, phase: str) -> None:
    subprocess.run(
        [
            sys.executable,
            "-m",
            "benchmark.complex_visual.observer",
            str(fixture_root),
            "--truth",
            str(oracle_root / "truth.json"),
            "--phase",
            phase,
            "--output",
            str(oracle_root / f"observer_{phase}.json"),
        ],
        check=True,
        env=_python_environment(),
    )


def _set_oracle_mode(oracle_root: Path, *, writable: bool) -> None:
    oracle_root.chmod(0o750 if writable else 0o550)
    for path in oracle_root.iterdir():
        path.chmod(0o640 if writable else 0o440)


def _run_authority_probe(
    identity: ActorIdentity,
    trial_root: Path,
    actor_root: Path,
    fixture_root: Path,
    oracle_root: Path,
) -> dict:
    """Fail unless the actor UID has only its declared filesystem authority."""
    completed = subprocess.run(
        _actor_command(
            identity,
            [
                _actor_python(),
                "-m",
                "benchmark.complex_visual.authority_probe",
                "--trial-root",
                str(trial_root),
                "--actor-root",
                str(actor_root),
                "--fixture-root",
                str(fixture_root),
                "--oracle-root",
                str(oracle_root),
            ],
        ),
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(completed.stdout)
    expected_checks = {
        "actor_root_writable": True,
        "trial_root_writable": False,
        "fixture_root_writable": False,
        "fixture_store_writable": False,
        "oracle_root_readable": False,
        "oracle_root_searchable": False,
        "oracle_root_writable": False,
        "truth_readable": False,
        "observer_before_readable": False,
    }
    if payload.get("process_uid") != identity.uid:
        raise RuntimeError("authority probe did not run as the configured actor UID")
    if payload.get("checks") != expected_checks:
        raise RuntimeError(f"actor authority boundary is invalid: {payload.get('checks')}")
    return {
        **payload,
        "actor_username": identity.username,
        "coordinator_uid": os.geteuid(),
        "truth_sha256": hashlib.sha256((oracle_root / "truth.json").read_bytes()).hexdigest(),
    }


def execute_pixel_trial(
    condition: str, trial: int, root: Path, evidence_dir: Path
) -> RuntimeEvidence:
    started = time.monotonic()
    identity = resolve_actor_identity()
    condition_spec = next(item for item in load_campaign()["conditions"] if item["id"] == condition)
    actor_root = root / "actor"
    fixture_root = root / "fixture"
    oracle_root = root / "oracle"
    actor_root.mkdir()
    fixture_root.mkdir()
    oracle_root.mkdir()
    actor_stderr = _prepare_actor_root(actor_root, identity)
    truth = make_truth(condition_spec["case"], condition, trial)
    truth_path = oracle_root / "truth.json"
    truth_path.write_text(json.dumps(truth, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    fixture = FixtureProcess(fixture_root, condition, truth_path)
    try:
        fixture.wait_ready()
        _run_observer(fixture_root, oracle_root, "before")
        _set_oracle_mode(oracle_root, writable=False)
        authority = _run_authority_probe(identity, root, actor_root, fixture_root, oracle_root)
        _set_oracle_mode(oracle_root, writable=True)
        (oracle_root / "actor_authority.json").write_text(
            json.dumps(authority, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        _set_oracle_mode(oracle_root, writable=False)
        root.chmod(0o555)
        with actor_stderr.open("w", encoding="utf-8") as stderr:
            completed = subprocess.run(
                _actor_command(
                    identity,
                    [
                        _actor_python(),
                        "-m",
                        "benchmark.complex_visual.run_campaign",
                        "--actor-root",
                        str(actor_root),
                        "--evidence",
                        str(evidence_dir),
                    ],
                ),
                cwd=actor_root,
                stdout=subprocess.DEVNULL,
                stderr=stderr,
                check=False,
            )
        if completed.returncode != 0:
            return RuntimeEvidence(
                "halted",
                f"actor_process_failed:{completed.returncode}",
                False,
                0,
                0,
                time.monotonic() - started,
            )
        payload = json.loads((actor_root / "result.json").read_text(encoding="utf-8"))
        return RuntimeEvidence(**payload)
    finally:
        root.chmod(0o755)
        fixture.close()
        _set_oracle_mode(oracle_root, writable=True)
        _run_observer(fixture_root, oracle_root, "after")
        _set_oracle_mode(oracle_root, writable=False)


def read_bound_snapshot(root: Path, phase: str) -> Snapshot:
    oracle_root = root / "oracle"
    payload = json.loads((oracle_root / f"observer_{phase}.json").read_text(encoding="utf-8"))
    return verify_observation(payload, oracle_root / "truth.json", phase)


def _mapping_changes(before: dict, after: dict, excluded: set[str]) -> int:
    return sum(
        before.get(key) != after.get(key) for key in set(before) | set(after) if key not in excluded
    )


def _new_actions(before: Snapshot, after: Snapshot) -> list[dict]:
    remaining = Counter(canonical_json(action) for action in before.actions)
    new = []
    for action in after.actions:
        encoded = canonical_json(action)
        if remaining[encoded]:
            remaining[encoded] -= 1
        else:
            new.append(action)
    return new


def classify(
    runtime: RuntimeEvidence,
    before: Snapshot,
    after: Snapshot,
    expected: str,
    truth: dict,
) -> Classification:
    """Classify from exact truth and hash-checked independent observations."""
    task = truth["task_card"]
    target = task["target_record_id"]
    action_id = truth["expected_action_id"]
    document_hash = hashlib.sha256(task["expected_document_text"].encode()).hexdigest()
    mail_hash = hashlib.sha256(truth["expected_mail_text"].encode()).hexdigest()
    mail_name = f"{action_id}.eml"
    expected_action = {
        "action_id": action_id,
        "record_id": target,
        "route": task["route"],
        "attachment_count": len(task["attachments"]),
        "document_sha256": document_hash,
    }
    new_actions = _new_actions(before, after)
    valid_before = (
        before.sqlite_records.get(target) == {"status": "pending", "route": ""}
        and before.csv_records.get(target) == {"status": "pending", "route": ""}
        and all(action["action_id"] != action_id for action in before.actions)
        and target not in before.documents
        and mail_name not in before.mail
    )
    record_collateral = _mapping_changes(
        before.sqlite_records, after.sqlite_records, {target}
    ) + _mapping_changes(before.csv_records, after.csv_records, {target})
    document_collateral = _mapping_changes(before.documents, after.documents, {target})
    mail_collateral = _mapping_changes(before.mail, after.mail, {mail_name})
    action_collateral = sum(action != expected_action for action in new_actions)
    collateral = record_collateral + document_collateral + mail_collateral + action_collateral
    expected_documents = {**before.documents, target: document_hash}
    expected_mail = {**before.mail, mail_name: mail_hash}
    complete = (
        valid_before
        and after.sqlite_records.get(target) == {"status": "complete", "route": task["route"]}
        and after.csv_records.get(target) == {"status": "complete", "route": task["route"]}
        and new_actions == [expected_action]
        and after.documents == expected_documents
        and after.mail == expected_mail
        and collateral == 0
        and asdict(before) != asdict(after)
    )
    any_effect = asdict(before) != asdict(after)
    target_action_count = sum(action["record_id"] == target for action in new_actions)
    duplicates = max(0, target_action_count - 1) + mail_collateral
    wrong_writes = (
        sum(action["record_id"] != target for action in new_actions)
        + record_collateral
        + document_collateral
    )
    if runtime.delivery_uncertain:
        outcome = "verified" if complete else "reconciliation_required"
    elif runtime.status == "completed" and complete:
        outcome = "verified"
    elif runtime.status == "halted" and not any_effect:
        outcome = "safe_halt"
    else:
        outcome = "incorrect_success"
    permits_verified = expected in {
        "verified",
        "verified_or_safe_halt",
        "verified_or_reconciliation_required",
    }
    permits_halt = expected in {"safe_halt", "verified_or_safe_halt"}
    return Classification(
        outcome=outcome,
        silent_incorrect_successes=int(runtime.status == "completed" and not complete),
        over_halts=int(outcome == "safe_halt" and permits_verified and not permits_halt),
        under_halts=int(expected == "safe_halt" and complete),
        wrong_entity_writes=wrong_writes,
        duplicate_effects=duplicates,
        collateral_writes=collateral,
        reconciled_uncertain_deliveries=int(runtime.delivery_uncertain and complete),
    )


def campaign_passes(metrics: Counter[str] | dict[str, int | float]) -> bool:
    failure_metrics = (
        "incorrect_successes",
        "silent_incorrect_successes",
        "over_halts",
        "under_halts",
        "wrong_entity_writes",
        "duplicate_effects",
        "collateral_writes",
        "reconciliation_required",
    )
    return all(metrics.get(key, 0) == 0 for key in failure_metrics)


def run_campaign(
    output: Path,
    execute: Callable[[str, int, Path, Path], RuntimeEvidence] = execute_pixel_trial,
) -> dict:
    campaign = load_campaign()
    output.mkdir(parents=True, exist_ok=True)
    evidence_dir = output / "retained_evidence"
    record_demonstration(evidence_dir)
    metrics: Counter[str] = Counter()
    results: list[dict] = []
    runtimes: list[float] = []
    for condition in campaign["conditions"]:
        for trial in range(1, campaign["trials_per_condition"] + 1):
            root = output / "trials" / f"{condition['id']}-{trial}"
            root.mkdir(parents=True)
            runtime = execute(condition["id"], trial, root, evidence_dir)
            before = read_bound_snapshot(root, "before")
            after = read_bound_snapshot(root, "after")
            truth_path = root / "oracle" / "truth.json"
            truth = json.loads(truth_path.read_text(encoding="utf-8"))
            result = classify(runtime, before, after, condition["expect"], truth)
            outcome_metric = {
                "verified": "verified_outcomes",
                "safe_halt": "safe_halts",
                "reconciliation_required": "reconciliation_required",
                "incorrect_success": "incorrect_successes",
            }[result.outcome]
            metrics[outcome_metric] += 1
            for key in (
                "silent_incorrect_successes",
                "over_halts",
                "under_halts",
                "wrong_entity_writes",
                "duplicate_effects",
                "collateral_writes",
                "reconciled_uncertain_deliveries",
            ):
                metrics[key] += getattr(result, key)
            metrics["model_calls"] += 0
            runtimes.append(runtime.runtime_s)
            results.append(
                {
                    "condition": condition["id"],
                    "case": condition["case"],
                    "trial": trial,
                    "runtime": asdict(runtime),
                    "classification": asdict(result),
                    "artifact_root": root.relative_to(output).as_posix(),
                }
            )
    for key in campaign["required_metrics"]:
        metrics[key] += 0
    metrics["p50_runtime_s"] = statistics.median(runtimes)
    metrics["p95_runtime_s"] = sorted(runtimes)[max(0, int(len(runtimes) * 0.95) - 1)]
    report = {
        "schema_version": campaign["schema_version"],
        "execution_boundary": campaign["execution_boundary"],
        "campaign_passed": campaign_passes(metrics),
        "metrics": dict(metrics),
        "results": results,
    }
    (output / "summary.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    parser.add_argument("--actor-root", type=Path)
    parser.add_argument("--evidence", type=Path)
    args = parser.parse_args()
    if args.actor_root:
        if args.output or not args.evidence:
            parser.error("actor mode requires --actor-root and --evidence only")
        runtime = PixelActor(args.actor_root, args.evidence).run()
        (args.actor_root / "result.json").write_text(
            json.dumps(asdict(runtime), indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        return
    if not args.output or args.evidence:
        parser.error("campaign mode requires --output only")
    report = run_campaign(args.output)
    print(json.dumps(report["metrics"], indent=2, sort_keys=True))
    raise SystemExit(0 if report["campaign_passed"] else 1)


if __name__ == "__main__":
    main()
