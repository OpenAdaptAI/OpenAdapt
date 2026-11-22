"""Copy a readiness run's files out of the machine-local run folder, by
allowlist only.

A task's run folder holds files that must never leave it: held requests and
screenshots, the screen text, the flight recorder's ``egress/`` record and
``egress/pseudonyms.json`` (the token-to-real-value map, in plaintext), the
agent's log. :func:`export_task` copies only the file names in
:data:`ALLOWED` and, for each, only the keys its schema allows, at the top
level of the folder; everything else (an unknown file, a directory, a file
with an unexpected key or that isn't a JSON object) is left behind and
listed in the result. Fail closed: a new file type is never exported until
it is added here.

``proposed_write.json`` and ``case.json`` carry field values; export them
only from synthetic runs (``include_values=True``). The scorecard
(``scorecard.json``) carries no values by construction.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

__all__ = ["ALLOWED", "VALUE_FILES", "export_task"]

#: File name -> the top-level keys it may carry.
ALLOWED: Mapping[str, frozenset[str]] = {
    "run.json": frozenset({
        "schema", "task_id", "status", "ended", "agent_wall_s", "exit_code", "usage",
        "model_cost_usd", "model_cost_source", "egress", "landed_writes", "held",
        "double_save", "other_origin_refused", "proposed", "before_screen",
        "extra_target_writes", "target_gate", "session", "notes", "requests",
        "browser_network",
    }),
    "egress.json": frozenset({
        "mode", "enforced", "model_calls", "cost_usd", "cost_source", "refused_requests",
    }),
    "usage.json": frozenset({
        "schema", "calls", "input_tokens", "output_tokens", "model", "cost_usd",
        "stopped", "final_line",
    }),
    "case.json": frozenset({
        "schema", "task_id", "status", "reason", "identity", "fields", "contamination",
        "human_time_s", "agent_time_s", "model_cost_usd", "model_cost_source",
        "model_calls_metered", "landed_writes", "double_save", "out_of_scope_requests",
        "undeclared_fields", "extra_held_writes",
    }),
    "proposed_write.json": frozenset({
        "schema", "task_id", "request_index", "method", "url_path", "origin", "held_at",
        "identity", "fields", "withheld_fields", "resource_type", "refused", "screenshot",
        "screen_text", "source", "target",
    }),
    "scorecard.json": frozenset({
        "schema", "generated_at", "agent_label", "task_type", "mode", "replay_state",
        "scope_statement", "cases_total", "matched", "mismatched", "not_comparable",
        "not_checked", "failed_scope", "scope_ok", "risks", "field_errors",
        "agent_time_s", "human_time_s", "model_cost_usd", "egress", "savings",
        "landed_writes",
    }),
}
#: Files that carry field values.
VALUE_FILES = frozenset({"case.json", "proposed_write.json"})
#: run.json's ``usage`` comes from the agent: only its counts are kept.
_USAGE_KEYS = frozenset({"calls", "input_tokens", "output_tokens", "model", "cost_usd"})


def _clean(name: str, doc: dict[str, Any]) -> dict[str, Any]:
    if name == "run.json":
        usage = doc.get("usage")
        doc = {
            **doc,
            "usage": {k: v for k, v in usage.items() if k in _USAGE_KEYS}
            if isinstance(usage, dict) else None,
        }
    if name == "usage.json":
        doc = {k: v for k, v in doc.items() if k not in ("final_line",)}
    return doc


def export_task(
    task_dir: Path | str,
    dest: Path | str,
    *,
    include_values: bool = False,
    extra: Mapping[str, Path] = {},
) -> dict[str, list[str]]:
    """Copy the allowed files of one task's run folder (and ``extra``, such as
    ``{"scorecard.json": path}``) into ``dest``; returns
    ``{"copied": [...], "left": [...]}`` with the reason for each left file."""

    task_dir, dest = Path(task_dir), Path(dest)
    sources = {p.name: p for p in sorted(task_dir.iterdir())} if task_dir.is_dir() else {}
    sources.update({name: Path(p) for name, p in extra.items()})
    copied: list[str] = []
    left: list[str] = []
    for name, path in sources.items():
        allowed = ALLOWED.get(name)
        if allowed is None or not path.is_file():
            left.append(f"{name}: not an exported file type")
            continue
        if name in VALUE_FILES and not include_values:
            left.append(f"{name}: carries field values")
            continue
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            left.append(f"{name}: not readable JSON")
            continue
        if not isinstance(doc, dict) or set(doc) - allowed:
            left.append(f"{name}: unexpected keys {sorted(set(doc) - allowed) if isinstance(doc, dict) else 'not an object'}")
            continue
        dest.mkdir(parents=True, exist_ok=True)
        (dest / name).write_text(
            json.dumps(_clean(name, doc), indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        copied.append(name)
    return {"copied": copied, "left": left}
