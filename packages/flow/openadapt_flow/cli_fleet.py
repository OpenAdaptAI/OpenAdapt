"""``openadapt-flow fleet``: local fleet drift patch settings.

Everything here acts on this tenant's fleet directory (``--fleet-dir``, or
``OPENADAPT_FLEET_DIR``, or ``~/.openadapt/fleet``):

* ``opt-in.json``: which app families or domains may export or import
  patches. Missing means off for every app;
* ``roles.json``: workflow authors and policy approvers (disjoint);
* ``promotion-policy.json``: the signed promotion policy;
* ``trust.json``: the pinned fleet roots.

No command here sends anything anywhere, imports a patch, or promotes one.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from openadapt_flow.fleet.optin import OPT_IN_FILE, FleetOptIn, default_fleet_dir
from openadapt_flow.fleet.policy import (
    POLICY_FILE,
    ROLES_FILE,
    FleetPromotionPolicy,
    PolicyVerificationError,
    RoleRegistry,
    SignedPromotionPolicy,
    sign_policy,
    verify_policy,
)
from openadapt_flow.fleet.signing import FleetTrust, SoftwareKeyCustody, public_key_b64

TRUST_FILE = "trust.json"


def _dir(args: argparse.Namespace) -> Path:
    return Path(args.fleet_dir).expanduser() if args.fleet_dir else default_fleet_dir()


def _directions(args: argparse.Namespace) -> list[str]:
    chosen = [d for d in ("export", "import") if getattr(args, d)]
    if not chosen:
        raise SystemExit("choose --export, --import, or both")
    return chosen


def _cmd_status(args: argparse.Namespace) -> int:
    root = _dir(args)
    opt_in = FleetOptIn.load(root / OPT_IN_FILE)
    roles = RoleRegistry.load(root / ROLES_FILE)
    report: dict = {"fleet_dir": str(root)}
    for direction in ("export", "import"):
        entries = opt_in.entries(direction)  # type: ignore[arg-type]
        report[direction] = [e.key for e in entries] or "off for every app"
    report["workflow_authors"] = list(roles.workflow_authors)
    report["policy_approvers"] = sorted(roles.policy_approvers)
    policy_path = root / POLICY_FILE
    if not policy_path.is_file():
        report["policy"] = "none: every imported patch waits for a person"
    else:
        try:
            policy = verify_policy(SignedPromotionPolicy.load(policy_path), roles)
        except (PolicyVerificationError, ValueError) as exc:
            report["policy"] = (
                f"does not verify ({exc}): every patch waits for a person"
            )
        else:
            report["policy"] = {
                "auto_promote": policy.auto_promote,
                "app_families": list(policy.app_families),
                "approved_by": policy.approved_by,
                "security_official": policy.security_official.official_name,
                "sha256": policy.policy_sha256(),
            }
    trust_path = root / TRUST_FILE
    report["trusted_roots"] = (
        sorted(FleetTrust.model_validate_json(trust_path.read_text()).roots)
        if trust_path.is_file()
        else "none: every patch is refused"
    )
    if args.as_json:
        print(json.dumps(report, indent=2))
        return 0
    for key, value in report.items():
        print(f"{key}: {value}")
    return 0


def _cmd_allow(args: argparse.Namespace, *, allow: bool) -> int:
    if bool(args.app_family) == bool(args.domain):
        raise SystemExit("name exactly one of --app-family or --domain")
    root = _dir(args)
    path = root / OPT_IN_FILE
    opt_in = FleetOptIn.load(path)
    for direction in _directions(args):
        if allow:
            if not args.admin:
                raise SystemExit("--admin is required: who is allowing this")
            opt_in = opt_in.allow(
                direction,  # type: ignore[arg-type]
                added_by=args.admin,
                app_family=args.app_family,
                domain=args.domain,
            )
        else:
            opt_in = opt_in.disallow(
                direction,  # type: ignore[arg-type]
                app_family=args.app_family,
                domain=args.domain,
            )
    opt_in.save(path)
    target = args.app_family or args.domain
    verb = "allowed" if allow else "removed"
    print(f"{verb} {' and '.join(_directions(args))} for {target} in {path}")
    return 0


def _load_private_key(path: str) -> SoftwareKeyCustody:
    key = serialization.load_pem_private_key(Path(path).read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise SystemExit("the policy key must be an Ed25519 private key")
    return SoftwareKeyCustody(key)


def _cmd_keygen(args: argparse.Namespace) -> int:
    out = Path(args.out)
    if out.exists():
        raise SystemExit(f"{out} exists; refusing to overwrite a key")
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(pem)
    print(public_key_b64(key))
    return 0


def _cmd_roles(args: argparse.Namespace) -> int:
    root = _dir(args)
    path = root / ROLES_FILE
    roles = RoleRegistry.load(path)
    authors = set(roles.workflow_authors)
    approvers = dict(roles.policy_approvers)
    if args.author:
        authors.add(args.author)
    if args.approver:
        if not args.public_key:
            raise SystemExit(
                "--approver needs --public-key (base64, from `fleet policy keygen`)"
            )
        approvers[args.approver] = args.public_key
    try:
        updated = RoleRegistry(
            workflow_authors=tuple(sorted(authors)), policy_approvers=approvers
        )
    except ValueError as exc:
        raise SystemExit(f"refused: {exc}") from exc
    updated.save(path)
    print(f"roles saved to {path}")
    return 0


def _cmd_policy_sign(args: argparse.Namespace) -> int:
    if not args.attest_security_official:
        raise SystemExit(
            "refused: pass --attest-security-official to state that your "
            "organisation's security official authorised this policy"
        )
    root = _dir(args)
    roles = RoleRegistry.load(root / ROLES_FILE)
    draft = json.loads(Path(args.draft).read_text())
    now = datetime.now(timezone.utc).isoformat()
    draft["approver"] = {
        "person_id": args.approver_id,
        "name": args.approver_name,
        "title": args.approver_title,
    }
    draft["security_official"] = {
        "official_name": args.security_official_name,
        "official_title": args.security_official_title,
        "authorised_at": args.authorised_at or now,
    }
    draft.setdefault("approved_at", now)
    try:
        policy = FleetPromotionPolicy.model_validate(draft)
    except ValueError as exc:
        raise SystemExit(f"refused: the policy draft is invalid: {exc}") from exc
    signed = sign_policy(policy, _load_private_key(args.key))
    try:
        verify_policy(signed, roles)
    except PolicyVerificationError as exc:
        raise SystemExit(f"refused: {exc}") from exc
    out = Path(args.out) if args.out else root / POLICY_FILE
    signed.save(out)
    print(f"signed policy {policy.policy_sha256()[:16]} written to {out}")
    return 0


def _cmd_policy_verify(args: argparse.Namespace) -> int:
    root = _dir(args)
    path = Path(args.policy) if args.policy else root / POLICY_FILE
    try:
        policy = verify_policy(
            SignedPromotionPolicy.load(path), RoleRegistry.load(root / ROLES_FILE)
        )
    except (PolicyVerificationError, ValueError, OSError) as exc:
        print(f"policy does not verify: {exc}", file=sys.stderr)
        return 1
    print(
        f"policy {policy.policy_sha256()[:16]} verifies: approved by "
        f"{policy.approved_by}; authorised by security official "
        f"{policy.security_official.official_name}; auto_promote="
        f"{str(policy.auto_promote).lower()}"
    )
    return 0


def _cmd_simulate(args: argparse.Namespace) -> int:
    from openadapt_flow.fleet.simulator import main

    return main(["--out", args.out])


def cmd_fleet(args: argparse.Namespace) -> int:
    handlers = {
        "status": _cmd_status,
        "allow": lambda a: _cmd_allow(a, allow=True),
        "disallow": lambda a: _cmd_allow(a, allow=False),
        "roles": _cmd_roles,
        "simulate": _cmd_simulate,
    }
    if args.fleet_cmd == "policy":
        return {
            "sign": _cmd_policy_sign,
            "verify": _cmd_policy_verify,
            "keygen": _cmd_keygen,
        }[args.policy_cmd](args)
    return handlers[args.fleet_cmd](args)


def register_fleet_parser(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser(
        "fleet",
        help=(
            "Fleet drift patch settings: opt-in per app family, roles, and the "
            "signed promotion policy. Off for every app by default."
        ),
    )
    parser.add_argument(
        "--fleet-dir",
        default=None,
        help="Fleet settings directory (default: $OPENADAPT_FLEET_DIR or ~/.openadapt/fleet)",
    )
    verbs = parser.add_subparsers(dest="fleet_cmd", required=True)

    status = verbs.add_parser("status", help="Show opt-in, roles, policy and trust")
    status.add_argument("--json", action="store_true", dest="as_json")

    for name, text in (
        ("allow", "Allow patch export and/or import for an app family or domain"),
        ("disallow", "Remove an app family or domain from the allowlist"),
    ):
        p = verbs.add_parser(name, help=text)
        p.add_argument("--export", action="store_true", help="Patch export")
        p.add_argument(
            "--import", action="store_true", dest="import", help="Patch import"
        )
        p.add_argument("--app-family", help="e.g. availity/payer-portal")
        p.add_argument(
            "--domain", help="e.g. availity.com, matched against the app host"
        )
        if name == "allow":
            p.add_argument("--admin", help="Name of the admin making this change")

    roles = verbs.add_parser(
        "roles", help="Add a workflow author or a policy approver (never both)"
    )
    roles.add_argument("--author", help="Person id of a workflow author")
    roles.add_argument("--approver", help="Person id of a policy approver")
    roles.add_argument("--public-key", help="Approver's base64 Ed25519 public key")

    policy = verbs.add_parser("policy", help="Sign or verify the promotion policy")
    psub = policy.add_subparsers(dest="policy_cmd", required=True)
    keygen = psub.add_parser("keygen", help="Create a policy approver key (PEM, 0600)")
    keygen.add_argument("--out", required=True)
    sign = psub.add_parser("sign", help="Sign a policy draft as a policy approver")
    sign.add_argument(
        "--draft", required=True, help="JSON with auto_promote, app_families, ..."
    )
    sign.add_argument(
        "--key", required=True, help="Approver's Ed25519 private key (PEM)"
    )
    sign.add_argument("--approver-id", required=True)
    sign.add_argument("--approver-name", required=True)
    sign.add_argument("--approver-title", required=True)
    sign.add_argument("--security-official-name", required=True)
    sign.add_argument("--security-official-title", required=True)
    sign.add_argument(
        "--authorised-at", help="When the security official authorised it (ISO 8601)"
    )
    sign.add_argument(
        "--attest-security-official",
        action="store_true",
        help="State that your organisation's security official authorised this policy",
    )
    sign.add_argument("--out", help=f"Output path (default: <fleet dir>/{POLICY_FILE})")
    verify = psub.add_parser(
        "verify", help="Verify the signed policy against roles.json"
    )
    verify.add_argument("--policy", help=f"Default: <fleet dir>/{POLICY_FILE}")

    simulate = verbs.add_parser("simulate", help="Run the three-tenant local simulator")
    simulate.add_argument("--out", required=True)

    parser.set_defaults(func=cmd_fleet)


__all__ = ["cmd_fleet", "register_fleet_parser"]
