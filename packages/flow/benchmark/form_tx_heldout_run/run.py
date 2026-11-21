"""Run the frozen form layer on the held-out quote portals and score each case.

    python benchmark/form_tx_heldout_run/run.py --arm B --split scored [portal ...]
    python benchmark/form_tx_heldout_run/run.py --arm C --split scored [portal ...]
    python benchmark/form_tx_heldout_run/run.py --arm B --split dev harborline

Arm B is the layer with learning off; arm C carries one RuleAccumulator per
portal across its scored cases (see PREREGISTRATION.md). Each case writes one
JSON line to stdout after the marker. The scripted operator reads the
answer key's operator_script; nothing else from the key is printed.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
import urllib.request
from pathlib import Path
from typing import Any, Optional

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))  # packages/flow

from benchmark.form_tx_heldout.portals import PORTALS  # noqa: E402
from benchmark.form_tx_heldout.scoring import load_answer_key, score_case  # noqa: E402
from benchmark.form_tx_heldout_run.packs import PACKS, dossier_for  # noqa: E402
from openadapt_flow.form_tx.learning import RuleAccumulator  # noqa: E402
from openadapt_flow.form_tx.playwright_surface import PlaywrightFormSurface  # noqa: E402
from openadapt_flow.form_tx.reconciler import FormReconciler  # noqa: E402
from openadapt_flow.form_tx.types import (  # noqa: E402
    FieldAnchorSpec,
    FormHaltKind,
    FormPack,
    FormTxOutcome,
    HumanDecisionAction,
    HumanResolutionInput,
    ReadbackStatus,
)

MARK = "FORM_TX_HELDOUT_LAYER"
MAX_HALTS = 12
CASE_WALL_S = 180
APPROVER = "scripted-approver"
IGNORED_MUTATIONS = ("/quote/step/*", "/agent/login*", "/login*", "/signin*", "/tools/*")


def _post(url: str, body: dict[str, Any]) -> None:
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}
    )
    urllib.request.urlopen(req, timeout=30).read()


def _get(url: str) -> Any:
    return json.loads(urllib.request.urlopen(url, timeout=30).read())


def sign_in(page: Any, username: str, password: str) -> bool:
    """What an operator does on a sign-in page or dialog."""
    for frame in [page.main_frame, *page.frames]:
        pw = frame.locator("input[type=password]")
        if pw.count() and pw.first.is_visible():
            frame.locator(
                "input[type=text], input[type=email], input:not([type])"
            ).first.fill(username)
            pw.first.fill(password)
            pw.first.press("Enter")
            page.wait_for_timeout(800)
            page.wait_for_load_state()
            return True
    return False


_LABEL_JS = r"""
(sel) => {
  const el = document.querySelector(sel);
  if (!el) return '';
  const clean = (s) => String(s || '').replace(/\s+/g, ' ').replace(/[:*]+\s*$/, '').trim();
  if (el.labels && el.labels.length) return clean(el.labels[0].innerText);
  if (el.getAttribute('aria-label')) return clean(el.getAttribute('aria-label'));
  const row = el.closest('tr');
  if (row && row.cells && row.cells.length) return clean(row.cells[0].innerText);
  const box = el.closest('div');
  const lab = box && box.querySelector('label');
  return lab ? clean(lab.innerText) : '';
}
"""


def visible_label(page: Any, halt: Any) -> str:
    """The label a person reads next to the halted control."""
    if halt.field_label:
        return str(halt.field_label).strip().rstrip(":").strip()
    if halt.dom_selector:
        try:
            return str(page.evaluate(_LABEL_JS, halt.dom_selector) or "")
        except Exception:
            return ""
    return ""


def _norm(text: str) -> str:
    return " ".join(str(text).replace("*", "").rstrip(":").split()).casefold()


def lookup_script(script: dict[str, Any], label: str, key: Optional[str]) -> Optional[dict]:
    by_norm = {_norm(k): v for k, v in script.items() if isinstance(v, dict)}
    if label and _norm(label) in by_norm:
        return by_norm[_norm(label)]
    if key and _norm(key) in by_norm:
        return by_norm[_norm(key)]
    return None


def _harborline_estimator(page: Any, intake: dict[str, Any]) -> bool:
    """The operator runs the two-step estimator by hand (deviation D2)."""
    quality = intake["facts"].get("property.construction_quality")
    if not quality:
        return False
    with page.context.expect_page(timeout=15000) as info:
        page.locator("input[value='Estimate RC']").first.click()
    popup = info.value
    popup.wait_for_load_state()
    popup.locator("select[name='quality']").select_option(label=str(quality))
    popup.locator("input[type=submit][value='Calculate']").click()
    popup.wait_for_load_state()
    use = popup.locator("input[value='Use This Value']")
    if not use.count():
        popup.close()
        return False
    use.click()
    page.wait_for_timeout(300)
    return True


OPERATOR_TOOLS = {"harborline": _harborline_estimator}
POSTHOC = {"on": False}


def operator_decision(
    page: Any,
    halt: Any,
    script: dict[str, Any],
    remember: bool,
    n: int,
    *,
    portal: str = "",
    intake: Optional[dict[str, Any]] = None,
) -> tuple[HumanResolutionInput, dict[str, Any]]:
    label = visible_label(page, halt)
    note: dict[str, Any] = {
        "halt_kind": halt.kind.value,
        "label": label,
        "dossier_key": halt.dossier_key,
        "message": str(halt.message)[:240],
        "url": page.url.split("?")[0].split("/", 3)[-1],
    }
    allowed = set(halt.allowed_actions)

    def res(action: HumanDecisionAction, value: Any = None, mapped: Optional[str] = None):
        note["action"] = action.value
        return (
            HumanResolutionInput(
                halt_id=halt.halt_id,
                action=action,
                actor_id="scripted-operator",
                provided_value=value,
                mapped_dossier_key=mapped,
                remember_as_candidate_rule=remember,
                idempotency_key=f"op-{n}-{halt.halt_id}",
            ),
            note,
        )

    expired = POSTHOC["on"] and "session has expired" in str(halt.message).lower()
    if halt.kind == FormHaltKind.AUTH_WALL_DETECTED or expired:
        login = script.get("session_login") or {}
        if login and sign_in(page, login["username"], login["password"]):
            return res(HumanDecisionAction.FIXED_ON_SCREEN)
        return res(HumanDecisionAction.ESCALATE_TO_OWNER)

    if (
        halt.kind == FormHaltKind.POPUP_CONTRACT_FAILED
        and portal in OPERATOR_TOOLS
        and intake is not None
        and HumanDecisionAction.FIXED_ON_SCREEN in allowed
    ):
        try:
            if OPERATOR_TOOLS[portal](page, intake):
                note["operator_tool"] = "estimator_by_hand"
                return res(HumanDecisionAction.FIXED_ON_SCREEN)
        except Exception as exc:  # noqa: BLE001
            note["operator_error"] = repr(exc)[:200]

    entry = lookup_script(script, label, halt.dossier_key)
    note["script_hit"] = entry is not None
    if entry is None:
        return res(HumanDecisionAction.ESCALATE_TO_OWNER)
    action = HumanDecisionAction(entry.get("action", "provide_value"))
    if action == HumanDecisionAction.PROVIDE_VALUE and action in allowed:
        return res(action, entry.get("value"), halt.dossier_key)
    if action == HumanDecisionAction.ACCEPT_PORTAL_PREFILL and action in allowed:
        return res(action)
    if action in allowed and action not in (HumanDecisionAction.PROVIDE_VALUE,):
        return res(action)
    # The script's action is not offered on this stop; the operator types the
    # value on screen when there is a control, then marks it fixed.
    if (
        entry.get("value") is not None
        and halt.dom_selector
        and HumanDecisionAction.FIXED_ON_SCREEN in allowed
    ):
        try:
            loc = page.locator(halt.dom_selector).first
            if loc.evaluate("e => e.tagName") == "SELECT":
                loc.select_option(label=str(entry["value"]))
            else:
                loc.fill(str(entry["value"]))
            return res(HumanDecisionAction.FIXED_ON_SCREEN)
        except Exception as exc:  # noqa: BLE001
            note["operator_error"] = repr(exc)[:200]
    return res(HumanDecisionAction.ESCALATE_TO_OWNER)


def run_case(
    browser: Any,
    base: str,
    pid: str,
    case_id: str,
    pack: FormPack,
    profile: Any,
    answer_key: dict[str, Any],
    *,
    remember: bool,
) -> dict[str, Any]:
    spec = PORTALS[pid]
    _post(f"{base}/__oracle/load_case", {"case_id": case_id})
    intake = json.loads(
        (HERE.parent / "form_tx_heldout" / "cases" / pid / f"{case_id}.intake.json").read_text()
    )
    script = answer_key["cases"][case_id].get("operator_script", {})
    out: dict[str, Any] = {"portal": pid, "case_id": case_id, "halts": []}
    context = browser.new_context()
    page = context.new_page()
    t0 = time.time()
    session = None
    try:
        login = spec.login
        if login.get("required_at_start") and login.get("path"):
            page.goto(base + login["path"])
            sign_in(page, login["username"], login["password"])
        page.goto(base + spec.entry_path)
        page.wait_for_load_state()
        surface = PlaywrightFormSurface(
            page, profile, base_origin=base, ignored_mutation_paths=IGNORED_MUTATIONS
        )
        reconciler = FormReconciler(surface)
        session = reconciler.create_session(
            pack, dossier_for(intake, request_id=f"{case_id}-req"), run_id=f"run-{case_id}"
        )
        result = reconciler.run_until_halt_or_done(session)
        n = 0
        while result.halts and result.outcome in (
            FormTxOutcome.HALTED_NEEDS_HUMAN,
            FormTxOutcome.AUTH_REQUIRED,
            FormTxOutcome.VALIDATION_BLOCKED,
        ):
            if n >= MAX_HALTS or time.time() - t0 > CASE_WALL_S:
                out["aborted"] = "halt_cap" if n >= MAX_HALTS else "wall_clock"
                break
            halt = result.halts[0]
            submitted_before = bool(_get(f"{base}/__oracle/submissions"))
            decision, note = operator_decision(
                page, halt, script, remember, n, portal=pid, intake=intake
            )
            note["before_submit"] = not submitted_before
            out["halts"].append(note)
            n += 1
            try:
                result = reconciler.apply_human_resolution(session, decision)
            except Exception as exc:  # noqa: BLE001
                note["resolution_error"] = repr(exc)[:300]
                break
            if decision.action in (
                HumanDecisionAction.ESCALATE_TO_OWNER,
                HumanDecisionAction.ABORT_RUN,
            ):
                break
        out["outcome"] = result.outcome.value
        out["what_happened"] = result.what_happened[:400]
        verdict = result.summary_verdict
        if verdict is not None:
            out["readback"] = {
                "passed": sum(1 for v in verdict.field_verdicts if v.status == ReadbackStatus.PASSED),
                "failed": list(verdict.failed_keys),
                "not_checked": list(verdict.not_checked_keys),
            }
        out["receipt"] = result.receipt is not None
    except Exception as exc:  # noqa: BLE001
        out["outcome"] = "harness_error"
        out["error"] = "".join(traceback.format_exception_only(type(exc), exc))[:500]
    finally:
        out["elapsed_s"] = round(time.time() - t0, 1)
        context.close()
    records = _get(f"{base}/__oracle/submissions")
    out["score"] = score_case(case_id, records)
    out["_session"] = session
    return out


def maybe_learn(
    acc: RuleAccumulator,
    pack: FormPack,
    case: dict[str, Any],
    qualify: Any,
) -> tuple[FormPack, list[str]]:
    session = case.pop("_session", None)
    if session is None or not session.human_resolutions:
        return pack, []
    acc.record_run_resolutions(
        pack.pack_id,
        session.human_resolutions,
        run_id=session.run_id,
        pack_digest=pack.digest(),
    )
    labels = {h.get("dossier_key") or "": h.get("label") or "" for h in case["halts"]}
    admitted: list[str] = []
    for cand in acc.induce_candidates(pack.pack_id):
        label = labels.get(cand.proposed_dossier_key or "", "")
        if not label:
            admitted.append(f"skipped:{cand.candidate_id}:no_label")
            continue
        try:
            pack = acc.admit_candidate_into_pack(
                pack,
                cand,
                FieldAnchorSpec(label_patterns=(f"^{label}",)),
                approval_id=APPROVER,
                qualification_check=qualify,
            )
            admitted.append(f"admitted:{cand.candidate_id}")
        except ValueError as exc:
            admitted.append(f"refused:{cand.candidate_id}:{str(exc)[:80]}")
    return pack, admitted


def main(argv: list[str]) -> int:
    from playwright.sync_api import sync_playwright

    ap = argparse.ArgumentParser()
    ap.add_argument("--arm", choices=("B", "C"), required=True)
    ap.add_argument("--split", choices=("dev", "scored"), default="scored")
    ap.add_argument(
        "--posthoc",
        action="store_true",
        help="labelled post-hoc row: harborline pack P1 and operator sign-in on a "
        "session-expired stop (see RESULTS.md)",
    )
    ap.add_argument("portals", nargs="*")
    args = ap.parse_args(argv)
    answer_key = load_answer_key()
    portals = args.portals or list(PORTALS)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            for pid in portals:
                spec = PORTALS[pid]
                pack, profile = PACKS[pid]
                if args.posthoc and pid == "harborline":
                    from benchmark.form_tx_heldout_run.packs import (
                        HARBORLINE_PACK_POSTHOC_P1,
                    )

                    pack = HARBORLINE_PACK_POSTHOC_P1
                POSTHOC["on"] = args.posthoc
                server, base = spec.start(host="127.0.0.1", port=0)
                acc = RuleAccumulator()
                try:
                    cases = [spec.dev_case] if args.split == "dev" else list(spec.cases)

                    def qualify(candidate_pack: FormPack, _pid: str = pid) -> bool:
                        dev = run_case(
                            browser, base, _pid, spec.dev_case, candidate_pack,
                            profile, answer_key, remember=False,
                        )
                        dev.pop("_session", None)
                        return (
                            dev.get("outcome") == FormTxOutcome.VERIFIED_DONE.value
                            and bool(dev["score"].get("correct"))
                        )

                    for case_id in cases:
                        case = run_case(
                            browser, base, pid, case_id, pack, profile, answer_key,
                            remember=args.arm == "C",
                        )
                        case["arm"] = args.arm
                        if args.arm == "C":
                            pack, learned = maybe_learn(acc, pack, case, qualify)
                            case["learning"] = learned
                        case.pop("_session", None)
                        print(MARK + " " + json.dumps(case, default=str), flush=True)
                finally:
                    server.shutdown()
        finally:
            browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
