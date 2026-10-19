"""Spoken narration lined up with a recording's steps.

A recording directory (``meta.json`` + ``events.jsonl`` + ``frames/``) may
carry ``narration.json``: what the person said while demonstrating, cut into
sentences, scrubbed of patient data, and lined up with the recorded steps.
The compiler uses it to **propose** a branch rule (see
:mod:`openadapt_flow.compiler.narration_rules`); a proposal never certifies
itself.

The file is the same for every backend. It only needs the ``t`` (seconds since
the recording started) that every ``events.jsonl`` line carries, so web,
native, RDP and Citrix recordings get the same treatment. It arrives in one of
two ways:

* ``openadapt-flow`` converts an openadapt-capture session that recorded with
  ``--audio`` (:mod:`openadapt_flow.adapters.capture`); capture has already
  scrubbed the text.
* ``openadapt-flow narration attach RECORDING --audio FILE.wav`` (or
  ``--transcript FILE.json``) transcribes on this machine and scrubs here.

Rules:

* **Scrubbed or not written.** :func:`attach_narration` refuses without an
  ``openadapt-privacy`` scrubber. :func:`load_narration` refuses a file that
  does not say it was scrubbed.
* **Local only.** Transcription uses openadapt-capture's on-device backends.
* **Alignment is by time.** A sentence belongs to the next action the person
  takes (people say what they are about to do), or to the last action when
  they speak after the final one.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

from pydantic import BaseModel, Field

NARRATION_FILE = "narration.json"
NARRATION_SCHEMA = "openadapt.flow.narration/v1"

#: A sentence that starts this soon after an action began still belongs to it
#: (the person kept talking as they clicked).
LEAD_S = 0.3

#: Event kinds that are steps (structural events such as switch_surface are not).
_NON_STEP_KINDS = frozenset({"switch_surface"})


class NarrationRefused(RuntimeError):
    """Narration was not written or read (not scrubbed, or no scrubber)."""


class NarrationLine(BaseModel):
    """One spoken sentence, timed from the start of the recording."""

    t_start: float = Field(description="Seconds since the recording started")
    t_end: float
    text: str = Field(description="Scrubbed sentence")
    step_id: Optional[str] = Field(
        default=None,
        description="The step it is lined up with (step_<event index>)",
    )


class Narration(BaseModel):
    """The ``narration.json`` sidecar."""

    schema_id: str = Field(default=NARRATION_SCHEMA, alias="schema")
    scrubbed: bool = True
    scrubbed_by: dict[str, Any] = Field(default_factory=dict)
    source: str = Field(default="", description="openadapt-capture | attach")
    lines: list[NarrationLine] = Field(default_factory=list)

    model_config = {"populate_by_name": True}

    def for_step(self, step_id: str) -> list[NarrationLine]:
        return [line for line in self.lines if line.step_id == step_id]


def step_id_for_event(index: int) -> str:
    """The compiler's step id for ``events.jsonl`` event ``index``."""
    return f"step_{index:03d}"


def read_events(recording_dir: Path) -> list[dict[str, Any]]:
    path = Path(recording_dir) / "events.jsonl"
    if not path.is_file():
        raise FileNotFoundError(f"no events.jsonl in {recording_dir}")
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def align(
    sentences: Iterable[tuple[float, float, str]],
    events: Sequence[dict[str, Any]],
) -> list[NarrationLine]:
    """Line each ``(t_start, t_end, text)`` up with a recorded step.

    Times are seconds since the recording started (the events' ``t``). A
    sentence goes to the first step whose ``t`` is at or after the sentence's
    start minus :data:`LEAD_S`; with none, to the last step.
    """
    steps = sorted(
        (
            (float(e["t"]), int(e["i"]))
            for e in events
            if "t" in e and "i" in e and e.get("kind") not in _NON_STEP_KINDS
        ),
        key=lambda pair: pair[0],
    )
    out: list[NarrationLine] = []
    for start, end, text in sentences:
        text = str(text or "").strip()
        if not text:
            continue
        chosen: Optional[int] = None
        for t, i in steps:
            if t >= start - LEAD_S:
                chosen = i
                break
        if chosen is None and steps:
            chosen = steps[-1][1]
        out.append(
            NarrationLine(
                t_start=round(float(start), 3),
                t_end=round(float(end), 3),
                text=text,
                step_id=step_id_for_event(chosen) if chosen is not None else None,
            )
        )
    return out


