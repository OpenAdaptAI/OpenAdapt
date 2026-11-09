"""Generate the interactive run player from REAL compiled-run artifacts.

The player is a single self-contained ``player.html`` that lets a viewer
scrub through the actual per-step screenshots the replayer saved for three
runs of the SAME compiled MockMed workflow:

1. **baseline** — a clean replay; every anchor matches on the ``template``
   rung, zero heals (reused from ``docs/showcase/baseline-run``).
2. **theme drift — self-heals** — the same workflow replayed against a theme
   it never saw; each drifted anchor re-resolves via a lower rung
   (``geometry``/``ocr``) and the fix is written as a reviewable diff
   (reused from ``docs/showcase/theme-drift-run``).
3. **surprise modal — halts** — the same workflow replayed with a blocking
   survey modal injected on Save; the final step's postcondition never holds
   and the run STOPS loudly instead of reporting a false success (regenerated
   here via a real, model-free replay against the bundled MockMed app).

Every frame in the player is a real screenshot saved by the replayer — no
mockups. The replay is deterministic and model-free (``model_calls == 0``);
the only synthetic element is the MockMed demo app itself (a stand-in for a
real EMR), which the player labels as such.

Usage::

    python -m benchmark.run_player.generate            # reuse committed runs
    python -m benchmark.run_player.generate --regen-halt   # re-run the halt

The extracted per-step decision data is also written to ``player_data.json``
(metadata only, no image bytes) so the run is inspectable without a browser.
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from PIL import Image

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
SHOWCASE = REPO / "docs" / "showcase"
BUNDLE = SHOWCASE / "bundle"
RUNS_DIR = HERE / "runs"
HALT_RUN_DIR = RUNS_DIR / "modal-halt-run"

# Downscale width for embedded frames. The frames are real 1280x800
# screenshots; a flat synthetic UI stays perfectly legible at 960px and the
# whole self-contained player stays light.
FRAME_WIDTH = 840


@dataclass
class RunSpec:
    """One run shown in the player."""

    id: str
    label: str
    tagline: str
    run_dir: Path
    drift: Optional[str]  # MockMed ?drift= mode, for provenance in the report


RUN_SPECS = [
    RunSpec(
        id="baseline",
        label="Baseline replay",
        tagline="The UI it was recorded on. Every target matches exactly.",
        run_dir=SHOWCASE / "baseline-run",
        drift=None,
    ),
    RunSpec(
        id="theme",
        label="Theme drift — self-heals",
        tagline="A theme it has never seen. Watch each moved target re-resolve"
        " and heal.",
        run_dir=SHOWCASE / "theme-drift-run",
        drift="theme",
    ),
    RunSpec(
        id="halt",
        label="Surprise modal — halts",
        tagline="An unexpected pop-up blocks the save. Watch it STOP instead"
        " of lying.",
        run_dir=HALT_RUN_DIR,
        drift="modal",
    ),
]


# --------------------------------------------------------------------------
# Real replay: regenerate the HALT run (model-free) against bundled MockMed
# --------------------------------------------------------------------------

def regenerate_halt_run(out_dir: Path) -> None:
    """Produce the surprise-modal HALT run with a real, model-free replay.

    Mirrors ``openadapt-flow replay <bundle> --drift modal``: serve MockMed
    with the blocking-survey drift, replay the committed bundle, and let the
    final step's postcondition legitimately fail so the run halts. No model
    calls; the only injected imports are Playwright and the local app.
    """
    from playwright.sync_api import sync_playwright

    from openadapt_flow.backends.playwright_backend import PlaywrightBackend
    from openadapt_flow.ir import Workflow
    from openadapt_flow.mockmed.server import serve
    from openadapt_flow.runtime import Replayer

    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    workflow = Workflow.load(BUNDLE)
    url, stop = serve(port=0)
    url = f"{url.rstrip('/')}/?drift=modal"
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1280, "height": 800})
            page.goto(url)
            try:
                backend = PlaywrightBackend(page)
                report = Replayer(backend).run(
                    workflow,
                    bundle_dir=BUNDLE,
                    run_dir=out_dir,
                    save_healed_to=None,
                )
            finally:
                browser.close()
    finally:
        stop()

    assert not report.success, "modal-drift run was expected to HALT"
    assert report.model_calls == 0, "halt run must be model-free"


# --------------------------------------------------------------------------
# Frame encoding
# --------------------------------------------------------------------------

def _data_uri(png_path: Path, width: int = FRAME_WIDTH) -> Optional[str]:
    """Downscaled PNG data URI of a real captured frame, or None if missing."""
    if not png_path.is_file():
        return None
    img = Image.open(png_path).convert("RGB")
    if img.width > width:
        height = round(img.height * width / img.width)
        img = img.resize((width, height), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


# --------------------------------------------------------------------------
# Per-step extraction + plain-language captions
# --------------------------------------------------------------------------

RUNG_LABEL = {
    "template": "template",
    "template_global": "template (full-screen)",
    "ocr": "ocr",
    "geometry": "geometry",
    "grounder": "grounder",
}

# Plain-language, cold-viewer captions per resolution rung.
RUNG_CAPTION = {
    "template": "Found the target exactly where it was recorded — a"
    " pixel-perfect template match. No drift here.",
    "template_global": "The target shifted, but its recorded picture still"
    " matched after searching the whole screen.",
    "geometry": "The target moved and its pixels changed, so it was relocated"
    " from the landmarks around it — then the anchor was healed.",
    "ocr": "The theme repainted the target, so it was re-found by reading its"
    " on-screen label (OCR) — then the anchor was healed.",
    "grounder": "The target was located by a vision model (last-resort rung).",
}

RUNG_CLASS = {
    "template": "rung-template",
    "template_global": "rung-template",
    "geometry": "rung-heal",
    "ocr": "rung-heal",
    "grounder": "rung-heal",
}


def _action_kind(step_id: str, workflow: dict[str, Any]) -> str:
    for step in workflow.get("steps", []):
        if step.get("id") == step_id:
            return step.get("action", "")
    return ""


def _heal_diff(heal: dict[str, Any]) -> dict[str, Any]:
    """Compact before->after diff of what the heal changed on the anchor."""
    old = heal.get("old_anchor", {}) or {}
    new = heal.get("new_anchor", {}) or {}
    fields: list[dict[str, Any]] = []
    for key in ("click_point", "region", "ocr_text"):
        ov, nv = old.get(key), new.get(key)
        if ov != nv:
            fields.append({"field": key, "old": ov, "new": nv})
    # A heal ALWAYS refreshes the anchor's template crop from the live
    # (drifted) frame — that is the point of healing. When the click position
    # also moved, the diff shows it; when the position was already correct,
    # the template image was still re-captured so future runs match on the
    # cheap template rung again.
    note = (
        "target moved — click position and search region updated"
        if fields
        else "click position was already correct; the anchor's template"
        " image was refreshed from the healed (drifted) frame so future"
        " runs match on the cheap template rung"
    )
    return {
        "rung_used": heal.get("rung_used"),
        "kind": heal.get("kind"),
        "applied": heal.get("applied", False),
        "changed": fields,
        "note": note,
    }


def _identity_summary(identity: Optional[dict[str, Any]]) -> Optional[dict]:
    if not identity:
        return None
    return {
        "status": identity.get("status"),
        "mode": identity.get("mode"),
        "expected": identity.get("expected", ""),
        "observed": identity.get("observed", ""),
    }


def _step_caption(
    error: Optional[str],
    action: str,
    intent: str,
    rung: Optional[str],
    verified: Optional[bool],
) -> str:
    """One human sentence describing what happened this step."""
    if error:
        return (
            "STOPPED. The expected screen never appeared — an unexpected"
            " pop-up intercepted the save, so the run halted instead of"
            " reporting a success that did not happen."
        )
    if rung:
        return RUNG_CAPTION.get(rung, f"Resolved via the {rung} rung.")
    if action == "type":
        tail = (
            " The typed text was verified to have landed in the field."
            if verified
            else " (typed input)"
        )
        return "Typed into the focused field." + tail
    if action == "key":
        return "Pressed a key."
    if action in ("wait", "scroll"):
        return f"Performed a {action}."
    return intent


def _headline(
    error: Optional[str], rung: Optional[str], healed: bool, action: str
) -> str:
    if error:
        return "HALTED"
    if healed:
        return "HEALED"
    if rung in ("template", "template_global"):
        return "MATCHED"
    if action == "type":
        return "TYPED"
    return "OK"


def extract_run(spec: RunSpec) -> dict[str, Any]:
    """Extract the player record for one run from its real artifacts."""
    report = json.loads((spec.run_dir / "report.json").read_text())
    workflow = json.loads((BUNDLE / "workflow.json").read_text())

    steps: list[dict[str, Any]] = []
    for index, res in enumerate(report["results"]):
        step_id = res["step_id"]
        resolution = res.get("resolution")
        rung = resolution["rung"] if resolution else None
        healed = res.get("heal") is not None
        action = _action_kind(step_id, workflow)
        error = res.get("error")
        ok = res.get("ok", False)
        verified = res.get("input_verified")

        before = (
            _data_uri(spec.run_dir / res["before_png"])
            if res.get("before_png")
            else None
        )
        after = (
            _data_uri(spec.run_dir / res["after_png"])
            if res.get("after_png")
            else None
        )

        steps.append(
            {
                "i": index,
                "id": step_id,
                "intent": res["intent"],
                "action": action,
                "ok": ok,
                "headline": _headline(error, rung, healed, action),
                "caption": _step_caption(
                    error, action, res["intent"], rung, verified
                ),
                "rung": rung,
                "rung_label": RUNG_LABEL.get(rung, rung) if rung else None,
                "rung_class": (
                    RUNG_CLASS.get(rung, "rung-other")
                    if rung
                    else ("rung-key" if action in ("type", "key") else "rung-other")
                ),
                "confidence": (
                    resolution["confidence"] if resolution else None
                ),
                "elapsed_ms": res.get("elapsed_ms"),
                "identity": _identity_summary(res.get("identity")),
                "postconditions_ok": res.get("postconditions_ok"),
                "input_verified": verified,
                "healed": healed,
                "heal_diff": _heal_diff(res["heal"]) if healed else None,
                "error": error,
                # image bytes live only in the HTML payload, not the JSON
                "before": before,
                "after": after,
            }
        )

    halted_at = next((s["id"] for s in steps if not s["ok"]), None)
    summary = {
        "steps": len(steps),
        "ok_steps": sum(1 for s in steps if s["ok"]),
        "heals": report.get("heal_count", 0),
        "rungs": report.get("rung_counts", {}),
        "model_calls": report.get("model_calls", 0),
        "total_ms": report.get("total_ms", 0.0),
        "success": report.get("success", False),
        "halted_at": halted_at,
    }
    return {
        "id": spec.id,
        "label": spec.label,
        "tagline": spec.tagline,
        "drift": spec.drift,
        "workflow_name": report.get("workflow_name", ""),
        "summary": summary,
        "steps": steps,
    }


# --------------------------------------------------------------------------
# HTML player
# --------------------------------------------------------------------------

def _strip_images(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Copy of the run records with image data URIs removed (for the JSON)."""
    out = []
    for run in runs:
        run = json.loads(json.dumps(run))
        for step in run["steps"]:
            step.pop("before", None)
            step.pop("after", None)
        out.append(run)
    return out


