"""Turn tasks a person did into reference episodes.

``openadapt-flow readiness import --from DIR --out EPISODES`` reads one
folder per task the person did::

    DIR/<any name>/task.json     openadapt.readiness-task/v1 (below)
    DIR/<any name>/recording/    the browser recording, with network capture
                                 (network.jsonl and events.jsonl); the files
                                 may also sit directly in the task folder

``task.json``::

    {"schema": "openadapt.readiness-task/v1",
     "task_id": "intake-000123",             # 8-128 of A-Za-z0-9._:-
     "instruction": "Add the triage note from the referral.",
     "inputs": {"note": "..."},              # what the agent is given
     "expected_record": {"patient_id": "p1", "name": "Jane Sample"},
     "save": {"method": "POST", "path": "/api/encounter"},   # optional
     "fields": ["type", "note"],             # needed for a read-back
     "readback": {"verify": "verify.yaml", "match": {}}}     # optional

The person's saved values come from, in order:

1. ``network_capture``: the save request in the recording's network capture
   (docs/NETWORK_CAPTURE.md), parsed with the shadow hold's own parser
   (:func:`openadapt_flow.agent_guard.shadow_hold.parse_body_fields`). The
   save is the last write to the app's origin that succeeded (``save``
   narrows it to one method and path; two different writes without ``save``
   are refused as ambiguous).
2. ``readback``: the saved record read back through the verify config's
   system-of-record verifier (``verify-run``'s reader). Exactly one record
   must carry every ``expected_record`` and ``readback.match`` value, and
   ``fields`` names what is compared.
3. Otherwise the episode is refused. A reference is never made up.

The person's time is from the recording: the first recorded event to the
save request (``human_duration_s``; ``null`` without timestamps).

Each episode is written to ``EPISODES/<task_id>/``: ``reference_episode.json``
(``openadapt.reference-episode/v1``, mode 0600, it holds values),
``inputs.json`` (0600), and ``guard_task.json`` (``openadapt.guard-task/v1``,
intended fields by name only) so the guard runs unchanged.

The candidate is told which record to work on. The record is what the guard
checks (``expected_record``: the identity shown on the record the person
saved, such as a name and an id); its fields are added to the candidate's
inputs as plain fields (``record_<name>``, unless the task's inputs already
carry that key) and the instruction ends with one line naming them, so an
app that lists several records can be worked without guessing. A task can
turn this off with ``"record_in_inputs": false``. The values stay plain
input fields so a proxy that pseudonymizes text sees them like any other.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from openadapt_flow.agent_guard.requests import NotASave, origin_of
from openadapt_flow.agent_guard.shadow_hold import parse_body_fields

__all__ = [
    "GUARD_TASK_SCHEMA",
    "READINESS_TASK_SCHEMA",
    "REFERENCE_SCHEMA",
    "ImportResult",
    "ReferenceRefused",
    "build_episode",
    "import_episodes",
    "load_episodes",
    "verify_config_reader",
]

READINESS_TASK_SCHEMA = "openadapt.readiness-task/v1"
REFERENCE_SCHEMA = "openadapt.reference-episode/v1"
GUARD_TASK_SCHEMA = "openadapt.guard-task/v1"
_TASK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")
_READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

#: Reads the saved records for a read-back: (task.json's readback, the task
#: folder, the expected record) -> records, or raises.
RecordReader = Callable[[Mapping[str, Any], Path, Mapping[str, str]], list]


class ReferenceRefused(ValueError):
    """An episode that can't be a reference. ``code`` is closed."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class ImportResult:
    imported: list[str] = field(default_factory=list)
    refused: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "openadapt.readiness-import/v1",
            "imported": len(self.imported),
            "task_ids": list(self.imported),
            "refused": list(self.refused),
        }


def _write_private(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)


