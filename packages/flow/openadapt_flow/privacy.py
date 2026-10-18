"""PHI/PII scrubbing shim over the optional ``openadapt-privacy`` dependency.

openadapt-flow processes patient data: identity band text (name / DOB / MRN),
typed field values, OCR of full screenshots, and the human-readable run report.
This module is the single choke point through which every PERSISTED-or-LOGGED
text (and, opt-in, image) passes so PHI can be scrubbed before it lands in a
shareable artifact (``REPORT.md``) or the console.

``openadapt-privacy`` (Presidio-backed) is an **optional** dependency, installed
via the ``privacy`` extra::

    pip install 'openadapt-flow[privacy]'
    python -m spacy download en_core_web_sm

Posture is controlled by two environment variables, safe by default:

* ``OPENADAPT_FLOW_SCRUB`` — ``auto`` (default) | ``on`` | ``off``
    - ``auto``: scrub whenever the capability is installed; if it is not, write
      plaintext (keeps the local demo working). Clearly documented in
      ``docs/PRIVACY.md``.
    - ``on``: scrub, and **fail closed** — raise if the capability is missing.
      This is the setting a compliance team pins for a clinical deployment.
    - ``off``: never scrub (e.g. an already-de-identified fixture corpus).
* ``OPENADAPT_FLOW_SCRUB_IMAGES`` — ``0`` (default) | ``1``
    Opt-in Presidio image redaction of PERSISTED screenshots/crops under
    ``auto``. Off by default there because it is destructive (burns boxes into
    the saved frame) and slow (OCR + NER per frame). **Under ``SCRUB=on`` image
    redaction is implied regardless of this flag** — a compliance-pinned run
    must not leave full-frame PHI screenshots unredacted in the shareable
    ``REPORT.md`` while text is scrubbed (that is a false sense of
    de-identification).

The scrubber is a lazy singleton so importing this module never pulls in
Presidio/spaCy. Tests inject a fast fake via :func:`set_text_scrubber` /
:func:`set_image_scrubber`.
"""

from __future__ import annotations

import io
import os
from typing import TYPE_CHECKING, Optional, Protocol, runtime_checkable

if TYPE_CHECKING:
    from pathlib import Path

__all__ = [
    "PrivacyNotAvailable",
    "scrub_mode",
    "text_scrubbing_enabled",
    "image_redaction_enabled",
    "scrub_text",
    "scrub_params",
    "scrub_image_bytes",
    "get_text_scrubber",
    "get_scrubber",
    "scrubbing_available",
    "set_text_scrubber",
    "set_image_scrubber",
    "reset_scrubbers",
    "OUTBOUND_OPT_OUT_ENV",
    "outbound_opt_out",
    "require_outbound_scrubber",
    "scrub_outbound_text",
    "scrub_outbound_tree",
    "scrub_outbound_image",
    "opt_out_ledger_path",
    "prepare_outbound",
    "prepare_outbound_image",
]


class PrivacyNotAvailable(RuntimeError):
    """Raised when ``OPENADAPT_FLOW_SCRUB=on`` but openadapt-privacy is missing."""


@runtime_checkable
class TextScrubber(Protocol):
    """Minimal text-scrubbing contract (satisfied by PresidioScrubbingProvider)."""

    def scrub_text(self, text: str, is_separated: bool = False) -> str: ...


@runtime_checkable
class ImageScrubber(Protocol):
    """Minimal image-scrubbing contract (satisfied by PresidioScrubbingProvider)."""

    def scrub_image(self, image, fill_color: Optional[int] = None): ...


@runtime_checkable
class Scrubber(TextScrubber, ImageScrubber, Protocol):
    """Combined text+image scrubbing contract (the PresidioScrubbingProvider).

    A single provider does both text and image de-identification, so the
    outbound-upload path (which must scrub recording frames AND text artifacts
    before they leave the machine, regardless of the local console posture) can
    take one capability object rather than two mode-gated getters.
    """


# Cached singletons. ``_UNSET`` distinguishes "not yet built" from "built, but
# unavailable" (``None``).
_UNSET = object()
_text_scrubber: object = _UNSET
_image_scrubber: object = _UNSET


def scrub_mode() -> str:
    """Current scrub mode: ``auto`` (default), ``on``, or ``off``."""
    mode = os.environ.get("OPENADAPT_FLOW_SCRUB", "auto").strip().lower()
    return mode if mode in ("auto", "on", "off") else "auto"


