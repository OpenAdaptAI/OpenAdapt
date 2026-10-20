"""A pseudonymous operator record, built only from anchored offline Seals.

``openadapt-flow local-seal profile RUN_DIRS... --keys site-keys.json`` writes
``operator-profile.json`` (``openadapt.operator-profile/v1``). It is keyed
by the Seal key's ID and fingerprint, so it describes "whoever holds this
key", not a named person, unless a run authorization signed with a GitHub
SSH key links one (see below).

For each key it states:

* how many runs ended in each outcome. All four headline outcomes are always
  shown, with zeros: verified, stopped before any effect, completed but
  unverified, failed. Any other outcome is listed by its code. Successes are
  never shown alone.
* the apps and programs the runs used, the first and last time the
  transparency log recorded one of its Seals, and the log indexes, so anyone
  can look each one up.
* ``run_list``: one line per counted Seal (its SHA-256, scope item, outcome
  codes, app, program, issue time and log entry), so a reader can recount.
* ``linked_identity``, only when a run authorization verifies, its signing
  key was checked against GitHub live (``--live``) or against allowed
  signers the verifier pinned, it names this Seal key in its scope, and the
  key's Seals reference it. A handle checked only against the copy stored
  with the authorization is self-asserted and never linked.

The same Seal or Rekor entry given twice counts once. An item used by two
Seals under a single-use authorization counts once, for the first Seal.

Only Seals that verify against the pinned keys *and* carry a valid
transparency-log anchor count. Everything else is counted under
``excluded`` and nothing more is said about it.

The profile holds no report content: no patient names, parameters, record
IDs, run IDs, directory names or screenshots. It is built from the Seal, the
anchor, and four fields of the sealed report: the two outcome codes, the
program name and the app name.
"""

from __future__ import annotations

import base64
import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Sequence

SCHEMA = "openadapt.operator-profile/v1"
PROFILE_FILE = "operator-profile.json"
HEADLINE = ("verified", "stopped_before_effect", "completed_unverified", "failed")
_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


def outcome_category(execution_outcome: Any, transaction_outcome: Any) -> str:
    """One headline category, or the precise outcome code for anything else."""
    tx = transaction_outcome if isinstance(transaction_outcome, str) else None
    ex = execution_outcome if isinstance(execution_outcome, str) else None
    if ex == "VERIFIED" and tx in (None, "VERIFIED"):
        return "verified"
    if tx == "HALTED_BEFORE_EFFECT":
        return "stopped_before_effect"
    if tx == "COMPLETED_UNVERIFIED" or (tx is None and ex == "COMPLETED_UNVERIFIED"):
        return "completed_unverified"
    if tx == "FAILED_PLATFORM" or (tx is None and ex == "FAILED"):
        return "failed"
    return tx or ex or "UNKNOWN"


def _clean_name(value: Any) -> Optional[str]:
    """A short lower-case name, or nothing: free text never enters the profile."""
    if isinstance(value, str) and _NAME.match(value.lower()):
        return value.lower()
    return None


def app_name(report: dict[str, Any]) -> Optional[str]:
    """The report's app name; otherwise the program name's first word (``openemr``)."""
    for key in ("app", "app_name", "application_name"):
        name = _clean_name(report.get(key))
        if name:
            return name
    program = _clean_name(report.get("workflow_name"))
    return program.split("-", 1)[0] if program else None


def _code(value: Any) -> Optional[str]:
    return (
        value
        if isinstance(value, str) and re.match(r"^[A-Z][A-Z_]{0,31}$", value)
        else None
    )


