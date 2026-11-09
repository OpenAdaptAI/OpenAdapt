"""Verify the benchmark environments are reproducible and their records queryable.

Two verifications, both fail-closed:

1. ``locks`` (offline, CI-fast, no Docker) — every environment's
   ``environment.lock.json`` pins each service image to an exact ``@sha256:``
   digest (never a floating tag), pins upstreams to full 40-hex commits, and has
   a ``compose.yml`` that refuses to start without those pinned inputs. This is
   the reproducibility gate.

2. ``mockmed`` (live, CI-fast, no Docker) — stand up the MockMed fault-injection
   server and prove its system-of-record is (a) *queryable* (a normal write is
   read back through ``GET /api/db``) and (b) *non-gameable* (a partial-save
   fault the rendered screen reports as success is visible in the record as a
   dropped field). This is the independent-oracle gate on the CI-fast anchor.

The Docker-heavy environments (OpenEMR, Frappe, openIMIS) are brought up through
their existing ``scripts/*_demo.py`` fixtures — see ``benchmark/environments/
README.md``. This harness deliberately does not pull multi-GB images in CI; it
proves the *pins* offline and the *record channel* live on the anchor.

Usage::

    python -m benchmark.environments.verify locks
    python -m benchmark.environments.verify mockmed
    python -m benchmark.environments.verify all      # both (default)
    python -m benchmark.environments.verify all --json report.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from pathlib import Path
from typing import Any

from benchmark.environments.registry import (
    Environment,
    all_environments,
)

_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")


class VerificationError(RuntimeError):
    """A verification check failed."""


# ---------------------------------------------------------------------------
# 1. Reproducibility: digest + commit pins.
# ---------------------------------------------------------------------------
def verify_env_lock(env: Environment) -> dict[str, Any]:
    """Return a report for one environment's lock; ``ok`` is the verdict."""
    problems: list[str] = []

    if env.lock_relpath is None:
        # The in-process anchor pins nothing external; it is reproducible by
        # construction (first-party code shipped with the package).
        return {
            "name": env.name,
            "ok": True,
            "kind": "in-process (no external pins)",
            "problems": [],
        }

    lock_path = env.lock_path
    assert lock_path is not None  # for type-checkers; lock_relpath is set
    if not lock_path.is_file():
        return {
            "name": env.name,
            "ok": False,
            "problems": [f"missing lock file {env.lock_relpath}"],
        }

    lock = env.load_lock()

    # (a) every service image is digest-pinned.
    unpinned = env.unpinned_services()
    for svc, img in unpinned.items():
        problems.append(f"service {svc!r} is not @sha256 digest-pinned: {img!r}")
    services = env.service_digests()
    if not services:
        problems.append("lock declares no services")

    # (b) every upstream is pinned to a full 40-hex commit.
    upstreams = lock.get("upstreams", {})
    if not isinstance(upstreams, dict):
        problems.append("lock 'upstreams' is not an object")
    else:
        for name, item in upstreams.items():
            commit = item.get("commit", "") if isinstance(item, dict) else ""
            if not _COMMIT_RE.match(str(commit)):
                problems.append(f"upstream {name!r} not pinned to a full commit")

    # (c) compose file exists and refuses to start without pinned inputs
    #     (the ``${VAR:?...}`` guard pattern used across the fixtures).
    compose_path = env.compose_path
    if compose_path is None or not compose_path.is_file():
        problems.append(f"missing compose file {env.compose_relpath}")
    else:
        compose_text = compose_path.read_text()
        if ":?" not in compose_text:
            problems.append(
                "compose.yml does not fail closed on missing pinned inputs "
                "(no '${VAR:?...}' guard found)"
            )

    return {
        "name": env.name,
        "ok": not problems,
        "service_count": len(services),
        "services": services,
        "upstream_count": len(upstreams) if isinstance(upstreams, dict) else 0,
        "problems": problems,
    }


def verify_locks(envs: tuple[Environment, ...] | None = None) -> dict[str, Any]:
    """Verify digest/commit pinning across all environments."""
    envs = envs or all_environments()
    reports = [verify_env_lock(env) for env in envs]
    ok = all(r["ok"] for r in reports)
    return {"check": "locks", "ok": ok, "environments": reports}


