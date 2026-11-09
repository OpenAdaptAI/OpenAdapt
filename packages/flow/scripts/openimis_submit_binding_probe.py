"""Check the submit binding on openIMIS's unlabeled claim Save, without saving.

openIMIS saves a claim through a floating floppy-disk button with no text,
accessible name, or tooltip. Flow clicks it only through a submit binding
(``openadapt_flow.submit_binding``; docs/LIMITS.md). This probe checks the
binding on the live claim form of the reference stack in
``benchmark/openimis_claims``:

1. Demonstration: open a blank claim form (unmeasured setup, as in
   ``openimis_claims_demo.py``), type a synthetic claim through the engine's
   Playwright backend, observe the Save, and apply the compiler's rule.
2. Run: in a fresh browser, type different synthetic values, then call the
   engine's pre-click check (``arm_guarded_submit_coordinate``, the check the
   replayer runs right before the click) on:

   - the Save, unchanged (must pass);
   - every other icon button in view (must refuse);
   - the Save and a second unlabeled icon the probe adds beside it (must
     refuse both);
   - the Save after the probe removes that icon (must pass);
   - the Save after the probe moves the floating buttons to the left (must
     refuse).

The probe cancels every armed check and never clicks, so nothing is written
to openIMIS. Page changes happen only in the probe's own browser.

Usage (after ``openimis_claims_demo.py up`` and ``bootstrap``)::

    python scripts/openimis_submit_binding_probe.py --out probe.json

All data is synthetic.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "benchmark"))

from openimis_claims.fixture import (  # noqa: E402
    POLICYHOLDER_CHF,
    OpenIMISFixture,
)

from scripts.openimis_claims_demo import (  # noqa: E402
    _center,
    _control_input,
    _setup_claim_form,
    _wait_option,
)

DEMONSTRATION = {
    "claim_no": "OAPROBE1",
    "diagnosis": "Scarlet",
    "explanation": "Synthetic probe demonstration, never saved",
}
RUN = {
    "claim_no": "OAPROBE2",
    "diagnosis": "Erysipelas",
    "explanation": "Synthetic probe run, never saved",
}

_SAVE = "button.MuiFab-root:enabled"

# A second unlabeled icon button beside the Save, in the same wrapper.
_ADD_SECOND_ICON_JS = """() => {
    const fabs = [...document.querySelectorAll('button.MuiFab-root')];
    const save = fabs[fabs.length - 1];
    const slot = save.closest('div').parentElement;
    const extra = document.createElement('button');
    extra.type = 'button';
    extra.className = save.className;
    extra.setAttribute('data-probe', 'second-icon');
    extra.innerHTML = '<svg width="24" height="24" viewBox="0 0 24 24">'
        + '<path d="M6 19c0 1.1.9 2 2 2h8c1.1 0 2-.9 2-2V7H6v12z"></path></svg>';
    extra.addEventListener('click', () => { window.__probeClicked = true; });
    slot.appendChild(extra);
}"""
_REMOVE_SECOND_ICON_JS = (
    "() => document.querySelectorAll('[data-probe]').forEach((el) => el.remove())"
)
_MOVE_LEFT_CSS = (
    "div:has(> div > span > button.MuiFab-root)"
    " { left: 24px !important; right: auto !important; }"
)


def _fill(backend: Any, values: dict[str, str]) -> dict[str, int]:
    """Type a synthetic claim through the backend, as record and replay do."""

    page = backend.page
    backend.type_text(POLICYHOLDER_CHF)  # into Insurance No., focused by setup
    page.get_by_text("Policy Information", exact=False).first.wait_for(
        state="visible", timeout=60_000
    )
    page.wait_for_timeout(1_000)
    _control_input(page, "Claim No").click()
    backend.type_text(values["claim_no"])
    page.get_by_placeholder("Search Diagnosis").first.click()
    backend.type_text(values["diagnosis"])
    _wait_option(page, values["diagnosis"])
    backend.press("Enter")
    _control_input(page, "Explanation").click()
    backend.type_text(values["explanation"])
    page.mouse.wheel(0, 400)
    page.wait_for_timeout(500)
    page.get_by_placeholder("Search Service").first.click()
    backend.type_text("General")
    _wait_option(page, "General Consultation").click()
    page.wait_for_timeout(1_500)
    # How many of the inputs the backend typed into are still on the page.
    # openIMIS re-renders some of them; the binding counts only those that
    # are, at record time and at run time alike.
    return page.evaluate(
        """(key) => {
            const fills = Array.isArray(window[key]) ? window[key] : [];
            return {
                typed_into: fills.length,
                still_on_page: fills.filter((el) => el.isConnected).length,
            };
        }""",
        backend._fill_store_key,
    )


def _arm(backend: Any, point: tuple[int, int], binding: Any) -> dict[str, Any]:
    """Run the pre-click check at ``point``; always cancel, never click."""

    from openadapt_flow.backend import StructuralResolutionRefused

    try:
        backend.arm_guarded_submit_coordinate(*point, binding=binding)
    except StructuralResolutionRefused as exc:
        return {"point": list(point), "passed": False, "refusal": str(exc)}
    finally:
        backend.cancel_guarded_coordinate()
    return {"point": list(point), "passed": True, "refusal": None}


def _git_head() -> str:
    return subprocess.run(
        ["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()


def probe(fixture: OpenIMISFixture) -> dict[str, Any]:
    from openadapt_flow.backends.playwright_backend import PlaywrightBackend
    from openadapt_flow.submit_binding import (
        decide_submit_binding,
        observation_from_page,
    )

    result: dict[str, Any] = {
        "engine_head": _git_head(),
        "never_clicked": True,
        "nothing_saved": True,
    }

    backend, close = PlaywrightBackend.launch(fixture.front_url, headless=True)
    try:
        _setup_claim_form(backend.page, fixture)
        fields = _fill(backend, DEMONSTRATION)
        point = _center(backend.page.locator(_SAVE).last)
        observation = backend.submit_binding_at(*point)
        if observation is None:
            raise SystemExit("no submit-binding observation at the Save")
        binding, reason = decide_submit_binding(observation_from_page(observation))
        result["demonstration"] = {
            "fields": fields,
            "save_point": list(point),
            "observation": observation,
            "binding": binding.model_dump(mode="json") if binding else None,
            "unbound_reason": reason,
        }
    finally:
        close()
    if binding is None:
        return result

    backend, close = PlaywrightBackend.launch(fixture.front_url, headless=True)
    try:
        page = backend.page
        _setup_claim_form(page, fixture)
        run: dict[str, Any] = {"fields": _fill(backend, RUN)}
        save = _center(page.locator(_SAVE).last)
        run["save"] = _arm(backend, save, binding)

        others = []
        icons = page.locator("button.MuiIconButton-root")
        for i in range(icons.count()):
            icon = icons.nth(i)
            box = icon.bounding_box() if icon.is_visible() else None
            if not box:
                continue
            p = (int(box["x"] + box["width"] / 2), int(box["y"] + box["height"] / 2))
            if 0 <= p[0] < 1280 and 0 <= p[1] < 800:
                others.append(_arm(backend, p, binding))
        run["other_icon_buttons"] = others

        page.evaluate(_ADD_SECOND_ICON_JS)
        page.wait_for_timeout(300)
        run["save_with_second_icon"] = _arm(backend, save, binding)
        run["second_icon"] = _arm(
            backend, _center(page.locator("[data-probe=second-icon]")), binding
        )
        page.evaluate(_REMOVE_SECOND_ICON_JS)
        page.wait_for_timeout(300)
        run["save_after_removing_second_icon"] = _arm(backend, save, binding)

        page.add_style_tag(content=_MOVE_LEFT_CSS)
        page.wait_for_timeout(500)
        run["save_moved_left"] = _arm(
            backend, _center(page.locator(_SAVE).last), binding
        )
        result["never_clicked"] = not page.evaluate("Boolean(window.__probeClicked)")
        result["run"] = run
    finally:
        close()

    must_pass = [run["save"], run["save_after_removing_second_icon"]]
    must_refuse = [
        *run["other_icon_buttons"],
        run["save_with_second_icon"],
        run["second_icon"],
        run["save_moved_left"],
    ]
    result["summary"] = {
        "save_bound": True,
        "checks_that_must_pass": len(must_pass),
        "passed": sum(1 for c in must_pass if c["passed"]),
        "checks_that_must_refuse": len(must_refuse),
        "refused": sum(1 for c in must_refuse if not c["passed"]),
    }
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=OpenIMISFixture().http_port)
    parser.add_argument("--out", required=True, help="where to write the JSON result")
    args = parser.parse_args()
    result = probe(OpenIMISFixture(http_port=args.port))
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps(result.get("summary") or result.get("demonstration"), indent=2))
    summary = result.get("summary")
    ok = (
        summary is not None
        and result["never_clicked"]
        and summary["passed"] == summary["checks_that_must_pass"]
        and summary["refused"] == summary["checks_that_must_refuse"]
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