def _iso(seconds: int) -> str:
    return datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_profile(
    run_dirs: Sequence[Path],
    registry: Sequence[Any],
    *,
    log_keys: Optional[Sequence[Any]] = None,
    authorizations: Sequence[Any] = (),
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Count anchored, verified Seals per Seal key. See the module docstring."""
    from openadapt_flow.offline_seal import (
        SEAL_FILE,
        LocalSealV1,
        loads_strict,
        seal_sha256,
        verify_run,
    )
    from openadapt_flow.run_authorization import check_run, reused_items

    operators: dict[str, dict[str, Any]] = {}
    excluded = {"unanchored": 0, "not_valid": 0, "duplicate": 0, "item_reused": 0}
    valid: list[tuple[Any, dict[str, Any], dict[str, Any], str]] = []
    seen_seals: set[str] = set()
    seen_entries: set[str] = set()
    for run_dir in run_dirs:
        run_dir = Path(run_dir)
        result = verify_run(run_dir, registry, require_anchor=True, log_keys=log_keys)
        if not result.ok or result.anchor is None:
            key = "unanchored" if result.reason == "anchor_missing" else "not_valid"
            excluded[key] += 1
            continue
        seal = LocalSealV1.model_validate(
            loads_strict((run_dir / SEAL_FILE).read_text(encoding="utf-8"))
        ).wire()
        digest = seal_sha256(seal)
        uuid = str(result.anchor.uuid)
        # The same Seal (or the same log entry) counted twice is one run.
        if digest in seen_seals or uuid in seen_entries:
            excluded["duplicate"] += 1
            continue
        seen_seals.add(digest)
        seen_entries.add(uuid)
        report = loads_strict((run_dir / "report.json").read_bytes())
        valid.append((result, seal, report, digest))
    reused: set[str] = set()
    for auth in authorizations:
        if auth.ok:
            reused |= reused_items([seal for _, seal, _, _ in valid], auth)
    for result, seal, report, digest in valid:
        if digest in reused:
            excluded["item_reused"] += 1
            continue
        key_id = str(result.key_id)
        op = operators.get(key_id)
        if op is None:
            pinned = next(k for k in registry if k.key_id == key_id)
            raw = base64.b64decode(pinned.public_key)
            op = operators[key_id] = {
                "seal_key_id": key_id,
                "public_key": pinned.public_key,
                "public_key_sha256": hashlib.sha256(raw).hexdigest(),
                "run_list": [],
                "outcomes": {name: 0 for name in HEADLINE},
                "other_outcomes": {},
                "runs": 0,
                "apps": set(),
                "programs": set(),
                "anchored": [],
                "identities": {},
            }
        category = outcome_category(
            report.get("execution_outcome"), report.get("transaction_outcome")
        )
        if category in HEADLINE:
            op["outcomes"][category] += 1
        else:
            op["other_outcomes"][category] = op["other_outcomes"].get(category, 0) + 1
        op["runs"] += 1
        for bucket, value in (
            ("apps", app_name(report)),
            ("programs", _clean_name(report.get("workflow_name"))),
        ):
            if value:
                op[bucket].add(value)
        op["run_list"].append(
            {
                "seal_sha256": digest,
                "scope_item": seal.get("scope_item"),
                "outcome": category,
                "execution_outcome": _code(report.get("execution_outcome")),
                "transaction_outcome": _code(report.get("transaction_outcome")),
                "app": app_name(report),
                "program": _clean_name(report.get("workflow_name")),
                "issued_at": seal.get("issued_at"),
                "anchor": {
                    "uuid": result.anchor.uuid,
                    "log_index": result.anchor.log_index,
                    "integrated_time": result.anchor.integrated_time,
                },
            }
        )
        op["anchored"].append(
            (int(result.anchor.integrated_time or 0), int(result.anchor.log_index or 0))
        )
        for auth in authorizations:
            # Only a verified identity (github.com live, or pinned allowed
            # signers) is linked; a self-asserted handle never is.
            if (
                auth.ok
                and auth.identity_verified
                and auth.statement is not None
                and check_run(seal, report, auth) is None
            ):
                op["identities"][auth.statement.authorizer] = auth
    out: list[dict[str, Any]] = []
    for key_id in sorted(operators):
        op = operators[key_id]
        times = sorted(t for t, _ in op["anchored"])
        entry: dict[str, Any] = {
            "seal_key_id": key_id,
            "public_key": op["public_key"],
            "public_key_sha256": op["public_key_sha256"],
            "runs": op["runs"],
            "run_list": sorted(
                op["run_list"], key=lambda r: r["anchor"]["integrated_time"] or 0
            ),
            "outcomes": op["outcomes"],
            "other_outcomes": dict(sorted(op["other_outcomes"].items())),
            "apps": sorted(op["apps"]),
            "programs": sorted(op["programs"]),
            "first_anchored_at": _iso(times[0]),
            "last_anchored_at": _iso(times[-1]),
            "rekor_log_indexes": sorted(i for _, i in op["anchored"]),
        }
        if op["identities"]:
            entry["linked_identity"] = [
                {
                    "handle": handle,
                    "provider": auth.statement.provider,
                    "ssh_key_fingerprint": auth.signer_fingerprint,
                    "run_authorization_sha256": auth.sha256,
                    "checked_against": auth.signers_source,
                }
                for handle, auth in sorted(op["identities"].items())
            ]
        out.append(entry)
    return {
        "schema": SCHEMA,
        "generated_at": (now or datetime.now(timezone.utc))
        .astimezone(timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%SZ"),
        "counted": "Seals that verify against the pinned keys and have a valid "
        "transparency-log anchor",
        "operators": {e["seal_key_id"]: e for e in out},
        "excluded": excluded,
    }
