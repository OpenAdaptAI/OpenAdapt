"""Verify a write against the system of record (the ``openadapt verify`` SDK).

``from openadapt.verify import Contract, begin_write, verify_write`` is
:mod:`openadapt_flow.verifier`: one outcome (CONFIRMED, REFUTED or
UNCERTAIN) for any write, whoever makes it, and a ledger that stops an
uncertain write from being sent twice. Needs the engine (Python 3.10-3.12).
"""

from openadapt_flow.verifier import *  # noqa: F401,F403
from openadapt_flow.verifier import __all__  # noqa: F401
