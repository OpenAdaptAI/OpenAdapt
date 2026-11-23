"""Built-in everyday OS quickstart tasks (zero sample apps, zero logins).

Provides parameterized task definitions and read-only ``local-state`` verification
contracts (including optional revert/undo contracts) for:

1. ``night-shift``: Toggle macOS Night Shift (``macos_night_shift``) or Windows
   Night Light (``windows_night_light``).
2. ``calendar-block``: Schedule a recurring Calendar block with parameterized
   title, start/end time, duration, and recurrence (``macos_calendar_event``).

Nothing in this module mutates OS settings or Calendar data; probes are strictly
read-only and fail closed when unreadable.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Optional

import yaml  # type: ignore[import-untyped]

EVERYDAY_TASKS = ("night-shift", "calendar-block")
VALID_RECURRENCES = ("none", "daily", "weekly", "weekdays")
DEFAULT_CALENDAR_TITLE = "OpenAdapt Focus Block"
DEFAULT_CALENDAR_NAME = "Calendar"
DEFAULT_DURATION_MINUTES = 30
DEFAULT_RECURRENCE = "weekly"


class EverydayTaskError(Exception):
    """Raised when an everyday quickstart task cannot be configured on this host."""


@dataclass(frozen=True)
class EverydayTaskBundle:
    """Generated contracts and read-only baseline snapshot for an everyday task."""

    task: str
    platform: str
    out_dir: Path
    verify_path: Path
    revert_verify_path: Optional[Path]
    params_path: Path
    baseline_state: Optional[dict[str, Any]]
    params: dict[str, Any]
    revert: bool


def default_start_iso(now: Optional[datetime] = None) -> str:
    """Return the next top-of-the-hour local timestamp as an ISO-8601 string."""
    ref = now or datetime.now()
    next_hour = ref.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    return next_hour.isoformat(timespec="seconds")


def _parse_iso(value: str) -> datetime:
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        return datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise EverydayTaskError(
            f"--start must be a valid ISO-8601 timestamp (got {value!r})"
        ) from exc


def build_night_shift_contracts(
    *,
    platform: str,
    baseline_enabled: Optional[bool] = None,
    revert: bool = True,
) -> tuple[dict[str, Any], Optional[dict[str, Any]], dict[str, Any]]:
    """Build forward and optional revert ``verify.yaml`` contracts for Night Shift/Light."""
    if platform == "darwin":
        source_type = "macos_night_shift"
        source_id = "night_shift"
        label = "macOS Night Shift"
    elif platform == "win32":
        source_type = "windows_night_light"
        source_id = "night_light"
        label = "Windows Night Light"
    else:
        raise EverydayTaskError(
            f"night-shift task supports macOS (Night Shift) and Windows (Night Light); "
            f"unsupported platform {platform!r}"
        )

    target_enabled = (not baseline_enabled) if baseline_enabled is not None else True
    restore_enabled = (
        baseline_enabled if baseline_enabled is not None else (not target_enabled)
    )

    def _contract(expected_enabled: bool) -> dict[str, Any]:
        return {
            "schema_version": "openadapt.verify/v1",
            "minimum_effect_strength": "independent_system_of_record",
            "verifier": {
                "kind": "local-state",
                "local_state": [{"type": source_type, "id": source_id}],
            },
            "frame": {"target_fields": ["enabled", "value"]},
            "effects": [
                {
                    "kind": "field_equals",
                    "match": {"id": source_id},
                    "field": "value",
                    "value": "true" if expected_enabled else "false",
                }
            ],
        }

    forward = _contract(target_enabled)
    revert_contract = _contract(restore_enabled) if revert else None
    params: dict[str, Any] = {
        "task": "night-shift",
        "label": label,
        "platform": platform,
        "source_type": source_type,
        "baseline_enabled": baseline_enabled,
        "target_enabled": "true" if target_enabled else "false",
        "restore_enabled": "true" if restore_enabled else "false",
        "revert": revert,
    }
    return forward, revert_contract, params


def build_calendar_block_contracts(
    *,
    title: str = DEFAULT_CALENDAR_TITLE,
    calendar_name: str = DEFAULT_CALENDAR_NAME,
    start_iso: Optional[str] = None,
    duration_minutes: int = DEFAULT_DURATION_MINUTES,
    recurrence: str = DEFAULT_RECURRENCE,
    platform: str = "darwin",
    revert: bool = True,
) -> tuple[dict[str, Any], Optional[dict[str, Any]], dict[str, Any]]:
    """Build parameterized forward and optional revert contracts for a Calendar block."""
    if platform != "darwin":
        raise EverydayTaskError(
            f"calendar-block local-state probe currently supports macOS Calendar; "
            f"unsupported platform {platform!r}"
        )
    if not title or not title.strip():
        raise EverydayTaskError("--title must be non-empty")
    if not calendar_name or not calendar_name.strip():
        raise EverydayTaskError("--calendar must be non-empty")
    if duration_minutes < 1 or duration_minutes > 480:
        raise EverydayTaskError("--duration must be between 1 and 480 minutes")
    if recurrence not in VALID_RECURRENCES:
        raise EverydayTaskError(
            f"--recurrence must be one of {', '.join(VALID_RECURRENCES)}"
        )

    resolved_start = start_iso or default_start_iso()
    start_dt = _parse_iso(resolved_start)
    end_dt = start_dt + timedelta(minutes=duration_minutes)
    end_iso = end_dt.isoformat(timespec="seconds")

    source_spec = {
        "type": "macos_calendar_event",
        "id": "calendar_block",
        "calendar_name": calendar_name.strip(),
        "title": title.strip(),
        "start_iso": resolved_start,
    }

    def _contract(expected_exists: bool) -> dict[str, Any]:
        return {
            "schema_version": "openadapt.verify/v1",
            "minimum_effect_strength": "independent_system_of_record",
            "verifier": {
                "kind": "local-state",
                "local_state": [source_spec],
            },
            "frame": {"target_fields": ["present", "exists", "value"]},
            "effects": [
                {
                    "kind": "field_equals",
                    "match": {"id": "calendar_block"},
                    "field": "value",
                    "value": "true" if expected_exists else "false",
                }
            ],
        }

    forward = _contract(True)
    revert_contract = _contract(False) if revert else None
    params: dict[str, Any] = {
        "task": "calendar-block",
        "platform": platform,
        "calendar_name": calendar_name.strip(),
        "title": title.strip(),
        "start_iso": resolved_start,
        "end_iso": end_iso,
        "duration_minutes": duration_minutes,
        "recurrence": recurrence,
        "revert": revert,
    }
    return forward, revert_contract, params


def _probe_baseline(
    verify_doc: dict[str, Any],
    *,
    platform: str,
    verifier_factory: Optional[Callable[..., Any]] = None,
) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """Run the read-only ``local-state`` probe once without mutating OS state."""
    from openadapt_flow.runtime.effects.local_state import LocalStateVerifier
    from openadapt_flow.verify_run import VerifyConfig

    cfg = VerifyConfig.model_validate(verify_doc)
    sources = list(cfg.verifier.local_state or [])
    if verifier_factory is not None:
        verifier = verifier_factory(sources, platform=platform)
    else:
        verifier = LocalStateVerifier(sources, platform=platform)
    records = verifier.read()
    if records is None or not records:
        return None, getattr(verifier, "last_error", "unreadable")
    return records[0], None


def run_everyday_task(
    task: str,
    root: Path,
    *,
    title: str = DEFAULT_CALENDAR_TITLE,
    calendar_name: str = DEFAULT_CALENDAR_NAME,
    start_iso: Optional[str] = None,
    duration_minutes: int = DEFAULT_DURATION_MINUTES,
    recurrence: str = DEFAULT_RECURRENCE,
    revert: bool = True,
    platform: Optional[str] = None,
    say: Callable[[str], None] = print,
    verifier_factory: Optional[Callable[..., Any]] = None,
) -> EverydayTaskBundle:
    """Prepare and baseline-probe a built-in everyday OS task into ``root``."""
    if task not in EVERYDAY_TASKS:
        raise EverydayTaskError(
            f"Unknown task {task!r}. Expected one of: {', '.join(EVERYDAY_TASKS)}"
        )

    active_platform = platform or sys.platform
    baseline_state: Optional[dict[str, Any]] = None
    probe_error: Optional[str] = None

    if task == "night-shift":
        # Probe baseline first so the forward target flips the actual current state.
        probe_doc, _, _ = build_night_shift_contracts(
            platform=active_platform, baseline_enabled=None, revert=revert
        )
        baseline_state, probe_error = _probe_baseline(
            probe_doc, platform=active_platform, verifier_factory=verifier_factory
        )
        baseline_enabled = (
            bool(baseline_state["enabled"])
            if baseline_state is not None
            and isinstance(baseline_state.get("enabled"), bool)
            else None
        )
        forward, revert_doc, params = build_night_shift_contracts(
            platform=active_platform,
            baseline_enabled=baseline_enabled,
            revert=revert,
        )
    else:
        forward, revert_doc, params = build_calendar_block_contracts(
            title=title,
            calendar_name=calendar_name,
            start_iso=start_iso,
            duration_minutes=duration_minutes,
            recurrence=recurrence,
            platform=active_platform,
            revert=revert,
        )
        baseline_state, probe_error = _probe_baseline(
            forward, platform=active_platform, verifier_factory=verifier_factory
        )

    root.mkdir(parents=True, exist_ok=True)
    verify_path = root / "verify.yaml"
    verify_path.write_text(yaml.safe_dump(forward, sort_keys=False), encoding="utf-8")

    revert_verify_path: Optional[Path] = None
    if revert_doc is not None:
        revert_verify_path = root / "revert_verify.yaml"
        revert_verify_path.write_text(
            yaml.safe_dump(revert_doc, sort_keys=False), encoding="utf-8"
        )

    params_path = root / "task.json"
    params_path.write_text(
        json.dumps(params, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    say(f"Everyday task: {task} ({active_platform})")
    if baseline_state is not None:
        say(
            f"  Baseline probe (read-only): {baseline_state['type']} -> "
            f"value={baseline_state.get('value')}"
        )
    else:
        say(
            f"  Baseline probe (read-only): INDETERMINATE ({probe_error or 'unavailable'})"
        )
    say(f"  Verify contract: {verify_path}")
    if revert_verify_path is not None:
        say(f"  Revert contract: {revert_verify_path} (leaves machine clean)")
    say(f"  Task parameters: {params_path}")

    return EverydayTaskBundle(
        task=task,
        platform=active_platform,
        out_dir=root,
        verify_path=verify_path,
        revert_verify_path=revert_verify_path,
        params_path=params_path,
        baseline_state=baseline_state,
        params=params,
        revert=revert,
    )


__all__ = [
    "DEFAULT_CALENDAR_NAME",
    "DEFAULT_CALENDAR_TITLE",
    "DEFAULT_DURATION_MINUTES",
    "DEFAULT_RECURRENCE",
    "EVERYDAY_TASKS",
    "EverydayTaskBundle",
    "EverydayTaskError",
    "VALID_RECURRENCES",
    "build_calendar_block_contracts",
    "build_night_shift_contracts",
    "default_start_iso",
    "run_everyday_task",
]
