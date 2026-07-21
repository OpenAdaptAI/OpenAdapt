"""Grounder protocol and implementations.

A Grounder is the last rung of the resolution ladder: given the current
screen, a step intent, and optional anchor text, it returns a Match-like
object (point/region/confidence) or None.

Ships:

* :class:`NullGrounder` — always None; keeps the ladder wiring uniform (the
  no-dependency default).
* :class:`OCRAnchorGrounder` — the PRIMARY grounding rung. OCR text-anchoring
  via ``openadapt-grounding`` (the ``grounding`` extra): OCR the frame, cluster
  boxes into rows, anchor on the row's unique key (carried in ``intent`` — e.g.
  patient name/MRN), then take the target control's box on that row. Benchmark
  #41 (``benchmark/grounding_eval``) measured this at 88-100% on dense EMR lists
  where the single-shot remote-VLM grounder scored 0/6. Import-guarded and
  fail-safe: returns None (abstain) if the extra is not installed or nothing is
  located, so the ladder HALTS rather than mis-clicks.
* :class:`FallbackGrounder` — chains grounders, returning the first non-None
  proposal. Used to place OCR text-anchoring first and the remote-VLM grounder
  behind it, for text-less surfaces (icon toolbars, canvases).
* :class:`AnthropicGrounder` — import-guarded behind the optional ``anthropic``
  package (the ``grounder`` extra). Never exercised in tests; needs an API key.

SAFETY INVARIANT (unchanged): a grounder only ever *proposes* a point. The
deterministic identity band still disposes before any click, and the risk gate
(:func:`openadapt_flow.runtime.resolver.is_below_ocr`) refuses a grounder
resolution for irreversible steps.
"""

from __future__ import annotations

import base64
import io
import json
import re
import statistics
from typing import Any, Optional, Protocol, runtime_checkable

from pydantic import BaseModel

from openadapt_flow.ir import Point, Region


class GrounderMatch(BaseModel):
    """Match-like result returned by a Grounder.

    Mirrors the shape of ``openadapt_flow.vision.Match`` without importing
    the vision package (the runtime must stay decoupled from it).
    """

    point: Point
    region: Region
    confidence: float


@runtime_checkable
class Grounder(Protocol):
    """Protocol for model-backed target grounding."""

    def locate(
        self,
        screen_png: bytes,
        intent: str,
        ocr_text: Optional[str] = None,
    ) -> Optional[GrounderMatch]:
        """Locate the target described by ``intent`` on the screen.

        Args:
            screen_png: Current frame as PNG bytes.
            intent: Human-readable description of the target/step.
            ocr_text: Text label at/near the target, if known.

        Returns:
            A Match-like object, or None if the target cannot be located.
        """
        ...


# Egress classification (PHI audit REM-3). A component "may egress" when its
# operation can send a screenshot off the local box (to a paid API or an on-prem
# appliance). The replayer refuses to wire an egress-capable component unless the
# operator explicitly opts in (``allow_model_grounding``), and the run report
# flags any run where a screenshot could leave the box. Local, in-process
# grounders (Null / OCR-anchoring) carry ``MAY_EGRESS = False``.


def component_may_egress(component: object) -> bool:
    """Whether a grounder / identity-VLM / state-verifier can send a screenshot
    off the box. Reads the component's ``MAY_EGRESS`` marker (default False for
    an unmarked stub / local component); a :class:`FallbackGrounder` is egress
    if ANY chained grounder is."""
    if component is None:
        return False
    if isinstance(component, FallbackGrounder):
        return any(component_may_egress(g) for g in component._grounders)
    return bool(getattr(component, "MAY_EGRESS", False))


class NullGrounder:
    """Grounder that never locates anything (the safe default)."""

    MAY_EGRESS = False

    def locate(
        self,
        screen_png: bytes,
        intent: str,
        ocr_text: Optional[str] = None,
    ) -> Optional[GrounderMatch]:
        """Always return None."""
        return None


# --------------------------------------------------------------------------
# OCR text-anchoring grounder (openadapt-grounding) — the PRIMARY rung.
# --------------------------------------------------------------------------

# Row-anchor scoring weights. A code-like token (the MRN / account number —
# the row's UNIQUE key) is far more discriminative than a name token, so a
# match on it dominates. The glyph-collapsed variant scores a little lower
# than an exact hit (an O/0-collapsed match may be an adjacent sibling — the
# identity band's job to separate, never the grounder's).
_MRN_EXACT_WEIGHT = 5.0
_MRN_GLYPH_WEIGHT = 4.0
_NAME_TOKEN_WEIGHT = 1.0

