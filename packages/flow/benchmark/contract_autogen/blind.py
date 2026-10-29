"""Record a blind write type's demonstrations on live Medplum, and run its suite.

    PYTHONPATH=. python -m benchmark.contract_autogen.blind teach \\
        --task task.yaml --out benchmark/contract_autogen/teach/<type>
    PYTHONPATH=. python -m benchmark.contract_autogen.blind suite \\
        --suite suite.yaml --sha256 <frozen digest> --contract c.yaml --out r.json

``teach`` writes the directory ``openadapt_flow.generate.ensure_contract``
reads. For each demonstration it empties the project of the type, seeds the
patient, reads every resource of the type, posts the demonstration's
resource as the other user (a person demonstrating), and reads again. Then
it saves the server's CapabilityStatement and its SearchParameter
definitions for the type. The server facts are #1640's probe, measured once
for this image (``fhir_medplum/readpath/probe.json``).

``suite`` runs a fault suite with #1640's executor (``readpath/suite.py``).
The one difference is that a case carries its task ``params``, not a
referral, and a ``{"ref": name}`` value is the seeded resource's reference.
BLIND.md says what is frozen and what is reported.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Optional

from benchmark.fhir_medplum.fixture import FixtureError, MedplumFixture, _http
from benchmark.fhir_medplum.readpath import suite as runner

HERE = Path(__file__).resolve().parent
PROBE = HERE.parent / "fhir_medplum" / "readpath" / "probe.json"


def resolve_params(params: dict[str, Any], names: dict[str, str]) -> dict[str, Any]:
    return {
        k: names[v["ref"]] if isinstance(v, dict) and set(v) == {"ref"} else v
        for k, v in params.items()
    }


def read_all(url: str, token: str) -> list[dict[str, Any]]:
    """Every resource of a search, across every page."""

    out: list[dict[str, Any]] = []
    for _ in range(50):
        bundle = _http("GET", url, token=token)
        out += [e["resource"] for e in bundle.get("entry") or []]
        url = next(
            (
                link["url"]
                for link in bundle.get("link") or []
                if link.get("relation") == "next"
            ),
            "",
        )
        if not url:
            return out
    raise FixtureError(f"more than 50 pages: {url}")


def _dump(path: Path, doc: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n")


def cmd_teach(args: argparse.Namespace) -> int:
    import yaml

    task = yaml.safe_load(Path(args.task).read_text())
    rtype = task["resource_type"]
    out = Path(args.out)
    fx = MedplumFixture()
    actors = runner.Actors(fx)
    reader = fx.client_token("record-check")
    search = f"{fx.fhir_url}/{rtype}?_count=200"
    for demo in task["demos"]:
        runner.wipe(fx, actors, {rtype, "Patient"})
        names: dict[str, str] = {}
        for seed in demo.get("seed") or []:
            runner.write(fx, actors, "setup", {"op": "create", **seed}, names)
        before = read_all(search, reader)
        runner.write(
            fx, actors, "other", {"op": "create", "resource": demo["resource"]}, names
        )
        after = read_all(search, reader)
        base = out / "demos" / demo["name"]
        _dump(base / "params.json", resolve_params(demo["params"], names))
        _dump(base / "before.json", before)
        _dump(base / "after.json", after)
    _dump(
        out / "capability.json", _http("GET", f"{fx.fhir_url}/metadata", token=reader)
    )
    _dump(
        out / "search_parameters.json",
        read_all(f"{fx.fhir_url}/SearchParameter?base={rtype}&_count=200", reader),
    )
    shutil.copyfile(PROBE, out / "facts.json")
    _dump(
        out / "teach.json",
        {
            "resource_type": rtype,
            "deployment": {
                "base_url": "${MEDPLUM_FHIR_URL}",
                "access_token_env": "MEDPLUM_RECORD_TOKEN",
                "self": ["${MEDPLUM_BOT_REF}"],
            },
        },
    )
    print(f"teach directory: {out}")
    return 0


def run_case(
    fx: MedplumFixture,
    actors: runner.Actors,
    contract: Any,
    case: dict[str, Any],
    types: set[str],
) -> dict[str, Any]:
    from openadapt_flow.ensure_record import capture_baseline, verify_ensure

    runner.wipe(fx, actors, types)
    names: dict[str, str] = {}
    for seed in case.get("seed") or []:
        runner.write(fx, actors, "setup", {"op": "create", **seed}, names)
    params = resolve_params(case["params"], names)
    time.sleep(2.5)  # setup writes must be outside the window (see suite.py)
    os.environ["MEDPLUM_RECORD_TOKEN"] = fx.client_token("record-check")
    t0 = time.monotonic()
    baseline = capture_baseline(contract, params)
    for op in case.get("run") or []:
        runner.write(fx, actors, op["actor"], op, names)
    result = verify_ensure(contract, params, baseline)
    expected = case["expected"]
    want_id = names.get(expected.get("id") or "", "").split("/")[-1] or None
    id_right = expected["outcome"] == "needs_review" or result.record_id == want_id
    right = result.outcome == expected["outcome"] and id_right
    return {
        "id": case["id"],
        "class": case["class"],
        "expected": expected["outcome"],
        "actual": result.outcome,
        "right": right,
        "fault_passed_as_verified": result.outcome != "needs_review" and not right,
        "false_stop": expected["outcome"] != "needs_review"
        and result.outcome == "needs_review",
        "reasons": result.reasons,
        "seconds": round(time.monotonic() - t0, 2),
    }


def cmd_suite(args: argparse.Namespace) -> int:
    import yaml

    from openadapt_flow.ensure_record import load_contract

    raw = Path(args.suite).read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != args.sha256:
        print(f"suite digest {digest} is not the frozen {args.sha256}", file=sys.stderr)
        return 2
    suite = yaml.safe_load(raw)
    fx = MedplumFixture()
    actors = runner.Actors(fx)
    bot_ref = actors.ref("bot")
    me = _http("GET", f"{fx.base_url}/auth/me", token=actors.token("bot"))
    if me["project"].get("superAdmin") or me["membership"].get("admin"):
        print("the bot is an administrator; refusing to run", file=sys.stderr)
        return 2
    policy = fx.restrict_agent(bot_ref)
    actors._tokens.pop("bot", None)  # sign in again under the policy
    if not runner.audit_is_read_only(fx, actors, "bot"):
        print("the bot can write AuditEvents; refusing to run", file=sys.stderr)
        return 2
    os.environ["MEDPLUM_BOT_REF"] = bot_ref
    os.environ.setdefault("MEDPLUM_FHIR_URL", fx.fhir_url)
    os.environ.setdefault("MEDPLUM_RECORD_TOKEN", fx.client_token("record-check"))
    contract = load_contract(args.contract)
    types = runner.types_in(suite) | set(contract.change_feed.sweep_types)
    rows = []
    for case in suite["cases"]:
        try:
            row = run_case(fx, actors, contract, case, types)
        except FixtureError as exc:
            row = {"id": case["id"], "class": case["class"], "error": str(exc)[:300]}
            row["right"] = False
        rows.append(row)
        print(
            json.dumps({k: row.get(k) for k in ("id", "expected", "actual", "right")})
        )
    summary = {
        "suite_sha256": digest,
        "cases": len(rows),
        "right": sum(1 for r in rows if r.get("right")),
        "fault_passed_as_verified": sum(
            1 for r in rows if r.get("fault_passed_as_verified")
        ),
        "false_stops": sum(1 for r in rows if r.get("false_stop")),
        "errors": sum(1 for r in rows if "error" in r),
        "agent_access_policy": f"AccessPolicy/{policy}",
    }
    _dump(Path(args.out), {"summary": summary, "cases": rows})
    print(json.dumps(summary))
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("teach")
    t.add_argument("--task", required=True)
    t.add_argument("--out", required=True)
    s = sub.add_parser("suite")
    s.add_argument("--suite", required=True)
    s.add_argument("--sha256", required=True)
    s.add_argument("--contract", required=True)
    s.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    return cmd_teach(args) if args.cmd == "teach" else cmd_suite(args)


if __name__ == "__main__":
    raise SystemExit(main())
