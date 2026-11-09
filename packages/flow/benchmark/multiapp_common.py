"""Shared harness pieces for the multi-system workflow benchmarks.

Used by ``benchmark/ap_invoice`` and ``benchmark/o2c_recon``. Everything here
is deterministic, synthetic, and localhost-only: null backend/vision fakes for
the API-tier replay path (the same pattern ``benchmark/effect_e2e`` uses), a
surface-routing composite effect verifier, a CSV row oracle for spreadsheet
write-back verification, deterministic RFC822 email + minimal PDF fixtures,
and the governed Standard-profile admission helper.

No model calls, no network beyond 127.0.0.1, no PHI (all names are synthetic
company/worker placeholders).
"""

from __future__ import annotations

import csv
import json
import struct
import time
import zlib
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import formatdate
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

from openadapt_flow.deployment import DeploymentConfig, PolicySection
from openadapt_flow.execution_profiles import execution_profile_contract
from openadapt_flow.ir import Workflow
from openadapt_flow.run_gate import build_runtime_authorization, evaluate_run_gate
from openadapt_flow.runtime.authorization import GovernedRunAuthorization
from openadapt_flow.runtime.effects._common import judge_records
from openadapt_flow.runtime.effects.effect import (
    Effect,
    EffectState,
    EffectVerdict,
)
from openadapt_flow.verification import VerificationTier, verifier_effect_tier

# -- replayer fakes ----------------------------------------------------------
# The api actuation tier returns before any GUI resolve/act, so these only
# need to satisfy the replayer's construction and the (unreached) settle path.


def tiny_png() -> bytes:
    """A valid 1x1 white PNG (the api-tier path never renders it)."""
    raw = b"\x00\xff\xff\xff"
    idat = zlib.compress(raw)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", idat)
        + chunk(b"IEND", b"")
    )


_PNG = tiny_png()
_VIEWPORT = (2, 2)


class NullVision:
    """Vision facade stub: stable frame, every text check passes.

    Sufficient for pure API-tier workflows (no GUI resolution happens); also
    reports ``settled=True`` so ``require_settled=True`` never trips on the
    (unused) GUI path.
    """

    def wait_settled(self, backend: Any, **_kw: Any) -> bytes:
        return backend.screenshot()

    def wait_settled_result(self, backend: Any, **_kw: Any) -> Any:
        return SimpleNamespace(png=backend.screenshot(), settled=True)

    def text_present(self, *_a: Any, **_kw: Any) -> bool:
        return True

    def find_text(self, *_a: Any, **_kw: Any) -> None:
        return None

    def find_template(self, *_a: Any, **_kw: Any) -> None:
        return None

    def find_structural_template(self, *_a: Any, **_kw: Any) -> None:
        return None

    def ocr(self, *_a: Any, **_kw: Any) -> list[Any]:
        return []

    def pixels_changed(self, *_a: Any, **_kw: Any) -> bool:
        return True

    def phash_png(self, *_a: Any, **_kw: Any) -> str:
        return "aa"

    def phash_distance(self, *_a: Any, **_kw: Any) -> int:
        return 0


class NullBackend:
    """Backend stub for API-tier-only replays (no GUI action is delivered)."""

    @property
    def viewport(self) -> tuple[int, int]:
        return _VIEWPORT

    def screenshot(self) -> bytes:
        return _PNG

    def click(self, *_a: Any, **_kw: Any) -> None:
        pass

    def type_text(self, *_a: Any, **_kw: Any) -> None:
        pass

    def press(self, *_a: Any, **_kw: Any) -> None:
        pass

    def scroll(self, *_a: Any, **_kw: Any) -> None:
        pass


# -- surface-routed composite effect verifier --------------------------------


def surface_of(effect: Effect, default: str) -> str:
    """The record surface an effect targets, from its ``probe`` marker."""
    probe = effect.probe or ""
    if probe.startswith("surface="):
        return probe[len("surface=") :].split("|", 1)[0].strip()
    return default


