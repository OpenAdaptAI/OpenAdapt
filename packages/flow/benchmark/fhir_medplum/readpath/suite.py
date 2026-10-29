"""Run a blind fault suite for ``ensure_patient_registered`` on live Medplum.

    PYTHONPATH=. python -m benchmark.fhir_medplum.readpath.suite \\
        --suite heldout.yaml --sha256 <frozen digest> \\
        --contract benchmark/fhir_medplum/readpath/ensure_patient.yaml \\
        --out results.json

The suite format (``openadapt.medplum-fault-suite/v1``) is the one the blind
author was briefed on: per case a seed written by a setup user, then
create/update/delete operations by the bot or another user, then the
expected outcome. This harness executes it literally. The verifier under
test (``openadapt_flow.ensure_record``) reads only Medplum's FHIR API with
its own client: searches, ``AuditEvent`` and ``_history``.

Each case starts from a project with no clinical resources: the setup user
deletes every resource of the types the suite names, and of the sweep types,
before the seed. The deletes happen before the baseline, so they are outside
the run window.

Synthetic data only. The suite file is private; the results this writes hold
case ids, classes, expected and actual outcomes and reasons, no values.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Optional

from benchmark.fhir_medplum.fixture import FixtureError, MedplumFixture, _http

BOT_EMAIL = "openadapt.bot@example.invalid"
OTHER_EMAIL = "clinician.other@example.invalid"


def audit_is_read_only(fx: MedplumFixture, actors: "Actors", who: str) -> bool:
    """True when the member can't write an AuditEvent (refused by the server)."""

    probe = {
        "resourceType": "AuditEvent",
        "type": {"code": "rest"},
        "recorded": "2000-01-01T00:00:00Z",
        "source": {"observer": {"display": "preflight"}},
        "agent": [{"who": {"reference": actors.ref(who)}, "requestor": True}],
    }
    try:
        made = _http(
            "POST", f"{fx.fhir_url}/AuditEvent", body=probe, token=actors.token(who)
        )
    except FixtureError as exc:
        return "HTTP 403" in str(exc)
    _http(
        "DELETE", f"{fx.fhir_url}/AuditEvent/{made['id']}", token=actors.token("setup")
    )
    return False


def user_token(fx: MedplumFixture, email: str, password: str) -> str:
    """A token for one project member, by the same login flow the app uses."""

    import secrets

    verifier = secrets.token_urlsafe(32)
    login = _http(
        "POST",
        f"{fx.base_url}/auth/login",
        body={
            "email": email,
            "password": password,
            "scope": "openid",
            "codeChallengeMethod": "plain",
            "codeChallenge": verifier,
        },
    )
    if "code" not in login and login.get("memberships"):
        login = _http(
            "POST",
            f"{fx.base_url}/auth/profile",
            body={"login": login["login"], "profile": login["memberships"][0]["id"]},
        )
    token = _http(
        "POST",
        f"{fx.base_url}/oauth2/token",
        form={
            "grant_type": "authorization_code",
            "code": login["code"],
            "code_verifier": verifier,
        },
    )
    return token["access_token"]


def profile_ref(fx: MedplumFixture, token: str) -> str:
    me = _http("GET", f"{fx.base_url}/auth/me", token=token)
    prof = me["profile"]
    return f"{prof['resourceType']}/{prof['id']}"


class Actors:
    def __init__(self, fx: MedplumFixture) -> None:
        self.fx = fx
        self.passwords = {
            "bot": fx.ensure_user(BOT_EMAIL, "OpenAdapt", "Bot"),
            "other": fx.ensure_user(OTHER_EMAIL, "Casey", "Other"),
        }
        self.emails = {"bot": BOT_EMAIL, "other": OTHER_EMAIL}
        self._tokens: dict[str, tuple[float, str]] = {}

    def token(self, who: str) -> str:
        now = time.monotonic()
        cached = self._tokens.get(who)
        if cached and now - cached[0] < 600:
            return cached[1]
        if who == "setup":
            tok = self.fx.admin_token()
        else:
            tok = user_token(self.fx, self.emails[who], self.passwords[who])
        self._tokens[who] = (now, tok)
        return tok

    def ref(self, who: str) -> str:
        return profile_ref(self.fx, self.token(who))


