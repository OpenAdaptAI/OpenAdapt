"""Built-in everyday OS quickstart tasks (zero sample apps, zero logins).

Generates parameterized task definitions and read-only ``local-state``
verification contract files (``verify.yaml`` and, when baseline state is known,
``revert_verify.yaml``) for:

1. ``night-shift``: Toggle macOS Night Shift (``macos_night_shift``) or Windows
   Night Light (``windows_night_light``).
2. ``calendar-block``: Schedule a Calendar block with parameterized title,
   start/end time, duration, and recurrence (``macos_calendar_event`` via
   macOS EventKit).

This module only writes contract/parameter files (it does not mutate OS
settings or Calendar data, and does not run the change or revert itself).
For ``calendar-block``, baseline probing is opt-in (``probe_baseline=True``)
so running quickstart never queries EventKit without explicit user intent.
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
    # Never invent a revert baseline when the prior state is unreadable.
    restore_enabled: Optional[bool] = (
        baseline_enabled if baseline_enabled is not None else None
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
    revert_contract = (
        _contract(restore_enabled) if (revert and restore_enabled is not None) else None
    )
    params: dict[str, Any] = {
        "task": "night-shift",
        "label": label,
        "platform": platform,
        "source_type": source_type,
        "baseline_enabled": baseline_enabled,
        "target_enabled": "true" if target_enabled else "false",
        "restore_enabled": (
            ("true" if restore_enabled else "false")
            if restore_enabled is not None
            else None
        ),
        "revert": revert,
    }
    if revert and restore_enabled is None:
        params["revert_withheld_reason"] = "baseline_indeterminate"
    return forward, revert_contract, params


def build_calendar_block_contracts(
    *,
    title: str = DEFAULT_CALENDAR_TITLE,
    calendar_name: str = DEFAULT_CALENDAR_NAME,
    start_iso: Optional[str] = None,
    duration_minutes: int = DEFAULT_DURATION_MINUTES,
    recurrence: str = DEFAULT_RECURRENCE,
    event_uid: Optional[str] = None,
    platform: str = "darwin",
    revert: bool = True,
) -> tuple[dict[str, Any], Optional[dict[str, Any]], dict[str, Any]]:
    """Build parameterized forward and optional revert contracts for a Calendar block."""
    if platform != "darwin":
        raise EverydayTaskError(
            f"calendar-block local-state probe currently supports macOS EventKit; "
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

    source_spec: dict[str, Any] = {
        "type": "macos_calendar_event",
        "id": "calendar_block",
        "calendar_name": calendar_name.strip(),
        "title": title.strip(),
        "start_iso": resolved_start,
        "end_iso": end_iso,
        "recurrence": recurrence,
    }
    if event_uid is not None and event_uid.strip():
        source_spec["event_uid"] = event_uid.strip()

    def _contract(expected_exists: bool) -> dict[str, Any]:
        return {
            "schema_version": "openadapt.verify/v1",
            "minimum_effect_strength": "independent_system_of_record",
            "verifier": {
                "kind": "local-state",
                "local_state": [source_spec],
            },
            "frame": {"target_fields": ["present", "exists", "event_uid", "value"]},
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
        "event_uid": event_uid.strip() if event_uid and event_uid.strip() else None,
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
    try:
        from openadapt_flow.runtime.effects.local_state import LocalStateVerifier
        from openadapt_flow.verify_run import VerifyConfig
    except ImportError as exc:
        return (
            None,
            f"local_state verifier unavailable in installed openadapt-flow: {exc}",
        )

    try:
        cfg = VerifyConfig.model_validate(verify_doc)
    except Exception as exc:
        return (
            None,
            f"local_state probe unsupported by installed openadapt-flow: {exc}",
        )
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
    event_uid: Optional[str] = None,
    revert: bool = True,
    probe_baseline: bool = False,
    platform: Optional[str] = None,
    say: Callable[[str], None] = print,
    verifier_factory: Optional[Callable[..., Any]] = None,
) -> EverydayTaskBundle:
    """Write parameterized verify/revert contracts for a built-in everyday OS task into ``root``."""
    if task not in EVERYDAY_TASKS:
        raise EverydayTaskError(
            f"Unknown task {task!r}. Expected one of: {', '.join(EVERYDAY_TASKS)}"
        )

    active_platform = platform or sys.platform
    baseline_state: Optional[dict[str, Any]] = None
    probe_error: Optional[str] = None
    probe_skipped = False

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
        bound_uid = event_uid.strip() if event_uid and event_uid.strip() else None
        forward, revert_doc, params = build_calendar_block_contracts(
            title=title,
            calendar_name=calendar_name,
            start_iso=start_iso,
            duration_minutes=duration_minutes,
            recurrence=recurrence,
            event_uid=bound_uid,
            platform=active_platform,
            revert=revert,
        )
        if probe_baseline:
            baseline_state, probe_error = _probe_baseline(
                forward, platform=active_platform, verifier_factory=verifier_factory
            )
            if baseline_state is not None and baseline_state.get("exists") is True:
                raise EverydayTaskError(
                    "Refusing to generate calendar-block contracts: a matching event "
                    f"({title!r} at {params['start_iso']} in {calendar_name!r}) already "
                    "exists in the baseline. Choose a different --title or --start so "
                    "the revert check cannot target a pre-existing user event."
                )
        else:
            probe_skipped = True

        # Fail closed on calendar revert: only write revert_verify.yaml when an
        # explicit event_uid is bound OR the baseline was explicitly probed and
        # confirmed NOT_FOUND, so we never tell a user to delete an unverified
        # pre-existing event.
        baseline_confirmed_absent = (
            baseline_state is not None and baseline_state.get("exists") is False
        )
        if (
            revert
            and revert_doc is not None
            and not (bound_uid or baseline_confirmed_absent)
        ):
            revert_doc = None
            params["revert_withheld_reason"] = (
                "baseline_not_probed" if probe_skipped else "baseline_indeterminate"
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
    if probe_skipped:
        say(
            "  Baseline probe: skipped by default for calendar-block "
            "(pass --probe-baseline to read macOS EventKit; requires Calendars permission)"
        )
    elif baseline_state is not None:
        say(
            f"  Baseline probe (read-only): {baseline_state['type']} -> "
            f"value={baseline_state.get('value')}"
        )
    else:
        say(
            f"  Baseline probe (read-only): INDETERMINATE ({probe_error or 'unavailable'})"
        )
    say(f"  Verify contract written: {verify_path} (run via openadapt-flow verify-run)")
    if revert_verify_path is not None:
        say(
            f"  Revert contract written: {revert_verify_path} "
            "(run via openadapt-flow verify-run after undoing the change)"
        )
    elif revert and task == "night-shift":
        say(
            "  Revert contract withheld: baseline was INDETERMINATE "
            "(will not invent a prior Night Shift state)"
        )
    elif revert and task == "calendar-block":
        reason = params.get("revert_withheld_reason", "baseline_not_probed")
        say(
            f"  Revert contract withheld ({reason}): pass --probe-baseline "
            "(confirmed NOT_FOUND) or --event-uid to generate revert_verify.yaml"
        )
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