class SurfaceRoutedVerifier:
    """Route each typed effect to the separate verifier for its surface.

    Generalizes ``benchmark.effect_e2e.verifiers.CompositeSqlVerifier`` to a
    heterogeneous verifier map (read-only SQL, REST record oracle, file/maildir
    arrival, CSV row oracle), and advertises the routed sub-verifier's
    evidence tier via ``verification_tier_for`` so a governed standard-profile
    run can classify the confirmed evidence.
    """

    substrate = "composite-multi"

    def __init__(self, verifiers: dict[str, Any], *, default_surface: str) -> None:
        self._verifiers = verifiers
        self._default = default_surface

    def verification_tier_for(self, effect: Any) -> Optional[VerificationTier]:
        if effect is None:
            return VerificationTier.INDEPENDENT_SYSTEM
        verifier = self._verifiers.get(surface_of(effect, self._default))
        if verifier is None:
            return None
        return verifier_effect_tier(verifier, effect)

    def capture_pre_state(self, context: Any = None) -> EffectState:
        detail: dict[str, Any] = {}
        reachable = True
        for name, verifier in self._verifiers.items():
            state = verifier.capture_pre_state(context)
            detail[name] = {"reachable": state.reachable, "records": state.records}
            reachable = reachable and state.reachable
        return EffectState(
            substrate=self.substrate, reachable=reachable, records=[], detail=detail
        )

    def verify(
        self, expected: Effect, before: EffectState, context: Any = None
    ) -> EffectVerdict:
        name = surface_of(expected, self._default)
        verifier = self._verifiers[name]
        sub = before.detail.get(name, {})
        sub_before = EffectState(
            substrate=verifier.substrate,
            reachable=bool(sub.get("reachable", False)),
            records=list(sub.get("records", [])),
        )
        return verifier.verify(expected, sub_before, context)


class CsvRecordVerifier:
    """Out-of-band spreadsheet oracle: RE-READ a CSV file as the record surface.

    Each data row flattens to a dict keyed by the header row, so the standard
    ``Effect(kind=RECORD_WRITTEN, match={...}, count_new_only=True)`` contract
    reads "exactly one NEW row for this key was appended by this action". A
    missing file counts as an EMPTY readable surface only while the parent
    directory exists; a missing directory or an unparsable file is an
    unreachable system of record (INDETERMINATE, fail-safe HALT).
    """

    substrate = "csv"
    verification_tier = VerificationTier.INDEPENDENT_SYSTEM

    def __init__(
        self,
        path: Path | str,
        *,
        timeout_s: float = 0.3,
        poll_interval_s: float = 0.02,
    ) -> None:
        self.path = Path(path)
        self.timeout_s = timeout_s
        self.poll_interval_s = poll_interval_s

    def _scan(self) -> Optional[list[dict[str, Any]]]:
        if not self.path.parent.is_dir():
            return None
        if not self.path.exists():
            return []
        try:
            with self.path.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
        except (OSError, csv.Error, UnicodeDecodeError):
            return None
        records: list[dict[str, Any]] = []
        for i, row in enumerate(rows):
            record: dict[str, Any] = {k: v for k, v in row.items() if k is not None}
            record["id"] = f"row-{i}-" + "|".join(
                f"{k}={record[k]}" for k in sorted(record)
            )
            records.append(record)
        return records

    def capture_pre_state(self, context: Any = None) -> EffectState:
        records = self._scan()
        return EffectState(
            substrate=self.substrate,
            reachable=records is not None,
            records=records or [],
            detail={"path": str(self.path)},
        )

    def verify(
        self, expected: Effect, before: EffectState, context: Any = None
    ) -> EffectVerdict:
        deadline = time.monotonic() + max(0.0, expected.timeout_s)
        while True:
            current = self._scan()
            last = judge_records(expected, before, current, substrate=self.substrate)
            if last.confirmed or time.monotonic() >= deadline:
                return last
            time.sleep(self.poll_interval_s)


# -- deterministic email fixtures (maildir) ----------------------------------

