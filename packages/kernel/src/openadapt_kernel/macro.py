"""Full-DOM schema extraction (including Shadow DOM & iframes), transactional form staging, and stepwise live-DOM healing."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Literal, Optional, Sequence

from openadapt_kernel.interceptor import CommitInterceptor
from openadapt_kernel.ledger import ObligationLedger, ProvenanceViolation

_OBSERVE_SCHEMA_JS = """() => {
  const selectorQuery = 'input, textarea, select, button, a[href], [role="button"], [role="textbox"], [role="combobox"], [role="checkbox"]';
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

  const items = [];
  const labelTextFor = (el, root) => {
    if (el.getAttribute('aria-label')) return el.getAttribute('aria-label').trim();
    if (el.id && root && root.querySelector) {
      const lbl = root.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (lbl) return (lbl.innerText || lbl.textContent || '').trim();
    }
    const parentLabel = el.closest ? el.closest('label') : null;
    if (parentLabel) return (parentLabel.innerText || parentLabel.textContent || '').trim();
    if (el.placeholder) return el.placeholder.trim();
    const txt = (el.innerText || el.textContent || '').trim();
    return txt.slice(0, 120);
  };
  const cssSelectorFor = (el) => {
    const tid = el.getAttribute('data-testid');
    if (tid) return `[data-testid="${tid.replace(/"/g, '\\\\"')}"]`;
    const name = el.getAttribute('name');
    if (name) return `${el.tagName.toLowerCase()}[name="${name.replace(/"/g, '\\\\"')}"]`;
    const aria = el.getAttribute('aria-label');
    if (aria) return `${el.tagName.toLowerCase()}[aria-label="${aria.replace(/"/g, '\\\\"')}"]`;
    if (el.id && !/^input-\\d+$/.test(el.id)) return `#${CSS.escape(el.id)}`;
    if (el.id) return `#${CSS.escape(el.id)}`;
    return '';
  };
  const modalRoots = Array.from(
    document.querySelectorAll('[role="dialog"], .v-overlay--active, .v-dialog--active, .modal.show, dialog[open]')
  );
  const isInsideModal = (el) => {
    for (const mr of modalRoots) {
      if (mr.contains(el)) return true;
    }
    return false;
  };

  for (const { el, inShadow, root } of collected) {
    const st = window.getComputedStyle(el);
    if (!st || st.display === 'none' || st.visibility === 'hidden') continue;
    const type = (el.getAttribute('type') || '').toLowerCase();
    if (type === 'hidden') continue;
    const rect = el.getBoundingClientRect();
    if (rect.width === 0 && rect.height === 0) continue;
    const tag = el.tagName.toLowerCase();
    const role = (el.getAttribute('role') || '').toLowerCase();
    const inModal = isInsideModal(el);
    const options = tag === 'select'
      ? Array.from(el.options).map(o => ({ value: o.value, text: (o.text || '').trim() }))
      : [];
    items.push({
      tag,
      input_type: type,
      role,
      label: labelTextFor(el, root),
      placeholder: (el.getAttribute('placeholder') || '').trim(),
      name: (el.getAttribute('name') || '').trim(),
      element_id: (el.id || '').trim(),
      testid: (el.getAttribute('data-testid') || '').trim(),
      in_modal: inModal,
      selector: cssSelectorFor(el),
      value: tag === 'input' && (type === 'checkbox' || type === 'radio')
        ? String(Boolean(el.checked))
        : String(el.value !== undefined ? el.value : (el.getAttribute('data-value') || el.innerText || '').trim()),
      options,
      in_shadow: inShadow,
    });
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
    """Replays compiled plans and re-grounds broken steps against the live mounted DOM."""

    def __init__(
        self,
        llm_selector_fn: Optional[Callable[[PlanStep, List[ControlSchema]], Optional[str]]] = None,
    ) -> None:
        self._llm_selector_fn = llm_selector_fn

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

    def _heal_live_step(self, page: Any, step: PlanStep) -> Optional[str]:
        controls = observe_schema(page)
        if self._llm_selector_fn is not None:
            proposed = self._llm_selector_fn(step, controls)
            if proposed:
                return proposed
        allowed = (
            ("button", "a")
            if step.action == "click"
            else ("select",)
            if step.action == "select"
            else ("input", "textarea")
        )
        ctrl = _match_control(
            step.semantic_label,
            controls,
            prefer_modal=step.prefer_modal or any(c.in_modal for c in controls),
            allowed_tags=allowed,
        )
        if ctrl is None:
            return None
        if ctrl.selector:
            return ctrl.selector
        if ctrl.label and ctrl.tag in ("button", "a"):
            return f"{ctrl.tag}:has-text({ctrl.label!r})"
        return None

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
                new_selector = self._heal_live_step(page, step)
                if not new_selector:
                    return HealResult(
                        verified=False,
                        healed_steps=healed_ids,
                        healed_plan=healed_plan,
                        failure_reason=f"could_not_reground_step:{step.step_id}",
                    )
                try:
                    self._execute_step(page, step, new_selector)
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
                        selector=new_selector,
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
