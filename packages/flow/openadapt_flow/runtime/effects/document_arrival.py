"""Document / report arrival with a PARSEABLE-CONTENT assertion.

The third rung of the file-shaped ladder:

- :class:`~openadapt_flow.runtime.effects.file_arrival.FileArrivalVerifier`
  proves a conforming file ARRIVED (name / size / mtime / content probe);
- :class:`~openadapt_flow.runtime.effects.document_hash.DocumentHashVerifier`
  proves exact BYTES (SHA-256 identity);
- this adapter proves the document both arrived AND PARSES to the declared
  business content -- "a claim report landed AND it is valid JSON whose
  ``claim.id`` is THIS run's claim" -- which neither arrival nor a byte hash
  can express for a generated report whose bytes vary run to run.

Each candidate file under ``root`` matching ``pattern`` flattens to a record::

    {"id": <relative path>, "name": ..., "size": ..., "mtime": ...,
     "parseable": "True"/"False", "fresh": "True"/"False",
     <extracted field>: <value>, ...}

where the extracted fields come from the configured parser:

- ``format="json"``: the document must be a JSON object; ``field_paths`` maps
  record field -> dotted path (``claim.id``) into it.
- ``format="text"``: ``text_pattern`` is a regex with NAMED GROUPS; each
  group becomes a record field. No match -> the fields stay absent (the
  selector then cannot match -- fail-safe).

An UNPARSEABLE candidate still yields a record (``parseable: "False"`` with no
extracted fields) so a contract matching ``{"parseable": "True", ...}``
REFUTES on a corrupt report instead of the store reading as empty.

Entity binding is the standard ``Effect.match`` with ``{param: ...}`` values
-- e.g. ``match={"parseable": "True", "claim_id": {"param": "claim_id"}}``,
``expected_count=1``, ``count_new_only=True``.

Fail-safe: a missing root or an unreadable candidate reads as *unreadable* ->
INDETERMINATE (UNAVAILABLE) -> HALT.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Literal, Optional

from openadapt_flow.runtime.effects.adapter import (
    CollateralHook,
    RedactionPolicy,
    VerifierAdapterBase,
    apply_collateral_hooks,
    poll_until_settled,
    redact_verdict,
)
from openadapt_flow.runtime.effects.effect import (
    Effect,
    EffectState,
    EffectVerdict,
)
from openadapt_flow.verification import VerificationTier

#: How many bytes of a candidate document the parser reads.
DOCUMENT_READ_LIMIT = 8 << 20  # 8 MiB


def extract_json_path(document: Any, dotted: str) -> Any:
    """Extract ``dotted`` (``claim.id``) from a parsed JSON document.

    A segment landing on a list takes the first element (the common
    single-row report case). Returns ``None`` when any segment is missing.
    """
    node: Any = document
    for seg in dotted.split("."):
        if isinstance(node, list):
            node = node[0] if node else None
        if not isinstance(node, dict):
            return None
        node = node.get(seg)
    return node


class DocumentArrivalVerifier(VerifierAdapterBase):
    """Verify that a parseable document with the declared content arrived.

    Args:
        root: Directory the workflow's export/report lands in.
        pattern: Filename glob for candidate documents.
        format: ``"json"`` (default) or ``"text"``.
        field_paths: ``format="json"`` only -- record field -> dotted path
            into the parsed document.
        text_pattern: ``format="text"`` only -- regex with named groups; each
            group becomes a record field.
        mtime_window_s: Freshness window for the ``fresh`` flag (``None`` ->
            always fresh).
        redaction: Field-level evidence-minimization policy.
        collateral_hooks: Substrate-specific collateral checks.
        poll_interval_s: Settlement poll gap within ``Effect.timeout_s``.
        clock: Injectable epoch-seconds source (tests).
    """

    substrate = "document"
    verification_tier = VerificationTier.INDEPENDENT_SYSTEM

    def __init__(
        self,
        root: str,
        *,
        pattern: str = "*",
        format: Literal["json", "text"] = "json",
        field_paths: Optional[dict[str, str]] = None,
        text_pattern: Optional[str] = None,
        mtime_window_s: Optional[float] = None,
        redaction: Optional[RedactionPolicy] = None,
        collateral_hooks: tuple[CollateralHook, ...] = (),
        poll_interval_s: float = 0.2,
        clock: Any = time.time,
    ) -> None:
        if format == "text" and not text_pattern:
            raise ValueError(
                "document format 'text' requires text_pattern (a regex with "
                "named groups to extract the asserted content)"
            )
        self.root = Path(root)
        self.pattern = pattern
        self.format = format
        self.field_paths = dict(field_paths or {})
        self.text_pattern = re.compile(text_pattern) if text_pattern else None
        self.mtime_window_s = mtime_window_s
        self.redaction = redaction
        self.collateral_hooks = collateral_hooks
        self.poll_interval_s = poll_interval_s
        self._clock = clock

    # -- parsing ------------------------------------------------------------

    def _extract_fields(self, raw: bytes) -> Optional[dict[str, str]]:
        """Parse ``raw`` per the configured format.

        Returns the extracted fields, or ``None`` when the document does not
        parse / does not match (-> ``parseable: "False"``).
        """
        text = raw.decode("utf-8", errors="replace")
        if self.format == "json":
            try:
                document = json.loads(text)
            except ValueError:
                return None
            return {
                field: "" if value is None else str(value)
                for field, dotted in self.field_paths.items()
                for value in (extract_json_path(document, dotted),)
            }
        assert self.text_pattern is not None
        found = self.text_pattern.search(text)
        if found is None:
            return None
        return {
            key: "" if value is None else str(value)
            for key, value in found.groupdict().items()
        }

    def _scan(self) -> Optional[list[dict[str, Any]]]:
        if not self.root.is_dir():
            return None
        records: list[dict[str, Any]] = []
        for path in sorted(self.root.glob(self.pattern)):
            if not path.is_file():
                continue
            try:
                with path.open("rb") as stream:
                    raw = stream.read(DOCUMENT_READ_LIMIT + 1)
                stat = path.stat()
            except OSError:
                return None  # a vanished/unreadable candidate is unreadable
            if len(raw) > DOCUMENT_READ_LIMIT:
                return None
            fresh = True
            if self.mtime_window_s is not None:
                age_s = float(self._clock()) - stat.st_mtime
                fresh = -self.mtime_window_s <= age_s <= self.mtime_window_s
            record: dict[str, Any] = {
                "id": str(path.relative_to(self.root)),
                "name": path.name,
                "size": stat.st_size,
                "mtime": stat.st_mtime,
                "fresh": str(fresh),
            }
            fields = self._extract_fields(raw)
            record["parseable"] = str(fields is not None)
            if fields:
                record.update(fields)
            records.append(record)
        return records

    # -- VerifierAdapter lifecycle ------------------------------------------

    def capture_pre_state(self, context: Any = None) -> EffectState:
        records = self._scan()
        return EffectState(
            substrate=self.substrate,
            reachable=records is not None,
            records=records or [],
            detail={
                "root": str(self.root),
                "pattern": self.pattern,
                "format": self.format,
            },
        )

    def verify(
        self, expected: Effect, before: EffectState, context: Any = None
    ) -> EffectVerdict:
        verdict = poll_until_settled(
            self._scan,
            expected,
            before,
            substrate=self.substrate,
            poll_interval_s=self.poll_interval_s,
        )
        if self.collateral_hooks:
            verdict = apply_collateral_hooks(
                verdict, before, self.capture_post_state(context), self.collateral_hooks
            )
        return redact_verdict(verdict, self.redaction, field=expected.field)
