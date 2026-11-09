"""Build a scrubbed copy of a recording before it leaves the computer.

``capture share send`` calls :func:`scrub_recording` by default. The copy is
built in a temporary folder; the original recording is never changed.

What the copy keeps, and how:

* ``*.db`` (SQLite): copied, then every text value is scrubbed, every JSON
  value has its string leaves scrubbed, screenshots are redacted with image
  scrubbing, and other binary columns (screenshot diffs, audio) are emptied.
  Per-key typing columns are emptied too: a name typed one key at a time cannot
  be recognized by a text scrubber, so the characters are withheld rather than
  trusted. The copy is vacuumed with ``secure_delete`` so the old values do not
  survive in free pages.
* ``*.json`` and ``*.jsonl``: string leaves scrubbed.
* ``*.txt``, ``*.md``, ``*.csv``, ``*.log``: text scrubbed.
* ``*.png``, ``*.jpg``, ``*.jpeg``: image scrubbed.
* Everything else (video, audio, archives, unknown types, links) is withheld
  and listed in ``SCRUB_MANIFEST.json``. So are the capture's sealed terminal
  record and artifact manifest, because they describe the original bytes.

Scrubbing needs ``openadapt-privacy`` (the ``privacy`` extra). Without it,
:func:`get_scrubber` returns ``None`` and the share refuses to send; it never
falls back to the raw recording.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import shutil
import sqlite3
import time
from importlib import metadata
from pathlib import Path
from typing import Any, Optional, Protocol, runtime_checkable

MANIFEST_NAME = "SCRUB_MANIFEST.json"
MANIFEST_SCHEMA = "openadapt.capture.share-scrub/v1"

_SQLITE_MAGIC = b"SQLite format 3\x00"
_JSON_SUFFIXES = {".json"}
_JSONL_SUFFIXES = {".jsonl"}
_TEXT_SUFFIXES = {".txt", ".md", ".csv", ".log"}
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg"}
# Files that certify the ORIGINAL capture bytes. A scrubbed copy cannot claim
# them, so they are withheld rather than rewritten.
_ORIGINAL_ONLY = {"capture-artifact-manifest.json", "capture-terminal.json"}

# Columns whose values are typed one key at a time. A text scrubber sees one
# character, so it cannot recognize a name or record number. Withhold them.
_KEY_COLUMNS = {
    "key_char",
    "canonical_key_char",
    "key_vk",
    "canonical_key_vk",
}
# Binary columns that are redacted as images. Every other BLOB is emptied.
_IMAGE_BLOB_COLUMNS = {("screenshot", "png_data")}
# Columns that are emptied whatever their type (word-level audio timing).
_WITHHELD_COLUMNS = {("audio_info", "words_with_timestamps")}
# Hash columns recomputed from the scrubbed bytes, so they do not identify the
# original frame.
_HASH_OF_IMAGE = {("screenshot", "png_sha256"): "png_data"}


class ShareScrubError(RuntimeError):
    """The recording could not be scrubbed; nothing was sent."""


@runtime_checkable
class Scrubber(Protocol):
    """The part of ``openadapt-privacy``'s provider that sharing uses."""

    def scrub_text(self, text: str, is_separated: bool = False) -> str: ...

    def scrub_image(self, image: Any, fill_color: Optional[int] = None) -> Any: ...


_UNSET = object()
_scrubber: object = _UNSET


def _build_provider() -> Optional[Scrubber]:
    try:
        from openadapt_privacy.providers.presidio import PresidioScrubbingProvider
    except Exception:  # noqa: BLE001 - any import failure means unavailable
        return None
    try:
        return PresidioScrubbingProvider()
    except Exception:  # noqa: BLE001
        return None


def get_scrubber() -> Optional[Scrubber]:
    """Return the scrubbing provider, or ``None`` when the extra is missing."""
    global _scrubber
    if _scrubber is _UNSET:
        _scrubber = _build_provider()
    return _scrubber  # type: ignore[return-value]


def set_scrubber(scrubber: Optional[Scrubber]) -> None:
    """Inject a scrubber (tests and custom providers)."""
    global _scrubber
    _scrubber = scrubber


def reset_scrubber() -> None:
    """Forget the cached scrubber so the next call rebuilds it."""
    global _scrubber
    _scrubber = _UNSET


