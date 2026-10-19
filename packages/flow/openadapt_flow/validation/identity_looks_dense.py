"""Extra identity looks on the dense record list: before and after.

Drives the REAL ``Replayer._verify_identity`` (identifier-cell OCR, the 2x
re-read, the known-ID list and the extra looks of
:mod:`openadapt_flow.runtime.identity_looks`) against the dense record list of
:mod:`.dense_surface`, rendered live in a browser. The pixel path is the one
the compiler builds for a pixel recording: the identifier region is the tight
box of the row's identity text (``context_region_from_lines``) and the
recorded identity is the OCR text inside it.

Each armed click is scored twice, at the step-entry check and, when that
check verified, again at the actuation-time check, which takes no input:

- **correct row**: the resolver landed on the recorded record. A stop is a
  false stop (an over-halt).
- **sibling row**: the resolver landed on the adjacent different record. A
  verify is a wrong-record accept. Must stay 0.

Variants: no list and no copy (the shipped default), the known-ID list, the
copy look, and both. Modes: recorded record (the band names a fixed record)
and parameter (the MRN is a run parameter, as in a worklist). The copy look
drags across the identifier's text box with the real mouse; the copied text
is the page's own selection, which is what a redirected clipboard receives.

The same script runs on ``origin/main`` for the before numbers: variants the
code under test does not support are skipped.

Run:
    python -m openadapt_flow.validation.identity_looks_dense --out DIR
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import inspect
import json
import subprocess
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Optional

SCHEMA = "openadapt.identity-looks-dense.v1"
VARIANTS = ("shipped", "list", "copy", "list_copy")
MODES = ("recorded", "param")


def _git_commit() -> Optional[str]:
    """Commit of the openadapt_flow under test (the before run imports
    another checkout's package with this script)."""
    try:
        import openadapt_flow

        here = Path(openadapt_flow.__file__).resolve().parent
        out = subprocess.run(
            ["git", "-C", str(here), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
        return out.stdout.strip() or None
    except Exception:  # noqa: BLE001 - provenance only
        return None


class CachedVision:
    """``openadapt_flow.vision`` with OCR memoized by (frame bytes, region).

    The fixture renders deterministically, so identical frames get identical
    reads; caching changes no verdict, only the run time.
    """

    def __init__(self) -> None:
        import openadapt_flow.vision as vision

        self._vision = vision
        self._cache: dict[tuple[str, Any], Any] = {}
        self.calls = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._vision, name)

    def ocr(self, png: bytes, region: Any = None, **kw: Any) -> Any:
        key = (hashlib.sha256(png).hexdigest(), tuple(region) if region else None)
        if key not in self._cache:
            self.calls += 1
            self._cache[key] = self._vision.ocr(png, region=region, **kw)
        return self._cache[key]


@dataclass
class PageBackend:
    """A live browser page seen only as pixels, with mouse drag and copy.

    Coordinates are screen pixels (CSS pixels times the device scale). The
    clipboard is the page's selection at the copy key, which is what a
    redirected remote clipboard would receive.
    """

    page: Any
    dsf: int
    viewport: tuple[int, int]
    clean_png: bytes = b""
    dirty: bool = False
    copied: Optional[str] = None
    drags: int = 0
    # (clean frame sha, drag) -> (selection text, post frame)
    _memo: dict[tuple[str, tuple[int, int, int, int]], tuple[str, bytes]] = field(
        default_factory=dict
    )
    _pending: Optional[tuple[str, tuple[int, int, int, int]]] = None
    _post: Optional[bytes] = None

    def screenshot(self) -> bytes:
        if not self.dirty:
            return self.clean_png
        if self._post is not None:
            return self._post
        return self.page.screenshot(full_page=True)

    def drag(self, x: int, y: int, end_x: int, end_y: int) -> None:
        key = (hashlib.sha256(self.clean_png).hexdigest()[:16], (x, y, end_x, end_y))
        self.drags += 1
        self.dirty = True
        if key in self._memo:
            self._pending = key
            self._post = self._memo[key][1]
            return
        d = self.dsf
        self.page.mouse.move(x / d, y / d)
        self.page.mouse.down()
        self.page.mouse.move(end_x / d, end_y / d, steps=6)
        self.page.mouse.up()
        self._pending = key
        self._post = None

    def press(self, key: str) -> None:
        if key.lower() not in ("control+c", "meta+c", "controlormeta+c"):
            raise AssertionError(f"unexpected key {key!r}")
        if self._pending is None:
            return
        if self._pending in self._memo:
            self.copied = self._memo[self._pending][0]
            return
        text = self.page.evaluate("() => String(window.getSelection())")
        post = self.page.screenshot(full_page=True)
        self._memo[self._pending] = (text, post)
        self._post = post
        self.copied = text

    def reset(self) -> None:
        """Harness only: clear the selection between trials."""
        self.page.evaluate("() => window.getSelection().removeAllRanges()")
        self.page.mouse.move(1, 1)
        self.dirty = False
        self._pending = None
        self._post = None


class PageClipboard:
    def __init__(self, backend: PageBackend) -> None:
        self.backend = backend

    def read_text(self) -> Optional[str]:
        return self.backend.copied

    def write_text(self, text: str) -> None:
        self.backend.copied = text


def _open_page(pw: Any, table: Any, cond: Any, top_offset_px: int) -> tuple[Any, Any]:
    from openadapt_flow.validation.dense_surface import render_table_html

    html = render_table_html(
        table,
        font_family=cond.font_family,
        font_px=cond.font_px,
        row_pad_px=cond.row_pad_px,
        top_offset_px=top_offset_px,
    )
    browser = pw.chromium.launch(headless=True)
    page = browser.new_page(
        viewport={"width": 1120, "height": 1600},
        device_scale_factor=cond.device_scale_factor,
    )
    page.set_content(html, wait_until="networkidle")
    full_h = page.evaluate("document.body.scrollHeight")
    page.set_viewport_size({"width": 1120, "height": int(full_h) + 20})
    return browser, page


def _points(page: Any, n: int, dsf: int) -> dict[int, tuple[tuple[int, int], ...]]:
    out: dict[int, tuple[tuple[int, int], ...]] = {}
    for i in range(n):
        pts = []
        for sel in (f'[data-name="{i}"]', f'[data-open="{i}"]'):
            bb = page.eval_on_selector(
                sel,
                "el => { const r = el.getBoundingClientRect();"
                " return [r.x, r.y, r.width, r.height]; }",
            )
            pts.append((int((bb[0] + bb[2] / 2) * dsf), int((bb[1] + bb[3] / 2) * dsf)))
        out[i] = tuple(pts)
    return out


def _supports_copy() -> bool:
    from openadapt_flow.runtime.replayer import Replayer

    return "identity_copy" in inspect.signature(Replayer.__init__).parameters


def _replayer(backend: Any, vision: Any, variant: str, known_ids: Any) -> Any:
    from openadapt_flow.runtime.replayer import Replayer

    kwargs: dict[str, Any] = {"vision": vision, "poll_interval_s": 0.0}
    if variant in ("list", "list_copy"):
        kwargs["known_ids"] = known_ids
    if variant in ("copy", "list_copy"):
        from openadapt_flow.runtime.identity_looks import IdentityCopyConfig

        kwargs["identity_copy"] = IdentityCopyConfig()
        kwargs["read_clipboard"] = PageClipboard(backend)
    return Replayer(backend, **kwargs)


def _check_both_gates(
    replayer: Any, backend: PageBackend, step: Any, res: Any, wf: Any, params: dict
) -> dict[str, Any]:
    entry_png = backend.screenshot()
    if hasattr(replayer, "_entry_identity_gate_error"):
        replayer.__dict__.setdefault("_identity_look_bindings", {}).pop(step.id, None)
        replayer._identity_input_looks = True
    try:
        entry = replayer._verify_identity(
            step, res, entry_png, params, wf, bundle_dir=None
        )
    finally:
        if hasattr(replayer, "_identity_input_looks"):
            replayer._identity_input_looks = False
    out: dict[str, Any] = {
        "entry": entry.status,
        "entry_reason": entry.abstain_reason,
        "looks": [
            [look.kind, look.outcome, look.reason]
            for look in getattr(entry, "looks", [])
        ],
        "look_halt": getattr(entry, "look_halt", None),
    }
    final = entry.status
    if entry.status == "verified" and backend.dirty:
        act_png = backend.screenshot()  # the selection highlight is still drawn
        act = replayer._verify_identity(step, res, act_png, params, wf, bundle_dir=None)
        out["actuation"] = act.status
        out["actuation_looks"] = [[x.kind, x.outcome, x.reason] for x in act.looks]
        final = act.status
    elif entry.status == "verified":
        out["actuation"] = "verified"  # no input was used: same frame, same verdict
    out["final"] = final
    backend.reset()
    return out


def run(
    seeds: list[int],
    *,
    n_rows: int = 40,
    progress: bool = False,
) -> dict[str, Any]:
    import cv2
    import numpy as np
    from playwright.sync_api import sync_playwright

    from openadapt_flow._browser_setup import ensure_chromium_installed
    from openadapt_flow.compiler.compile import (
        MIN_OCR_CONFIDENCE,
        _discriminative_crop_region,
    )
    from openadapt_flow.ir import ActionKind, Anchor, Resolution, Step, Workflow
    from openadapt_flow.runtime.identity import (
        band_region,
        context_region_from_lines,
        identifier_text_from_lines,
    )
    from openadapt_flow.runtime.known_ids import KnownIdList
    from openadapt_flow.validation.dense_surface import (
        CLICK_CONFIGS,
        RECORD_CONDITION,
        REPLAY_CONDITIONS,
        build_dense_table,
        render_frame,
    )

    ensure_chromium_installed()
    vision = CachedVision()
    variants = [v for v in VARIANTS if _supports_copy() or "copy" not in v]
    today = date.today()
    trials: list[dict[str, Any]] = []
    # Record frames first: render_frame runs its own Playwright session. Each
    # replay condition is recorded at its own device scale: the identifier
    # box is a pixel region, and a display-scale change is handled by a
    # separate mechanism (runtime.render_scale), not by this check.
    dsfs = sorted({c.device_scale_factor for c in REPLAY_CONDITIONS})
    record_frames = {
        (seed, d): render_frame(
            build_dense_table(seed, n_rows=n_rows),
            dataclasses.replace(RECORD_CONDITION, device_scale_factor=d),
            top_offset_px=0,
        )
        for seed in seeds
        for d in dsfs
    }
    with sync_playwright() as pw:
        for seed in seeds:
            table = build_dense_table(seed, n_rows=n_rows)
            known = KnownIdList(
                ids=tuple(r.mrn for r in table.rows),
                source=f"synthetic seed {seed}",
                complete=True,
            )
            idx = {id(r): i for i, r in enumerate(table.rows)}
            for k, cond in enumerate(REPLAY_CONDITIONS):
                rec = record_frames[(seed, cond.device_scale_factor)]
                rec_lines = vision.ocr(rec.png)
                rec_bgr = cv2.imdecode(
                    np.frombuffer(rec.png, np.uint8), cv2.IMREAD_COLOR
                )
                browser, page = _open_page(pw, table, cond, 4 + k * 3)
                try:
                    dsf = cond.device_scale_factor
                    png = page.screenshot(full_page=True)
                    import io

                    from PIL import Image

                    with Image.open(io.BytesIO(png)) as im:
                        viewport = im.size
                    backend = PageBackend(page=page, dsf=dsf, viewport=viewport)
                    backend.clean_png = png
                    live_pts = _points(page, len(table.rows), dsf)
                    for pair in table.pairs:
                        ti, si = idx[id(pair.target)], idx[id(pair.sibling)]
                        for config in CLICK_CONFIGS:
                            pi = 0 if config == "click_name" else 1
                            name_pt, open_pt = rec.points[ti][0], rec.points[ti][1]
                            rec_click = name_pt if pi == 0 else open_pt
                            crop = _discriminative_crop_region(rec_bgr, rec_click)
                            box = context_region_from_lines(
                                rec_lines,
                                exclude_region=crop,
                                band=band_region(rec_click, crop[3], rec.viewport),
                                point=rec_click,
                                min_confidence=MIN_OCR_CONFIDENCE,
                                reference_date=today,
                            )
                            context = (
                                identifier_text_from_lines(
                                    rec_lines,
                                    region=box,
                                    min_confidence=MIN_OCR_CONFIDENCE,
                                    reference_date=today,
                                )
                                if box
                                else None
                            )
                            base = {
                                "seed": seed,
                                "collision_class": pair.collision_class,
                                "click_config": config,
                                "replay_condition": cond.name,
                                "armed": bool(context),
                            }
                            if not context:
                                trials.append(base)
                                continue
                            anchor = Anchor(
                                template="t.png",
                                region=crop,
                                click_point=rec_click,
                                context_text=context,
                                identifier_region=box,
                            )
                            step = Step(
                                id=f"s{ti}_{config}",
                                intent="open record",
                                action=ActionKind.CLICK,
                                anchor=anchor,
                                risk="irreversible",
                            )
                            for mode in MODES:
                                params = (
                                    {"mrn": pair.target.mrn} if mode == "param" else {}
                                )
                                wf = Workflow(
                                    name="dense", params=dict(params), steps=[step]
                                )
                                for variant in variants:
                                    row = dict(base, mode=mode, variant=variant)
                                    for side, ri in (("correct", ti), ("sibling", si)):
                                        res = Resolution(
                                            rung="ocr",
                                            point=live_pts[ri][pi],
                                            confidence=0.9,
                                            elapsed_ms=1.0,
                                        )
                                        rp = _replayer(backend, vision, variant, known)
                                        row[side] = _check_both_gates(
                                            rp, backend, step, res, wf, params
                                        )
                                    trials.append(row)
                        if progress:
                            print(
                                f"seed {seed} {cond.name} {pair.collision_class} done"
                            )
                finally:
                    browser.close()
    return {
        "schema": SCHEMA,
        "meta": {
            "commit": _git_commit(),
            "seeds": seeds,
            "n_rows": n_rows,
            "replay_conditions": [c.name for c in REPLAY_CONDITIONS],
            "record_condition": f"{RECORD_CONDITION.name} at each replay's device scale",
            "click_configs": list(CLICK_CONFIGS),
            "variants": variants,
            "modes": list(MODES),
            "ocr_calls": vision.calls,
        },
        "trials": trials,
    }


def score(result: dict[str, Any]) -> dict[str, Any]:
    """Counts per (mode, variant), overall and by collision class."""
    out: dict[str, Any] = {}
    for t in result["trials"]:
        if not t.get("armed") or "variant" not in t:
            continue
        key = f"{t['mode']}/{t['variant']}"
        for scope in ("all", t["collision_class"]):
            b = out.setdefault(key, {}).setdefault(
                scope,
                {
                    "correct_row_clicks": 0,
                    "false_stops": 0,
                    "entry_verified_actuation_stopped": 0,
                    "sibling_row_clicks": 0,
                    "wrong_record_accepts": 0,
                    "copy_looks": 0,
                },
            )
            b["correct_row_clicks"] += 1
            b["sibling_row_clicks"] += 1
            c, s = t["correct"], t["sibling"]
            b["false_stops"] += c["final"] != "verified"
            b["entry_verified_actuation_stopped"] += (
                c["entry"] == "verified" and c["final"] != "verified"
            )
            b["wrong_record_accepts"] += (
                s["entry"] == "verified" or s.get("final") == "verified"
            )
            b["copy_looks"] += sum(
                1 for x in (c, s) for look in x["looks"] if look[0] == "clipboard"
            )
    return out


def unarmed(result: dict[str, Any]) -> int:
    return sum(1 for t in result["trials"] if not t.get("armed"))


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True)
    parser.add_argument("--seeds", type=int, nargs="+", default=[1, 2, 3])
    parser.add_argument("--n-rows", type=int, default=40)
    parser.add_argument("--progress", action="store_true")
    args = parser.parse_args(argv)
    result = run(args.seeds, n_rows=args.n_rows, progress=args.progress)
    scored = score(result)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "meta": result["meta"],
                "unarmed": unarmed(result),
                "scored": scored,
                "trials": result["trials"],
            },
            indent=1,
        )
        + "\n"
    )
    for key, groups in scored.items():
        a = groups["all"]
        print(
            f"{key}: false stops {a['false_stops']}/{a['correct_row_clicks']}, "
            f"wrong-record accepts {a['wrong_record_accepts']}/{a['sibling_row_clicks']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