def build_html(runs: list[dict[str, Any]]) -> str:
    """Render the self-contained interactive player HTML (body content)."""
    total_steps = sum(r["summary"]["steps"] for r in runs)
    payload = json.dumps(runs, separators=(",", ":"))
    return _HTML_TEMPLATE.replace("__RUNS_JSON__", payload).replace(
        "__TOTAL_STEPS__", str(total_steps)
    )


def generate(regen_halt: bool = False) -> dict[str, Any]:
    """Build the player + JSON; return the extracted (image-free) run data."""
    if regen_halt or not (HALT_RUN_DIR / "report.json").is_file():
        print(f"Regenerating HALT run at {HALT_RUN_DIR} (real replay)…")
        regenerate_halt_run(HALT_RUN_DIR)

    runs = [extract_run(spec) for spec in RUN_SPECS]

    html = build_html(runs)
    (HERE / "player.html").write_text(html)

    data = _strip_images(runs)
    (HERE / "player_data.json").write_text(json.dumps(data, indent=2))

    size_kb = (HERE / "player.html").stat().st_size / 1024
    for run in runs:
        s = run["summary"]
        state = (
            f"HALTED at {s['halted_at']}" if not s["success"] else "succeeded"
        )
        print(
            f"  {run['id']:9s} {s['steps']} steps, {s['ok_steps']} ok, "
            f"{s['heals']} heals, rungs={s['rungs']}, "
            f"model_calls={s['model_calls']} — {state}"
        )
    print(f"Wrote {HERE / 'player.html'} ({size_kb:.0f} KB)")
    print(f"Wrote {HERE / 'player_data.json'}")
    return {"runs": data, "html_bytes": len(html)}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--regen-halt",
        action="store_true",
        help="re-run the surprise-modal HALT replay (needs Playwright)",
    )
    args = ap.parse_args()
    generate(regen_halt=args.regen_halt)


