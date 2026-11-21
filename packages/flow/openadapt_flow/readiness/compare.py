"""Compare an agent's proposed write with what the person saved.

Per field of the human reference:

``matched``
    The proposed value equals the person's, after normalisation.
``mismatched``
    Both have the field and the values differ (the case keeps both).
``missing``
    The person saved the field and the agent's write does not carry it.
``extra``
    The agent's write carries a field the person did not save and the task
    does not declare (``undeclared_field`` risk).
``wrong_record``
    A record field (``expected_record``) in the write names another record.
``uncompared``
    A value Flow could not read on one side (an unreadable body, a repeated
    form name, a file). Never counted as matched.

Normalisation, general for every app: null and empty text are the same;
leading, trailing and repeated whitespace is ignored; two numbers are equal
when their decimal values are; an ISO date equals the same date with a
time of midnight. A lens's field codec (``openadapt.entity-lens/v1``) is
applied on top through :func:`openadapt_flow.tx.judge.apply_field_codec`.
Case is not folded: a careful person would send ``jane`` back.

A case is ``MATCHED`` only when the identity check passed, every human field
matched, the write has no field outside the declared ones, and the case is
comparable. Contamination: when the screen that showed the task's record,
read before any write was held, already shows the person's saved values for
at least half of the compared fields, the case is ``NOT_COMPARABLE``.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Optional, Sequence

from openadapt_flow.runtime.identity import normalize_structured

__all__ = [
    "CASE_SCHEMA",
    "CASE_STATUSES",
    "FIELD_KINDS",
    "compare_case",
    "compare_runs",
    "contamination",
    "values_equal",
]

CASE_SCHEMA = "openadapt.readiness-case/v1"
CASE_STATUSES = (
    "MATCHED",
    "MISMATCHED",
    "WRONG_RECORD",
    "NOT_COMPARABLE",
    "NOT_CHECKED",
    "FAILED_SCOPE",
)
FIELD_KINDS = (
    "matched",
    "mismatched",
    "missing",
    "extra",
    "wrong_record",
    "uncompared",
)
_NUMBER = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$")
_ISO_DATE = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:[T ]00:00(?::00(?:\.0+)?)?(?:Z|[+-]00:?00)?)?$")
_UNCOMPARED_BODY = "<body>"


def _text(value: Any) -> str:
    if value is None:
        return ""
    return " ".join(str(value).split())


def values_equal(human: Any, proposed: Any, codec: Any = None) -> bool:
    """Whether two field values are the same write (see the module docstring)."""

    left, right = _text(human), _text(proposed)
    if left == right:
        return True
    if _NUMBER.match(left) and _NUMBER.match(right):
        try:
            return Decimal(left) == Decimal(right)
        except InvalidOperation:
            return False
    date_l, date_r = _ISO_DATE.match(left), _ISO_DATE.match(right)
    if date_l and date_r and date_l.group(1) == date_r.group(1):
        return True
    if codec is not None:
        from openadapt_flow.tx.judge import apply_field_codec

        try:
            return bool(apply_field_codec(codec, left, right)[0])
        except Exception:  # noqa: BLE001 - a codec that can't apply is no match
            return False
    return False


def contamination(
    before_screen: Optional[str], human_fields: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """How many of the person's values the screen showed before the agent
    typed. ``not_comparable`` when at least half did, or when no screen was
    read before the write was held."""

    values = [
        normalize_structured(str(f.get("value")))
        for f in human_fields
        if f.get("value") not in (None, "") and normalize_structured(str(f["value"]))
    ]
    if before_screen is None:
        return {
            "compared": len(values),
            "shown": None,
            "not_comparable": True,
            "reason": "no_screen_before_write",
        }
    text = normalize_structured(before_screen)
    shown = sum(
        1 for v in values if re.search(rf"(?<!\w){re.escape(v)}(?!\w)", text)
    )
    return {
        "compared": len(values),
        "shown": shown,
        "not_comparable": bool(values) and shown * 2 >= len(values),
        "reason": "values_already_on_screen" if values and shown * 2 >= len(values) else None,
    }


def compare_case(
    reference: Mapping[str, Any],
    run: Optional[Mapping[str, Any]],
    proposed: Optional[Mapping[str, Any]],
    *,
    before_screen: Optional[str] = None,
    codecs: Optional[Mapping[str, Any]] = None,
    ignore_fields: Sequence[str] = (),
) -> dict[str, Any]:
    """One case: the reference, the run's outcome and its proposed write.

    The result keeps values (it is local, like the HTML); the scorecard JSON
    copies only names and kinds out of it.
    """

    codecs = dict(codecs or {})
    task_id = str(reference["task_id"])
    expected: dict[str, Any] = dict(reference.get("expected_record") or {})
    human = [
        f for f in reference.get("human_fields") or [] if f["name"] not in expected
    ]
    case: dict[str, Any] = {
        "schema": CASE_SCHEMA,
        "task_id": task_id,
        "status": "NOT_CHECKED",
        "reason": None,
        "identity": None,
        "fields": [],
        "contamination": None,
        "human_time_s": reference.get("human_duration_s"),
        "agent_time_s": (run or {}).get("agent_wall_s"),
        "model_cost_usd": (run or {}).get("model_cost_usd"),
        "landed_writes": int((run or {}).get("landed_writes") or 0),
        "double_save": bool((run or {}).get("double_save")),
        "out_of_scope_requests": int((run or {}).get("other_origin_refused") or 0)
        + int((run or {}).get("landed_writes") or 0),
        "undeclared_fields": 0,
    }
    if run is None:
        case["reason"] = "not_run"
        return case
    if case["landed_writes"]:
        case["status"] = "FAILED_SCOPE"
        case["reason"] = "a write reached the app"
        return case
    if proposed is None:
        case["reason"] = str(run.get("status") or "no_write_held").lower()
        return case

    identity = dict(proposed.get("identity") or {})
    case["identity"] = identity
    got = {f["name"]: f for f in proposed.get("fields") or []}
    body_unreadable = _UNCOMPARED_BODY in got and got[_UNCOMPARED_BODY].get(
        "parse"
    ) == "uncompared"
    rows: list[dict[str, Any]] = []
    for name, value in expected.items():
        if name in got and got[name].get("parse") != "uncompared":
            if not values_equal(value, got[name].get("value"), codecs.get(name)):
                rows.append(
                    {
                        "name": name,
                        "kind": "wrong_record",
                        "human": value,
                        "proposed": got[name].get("value"),
                    }
                )
    for field in human:
        name = field["name"]
        mine = got.get(name)
        row = {"name": name, "human": field.get("value"), "proposed": None}
        if field.get("parse") == "uncompared":
            row["kind"] = "uncompared"
        elif mine is None:
            row["kind"] = "uncompared" if body_unreadable else "missing"
        elif mine.get("parse") == "uncompared":
            row["kind"] = "uncompared"
        else:
            row["proposed"] = mine.get("value")
            row["kind"] = (
                "matched"
                if values_equal(field.get("value"), mine.get("value"), codecs.get(name))
                else "mismatched"
            )
        rows.append(row)
    declared = {f["name"] for f in human} | set(expected) | set(ignore_fields)
    for name, mine in got.items():
        if name in declared or name == _UNCOMPARED_BODY:
            continue
        rows.append(
            {
                "name": name,
                "kind": "uncompared" if mine.get("parse") == "uncompared" else "extra",
                "human": None,
                "proposed": mine.get("value"),
            }
        )
    case["fields"] = rows
    kinds = [r["kind"] for r in rows]
    case["undeclared_fields"] = kinds.count("extra")
    if not identity.get("ok") or "wrong_record" in kinds:
        case["status"] = "WRONG_RECORD"
        case["reason"] = identity.get("code") if not identity.get("ok") else (
            "a record field names another record"
        )
        return case
    check = contamination(before_screen, human)
    case["contamination"] = check
    if check["not_comparable"]:
        case["status"] = "NOT_COMPARABLE"
        case["reason"] = check["reason"]
        return case
    if any(k in ("mismatched", "missing", "extra") for k in kinds):
        case["status"] = "MISMATCHED"
        return case
    if "uncompared" in kinds or not human:
        case["status"] = "NOT_CHECKED"
        case["reason"] = "uncompared_fields" if human else "no_reference_fields"
        return case
    case["status"] = "MATCHED"
    return case


def _read_json(path: Any) -> Optional[dict[str, Any]]:
    import json
    from pathlib import Path

    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def compare_runs(
    episodes: Any,
    runs: Any,
    *,
    lens: Any = None,
    ignore_fields: Sequence[str] = (),
) -> dict[str, Any]:
    """Every reference episode against its run: ``openadapt.readiness-cases/v1``
    (local; it holds values)."""

    from pathlib import Path

    from openadapt_flow.readiness.reference import load_episodes

    runs = Path(runs)
    codecs = dict(getattr(lens, "field_codecs", None) or {})
    manifest = _read_json(runs / "run.json") or {}
    cases = []
    for _folder, episode in load_episodes(episodes):
        task_dir = runs / str(episode["task_id"])
        run = _read_json(task_dir / "run.json")
        proposed = _read_json(task_dir / "proposed_write.json")
        screen_path = task_dir / "before_screen.txt"
        before = (
            screen_path.read_text(encoding="utf-8") if screen_path.is_file() else None
        )
        cases.append(
            compare_case(
                episode,
                run,
                proposed,
                before_screen=before,
                codecs=codecs,
                ignore_fields=ignore_fields,
            )
        )
    return {
        "schema": "openadapt.readiness-cases/v1",
        "agent_label": manifest.get("agent_label"),
        "mode": manifest.get("mode"),
        "scope_statement": manifest.get("scope_statement"),
        "runs_landed_writes": manifest.get("landed_writes"),
        "cases": cases,
    }
