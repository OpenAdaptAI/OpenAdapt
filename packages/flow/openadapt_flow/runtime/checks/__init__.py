"""Checks tagged with where their evidence comes from.

``docs/design/DECOMPOSITION.md`` S5: a check reads either the screen or the
system of record. This package holds checks whose source is the record.
"""

from __future__ import annotations

from typing import Literal

#: Where a check's evidence comes from.
CheckSource = Literal["screen", "record"]

__all__ = ["CheckSource"]