def _build_provider() -> Optional[object]:
    """Instantiate the Presidio provider, or None if the extra is not installed."""
    try:
        from openadapt_privacy.providers.presidio import PresidioScrubbingProvider
    except Exception:  # noqa: BLE001 — any import/env failure => unavailable
        return None
    try:
        return PresidioScrubbingProvider()
    except Exception:  # noqa: BLE001
        return None


def get_text_scrubber() -> Optional[TextScrubber]:
    """Return the text scrubber for the current mode, or None when scrubbing is off/unavailable.

    Raises:
        PrivacyNotAvailable: mode is ``on`` but openadapt-privacy is not installed.
    """
    global _text_scrubber
    mode = scrub_mode()
    if mode == "off":
        return None
    if _text_scrubber is _UNSET:
        _text_scrubber = _build_provider()
    if _text_scrubber is None and mode == "on":
        raise PrivacyNotAvailable(
            "OPENADAPT_FLOW_SCRUB=on but openadapt-privacy is not installed. "
            "Install it with: pip install 'openadapt-flow[privacy]' and "
            "python -m spacy download en_core_web_sm, or set "
            "OPENADAPT_FLOW_SCRUB=auto to write plaintext locally."
        )
    return _text_scrubber  # type: ignore[return-value]


def _get_image_scrubber() -> Optional[ImageScrubber]:
    """Return the image scrubber (Presidio provider), or None when unavailable."""
    global _image_scrubber
    if _image_scrubber is _UNSET:
        _image_scrubber = _build_provider()
    if _image_scrubber is None and scrub_mode() == "on":
        raise PrivacyNotAvailable(
            "OPENADAPT_FLOW_SCRUB=on and OPENADAPT_FLOW_SCRUB_IMAGES=1 but "
            "openadapt-privacy is not installed."
        )
    return _image_scrubber  # type: ignore[return-value]


def text_scrubbing_enabled() -> bool:
    """True when text scrubbing is active (mode on/auto and a scrubber is available)."""
    if scrub_mode() == "off":
        return False
    return get_text_scrubber() is not None


def get_scrubber() -> Optional[Scrubber]:
    """Return the scrubbing provider if the capability is present, else None.

    Unlike :func:`get_text_scrubber`, this is a pure CAPABILITY check: it does
    NOT consult ``OPENADAPT_FLOW_SCRUB`` (so it returns a provider even under
    mode ``off``/``auto``) and it never raises. It is used by the OUTBOUND
    UPLOAD path, which must de-identify a recording's frames and text artifacts
    before they leave the machine regardless of the local-console scrub posture
    (a compliance-critical boundary, not a console convenience). The lazy
    singleton is shared with the text getter; tests inject via
    :func:`set_text_scrubber`.
    """
    global _text_scrubber
    if _text_scrubber is _UNSET:
        _text_scrubber = _build_provider()
    return _text_scrubber  # type: ignore[return-value]


def scrubbing_available() -> bool:
    """True when a scrubbing provider can be built or was injected (capability).

    This is the gate the upload path checks: when it is False, a recording must
    NOT be uploaded to the multi-tenant cloud (fail-closed), because there is no
    way to de-identify its frames/text first.
    """
    return get_scrubber() is not None


def _scrub_images_flag_set() -> bool:
    """True when ``OPENADAPT_FLOW_SCRUB_IMAGES`` is explicitly truthy."""
    return os.environ.get("OPENADAPT_FLOW_SCRUB_IMAGES", "0").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def image_redaction_enabled() -> bool:
    """True when persisted-image (frame) redaction is active.

    ``OPENADAPT_FLOW_SCRUB=on`` **implies** image redaction: a compliance team
    pins ``on`` precisely so no PHI is written unredacted, and the persisted
    step/heal frames embedded in the shareable ``REPORT.md`` are full-frame PHI
    screenshots. Leaving them raw while text is scrubbed is a false sense of
    de-identification, so ``on`` redacts them too (fail-closed: raises via
    :func:`_get_image_scrubber` if the capability is missing, exactly like text).

    Under ``auto`` (default), frame redaction stays **opt-in** via
    ``OPENADAPT_FLOW_SCRUB_IMAGES=1`` (it is destructive and slow). ``off``
    never redacts.
    """
    mode = scrub_mode()
    if mode == "off":
        return False
    if mode != "on" and not _scrub_images_flag_set():
        return False
    return _get_image_scrubber() is not None


