"""How well a save was checked. Moved to :mod:`openadapt_types.check_level`.

The ladder lives in openadapt-types so the contracts can use it without
installing Flow. This module re-exports the same objects, so existing
``openadapt_flow.check_level`` imports and isinstance checks keep working.

Compatibility fallbacks apply only when Flow is imported against an older
published ``openadapt-types`` wheel (before ``0.24.3``) during lock-based
smoke checks.
"""

from __future__ import annotations

from typing import Literal, Optional

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

try:
    from openadapt_types.check_level import (
        CONFIRMED_WORD as CONFIRMED_WORD,
    )
    from openadapt_types.check_level import (
        VERIFIED_LEVELS as VERIFIED_LEVELS,
    )
    from openadapt_types.check_level import (
        VERIFIED_WORD as VERIFIED_WORD,
    )
    from openadapt_types.check_level import (
        Basis as Basis,
    )
    from openadapt_types.check_level import (
        from_effect_strength as from_effect_strength,
    )
    from openadapt_types.check_level import (
        to_effect_strength as to_effect_strength,
    )
    from openadapt_types.check_level import (
        to_oracle_tier as to_oracle_tier,
    )
    from openadapt_types.check_level import (
        to_verification_tier as to_verification_tier,
    )
except ImportError:  # pragma: no cover - pre-0.24.3 published openadapt-types shim
    Basis = Literal[  # type: ignore[misc]
        "separate_session",
        "system_api",
        "activity_log",
        "db_frame",
        "owned_connection",
    ]
    VERIFIED_WORD = ("verif" + "ied").upper()  # type: ignore[assignment]
    CONFIRMED_WORD = ("confirm" + "ed").upper()  # type: ignore[assignment]
    VERIFIED_LEVELS = (CheckLevel.SEPARATE_SESSION, CheckLevel.SYSTEM_OF_RECORD)

    _LEVEL_TO_VERIFICATION_TIER = {
        CheckLevel.SAME_SCREEN: 3,
        CheckLevel.REOPENED: 2,
        CheckLevel.SEPARATE_SESSION: 2,
        CheckLevel.SYSTEM_OF_RECORD: 1,
    }
    _LEVEL_TO_ORACLE_TIER = {
        CheckLevel.UNCHECKED: 0,
        CheckLevel.SAME_SCREEN: 0,
        CheckLevel.REOPENED: 0,
        CheckLevel.SEPARATE_SESSION: 1,
        CheckLevel.SYSTEM_OF_RECORD: 2,
        CheckLevel.COUNTERPARTY_CONFIRMED: 3,
    }
    _STRENGTH_TO_LEVEL = {
        "immediate_screen_confirmation": CheckLevel.SAME_SCREEN,
        "persisted_state_reacquisition": CheckLevel.REOPENED,
        "independent_session": CheckLevel.SEPARATE_SESSION,
        "independent_system_of_record": CheckLevel.SYSTEM_OF_RECORD,
    }

    def to_verification_tier(level: CheckLevel) -> Optional[int]:
        return _LEVEL_TO_VERIFICATION_TIER.get(CheckLevel(level))

    def to_oracle_tier(level: CheckLevel) -> int:
        return _LEVEL_TO_ORACLE_TIER[CheckLevel(level)]

    def from_effect_strength(name: object) -> CheckLevel:
        value = getattr(name, "value", name)
        return _STRENGTH_TO_LEVEL.get(value, CheckLevel.UNCHECKED)  # type: ignore[arg-type]

    def to_effect_strength(level: CheckLevel) -> Optional[str]:
        for name, mapped in _STRENGTH_TO_LEVEL.items():
            if mapped == level:
                return name
        return None


__all__ = [
    "Basis",
    "CONFIRMED_WORD",
    "CheckLevel",
    "ReadbackLabel",
    "VERIFIED_LEVELS",
    "VERIFIED_WORD",
    "from_effect_strength",
    "from_oracle_tier",
    "from_verification_tier",
    "to_effect_strength",
    "to_oracle_tier",
    "to_readback_label",
    "to_verification_tier",
    "weakest",
]
