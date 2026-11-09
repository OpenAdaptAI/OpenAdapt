"""MIT reference reward worker: verified terminal effects for a training loop.

A reward receipt states one thing: OpenAdapt read the terminal effect of one
episode through an independent oracle and judged it against one reward
contract. It never states that Flow governed the policy's actions. A model
rollout is not a qualified program, so it never receives an Execute receipt
or an Execute Seal. The two receipts carry different schema ids and cannot
be exchanged for one another.

The worker signs receipts with a local Ed25519 key, the same way the
reference Execute server signs its local receipts. The key lives in a
sibling data directory. Evidence bytes stay on this machine; a receipt
carries digests only.
"""

from __future__ import annotations

from typing import Literal

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8788
DEFAULT_DATA_DIRNAME = "reward-ref"
REWARD_NOTICE: Literal[
    "Reward receipt. Not an Execute Seal. Flow did not govern the policy."
] = "Reward receipt. Not an Execute Seal. Flow did not govern the policy."

__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "DEFAULT_DATA_DIRNAME",
    "REWARD_NOTICE",
]
