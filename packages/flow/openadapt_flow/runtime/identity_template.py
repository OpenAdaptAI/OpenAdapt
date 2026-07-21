"""Salted-hash identity TEMPLATE: verify the wrong-patient guard without
storing plaintext PHI in the compiled bundle.

The OCR identity band (:mod:`openadapt_flow.runtime.identity`) verifies a
click's target by comparing the recorded band text (a patient's name / DOB /
MRN row) against the live band. Storing that recorded band *verbatim* in
``workflow.json`` makes the bundle unencrypted PHI-at-rest (the PHI audit's
REM-2). This module removes the plaintext: the compiler stores a **structural,
salted-hashed template** of the band instead — per-token salted hashes (of the
squashed raw form and of the OCR-canonical form) plus non-identifying SHAPE
flags (length, has-digit, name-plausible, identifier-shaped, glyph-vulnerable,
generational). That is enough to re-run the *same* token-level match at replay
(coverage / uncovered-run / contradiction / suspect / glyph budgets) WITHOUT
ever persisting a readable identifier.

Fidelity: this is a mechanical port of :func:`identity.band_match` into
"key space" — every equality the plaintext matcher does on a canonical/raw
string is done here on that string's salted hash instead, and every SHAPE
predicate is precomputed into a flag at build time. The ONE thing a one-way
hash cannot reproduce is the near-miss *ratio/containment* contradiction
(``difflib`` similarity between the recorded and live canonical STRINGS), so
that sub-signal is dropped in template mode. It is replaced by the STRICTER,
shape-based contradiction rules the plaintext matcher already has (an unmatched
name-/identifier-shaped recorded token with an unexplained same-shape observed
token is a replacement), so template mode can only ever be *stricter* than the
plaintext matcher — it never turns a plaintext ``mismatch``/``abstain`` into a
``verified`` (the never-false-accept invariant is preserved; the cost is a few
extra false-aborts on genuinely OCR-mangled true rows, disclosed in
docs/phi_at_rest.md and cross-checked against the real corpus in
tests/test_identity_template.py).

Threat model (be honest): a salted hash of a LOW-ENTROPY identifier (a name, a
DOB) is brute-forceable by anyone who holds BOTH the bundle and the in-bundle
salt. This module therefore removes *plaintext* PHI (grep-visible,
human-readable, log-leakable, git-committable) — it is NOT a cryptographic
control. The real at-rest protection is bundle encryption with deployment-time
key management (docs/phi_at_rest.md, deferred). To raise the bar meaningfully
today, set ``OPENADAPT_FLOW_IDENTITY_SALT`` at compile AND replay: the salt is
then NOT written to the bundle, so the hashes are one-way to anyone without the
external secret.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
from collections import Counter
from typing import Optional

from openadapt_flow.ir import (
    ConcatTemplate,
    IdentityCheck,
    IdentityTemplate,
    TokenTemplate,
)
from openadapt_flow.runtime import identity as _id

# Env var supplying an EXTERNAL salt (kept out of the bundle). When set at
# compile time the per-bundle salt is not persisted; replay must supply the
# same value or identity verification abstains (fail-safe).
IDENTITY_SALT_ENV = "OPENADAPT_FLOW_IDENTITY_SALT"

# Truncated digest width (hex chars). 16 hex = 64 bits: collision-free within a
# single short band, compact in the JSON artifact.
_DIGEST_HEX = 16


def _salt_bytes(salt_hex: str) -> bytes:
    """Decode a stored/loaded salt (hex) to bytes; env salt overrides it."""
    external = os.environ.get(IDENTITY_SALT_ENV)
    if external:
        return external.encode("utf-8")
    try:
        return bytes.fromhex(salt_hex) if salt_hex else b""
    except ValueError:
        return salt_hex.encode("utf-8")


def _hash(salt: bytes, text: str) -> str:
    """Salted one-way key for a string (HMAC-SHA256, truncated hex)."""
    return hmac.new(salt, text.encode("utf-8"), hashlib.sha256).hexdigest()[
        :_DIGEST_HEX
    ]


def new_salt_hex() -> str:
    """Fresh per-bundle salt (hex). Empty when an external env salt is set, so
    the bundle stores no salt and the hashes are one-way without the secret."""
    if os.environ.get(IDENTITY_SALT_ENV):
        return ""
    return os.urandom(16).hex()


# ---------------------------------------------------------------------------
# Build (compile time — has plaintext, emits hashes)
# ---------------------------------------------------------------------------


def _token_template(salt: bytes, squashed: str) -> TokenTemplate:
    return TokenTemplate(
        c=_hash(salt, _id.ocr_canonical(squashed)),
        r=_hash(salt, squashed),
        n=len(squashed),
        alpha=_id._alpha_dominated(squashed),
        name=_id._name_plausible(squashed),
        digit=_id._has_digit(squashed),
        idsh=_id._is_identifier_shaped(squashed),
        glyph=_id._is_glyph_vulnerable_identifier(squashed),
        gen=_id._is_generational_suffix(squashed),
    )


def build_identity_template(
    context_text: Optional[str],
    *,
    structured_identity: Optional[str] = None,
    param_examples: Optional[dict[str, str]] = None,
    salt_hex: Optional[str] = None,
) -> Optional[IdentityTemplate]:
    """Build a PHI-free identity template from the recorded plaintext band.

    Args:
        context_text: The recorded OCR context band (plaintext, compile-time
            only). May be None (no OCR band — e.g. structured-only anchors).
        structured_identity: Recorded structured (DOM/a11y) identity string,
            hashed into the template for the exact-match structured tier.
        param_examples: ``workflow.params`` (param name -> demo value), used to
            mark which band tokens were a parameter value (param-mode re-anchor).
        salt_hex: Reuse an existing per-bundle salt; a fresh one otherwise.

    Returns:
        An :class:`IdentityTemplate`, or None when there is neither a usable
        band nor a structured identity (nothing to protect / verify).
    """
    param_examples = param_examples or {}
    salt_hex = salt_hex if salt_hex is not None else new_salt_hex()
    salt = _salt_bytes(salt_hex)

    tmpl = IdentityTemplate(salt=salt_hex)

    if structured_identity:
        tmpl.structured = _hash(salt, _id.normalize_structured(structured_identity))

    # Mirror the compiler's context_from_lines gate: a band shorter than
    # MIN_CONTEXT_CHARS is too generic to discriminate and is never stored (any
    # sibling row sharing the generic columns would otherwise verify).
    if context_text and len(_id.squash(context_text)) >= _id.MIN_CONTEXT_CHARS:
        squashed_tokens = _id.tokenize(context_text)
        tmpl.band_len = len(_id.squash(context_text))
        tmpl.tokens = [_token_template(salt, t) for t in squashed_tokens]
        # Split-window concat keys (recorded tokens the live OCR may glue).
        for i in range(len(squashed_tokens)):
            for size in (2, 3, 4):
                if i + size > len(squashed_tokens):
                    break
                concat_raw = "".join(squashed_tokens[i : i + size])
                concat_c = _id.ocr_canonical(concat_raw)
                if len(concat_c) < _id.MIN_BLOCK:
                    continue
                tmpl.concats.append(
                    ConcatTemplate(
                        i=i,
                        size=size,
                        c=_hash(salt, concat_c),
                        r=_hash(salt, concat_raw),
                        digit=_id._has_digit(concat_raw),
                        name=_id._name_plausible(concat_raw),
                        n=len(concat_c),
                    )
                )
        # Which tokens were a parameter's demonstrated value (param re-anchor).
        # Use the plaintext matcher's whole-value embedding decision first.
        # A token shared with a longer parameter is not enough to make that
        # parameter part of the band (for example ``OpenAdapt`` as a first
        # name and inside ``openadapt@example.invalid``). Marking both would
        # require the unrelated full value during replay and create a false
        # identity halt, diverging from ``verify_target_identity``.
        embedded = set(_id.embedded_params(context_text, param_examples))
        raw_token_spans = [match.span() for match in re.finditer(r"\S+", context_text)]
        for name, example in param_examples.items():
            if name not in embedded:
                continue
            ex = _id.squash(example or "")
            # Match the plaintext substitute path exactly. When the complete
            # demonstrated value occurs verbatim, own only tokens inside that
            # occurrence. Otherwise use the same OCR-jitter fallback as
            # ``substitute_param``. Without the span rule, a value such as an
            # email containing an MRN substring could also claim and remove a
            # separate MRN token, turning a wrong-record mismatch into verify.
            occurrences = list(
                re.finditer(re.escape(example), context_text, re.IGNORECASE)
            )
            if occurrences:
                owned: set[int] = set()
                aligned = True
                for occurrence in occurrences:
                    start, end = occurrence.span()
                    overlapping = [
                        j
                        for j, (token_start, token_end) in enumerate(raw_token_spans)
                        if token_start < end and token_end > start
                    ]
                    if not overlapping:
                        aligned = False
                        break
                    first, last = overlapping[0], overlapping[-1]
                    if (
                        raw_token_spans[first][0] != start
                        or raw_token_spans[last][1] != end
                    ):
                        # The parameter occupies only part of a larger token.
                        # The current template IR cannot retain the token's
                        # dynamic prefix/suffix while replacing its middle;
                        # leave it hashed and safely over-halt on changed input.
                        aligned = False
                        break
                    owned.update(overlapping)
                idxs = sorted(owned) if aligned else []
            else:
                idxs = [
                    j
                    for j, t in enumerate(squashed_tokens)
                    if _id._token_belongs_to(t, ex)
                ]
            if idxs:
                tmpl.param_token_indices[name] = idxs
        tmpl.rests_on_confusable_identifier = any(t.glyph for t in tmpl.tokens)

    if not tmpl.tokens and not tmpl.structured:
        return None
    return tmpl


# ---------------------------------------------------------------------------
# Match (replay time — has observed plaintext + recorded hashes)
# ---------------------------------------------------------------------------


def _keyfn(salt: bytes):
    def canon(squashed: str) -> str:
        return _hash(salt, _id.ocr_canonical(squashed))

    def raw(squashed: str) -> str:
        return _hash(salt, squashed)

    return canon, raw


def _match_tokens_template(
    tmpl: IdentityTemplate,
    salt: bytes,
    obs: list[str],
) -> tuple[list[bool], list[bool], list[bool], list[bool], list[bool]]:
    """Key-space port of :func:`identity._match_tokens`.

    Recorded canonical/raw are salted hashes on the template; observed tokens
    are hashed with the same salt so equality is hash equality. Returns
    ``(matched, explained, raw_matched, suspect_evidence, glyph_ambiguous_id)``.
    """
    canon, raw = _keyfn(salt)
    toks = tmpl.tokens
    obs_c = [canon(o) for o in obs]
    obs_r = [raw(o) for o in obs]
    ne, no = len(toks), len(obs)
    matched = [False] * ne
    explained = [False] * no
    raw_matched = [False] * ne
    suspect_evidence = [False] * ne
    glyph_ambiguous_id = [False] * ne

    def mark(i: int, obs_raw_key: str, obs_squashed: str) -> None:
        matched[i] = True
        if toks[i].r == obs_raw_key:
            raw_matched[i] = True
        elif _suspicious_pair_flags(
            toks[i].digit, toks[i].name, toks[i].n, obs_squashed
        ):
            suspect_evidence[i] = True

    # single-token equivalence
    for i, t in enumerate(toks):
        for j in range(no):
            if t.c == obs_c[j]:
                mark(i, obs_r[j], obs[j])
                explained[j] = True

    # split: consecutive recorded tokens -> one observed token
    for ct in tmpl.concats:
        if all(matched[ct.i : ct.i + ct.size]):
            continue
        for j in range(no):
            if obs_c[j] == ct.c:
                rawok = ct.r == obs_r[j]
                for m in range(ct.i, ct.i + ct.size):
                    matched[m] = True
                    if rawok:
                        raw_matched[m] = True
                    elif _suspicious_pair_flags(ct.digit, ct.name, ct.n, obs[j]):
                        suspect_evidence[m] = True
                explained[j] = True

    # join: one recorded token -> consecutive observed tokens
    for i, t in enumerate(toks):
        if matched[i] or t.n < _id.MIN_BLOCK:
            continue
        done = False
        for j in range(no):
            for size in (2, 3, 4):
                if j + size > no:
                    break
                concat_raw = "".join(obs[j : j + size])
                if canon(concat_raw) == t.c:
                    mark(i, raw(concat_raw), concat_raw)
                    for m in range(j, j + size):
                        explained[m] = True
                    done = True
                    break
            if done:
                break

    # Unified glyph-vulnerable-identifier flag (raw match + recorded glyph shape)
    for i, t in enumerate(toks):
        if raw_matched[i] and t.glyph:
            glyph_ambiguous_id[i] = True
    return matched, explained, raw_matched, suspect_evidence, glyph_ambiguous_id


def _suspicious_pair_flags(
    exp_has_digit: bool, exp_name_plausible: bool, exp_len: int, observed_squashed: str
) -> bool:
    """Shape-only port of :func:`identity._suspicious_pair` (recorded side is
    known only by flags; observed side is plaintext)."""
    if exp_has_digit:
        return True
    return (
        exp_len >= _id.MIN_BLOCK
        and exp_name_plausible
        and _id._name_plausible(observed_squashed)
    )


def _contradicted_template(
    tmpl: IdentityTemplate,
    obs: list[str],
    matched: list[bool],
    explained: list[bool],
) -> list[bool]:
    """Shape-based port of :func:`identity._contradicted`.

    Drops the ratio/containment near-miss (needs recorded plaintext) and keeps
    the STRICTER shape rules: generational suffix, alphabetic replacement, and
    — added for identity parity — identifier replacement. Any residual the
    dropped fuzzy tier would have caught is caught here as a replacement or, in
    :func:`_band_match_template`, as an absent-name/uncovered-run failure.
    """
    toks = tmpl.tokens
    contradicted = [False] * len(toks)
    unexplained_alpha = any(
        not explained[j] and len(o) >= _id.MIN_BLOCK and _id._alpha_dominated(o)
        for j, o in enumerate(obs)
    )
    unexplained_ident = any(
        not explained[j] and _id._is_identifier_shaped(o) for j, o in enumerate(obs)
    )
    obs_suffix_unexplained = any(
        not explained[j] and _id._is_generational_suffix(o) for j, o in enumerate(obs)
    )
    for i, t in enumerate(toks):
        if matched[i]:
            continue
        if t.gen:
            contradicted[i] = True
            continue
        if t.n < _id.MIN_BLOCK:
            continue
        if unexplained_alpha and t.alpha:
            contradicted[i] = True
            continue
        if unexplained_ident and t.idsh:
            contradicted[i] = True
            continue
    if obs_suffix_unexplained:
        for i in range(len(toks)):
            if not matched[i]:
                contradicted[i] = True
    return contradicted


def _band_match_template(tmpl: IdentityTemplate, observed_text: str) -> _id.BandMatch:
    """Key-space port of :func:`identity.band_match` (recorded side hashed)."""
    toks = tmpl.tokens
    if not toks:
        return _id.BandMatch(0.0, 0, 0)
    salt = _salt_bytes(tmpl.salt)
    obs_raw = [tok for tok in observed_text.split() if _id.squash(tok)]
    obs = [_id.squash(tok) for tok in obs_raw]

    matched, explained, raw_matched, suspect_evidence, glyph_ambiguous_id = (
        _match_tokens_template(tmpl, salt, obs)
    )
    contradicted = _contradicted_template(tmpl, obs, matched, explained)

    matched_chars = 0
    total_chars = 0
    contradicted_chars = 0
    suspect_chars = 0
    glyph_id_chars = 0
    max_absent_alpha = 0
    uncovered_runs: list[int] = []
    current_run = 0
    for i, t in enumerate(toks):
        total_chars += t.n
        if matched[i]:
            matched_chars += t.n
            if not raw_matched[i] and suspect_evidence[i]:
                suspect_chars += t.n
            if glyph_ambiguous_id[i]:
                glyph_id_chars += t.n
            if current_run:
                uncovered_runs.append(current_run)
                current_run = 0
        else:
            current_run += t.n
            if contradicted[i]:
                contradicted_chars += t.n
            if t.alpha and t.name:
                max_absent_alpha = max(max_absent_alpha, t.n)
    if current_run:
        uncovered_runs.append(current_run)

    # Unexplained observed generational suffix (whole band matched otherwise).
    if contradicted_chars == 0 and any(
        not explained[j] and _id._is_generational_suffix(o) for j, o in enumerate(obs)
    ):
        contradicted_chars = max(
            (len(o) for o in obs if _id._is_generational_suffix(o)), default=2
        )

    # Short-token replacement, multiset accounting (key space): a missing short
    # recorded token AND an excess short observed token of the same length.
    canon, _raw = _keyfn(salt)
    exp_short = Counter(t.c for t in toks if t.n < _id.MIN_BLOCK and t.alpha)
    # length lookup by canonical-hash key for the same-length pairing.
    exp_short_len = {t.c: t.n for t in toks if t.n < _id.MIN_BLOCK and t.alpha}
    obs_short = Counter(canon(o) for o in obs if len(o) < _id.MIN_BLOCK and o.isalpha())
    obs_short_len = {
        canon(o): len(o) for o in obs if len(o) < _id.MIN_BLOCK and o.isalpha()
    }
    missing_short = exp_short - obs_short
    excess_short = obs_short - exp_short
    replaced = [
        exp_short_len[a]
        for a in missing_short
        for b in excess_short
        if exp_short_len.get(a) == obs_short_len.get(b)
    ]
    if replaced:
        contradicted_chars += max(replaced)

    unexplained_names = sum(
        1
        for j, raw_tok in enumerate(obs_raw)
        if not explained[j] and _id._name_shaped(raw_tok, obs[j])
    )

    return _id.BandMatch(
        matched_chars / total_chars if total_chars else 0.0,
        max(uncovered_runs, default=0),
        contradicted_chars,
        suspect_chars,
        unexplained_names,
        max_absent_alpha,
        glyph_id_chars,
    )


def verify_template_identity(
    tmpl: IdentityTemplate,
    observed_text: str,
    *,
    params: Optional[dict[str, str]] = None,
    param_examples: Optional[dict[str, str]] = None,
) -> IdentityCheck:
    """PHI-free counterpart to :func:`identity.verify_target_identity`.

    Same three-way OCR-tier verdict (verified / mismatch / abstain /
    unreadable) computed from the salted-hash template instead of the recorded
    plaintext band. ``expected`` on the returned check is a NON-PHI descriptor
    (the plaintext is gone); ``observed`` is the live band (run-dir artifact,
    scrubbed by the report layer when the privacy extra is active).
    """
    params = params or {}
    param_examples = param_examples or {}
    hay = _id.squash(observed_text)
    marker = "<identity template>"

    if tmpl.band_len < _id.MIN_CONTEXT_CHARS or not tmpl.tokens:
        return IdentityCheck(
            status="unreadable", expected=marker, observed=observed_text
        )

    in_band = [
        n for n in tmpl.param_token_indices if n in param_examples or n in params
    ]
    if in_band:
        if not hay:
            return IdentityCheck(status="unreadable", mode="param", expected=marker)
        salt = _salt_bytes(tmpl.salt)
        # Drop the recorded param tokens; re-anchor on the RUN's value (hashed
        # in with the same salt) so the demonstrated identifier is never stored.
        drop: set[int] = set()
        for name in in_band:
            drop.update(tmpl.param_token_indices.get(name, []))
        residue_tokens = [t for j, t in enumerate(tmpl.tokens) if j not in drop]
        run_value_tokens: list[TokenTemplate] = []
        for name in in_band:
            value = params.get(name, param_examples.get(name, ""))
            for tok in _id.tokenize(value):
                run_value_tokens.append(_token_template(salt, tok))
        sub_tmpl = IdentityTemplate(
            salt=tmpl.salt,
            band_len=tmpl.band_len,
            tokens=residue_tokens + run_value_tokens,
        )
        # Preserve split-concat keys for recorded residue windows that survive
        # substitution unchanged.  Recomputing them is impossible without the
        # recorded plaintext, but their salted hashes remain valid after an
        # index-only remap.  Windows touching a parameter are omitted.
        sub_tmpl.concats = _remap_residue_concats(tmpl, drop)

        # The run's value must actually appear in the live band (contiguous run).
        for name in in_band:
            value = _id.squash(params.get(name, param_examples.get(name, "")))
            run = _id.longest_run(value, hay)
            need = _id.required_run(len(value))
            if run < need:
                return IdentityCheck(
                    status="mismatch",
                    mode="param",
                    coverage=(run / need) if need else 0.0,
                    expected=marker,
                    observed=observed_text,
                    param=name,
                )
        match = _band_match_template(sub_tmpl, observed_text)
        return IdentityCheck(
            status=_id.band_verdict(match),
            mode="param",
            coverage=round(match.coverage, 4),
            expected=marker,
            observed=observed_text,
            param=in_band[0],
        )

    if not hay:
        return IdentityCheck(status="unreadable", expected=marker)
    match = _band_match_template(tmpl, observed_text)
    return IdentityCheck(
        status=_id.band_verdict(match),
        coverage=round(match.coverage, 4),
        expected=marker,
        observed=observed_text,
    )


def _remap_residue_concats(
    tmpl: IdentityTemplate, drop: set[int]
) -> list[ConcatTemplate]:
    """Remap concat windows whose recorded tokens all survive substitution.

    The recorded plaintext is deliberately unavailable at replay, so a concat
    hash cannot be recomputed from the per-token hashes.  It also does not need
    to be: a recorded window that contains no substituted parameter token has
    identical content in the residue.  Preserve that exact salted hash and
    shift only its start index.  Any window touching a dropped token is omitted
    because carrying it across substitution could bind to the demonstration's
    value instead of the run's value.

    This restores safe OCR split tolerance such as ``Postal`` + ``Code:``
    matching ``PostalCode:`` without weakening parameter identity.
    """
    remapped: list[ConcatTemplate] = []
    for concat in tmpl.concats:
        window = range(concat.i, concat.i + concat.size)
        if any(index in drop for index in window):
            continue
        new_i = sum(1 for index in range(concat.i) if index not in drop)
        remapped.append(concat.model_copy(update={"i": new_i}))
    return remapped


def token_in_template(tmpl: IdentityTemplate, token: str) -> bool:
    """Whether a plaintext token is present in the band template (by hash).

    A non-leaking membership probe for tests / audits: it hashes the token the
    same way the template stored its band tokens and checks for the canonical
    key. Returns False for a token the recorded band did not contain. Does NOT
    expose any stored identifier — it only answers a yes/no about a token the
    caller already holds.
    """
    if tmpl is None or not tmpl.tokens:
        return False
    salt = _salt_bytes(tmpl.salt)
    key = _hash(salt, _id.ocr_canonical(_id.squash(token)))
    return any(t.c == key for t in tmpl.tokens)


def verify_structured_template(
    tmpl: Optional[IdentityTemplate], live: Optional[str]
) -> Optional[IdentityCheck]:
    """Structured-tier verdict from a hashed template (tier 1, PHI-free).

    Compares the salted hash of the recorded structured identity against the
    salted hash of the live structured text. Exact-match semantics are
    preserved (a one-glyph MRN difference hashes differently => mismatch), with
    no plaintext stored. Returns None when the tier is unavailable (no recorded
    structured hash, or no live structured text) so the ladder falls through.
    """
    if tmpl is None or not tmpl.structured or not live:
        return None
    salt = _salt_bytes(tmpl.salt)
    ok = tmpl.structured == _hash(salt, _id.normalize_structured(live))
    return IdentityCheck(
        status="verified" if ok else "mismatch",
        mode="structured",
        coverage=1.0 if ok else 0.0,
        expected="<structured identity template>",
        observed=live,
    )