# Minimum OCR confidence (percent) for a box to be considered — mirrors the
# openadapt-grounding _run_ocr default; kept here so an empty label-less frame
# yields no boxes and the grounder abstains.
_MIN_ANCHOR_TOKEN_LEN = 3

# Stopwords stripped from a free-text ``intent`` before it is used as an
# anchor. They are common in "click X in the row for patient Y" phrasings and
# carry no row-identifying signal.
_INTENT_STOPWORDS = frozenset(
    {
        "the",
        "for",
        "row",
        "click",
        "select",
        "open",
        "in",
        "on",
        "of",
        "patient",
        "record",
        "button",
        "control",
        "mrn",
        "id",
        "and",
        "to",
        "a",
        "an",
        "at",
        "this",
        "that",
        "with",
    }
)


def _norm_glyph(s: str) -> str:
    """Collapse the O/0 and l/1/I confusables (matches benchmark/grounding_eval).

    Lets an OCR-noisy identifier still anchor by content. This deliberately
    OVER-matches on glyph-confusable siblings — safe, because the grounder only
    proposes a row band; disambiguating the one-row-away sibling is the identity
    band's job.
    """
    return (
        s.lower()
        .replace("o", "0")
        .replace("l", "1")
        .replace("i", "1")
        .replace("|", "1")
    )