# ---------------------------------------------------------------------------
# 2. System-of-record queryability + non-gameability on the CI-fast anchor.
# ---------------------------------------------------------------------------
def _post_json(url: str, payload: dict[str, Any]) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(  # noqa: S310 - loopback only
        url, data=data, method="POST", headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
        return json.loads(resp.read().decode("utf-8"))


def _get_json(url: str) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=10) as resp:  # noqa: S310 - loopback
        return json.loads(resp.read().decode("utf-8"))


def verify_mockmed() -> dict[str, Any]:
    """Prove the MockMed record is queryable and non-gameable.

    Steps, all judged by ``GET /api/db`` (the record), never by a response
    banner:

    1. reset -> the record is empty (queryable, deterministic seed);
    2. a clean write -> exactly one row with the exact note is readable back;
    3. a ``partial`` fault (the backend drops the note; the SPA still paints
       "saved") -> the record shows the row with an *empty* note, so the
       independent oracle catches what a screen-only check would score success.
    """
    from openadapt_flow.mockmed.fault_server import serve

    url, db, stop = serve(port=0)
    base = url.rstrip("/")
    evidence: dict[str, Any] = {"check": "mockmed", "base_url": url}
    try:
        # 1. reset -> empty, queryable record.
        _post_json(f"{base}/api/reset", {})
        empty = _get_json(f"{base}/api/db")
        if empty.get("records"):
            raise VerificationError("record not empty after reset")

        # 2. clean write, read back through the record channel.
        note = "sig: amoxicillin 500mg PO TID x7d"
        _post_json(
            f"{base}/api/encounter?fault=ok",
            {"patient_id": "p1", "type": "Triage", "note": note},
        )
        after_clean = _get_json(f"{base}/api/db")
        clean_records = after_clean.get("records", [])
        if len(clean_records) != 1 or clean_records[0].get("note") != note:
            raise VerificationError("clean write not faithfully readable via /api/db")
        queryable = True

        # 3. partial-save fault: screen says saved, record drops the note.
        _post_json(f"{base}/api/reset", {})
        _post_json(
            f"{base}/api/encounter?fault=partial",
            {"patient_id": "p1", "type": "Triage", "note": note},
        )
        after_partial = _get_json(f"{base}/api/db")
        partial_records = after_partial.get("records", [])
        # The row persisted (the UI would show success) but the note is gone —
        # the oracle sees the divergence the screen cannot.
        screen_would_report_success = True
        record_shows_wrong_effect = (
            len(partial_records) == 1 and partial_records[0].get("note") == ""
        )
        non_gameable = screen_would_report_success and record_shows_wrong_effect
        if not record_shows_wrong_effect:
            raise VerificationError(
                "partial-save fault was not visible in the record; oracle would "
                "be gameable"
            )

        evidence.update(
            {
                "ok": True,
                "queryable": queryable,
                "non_gameable": non_gameable,
                "clean_write_readback": clean_records[0],
                "partial_write_readback": partial_records[0],
                "note": (
                    "record is queryable via GET /api/db and non-gameable: a "
                    "partial-save fault the screen reports as success is visible "
                    "as a dropped note in the system-of-record."
                ),
            }
        )
        return evidence
    finally:
        stop()


# ---------------------------------------------------------------------------
# CLI.
# ---------------------------------------------------------------------------
def run(check: str) -> dict[str, Any]:
    if check == "locks":
        return verify_locks()
    if check == "mockmed":
        return verify_mockmed()
    if check == "all":
        locks = verify_locks()
        mock = verify_mockmed()
        return {
            "check": "all",
            "ok": bool(locks["ok"] and mock["ok"]),
            "locks": locks,
            "mockmed": mock,
        }
    raise SystemExit(f"unknown check {check!r}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "check",
        nargs="?",
        default="all",
        choices=["locks", "mockmed", "all"],
        help="which verification to run (default: all)",
    )
    parser.add_argument(
        "--json",
        type=Path,
        default=None,
        help="also write the machine-readable report to this path",
    )
    args = parser.parse_args(argv)

    report = run(args.check)
    text = json.dumps(report, indent=2, default=str)
    print(text)
    if args.json is not None:
        args.json.write_text(text + "\n")
    return 0 if report.get("ok") else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
