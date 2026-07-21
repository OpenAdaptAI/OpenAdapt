"""Optional local-VLM identity comparator (tier 3 of the identity ladder).

Ships :class:`MLXIdentityVLM`, an :class:`~openadapt_flow.runtime.identity.IdentityVLM`
implementation backed by a LOCAL open VLM served through MLX (Apple-silicon).
It is the runtime promotion of the validated experiment in
``openadapt_flow.validation.vlm_identity_probe`` (benchmark/vlm_identity,
PR #28): give the model the two magnified identifier crops stacked A-over-B
and ask "same characters or different?", VETO-ONLY.

It is OPTIONAL and OFF by default -- exactly like the grounder. The default
install pulls no model: the identity ladder runs structured-text +
pixel-compare + OCR + halt with zero extra dependencies. A caller that wants
the veto passes an instance into ``Replayer(identity_vlm=...)``; the mlx-vlm
import is guarded so importing this module never requires the package, and the
model loads lazily on first use.

ZERO Anthropic / cloud API calls: the model is a local open checkpoint (default
``mlx-community/Qwen3-VL-4B-Instruct-4bit``) served entirely on-device.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any, Optional

DEFAULT_MODEL = "mlx-community/Qwen3-VL-4B-Instruct-4bit"


class MLXIdentityVLM:
    """Local MLX VLM used as a veto-only same/different identity comparator.

    Wraps the validated ``vlm_identity_probe.Comparator`` so the runtime tier
    reuses the exact prompt, stacked-pair composition, and veto-only parsing
    that were measured (0% false-accept + 100% detection on the digit-flanked
    O/0 collapse surface, ~0.8s/call). The model loads lazily on first call.
    """

    def __init__(self, model_name: str = DEFAULT_MODEL) -> None:
        self.model_name = model_name
        self._comparator: Optional[Any] = None

    def _ensure_loaded(self) -> Any:
        if self._comparator is None:
            # Lazy + import-guarded: mlx-vlm is an optional, Apple-silicon-only
            # dependency; importing this module must never require it.
            from openadapt_flow.validation.vlm_identity_probe import Comparator

            self._comparator = Comparator(self.model_name).load()
        return self._comparator

    def same_or_different(self, recorded_png: bytes, live_png: bytes) -> str:
        """Return ``"same"`` or ``"different"`` for the two identifier crops.

        VETO-ONLY: the underlying comparator folds any non-confident answer to
        ``"different"`` (see ``vlm_identity_probe.parse_veto``), so an unsure
        model halts rather than passing a wrong patient.
        """
        comparator = self._ensure_loaded()
        with tempfile.TemporaryDirectory() as td:
            result = comparator.compare(recorded_png, live_png, Path(td))
        return str(result.get("verdict", "different"))
