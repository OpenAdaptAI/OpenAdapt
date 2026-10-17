"""Email-delivery verification against a maildir / SMTP-capture sink.

Many consequential workflows end in an EMAIL leaving the system -- a claim
acknowledgment, a statement, a referral letter. The screen saying "Sent"
proves nothing; the truth is whether a conforming message actually reached the
delivery capture point. This adapter reads a LOCAL mail store that an
SMTP-capture sink writes (a real ``Maildir`` -- ``cur``/``new``/``tmp`` -- or
a plain directory of ``.eml`` RFC-822 files, as produced by MailHog/Mailpit
file export, Postfix ``virtual`` delivery, or a test double), flattens each
message into a record, and judges it with the shared judge like every other
substrate.

Each message flattens to::

    {"id": <file name>, "message_id": ..., "to": ..., "from": ...,
     "subject": ..., "body_sha256": ..., "content_match": "True"/"False",
     "fresh": "True"/"False", "mtime": <epoch seconds>}

so the standard contract is entity-bound recipient + at-most-once::

    Effect(kind=RECORD_WRITTEN,
           match={"to": {"param": "recipient"}, "content_match": "True",
                  "fresh": "True"},
           expected_count=1, count_new_only=True)

-- "exactly one NEW conforming message to THIS run's recipient" (a duplicate
send shows up as two new records -> REFUTED; a message to a different
recipient never matches -> the wrong-entity fault is caught, and a
``collateral hook`` can additionally refuse ANY new message to an unexpected
recipient).

This adapter verifies DELIVERY TO THE CAPTURE POINT, not end-to-end receipt:
it is deterministic and CI-testable with no external service, and it is the
honest boundary -- what left the system, to whom, with what content.

Fail-safe: a missing/unscannable store or an unreadable message file reads as
*unreadable* -> INDETERMINATE (UNAVAILABLE) -> HALT (a vanished outbox is
never "no mail expected").
"""

from __future__ import annotations

import hashlib
import re
import time
from email import policy as email_policy
from email.message import Message
from email.parser import BytesParser
from pathlib import Path
from typing import Any, Optional

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

#: How many body bytes the optional content probe inspects.
BODY_PROBE_LIMIT = 1 << 20  # 1 MiB
#: Maximum RFC-822 message size accepted by this verifier. The cap is applied
#: while reading, so a malicious or accidental giant message cannot exhaust
#: runner memory before the verifier refuses it as unreadable.
MESSAGE_READ_LIMIT = 8 << 20  # 8 MiB


def _body_text(message: Message) -> str:
    """Best-effort text body: all ``text/*`` leaf parts, joined."""
    parts: list[str] = []
    for part in message.walk():
        if part.is_multipart():
            continue
        if not part.get_content_type().startswith("text/"):
            continue
        try:
            payload = part.get_payload(decode=True)
        except Exception:  # noqa: BLE001 - a broken part contributes nothing
            payload = None
        if isinstance(payload, bytes):
            parts.append(payload.decode("utf-8", errors="replace"))
    return "\n".join(parts)


class MaildirDeliveryVerifier(VerifierAdapterBase):
    """Verify email delivery against a local maildir / SMTP-capture directory.

    Args:
        root: The mail store. A directory containing ``cur``/``new`` is read
            as a Maildir (both subdirectories are scanned); anything else is
            read as a flat directory of RFC-822 files matching ``pattern``.
        pattern: Filename glob for the flat-directory layout.
        mtime_window_s: Freshness window in seconds for the ``fresh`` flag
            (``None`` -> every message is fresh).
        content_probe: Optional regex probed against each message's text body
            (first ``BODY_PROBE_LIMIT`` bytes) -> the ``content_match`` flag.
        redaction: Field-level evidence-minimization policy (subjects and
            recipients are routinely PII -- redact what the audit trail does
            not need).
        collateral_hooks: Substrate-specific collateral checks (e.g. "no new
            message to any OTHER recipient").
        poll_interval_s: Settlement poll gap within ``Effect.timeout_s``.
        clock: Injectable epoch-seconds source (tests).
    """

    substrate = "email"
    verification_tier = VerificationTier.INDEPENDENT_SYSTEM

    def __init__(
        self,
        root: str,
        *,
        pattern: str = "*.eml",
        mtime_window_s: Optional[float] = None,
        content_probe: Optional[str] = None,
        redaction: Optional[RedactionPolicy] = None,
        collateral_hooks: tuple[CollateralHook, ...] = (),
        poll_interval_s: float = 0.2,
        clock: Any = time.time,
    ) -> None:
        self.root = Path(root)
        self.pattern = pattern
        self.mtime_window_s = mtime_window_s
        self.content_probe = re.compile(content_probe) if content_probe else None
        self.redaction = redaction
        self.collateral_hooks = collateral_hooks
        self.poll_interval_s = poll_interval_s
        self._clock = clock

    # -- scanning -----------------------------------------------------------

    def _candidate_files(self) -> Optional[list[Path]]:
        if not self.root.is_dir():
            return None
        cur, new = self.root / "cur", self.root / "new"
        if cur.is_dir() or new.is_dir():
            files: list[Path] = []
            for sub in (cur, new):
                if sub.is_dir():
                    files.extend(p for p in sub.iterdir() if p.is_file())
            return sorted(files)
        return sorted(p for p in self.root.glob(self.pattern) if p.is_file())

    def _flatten(self, path: Path) -> Optional[dict[str, Any]]:
        """One message file -> one judgeable record; ``None`` when unreadable."""
        try:
            with path.open("rb") as stream:
                raw = stream.read(MESSAGE_READ_LIMIT + 1)
            if len(raw) > MESSAGE_READ_LIMIT:
                return None
            mtime = path.stat().st_mtime
        except OSError:
            return None
        try:
            message = BytesParser(policy=email_policy.default).parsebytes(raw)
        except Exception:  # noqa: BLE001 - an unparseable message is unreadable
            return None
        body = _body_text(message)[:BODY_PROBE_LIMIT]
        fresh = True
        if self.mtime_window_s is not None:
            age_s = float(self._clock()) - mtime
            fresh = -self.mtime_window_s <= age_s <= self.mtime_window_s
        content_match = True
        if self.content_probe is not None:
            content_match = self.content_probe.search(body) is not None
        return {
            "id": path.name,
            "message_id": str(message.get("Message-ID", "")).strip(),
            "to": str(message.get("To", "")).strip(),
            "from": str(message.get("From", "")).strip(),
            "subject": str(message.get("Subject", "")).strip(),
            "body_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
            "content_match": str(content_match),
            "fresh": str(fresh),
            "mtime": mtime,
        }

    def _scan(self) -> Optional[list[dict[str, Any]]]:
        """Every message in the store; ``None`` when the store or ANY message
        is unreadable (a partially readable outbox is never judged -- the
        unreadable message might be the one that matters)."""
        files = self._candidate_files()
        if files is None:
            return None
        records: list[dict[str, Any]] = []
        for path in files:
            record = self._flatten(path)
            if record is None:
                return None
            records.append(record)
        return records

    # -- VerifierAdapter lifecycle ------------------------------------------

    def capture_pre_state(self, context: Any = None) -> EffectState:
        records = self._scan()
        return EffectState(
            substrate=self.substrate,
            reachable=records is not None,
            records=records or [],
            detail={"root": str(self.root), "pattern": self.pattern},
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
