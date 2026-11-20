"""Refuse protected operation when the installed Flow cannot select it."""

import os

_PROTECTED_CONFIG_ENV = "OPENADAPT_LEDGER_AUTHORITY_CONFIG"


class ProtectedLedgerUnsupported(RuntimeError):
    """The installed Flow lacks the configured protected default."""


def require_protected_default_support() -> None:
    if _PROTECTED_CONFIG_ENV not in os.environ:
        return
    from openadapt_flow import write_ledger

    if (
        getattr(write_ledger, "PROTECTED_LEDGER_CONFIG_ENV", None)
        != _PROTECTED_CONFIG_ENV
    ):
        raise ProtectedLedgerUnsupported("ledger_authority_unsupported")