class OCRAnchorGrounder:
    """Grounder backed by ``openadapt-grounding``'s OCR text-anchoring.

    The PRIMARY grounding rung. Given the live frame plus the step's ``intent``
    (which carries the target row's unique key — patient name / MRN) and the
    target control's label (``ocr_text``, e.g. ``"Open"``), it:

    1. OCRs the whole frame once (``ElementLocator._run_ocr`` — the shipped
       openadapt-grounding OCR primitive).
    2. Clusters the OCR boxes into rows by vertical proximity.
    3. Scores each row against the anchor tokens extracted from ``intent``
       (name tokens + the glyph-normalised MRN — the unique key).
    4. Returns the ``ocr_text`` control box on the winning row.

    When ``intent`` carries no row anchor, it falls back to a *unique* whole-
    frame match on ``ocr_text`` (a distinctively labelled control). Anything
    ambiguous or absent yields None.

    Requires the optional ``openadapt-grounding`` package (install the
    ``grounding`` extra). Import is lazy; :meth:`available` builds an instance
    only when the dependency is importable.

    SAFE by construction: it only PROPOSES a point. It returns None (abstain,
    no proposal) whenever the dependency is missing or nothing is located, so
    the resolution ladder halts rather than mis-clicks, and the deterministic
    identity band still disposes before any click.
    """

    # Local, in-process (OCR on the box); no screenshot ever leaves the machine.
    MAY_EGRESS = False

    def __init__(self) -> None:
        """Build the grounder, importing openadapt-grounding lazily.

        Raises:
            ImportError: If the ``openadapt-grounding`` package (the
                ``grounding`` extra) is not installed.
        """
        try:
            from openadapt_grounding.builder import Registry
            from openadapt_grounding.locator import ElementLocator
        except ImportError as exc:  # pragma: no cover - env-dependent
            raise ImportError(
                "OCRAnchorGrounder requires the 'openadapt-grounding' package. "
                "Install it with: pip install 'openadapt-flow[grounding]'"
            ) from exc
        # An empty registry is fine: only the OCR primitive (_run_ocr) is used;
        # find()/registry lookups are not on this path.
        self._locator = ElementLocator(Registry([]))

    @classmethod
    def available(cls) -> Optional["OCRAnchorGrounder"]:
        """Return an instance if openadapt-grounding is installed, else None."""
        try:
            return cls()
        except ImportError:
            return None

    def locate(
        self,
        screen_png: bytes,
        intent: str,
        ocr_text: Optional[str] = None,
    ) -> Optional[GrounderMatch]:
        """Locate the target control by OCR text-anchoring.

        Args:
            screen_png: Current frame as PNG bytes.
            intent: Step intent; its distinctive tokens (patient name, MRN)
                anchor the correct row.
            ocr_text: The target control's label (the box to click on the
                anchored row). Required — without it there is no control to
                pick, so the grounder abstains.

        Returns:
            A :class:`GrounderMatch` on the located control box, or None
            (abstain) if openadapt-grounding is unavailable, the frame yields
            no OCR, no row anchors, or the control is not on the winning row.
        """
        if not ocr_text or not ocr_text.strip():
            return None
        img = self._open(screen_png)
        if img is None:
            return None
        boxes = self._ocr_boxes(img)
        if not boxes:
            return None

        anchor_tokens = self._anchor_tokens(intent, ocr_text)
        target_box: Optional[dict] = None

        if anchor_tokens:
            row = self._best_row(self._cluster_rows(boxes), anchor_tokens)
            if row is not None:
                target_box = self._label_box_in_row(row, ocr_text)
        else:
            # No row key in the intent: only accept a UNIQUE label match.
            target_box = self._unique_label_box(boxes, ocr_text)

        if target_box is None:
            return None
        return self._to_match(target_box)

    # -- internals ---------------------------------------------------------

    @staticmethod
    def _open(screen_png: bytes) -> Optional[Any]:
        """Decode PNG bytes to an RGB PIL image (None on any failure)."""
        try:
            from PIL import Image

            return Image.open(io.BytesIO(screen_png)).convert("RGB")
        except Exception:
            return None

    def _ocr_boxes(self, img: Any) -> list[dict]:
        """Run the library OCR primitive; return boxes in pixel space."""
        w, h = img.size
        out: list[dict] = []
        try:
            elements = self._locator._run_ocr(img)  # noqa: SLF001
        except Exception:
            return []
        for e in elements:
            x, y, bw, bh = e.bounds
            out.append(
                {
                    "text": e.text or "",
                    "cx": (x + bw / 2) * w,
                    "cy": (y + bh / 2) * h,
                    "x0": x * w,
                    "y0": y * h,
                    "w": bw * w,
                    "h": bh * h,
                    "conf": float(getattr(e, "confidence", 0.0) or 0.0),
                }
            )
        return out

    @staticmethod
    def _cluster_rows(boxes: list[dict]) -> list[dict]:
        """Group OCR boxes into rows by vertical (y) proximity."""
        if not boxes:
            return []
        med_h = statistics.median(b["h"] for b in boxes) or 20.0
        thresh = med_h * 0.8
        ordered = sorted(boxes, key=lambda b: b["cy"])
        rows: list[dict] = []
        cur: list[dict] = [ordered[0]]
        cur_y = ordered[0]["cy"]
        for b in ordered[1:]:
            if abs(b["cy"] - cur_y) <= thresh:
                cur.append(b)
                cur_y = sum(x["cy"] for x in cur) / len(cur)
            else:
                rows.append(OCRAnchorGrounder._finish_row(cur))
                cur = [b]
                cur_y = b["cy"]
        rows.append(OCRAnchorGrounder._finish_row(cur))
        return rows

    @staticmethod
    def _finish_row(boxes: list[dict]) -> dict:
        text = " ".join(b["text"] for b in sorted(boxes, key=lambda b: b["cx"]))
        return {"boxes": boxes, "text": text}

    def _anchor_tokens(self, intent: str, ocr_text: str) -> list[str]:
        """Extract row-identifying tokens (name words + MRN) from the intent.

        Drops stopwords and the target label's own tokens; keeps words of at
        least ``_MIN_ANCHOR_TOKEN_LEN`` chars and any code-like token (contains
        a digit — an MRN/account number).
        """
        label_tokens = {t.lower() for t in re.split(r"[\s,]+", ocr_text) if t}
        out: list[str] = []
        for raw in re.split(r"[\s,()]+", intent or ""):
            tok = raw.strip()
            if not tok:
                continue
            low = tok.lower()
            if low in label_tokens or low in _INTENT_STOPWORDS:
                continue
            if any(ch.isdigit() for ch in tok) or len(tok) >= _MIN_ANCHOR_TOKEN_LEN:
                out.append(tok)
        return out

    @staticmethod
    def _score_row(row_text: str, tokens: list[str]) -> float:
        """Score a row's text against anchor tokens (name + glyph-norm MRN)."""
        rt = row_text.lower()
        rt_norm = _norm_glyph(row_text)
        score = 0.0
        for tok in tokens:
            low = tok.lower()
            if any(ch.isdigit() for ch in tok):  # code-like: the unique key
                if low in rt:
                    score += _MRN_EXACT_WEIGHT
                elif _norm_glyph(tok) in rt_norm:
                    score += _MRN_GLYPH_WEIGHT
            elif low in rt:
                score += _NAME_TOKEN_WEIGHT
        return score

    def _best_row(self, rows: list[dict], tokens: list[str]) -> Optional[dict]:
        """Return the highest-scoring row, or None if nothing scores > 0."""
        best_row: Optional[dict] = None
        best_score = 0.0
        for row in rows:
            score = self._score_row(row["text"], tokens)
            if score > best_score:
                best_score = score
                best_row = row
        return best_row

    @staticmethod
    def _label_box_in_row(row: dict, ocr_text: str) -> Optional[dict]:
        """The control box matching ``ocr_text`` on ``row`` (rightmost if tied)."""
        needle = ocr_text.lower().strip()
        cands = [b for b in row["boxes"] if needle in b["text"].lower()]
        if not cands:
            return None
        return max(cands, key=lambda b: b["cx"])

    @staticmethod
    def _unique_label_box(boxes: list[dict], ocr_text: str) -> Optional[dict]:
        """Frame-wide match on ``ocr_text``; only if EXACTLY one box matches."""
        needle = ocr_text.lower().strip()
        cands = [b for b in boxes if needle in b["text"].lower()]
        if len(cands) != 1:
            return None  # 0 => not found; >1 => ambiguous => abstain (safe)
        return cands[0]

    @staticmethod
    def _to_match(box: dict) -> GrounderMatch:
        """Build a GrounderMatch centred on ``box`` with its OCR bounds."""
        px, py = int(round(box["cx"])), int(round(box["cy"]))
        region: Region = (
            max(0, int(round(box["x0"]))),
            max(0, int(round(box["y0"]))),
            max(1, int(round(box["w"]))),
            max(1, int(round(box["h"]))),
        )
        # OCR box confidence is 0..1; keep it conservative (grounder resolutions
        # are risk-gated below the OCR rung regardless).
        conf = box.get("conf", 0.5)
        confidence = float(conf) if isinstance(conf, (int, float)) else 0.5
        return GrounderMatch(point=(px, py), region=region, confidence=confidence)