def scrub_text(text: Optional[str]) -> Optional[str]:
    """Scrub PII/PHI from a single string, or return it unchanged when scrubbing is off.

    ``None`` and empty strings pass through untouched.
    """
    if not text:
        return text
    scrubber = get_text_scrubber()
    if scrubber is None:
        return text
    try:
        return scrubber.scrub_text(text)
    except Exception as exc:  # noqa: BLE001 - optional provider can be incomplete
        # ``auto`` treats an unusable optional provider the same as an absent
        # provider.  This includes a package whose external NLP model was not
        # installed.  ``on`` remains fail-closed for regulated deployments.
        global _text_scrubber
        _text_scrubber = None
        if scrub_mode() == "on":
            raise PrivacyNotAvailable(
                "OPENADAPT_FLOW_SCRUB=on but the privacy provider could not "
                "scrub text. Install its required models or configure a "
                "working provider before continuing."
            ) from exc
        return text


def scrub_params(params: dict[str, str]) -> dict[str, str]:
    """Scrub the VALUES of a param mapping (keys are field names, kept as-is)."""
    scrubber = get_text_scrubber()
    if scrubber is None:
        return params
    try:
        return {key: scrubber.scrub_text(value) for key, value in params.items()}
    except Exception as exc:  # noqa: BLE001 - optional provider can be incomplete
        global _text_scrubber
        _text_scrubber = None
        if scrub_mode() == "on":
            raise PrivacyNotAvailable(
                "OPENADAPT_FLOW_SCRUB=on but the privacy provider could not "
                "scrub parameters. Install its required models or configure "
                "a working provider before continuing."
            ) from exc
        return params


def scrub_image_bytes(png: bytes, *, force: bool = False) -> bytes:
    """Redact PII/PHI regions from a PNG, or return it unchanged when redaction is off.

    Two modes:

    * ``force=False`` (default): honour the mode gate — only active when
      :func:`image_redaction_enabled` (opt-in under ``auto``, implied under
      ``on``). This is the PERSIST/LOG path; on any redaction error the ORIGINAL
      bytes are returned unchanged (best-effort enhancement layered on the
      documented no-share local posture, never a correctness gate).
    * ``force=True``: the OUTBOUND UPLOAD path — redact whenever a provider is
      available (:func:`get_scrubber`), independent of ``OPENADAPT_FLOW_SCRUB``
      / ``OPENADAPT_FLOW_SCRUB_IMAGES``, and **re-raise on failure** so the
      caller aborts the upload rather than shipping an unredacted frame. The
      caller is responsible for first checking :func:`scrubbing_available`.
    """
    if not png:
        return png
    scrubber: Optional[ImageScrubber]
    if force:
        scrubber = get_scrubber()
    elif image_redaction_enabled():
        scrubber = _get_image_scrubber()
    else:
        return png
    if scrubber is None:
        return png
    try:
        from PIL import Image

        image = Image.open(io.BytesIO(png)).convert("RGB")
        redacted = scrubber.scrub_image(image)
        out = io.BytesIO()
        redacted.save(out, format="PNG")
        return out.getvalue()
    except Exception:  # noqa: BLE001 — never let redaction crash a run
        if force:
            raise
        return png


# -- outbound: anything that leaves the machine ------------------------------
#
# The local posture above (``OPENADAPT_FLOW_SCRUB``) decides what is written to
# THIS machine. Anything sent elsewhere (a model API, an agent endpoint, a
# share) follows a stricter rule that ignores that posture: it is scrubbed, and
# when the privacy extra is missing it is not sent. The only way around that is
# an explicit opt-out with a reason, which is written to a local ledger each
# time it is used.

OUTBOUND_OPT_OUT_ENV = "OPENADAPT_FLOW_SEND_UNSCRUBBED"
OPT_OUT_LEDGER_NAME = "unscrubbed-sends.jsonl"


def opt_out_ledger_path() -> "Path":
    """Local ledger of every unscrubbed send (``~/.openadapt/unscrubbed-sends.jsonl``).

    The root follows ``OPENADAPT_HOME`` like the rest of the engine config.
    """
    from pathlib import Path

    root = os.environ.get("OPENADAPT_HOME")
    base = Path(root) if root else Path.home() / ".openadapt"
    return base / OPT_OUT_LEDGER_NAME


