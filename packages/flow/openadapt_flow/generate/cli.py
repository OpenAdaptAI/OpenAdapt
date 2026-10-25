"""``openadapt-flow generate-parts``: the application parts, generated and verified.

    openadapt-flow generate-parts channels --openapi api.json --base-url URL \\
        --auth-env TOKEN_VAR --out channels.json
    openadapt-flow generate-parts snapshot demo1 --channels channels.json --label before
    #   ... demonstrate the save once, with the values in demo1/params.json ...
    openadapt-flow generate-parts snapshot demo1 --channels channels.json --label after
    #   (repeat for demo2, demo3 with different values)
    openadapt-flow generate-parts build --demo demo1 --demo demo2 --demo demo3 \\
        --channels channels.json --out parts/
    openadapt-flow generate-parts approve parts/ --approved-by "Dana Reyes"

No step makes a model call. ``build`` writes nothing that runs: a person
approves ``parts/parts.review.md`` first.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Optional

from openadapt_flow.cli_hints import command


def _load_doc(source: str) -> Any:
    """A JSON or YAML document from a file or an http(s) URL."""

    if source.startswith(("http://", "https://")):
        import urllib.request

        with urllib.request.urlopen(source, timeout=30) as resp:  # noqa: S310
            text = resp.read().decode("utf-8")
    else:
        text = Path(source).read_text(encoding="utf-8")
    try:
        return json.loads(text)
    except ValueError:
        import yaml

        return yaml.safe_load(text)


def _fhir_samples(
    section: dict[str, Any], token_env: Optional[str], verify_tls: bool
) -> list[Any]:
    """A few resources of the type, read once, to learn their field paths."""

    import ssl
    import urllib.request

    url = f"{section['base_url']}/{section['resource_type']}?_count=5"
    headers = {"Accept": "application/fhir+json"}
    if token_env and os.environ.get(token_env):
        headers["Authorization"] = f"Bearer {os.environ[token_env]}"
    context = None if verify_tls else ssl._create_unverified_context()  # noqa: S323
    try:
        with urllib.request.urlopen(
            urllib.request.Request(url, headers=headers), timeout=30, context=context
        ) as resp:
            bundle = json.loads(resp.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 - no samples: the candidate reads ids only
        return []
    return [
        e.get("resource")
        for e in (bundle.get("entry") or [])
        if isinstance(e, dict) and isinstance(e.get("resource"), dict)
    ]


def cmd_channels(args: argparse.Namespace) -> int:
    from openadapt_flow.generate.channel import (
        candidates_from_fhir,
        candidates_from_openapi,
        with_field_paths,
    )

    found = []
    if args.openapi:
        auth = {"bearer_env": args.auth_env} if args.auth_env else None
        found += candidates_from_openapi(
            _load_doc(args.openapi), args.base_url, auth=auth
        )
    if args.fhir_capability:
        base = args.fhir_base_url or args.base_url
        auth = {"access_token_env": args.auth_env} if args.auth_env else None
        for cand in candidates_from_fhir(
            _load_doc(args.fhir_capability), base, auth=auth
        ):
            if args.insecure_tls:
                cand.section["verify_tls"] = False
            if args.resource and cand.section["resource_type"] not in args.resource:
                continue
            found.append(
                with_field_paths(
                    cand,
                    _fhir_samples(cand.section, args.auth_env, not args.insecure_tls),
                )
            )
    if not found:
        print("No candidate read channel in the given documents.", file=sys.stderr)
        return 2
    Path(args.out).write_text(
        json.dumps(
            [{"origin": c.origin, "section": c.section} for c in found], indent=2
        )
        + "\n"
    )
    print(f"{len(found)} candidate channel(s) written to {args.out}:")
    for c in found:
        print(f"  {c.origin}")
    return 0


def _candidates(path: str) -> list[Any]:
    from openadapt_flow.generate.channel import ChannelCandidate

    return [
        ChannelCandidate(c["section"], c["origin"])
        for c in json.loads(Path(path).read_text())
    ]


def cmd_snapshot(args: argparse.Namespace) -> int:
    from openadapt_flow.generate.build import snapshot

    demo = Path(args.demo)
    params = json.loads((demo / "params.json").read_text())
    counts = snapshot(_candidates(args.channels), params, demo, args.label)
    for cid, n in counts.items():
        print(f"{cid}: {'unreadable' if n < 0 else f'{n} records'}")
    return 0


def _reference(spec: Optional[str]) -> Any:
    """``module:function`` -> the hand-built duplicate check, ``(new, table) -> bool``."""

    if not spec:
        return None
    import importlib

    module, _, name = spec.partition(":")
    return getattr(importlib.import_module(module), name)


def cmd_build(args: argparse.Namespace) -> int:
    from openadapt_flow.generate.build import BuildError, build

    try:
        result = build(
            [Path(d) for d in args.demo],
            Path(args.out),
            _candidates(args.channels),
            name=args.name,
            timeout_s=args.timeout,
            verify_tls=not args.insecure_tls,
            expected_records=args.expected_records,
            reference_duplicate_check=_reference(args.reference_duplicate_check),
            reference_identity=_reference(args.reference_identity),
            false_stop_budget=args.false_stop_budget,
        )
    except BuildError as exc:
        print(f"generate-parts REFUSED: {exc}", file=sys.stderr)
        return 2
    print((Path(args.out) / "parts.review.md").read_text())
    for problem in result.problems:
        print(f"PROBLEM: {problem}")
    print(f"Times (s): {json.dumps(result.seconds)}")
    print(
        "ADMITTED (not approved): approve with `"
        + command(f"generate-parts approve {args.out} --approved-by NAME")
        + "`"
        if result.admitted
        else "NOT ADMITTED: the problems above must be fixed first."
    )
    return 0 if result.admitted else 1


def cmd_approve(args: argparse.Namespace) -> int:
    from openadapt_flow.generate.build import BuildError, approve

    out = Path(args.parts)
    print((out / "parts.review.md").read_text())
    if not sys.stdin.isatty():
        print("generate-parts approve needs a person at a terminal.", file=sys.stderr)
        return 2
    answer = input('Type "approve" to approve exactly these parts: ').strip()
    if answer != "approve":
        print("Not approved.")
        return 1
    try:
        approval = approve(out, args.approved_by)
    except BuildError as exc:
        print(f"generate-parts REFUSED: {exc}", file=sys.stderr)
        return 2
    print(f"Approved by {approval['approved_by']} ({approval['manifest_sha256']}).")
    return 0


def add_parser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "generate-parts",
        help="Generate the app-specific parts (read channel, record contract, "
        "duplicate rule, input types) from demonstrations, and verify each",
    )
    gp = p.add_subparsers(dest="generate_command", required=True)

    c = gp.add_parser(
        "channels", help="List candidate read channels from published documents"
    )
    c.add_argument("--openapi", help="OpenAPI document (file or URL)")
    c.add_argument("--fhir-capability", help="FHIR CapabilityStatement (file or URL)")
    c.add_argument("--base-url", required=True, help="Base URL of the REST API")
    c.add_argument("--fhir-base-url", help="FHIR base URL, when it differs")
    c.add_argument("--resource", action="append", help="Only these FHIR resource types")
    c.add_argument("--auth-env", help="Environment variable holding the read token")
    c.add_argument(
        "--insecure-tls", action="store_true", help="A local test fixture only"
    )
    c.add_argument("--out", required=True)
    c.set_defaults(func=cmd_channels)

    s = gp.add_parser(
        "snapshot", help="Read every candidate channel before or after a demonstration"
    )
    s.add_argument("demo", help="The demonstration's folder (holds params.json)")
    s.add_argument("--channels", required=True)
    s.add_argument("--label", required=True, choices=("before", "after"))
    s.set_defaults(func=cmd_snapshot)

    b = gp.add_parser("build", help="Generate and verify the parts")
    b.add_argument(
        "--demo", action="append", required=True, help="A demonstration folder (2 or 3)"
    )
    b.add_argument("--channels", required=True)
    b.add_argument("--out", required=True)
    b.add_argument(
        "--name", default="record", help="What the save writes, for the review"
    )
    b.add_argument(
        "--timeout", type=float, default=5.0, help="Read-back timeout per check"
    )
    b.add_argument("--expected-records", type=int, default=None)
    b.add_argument(
        "--reference-duplicate-check",
        metavar="MODULE:FUNCTION",
        help="The system's hand-built duplicate check, (new, table) -> bool. The "
        "generated rule replaces it only if it misses no more on the calibration set",
    )
    b.add_argument(
        "--reference-identity",
        metavar="MODULE:FUNCTION",
        help="The site's hand-written source adapter, rows -> template records. "
        "The generated identity mapping is used only if it does no worse on the "
        "perturbation suite",
    )
    b.add_argument(
        "--false-stop-budget",
        type=float,
        default=0.02,
        help="Estimated false stops per new record the duplicate rule may spend",
    )
    b.add_argument(
        "--insecure-tls", action="store_true", help="A local test fixture only"
    )
    b.set_defaults(func=cmd_build)

    a = gp.add_parser(
        "approve", help="A person approves the review text, bound to the parts"
    )
    a.add_argument("parts")
    a.add_argument("--approved-by", required=True)
    a.set_defaults(func=cmd_approve)
