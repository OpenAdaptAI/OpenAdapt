"""Effect locations: where a careful person would look again to see a value.

Echo before commit, re-observe after. For each intended value, a pack stores
an effect location compiled from the demonstration: how to get there (the path
and a start condition), what to look for (the locator), what it should show
(the expected value, read through a codec), how many times (usually once) and,
optionally, what it showed before. A review page is one effect location; a
summary row, a lookup-table row, a reopened record, a separate session and an
export are others.

:func:`evaluate` judges one location from one reading. :func:`evaluate_all`
judges every intended value of a run. A location that can't be reached, found
or read is ``not_checked``, never a silent pass. The engine holds no
vocabulary for any application or field: every word, pattern and token table
comes from the pack. See ``docs/EFFECT_LOCATION.md``.
"""

from __future__ import annotations

from openadapt_flow.effect_location.codecs import CodecError, decode
from openadapt_flow.effect_location.evaluator import (
    KeyResult,
    Report,
    evaluate,
    evaluate_all,
)
from openadapt_flow.effect_location.record import (
    CODEC_KINDS,
    LOCATOR_KINDS,
    SCHEMA,
    Codec,
    EffectLocation,
    Locator,
    Reading,
    Start,
)

__all__ = [
    "CODEC_KINDS",
    "LOCATOR_KINDS",
    "SCHEMA",
    "Codec",
    "CodecError",
    "EffectLocation",
    "KeyResult",
    "Locator",
    "Reading",
    "Report",
    "Start",
    "decode",
    "evaluate",
    "evaluate_all",
]
