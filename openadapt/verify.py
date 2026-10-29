"""Verify a write against the system of record (the ``openadapt verify`` SDK).

``from openadapt.verify import WriteContract, begin_write, verify_write`` is
:mod:`openadapt_flow.verify`: a verified receipt or a refusal for any write,
whoever makes it. Needs the engine (Python 3.10-3.12).
"""

from openadapt_flow.verify import *  # noqa: F401,F403
from openadapt_flow.verify import __all__  # noqa: F401