class _Scrub:
    """One scrubbing pass with a cache, so repeated values are scrubbed once."""

    def __init__(self, scrubber: Scrubber) -> None:
        self.scrubber = scrubber
        self._cache: dict[str, str] = {}
        self.text_values = 0
        self.images = 0

    def text(self, value: str) -> str:
        if not value or not value.strip():
            return value
        if value not in self._cache:
            self._cache[value] = self.scrubber.scrub_text(value)
            self.text_values += 1
        return self._cache[value]

    def tree(self, value: Any) -> Any:
        if isinstance(value, str):
            # A one-character string is a key press or a fragment of one; it
            # cannot be recognized on its own, so it is withheld.
            if len(value.strip()) == 1 and value.strip().isalnum():
                return ""
            return self.text(value)
        if isinstance(value, list):
            return [self.tree(item) for item in value]
        if isinstance(value, dict):
            return {self.text(str(k)): self.tree(v) for k, v in value.items()}
        return value

    def image_bytes(self, data: bytes) -> bytes:
        from PIL import Image

        image = Image.open(io.BytesIO(data)).convert("RGB")
        redacted = self.scrubber.scrub_image(image)
        out = io.BytesIO()
        redacted.save(out, format="PNG")
        self.images += 1
        return out.getvalue()


def _safe_name(scrub: _Scrub, name: str) -> str:
    # Folder and file names often join words with _ or -; a scrubber reads
    # them better as words.
    spaced = re.sub(r"[_\-.]+", " ", name)
    scrubbed = scrub.text(spaced)
    if scrubbed == spaced:
        scrubbed = name
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", scrubbed).strip("._")
    return cleaned or "recording"


def _scrub_sqlite(src: Path, dst: Path, scrub: _Scrub) -> dict[str, Any]:
    source = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    target = sqlite3.connect(dst)
    try:
        source.backup(target)
    finally:
        source.close()
    withheld: set[str] = set()
    try:
        target.execute("PRAGMA journal_mode=DELETE")
        target.execute("PRAGMA secure_delete=ON")
        tables = [
            row[0]
            for row in target.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%'"
            )
        ]
        for table in tables:
            columns = [row[1] for row in target.execute(f'PRAGMA table_info("{table}")')]
            rowids = [r[0] for r in target.execute(f'SELECT rowid FROM "{table}"')]
            for rowid in rowids:
                row = target.execute(
                    f'SELECT * FROM "{table}" WHERE rowid=?', (rowid,)
                ).fetchone()
                updates: dict[str, Any] = {}
                for column, value in zip(columns, row):
                    if value is None:
                        continue
                    new = _scrub_cell(table, column, value, scrub, withheld)
                    if new != value:
                        updates[column] = new
                for hash_column, image_column in _HASH_OF_IMAGE.items():
                    if hash_column[0] == table and hash_column[1] in columns:
                        image = updates.get(
                            image_column, row[columns.index(image_column)]
                        )
                        updates[hash_column[1]] = (
                            hashlib.sha256(image).hexdigest()
                            if isinstance(image, bytes)
                            else None
                        )
                if updates:
                    assignments = ", ".join(f'"{c}"=?' for c in updates)
                    target.execute(
                        f'UPDATE "{table}" SET {assignments} WHERE rowid=?',
                        (*updates.values(), rowid),
                    )
        target.commit()
        target.execute("VACUUM")
    finally:
        target.close()
    return {"withheld_columns": sorted(withheld)}


def _scrub_cell(
    table: str, column: str, value: Any, scrub: _Scrub, withheld: set[str]
) -> Any:
    if column in _KEY_COLUMNS or (table, column) in _WITHHELD_COLUMNS:
        withheld.add(f"{table}.{column}")
        return None
    if isinstance(value, bytes):
        if (table, column) in _IMAGE_BLOB_COLUMNS:
            return scrub.image_bytes(value)
        withheld.add(f"{table}.{column}")
        return None
    if isinstance(value, str):
        if column in ("key_name", "canonical_key_name") and len(value) == 1:
            withheld.add(f"{table}.{column}")
            return None
        stripped = value.lstrip()
        if stripped[:1] in ("{", "["):
            try:
                parsed = json.loads(value)
            except ValueError:
                return scrub.text(value)
            return json.dumps(scrub.tree(parsed), sort_keys=True)
        return scrub.text(value)
    return value


