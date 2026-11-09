"""How well a save was checked. Moved to :mod:`openadapt_types.check_level`.

The ladder lives in openadapt-types so the contracts can use it without
installing Flow. This module re-exports the same objects, so existing
``openadapt_flow.check_level`` imports and isinstance checks keep working.
"""

from __future__ import annotations

from openadapt_types.check_level import (
    CheckLevel as CheckLevel,
)
from openadapt_types.check_level import (
    ReadbackLabel as ReadbackLabel,
)
from openadapt_types.check_level import (
    from_oracle_tier as from_oracle_tier,
)
from openadapt_types.check_level import (
    from_verification_tier as from_verification_tier,
)
from openadapt_types.check_level import (
    to_readback_label as to_readback_label,
)
from openadapt_types.check_level import (
    weakest as weakest,
)

__all__ = [
    "CheckLevel",
    "ReadbackLabel",
    "from_oracle_tier",
    "from_verification_tier",
    "to_readback_label",
    "weakest",
]