MAILDIR_SUBDIRS = ("new", "cur", "tmp")


def init_maildir(root: Path) -> Path:
    """Create an empty maildir layout under ``root`` and return it."""
    for sub in MAILDIR_SUBDIRS:
        (root / sub).mkdir(parents=True, exist_ok=True)
    return root


def write_maildir_message(root: Path, name: str, message: bytes) -> Path:
    """Deliver ``message`` into ``root/new/<name>`` (deterministic filename)."""
    target = root / "new" / name
    tmp = root / "tmp" / name
    tmp.write_bytes(message)
    tmp.replace(target)
    return target


def read_maildir_messages(root: Path) -> dict[str, bytes]:
    """Every message in the maildir (new + cur), keyed by filename."""
    out: dict[str, bytes] = {}
    for sub in ("new", "cur"):
        folder = root / sub
        if not folder.is_dir():
            continue
        for path in sorted(folder.iterdir()):
            if path.is_file():
                out[path.name] = path.read_bytes()
    return out


def build_request_email(
    *,
    from_addr: str,
    to_addr: str,
    subject: str,
    body: str,
    message_id: str,
    pdf_name: Optional[str] = None,
    pdf_bytes: Optional[bytes] = None,
) -> bytes:
    """A deterministic RFC822 message, optionally with one PDF attachment."""
    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg["Date"] = formatdate(0, usegmt=True)  # fixed epoch date: deterministic
    msg["Message-ID"] = message_id
    msg.set_content(body)
    if pdf_bytes is not None:
        msg.add_attachment(
            pdf_bytes,
            maintype="application",
            subtype="pdf",
            filename=pdf_name or "attachment.pdf",
        )
    return msg.as_bytes()


def parse_email(message: bytes) -> tuple[EmailMessage, list[tuple[str, bytes]]]:
    """Parse an RFC822 message; return (message, [(attachment name, bytes)])."""
    parsed = BytesParser().parsebytes(message)
    attachments: list[tuple[str, bytes]] = []
    for part in parsed.walk():
        if part.get_content_disposition() == "attachment":
            payload = part.get_payload(decode=True)
            if isinstance(payload, bytes):
                attachments.append((part.get_filename() or "", payload))
    return parsed, attachments  # type: ignore[return-value]


# -- deterministic minimal PDF fixtures --------------------------------------


def make_pdf(lines: list[str]) -> bytes:
    """A minimal valid single-page PDF whose text content is ``lines``.

    Uncompressed Helvetica text, one line per ``Tj``. Parentheses and
    backslashes are stripped from input lines so the content stream never
    needs escaping (fixture data contains none).
    """
    safe = [line.replace("(", "").replace(")", "").replace("\\", "") for line in lines]
    content = "BT /F1 11 Tf 72 720 Td\n"
    for i, line in enumerate(safe):
        if i:
            content += "0 -14 Td\n"
        content += f"({line}) Tj\n"
    content += "ET\n"
    stream = content.encode("latin-1")

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length "
        + str(len(stream)).encode()
        + b" >>\nstream\n"
        + stream
        + b"endstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for i, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref_at = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (
        b"trailer\n<< /Size "
        + str(len(objects) + 1).encode()
        + b" /Root 1 0 R >>\nstartxref\n"
        + str(xref_at).encode()
        + b"\n%%EOF\n"
    )
    return bytes(out)


def extract_pdf_lines(data: bytes) -> list[str]:
    """Extract the ``(...) Tj`` text lines from a :func:`make_pdf` document."""
    text = data.decode("latin-1", errors="replace")
    start = text.find("stream\n")
    end = text.find("endstream", start)
    if start < 0 or end < 0:
        return []
    body = text[start + len("stream\n") : end]
    lines: list[str] = []
    for chunk in body.split(") Tj"):
        open_at = chunk.rfind("(")
        if open_at >= 0:
            lines.append(chunk[open_at + 1 :])
    return lines


