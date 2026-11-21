"""Record one demonstration per portal: what the form layer sees on the dev case.

The portal author's careful-human driver runs on each portal's dev case only.
Before each of its clicks and value changes (fill, select, check, type), and
before the browser context closes, this saves
what ``PlaywrightFormSurface`` sees in every frame of every open page:
controls, page-text pairs, banners and dialogs. The pack author writes packs
from these recordings and the dev intake, without reading portal source.

    python benchmark/form_tx_heldout4_run/record_demo.py [portal ...] > demo.jsonl
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))  # packages/flow

from benchmark.form_tx_heldout4 import reference_human, runner  # noqa: E402
from benchmark.form_tx_heldout4.portals import PORTALS  # noqa: E402
from openadapt_flow.form_tx.playwright_surface import PlaywrightFormSurface  # noqa: E402
from openadapt_flow.form_tx.types import UIProfileSpec  # noqa: E402

MARK = "FORM_TX_DEMO"
_STATE = {"busy": False, "step": 0, "portal": ""}
_PROFILE = UIProfileSpec(profile_id="demo-recorder", max_settle_ms=6000)


def _capture(page: Any, why: str) -> None:
    if _STATE["busy"]:
        return
    _STATE["busy"] = True
    try:
        for frame in page.frames:
            row: dict[str, Any] = {
                "portal": _STATE["portal"],
                "step": _STATE["step"],
                "why": why,
                "frame_url": frame.url,
                "is_main": frame == page.main_frame,
            }
            try:
                surface = PlaywrightFormSurface(frame, _PROFILE)
                snap = surface.snapshot()
                surface._detach()
                row["controls"] = [
                    {
                        k: v
                        for k, v in c.model_dump().items()
                        if v not in ("", None, (), [], False) or k in ("checked",)
                    }
                    for c in snap.controls
                ]
                row["text_pairs"] = [p.model_dump() for p in snap.text_pairs]
                row["banners"] = list(snap.banner_errors)
                row["modals"] = [m.model_dump() for m in snap.active_modals]
                row["title"] = snap.title
            except Exception as exc:  # noqa: BLE001
                row["error"] = repr(exc)[:300]
            print(MARK + " " + json.dumps(row, default=str), flush=True)
        _STATE["step"] += 1
    finally:
        _STATE["busy"] = False


def _patch() -> None:
    from playwright.sync_api import BrowserContext, Locator, Page

    orig_click = Locator.click
    orig_page_click = Page.click
    orig_close = BrowserContext.close

    def click(self: Any, *a: Any, **kw: Any) -> Any:
        try:
            desc = str(self)
        except Exception:  # noqa: BLE001
            desc = "?"
        _capture(self.page, f"before click {desc[:200]}")
        return orig_click(self, *a, **kw)

    def page_click(self: Any, selector: str, *a: Any, **kw: Any) -> Any:
        _capture(self, f"before click {selector[:200]}")
        return orig_page_click(self, selector, *a, **kw)

    def close(self: Any, *a: Any, **kw: Any) -> Any:
        for p in list(self.pages):
            _capture(p, "end of case")
        return orig_close(self, *a, **kw)

    Locator.click = click  # type: ignore[method-assign]
    Page.click = page_click  # type: ignore[method-assign]
    BrowserContext.close = close  # type: ignore[method-assign]

    # Also capture before every value change, so the first sighting of each
    # control is its blank-form value (the defaults table needs it).
    def _wrap_locator(name: str) -> None:
        orig = getattr(Locator, name)

        def wrapped(self: Any, *a: Any, **kw: Any) -> Any:
            try:
                desc = str(self)
            except Exception:  # noqa: BLE001
                desc = "?"
            _capture(self.page, f"before {name} {desc[:200]}")
            return orig(self, *a, **kw)

        setattr(Locator, name, wrapped)

    def _wrap_page(name: str) -> None:
        orig = getattr(Page, name)

        def wrapped(self: Any, selector: str, *a: Any, **kw: Any) -> Any:
            _capture(self, f"before {name} {selector[:200]}")
            return orig(self, selector, *a, **kw)

        setattr(Page, name, wrapped)

    for _name in ("fill", "select_option", "check", "uncheck", "set_checked", "type", "press_sequentially"):
        if hasattr(Locator, _name):
            _wrap_locator(_name)
    for _name in ("fill", "select_option", "check", "uncheck", "type"):
        if hasattr(Page, _name):
            _wrap_page(_name)


def main(argv: list[str]) -> None:
    _patch()
    for pid in argv or list(PORTALS):
        _STATE["portal"], _STATE["step"] = pid, 0
        spec = PORTALS[pid]
        rows = runner.run_cases(
            reference_human.make_source, [pid], include_dev=True, only=[spec.dev_case]
        )
        print(
            MARK
            + "_RESULT "
            + json.dumps(
                [{"case_id": r["case_id"], "correct": r.get("correct")} for r in rows]
            ),
            flush=True,
        )


if __name__ == "__main__":
    main(sys.argv[1:])
