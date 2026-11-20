"""Connector CLI registration and handlers; governed execution stays shared."""

from __future__ import annotations

import argparse
from typing import Callable


def register_connector_parser(
    sub: argparse._SubParsersAction, command: Callable[[argparse.Namespace], int]
) -> None:
    """Register the existing connector commands with the caller's callback."""
    p = sub.add_parser(
        "connector",
        help=(
            "BYOC connector: enroll this machine with OpenAdapt Cloud and run "
            "governed jobs LOCALLY (customer owns the data boundary; PHI stays "
            "on this side)"
        ),
        allow_abbrev=False,
    )
    csub = p.add_subparsers(dest="connector_cmd", required=True)

    pe = csub.add_parser(
        "enroll",
        help="Enroll against a mock/development control plane and persist its token",
        allow_abbrev=False,
    )
    pe.add_argument(
        "--control-plane-url", help="Control plane base URL (or CONTROL_PLANE_URL)"
    )
    pe.add_argument(
        "--enrollment-secret", help="Org enrollment secret (or BYOC_ENROLLMENT_SECRET)"
    )
    pe.add_argument("--org-id", help="Org id to enroll under (or BYOC_ORG_ID)")
    pe.add_argument("--name", help="Connector name (or BYOC_CONNECTOR_NAME)")
    pe.add_argument(
        "--profile", help="Path to the deployment.yaml governed runs use as --config"
    )
    pe.add_argument(
        "--policy", help="Pinned admitted policy name (else the deployment config's)"
    )
    pe.add_argument("--storage-backend", help="Customer storage backend (local)")
    pe.add_argument(
        "--storage-root",
        help="Customer storage root dir (a full-disk-encrypted volume)",
    )
    pe.set_defaults(func=command)

    pr = csub.add_parser(
        "run",
        help="Poll the control plane and run governed jobs until interrupted",
        allow_abbrev=False,
    )
    pr.add_argument(
        "--control-plane-url", help="Control plane base URL (or CONTROL_PLANE_URL)"
    )
    pr.add_argument("--token", help="Per-connector token (or BYOC_CONNECTOR_TOKEN)")
    pr.add_argument("--org-id", help="Org id (or BYOC_ORG_ID)")
    pr.add_argument(
        "--profile", help="Path to the deployment.yaml governed runs use as --config"
    )
    pr.add_argument(
        "--policy", help="Pinned admitted policy name (else the deployment config's)"
    )
    pr.add_argument("--storage-backend", help="Customer storage backend (local)")
    pr.add_argument("--storage-root", help="Customer storage root dir")
    pr.add_argument(
        "--poll-wait", type=int, help="Long-poll wait seconds (or BYOC_POLL_WAIT_S)"
    )
    pr.add_argument(
        "--once", action="store_true", help="Poll once then exit (cron-style)"
    )
    pr.add_argument(
        "--secrets-dir",
        help="Local secret store: <dir>/<secrets_ref>.env, mode 0600 (or BYOC_SECRETS_DIR)",
    )
    pr.add_argument(
        "--trusted-admission-keys",
        help=(
            "Comma-separated Ed25519 public keys of the program admitters this "
            "runner trusts (or BYOC_TRUSTED_ADMISSION_KEYS)"
        ),
    )
    pr.add_argument(
        "--default-secrets-ref",
        help="Local secret reference used when a job names none",
    )
    pr.add_argument(
        "--admission-key-dir",
        help="Customer-owned Flow admission key directory (public keys only)",
    )
    pr.add_argument(
        "--egress-audit-log",
        help="Append every request body sent to this local JSONL file",
    )
    pr.add_argument(
        "--max-workers", type=int, help="Local ceiling on parallel jobs (1-16)"
    )
    pr.set_defaults(func=command)

    pt = csub.add_parser(
        "rotate-token",
        help="Swap this connector's token now and persist the new one (0600)",
        allow_abbrev=False,
    )
    pt.add_argument(
        "--control-plane-url", help="Control plane base URL (or CONTROL_PLANE_URL)"
    )
    pt.set_defaults(func=command)