def _is_sqlite(path: Path) -> bool:
    with path.open("rb") as handle:
        return handle.read(len(_SQLITE_MAGIC)) == _SQLITE_MAGIC


def scrub_recording(
    recording: Path, out_dir: Path, scrubber: Optional[Scrubber] = None
) -> Path:
    """Write a scrubbed copy of ``recording`` under ``out_dir``.

    Returns the copy's folder. Raises :class:`ShareScrubError` when no
    scrubber is available or any file fails to scrub; in that case nothing
    should be sent.
    """
    scrubber = scrubber if scrubber is not None else get_scrubber()
    if scrubber is None:
        raise ShareScrubError(
            "Scrubbing needs openadapt-privacy. Install it with "
            "pip install 'openadapt-capture[privacy]' and "
            "python -m spacy download en_core_web_sm."
        )
    scrub = _Scrub(scrubber)
    dest = out_dir / _safe_name(scrub, recording.name)
    dest.mkdir(parents=True, exist_ok=False)
    scrubbed: list[dict[str, Any]] = []
    withheld: list[dict[str, str]] = []
    for path in sorted(recording.rglob("*")):
        rel = path.relative_to(recording)
        rel_text = rel.as_posix()
        if path.is_symlink():
            withheld.append({"path": rel_text, "reason": "symbolic link"})
            continue
        if path.is_dir():
            continue
        if rel_text in _ORIGINAL_ONLY:
            withheld.append(
                {"path": rel_text, "reason": "certifies the original bytes"}
            )
            continue
        suffix = path.suffix.lower()
        target = dest.joinpath(*(_safe_name(scrub, part) for part in rel.parts[:-1]))
        target = target / (_safe_name(scrub, path.stem) + suffix)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            if suffix in (".db", ".sqlite", ".sqlite3") and _is_sqlite(path):
                detail = _scrub_sqlite(path, target, scrub)
                scrubbed.append({"path": rel_text, "as": "sqlite", **detail})
            elif suffix in _JSON_SUFFIXES:
                data = json.loads(path.read_text(encoding="utf-8"))
                target.write_text(
                    json.dumps(scrub.tree(data), indent=2, sort_keys=True),
                    encoding="utf-8",
                )
                scrubbed.append({"path": rel_text, "as": "json"})
            elif suffix in _JSONL_SUFFIXES:
                lines = path.read_text(encoding="utf-8").splitlines()
                target.write_text(
                    "".join(
                        json.dumps(scrub.tree(json.loads(line)), sort_keys=True) + "\n"
                        for line in lines
                        if line.strip()
                    ),
                    encoding="utf-8",
                )
                scrubbed.append({"path": rel_text, "as": "jsonl"})
            elif suffix in _TEXT_SUFFIXES:
                target.write_text(
                    scrub.text(path.read_text(encoding="utf-8")), encoding="utf-8"
                )
                scrubbed.append({"path": rel_text, "as": "text"})
            elif suffix in _IMAGE_SUFFIXES:
                target.with_suffix(".png").write_bytes(
                    scrub.image_bytes(path.read_bytes())
                )
                scrubbed.append({"path": rel_text, "as": "image"})
            else:
                withheld.append(
                    {"path": rel_text, "reason": "type cannot be scrubbed"}
                )
        except Exception as exc:  # noqa: BLE001 - any failure stops the share
            shutil.rmtree(dest, ignore_errors=True)
            raise ShareScrubError(
                f"Could not scrub {rel_text}: {type(exc).__name__}. Nothing was sent."
            ) from exc
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "created_at": time.time(),
        "provider": _provider_evidence(scrubber),
        "scrubbed": scrubbed,
        "withheld": withheld,
        "counts": {"text_values": scrub.text_values, "images": scrub.images},
        "note": (
            "Scrubbed copy of a recording. Scrubbing is automatic and can miss "
            "data; review before sharing further."
        ),
    }
    (dest / MANIFEST_NAME).write_text(
        json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
    )
    return dest


def _provider_evidence(scrubber: Scrubber) -> dict[str, Any]:
    try:
        version = metadata.version("openadapt-privacy")
    except metadata.PackageNotFoundError:
        version = None
    return {
        "class": f"{type(scrubber).__module__}.{type(scrubber).__qualname__}",
        "openadapt_privacy_version": version,
    }
