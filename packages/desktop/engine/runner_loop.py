"""Compatibility imports for Desktop's Flow-backed hosted runner.

The hosted execution contract lives in :mod:`engine.hosted_runner`. Keep this
module only so older Desktop integrations can import the transport and service
names while using that same implementation.
"""

from engine.hosted_runner import (
    BACKOFF_BASE_S,
    BACKOFF_CAP_S,
    DEFAULT_LEASE_S,
    DEFAULT_WAIT_S,
    POLL_PATH,
    REGISTER_PATH,
    HttpHostedRunnerTransport,
    ReauthRequired,
    RunnerJournal,
    RunnerJournalError,
    RunnerService,
    RunnerSessionStale,
    RunnerTransportError,
    RunnerTrustManifestError,
    backoff_delay,
    callback_path,
    callback_url,
)

__all__ = [
    "BACKOFF_BASE_S",
    "BACKOFF_CAP_S",
    "DEFAULT_LEASE_S",
    "DEFAULT_WAIT_S",
    "POLL_PATH",
    "REGISTER_PATH",
    "HttpHostedRunnerTransport",
    "ReauthRequired",
    "RunnerJournal",
    "RunnerJournalError",
    "RunnerService",
    "RunnerSessionStale",
    "RunnerTransportError",
    "RunnerTrustManifestError",
    "backoff_delay",
    "callback_path",
    "callback_url",
]