_HTML_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>openadapt-flow — scrub a real compiled run</title>
<style>
:root{
  --bg:#f6f7f9; --fg:#161a1f; --muted:#5b6570; --card:#ffffff; --line:#e2e6ea;
  --safe:#137a3f; --safe-bg:#e7f5ec; --unsafe:#b3261e; --unsafe-bg:#fdeceb;
  --halt:#8a5a00; --halt-bg:#fbf3e2; --verify:#137a3f; --verify-bg:#e7f5ec;
  --code:#0b3d2e; --code-bg:#eef4f1; --accent:#1f3a5f;
}
@media (prefers-color-scheme: dark){
  :root{
    --bg:#0f1720; --fg:#e6edf3; --muted:#9fb0c0; --card:#161d26; --line:#263039;
    --safe:#4ade80; --safe-bg:#12321f; --unsafe:#ff6b61; --unsafe-bg:#3a1512;
    --halt:#e7b84b; --halt-bg:#332a12; --verify:#4ade80; --verify-bg:#12321f;
    --code:#a5f3d0; --code-bg:#10231b; --accent:#8fb2e0;
  }
}
:root[data-theme="dark"]{
  --bg:#0f1720; --fg:#e6edf3; --muted:#9fb0c0; --card:#161d26; --line:#263039;
  --safe:#4ade80; --safe-bg:#12321f; --unsafe:#ff6b61; --unsafe-bg:#3a1512;
  --halt:#e7b84b; --halt-bg:#332a12; --verify:#4ade80; --verify-bg:#12321f;
  --code:#a5f3d0; --code-bg:#10231b; --accent:#8fb2e0;
}
:root[data-theme="light"]{
  --bg:#f6f7f9; --fg:#161a1f; --muted:#5b6570; --card:#ffffff; --line:#e2e6ea;
  --safe:#137a3f; --safe-bg:#e7f5ec; --unsafe:#b3261e; --unsafe-bg:#fdeceb;
  --halt:#8a5a00; --halt-bg:#fbf3e2; --verify:#137a3f; --verify-bg:#e7f5ec;
  --code:#0b3d2e; --code-bg:#eef4f1; --accent:#1f3a5f;
}
*{box-sizing:border-box;}
body{margin:0;background:var(--bg);color:var(--fg);
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  line-height:1.5;}
main{max-width:1100px;margin:0 auto;padding:28px 20px 64px;}
h1{font-size:29px;margin:0 0 6px;letter-spacing:-0.02em;}
.sub{color:var(--muted);margin:0 0 16px;max-width:74ch;}
code{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  background:var(--code-bg);color:var(--code);padding:1px 5px;border-radius:4px;
  font-size:0.92em;}
