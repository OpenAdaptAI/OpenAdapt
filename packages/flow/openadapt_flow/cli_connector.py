"""Register the connector command family outside the main CLI entrypoint."""

from __future__ import annotations

import argparse
from typing import Callable


def register_connector_parser(
    sub: argparse._SubParsersAction,
    dispatch: Callable[[argparse.Namespace], int],
) -> None:
    # --- connector: the BYOC (bring-your-own-cloud) outbound-pull daemon -------
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
    pe.set_defaults(func=dispatch)

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
    pr.set_defaults(func=dispatch)

    from openadapt_flow.connector.windows_service import register_service_parser

    register_service_parser(csub)
