"""Engine-side BYOC (bring-your-own-cloud) Connector.

A real, runnable outbound-pull daemon that lets the OpenAdapt Cloud control plane
MANAGE governed runs that EXECUTE inside the CUSTOMER'S own environment (their
VM / VPC / on-prem). The customer owns the Windows licensing and the data
boundary: the PHI-bearing bundle and report bytes stay in their storage, and only
PHI-free status/halt metadata ever flows back to the control plane.

Loop (all outbound HTTPS; zero inbound ports):
    register -> POST /api/connector/register   (enroll once, get a token)
    poll     -> POST /api/connector/poll        (long-poll; lease the next job)
    execute  -> the governed ``openadapt-flow run`` admission gate + Replayer
                (identity gates + effect verification + halt-don't-guess intact),
                against the CUSTOMER'S own storage
    callback -> POST /api/internal/run-callback (PHI-free status/metrics)
    ack      -> POST /api/connector/ack         (release the lease done|failed)

CLI: ``openadapt-flow connector enroll`` then ``openadapt-flow connector run``.
"""

from openadapt_flow.connector.client import ConnectorClient, ConnectorClientError
from openadapt_flow.connector.config import (
    ConnectorConfigError,
    ConnectorSettings,
    connector_config_path,
    load_settings,
    save_enrollment,
)
from openadapt_flow.connector.daemon import (
    handle_job,
    phi_free_callback_body,
    run_loop,
    run_once,
)
from openadapt_flow.connector.executor import (
    ExecutionResult,
    build_run_argv,
    execute_job,
    metrics_from_report,
    status_from_report,
)
from openadapt_flow.connector.protocol import (
    ByocGovernanceError,
    ByocJob,
    ByocJobParseError,
    GroundingModel,
    parse_job,
)
from openadapt_flow.connector.storage import (
    InMemoryCustomerStorage,
    LocalCustomerStorage,
    build_storage,
)

__all__ = [
    "ByocGovernanceError",
    "ByocJob",
    "ByocJobParseError",
    "ConnectorClient",
    "ConnectorClientError",
    "ConnectorConfigError",
    "ConnectorSettings",
    "ExecutionResult",
    "GroundingModel",
    "InMemoryCustomerStorage",
    "LocalCustomerStorage",
    "build_run_argv",
    "build_storage",
    "connector_config_path",
    "execute_job",
    "handle_job",
    "load_settings",
    "metrics_from_report",
    "parse_job",
    "phi_free_callback_body",
    "run_loop",
    "run_once",
    "save_enrollment",
    "status_from_report",
]