.method{background:var(--card);border:1px solid var(--line);border-radius:10px;
  padding:14px 16px;margin:0 0 20px;color:var(--muted);font-size:14px;max-width:88ch;}
.method b{color:var(--fg);}
.tabs{display:flex;flex-wrap:wrap;gap:8px;margin:0 0 4px;}
.tab{background:var(--card);border:1px solid var(--line);color:var(--fg);
  border-radius:999px;padding:8px 15px;font-size:14px;font-weight:600;cursor:pointer;
  display:flex;align-items:center;gap:8px;}
.tab .dot{width:9px;height:9px;border-radius:50%;}
.tab.active{border-color:var(--accent);box-shadow:inset 0 0 0 1px var(--accent);}
.tab .dot.baseline{background:var(--safe);}
.tab .dot.theme{background:var(--halt);}
.tab .dot.halt{background:var(--unsafe);}
.tagline{color:var(--muted);font-size:14px;margin:8px 2px 14px;min-height:1.4em;}
.stage{position:relative;background:#000;border:1px solid var(--line);
  border-radius:12px;overflow:hidden;aspect-ratio:1280/800;}
.stage img{width:100%;height:100%;object-fit:contain;display:block;}
.stagebar{position:absolute;left:0;right:0;bottom:0;
  background:linear-gradient(to top,rgba(0,0,0,0.82),rgba(0,0,0,0));
  color:#fff;padding:34px 16px 12px;font-size:15px;}
.stagebar .cap{max-width:80ch;}
.stagebar.halt{background:linear-gradient(to top,rgba(140,10,10,0.92),rgba(140,10,10,0));}
.badge{display:inline-block;font-weight:800;font-size:11px;letter-spacing:0.05em;
  text-transform:uppercase;padding:3px 9px;border-radius:999px;margin-right:8px;}
.badge.rung-template{background:var(--safe-bg);color:var(--safe);}
.badge.rung-heal{background:var(--halt-bg);color:var(--halt);}
.badge.rung-key{background:var(--code-bg);color:var(--code);}
.badge.rung-other{background:var(--code-bg);color:var(--muted);}
.badge.halted{background:var(--unsafe-bg);color:var(--unsafe);}
.frameflag{position:absolute;top:10px;right:10px;background:rgba(0,0,0,0.6);color:#fff;
  font-size:11px;padding:3px 8px;border-radius:6px;letter-spacing:0.03em;z-index:2;}
.controls{display:flex;align-items:center;gap:12px;margin:14px 0 6px;flex-wrap:wrap;}
.controls button{background:var(--card);border:1px solid var(--line);color:var(--fg);
  border-radius:8px;padding:8px 12px;font-size:14px;font-weight:600;cursor:pointer;}
.controls button:hover{border-color:var(--accent);}
.counter{font-variant-numeric:tabular-nums;color:var(--muted);font-size:14px;
  min-width:11ch;}
.toggle{margin-left:auto;font-size:13px;color:var(--muted);display:flex;
  align-items:center;gap:6px;cursor:pointer;}
.track{position:relative;margin:10px 2px 6px;}
.ticks{display:flex;gap:3px;}
.tick{flex:1;height:12px;border-radius:3px;background:var(--line);cursor:pointer;
  border:1px solid transparent;}
.tick.rung-template{background:var(--safe);}
.tick.rung-heal{background:var(--halt);}
.tick.rung-key{background:var(--muted);opacity:0.55;}
.tick.rung-other{background:var(--muted);opacity:0.4;}
.tick.halted{background:var(--unsafe);}
.tick.active{border-color:var(--fg);transform:scaleY(1.5);}
input[type=range]{width:100%;margin:6px 0 0;accent-color:var(--accent);}
.panel{background:var(--card);border:1px solid var(--line);border-radius:12px;
  padding:16px 18px;margin-top:16px;}
.panel h3{margin:0 0 10px;font-size:16px;}
.rows{display:grid;grid-template-columns:150px 1fr;gap:6px 14px;font-size:14px;}
.rows dt{color:var(--muted);}
.rows dd{margin:0;}
.pill{display:inline-block;font-size:12px;font-weight:700;padding:2px 8px;
  border-radius:999px;}
.pill.ok{background:var(--safe-bg);color:var(--safe);}
.pill.bad{background:var(--unsafe-bg);color:var(--unsafe);}
.pill.na{background:var(--code-bg);color:var(--muted);}
.diff{margin-top:10px;font-family:ui-monospace,Menlo,Consolas,monospace;
  font-size:13px;background:var(--code-bg);border-radius:8px;padding:10px 12px;}
.diff .old{color:var(--unsafe);}
.diff .new{color:var(--safe);}
.errbox{margin-top:10px;background:var(--unsafe-bg);color:var(--unsafe);
  border:1px solid var(--unsafe);border-radius:8px;padding:10px 12px;font-size:13.5px;}
.summary{display:flex;flex-wrap:wrap;gap:8px;margin-top:12px;}
.chip{font-size:12.5px;background:var(--card);border:1px solid var(--line);
  border-radius:999px;padding:4px 11px;color:var(--muted);}
.chip b{color:var(--fg);}
.honest{margin-top:26px;color:var(--muted);font-size:13px;border-top:1px solid var(--line);
  padding-top:14px;max-width:88ch;}
.honest b{color:var(--fg);}
@media (max-width:560px){.rows{grid-template-columns:1fr;}}
</style>
</head>
<body>

<main>
  <h1>Watch a real automation run — step by step</h1>
  <p class="sub">This is one recorded workflow, compiled once, then replayed
  three ways. Press play, or drag the slider, to step through the actual
  screenshots the replayer saved. Watch it <b>heal</b> when the interface
  changes, and <b>stop</b> when something is wrong.</p>

  <div class="method">
    <b>What you're watching.</b> A nurse's triage task in a demo EMR was
    recorded once and compiled into a deterministic workflow. Below, the same
    compiled workflow runs against three versions of the app. For each step
    the overlay shows <b>how the target was found</b> (the resolution
    &ldquo;rung&rdquo;), whether it had to <b>heal</b>, and whether the
    expected screen actually appeared afterward (the postcondition). Every
    frame is a genuine screenshot from the run — nothing here is staged.
  </div>

  <div class="tabs" id="tabs"></div>
  <div class="tagline" id="tagline"></div>
  <div class="summary" id="summary"></div>

  <div class="stage" id="stage" style="margin-top:14px;">
    <div class="frameflag" id="frameflag">after</div>
    <img id="frame" alt="run frame">
    <div class="stagebar" id="stagebar"><div class="cap" id="cap"></div></div>
  </div>

  <div class="controls">
    <button id="prev">&lsaquo; Prev</button>
    <button id="play">&#9654; Play</button>
    <button id="next">Next &rsaquo;</button>
    <span class="counter" id="counter"></span>
    <label class="toggle"><input type="checkbox" id="ba"> show &ldquo;before&rdquo; frame</label>
  </div>

  <div class="track">
    <div class="ticks" id="ticks"></div>
    <input type="range" id="slider" min="0" value="0" step="1">
  </div>

  <div class="panel" id="detail"></div>

  <p class="honest">
    <b>Honest by construction.</b> The app is <b>MockMed</b>, a synthetic
    stand-in for a real EMR — the data is fake, but the record &rarr;
    compile &rarr; replay pipeline and every frame are real. The replay is
    deterministic and <b>model-free</b>: <code>model_calls = 0</code> on all
    three runs, $0.00 per run. The theme-drift run heals via the
    <code>geometry</code> and <code>ocr</code> rungs and writes each fix as a
    reviewable diff; the surprise-modal run <b>halts</b> rather than report a
    save that never happened.
    Across all runs: <b>__TOTAL_STEPS__</b> real steps captured.
  </p>
</main>

<script>
const RUNS = __RUNS_JSON__;
let runId = RUNS[0].id;
let idx = 0;
let playing = false;
let timer = null;

const $ = (id) => document.getElementById(id);
function run(){ return RUNS.find(r => r.id === runId); }

function renderTabs(){
  const t = $("tabs");
  t.innerHTML = "";
  RUNS.forEach(r => {
    const b = document.createElement("button");
    b.className = "tab" + (r.id === runId ? " active" : "");
    b.innerHTML = '<span class="dot ' + r.id + '"></span>' + r.label;
    b.onclick = () => { runId = r.id; idx = 0; stop(); renderAll(); };
    t.appendChild(b);
  });
}

function renderSummary(){
  const s = run().summary;
  const rungs = Object.entries(s.rungs).map(([k,v]) => k+"×"+v).join(", ") || "—";
  const state = s.success
    ? '<span class="pill ok">succeeded</span>'
    : '<span class="pill bad">HALTED at ' + s.halted_at + '</span>';
  $("tagline").textContent = run().tagline;
  $("summary").innerHTML =
    '<span class="chip"><b>' + s.steps + '</b> steps</span>' +
    '<span class="chip"><b>' + s.ok_steps + '</b> ok</span>' +
    '<span class="chip"><b>' + s.heals + '</b> heals</span>' +
    '<span class="chip">rungs: <b>' + rungs + '</b></span>' +
    '<span class="chip">model calls: <b>' + s.model_calls + '</b></span>' +
    '<span class="chip">' + state + '</span>';
}

function renderTicks(){
  const steps = run().steps;
  const ticks = $("ticks");
  ticks.innerHTML = "";
  steps.forEach((st, i) => {
    const d = document.createElement("div");
    const cls = st.ok ? st.rung_class : "halted";
    d.className = "tick " + cls + (i === idx ? " active" : "");
    d.title = st.id + " — " + st.intent;
    d.onclick = () => { idx = i; stop(); renderStep(); };
    ticks.appendChild(d);
  });
  const sl = $("slider");
  sl.max = steps.length - 1;
  sl.value = idx;
}

function esc(s){ return String(s == null ? "" : s)
  .replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;"); }

function renderStep(){
  const steps = run().steps;
  const st = steps[idx];
  const showBefore = $("ba").checked;
  const src = showBefore ? (st.before || st.after) : (st.after || st.before);
  $("frame").src = src || "";
  $("frameflag").textContent = showBefore ? "before" : "after";

  const bar = $("stagebar");
  bar.className = "stagebar" + (st.ok ? "" : " halt");
  const badgeCls = st.ok ? st.rung_class : "halted";
  $("cap").innerHTML =
    '<span class="badge ' + badgeCls + '">' + esc(st.headline) + '</span>' +
    esc(st.caption);

  $("counter").textContent = "step " + (idx+1) + " / " + steps.length +
    "  ·  " + st.id;
  $("slider").value = idx;
  document.querySelectorAll(".tick").forEach((t,i) =>
    t.classList.toggle("active", i === idx));

  renderDetail(st);
}

function pill(v){
  if (v === true) return '<span class="pill ok">pass</span>';
  if (v === false) return '<span class="pill bad">fail</span>';
  return '<span class="pill na">n/a</span>';
}

function renderDetail(st){
  const rows = [];
  rows.push(["intent", "<code>" + esc(st.intent) + "</code>"]);
  rows.push(["action", esc(st.action || "—")]);
  if (st.rung){
    let r = '<b>' + esc(st.rung_label) + '</b>';
    if (st.confidence != null) r += " · confidence " + st.confidence.toFixed(2);
    if (st.elapsed_ms != null) r += " · " + Math.round(st.elapsed_ms) + " ms";
    rows.push(["resolution rung", r]);
  } else {
    rows.push(["resolution rung", "— (no target to locate)"]);
  }
  if (st.identity){
    rows.push(["identity check",
      '<b>' + esc(st.identity.status) + '</b> (' + esc(st.identity.mode) + ')']);
  }
  if (st.input_verified != null){
    rows.push(["typed input", pill(st.input_verified) + " landed"]);
  }
  rows.push(["postcondition", pill(st.postconditions_ok) +
    " (did the expected screen appear?)"]);
  rows.push(["healed", st.healed
    ? '<span class="pill ok">yes</span>' : '<span class="pill na">no</span>']);

  let html = '<h3>' + esc(st.id) + " — " + esc(st.headline) + '</h3><dl class="rows">';
  rows.forEach(([k,v]) => { html += "<dt>"+esc(k)+"</dt><dd>"+v+"</dd>"; });
  html += "</dl>";

  if (st.heal_diff){
    html += '<div class="diff"><b>heal · anchor_refresh via ' +
      esc(st.heal_diff.rung_used) + '</b> (applied: ' + st.heal_diff.applied + ')';
    st.heal_diff.changed.forEach(c => {
      html += '<div>' + esc(c.field) + ': <span class="old">' +
        esc(JSON.stringify(c.old)) + '</span> → <span class="new">' +
        esc(JSON.stringify(c.new)) + '</span></div>';
    });
    html += '<div style="margin-top:6px;color:var(--muted)">' +
      esc(st.heal_diff.note) + '</div></div>';
  }
  if (st.error){
    html += '<div class="errbox"><b>Run halted here.</b><br>' + esc(st.error) + '</div>';
  }
  $("detail").innerHTML = html;
}

function renderAll(){ renderTabs(); renderSummary(); renderTicks(); renderStep(); }

function stop(){ playing = false; $("play").innerHTML = "&#9654; Play";
  if (timer){ clearInterval(timer); timer = null; } }
function play(){
  playing = true; $("play").innerHTML = "&#10073;&#10073; Pause";
  timer = setInterval(() => {
    const n = run().steps.length;
    if (idx >= n - 1){ stop(); return; }
    idx++; renderStep();
  }, 1400);
}

$("play").onclick = () => { playing ? stop() : play(); };
$("prev").onclick = () => { stop(); idx = Math.max(0, idx-1); renderStep(); };
$("next").onclick = () => { stop(); idx = Math.min(run().steps.length-1, idx+1); renderStep(); };
$("slider").oninput = (e) => { stop(); idx = +e.target.value; renderStep(); };
$("ba").onchange = renderStep;
document.addEventListener("keydown", (e) => {
  if (e.key === "ArrowRight"){ $("next").click(); }
  else if (e.key === "ArrowLeft"){ $("prev").click(); }
  else if (e.key === " "){ e.preventDefault(); $("play").click(); }
});

renderAll();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
