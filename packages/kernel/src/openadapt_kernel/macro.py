"""Full-DOM schema extraction (including Shadow DOM & iframes), indexed live-DOM grounding, transactional form staging, and stepwise healing."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Literal,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)

from openadapt_kernel.interceptor import CommitInterceptor
from openadapt_kernel.ledger import ObligationLedger, ProvenanceViolation

if TYPE_CHECKING:
    from openadapt_kernel.canonical import FieldType

_OBSERVE_SCHEMA_JS = """() => {
  const selectorQuery = 'input, textarea, select, button, a[href], [contenteditable="true"], [role="button"], [role="textbox"], [role="combobox"], [role="checkbox"], [role="option"]';
  const collected = [];
  const walkRoot = (root, inShadow) => {
    const all = root.querySelectorAll('*');
    for (const el of all) {
      if (el.matches && el.matches(selectorQuery)) {
        collected.push({ el, inShadow, root });
      }
      if (el.shadowRoot) {
        walkRoot(el.shadowRoot, true);
      }
    }
  };
  walkRoot(document, false);

  const labelTextFor = (el, root) => {
    if (el.getAttribute('aria-label')) return el.getAttribute('aria-label').trim();
    if (el.id && root && root.querySelector) {
      const lbl = root.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (lbl) return (lbl.innerText || lbl.textContent || '').trim();
    }
    const parentLabel = el.closest ? el.closest('label') : null;
    if (parentLabel) return (parentLabel.innerText || parentLabel.textContent || '').trim();
    const fieldWrap = el.closest ? el.closest('.v-input, .v-field, .field, .form-group') : null;
    if (fieldWrap) {
      const lbl = fieldWrap.querySelector('label');
      if (lbl) return (lbl.innerText || lbl.textContent || '').trim();
    }
    if (el.placeholder) return el.placeholder.trim();
    const txt = (el.innerText || el.textContent || '').trim();
    return txt.slice(0, 120);
  };

  const modalRoots = Array.from(
    document.querySelectorAll('[role="dialog"], .v-dialog, .v-overlay--active, .v-dialog--active, .menuable__content__active, .modal.show, dialog[open]')
  ).filter(r => {
    const st = window.getComputedStyle(r);
    const rect = r.getBoundingClientRect();
    return st && st.display !== 'none' && st.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
  });

  const isInsideModal = (el) => {
    for (const mr of modalRoots) {
      if (mr.contains(el)) return true;
    }
    return false;
  };

  const isEphemeralId = (id) => /^(input|v-input|checkbox|select|radio|textarea|switch|field)-\\d+$/i.test(id || '');

  const visibleMatches = (root, sel) => {
    try {
      return Array.from(root.querySelectorAll(sel)).filter(n => {
        const st = window.getComputedStyle(n);
        const r = n.getBoundingClientRect();
        return st && st.display !== 'none' && st.visibility !== 'hidden' && r.width > 0 && r.height > 0;
      });
    } catch (e) {
      return [];
    }
  };

  const durableSelectorFor = (el, root, tag, type, label, inModal) => {
    const esc = (s) => s.replace(/\\\\/g, '\\\\\\\\').replace(/"/g, '\\\\"');
    const tid = el.getAttribute('data-testid');
    if (tid) {
      const s = `[data-testid="${esc(tid)}"]`;
      if (visibleMatches(root, s)[0] === el) return s;
    }
    const name = el.getAttribute('name');
    if (name) {
      const s = `${tag}[name="${esc(name)}"]`;
      if (visibleMatches(root, s)[0] === el) return s;
    }
    const aria = el.getAttribute('aria-label');
    if (aria) {
      const s = `${tag}[aria-label="${esc(aria)}"]`;
      if (visibleMatches(root, s)[0] === el) return s;
    }
    if (el.id && !isEphemeralId(el.id)) {
      const s = `#${CSS.escape(el.id)}`;
      if (visibleMatches(root, s)[0] === el) return s;
    }
    // Synthesize a live-verified container-scoped structural selector (avoids ephemeral #input-110 IDs!)
    const isEditable = el.getAttribute('contenteditable') === 'true';
    const rawRole = (el.getAttribute('role') || '').toLowerCase();
    const scopes = inModal
      ? ['[role="dialog"]', '.v-dialog--active', '.v-overlay--active .v-card', '.v-dialog .v-card', '.v-card', 'form']
      : ['main form', 'main', 'form', 'body'];
    const basePart = isEditable ? `${tag}[contenteditable="true"]`
      : (tag === 'input' && type) ? `input[type="${esc(type)}"]`
      : (tag === 'button' && type === 'submit') ? 'button[type="submit"]'
      : (rawRole === 'combobox' || rawRole === 'option') ? `[role="${esc(rawRole)}"]`
      : tag;
    for (const sc of scopes) {
      const cand = `${sc} ${basePart}`;
      const hits = visibleMatches(root, cand);
      const pos = hits.indexOf(el);
      if (pos === 0) return cand;
      if (pos > 0) return `${cand} >> nth=${pos}`;
    }
    if (tag === 'button' && label && label.length <= 40) {
      return `${inModal ? '.v-card ' : ''}button:has-text("${esc(label)}")`;
    }
    if (el.id) return `#${CSS.escape(el.id)}`;
    return '';
  };

  const items = [];
  let idx = 0;
  for (const { el, inShadow, root } of collected) {
    const st = window.getComputedStyle(el);
    if (!st || st.display === 'none' || st.visibility === 'hidden') continue;
    const type = (el.getAttribute('type') || '').toLowerCase();
    if (type === 'hidden') continue;
    const rect = el.getBoundingClientRect();
    if (rect.width === 0 && rect.height === 0) continue;
    const tag = el.tagName.toLowerCase();
    const role = (el.getAttribute('role') || (el.getAttribute('contenteditable') === 'true' ? 'textbox' : '')).toLowerCase();
    const inModal = isInsideModal(el);
    const label = labelTextFor(el, root);
    const options = tag === 'select'
      ? Array.from(el.options).map(o => ({ value: o.value, text: (o.text || '').trim() }))
      : [];
    el.setAttribute('data-oa-idx', String(idx));
    items.push({
      control_index: idx,
      tag,
      input_type: type,
      role,
      label,
      placeholder: (el.getAttribute('placeholder') || '').trim(),
      name: (el.getAttribute('name') || '').trim(),
      element_id: (el.id || '').trim(),
      testid: (el.getAttribute('data-testid') || '').trim(),
      in_modal: inModal,
      selector: durableSelectorFor(el, root, tag, type, label, inModal),
      value: tag === 'input' && (type === 'checkbox' || type === 'radio')
        ? String(Boolean(el.checked))
        : String(el.value !== undefined ? el.value : (el.getAttribute('data-value') || el.innerText || '').trim()),
      options,
      in_shadow: inShadow,
      disabled: Boolean(el.disabled || el.getAttribute('aria-disabled') === 'true'),
    });
    idx += 1;
  }
  return items;
}"""


@dataclass
class ControlSchema:
    tag: str
    input_type: str
    role: str
    label: str
    placeholder: str
    name: str
    element_id: str
    testid: str
    in_modal: bool
    selector: str
    value: str = ""
    options: List[Dict[str, str]] = field(default_factory=list)
    in_shadow: bool = False
    frame_index: int = -1
    control_index: int = -1
    disabled: bool = False


@dataclass
class StageResult:
    ok: bool
    staged: Dict[str, str] = field(default_factory=dict)
    mismatched: Dict[str, str] = field(default_factory=dict)
    unmatched: List[str] = field(default_factory=list)
    provenance_violations: List[ProvenanceViolation] = field(default_factory=list)


@dataclass
class PlanStep:
    step_id: str
    action: Literal["click", "fill", "select", "check"]
    selector: str
    semantic_label: str
    value: Optional[str] = None
    is_commit: bool = False
    prefer_modal: bool = False
    wait_ms: int = 250


@dataclass
class HealResult:
    verified: bool
    healed_steps: List[str] = field(default_factory=list)
    healed_plan: List[PlanStep] = field(default_factory=list)
    failure_reason: str = ""


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", (text or "").lower()))


def observe_schema(page: Any, *, include_frames: bool = True) -> List[ControlSchema]:
    """Extract all visible interactive controls across main DOM, open Shadow DOM, and child iframes."""
    controls: List[ControlSchema] = []
    raw_main = page.evaluate(_OBSERVE_SCHEMA_JS) or []
    for item in raw_main:
        controls.append(ControlSchema(frame_index=-1, **item))

    if include_frames and hasattr(page, "frames"):
        frames = list(page.frames)
        main_frame = getattr(page, "main_frame", frames[0] if frames else None)
        for idx, frame in enumerate(frames):
            if frame == main_frame:
                continue
            try:
                raw_sub = frame.evaluate(_OBSERVE_SCHEMA_JS) or []
                for item in raw_sub:
                    controls.append(ControlSchema(frame_index=idx, **item))
            except Exception:
                continue
    return controls


def _match_control(
    key: str,
    controls: Sequence[ControlSchema],
    *,
    prefer_modal: bool = False,
    allowed_tags: Optional[Sequence[str]] = None,
) -> Optional[ControlSchema]:
    key_l = key.strip().lower()
    key_toks = _tokens(key)
    best: Optional[ControlSchema] = None
    best_score = 0.0

    for ctrl in controls:
        if allowed_tags and ctrl.tag not in allowed_tags and ctrl.role not in ("combobox", "textbox"):
            continue
        score = 0.0
        if ctrl.selector and ctrl.selector.lower() == key_l:
            score = 100.0
        elif ctrl.testid and ctrl.testid.lower() == key_l:
            score = 95.0
        elif ctrl.name and ctrl.name.lower() == key_l:
            score = 90.0
        elif ctrl.element_id and ctrl.element_id.lower() == key_l:
            score = 88.0
        elif ctrl.label and ctrl.label.lower() == key_l:
            score = 85.0
        elif ctrl.placeholder and ctrl.placeholder.lower() == key_l:
            score = 80.0
        else:
            cand_toks = _tokens(f"{ctrl.label} {ctrl.placeholder} {ctrl.name} {ctrl.testid} {ctrl.element_id}")
            if key_toks and cand_toks:
                overlap = len(key_toks & cand_toks) / len(key_toks)
                if overlap >= 0.5:
                    score = 50.0 * overlap
        if score > 0.0 and prefer_modal and ctrl.in_modal:
            score += 20.0
        if score > best_score:
            best_score = score
            best = ctrl
    return best


def _target_root(page: Any, ctrl: ControlSchema) -> Any:
    if ctrl.frame_index >= 0 and hasattr(page, "frames"):
        frames = list(page.frames)
        if ctrl.frame_index < len(frames):
            return frames[ctrl.frame_index]
    return page


def _resolve_locator(page: Any, ctrl: ControlSchema) -> Any:
    root = _target_root(page, ctrl)
    if ctrl.control_index >= 0 and ctrl.frame_index == -1:
        idx_loc = root.locator(f'[data-oa-idx="{ctrl.control_index}"]').first
        try:
            if idx_loc.count() > 0:
                return idx_loc
        except Exception:
            pass
    if ctrl.selector:
        return root.locator(ctrl.selector).first
    if ctrl.label and ctrl.tag in ("button", "a"):
        return root.locator(f"{ctrl.tag}:has-text({ctrl.label!r})").first
    raise ValueError(f"Control lacks deterministic selector: {ctrl}")


def stage_form(
    page: Any,
    bindings: Dict[str, str],
    *,
    ledger: Optional[ObligationLedger] = None,
    opaque_fields: Iterable[str] = (),
    interceptor: Optional[CommitInterceptor] = None,
    subject_key: Optional[str] = None,
    prefer_modal: bool = False,
) -> StageResult:
    """Fill an entire form (main DOM, Shadow DOM, iframes, ARIA comboboxes) in one verified batch."""
    if ledger is not None and opaque_fields:
        violations = ledger.verify_ssa_provenance(
            bindings, opaque_fields=opaque_fields, target_subject_key=subject_key
        )
        if violations:
            return StageResult(ok=False, provenance_violations=violations)

    controls = observe_schema(page)
    input_controls = [
        c
        for c in controls
        if c.tag in ("input", "textarea", "select") or c.role in ("combobox", "textbox", "checkbox")
    ]
    result = StageResult(ok=True)

    for key, val in bindings.items():
        ctrl = _match_control(key, input_controls, prefer_modal=prefer_modal)
        if ctrl is None:
            result.unmatched.append(key)
            result.ok = False
            continue

        root = _target_root(page, ctrl)
        loc = _resolve_locator(page, ctrl)
        loc.scroll_into_view_if_needed()

        if ctrl.tag == "select":
            loc.select_option(val)
            actual = str(loc.input_value())
            matched = actual == val or any(
                o["text"].lower() == val.lower() and o["value"] == actual for o in ctrl.options
            )
        elif ctrl.role == "combobox" and ctrl.tag not in ("input", "select"):
            loc.click()
            opt = root.locator(f'[role="option"]:has-text("{val}")').first
            opt.wait_for(state="visible", timeout=1500)
            opt.click()
            actual = str(
                loc.evaluate("el => (el.getAttribute('data-value') || el.innerText || '').trim()")
            )
            matched = val.lower() in actual.lower()
        elif ctrl.input_type in ("checkbox", "radio"):
            want_checked = str(val).strip().lower() in ("true", "1", "yes", "checked")
            if want_checked:
                loc.check()
            else:
                loc.uncheck()
            actual = str(bool(loc.is_checked()))
            matched = bool(loc.is_checked()) == want_checked
        else:
            loc.fill(str(val))
            actual = str(loc.input_value())
            matched = actual == str(val)

        if matched:
            result.staged[key] = actual
            if interceptor is not None:
                interceptor.mark_field_edited(
                    ctrl.testid or ctrl.name or ctrl.element_id or key,
                    actual,
                    input_type=ctrl.input_type,
                    aria_role=ctrl.role,
                    label=ctrl.label,
                )
        else:
            result.mismatched[key] = f"expected={val!r}, actual={actual!r}"
            result.ok = False

    return result


class StepwiseHealer:
    """Replays compiled plans and re-grounds broken steps via indexed live-DOM grounding."""

    def __init__(
        self,
        llm_selector_fn: Optional[Callable[[PlanStep, List[ControlSchema]], Optional[str]]] = None,
        llm_index_fn: Optional[Callable[[PlanStep, List[ControlSchema]], Optional[int]]] = None,
    ) -> None:
        self._llm_selector_fn = llm_selector_fn
        self._llm_index_fn = llm_index_fn

    def _execute_step(self, page: Any, step: PlanStep, selector: str) -> None:
        loc = page.locator(selector).first
        loc.wait_for(state="visible", timeout=2000)
        loc.scroll_into_view_if_needed()
        if step.action == "click":
            loc.click()
        elif step.action == "fill":
            loc.fill(str(step.value or ""))
            if str(loc.input_value()) != str(step.value or ""):
                raise RuntimeError(f"Postcondition mismatch on {selector}")
        elif step.action == "select":
            loc.select_option(str(step.value or ""))
        elif step.action == "check":
            if str(step.value or "true").lower() in ("true", "1", "yes"):
                loc.check()
            else:
                loc.uncheck()
        if step.wait_ms > 0 and hasattr(page, "wait_for_timeout"):
            page.wait_for_timeout(step.wait_ms)

    def _heal_live_step(self, page: Any, step: PlanStep) -> Tuple[Optional[str], Optional[str]]:
        """Return (immediate_live_selector, durable_cached_selector) grounded on live DOM."""
        controls = observe_schema(page)
        by_idx = {c.control_index: c for c in controls if c.control_index >= 0}

        # 1. Preferred: Indexed selection (zero CSS syntax hallucination possible!)
        if self._llm_index_fn is not None:
            picked_idx = self._llm_index_fn(step, controls)
            if picked_idx is not None and picked_idx in by_idx:
                ctrl = by_idx[picked_idx]
                live_sel = f'[data-oa-idx="{ctrl.control_index}"]'
                durable = ctrl.selector or (
                    f"{ctrl.tag}:has-text({ctrl.label!r})" if ctrl.label and ctrl.tag in ("button", "a") else live_sel
                )
                return live_sel, durable

        # 2. Legacy free-form selector callback (only accepted if live-verified visible on DOM!)
        if self._llm_selector_fn is not None:
            proposed = self._llm_selector_fn(step, controls)
            if proposed:
                try:
                    if page.locator(proposed).first.is_visible(timeout=500):
                        return proposed, proposed
                except Exception:
                    pass

        # 3. Deterministic semantic matcher fallback
        allowed = (
            ("button", "a")
            if step.action == "click"
            else ("select",)
            if step.action == "select"
            else ("input", "textarea")
        )
        matched_ctrl = _match_control(
            step.semantic_label,
            controls,
            prefer_modal=step.prefer_modal or any(c.in_modal for c in controls),
            allowed_tags=allowed,
        )
        if matched_ctrl is None:
            return None, None
        live_sel = f'[data-oa-idx="{matched_ctrl.control_index}"]' if matched_ctrl.control_index >= 0 else matched_ctrl.selector
        durable = matched_ctrl.selector or (
            f"{matched_ctrl.tag}:has-text({matched_ctrl.label!r})" if matched_ctrl.label and matched_ctrl.tag in ("button", "a") else live_sel
        )
        return live_sel or durable, durable

    def run_and_heal(
        self,
        page: Any,
        plan: Sequence[PlanStep],
        *,
        readback_oracle: Callable[[Any], bool],
    ) -> HealResult:
        """Execute plan, interactively healing any broken step on the live DOM."""
        healed_plan: List[PlanStep] = []
        healed_ids: List[str] = []

        for step in plan:
            try:
                self._execute_step(page, step, step.selector)
                healed_plan.append(step)
            except Exception:
                live_sel, durable_sel = self._heal_live_step(page, step)
                if not live_sel:
                    return HealResult(
                        verified=False,
                        healed_steps=healed_ids,
                        healed_plan=healed_plan,
                        failure_reason=f"could_not_reground_step:{step.step_id}",
                    )
                try:
                    self._execute_step(page, step, live_sel)
                except Exception as exc:
                    return HealResult(
                        verified=False,
                        healed_steps=healed_ids,
                        healed_plan=healed_plan,
                        failure_reason=f"healed_selector_failed:{step.step_id}:{exc}",
                    )
                healed_ids.append(step.step_id)
                healed_plan.append(
                    PlanStep(
                        step_id=step.step_id,
                        action=step.action,
                        selector=durable_sel or live_sel,
                        semantic_label=step.semantic_label,
                        value=step.value,
                        is_commit=step.is_commit,
                        prefer_modal=step.prefer_modal,
                        wait_ms=step.wait_ms,
                    )
                )

        if not readback_oracle(page):
            return HealResult(
                verified=False,
                healed_steps=healed_ids,
                healed_plan=healed_plan,
                failure_reason="independent_readback_refuted_commit",
            )

        return HealResult(verified=True, healed_steps=healed_ids, healed_plan=healed_plan)


_EPHEMERAL_SELECTOR_PATTERNS: Tuple[re.Pattern[str], ...] = (
    re.compile(r"#[A-Za-z_][\w-]*[-_]\d+\b"),
    re.compile(r"(?:\.|\[class[*^$~]?=[\"']?)(?:css-[a-z0-9]{4,}|sc-[A-Za-z0-9]{4,}|jss\d+|Mui\w+-\d+)\b|__[A-Za-z0-9]{5,}\b"),
    re.compile(r"(?:#|id=[\"']?)[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b|#\\?:r[0-9a-z]+\\?:|#[0-9a-f]{8,}\b", re.IGNORECASE),
)


@dataclass(frozen=True)
class ControlSignature:
    in_modal: bool = False
    tag: str = ""
    input_type: str = ""
    role: str = ""
    name: str = ""
    placeholder: str = ""
    label_tokens: Tuple[str, ...] = ()
    text_tokens: Tuple[str, ...] = ()
    ordinal: int = 0

    @classmethod
    def from_control(cls, ctrl: ControlSchema, *, ordinal: int = 0, text: str = "") -> "ControlSignature":
        return cls(
            ctrl.in_modal, ctrl.tag.lower(), ctrl.input_type.lower(), ctrl.role.lower(), ctrl.name.strip(),
            ctrl.placeholder.strip(), tuple(sorted(_tokens(ctrl.label))), tuple(sorted(_tokens(text or ctrl.value))), ordinal,
        )


@dataclass(frozen=True)
class LiftedPackStep:
    step_id: str
    action: str
    selector: Optional[str]
    control_signature: Optional[ControlSignature]
    value: Optional[str] = None
    stripped_ephemeral_selector: Optional[str] = None
    parameterized_var: Optional[str] = None


@dataclass
class LiftedPackSteps:
    steps: List[LiftedPackStep] = field(default_factory=list)
    stripped_selector_count: int = 0
    parameterized_vars: List[str] = field(default_factory=list)
    pruned_exploratory_count: int = 0


def is_ephemeral_selector(sel: str) -> bool:
    """Detect framework-generated ephemeral IDs, CSS-in-JS hashes, and UUID selectors."""
    s = (sel or "").strip()
    return bool(s) and any(pat.search(s) for pat in _EPHEMERAL_SELECTOR_PATTERNS)


def parameterize_value_against_dossier(
    val: str, dossier: Mapping[str, str], *, field_types: Optional[Mapping[str, FieldType]] = None
) -> str:
    """Replace a concrete value with a $var reference when it canonically matches a dossier entry."""
    from openadapt_kernel import canonical as _can

    if not val or val.startswith("$"):
        return val
    ftypes = field_types or {}
    for key, d_val in dossier.items():
        if not d_val:
            continue
        var_ref = _can.canonical_var_name(key)
        explicit = ftypes.get(key) or ftypes.get(var_ref) or ftypes.get(var_ref.lstrip("$"))
        if explicit is not None:
            matched = _can.canonical_equal(d_val, val, explicit)
        else:
            matched = (
                (_can.parse_canonical_date(d_val) is not None and _can.canonical_equal(d_val, val, "date"))
                or (_can.parse_canonical_phone(d_val) is not None and _can.canonical_equal(d_val, val, "phone"))
                or (
                    any(ch in d_val or ch in val for ch in ("$", ",", "."))
                    and _can.parse_canonical_currency_cents(d_val) is not None
                    and _can.canonical_equal(d_val, val, "currency")
                )
                or _can.canonical_equal(d_val, val, "casefold_ws")
            )
        if matched:
            return var_ref
    return val


def _build_signature_from_step(raw: Mapping[str, Any]) -> Optional[ControlSignature]:
    ctrl = raw.get("control")
    if isinstance(ctrl, ControlSchema):
        return ControlSignature.from_control(ctrl, ordinal=int(raw.get("ordinal", 0)), text=str(raw.get("text", "")))
    src: Mapping[str, Any] = ctrl if isinstance(ctrl, Mapping) else raw
    label_val, text_val = str(src.get("label") or raw.get("semantic_label") or ""), str(src.get("text") or "")
    if not any(src.get(k) for k in ("tag", "input_type", "role", "name", "placeholder", "in_modal")) and not (label_val or text_val):
        return None
    return ControlSignature(
        bool(src.get("in_modal", raw.get("prefer_modal", False))), str(src.get("tag", "")).lower(),
        str(src.get("input_type", "")).lower(), str(src.get("role", "")).lower(), str(src.get("name", "")).strip(),
        str(src.get("placeholder", "")).strip(), tuple(sorted(_tokens(label_val))), tuple(sorted(_tokens(text_val))), int(src.get("ordinal", 0)),
    )


def _coerce_repair_step(step: Any, idx: int) -> Dict[str, Any]:
    if isinstance(step, Mapping):
        return dict(step)
    var_key = str(getattr(step, "var_key", "") or "").strip()
    arg = getattr(step, "arg", None)
    val: Optional[str] = f"${var_key.lstrip('$')}" if var_key else (str(arg) if arg is not None else None)
    sig = getattr(step, "ctrl_sig", ("", "", "", "")) or ("", "", "", "")
    modal_ord = int(getattr(step, "modal_ordinal", -1))
    return {
        "step_id": f"repair_{idx}",
        "action": str(getattr(step, "method", "click") or "click"),
        "selector": str(getattr(step, "selector", "") or ""),
        "value": val,
        "in_modal": modal_ord >= 0,
        "ordinal": max(0, modal_ord),
        "tag": str(sig[0]) if len(sig) > 0 else "",
        "input_type": str(sig[1]) if len(sig) > 1 else "",
        "role": str(sig[2]) if len(sig) > 2 else "",
        "label": str(sig[3]) if len(sig) > 3 else "",
    }


def lift_cold_trace_to_pack(
    raw_steps: Sequence[Mapping[str, Any]],
    dossier: Mapping[str, str],
    *,
    field_types: Optional[Mapping[str, FieldType]] = None,
    verified_repair_steps: Sequence[Any] = (),
    prune_exploratory_prefix_before_repair: bool = True,
) -> LiftedPackSteps:
    """Lift raw trace steps into durable pack steps, pruning unverified exploratory prefixes before verified repairs."""
    pruned = 0
    steps_to_lift: List[Mapping[str, Any]]
    if verified_repair_steps and prune_exploratory_prefix_before_repair:
        pruned = len(raw_steps)
        steps_to_lift = [_coerce_repair_step(rs, i) for i, rs in enumerate(verified_repair_steps)]
    else:
        steps_to_lift = list(raw_steps)
        if prune_exploratory_prefix_before_repair and steps_to_lift:
            repair_idx = next(
                (i for i, s in enumerate(steps_to_lift) if s.get("verified_repair") or s.get("source") == "distilled_repair"),
                None,
            )
            if repair_idx is not None and repair_idx > 0:
                pruned = repair_idx
                steps_to_lift = steps_to_lift[repair_idx:]
            else:
                reset_indices = [i for i, s in enumerate(steps_to_lift) if s.get("is_navigation_reset")]
                if reset_indices and reset_indices[-1] > 0:
                    pruned = reset_indices[-1]
                    steps_to_lift = steps_to_lift[reset_indices[-1]:]
        if verified_repair_steps:
            steps_to_lift.extend(_coerce_repair_step(rs, i) for i, rs in enumerate(verified_repair_steps))

    lifted, seen_vars = LiftedPackSteps(pruned_exploratory_count=pruned), set()
    for idx, raw in enumerate(steps_to_lift):
        sel = str(raw.get("selector") or "").strip() or None
        stripped = sel if (sel and is_ephemeral_selector(sel)) else None
        if stripped is not None:
            sel = None
            lifted.stripped_selector_count += 1
        raw_val = raw.get("value")
        val_out = parameterize_value_against_dossier(str(raw_val), dossier, field_types=field_types) if raw_val is not None else None
        param_var = val_out if (raw_val is not None and val_out is not None and val_out != str(raw_val) and val_out.startswith("$")) else None
        if param_var and param_var not in seen_vars:
            seen_vars.add(param_var)
            lifted.parameterized_vars.append(param_var)
        lifted.steps.append(
            LiftedPackStep(str(raw.get("step_id") or f"step_{idx}"), str(raw.get("action") or "click"), sel, _build_signature_from_step(raw), val_out, stripped, param_var)
        )
    return lifted


def lift_distilled_repair_to_pack(
    repair_steps: Sequence[Any],
    dossier: Mapping[str, str],
    *,
    exploratory_prefix: Sequence[Mapping[str, Any]] = (),
    field_types: Optional[Mapping[str, FieldType]] = None,
) -> LiftedPackSteps:
    """Lift a verified DistilledRepairStep sequence into a pack, pruning any exploratory prefix."""
    return lift_cold_trace_to_pack(
        exploratory_prefix,
        dossier,
        field_types=field_types,
        verified_repair_steps=repair_steps,
        prune_exploratory_prefix_before_repair=True,
    )
