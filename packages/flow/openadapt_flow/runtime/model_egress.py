"""Count model calls by placement and record every frame sent off the box.

The replayer wraps each model component it is given (grounder, identity VLM,
state verifier) with :meth:`EgressRecorder.wrap`. A wrapped component that may
send a frame off the box (see
:func:`openadapt_flow.runtime.grounder.component_may_egress`) writes one line
to the local egress ledger **before** each call. If that line can't be
written, the call is not made and the component returns its fail-safe answer
(the grounder abstains, the identity VLM says "different", the state verifier
says no), so the step halts. Nothing leaves the box unrecorded, and a halted
run's frames are on the ledger, since each line is written when the frame is
sent.

The ledger is append-only JSON lines, mode 0600, fsynced per line. A line
holds the frame's SHA-256 and size, the component, the destination host, the
run and the step, and the time. It never holds pixels or screen text. By
default it is ``egress-ledger.jsonl`` next to the run directory, so it covers
every run written under the same runs root; ``OPENADAPT_FLOW_EGRESS_LEDGER``
names another file.

Calls are also counted, by placement, onto the step's
:class:`~openadapt_flow.ir.StepResult`: off-box identity-VLM calls in
``identity_vlm_calls``, in-process ones in ``local_identity_vlm_calls``, and
off-box grounder calls (including ones where the grounder abstained) in
``grounder_calls``. The replayer folds the off-box counts into
``RunReport.model_calls`` and the in-process ones into ``local_model_calls``.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit

LEDGER_ENV = "OPENADAPT_FLOW_EGRESS_LEDGER"
LEDGER_NAME = "egress-ledger.jsonl"


def append_ledger_line(path: Path, entry: dict[str, Any]) -> None:
    """Append one JSON line to ``path`` and fsync it. Raises ``OSError``."""

    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(entry, sort_keys=True, separators=(",", ":")) + "\n").encode()
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        while data:
            written = os.write(fd, data)
            if written <= 0:
                raise OSError("the egress ledger append made no progress")
            data = data[written:]
        os.fsync(fd)
    finally:
        os.close(fd)


def _destination(component: Any) -> str:
    """The host a component sends to, or ``"unknown"``."""

    for holder in (component, getattr(component, "_client", None)):
        for name in ("base_url", "_base_url"):
            url = getattr(holder, name, None)
            if isinstance(url, str) and url:
                host = urlsplit(url if "//" in url else f"//{url}").hostname
                if host:
                    return host
    return "unknown"


class EgressRecorder:
    """Per-replayer ledger writer and placement-aware call counter."""

    def __init__(self) -> None:
        self.ledger_path: Optional[Path] = None
        self.run_ref: Optional[str] = None
        self.step_id: Optional[str] = None
        #: Every ledger line this recorder made, also when no run is bound
        #: (a replayer driven outside ``run``, as unit tests do).
        self.entries: list[dict[str, Any]] = []
        self._pending = {
            "identity_vlm_calls": 0,
            "local_identity_vlm_calls": 0,
            "grounder_calls": 0,
        }

    def bind_run(self, run_dir: Path, run_id: Optional[str] = None) -> None:
        """Point the ledger at this run (call before the run sends anything)."""

        override = os.environ.get(LEDGER_ENV, "").strip()
        run_dir = Path(run_dir)
        self.ledger_path = Path(override) if override else run_dir.parent / LEDGER_NAME
        self.run_ref = run_id or run_dir.name

    def admit(self, kind: str, component: Any, *frames: bytes) -> bool:
        """Record a frame about to leave the box. False means: don't send."""

        entry = {
            "at": datetime.now(timezone.utc).isoformat(),
            "run": self.run_ref,
            "step": self.step_id,
            "component": kind,
            "destination": _destination(component),
            "frames": [
                {"sha256": hashlib.sha256(f).hexdigest(), "bytes": len(f)}
                for f in frames
                if isinstance(f, (bytes, bytearray))
            ],
        }
        if self.ledger_path is not None:
            try:
                append_ledger_line(self.ledger_path, entry)
            except OSError:
                return False
        self.entries.append(entry)
        return True

    def count(self, field: str) -> None:
        self._pending[field] += 1

    def drain(self, result: Any) -> None:
        """Move the calls counted since the last drain onto ``result``."""

        if result is None:
            return
        for field, n in self._pending.items():
            if n:
                setattr(result, field, getattr(result, field) + n)
                self._pending[field] = 0

    def wrap(self, kind: str, component: Any) -> Any:
        """``component`` wrapped for recording, or as is when nothing to do."""

        from openadapt_flow.runtime.grounder import (
            FallbackGrounder,
            component_may_egress,
        )

        if component is None:
            return None
        if isinstance(component, FallbackGrounder):
            return FallbackGrounder(
                [self.wrap(kind, g) for g in component._grounders]
            )
        off_box = component_may_egress(component)
        if kind == "identity_vlm":
            return _IdentityVLM(self, component, off_box)
        if not off_box:
            return component
        if kind == "grounder":
            return _Grounder(self, component)
        return _StateVerifier(self, component)


class _Wrapped:
    MAY_EGRESS: bool

    def __init__(self, recorder: EgressRecorder, inner: Any) -> None:
        self._recorder = recorder
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _Grounder(_Wrapped):
    MAY_EGRESS = True

    def locate(self, screen_png: bytes, intent: str, ocr_text: Optional[str] = None):
        if not self._recorder.admit("grounder", self._inner, screen_png):
            return None
        self._recorder.count("grounder_calls")
        return self._inner.locate(screen_png, intent, ocr_text)


class _IdentityVLM(_Wrapped):
    def __init__(self, recorder: EgressRecorder, inner: Any, off_box: bool) -> None:
        super().__init__(recorder, inner)
        self.MAY_EGRESS = off_box

    def same_or_different(self, recorded_png: bytes, live_png: bytes) -> str:
        if self.MAY_EGRESS:
            if not self._recorder.admit(
                "identity_vlm", self._inner, recorded_png, live_png
            ):
                return "different"
            self._recorder.count("identity_vlm_calls")
        else:
            self._recorder.count("local_identity_vlm_calls")
        return self._inner.same_or_different(recorded_png, live_png)


class _StateVerifier(_Wrapped):
    MAY_EGRESS = True

    def holds(self, screenshot: bytes, expected_state: str) -> bool:
        if not self._recorder.admit("state_verifier", self._inner, screenshot):
            return False
        return self._inner.holds(screenshot, expected_state)


def _grounder_resolution(result: Any, local: bool) -> bool:
    res = getattr(result, "resolution", None)
    return bool(
        result.ok
        and res is not None
        and res.rung == "grounder"
        and (res.grounder_model == "local") is local
    )


def model_calls_off_box(result: Any) -> int:
    """Model calls on ``result`` that may have sent a frame off the box.

    Drift-oracle calls, off-box identity-VLM calls, and grounder calls. The
    recorder counts every off-box grounder call, including abstentions and
    calls on failed steps; a result built elsewhere (no recorder count) still
    counts its grounder resolution, as before.
    """

    return int(
        result.drift_oracle_calls
        + result.identity_vlm_calls
        + max(result.grounder_calls, int(_grounder_resolution(result, local=False)))
    )


def model_calls_in_process(result: Any) -> int:
    """Model calls on ``result`` that ran in process (no frame left the box)."""

    return int(
        _grounder_resolution(result, local=True) + result.local_identity_vlm_calls
    )