# -- executing a case --------------------------------------------------------------


def _resolve(node: Any, names: dict[str, str]) -> Any:
    if isinstance(node, dict):
        if set(node) == {"ref"} and isinstance(node["ref"], str):
            return {"reference": names[node["ref"]]}
        return {k: _resolve(v, names) for k, v in node.items()}
    if isinstance(node, list):
        return [_resolve(v, names) for v in node]
    return node


def _walk(doc: Any, path: str, *, create: bool) -> tuple[Any, str]:
    parts = path.split(".")
    node = doc
    for i, part in enumerate(parts[:-1]):
        nxt_is_index = parts[i + 1].isdigit()
        if isinstance(node, list):
            idx = int(part)
            while create and len(node) <= idx:
                node.append([] if nxt_is_index else {})
            node = node[idx]
        else:
            if part not in node and create:
                node[part] = [] if nxt_is_index else {}
            node = node[part]
    return node, parts[-1]


def apply_edits(
    resource: dict[str, Any], op: dict[str, Any], names: dict[str, str]
) -> dict[str, Any]:
    out = copy.deepcopy(resource)
    out.pop("meta", None)
    for path, value in (op.get("set") or {}).items():
        parent, last = _walk(out, path, create=True)
        value = _resolve(value, names)
        if isinstance(parent, list):
            idx = int(last)
            while len(parent) <= idx:
                parent.append(None)
            parent[idx] = value
        else:
            parent[last] = value
    for path in op.get("remove") or []:
        parent, last = _walk(out, path, create=False)
        if isinstance(parent, list):
            parent.pop(int(last))
        else:
            parent.pop(last, None)
    return out


def write(
    fx: MedplumFixture,
    actors: Actors,
    who: str,
    op: dict[str, Any],
    names: dict[str, str],
) -> None:
    """One operation, by one actor. ``never_sent`` skips it."""

    if op.get("response") == "never_sent":
        return
    token = actors.token(who)
    kind = op["op"]
    if kind == "create":
        body = _resolve(op["resource"], names)
        made = _http(
            "POST", f"{fx.fhir_url}/{body['resourceType']}", body=body, token=token
        )
        if op.get("as"):
            names[op["as"]] = f"{made['resourceType']}/{made['id']}"
    elif kind == "update":
        ref = names[op["target"]]
        current = _http("GET", f"{fx.fhir_url}/{ref}", token=token)
        _http(
            "PUT",
            f"{fx.fhir_url}/{ref}",
            body=apply_edits(current, op, names),
            token=token,
        )
    elif kind == "delete":
        _http("DELETE", f"{fx.fhir_url}/{names[op['target']]}", token=token)
    else:
        raise ValueError(f"unknown op {kind!r}")


def types_in(suite: dict[str, Any]) -> set[str]:
    found: set[str] = {"Patient"}

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if isinstance(node.get("resourceType"), str):
                found.add(node["resourceType"])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(suite.get("cases"))
    return found


def wipe(fx: MedplumFixture, actors: Actors, types: set[str]) -> None:
    token = actors.token("setup")
    for rtype in sorted(types):
        for _ in range(20):
            bundle = _http(
                "GET", f"{fx.fhir_url}/{rtype}?_count=200&_elements=id", token=token
            )
            ids = [e["resource"]["id"] for e in bundle.get("entry") or []]
            if not ids:
                break
            for rid in ids:
                _http("DELETE", f"{fx.fhir_url}/{rtype}/{rid}", token=token)


def referral_params(referral: dict[str, Any]) -> dict[str, str]:
    """The suite's referral, in the demonstration's parameter names."""

    return {
        "fname": referral["given"],
        "lname": referral["family"],
        "DOB": referral["birthDate"],
        "sex": referral["gender"],
        "street": referral["line"],
        "city": referral["city"],
        "state_label": referral["state_name"],
        "postal_code": referral["postalCode"],
        "phone_home": referral["phone"],
        "email": referral["email"],
    }


