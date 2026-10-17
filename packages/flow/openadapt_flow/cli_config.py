"""Generate a deployment YAML bound to one sealed workflow bundle.

The copyable certify/run path needs a ``deploy.yaml``. This command writes a
schema-valid draft that ``load_deployment`` refuses until the operator fills
every unresolved field. Secrets are env var names, never literals.

Invoked as ``python -m openadapt_flow.cli_config init BUNDLE --out deploy.yaml``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from openadapt_flow.deployment import UNRESOLVED, load_deployment

BACKENDS = ("web", "windows", "macos", "linux", "rdp", "citrix")

_HEADER = """\
# Draft from `python -m openadapt_flow.cli_config init`.
# Fill every path in `unresolved`, then delete that list.
# Do not put secrets in this file. Use env var names.
#
#   python -m openadapt_flow.cli_config init bundle --out deploy.yaml
#   # review and complete deploy.yaml
#   openadapt-flow certify bundle --config deploy.yaml
#   openadapt-flow run bundle --profile standard --config deploy.yaml
#
# certify and run call load_deployment, which refuses a non-empty unresolved
# list and any field still set to UNRESOLVED.
"""


def _require_bundle(bundle: Path) -> Path:
    payload = bundle / "workflow.json"
    if not payload.is_file():
        raise SystemExit(f"{bundle} is not a workflow bundle (no workflow.json)")
    return payload


def bundle_digest(bundle: Path) -> str:
    """Return the sealed content digest, or a SHA-256 of workflow.json."""

    _require_bundle(bundle)
    manifest_path = bundle / "manifest.json"
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text())
        except json.JSONDecodeError as exc:
            raise SystemExit(f"{manifest_path} is not valid JSON: {exc}") from exc
        digest = manifest.get("content_digest")
        if isinstance(digest, str) and digest.strip():
            return digest.strip()
    return hashlib.sha256((bundle / "workflow.json").read_bytes()).hexdigest()


def bundle_surface(bundle: Path) -> Optional[str]:
    """Return the workflow surface when it is a known backend kind."""

    payload = _require_bundle(bundle)
    try:
        raw = json.loads(payload.read_text())
    except json.JSONDecodeError as exc:
        raise SystemExit(f"{payload} is not valid JSON: {exc}") from exc
    surface = raw.get("surface")
    if surface in BACKENDS:
        return surface
    return None


def bundle_name(bundle: Path) -> str:
    payload = _require_bundle(bundle)
    try:
        raw = json.loads(payload.read_text())
    except json.JSONDecodeError:
        return bundle.name
    name = raw.get("name")
    if isinstance(name, str) and name.strip():
        return name.strip()
    return bundle.name


def _backend_block(kind: str) -> tuple[dict[str, Any], list[str]]:
    unresolved: list[str] = []
    backend: dict[str, Any] = {"kind": kind}
    if kind == "web":
        backend["url"] = UNRESOLVED
        backend["headed"] = False
        unresolved.append("backend.url")
    elif kind == "windows":
        backend["agent_url"] = UNRESOLVED
        unresolved.append("backend.agent_url")
    elif kind == "macos":
        backend["macos_app"] = UNRESOLVED
        backend["macos_window_title"] = UNRESOLVED
        unresolved.extend(["backend.macos_app", "backend.macos_window_title"])
    elif kind == "linux":
        backend["linux_app"] = UNRESOLVED
        backend["linux_window_title"] = UNRESOLVED
        backend["linux_allow_physical_input"] = False
        unresolved.extend(["backend.linux_app", "backend.linux_window_title"])
    elif kind == "rdp":
        backend["rdp_host"] = UNRESOLVED
        backend["rdp_username"] = UNRESOLVED
        backend["rdp_port"] = 3389
        unresolved.extend(["backend.rdp_host", "backend.rdp_username"])
    elif kind == "citrix":
        backend["rdp_window"] = UNRESOLVED
        backend["rdp_window_title"] = UNRESOLVED
        unresolved.extend(["backend.rdp_window", "backend.rdp_window_title"])
    else:
        raise SystemExit(f"unknown backend {kind!r}")
    return backend, unresolved


def build_draft(
    bundle: Path,
    *,
    backend: Optional[str] = None,
) -> dict[str, Any]:
    """Return a schema-valid draft mapping bound to ``bundle``."""

    kind = backend or bundle_surface(bundle) or "web"
    if kind not in BACKENDS:
        raise SystemExit(f"unknown backend {kind!r}")
    backend_block, unresolved = _backend_block(kind)
    unresolved.extend(
        [
            "effects.kind",
            "effects.base_url",
            "policy.policy",
            "identity.record_id_env",
            "idempotency.key_env",
        ]
    )
    return {
        "name": bundle_name(bundle),
        "bundle_digest": bundle_digest(bundle),
        "unresolved": unresolved,
        "backend": backend_block,
        "actuation": {"api": False, "base_url": "", "timeout_s": 5.0},
        "effects": {
            "kind": "none",
            "base_url": UNRESOLVED,
            "auth": {
                "bearer_env": "OPENADAPT_SOR_BEARER_TOKEN",
            },
        },
        "identity": {
            "require_fresh_frame": True,
            "record_id_env": "OPENADAPT_RECORD_ID",
        },
        "idempotency": {"key_env": "OPENADAPT_IDEMPOTENCY_KEY"},
        "runtime": {
            "profile": "standard",
            "durable": True,
            "require_settled": True,
            "allow_model_grounding": False,
        },
        "policy": {"policy": "clinical-write"},
    }


def render_draft(data: Mapping[str, Any]) -> str:
    import yaml

    body = yaml.safe_dump(
        dict(data),
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
    )
    return _HEADER + "\n" + body


def init_deploy_config(
    bundle: Path,
    out: Path,
    *,
    backend: Optional[str] = None,
) -> Path:
    """Write a draft deployment YAML and return ``out``."""

    bundle = bundle.resolve()
    out = out.resolve()
    if out.exists():
        raise SystemExit(f"--out {out} already exists; refuse to overwrite")
    draft = build_draft(bundle, backend=backend)
    text = render_draft(draft)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text)
    # Draft must parse, and must be refused by the governed loader.
    parsed = load_deployment(out, allow_unresolved=True)
    if parsed.bundle_digest != draft["bundle_digest"]:
        raise SystemExit("internal error: written digest does not round-trip")
    try:
        load_deployment(out)
    except ValueError:
        pass
    else:
        raise SystemExit("internal error: draft loaded as complete")
    return out


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m openadapt_flow.cli_config",
        description=(
            "Write a deployment YAML bound to one workflow bundle. "
            "certify and run refuse the draft until unresolved fields are filled."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser(
        "init",
        help="Write a draft deploy.yaml for a workflow bundle",
    )
    init.add_argument("bundle", help="Workflow bundle directory")
    init.add_argument(
        "--out",
        required=True,
        help="Destination YAML path (must not already exist)",
    )
    init.add_argument(
        "--backend",
        choices=BACKENDS,
        default=None,
        help="Execution backend (default: the bundle surface, else web)",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.command != "init":
        parser.error(f"unknown command {args.command!r}")
    path = init_deploy_config(
        Path(args.bundle),
        Path(args.out),
        backend=args.backend,
    )
    sys.stdout.write(
        f"Wrote draft {path}. Fill unresolved fields before certify or run.\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
