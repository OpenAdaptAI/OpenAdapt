"""Write an exchange into an L1 extraction directory.

Each artifact becomes one :func:`~openadapt_flow.emit.l1_artifact.emit_l1_artifact`
call, so the extraction directory gets the same canonical
``{file_number}_{date}_{doctype}`` files and ``manifest.csv`` rows a records
platform already ingests:

- a ``source_document`` with a payload: the payload file;
- a ``structured_observation``: the exchange JSON itself, which carries the
  field values, grades, decisions and Seal references.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from openadapt_flow.emit.l1_artifact import L1ArtifactRef, emit_l1_artifact
from openadapt_flow.interchange.conformance import check_file, resolve_payload
from openadapt_flow.interchange.model import Exchange


class ExchangeNotConforming(ValueError):
    """The exchange has conformance violations and was not emitted."""


def emit_exchange(
    exchange_path: Path | str, extraction_dir: Path | str
) -> list[L1ArtifactRef]:
    """Emit every artifact of a conforming exchange. Refuses a non-conforming one."""
    path = Path(exchange_path)
    violations = check_file(path)
    if violations:
        raise ExchangeNotConforming("; ".join(str(v) for v in violations))
    ex = Exchange.model_validate(json.loads(path.read_text(encoding="utf-8")))
    refs: list[L1ArtifactRef] = []
    for a in ex.artifacts:
        if a.kind == "source_document" and a.payload is None:
            continue
        if a.payload is None:
            payload = path
        else:
            # check_file already refused a path outside the exchange
            # directory; resolve again so a file swapped in since is not
            # copied from outside it.
            resolved = resolve_payload(path.parent, a.payload.path)
            if resolved is None:
                raise ExchangeNotConforming(
                    f"IX071 artifacts[{a.artifact_id}].payload: the path "
                    "resolves outside the exchange directory"
                )
            # Hash again just before copying, so a file changed since the
            # check is not emitted under the exchange's sha256.
            try:
                digest = hashlib.sha256(resolved.read_bytes()).hexdigest()
            except OSError:
                digest = None
            if digest != a.payload.sha256:
                raise ExchangeNotConforming(
                    f"IX070 artifacts[{a.artifact_id}].payload: the file changed "
                    "after the check"
                )
            payload = resolved
        refs.append(
            emit_l1_artifact(
                payload,
                extraction_dir,
                file_number=a.file_number,
                date=a.date,
                doctype=a.doctype,
                source_type=a.source_type,
                session_id=ex.session_id or ex.exchange_id,
            )
        )
    return refs
