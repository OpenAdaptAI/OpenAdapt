"""The local Run Receipt: a shareable artifact built from an ALLOW-LIST.

A run's ``REPORT.md`` is the operator's artifact and is deliberately rich: it
embeds a before/after screenshot per step, the plaintext values that were
typed, the OCR text harvested for identity checks, resolved coordinates, and
the run's parameters.  Every one of those is a PHI or secret carrier.  Sharing
one from a live system of record is a reportable breach, which is exactly what
:class:`openadapt_flow.privacy.PlaintextPHIWarning` warns about.

Therefore the shareable artifact is **generated additively from a closed
allow-list, never redacted subtractively from the operator artifact.**
Subtractive redaction of a run report is unwinnable: burned-in pixels, OCR text
that was captured *precisely because* it identifies a record, and free-form
halt reasons all leak, and one missed field is a breach.

The shape follows the reviewed precedent already shipped in
``openadapt-types`` 0.6.0's portable human-decision contract, which permits only
closed enums, counts, digests, opaque identifiers, expiry, and allowed actions,
with no screenshot, OCR text, raw value, or free-form field.

Nothing outside :class:`RunReceipt` may serialize.  The model is
``extra="forbid"``, so an unknown key is REFUSED rather than silently dropped,
and every field is either a closed enum, a bounded count, a digest, a version
string, or one operator-typed label that is never derived from the recording.

Explicitly forbidden and structurally unrepresentable here: screenshots, OCR
text, typed values, parameters, URLs, hostnames, coordinates, application name,
organization name, user name, workflow name, step intents, and halt free text.

The receipt is a LOCAL file.  Nothing in this module performs network I/O.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from openadapt_flow.verification import VerificationTier

#: Schema identifier carried by every emitted receipt.
RECEIPT_SCHEMA = "openadapt.run-receipt/v1"

#: Terminal execution outcomes a receipt may state.
ReceiptOutcome = Literal[
    "VERIFIED",
    "COMPLETED_UNVERIFIED",
    "HALTED",
    "FAILED",
    "ROLLED_BACK",
]

#: Closed terminal transaction classes (``openadapt_flow.transaction``).  This
#: is the ONLY halt description a receipt carries -- never the free-text reason.
ReceiptHaltClass = Literal[
    "VERIFIED",
    "HALTED_BEFORE_EFFECT",
    "RECONCILIATION_REQUIRED",
    "FAILED_PLATFORM",
    "CANCELED",
    "REJECTED_POLICY",
    "COMPLETED_UNVERIFIED",
    "ROLLED_BACK",
]

#: Resolution rungs the ladder may report (mirrors the hosted rail's closed set).
ReceiptRung = Literal[
    "structural",
    "template",
    "template_global",
    "geometry",
    "ocr",
    "grounder",
    "api",
]

#: Evidence classes the outcome envelope may carry.
ReceiptEvidenceClass = Literal[
    "authorization",
    "identity",
    "postcondition",
    "effect_tier_1",
    "effect_tier_2",
    "effect_tier_3",
    "effect_tier_4",
    "model",
    "compensation",
]

#: Strongest effect evidence actually reached, or ``none``.
ReceiptEffectTier = Literal[
    "none",
    "independent_system",
    "independent_session",
    "persisted_state_reacquisition",
    "immediate_screen",
]

ReceiptSubstrate = Literal["web", "windows", "macos", "linux", "rdp", "citrix"]

#: ``synthetic-tutorial`` marks a run against the bundled synthetic application,
#: which contains no real data BY CONSTRUCTION.  Anything else is ``production``
#: and is subject to the sanitize/approve egress flow before it may be published.
ReceiptProvenance = Literal["synthetic-tutorial", "production"]

_TIER_NAMES: dict[int, ReceiptEffectTier] = {
    int(VerificationTier.INDEPENDENT_SYSTEM): "independent_system",
    int(VerificationTier.INDEPENDENT_SESSION): "independent_session",
    int(VerificationTier.PERSISTED_STATE_REACQUISITION): (
        "persisted_state_reacquisition"
    ),
    int(VerificationTier.IMMEDIATE_SCREEN): "immediate_screen",
}

#: Upper bound on the operator-typed label.  A label is the single free-text
#: field on the receipt; it is typed by the person publishing, never derived
#: from the recording, and it is bounded so it cannot become a payload.
MAX_LABEL_CHARS = 80


class ReceiptError(ValueError):
    """A receipt could not be built or written."""


class RunReceipt(BaseModel):
    """The complete, closed set of fields a shareable receipt may contain.

    ``extra="forbid"``: an unknown key is refused, not dropped.  This is the
    load-bearing property -- the receipt is safe because nothing outside this
    declaration can reach it, not because something was removed.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["openadapt.run-receipt/v1"] = "openadapt.run-receipt/v1"

    #: Terminal outcome.  A closed enum, straight from the run report.
    outcome: ReceiptOutcome
    #: Terminal transaction class.  Closed enum; NEVER the free-text reason.
    halt_class: Optional[ReceiptHaltClass] = None
    #: Whether the outcome is a chargeable business result.  ``COMPLETED_UNVERIFIED``
    #: is never billable and never counts as success.
    billable: bool = False

    #: Operator-typed, optional, bounded.  Never derived from the recording.
    label: Optional[str] = None

    steps_total: int = Field(ge=0)
    steps_ok: int = Field(ge=0)
    heals: int = Field(ge=0)
    model_calls: int = Field(ge=0)
    est_cost_usd: float = Field(ge=0.0)
    duration_ms: int = Field(ge=0)

    rung_histogram: dict[ReceiptRung, int] = Field(default_factory=dict)
    evidence_classes: list[ReceiptEvidenceClass] = Field(default_factory=list)
    effect_tier_reached: ReceiptEffectTier = "none"
    effects_required: int = Field(default=0, ge=0)
    effects_confirmed: int = Field(default=0, ge=0)

    #: Identity-armed steps over identity-applicable steps, as counts only.
    identity_armed: int = Field(default=0, ge=0)
    identity_applicable: int = Field(default=0, ge=0)

    #: The counter-metric, always present: consequential steps this run stopped
    #: on even though the independent verifier had CONFIRMED the effect.  A
    #: receipt that reports successes without reporting over-halts is worth
    #: nothing, so this field is not optional.
    over_halt_count: int = Field(default=0, ge=0)

    substrate: Optional[ReceiptSubstrate] = None
    provenance: ReceiptProvenance
    flow_version: str
    launcher_version: Optional[str] = None

    #: Exact content digest of the bundle that ran.  This is what makes the
    #: receipt checkable: a third party can run the same public bundle and
    #: compare digests.  Verifiability is the point.
    bundle_digest: Optional[str] = Field(default=None, pattern="^[a-f0-9]{64}$")
    #: SHA-256 over the canonical JSON of every OTHER field.  Set by
    #: :func:`build_receipt`; a caller never supplies it.
    receipt_digest: Optional[str] = Field(default=None, pattern="^[a-f0-9]{64}$")

    #: Truncated to the hour, UTC.  Minute/second resolution is a correlation
    #: handle that a receipt does not need.
    generated_at: str

    @field_validator("label")
    @classmethod
    def _bounded_single_line_label(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        text = value.strip()
        if not text:
            return None
        if len(text) > MAX_LABEL_CHARS:
            raise ValueError(
                f"label exceeds {MAX_LABEL_CHARS} characters; a receipt label "
                "is a title, not a payload"
            )
        if any(char < " " or char == "\x7f" for char in text):
            raise ValueError("label must be a single line of printable text")
        return text

    def canonical_json(self, *, include_digest: bool = True) -> bytes:
        """Deterministic bytes for hashing and for the publish diff."""

        payload = self.model_dump(mode="json", exclude_none=True)
        if not include_digest:
            payload.pop("receipt_digest", None)
        return json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")


def _hour_utc(value: Optional[str]) -> str:
    """Parse ``value`` and truncate to the hour in UTC."""

    if value:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            parsed = datetime.now(timezone.utc)
    else:
        parsed = datetime.now(timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (
        parsed.astimezone(timezone.utc)
        .replace(minute=0, second=0, microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _strongest_confirmed_tier(report: Any) -> ReceiptEffectTier:
    """Strongest (numerically lowest) tier among CONFIRMED effect evidence."""

    best: Optional[int] = None
    for result in report.results:
        for evidence in result.effect_evidence:
            if evidence.final_verdict != "confirmed":
                continue
            tier = evidence.verification_tier
            if tier is None:
                continue
            best = int(tier) if best is None else min(best, int(tier))
    if best is None:
        return "none"
    return _TIER_NAMES.get(best, "none")


def _over_halt_count(report: Any) -> int:
    """Steps this run stopped on although the effect was CONFIRMED.

    An over-halt is a refusal that cost the operator a completed task without
    preventing a wrong write: the independent verifier had already confirmed
    the declared effect, and the run halted anyway.  Counting it here keeps the
    receipt honest -- the same discipline the safety claims are measured by.
    """

    count = 0
    for result in report.results:
        halted = bool(result.safety_halt) or result.failure_category in {
            "governed_refusal",
            "safety_halt",
        }
        if not halted:
            continue
        if any(
            evidence.final_verdict == "confirmed" for evidence in result.effect_evidence
        ):
            count += 1
    return count


def build_receipt(
    report: Any,
    *,
    provenance: ReceiptProvenance,
    label: Optional[str] = None,
    launcher_version: Optional[str] = None,
) -> RunReceipt:
    """Project a :class:`~openadapt_flow.ir.RunReport` onto the allow-list.

    Every value is read from a typed field of the report.  No free text, no
    parameter, no image path, no URL, and no workflow name is consulted.

    Args:
        report: The completed :class:`~openadapt_flow.ir.RunReport`.
        provenance: ``synthetic-tutorial`` only when the run executed against
            the bundled synthetic application; otherwise ``production``.
        label: Optional operator-typed title.  Never derived from the run.
        launcher_version: Optional launcher version when a launcher drove this
            run; omitted when unknown rather than guessed.

    Returns:
        The receipt, with :attr:`RunReceipt.receipt_digest` bound to the
        canonical JSON of every other field.

    Raises:
        ReceiptError: the report carries no classified execution outcome, so
            there is no outcome the receipt could truthfully state.
    """

    outcome = getattr(report, "execution_outcome", None)
    if outcome is None:
        raise ReceiptError(
            "run report carries no classified execution outcome; a receipt "
            "never invents one"
        )

    envelope = getattr(report, "outcome_envelope", None)
    evidence_classes: list[str] = []
    effects_required = 0
    effects_confirmed = 0
    if envelope is not None:
        evidence_classes = sorted(envelope.evidence_classes)
        effects_required = int(envelope.required_contracts.effect)
        effects_confirmed = int(envelope.passed_contracts.effect)

    rungs: dict[str, int] = {}
    for rung, count in dict(report.rung_counts).items():
        if rung not in ReceiptRung.__args__:  # type: ignore[attr-defined]
            raise ReceiptError(f"report contains an unknown resolution rung: {rung!r}")
        rungs[rung] = int(count)

    draft = RunReceipt(
        outcome=outcome,
        halt_class=getattr(report, "transaction_outcome", None),
        billable=bool(getattr(report, "transaction_billable", False)),
        label=label,
        steps_total=len(report.results),
        steps_ok=sum(1 for result in report.results if result.ok),
        heals=int(report.heal_count),
        model_calls=int(report.model_calls),
        est_cost_usd=round(float(report.est_model_cost_usd), 6),
        duration_ms=int(round(float(report.total_ms))),
        rung_histogram=rungs,  # type: ignore[arg-type]
        evidence_classes=evidence_classes,  # type: ignore[arg-type]
        effect_tier_reached=_strongest_confirmed_tier(report),
        effects_required=effects_required,
        effects_confirmed=effects_confirmed,
        identity_armed=int(report.identity_armed_steps),
        identity_applicable=int(report.identity_applicable_steps),
        over_halt_count=_over_halt_count(report),
        substrate=getattr(report, "execution_target_kind", None),
        provenance=provenance,
        flow_version=_flow_version(),
        launcher_version=launcher_version,
        bundle_digest=getattr(report, "bundle_content_digest", None),
        generated_at=_hour_utc(getattr(report, "started_at", None)),
    )
    digest = hashlib.sha256(draft.canonical_json(include_digest=False)).hexdigest()
    return draft.model_copy(update={"receipt_digest": digest})


def _flow_version() -> str:
    from openadapt_flow import __version__

    return __version__


def render_receipt_markdown(receipt: RunReceipt) -> str:
    """Render the receipt as a small, postable Markdown card.

    Reads only :class:`RunReceipt` -- it cannot widen the allow-list.
    """

    identity = f"{receipt.identity_armed} of {receipt.identity_applicable}"
    effects = f"{receipt.effects_confirmed} of {receipt.effects_required}"
    ladder = (
        ", ".join(
            f"{rung} {count}" for rung, count in sorted(receipt.rung_histogram.items())
        )
        or "none recorded"
    )
    lines = [
        f"# {receipt.outcome} — OpenAdapt run receipt",
        "",
    ]
    if receipt.label:
        lines += [f"**{receipt.label}**", ""]
    lines += [
        "| | |",
        "|---|---|",
        f"| outcome | `{receipt.outcome}` |",
        f"| transaction | `{receipt.halt_class}` |",
        f"| billable | {'yes' if receipt.billable else 'no'} |",
        f"| steps | {receipt.steps_ok} of {receipt.steps_total} ok |",
        f"| model calls | **{receipt.model_calls}** |",
        f"| est. model cost | ${receipt.est_cost_usd:.6f} |",
        f"| duration | {receipt.duration_ms} ms |",
        f"| heals | {receipt.heals} |",
        f"| effects confirmed | {effects} |",
        f"| strongest effect evidence | `{receipt.effect_tier_reached}` |",
        f"| identity-armed steps | {identity} |",
        f"| over-halts | {receipt.over_halt_count} |",
        f"| resolution ladder | {ladder} |",
        f"| substrate | `{receipt.substrate}` |",
        f"| provenance | `{receipt.provenance}` |",
        f"| flow version | {receipt.flow_version} |",
    ]
    if receipt.launcher_version:
        lines.append(f"| launcher version | {receipt.launcher_version} |")
    lines += [
        f"| bundle digest | `{receipt.bundle_digest}` |",
        f"| receipt digest | `{receipt.receipt_digest}` |",
        f"| generated | {receipt.generated_at} |",
        "",
        "Run the same public bundle yourself and compare the bundle digest.",
        "",
        "This receipt is generated from a closed allow-list. It contains no "
        "screenshot, no OCR text, no typed value, no parameter, no URL, no "
        "hostname, no coordinate, and no free-form halt reason.",
    ]
    return "\n".join(lines) + "\n"


_CARD_BG = (16, 18, 22)
_CARD_FG = (233, 236, 241)
_CARD_MUTED = (140, 148, 160)
_CARD_OK = (86, 204, 132)
_CARD_WARN = (232, 176, 84)


def _receipt_rows(receipt: RunReceipt) -> list[tuple[str, str]]:
    """The rendered rows, derived ONLY from :class:`RunReceipt` fields."""

    ladder = (
        "  ".join(
            f"{rung} {count}" for rung, count in sorted(receipt.rung_histogram.items())
        )
        or "none recorded"
    )
    rows = [
        ("transaction", str(receipt.halt_class)),
        ("steps", f"{receipt.steps_ok} of {receipt.steps_total} ok"),
        ("model calls", str(receipt.model_calls)),
        ("est. model cost", f"${receipt.est_cost_usd:.6f}"),
        ("duration", f"{receipt.duration_ms} ms"),
        ("heals", str(receipt.heals)),
        (
            "effects confirmed",
            f"{receipt.effects_confirmed} of {receipt.effects_required}",
        ),
        ("effect evidence", receipt.effect_tier_reached),
        (
            "identity-armed",
            f"{receipt.identity_armed} of {receipt.identity_applicable}",
        ),
        ("over-halts", str(receipt.over_halt_count)),
        ("ladder", ladder),
        ("substrate", str(receipt.substrate)),
        ("provenance", receipt.provenance),
        ("flow", receipt.flow_version),
    ]
    if receipt.launcher_version:
        rows.append(("launcher", receipt.launcher_version))
    rows += [
        ("bundle digest", (receipt.bundle_digest or "")[:16] or "unbound"),
        ("receipt digest", (receipt.receipt_digest or "")[:16] or "unbound"),
        ("generated", receipt.generated_at),
    ]
    return rows


def render_receipt_png(receipt: RunReceipt) -> Any:
    """Render the receipt as a small dark card (a Pillow ``Image``).

    Reads only :class:`RunReceipt`, so it cannot widen the allow-list.
    """

    from PIL import Image, ImageDraw, ImageFont

    def font(size: int) -> Any:
        try:
            return ImageFont.load_default(size=size)
        except TypeError:  # pragma: no cover - very old Pillow
            return ImageFont.load_default()

    title_font = font(34)
    label_font = font(15)
    value_font = font(15)
    foot_font = font(12)

    rows = _receipt_rows(receipt)
    pad = 28
    row_h = 24
    width = 620
    height = pad + 48 + (26 if receipt.label else 0) + len(rows) * row_h + pad + 30

    image = Image.new("RGB", (width, height), _CARD_BG)
    draw = ImageDraw.Draw(image)
    accent = _CARD_OK if receipt.outcome == "VERIFIED" else _CARD_WARN
    draw.rectangle([(0, 0), (6, height)], fill=accent)

    y = pad
    draw.text((pad, y), receipt.outcome, font=title_font, fill=accent)
    y += 44
    if receipt.label:
        draw.text((pad, y), receipt.label, font=label_font, fill=_CARD_FG)
        y += 26

    for key, value in rows:
        draw.text((pad, y), key, font=label_font, fill=_CARD_MUTED)
        draw.text((pad + 190, y), value, font=value_font, fill=_CARD_FG)
        y += row_h

    draw.text(
        (pad, height - pad - 4),
        "no screenshot, no OCR text, no typed value, no parameter, no URL",
        font=foot_font,
        fill=_CARD_MUTED,
    )
    return image


def write_receipt(receipt: RunReceipt, out_dir: Path | str) -> dict[str, Path]:
    """Write ``receipt.json``, ``receipt.md``, and ``receipt.png`` locally.

    Purely local: this function performs no network I/O and never uploads.
    """

    directory = Path(out_dir)
    directory.mkdir(parents=True, exist_ok=True)
    json_path = directory / "receipt.json"
    json_path.write_bytes(receipt.canonical_json() + b"\n")
    md_path = directory / "receipt.md"
    md_path.write_text(render_receipt_markdown(receipt), encoding="utf-8")
    png_path = directory / "receipt.png"
    render_receipt_png(receipt).save(png_path, format="PNG")
    return {"json": json_path, "markdown": md_path, "png": png_path}