def outbound_opt_out(purpose: str, reason: Optional[str] = None) -> Optional[str]:
    """Return the opt-out reason for an unscrubbed send, recording it, or None.

    ``reason`` is an explicit per-call opt-out (for example a CLI flag). When it
    is not given, ``OPENADAPT_FLOW_SEND_UNSCRUBBED`` is read; its value is the
    reason. An empty or whitespace reason is not an opt-out. Every opt-out that
    is used appends one line to :func:`opt_out_ledger_path` before anything is
    sent, so the record exists even when the send then fails.
    """
    import json
    import time

    chosen = reason if reason is not None else os.environ.get(OUTBOUND_OPT_OUT_ENV)
    chosen = (chosen or "").strip()
    if not chosen:
        return None
    ledger = opt_out_ledger_path()
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with ledger.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "schema": "openadapt.flow.unscrubbed-send/v1",
                    "recorded_at": time.time(),
                    "purpose": purpose,
                    "reason": chosen,
                },
                sort_keys=True,
            )
            + "\n"
        )
    return chosen


def require_outbound_scrubber(purpose: str) -> Scrubber:
    """Return the scrubbing provider for an outbound send, or raise.

    Ignores ``OPENADAPT_FLOW_SCRUB``: ``off`` and ``auto`` relax what is written
    locally, never what is sent.

    Raises:
        PrivacyNotAvailable: the privacy extra is missing, so nothing may be sent.
    """
    scrubber = get_scrubber()
    if scrubber is None:
        raise PrivacyNotAvailable(
            f"Not sending {purpose}: it can contain patient data and "
            "openadapt-privacy is not installed to scrub it. Install it with "
            "pip install 'openadapt-flow[privacy]' and python -m spacy download "
            f"en_core_web_sm, or set {OUTBOUND_OPT_OUT_ENV}='<reason>' to send "
            "it unscrubbed (recorded)."
        )
    return scrubber


def scrub_outbound_text(text: Optional[str], scrubber: Scrubber) -> Optional[str]:
    """Scrub one outbound string. Errors propagate so the send is aborted."""
    if not text:
        return text
    return scrubber.scrub_text(text)


def scrub_outbound_tree(value: object, scrubber: Scrubber) -> object:
    """Scrub every string in a JSON-like value (dict keys included)."""
    if isinstance(value, str):
        return scrub_outbound_text(value, scrubber)
    if isinstance(value, list):
        return [scrub_outbound_tree(item, scrubber) for item in value]
    if isinstance(value, tuple):
        return tuple(scrub_outbound_tree(item, scrubber) for item in value)
    if isinstance(value, dict):
        return {
            scrub_outbound_tree(k, scrubber): scrub_outbound_tree(v, scrubber)
            for k, v in value.items()
        }
    return value


def scrub_outbound_image(png: bytes, scrubber: Scrubber) -> bytes:
    """Redact one outbound PNG. Errors propagate so the send is aborted."""
    if not png:
        return png
    from PIL import Image

    image = Image.open(io.BytesIO(png)).convert("RGB")
    redacted = scrubber.scrub_image(image)
    out = io.BytesIO()
    redacted.save(out, format="PNG")
    return out.getvalue()


def prepare_outbound(value: object, purpose: str) -> object:
    """Return ``value`` ready to leave the machine: scrubbed, or opted out.

    With an opt-out (``OPENADAPT_FLOW_SEND_UNSCRUBBED``) the value is returned
    unchanged and the opt-out is recorded. Otherwise every string in it is
    scrubbed; with no scrubber this raises :class:`PrivacyNotAvailable` and the
    caller must not send.
    """
    if outbound_opt_out(purpose) is not None:
        return value
    return scrub_outbound_tree(value, require_outbound_scrubber(purpose))


def prepare_outbound_image(png: bytes, purpose: str) -> bytes:
    """Return a PNG ready to leave the machine: redacted, or opted out."""
    if outbound_opt_out(purpose) is not None:
        return png
    return scrub_outbound_image(png, require_outbound_scrubber(purpose))


# -- test / embedding hooks --------------------------------------------------


def set_text_scrubber(scrubber: Optional[TextScrubber]) -> None:
    """Inject a text scrubber (tests / custom providers), bypassing lazy build."""
    global _text_scrubber
    _text_scrubber = scrubber


def set_image_scrubber(scrubber: Optional[ImageScrubber]) -> None:
    """Inject an image scrubber (tests / custom providers), bypassing lazy build."""
    global _image_scrubber
    _image_scrubber = scrubber


def reset_scrubbers() -> None:
    """Clear cached scrubbers so the next call rebuilds from the environment."""
    global _text_scrubber, _image_scrubber
    _text_scrubber = _UNSET
    _image_scrubber = _UNSET
