"""Trainer adapters for the certified reward contract.

A trainer (TRL GRPO or verl) asks a reward endpoint to score one episode and
gets back a signed ``RewardEvidenceReceiptV1`` from ``openadapt-types``. The
adapters in this package turn that receipt into the scalar the trainer
expects and enforce three rules the receipt states:

* an unscored outcome (``reconciliation_required``, ``failed_platform``) is
  dropped from its GRPO group, never scored ``0.0``;
* a ``development_only`` receipt (oracle tier 0 or 1) is never certified;
* certificate expiry is logged, and in ``require_certified`` mode it stops
  the run.

Every episode also carries the oracle identity the worker needs to read the
record (``metadata.oracle_identity`` on the wire). An episode without one is
refused here, before any HTTP call, not by the worker with a 422.

The reward worker itself (oracle read, judge, signing, HTTP route) lives in
``openadapt-flow``. This package consumes its receipts.
"""

from openadapt_evals.reward.receipts import (
    CallableRewardSource,
    EpisodeDescriptor,
    HttpRewardEndpoint,
    OracleIdentityError,
    RewardEndpointError,
    ScoredEpisode,
    UncertifiedRewardError,
    assess_receipt,
    fill_unscored_with_group_mean,
    parse_receipt,
)

__all__ = [
    "CallableRewardSource",
    "EpisodeDescriptor",
    "HttpRewardEndpoint",
    "OracleIdentityError",
    "RewardEndpointError",
    "ScoredEpisode",
    "UncertifiedRewardError",
    "assess_receipt",
    "fill_unscored_with_group_mean",
    "parse_receipt",
]