def parse_kv_lines(lines: list[str]) -> dict[str, str]:
    """Parse ``KEY: value`` document lines into a dict (fixture convention)."""
    out: dict[str, str] = {}
    for line in lines:
        if ":" in line:
            key, value = line.split(":", 1)
            out[key.strip().lower()] = value.strip()
    return out


# -- direct persisted-state adjudication ------------------------------------


def unexpected_record_change_violations(
    before: dict[str, list[dict[str, Any]]],
    after: dict[str, list[dict[str, Any]]],
    *,
    key_fields: dict[str, str],
    allowed_keys: dict[str, set[str]],
    excluded_tables: frozenset[str] = frozenset(),
    prefix: str = "",
) -> list[str]:
    """Return changes outside an explicit table/key transition allowlist.

    Every non-excluded table is covered.  Known business tables are grouped by
    their stable record key so inserts, deletes, and in-place edits to an
    unrelated record are caught.  An unrecognized table is fail-closed: its
    complete canonical row set must remain byte-for-byte equivalent.

    Values for allowed records are still checked by each benchmark's separate
    business-state oracle; this helper ensures that *nothing else* changed.
    """

    def canonical(rows: list[dict[str, Any]]) -> list[str]:
        return sorted(
            json.dumps(row, sort_keys=True, separators=(",", ":"), default=str)
            for row in rows
        )

    def grouped(
        rows: list[dict[str, Any]], key_field: str
    ) -> dict[str, list[dict[str, Any]]]:
        result: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            result.setdefault(str(row.get(key_field)), []).append(row)
        return result

    violations: list[str] = []
    for table in sorted(set(before) | set(after)):
        if table in excluded_tables:
            continue
        label = f"{prefix}.{table}" if prefix else table
        before_rows = before.get(table, [])
        after_rows = after.get(table, [])
        key_field = key_fields.get(table)
        if key_field is None:
            if canonical(before_rows) != canonical(after_rows):
                violations.append(f"unexpected_table_change:{label}")
            continue
        before_by_key = grouped(before_rows, key_field)
        after_by_key = grouped(after_rows, key_field)
        permitted = allowed_keys.get(table, set())
        for key in sorted(set(before_by_key) | set(after_by_key)):
            if key in permitted:
                continue
            if canonical(before_by_key.get(key, [])) != canonical(
                after_by_key.get(key, [])
            ):
                violations.append(f"unexpected_record_change:{label}:{key}")
    return violations


# -- governed authorization helper -------------------------------------------


def standard_authorization(
    workflow: Workflow,
    *,
    bundle_dir: Path,
    effect_verifier: object,
    api_actuator: object,
    params: Optional[dict[str, str]],
    worklists: Optional[dict[str, list[dict[str, str]]]],
) -> GovernedRunAuthorization:
    """Admit and bind one run through the real Standard-profile gate.

    The benchmark policy is intentionally scoped to deterministic API-tier
    fixtures: every consequential write must carry an exact API identity
    binding and a declared system-effect contract.  ``evaluate_run_gate`` then
    applies the named Standard profile (certification, identity/effect
    coverage, verifier tier, durability, settling, and manifest integrity).
    ``build_runtime_authorization`` binds that admission to the exact sealed
    bundle and runtime inputs; any later drift is refused by the replayer.
    """
    assert workflow.manifest is not None, "seal the bundle before authorizing"
    policy = Path(__file__).with_name("multiapp-standard.yaml")
    gate = evaluate_run_gate(
        workflow,
        bundle_dir=bundle_dir,
        deployment=DeploymentConfig(
            policy=PolicySection(policy=str(policy)),
        ),
        effect_verifier=effect_verifier,
        api_actuator=api_actuator,
        policy_source=str(policy),
        profile_contract=execution_profile_contract("standard"),
        effective_durable=True,
        effective_require_settled=True,
    )
    if not gate.passed:
        raise RuntimeError(gate.render())
    return build_runtime_authorization(
        workflow,
        gate,
        approval_source="benchmark-standard-run-gate",
        params=params,
        worklists=worklists,
    )
