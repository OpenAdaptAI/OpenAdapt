"""MIT reference Execute server: one process, local self-signed receipts.

This is the engine-side host for the public Execute v1 request schema. It is
complete for one operator on one machine. It is not OpenAdapt Cloud: receipts
are self-signed with a local Ed25519 key, never an OpenAdapt production Seal.
"""

from __future__ import annotations

from typing import Literal

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787
DEFAULT_DATA_DIRNAME = "execute-ref"
SELF_SIGNED_NOTICE: Literal[
    "Self-signed. Counterparties that require an OpenAdapt Seal still use Cloud."
] = "Self-signed. Counterparties that require an OpenAdapt Seal still use Cloud."

__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "DEFAULT_DATA_DIRNAME",
    "SELF_SIGNED_NOTICE",
]