class FallbackGrounder:
    """Chain grounders; return the first non-None proposal.

    Places a stronger grounder ahead of weaker ones — e.g. OCR text-anchoring
    first, the remote-VLM grounder behind it for text-less surfaces (icon
    toolbars, canvases) where OCR anchoring abstains. Still only ever PROPOSES:
    if every grounder abstains, so does the chain (the ladder halts).
    """

    def __init__(self, grounders: list[Any]) -> None:
        """Store the non-None grounders, tried in order."""
        self._grounders = [g for g in grounders if g is not None]

    def locate(
        self,
        screen_png: bytes,
        intent: str,
        ocr_text: Optional[str] = None,
    ) -> Optional[GrounderMatch]:
        """Return the first grounder's non-None proposal, else None."""
        for g in self._grounders:
            match = g.locate(screen_png, intent, ocr_text)
            if match is not None:
                return match
        return None


def build_grounder(fallback: Optional[Any] = None) -> Optional[Any]:
    """Assemble the runtime's preferred grounder.

    Order of preference:

    1. :class:`OCRAnchorGrounder` — the PRIMARY rung, active whenever the
       ``grounding`` extra (openadapt-grounding) is installed. No appliance,
       GPU, or paid API needed.
    2. ``fallback`` — an optional weaker grounder (e.g. the remote-VLM
       ``RemoteGrounder``) for text-less surfaces. Passed in by the caller so
       this module stays decoupled from ``runtime.remote_vlm``.

    Returns a single grounder, a :class:`FallbackGrounder` chaining both, or
    None when neither is available (equivalent to :class:`NullGrounder`: the
    ladder simply has no grounder rung — the safe, model-free default).
    """
    chain: list[Any] = []
    ocr = OCRAnchorGrounder.available()
    if ocr is not None:
        chain.append(ocr)
    if fallback is not None:
        chain.append(fallback)
    if not chain:
        return None
    if len(chain) == 1:
        return chain[0]
    return FallbackGrounder(chain)


_DEFAULT_MODEL = "claude-opus-4-8"
_REGION_HALF_W = 20
_REGION_HALF_H = 10

_PROMPT = (
    "You are grounding a UI automation target on a screenshot.\n"
    "Target intent: {intent}\n"
    "Target text label (may be stale): {ocr_text}\n\n"
    "Reply with ONLY a JSON object of pixel coordinates for the point to "
    'click, e.g. {{"x": 123, "y": 45}}. If the target is not visible, reply '
    'with ONLY {{"x": null, "y": null}}.'
)


