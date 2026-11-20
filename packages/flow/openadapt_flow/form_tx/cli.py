"""Command-line interface for OpenAdapt Governed Form & Wizard Transactions (form_tx).

Supports deterministic pack induction from structural snapshots, deterministic
pack inspection, and UI profile discovery. Zero application or vendor names.
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from pathlib import Path
from typing import Optional, Sequence

from pydantic import ValidationError

from openadapt_flow.form_tx.pack_compiler import (
    CanonicalDomainSchema,
    build_patient_registration_domain_schema,
    build_property_quote_domain_schema,
    induce_form_pack,
)
from openadapt_flow.form_tx.types import FormPack, FormSnapshot, UIProfileSpec

BUILTIN_UI_PROFILES: dict[str, UIProfileSpec] = {
    "standard_web_form": UIProfileSpec(
        profile_id="standard_web_form",
        description="Standard HTML form with native controls and synchronous validation.",
        authenticated_landmark_selectors=(
            "[data-authenticated-shell]",
            "nav[aria-label]",
        ),
        debounce_grace_ms=50,
        settle_window_ms=80,
        max_settle_ms=5000,
    ),
    "async_spa_portal": UIProfileSpec(
        profile_id="async_spa_portal",
        description=(
            "Asynchronous single-page portal with detached comboboxes, debounced "
            "field validation, and ARIA busy indicators."
        ),
        authenticated_landmark_selectors=(
            "[data-authenticated-shell]",
            "[data-user-menu]",
        ),
        debounce_grace_ms=180,
        settle_window_ms=200,
        max_settle_ms=15000,
    ),
    "server_postback_wizard": UIProfileSpec(
        profile_id="server_postback_wizard",
        description=(
            "Multi-screen enterprise wizard with partial-DOM server postback updates, "
            "validation alert banners, and read-only session keepalive endpoint."
        ),
        authenticated_landmark_selectors=(
            "[data-workspace-shell]",
            "#authenticated-nav",
        ),
        keepalive_path="/api/session/keepalive",
        debounce_grace_ms=200,
        settle_window_ms=250,
        max_settle_ms=20000,
    ),
    "modal_heavy_intake": UIProfileSpec(
        profile_id="modal_heavy_intake",
        description=(
            "Administrative or clinical intake portal with duplicate-check modal "
            "dialogs and inline field error banners."
        ),
        authenticated_landmark_selectors=(
            "[data-intake-workspace]",
            "[data-session-active='true']",
        ),
        debounce_grace_ms=120,
        settle_window_ms=150,
        max_settle_ms=12000,
    ),
}


def discover_ui_profiles(
    profiles_dir: Optional[Path] = None,
) -> tuple[UIProfileSpec, ...]:
    """Return deterministically ordered ``UIProfileSpec`` entries."""
    registry: dict[str, UIProfileSpec] = dict(BUILTIN_UI_PROFILES)

    for module_name in (
        "openadapt_flow.form_tx.profiles",
        "openadapt_flow.form_tx.ui_profiles",
    ):
        try:
            mod = importlib.import_module(module_name)
        except ImportError:
            continue
        for attr_val in vars(mod).values():
            if isinstance(attr_val, UIProfileSpec):
                registry[attr_val.profile_id] = attr_val
            elif isinstance(attr_val, dict):
                for item in attr_val.values():
                    if isinstance(item, UIProfileSpec):
                        registry[item.profile_id] = item
            elif isinstance(attr_val, (list, tuple)):
                for item in attr_val:
                    if isinstance(item, UIProfileSpec):
                        registry[item.profile_id] = item

    if profiles_dir is not None and profiles_dir.is_dir():
        for json_path in sorted(profiles_dir.glob("*.json")):
            raw = json.loads(json_path.read_text(encoding="utf-8"))
            spec = UIProfileSpec.model_validate(raw)
            registry[spec.profile_id] = spec

    return tuple(registry[k] for k in sorted(registry))


def _load_domain_schema(domain_arg: str) -> CanonicalDomainSchema:
    key = domain_arg.strip()
    if key == "patient_registration":
        return build_patient_registration_domain_schema()
    if key == "property_quote":
        return build_property_quote_domain_schema()
    domain_path = Path(key)
    if not domain_path.is_file():
        raise FileNotFoundError(
            f"Unknown domain preset or missing schema JSON file: {domain_arg!r}"
        )
    raw = json.loads(domain_path.read_text(encoding="utf-8"))
    return CanonicalDomainSchema.model_validate(raw)


def _load_snapshots(snapshot_paths: Sequence[str]) -> list[FormSnapshot]:
    snapshots: list[FormSnapshot] = []
    for raw_path in snapshot_paths:
        path = Path(raw_path)
        if not path.is_file():
            raise FileNotFoundError(f"Snapshot file not found: {raw_path!r}")
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, list):
            for entry in payload:
                snapshots.append(FormSnapshot.model_validate(entry))
        else:
            snapshots.append(FormSnapshot.model_validate(payload))
    if not snapshots:
        raise ValueError("At least one FormSnapshot is required for induce-pack")
    return snapshots


def _cmd_induce_pack(args: argparse.Namespace) -> int:
    domain = _load_domain_schema(args.domain)
    form_snapshots = _load_snapshots(args.snapshots)
    summary_snapshot: Optional[FormSnapshot] = None
    if args.summary_snapshot:
        summary_loaded = _load_snapshots([args.summary_snapshot])
        if len(summary_loaded) != 1:
            raise ValueError("--summary-snapshot must contain exactly one FormSnapshot")
        summary_snapshot = summary_loaded[0]

    report = induce_form_pack(
        pack_id=args.pack_id,
        ui_profile_id=args.ui_profile,
        domain=domain,
        form_snapshots=form_snapshots,
        summary_snapshot=summary_snapshot,
    )

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(report.pack.model_dump(mode="json"), indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )

    if args.report_out:
        report_path = Path(args.report_out)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(report.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    print(f"pack_id: {report.pack.pack_id}")
    print(f"domain_schema: {report.pack.domain_schema}")
    print(f"ui_profile_id: {report.pack.ui_profile_id}")
    print(f"digest: {report.pack.digest()}")
    print(f"certified_ready: {report.certified_ready}")
    print(
        f"matched_keys ({len(report.matched_keys)}): {', '.join(report.matched_keys) or 'none'}"
    )
    if report.unmatched_required_keys:
        print(
            f"unmatched_required_keys ({len(report.unmatched_required_keys)}): "
            f"{', '.join(report.unmatched_required_keys)}"
        )
    if report.ambiguous_controls:
        print(
            f"ambiguous_controls ({len(report.ambiguous_controls)}): "
            f"{'; '.join(report.ambiguous_controls)}"
        )
    if report.unmapped_portal_required_labels:
        print(
            f"unmapped_portal_required_labels ({len(report.unmapped_portal_required_labels)}): "
            f"{', '.join(report.unmapped_portal_required_labels)}"
        )

    if args.strict and not report.certified_ready:
        return 1
    return 0


def _cmd_inspect_pack(args: argparse.Namespace) -> int:
    pack_path = Path(args.pack_json)
    if not pack_path.is_file():
        raise FileNotFoundError(f"FormPack JSON file not found: {args.pack_json!r}")
    raw = json.loads(pack_path.read_text(encoding="utf-8"))
    pack = FormPack.model_validate(raw)
    digest = pack.digest()

    outputs = pack.summary_check.outputs if pack.summary_check is not None else ()
    readbacks = (
        pack.summary_check.field_readbacks if pack.summary_check is not None else ()
    )

    if args.json:
        summary_payload = {
            "pack_id": pack.pack_id,
            "domain_schema": pack.domain_schema,
            "ui_profile_id": pack.ui_profile_id,
            "digest": digest,
            "screens": [s.screen_id for s in pack.screens],
            "bindings": [
                {
                    "binding_id": b.binding_id,
                    "dossier_key": b.dossier_key,
                    "control_kind": b.control_kind.value,
                    "codec": b.codec.kind.value,
                }
                for b in pack.bindings
            ],
            "subroutines": [s.subroutine_id for s in pack.popup_subroutines],
            "outputs": [o.output_key for o in outputs],
            "field_readbacks": [r.dossier_key for r in readbacks],
        }
        print(json.dumps(summary_payload, indent=2, sort_keys=True))
        return 0

    print(f"pack_id: {pack.pack_id}")
    print(f"domain_schema: {pack.domain_schema}")
    print(f"ui_profile_id: {pack.ui_profile_id}")
    print(f"digest: {digest}")

    print(f"screens ({len(pack.screens)}):")
    for scr in pack.screens:
        flags = []
        if scr.next_button_anchor is not None:
            flags.append("next")
        if scr.is_commit_screen:
            flags.append("commit")
        flag_str = f" [{', '.join(flags)}]" if flags else ""
        print(f"  - {scr.screen_id}{flag_str}")

    print(f"bindings ({len(pack.bindings)}):")
    for bnd in pack.bindings:
        req = "required" if bnd.required_in_pack else "optional"
        print(
            f"  - {bnd.binding_id}: {bnd.dossier_key} "
            f"(kind={bnd.control_kind.value}, codec={bnd.codec.kind.value}, {req})"
        )

    print(f"subroutines ({len(pack.popup_subroutines)}):")
    for sub in pack.popup_subroutines:
        print(
            f"  - {sub.subroutine_id} -> {sub.return_output_key} "
            f"(codec={sub.return_codec.kind.value})"
        )

    print(f"outputs ({len(outputs)}):")
    for out in outputs:
        req = "required" if out.required else "optional"
        print(f"  - {out.output_key} (codec={out.codec.kind.value}, {req})")

    if readbacks:
        print(f"field_readbacks ({len(readbacks)}):")
        for rb in readbacks:
            print(f"  - {rb.dossier_key} (codec={rb.codec.kind.value})")

    return 0


def _cmd_list_profiles(args: argparse.Namespace) -> int:
    profiles_dir = Path(args.profiles_dir) if args.profiles_dir else None
    profiles = discover_ui_profiles(profiles_dir=profiles_dir)

    if args.json:
        print(
            json.dumps(
                [p.model_dump(mode="json") for p in profiles],
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    print(f"ui_profiles ({len(profiles)}):")
    for prof in profiles:
        keepalive = prof.keepalive_path or "landmark_snapshot"
        print(
            f"  - {prof.profile_id}: {prof.description} "
            f"[settle={prof.settle_window_ms}ms, keepalive={keepalive}]"
        )
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Build the ``argparse.ArgumentParser`` for ``form_tx`` CLI operations."""
    parser = argparse.ArgumentParser(
        prog="openadapt-form-tx",
        description="Governed Form & Wizard Transaction pack compiler and inspector.",
    )
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    induce = subparsers.add_parser(
        "induce-pack",
        help="Induce a declarative FormPack from structural form snapshots.",
    )
    induce.add_argument(
        "--domain",
        required=True,
        help="Domain preset ('patient_registration', 'property_quote') or path to CanonicalDomainSchema JSON.",
    )
    induce.add_argument(
        "--snapshots",
        nargs="+",
        required=True,
        help="One or more FormSnapshot JSON files.",
    )
    induce.add_argument(
        "--summary-snapshot",
        default=None,
        help="Optional summary screen FormSnapshot JSON file.",
    )
    induce.add_argument(
        "--ui-profile",
        required=True,
        help="Target UIProfileSpec profile_id.",
    )
    induce.add_argument(
        "--pack-id",
        required=True,
        help="Identifier for the induced FormPack.",
    )
    induce.add_argument(
        "--out",
        required=True,
        help="Output path for the induced FormPack JSON.",
    )
    induce.add_argument(
        "--report-out",
        default=None,
        help="Optional output path for the full PackInductionReport JSON.",
    )
    induce.add_argument(
        "--strict",
        action="store_true",
        help="Exit non-zero when report.certified_ready is False.",
    )
    induce.set_defaults(handler=_cmd_induce_pack)

    inspect_p = subparsers.add_parser(
        "inspect-pack",
        help="Print deterministic summary and digest of a FormPack JSON file.",
    )
    inspect_p.add_argument(
        "pack_json",
        help="Path to the FormPack JSON file.",
    )
    inspect_p.add_argument(
        "--json",
        action="store_true",
        help="Emit summary as canonical JSON.",
    )
    inspect_p.set_defaults(handler=_cmd_inspect_pack)

    profiles_p = subparsers.add_parser(
        "list-profiles",
        help="List available UIProfileSpec definitions.",
    )
    profiles_p.add_argument(
        "--profiles-dir",
        default=None,
        help="Optional directory containing custom UIProfileSpec JSON files.",
    )
    profiles_p.add_argument(
        "--json",
        action="store_true",
        help="Emit UIProfileSpec entries as canonical JSON.",
    )
    profiles_p.set_defaults(handler=_cmd_list_profiles)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Entry point for ``openadapt_flow.form_tx.cli``."""
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except (
        FileNotFoundError,
        ValueError,
        ValidationError,
        json.JSONDecodeError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