def run_case(
    fx: MedplumFixture,
    actors: Actors,
    contract: Any,
    case: dict[str, Any],
    default_referral: dict[str, Any],
    types: set[str],
) -> dict[str, Any]:
    from openadapt_flow.ensure_record import capture_baseline, verify_ensure

    referral = {**default_referral, **(case.get("referral") or {})}
    params = referral_params(referral)
    wipe(fx, actors, types)
    names: dict[str, str] = {}
    for seed in case.get("seed") or []:
        write(fx, actors, "setup", {"op": "create", **seed}, names)
    time.sleep(1.5)  # seeds fall before the window start (to the second)
    os.environ["MEDPLUM_RECORD_TOKEN"] = fx.client_token("record-check")
    t0 = time.monotonic()
    baseline = capture_baseline(contract, params)
    for op in case.get("run") or []:
        write(fx, actors, op["actor"], op, names)
    result = verify_ensure(contract, params, baseline)
    expected = case["expected"]
    want_id = names.get(expected.get("id", ""), "").split("/")[-1] or None
    right = result.outcome == expected["outcome"] and (
        expected["outcome"] == "needs_review" or result.record_id == want_id
    )
    return {
        "id": case["id"],
        "class": case["class"],
        "expected": expected["outcome"],
        "actual": result.outcome,
        "id_right": expected["outcome"] == "needs_review"
        or result.record_id == want_id,
        "right": right,
        "fault_passed_as_verified": result.outcome != "needs_review" and not right,
        "reasons": result.reasons,
        "evidence": result.evidence,
        "seconds": round(time.monotonic() - t0, 2),
    }


def main(argv: Optional[list[str]] = None) -> int:
    import yaml

    from openadapt_flow.ensure_record import load_contract

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--suite", required=True)
    ap.add_argument(
        "--sha256", required=True, help="the frozen digest; refuses any other file"
    )
    ap.add_argument("--contract", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--only", action="append", help="run only these case ids")
    args = ap.parse_args(argv)

    raw = Path(args.suite).read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != args.sha256:
        print(f"suite digest {digest} is not the frozen {args.sha256}", file=sys.stderr)
        return 2
    suite = yaml.safe_load(raw)
    fx = MedplumFixture()
    actors = Actors(fx)
    bot_ref = actors.ref("bot")
    me = _http("GET", f"{fx.base_url}/auth/me", token=actors.token("bot"))
    if me["project"].get("superAdmin") or me["membership"].get("admin"):
        print("the bot is an administrator; refusing to run", file=sys.stderr)
        return 2
    policy = fx.restrict_agent(bot_ref)
    actors._tokens.pop("bot", None)  # sign in again under the policy
    if not audit_is_read_only(fx, actors, "bot"):
        print("the bot can write AuditEvents; refusing to run", file=sys.stderr)
        return 2
    os.environ["MEDPLUM_BOT_REF"] = bot_ref
    os.environ.setdefault("MEDPLUM_FHIR_URL", fx.fhir_url)
    contract = load_contract(args.contract)
    types = types_in(suite) | set(contract.change_feed.sweep_types)
    rows = []
    for case in suite["cases"]:
        if args.only and case["id"] not in args.only:
            continue
        try:
            row = run_case(fx, actors, contract, case, suite["referral"], types)
        except FixtureError as exc:
            row = {
                "id": case["id"],
                "class": case["class"],
                "error": str(exc)[:300],
                "right": False,
            }
        rows.append(row)
        print(
            json.dumps(
                {
                    k: row.get(k)
                    for k in ("id", "expected", "actual", "right", "reasons")
                }
            )
        )
    summary = {
        "suite_sha256": digest,
        "cases": len(rows),
        "right": sum(1 for r in rows if r.get("right")),
        "fault_passed_as_verified": sum(
            1 for r in rows if r.get("fault_passed_as_verified")
        ),
        "errors": sum(1 for r in rows if "error" in r),
        "agent_access_policy": f"AccessPolicy/{policy}",
        "agent_audit_write_refused": True,
    }
    Path(args.out).write_text(
        json.dumps({"summary": summary, "cases": rows}, indent=2) + "\n"
    )
    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