class AnthropicGrounder:
    """Grounder backed by the Anthropic API (vision-capable Claude model).

    Requires the optional ``anthropic`` package (install the ``grounder``
    extra). Never used in tests; calls count as model calls in run reports.
    """

    # EGRESS: base64-encodes the full screen PNG and POSTs it to the Anthropic
    # API. The replayer refuses to wire this unless the operator opts in
    # (allow_model_grounding) and flags the run (PHI audit REM-3).
    MAY_EGRESS = True

    def __init__(self, model: str = _DEFAULT_MODEL, client: object = None) -> None:
        """Create the grounder.

        Args:
            model: Anthropic model id (must be vision-capable).
            client: Optional pre-built ``anthropic.Anthropic`` client
                (useful for injecting configuration).

        Raises:
            ImportError: If the ``anthropic`` package is not installed.
        """
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - env-dependent
            raise ImportError(
                "AnthropicGrounder requires the 'anthropic' package. "
                "Install it with: pip install 'openadapt-flow[grounder]'"
            ) from exc
        self._model = model
        self._client = client if client is not None else anthropic.Anthropic()

    def locate(
        self,
        screen_png: bytes,
        intent: str,
        ocr_text: Optional[str] = None,
    ) -> Optional[GrounderMatch]:
        """Ask the model for the click point of the described target.

        Args:
            screen_png: Current frame as PNG bytes.
            intent: Human-readable description of the target/step.
            ocr_text: Text label at/near the target, if known.

        Returns:
            A :class:`GrounderMatch` centered on the model's point, or None
            if the model reports the target is not visible or replies in an
            unparseable format.
        """
        image_b64 = base64.standard_b64encode(screen_png).decode("utf-8")
        response = self._client.messages.create(
            model=self._model,
            max_tokens=256,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": image_b64,
                            },
                        },
                        {
                            "type": "text",
                            "text": _PROMPT.format(
                                intent=intent, ocr_text=ocr_text or "(none)"
                            ),
                        },
                    ],
                }
            ],
        )
        if getattr(response, "stop_reason", None) == "refusal":
            return None
        text = next(
            (b.text for b in response.content if getattr(b, "type", "") == "text"),
            "",
        )
        payload = _extract_json_object(text)
        if payload is None:
            return None
        x, y = payload.get("x"), payload.get("y")
        if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
            return None
        px, py = int(round(x)), int(round(y))
        region: Region = (
            max(0, px - _REGION_HALF_W),
            max(0, py - _REGION_HALF_H),
            2 * _REGION_HALF_W,
            2 * _REGION_HALF_H,
        )
        return GrounderMatch(point=(px, py), region=region, confidence=0.5)


def _extract_json_object(text: str) -> Optional[dict]:
    """Extract the first JSON object embedded in ``text``, if any."""
    match = re.search(r"\{.*?\}", text, re.DOTALL)
    if match is None:
        return None
    try:
        obj = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def _match_from_xy(x: Any, y: Any) -> Optional[GrounderMatch]:
    """Build a :class:`GrounderMatch` from a model's ``x`` / ``y`` reply.

    Fail-safe: returns None (abstain) unless BOTH coordinates are finite
    numbers. A ``{"x": null, "y": null}`` "not visible" reply, a non-numeric
    value, or a NaN therefore yields no proposal, so the ladder halts rather
    than clicks a bogus point.
    """
    if isinstance(x, bool) or isinstance(y, bool):
        return None
    if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
        return None
    if x != x or y != y:  # NaN
        return None
    px, py = int(round(x)), int(round(y))
    region: Region = (
        max(0, px - _REGION_HALF_W),
        max(0, py - _REGION_HALF_H),
        2 * _REGION_HALF_W,
        2 * _REGION_HALF_H,
    )
    return GrounderMatch(point=(px, py), region=region, confidence=0.5)


