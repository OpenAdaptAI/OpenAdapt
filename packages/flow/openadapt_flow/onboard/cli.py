"""CLI entry point for ``openadapt onboard`` (single-command pack onboarding)."""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path
from typing import Any

import yaml

from openadapt_flow.onboard.types import CleanWriteCase, OnboardSpec


def _load_clean_writes(path: str | Path) -> list[CleanWriteCase]:
    p = Path(path)
    raw = p.read_text(encoding="utf-8")
    data = json.loads(raw) if p.suffix.lower() == ".json" else yaml.safe_load(raw)
    if not isinstance(data, list):
        raise ValueError(f"{p} must contain a list of clean write cases")
    cases: list[CleanWriteCase] = []
    for idx, item in enumerate(data):
        if not isinstance(item, dict):
            raise ValueError(f"{p}[{idx}] must be a mapping")
        kind = str(item["kind"])
        values = dict(item.get("values") or {})
        name = str(item.get("name") or f"{kind}#{idx + 1}")
        http_cfg = item.get("http")
        if not isinstance(http_cfg, dict) or "url" not in http_cfg:
            raise ValueError(f"{p}[{idx}] requires an 'http' mapping with 'url'")
        url = str(http_cfg["url"])
        method = str(http_cfg.get("method") or "POST").upper()
        body = http_cfg.get("body", values)

        def _make_writer(u: str = url, m: str = method, b: Any = body) -> Any:
            def _write() -> None:
                encoded = json.dumps(b).encode("utf-8")
                req = urllib.request.Request(
                    u,
                    data=encoded,
                    method=m,
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    resp.read()

            return _write

        cases.append(
            CleanWriteCase(
                kind=kind,
                values=values,
                apply_write=_make_writer(),
                name=name,
            )
        )
    return cases


def _cmd_onboard(args: argparse.Namespace) -> int:
    from openadapt_flow.onboard.engine import onboard_pack, synthesize_pack

    try:
        spec = OnboardSpec.load(args.spec)
    except (OSError, ValueError, KeyError) as exc:
        print(f"REFUSED: invalid onboard spec: {exc}", file=sys.stderr)
        return 2

    out_dir = Path(args.out)
    if args.dry_run:
        manifest_path, digest = synthesize_pack(spec, out_dir)
        payload = {
            "pack_name": spec.name,
            "certified": False,
            "dry_run": True,
            "manifest_path": str(manifest_path),
            "pack_digest": digest,
        }
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            print(
                f"Synthesized pack {spec.name!r} at {manifest_path} "
                f"(digest {digest}; dry-run, not certified)"
            )
        return 0

    clean_cases: list[CleanWriteCase] = []
    if args.clean_writes:
        try:
            clean_cases = _load_clean_writes(args.clean_writes)
        except (OSError, ValueError, KeyError) as exc:
            print(f"REFUSED: invalid clean-writes file: {exc}", file=sys.stderr)
            return 2

    result = onboard_pack(spec, out_dir, clean_cases)
    if args.json:
        print(json.dumps(result.to_dict(), indent=2))
    else:
        status = "CERTIFIED" if result.certified else "REFUSED"
        print(
            f"{status}: pack={result.pack_name!r} "
            f"planted_faults={result.planted_faults_caught}/{result.planted_faults_total} "
            f"clean_writes={result.clean_writes_verified}/{result.clean_writes_total} "
            f"receipts={result.receipts_issued} "
            f"false_stop_rate={result.false_stop_rate:.1%} "
            f"digest={result.pack_digest}"
        )
        for reason in result.refusal_reasons:
            print(f"  refusal: {reason}", file=sys.stderr)
    return 0 if result.certified else 2


def register(subparsers: Any) -> None:
    """Register the ``onboard`` subcommand on the Flow root parser."""

    p = subparsers.add_parser(
        "onboard",
        help="Synthesize and planted-fault-qualify a verification pack for any system of record",
        allow_abbrev=False,
    )
    p.add_argument(
        "--spec",
        required=True,
        help="Path to declarative onboard spec (YAML or JSON)",
    )
    p.add_argument(
        "--out",
        required=True,
        help="Output directory for synthesized pack, contracts, and QUALIFICATION.json",
    )
    p.add_argument(
        "--clean-writes",
        default=None,
        help="Optional YAML/JSON list of clean write cases (with HTTP target) to verify and receipt",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="Emit structured QualificationResult JSON to stdout",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Synthesize the pack without running qualification (pack is not certified)",
    )
    p.set_defaults(func=_cmd_onboard)