def _digest(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _read_task(folder: Path) -> dict[str, Any]:
    try:
        raw = json.loads((folder / "task.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ReferenceRefused("task_unreadable", f"cannot read {folder}/task.json") from exc
    if not isinstance(raw, dict) or raw.get("schema") != READINESS_TASK_SCHEMA:
        raise ReferenceRefused("task_invalid", f"task.json must be {READINESS_TASK_SCHEMA}")
    task_id = raw.get("task_id")
    if not isinstance(task_id, str) or not _TASK_ID.match(task_id):
        raise ReferenceRefused(
            "task_invalid", "task_id must be an opaque id (8-128 of A-Za-z0-9._:-)"
        )
    expected = raw.get("expected_record")
    if not isinstance(expected, dict) or not expected or not all(
        isinstance(v, str) and v.strip() for v in expected.values()
    ):
        raise ReferenceRefused(
            "task_invalid", "expected_record must name the record with non-empty text"
        )
    if not isinstance(raw.get("inputs", {}), dict):
        raise ReferenceRefused("task_invalid", "inputs must be an object")
    return raw


def _recording_dir(folder: Path) -> Optional[Path]:
    for candidate in (folder / "recording", folder):
        if (candidate / "network.jsonl").is_file():
            return candidate
    return None


def _event_times(recording: Path) -> list[float]:
    path = recording / "events.jsonl"
    if not path.is_file():
        return []
    times: list[float] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            t = json.loads(line).get("t")
        except (ValueError, AttributeError):
            continue
        if isinstance(t, (int, float)):
            times.append(float(t))
    return times


def _events_start(recording: Path) -> Optional[float]:
    times = _event_times(recording)
    return min(times) if times else None


def _recording_start(recording: Path) -> Optional[_dt.datetime]:
    try:
        meta = json.loads((recording / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    for key in ("started_at", "recorded_at", "created_at"):
        value = meta.get(key) if isinstance(meta, dict) else None
        if isinstance(value, str):
            try:
                stamp = _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                continue
            return stamp if stamp.tzinfo else stamp.replace(tzinfo=_dt.timezone.utc)
    return None


def _not_saves(raw: Mapping[str, Any]) -> list[NotASave]:
    rules = []
    for item in raw.get("not_saves") or []:
        if isinstance(item, dict) and item.get("method") and item.get("path"):
            rules.append(NotASave(str(item["method"]), str(item["path"])))
    return rules


def _save_line(
    rows: Sequence[Mapping[str, Any]],
    raw: Mapping[str, Any],
    app_origin: Optional[str],
) -> Mapping[str, Any]:
    from openadapt_flow.agent_guard.requests import HeldRequest

    save = raw.get("save") if isinstance(raw.get("save"), dict) else None
    rules = _not_saves(raw)
    writes = []
    for row in rows:
        method = str(row.get("method") or "GET").upper()
        url = str(row.get("url") or "")
        if method in _READ_METHODS:
            continue
        origin = origin_of(url)
        if app_origin and origin != app_origin:
            continue
        held = HeldRequest(request_id="", method=method, url=url)
        if any(rule.matches(held, origin) for rule in rules):
            continue
        if save is not None and (
            method != str(save.get("method", "")).upper()
            or held.path != str(save.get("path", ""))
        ):
            continue
        writes.append(row)
    if not writes:
        raise ReferenceRefused("no_save_in_capture", "the capture holds no save request")
    kinds = {
        (str(r.get("method")).upper(), HeldRequest("", "GET", str(r.get("url"))).path)
        for r in writes
    }
    if save is None and len(kinds) > 1:
        raise ReferenceRefused(
            "ambiguous_save",
            "the capture holds more than one kind of write; name the save in "
            "task.json (save: {method, path})",
        )
    line = writes[-1]
    status = line.get("status")
    if line.get("failed") or not isinstance(status, int) or not 200 <= status < 400:
        raise ReferenceRefused(
            "save_not_confirmed", "the person's save did not succeed in the capture"
        )
    return line


def _from_network(
    folder: Path, raw: Mapping[str, Any], app_origin: Optional[str], lens: Any
) -> Optional[dict[str, Any]]:
    from openadapt_flow import network_capture

    recording = _recording_dir(folder)
    if recording is None:
        return None
    rows = network_capture.load(recording)
    line = _save_line(rows, raw, app_origin)
    fields, withheld = parse_body_fields(
        method=str(line.get("method")),
        url=str(line.get("url")),
        content_type=str(line.get("request_type") or ""),
        body=line.get("body"),
        lens=lens,
        form_pairs=line.get("form"),
        body_omitted=bool(line.get("body_omitted") or line.get("request_truncated")),
    )
    if any(f.name == "<body>" for f in fields):
        raise ReferenceRefused(
            "save_unreadable", "the capture did not keep the save's body"
        )
    wanted = raw.get("fields")
    if isinstance(wanted, list) and wanted:
        keep = set(map(str, wanted)) | set(raw["expected_record"])
        fields = [f for f in fields if f.name in keep]
    started = _events_start(recording)
    saved_t = line.get("t")
    duration = (
        round(float(saved_t) - started, 3)
        if isinstance(saved_t, (int, float)) and started is not None
        else None
    )
    if duration is not None and duration < 0:
        duration = None
    t0 = _recording_start(recording)

    def stamp(t: Optional[float]) -> Optional[str]:
        if t0 is None or t is None:
            return None
        return (t0 + _dt.timedelta(seconds=float(t))).isoformat(timespec="milliseconds")

    url = str(line.get("url"))
    return {
        "human_fields": [
            {"name": f.name, "value": f.value, **({"parse": f.parse} if f.parse == "uncompared" else {})}
            for f in fields
        ],
        "human_source": "network_capture",
        "human_started_at": stamp(started),
        "human_saved_at": stamp(saved_t if isinstance(saved_t, (int, float)) else None),
        "human_duration_s": duration,
        "withheld_fields": withheld,
        "save": {
            "method": str(line.get("method")).upper(),
            "url_path": network_capture_path(url),
        },
    }


def network_capture_path(url: str) -> str:
    from urllib.parse import urlsplit

    return urlsplit(url).path or "/"


def verify_config_reader(
    readback: Mapping[str, Any], folder: Path, expected: Mapping[str, str]
) -> list:
    """The records a ``verify.yaml``'s system-of-record verifier reads
    (``verify-run``'s reader), bound to the task's record values."""

    from openadapt_flow.deployment import build_effect_verifier
    from openadapt_flow.verify_run import load_verify_config

    path = Path(str(readback.get("verify") or ""))
    if not path.is_absolute():
        path = folder / path
    cfg = load_verify_config(path)
    verifier = build_effect_verifier(cfg.verifier, dict(expected))
    if verifier is None:
        raise ReferenceRefused("readback_unavailable", "the verify config built no reader")
    state = verifier.capture_pre_state()
    if not getattr(state, "reachable", False):
        raise ReferenceRefused("readback_unavailable", "the system of record can't be read")
    return list(state.records)


def _from_readback(
    folder: Path, raw: Mapping[str, Any], reader: RecordReader
) -> Optional[dict[str, Any]]:
    readback = raw.get("readback")
    if not isinstance(readback, dict):
        return None
    wanted = raw.get("fields")
    if not isinstance(wanted, list) or not wanted:
        raise ReferenceRefused(
            "readback_invalid", "a read-back needs task.json fields: [names]"
        )
    expected = dict(raw["expected_record"])
    match = {**expected, **dict(readback.get("match") or {})}
    try:
        records = reader(readback, folder, expected)
    except ReferenceRefused:
        raise
    except Exception as exc:  # noqa: BLE001 - an unreadable store is no reference
        raise ReferenceRefused(
            "readback_unavailable", f"the saved record could not be read ({type(exc).__name__})"
        ) from exc
    hits = [
        r
        for r in records
        if isinstance(r, Mapping)
        and all(k in r and str(r[k]) == str(v) for k, v in match.items())
    ]
    if len(hits) != 1:
        raise ReferenceRefused(
            "readback_ambiguous",
            f"{len(hits)} saved records match the task's record, not exactly one",
        )
    record = hits[0]
    missing = [name for name in wanted if name not in record]
    if missing:
        raise ReferenceRefused(
            "readback_invalid", "the saved record has no field " + ", ".join(missing)
        )
    # Without the save request, the person's time is first to last event.
    times = _event_times(_recording_dir(folder) or folder)
    duration = round(max(times) - min(times), 3) if len(times) > 1 else None
    return {
        "human_fields": [
            {"name": str(n), "value": None if record[n] is None else str(record[n])}
            for n in wanted
        ],
        "human_source": "readback",
        "human_started_at": None,
        "human_saved_at": None,
        "human_duration_s": duration,
        "withheld_fields": 0,
        "save": None,
    }


def build_episode(
    folder: Path,
    *,
    app_origin: Optional[str] = None,
    lens: Any = None,
    reader: RecordReader = verify_config_reader,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """(reference episode, inputs, guard task) for one task folder.

    Raises :class:`ReferenceRefused` when no source gives the saved values.
    """

    raw = _read_task(folder)
    human = _from_network(folder, raw, app_origin, lens)
    if human is None:
        human = _from_readback(folder, raw, reader)
    if human is None:
        raise ReferenceRefused(
            "no_source",
            "no network capture of the save and no read-back: the person's "
            "saved values are unknown",
        )
    expected = dict(raw["expected_record"])
    intended = [
        f["name"] for f in human["human_fields"] if f["name"] not in expected
    ]
    if not intended:
        raise ReferenceRefused("no_fields", "the save carries no field to compare")
    inputs = task_inputs(raw, expected)
    episode = {
        "schema": REFERENCE_SCHEMA,
        "task_id": raw["task_id"],
        "inputs_digest": _digest(inputs),
        "inputs_path": "inputs.json",
        "expected_record": expected,
        **human,
    }
    guard_task = {
        "schema_version": GUARD_TASK_SCHEMA,
        "task_id": raw["task_id"],
        "expected_record": expected,
        "intended_fields": {name: None for name in intended},
    }
    return episode, inputs, guard_task


RECORD_PREFIX = "record_"


def task_inputs(raw: Mapping[str, Any], expected: Mapping[str, Any]) -> dict[str, Any]:
    """The instruction and inputs the candidate gets, with the target record
    named (see the module docstring)."""

    instruction = str(raw.get("instruction") or "").strip()
    given = dict(raw.get("inputs") or {})
    if raw.get("record_in_inputs", True) is False or not expected:
        return {"instruction": instruction, "inputs": given}
    keys, added = [], []
    for name, value in expected.items():
        key = f"{RECORD_PREFIX}{name}"
        if key not in given:
            given[key] = value
            added.append(key)
        keys.append(key)
    line = "Work on the record identified by the inputs " + ", ".join(keys) + "."
    return {
        "instruction": (instruction + "\n\n" + line).strip(),
        "inputs": given,
        # Added here, not the person's task inputs (a program gets no
        # parameter for them).
        "record_keys": added,
    }


def import_episodes(
    source: Path | str,
    out: Path | str,
    *,
    app_url: Optional[str] = None,
    lens: Any = None,
    reader: RecordReader = verify_config_reader,
) -> ImportResult:
    """Import every task folder under ``source`` (see the module docstring)."""

    source, out = Path(source), Path(out)
    app_origin = origin_of(app_url) if app_url else None
    result = ImportResult()
    folders = sorted(p for p in source.iterdir() if (p / "task.json").is_file())
    seen: set[str] = set()
    for folder in folders:
        try:
            episode, inputs, guard_task = build_episode(
                folder, app_origin=app_origin, lens=lens, reader=reader
            )
        except ReferenceRefused as exc:
            result.refused.append({"folder": folder.name, "code": exc.code, "why": exc.message})
            continue
        task_id = episode["task_id"]
        if task_id in seen:
            result.refused.append(
                {"folder": folder.name, "code": "duplicate_task_id", "why": "task_id repeats"}
            )
            continue
        seen.add(task_id)
        target = out / task_id
        _write_private(target / "inputs.json", inputs)
        _write_private(target / "reference_episode.json", episode)
        _write_private(target / "guard_task.json", guard_task)
        result.imported.append(task_id)
    _write_private(out / "import.json", result.to_dict())
    return result


def load_episodes(episodes: Path | str) -> list[tuple[Path, dict[str, Any]]]:
    """Every ``reference_episode.json`` under ``episodes``, by task id."""

    found = []
    for path in sorted(Path(episodes).glob("*/reference_episode.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        if doc.get("schema") != REFERENCE_SCHEMA:
            raise ValueError(f"{path} is not a {REFERENCE_SCHEMA} file")
        found.append((path.parent, doc))
    return found