class OpenAICompatibleGrounder:
    """Grounder backed by any OpenAI-compatible vision chat-completions API.

    One adapter for the whole "bring your own model" surface: OpenRouter, Azure
    OpenAI, a Bedrock/OpenAI proxy, and self-hosted vLLM / Ollama / LM Studio all
    speak the same ``POST {base_url}/chat/completions`` contract with an image
    supplied as a ``data:`` URL. The operator names the ``base_url`` and ``model``
    in the deployment config; the API key is read from a named environment
    variable (never stored in the config or here).

    EGRESS (PHI audit REM-3): base64-encodes the full screen PNG into the request
    body and POSTs it to ``base_url``. Off the local box, so the replayer refuses
    to wire it unless the operator opts in (``allow_model_grounding``) and, in PHI
    mode, unless the endpoint host is on the attested allowlist (see
    ``openadapt_flow.deployment``). It flags the run either way.

    FAIL-SAFE by construction: any transport error, non-2xx, malformed body,
    unparseable content, or ``{"x": null}`` reply returns None (abstain). It only
    ever PROPOSES a point; the deterministic identity band and risk gate still
    dispose before any click, so an outage or a confused model lowers
    availability (more halts), never safety.
    """

    MAY_EGRESS = True

    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        api_key: str = "",
        timeout: float = 10.0,
        client: Any = None,
    ) -> None:
        """Create the grounder.

        Args:
            base_url: Root of the OpenAI-compatible API (``/chat/completions`` is
                appended). Required.
            model: The vision-capable model id to request. Required.
            api_key: Bearer token sent as ``Authorization: Bearer <key>``. Empty
                => no auth header (a loopback vLLM/Ollama needs none). The caller
                resolves this from a named env var; it is never stored here.
            timeout: Per-call timeout in seconds.
            client: Optional pre-built ``httpx.Client``-like object exposing
                ``post(url, json=..., headers=..., timeout=...)`` (for tests /
                custom transports). None => module-level ``httpx.post``.

        Raises:
            ValueError: If ``base_url`` or ``model`` is empty/blank.
        """
        if not base_url or not str(base_url).strip():
            raise ValueError("OpenAICompatibleGrounder requires a non-empty base_url")
        if not model or not str(model).strip():
            raise ValueError("OpenAICompatibleGrounder requires a non-empty model")
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._model = model.strip()
        self._timeout = timeout
        self._headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._client = client

    def locate(
        self,
        screen_png: bytes,
        intent: str,
        ocr_text: Optional[str] = None,
    ) -> Optional[GrounderMatch]:
        """Ask the OpenAI-compatible model for the click point of the target.

        Returns a :class:`GrounderMatch` centered on the model's point, or None
        (abstain) on ANY failure — unreachable endpoint, timeout, non-2xx,
        malformed JSON, an unparseable reply, or a ``{"x": null}`` "not visible"
        answer. Never raises.
        """
        try:
            image_b64 = base64.standard_b64encode(screen_png).decode("utf-8")
            body = {
                "model": self._model,
                "max_tokens": 256,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/png;base64,{image_b64}"
                                },
                            },
                            {
                                "type": "text",
                                "text": _PROMPT.format(
                                    intent=intent, ocr_text=ocr_text or "(none)"
                                ),
                            },
                        ],
                    }
                ],
            }
            data = self._post(body)
        except Exception:
            return None  # SAFE: any failure => no proposal => halt
        if data is None:
            return None
        text = _openai_message_text(data)
        if not text:
            return None
        payload = _extract_json_object(text)
        if payload is None:
            return None
        return _match_from_xy(payload.get("x"), payload.get("y"))

    def _post(self, body: dict) -> Optional[dict]:
        """POST the request; return the parsed JSON dict, or None on failure."""
        if self._client is not None:
            resp = self._client.post(
                self._url, json=body, headers=self._headers, timeout=self._timeout
            )
        else:
            import httpx

            try:
                resp = httpx.post(
                    self._url,
                    json=body,
                    headers=self._headers,
                    timeout=self._timeout,
                )
            except httpx.HTTPError:
                return None  # unreachable, timeout, connection reset, ...
        if getattr(resp, "status_code", None) != 200:
            return None  # auth error, 4xx/5xx
        try:
            parsed = resp.json()
        except ValueError:
            return None  # malformed body
        return parsed if isinstance(parsed, dict) else None


def _openai_message_text(data: dict) -> str:
    """Pull the assistant message text out of a chat-completions response.

    Tolerant of the two shapes providers emit: ``content`` as a plain string,
    or as a list of content parts (``[{"type": "text", "text": ...}, ...]``).
    Returns "" when the shape is unexpected, so the caller abstains.
    """
    try:
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            return ""
        message = choices[0].get("message")
        if not isinstance(message, dict):
            return ""
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(
                part.get("text", "")
                for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            )
    except (AttributeError, IndexError, KeyError, TypeError):
        return ""
    return ""