def _connector_flags(args: argparse.Namespace) -> dict[str, object]:
    """Collect the connector CLI flags into the settings-resolution dict."""
    keys = (
        "control_plane_url",
        "token",
        "org_id",
        "name",
        "profile",
        "policy",
        "storage_backend",
        "storage_root",
        "poll_wait",
        "secrets_dir",
        "trusted_admission_keys",
        "egress_audit_log",
        "max_workers",
        "admission_key_dir",
        "default_secrets_ref",
    )
    return {
        k: getattr(args, k, None) for k in keys if getattr(args, k, None) is not None
    }


def _cmd_connector(args: argparse.Namespace) -> int:
    """The BYOC connector: enroll this machine, or run governed jobs locally.

    Both sub-verbs are thin wrappers over :mod:`openadapt_flow.connector`. The
    engine ships the connector so a dispatched run is the SAME fail-closed
    ``openadapt-flow run`` admission gate + governed Replayer the local CLI uses,
    executed inside the customer perimeter against the customer's own storage.
    """
    import os

    from openadapt_flow.connector import (
        ConnectorClient,
        ConnectorClientError,
        load_settings,
        run_loop,
        save_enrollment,
    )
    from openadapt_flow.connector.config import ConnectorConfigError
    from openadapt_flow.connector.executor import _subprocess_runner

    verb = getattr(args, "connector_cmd", None)
    try:
        settings = load_settings(_connector_flags(args))
    except ConnectorConfigError as exc:
        print(f"connector: {exc}")
        return 2

    if verb == "enroll":
        enrollment_secret = getattr(args, "enrollment_secret", None) or os.environ.get(
            "BYOC_ENROLLMENT_SECRET"
        )
        client = ConnectorClient(settings.control_plane_url)
        try:
            data = client.enroll(
                enrollment_secret=enrollment_secret,
                org_id=settings.org_id,
                name=settings.name,
            )
        except ConnectorClientError as exc:
            print(f"connector enroll failed: {exc}")
            return 1
        finally:
            client.close()
        settings.token = data.get("token") or settings.token
        settings.org_id = data.get("org_id") or settings.org_id
        path = save_enrollment(settings)
        print(
            f"enrolled connector {data.get('connector_id', '?')} for org "
            f"{settings.org_id}; token persisted to {path} (0600)."
        )
        return 0

    from openadapt_flow.connector.config import connector_config_path

    def persist_token(new_token: str) -> None:
        settings.token = new_token
        settings.pending_token = None
        save_enrollment(settings)

    def persist_pending(new_token: str) -> None:
        settings.pending_token = new_token
        save_enrollment(settings)

    can_persist = connector_config_path().is_file()

    if verb == "rotate-token":
        if not settings.token or not can_persist:
            print(
                "connector rotate-token: needs an enrolled connector.toml "
                f"({connector_config_path()}) holding the current token."
            )
            return 2
        from openadapt_flow.connector.daemon import finish_rotation, maybe_rotate

        client = ConnectorClient(settings.control_plane_url, token=settings.token)
        try:
            if settings.pending_token:
                ok = finish_rotation(
                    client, settings.pending_token, persist_token, print
                )
            else:
                ok = maybe_rotate(
                    client,
                    persist_token,
                    print,
                    persist_pending=persist_pending,
                    force=True,
                )
        finally:
            client.close()
        if not ok:
            print("connector rotate-token failed; the current token still works.")
            return 1
        print(
            "token rotated and persisted (0600); the previous token stops within 5 minutes."
        )
        return 0

    # verb == "run"
    if not settings.token:
        print(
            "connector run: no token. Create an organization connector in "
            "OpenAdapt Cloud → Settings, then pass --token or set "
            "BYOC_CONNECTOR_TOKEN."
        )
        return 2
    client = ConnectorClient(
        settings.control_plane_url,
        token=settings.token,
        egress_audit_log=settings.egress_audit_log,
    )
    try:
        if settings.pending_token and can_persist:
            from openadapt_flow.connector.daemon import finish_rotation

            finish_rotation(client, settings.pending_token, persist_token, print)
        return run_loop(
            client,
            settings,
            runner=_subprocess_runner,
            once=bool(getattr(args, "once", False)),
            persist_token=persist_token if can_persist else None,
            persist_pending=persist_pending if can_persist else None,
        )
    finally:
        client.close()