def write_narration(directory: Path, narration: Narration) -> Path:
    """Write ``narration.json`` atomically. Refuses unscrubbed narration."""
    if narration.scrubbed is not True:
        raise NarrationRefused("narration is written only after it is scrubbed")
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / NARRATION_FILE
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".narration-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(narration.model_dump_json(indent=2, by_alias=True) + "\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return path


def load_narration(path: Path | str) -> Optional[Narration]:
    """Read ``narration.json`` from a recording or bundle directory, or None."""
    file = Path(path) / NARRATION_FILE
    if not file.is_file():
        return None
    data = json.loads(file.read_text(encoding="utf-8"))
    if data.get("schema") != NARRATION_SCHEMA:
        raise NarrationRefused(f"{file} is not a narration file")
    if data.get("scrubbed") is not True:
        raise NarrationRefused(f"{file} does not say it was scrubbed; not reading it")
    return Narration.model_validate(data)


def _scrubber_evidence(scrubber: Any) -> dict[str, Any]:
    from importlib import metadata

    try:
        version = metadata.version("openadapt-privacy")
    except metadata.PackageNotFoundError:
        version = None
    return {
        "class": f"{type(scrubber).__module__}.{type(scrubber).__qualname__}",
        "openadapt_privacy_version": version,
    }


def require_scrubber() -> Any:
    """The openadapt-privacy scrubber, or :class:`NarrationRefused`."""
    from openadapt_flow import privacy

    scrubber = privacy.get_scrubber()
    if scrubber is None:
        raise NarrationRefused(
            "Narration is not stored without scrubbing: spoken words can name a "
            "patient. Install openadapt-privacy with pip install "
            "'openadapt-flow[privacy]' and python -m spacy download en_core_web_sm."
        )
    return scrubber


def attach_narration(
    recording_dir: Path | str,
    sentences: Iterable[tuple[float, float, str]],
    *,
    scrubber: Any = None,
    source: str = "attach",
) -> Path:
    """Scrub ``sentences``, line them up with the recording, and write them.

    ``sentences`` are ``(t_start, t_end, text)`` in seconds since the
    recording started. ``scrubber`` defaults to openadapt-privacy; without it
    this raises :class:`NarrationRefused` and writes nothing.
    """
    recording_dir = Path(recording_dir)
    scrubber = scrubber if scrubber is not None else require_scrubber()
    events = read_events(recording_dir)
    clean = []
    for start, end, text in sentences:
        try:
            scrubbed = scrubber.scrub_text(str(text))
        except Exception as exc:  # noqa: BLE001 - not scrubbed means not written
            raise NarrationRefused(
                f"the scrubber failed, nothing written: {exc}"
            ) from exc
        clean.append((start, end, scrubbed))
    narration = Narration(
        scrubbed=True,
        scrubbed_by=_scrubber_evidence(scrubber),
        source=source,
        lines=align(clean, events),
    )
    return write_narration(recording_dir, narration)


def sentences_from_transcript(
    result: dict[str, Any], *, offset: float = 0.0
) -> list[tuple[float, float, str]]:
    """Whisper-style result -> ``(t_start, t_end, text)`` sentences.

    ``offset`` is where the audio starts, in seconds since the recording
    started (0 when audio and recording started together).
    """
    try:
        from openadapt_capture.narration import utterances_from_transcript
    except ImportError:  # capture extra missing: one sentence per segment
        return [
            (
                float(s.get("start", 0.0)) + offset,
                float(s.get("end", 0.0)) + offset,
                str(s.get("text") or "").strip(),
            )
            for s in result.get("segments") or []
            if str(s.get("text") or "").strip()
        ]
    return [
        (u.start, u.end, u.text)
        for u in utterances_from_transcript(result, offset=offset)
    ]


def transcribe_audio(
    path: Path | str, *, model: str = "base", backend: str = "auto"
) -> dict[str, Any]:
    """Transcribe an audio file on this machine (openadapt-capture's backends)."""
    from openadapt_capture.narration import transcribe_file

    return transcribe_file(path, backend=backend, model=model)
