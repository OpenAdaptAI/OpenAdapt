"""Silent-wrong-action-rate benchmark: SCREEN-verify vs EFFECT-verify.

This turns the transactional fault-class matrix
(``tests/test_effect_fault_matrix.py``) into a *measured, publishable metric* —
the "silent wrong-action rate" instrument
(``docs/validation/SILENT_WRONG_ACTION_RATE.md``), pointed at our OWN runtime.
No competitor runs, no paid API, no model calls, localhost only.

The question it answers, numerically: across the MockMed transactional-fault
suite (``mockmed.fault_server``), how often does a **wrong / absent / duplicate
business effect** land while the SCREEN oracle (the weak, vision-style "did the
app paint a saved banner?" check) reports SUCCESS — i.e. the wrong write would
have gone undetected (a *silent wrong-action*) — versus how often the EFFECT
oracle (the #63 :class:`RestRecordVerifier`, reading the real system of record
at ``GET /api/db``) catches it and halts?

Three independent judgments are recorded per run, and the numbers are REAL —
every run actually drives ``fault_server`` and reads back the store:

1. **Ground truth** (``_business_effect``): computed directly off the
   system-of-record snapshot (before vs after), never from either oracle. The
   intended write is exactly one ``p1`` / ``Triage`` encounter carrying this
   run's note, with no pre-existing row destroyed. Anything else — absent,
   duplicate, wrong field (partial save), collateral loss (stale overwrite) —
   is a WRONG business effect.
2. **Screen oracle** (``_screen_shows_success``): the documented app.js
   ``saveViaBackend`` branch rule (``mockmed/static/app.js``) applied to the
   REAL HTTP status(es) the fault backend returned this run. It is the weak
   oracle a vision postcondition encodes: a painted "saved" banner == success.
   (The full end-to-end version driving the real browser + OCR is
   ``benchmark/fault_model/run.py``; here the same rule is evaluated against
   the live server response so the screen verdict is derived, not hardcoded.)
3. **Effect oracle** (``_effect_verify``): the #63 consequential-save contract
   — ``record_written`` (exactly once) AND ``field_equals`` (the note) —
   verified by :class:`RestRecordVerifier` against the system of record.

Headline metrics (all computed from the runs, never hardcoded):

- **silent-wrong-action rate** = fraction of runs where a wrong business
  effect occurred AND the oracle reported success (the wrong write would go
  undetected). Reported for BOTH verification modes; effect-verify should
  drive it to zero.
- **undetected-wrong rate** = the same numerator conditioned only on the runs
  where a wrong effect actually occurred (P[oracle says success | wrong
  effect]) — the cleanest apples-to-apples between the two oracles.
- **false-abort rate** = fraction of correct-effect runs the oracle halted on
  (a safe but costly refusal). Reported for both modes.

Outputs (repo benchmark convention): ``results.json``,
``SILENT_WRONG_ACTION.md``, and ``silent_wrong_action.png``.
"""

from __future__ import annotations

import argparse
import json
import platform
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import requests

from openadapt_flow.mockmed.fault_server import serve as fault_serve
from openadapt_flow.runtime.effects import (
    Effect,
    EffectKind,
    RestRecordVerifier,
    ValueExpr,
)

#: The recorded target the whole suite attacks (mirrors the fault matrix).
TARGET = {"patient_id": "p1", "type": "Triage"}
#: This suite's note; a partial save drops it, so field_equals catches that.
NOTE = "Silent-wrong-action benchmark triage note"
#: Idempotency key used only by the ``idempotent`` (recommended-fix) scenario.
IDEMPOTENCY_KEY = "swa-run-key"
#: Client-side abort window app.js uses for the write (``AbortController``),
#: in seconds; ``timeout`` mode hangs past it server-side after committing.
CLIENT_ABORT_S = 1.2
#: How long the effect verifier may poll the system of record per effect.
EFFECT_TIMEOUT_S = 1.0

DEFAULT_N = 10
DEFAULT_OUT_DIR = "benchmark/silent_wrong_action"


@dataclass(frozen=True)
class Scenario:
    """One transactional-fault scenario driven at the persistence boundary.

    Attributes:
        name: The ``?fault=`` mode forwarded to ``fault_server``.
        delivery: How app.js issues the write under this mode — ``"single"``
            (one POST) or ``"double"`` (double-submit / double-delivered
            click, two POSTs).
        keyed: Whether the write carries the idempotency key (only the
            ``idempotent`` recommended-fix scenario does).
        seed_concurrent: Seed a concurrent actor's row before the run (the
            ``stale`` last-write-wins scenario overwrites it).
        blurb: One-line human description for the report.
    """

    name: str
    delivery: str
    keyed: bool
    seed_concurrent: bool
    blurb: str


#: The full transactional-fault suite (fault_server modes). Order groups the
#: clean control, the five silent-under-screen classes, and the two the screen
#: does flag (timeout false-abort, session safe-halt) plus the recommended fix.
SCENARIOS: tuple[Scenario, ...] = (
    Scenario("ok", "single", False, False, "control: a clean accepted write"),
    Scenario(
        "partial",
        "single",
        False,
        False,
        "backend persisted the row but dropped the note field",
    ),
    Scenario(
        "optimistic",
        "single",
        False,
        False,
        "UI painted success; the server then rejected the write",
    ),
    Scenario(
        "duplicate", "double", False, False, "double-submit: the write landed twice"
    ),
    Scenario(
        "double",
        "double",
        False,
        False,
        "double-delivered click: the write landed twice",
    ),
    Scenario(
        "stale",
        "single",
        False,
        True,
        "last-write-wins destroyed a concurrent actor's row",
    ),
    Scenario(
        "timeout",
        "single",
        False,
        False,
        "row committed, then the client aborted (screen false-abort)",
    ),
    Scenario(
        "session",
        "single",
        False,
        False,
        "session expired (401): nothing persisted (both halt safely)",
    ),
    Scenario(
        "idempotent",
        "double",
        True,
        False,
        "recommended fix: idempotency key collapses the double-submit",
    ),
)


def _post(base: str, mode: str, *, key: Optional[str], timed: bool) -> Optional[int]:
    """Issue one write POST to the fault backend; return its HTTP status.

    Returns ``None`` when the request aborted before a response (the client
    timeout that ``timeout`` mode induces) — exactly the signal app.js's
    ``AbortController`` surfaces to ``showSaveError``.
    """
    payload: dict[str, Any] = {"patient_id": "p1", "type": "Triage", "note": NOTE}
    if key is not None:
        payload["key"] = key
    url = f"{base}/api/encounter?fault={mode}"
    try:
        resp = requests.post(
            url, json=payload, timeout=CLIENT_ABORT_S if timed else 5.0
        )
    except requests.exceptions.RequestException:
        return None
    return resp.status_code


def _drive(base: str, scenario: Scenario) -> list[Optional[int]]:
    """Reproduce the write(s) app.js issues under ``scenario`` and return the
    real HTTP status of each (``None`` for a client-aborted request)."""
    key = IDEMPOTENCY_KEY if scenario.keyed else None
    if scenario.name == "timeout":
        # app.js posts with the abort controller armed; the server commits
        # then hangs past it, so the client sees an aborted request.
        return [_post(base, scenario.name, key=key, timed=True)]
    if scenario.delivery == "double":
        return [
            _post(base, scenario.name, key=key, timed=False),
            _post(base, scenario.name, key=key, timed=False),
        ]
    return [_post(base, scenario.name, key=key, timed=False)]


def _ok(status: Optional[int]) -> bool:
    return status is not None and 200 <= status < 300


def _screen_shows_success(mode: str, statuses: list[Optional[int]]) -> bool:
    """The SCREEN oracle: does app.js paint the "saved" banner this run?

    Encodes ``mockmed/static/app.js`` ``saveViaBackend`` exactly, applied to
    the REAL server response(s):

    - ``optimistic``  -- paints success BEFORE the write resolves, then
      ignores the result: banner regardless of the (rejecting) status.
    - ``timeout``     -- posts with the abort controller; on abort ->
      ``showSaveError`` (no banner). Banner only if a 2xx beat the abort.
    - ``duplicate`` / ``double`` / ``idempotent`` -- two POSTs; the banner is
      shown once if either returned 2xx.
    - ``ok`` / ``partial`` / ``session`` / ``stale`` -- one POST: a 401 bounces
      to ``#login`` (no banner); a 2xx paints the banner; anything else ->
      ``showSaveError``.
    """
    if mode == "optimistic":
        return True
    if mode == "timeout":
        return any(_ok(s) for s in statuses)
    if mode in ("duplicate", "double", "idempotent"):
        return any(_ok(s) for s in statuses)
    status = statuses[0] if statuses else None
    if status == 401:
        return False
    return _ok(status)


def _matches(record: dict[str, Any], selector: dict[str, str]) -> bool:
    return all(str(record.get(k)) == str(v) for k, v in selector.items())


def _business_effect(
    before: list[dict[str, Any]],
    after: list[dict[str, Any]],
    *,
    expected_note: str = NOTE,
    target: dict[str, str] = TARGET,
) -> tuple[bool, str]:
    """Independent ground truth off the system-of-record store (never an oracle).

    The intended effect is exactly one NEW record matching ``target`` with
    ``note == expected_note`` and no pre-existing row destroyed. Returns
    ``(correct, fault_class)`` where ``fault_class`` is ``"correct"`` or one of
    ``absent`` / ``duplicate`` / ``wrong_field`` / ``collateral_loss``.
    """
    before_ids = {r.get("id") for r in before}
    after_ids = {r.get("id") for r in after}
    lost = [r for r in before if r.get("id") not in after_ids]
    if lost:
        return False, "collateral_loss"
    new = [r for r in after if r.get("id") not in before_ids]
    matching = [r for r in new if _matches(r, target)]
    if not matching:
        return False, "absent"
    if len(matching) > 1:
        return False, "duplicate"
    if str(matching[0].get("note")) != expected_note:
        return False, "wrong_field"
    return True, "correct"


def _effect_verify(
    verifier: RestRecordVerifier,
    before: Any,
    *,
    keyed: bool,
) -> Any:
    """The EFFECT oracle: the #63 consequential-save contract against the SoR.

    Both effects must confirm (the runtime gate): ``record_written`` exactly
    once, then ``field_equals`` on the note. The keyed scenario counts records
    bearing the idempotency key, so a de-duplicated double-submit confirms.
    The first non-confirmed effect is the verdict (never guess success).
    """
    written = Effect(
        kind=EffectKind.RECORD_WRITTEN,
        match={k: ValueExpr(literal=v) for k, v in TARGET.items()},
        expected_count=1,
        idempotency_key=ValueExpr(literal=IDEMPOTENCY_KEY) if keyed else None,
        risk="irreversible",
        timeout_s=EFFECT_TIMEOUT_S,
    )
    field = Effect(
        kind=EffectKind.FIELD_EQUALS,
        match={k: ValueExpr(literal=v) for k, v in TARGET.items()},
        field="note",
        value=ValueExpr(literal=NOTE),
        risk="irreversible",
        timeout_s=EFFECT_TIMEOUT_S,
    )
    v1 = verifier.verify(written, before)
    if not v1.confirmed:
        return v1
    return verifier.verify(field, before)


def run_one(base: str, db: Any, scenario: Scenario) -> dict[str, Any]:
    """Drive one scenario once and record all three judgments.

    Args:
        base: Fault-server base URL (no trailing slash).
        db: The ``FaultDB`` ground-truth store (reset per run here).
        scenario: The fault scenario to drive.

    Returns:
        One result row: the screen verdict, the effect verdict, and the
        independent ground-truth business effect, plus the derived
        classifications (silent-wrong / false-abort) for each oracle.
    """
    db.reset(seed_concurrent=scenario.seed_concurrent)
    before_snapshot = db.snapshot()["records"]

    verifier = RestRecordVerifier(base)
    before_state = verifier.capture_pre_state()

    statuses = _drive(base, scenario)

    after_snapshot = db.snapshot()["records"]
    correct, fault_class = _business_effect(before_snapshot, after_snapshot)

    screen_pass = _screen_shows_success(scenario.name, statuses)
    effect_verdict = _effect_verify(verifier, before_state, keyed=scenario.keyed)
    effect_confirmed = effect_verdict.confirmed

    wrong = not correct
    return {
        "scenario": scenario.name,
        "blurb": scenario.blurb,
        "statuses": statuses,
        "records_after": len(after_snapshot),
        "ground_truth_correct": correct,
        "ground_truth_fault": fault_class,
        "screen_pass": screen_pass,
        "effect_verdict": effect_verdict.verdict.value,
        "effect_confirmed": effect_confirmed,
        "effect_reason": effect_verdict.reason,
        # Per-oracle classifications for this run:
        "screen_silent_wrong": wrong and screen_pass,
        "effect_silent_wrong": wrong and effect_confirmed,
        "screen_false_abort": correct and not screen_pass,
        "effect_false_abort": correct and not effect_confirmed,
    }


def _rate(rows: list[dict[str, Any]], key: str) -> float:
    return (sum(1 for r in rows if r[key]) / len(rows)) if rows else 0.0


def aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute the headline metrics and per-scenario breakdown from run rows."""
    n = len(rows)
    wrong_rows = [r for r in rows if not r["ground_truth_correct"]]
    correct_rows = [r for r in rows if r["ground_truth_correct"]]

    def undetected(rows_: list[dict[str, Any]], key: str) -> float:
        return sum(1 for r in rows_ if r[key]) / len(rows_) if rows_ else 0.0

    # Per-scenario summary (verdicts are deterministic per class; N proves
    # reproducibility — flag any within-scenario disagreement honestly).
    per_scenario: dict[str, Any] = {}
    for sc in SCENARIOS:
        srows = [r for r in rows if r["scenario"] == sc.name]
        if not srows:
            continue
        per_scenario[sc.name] = {
            "n": len(srows),
            "blurb": sc.blurb,
            "ground_truth_correct": _all_same(srows, "ground_truth_correct"),
            "ground_truth_fault": _all_same(srows, "ground_truth_fault"),
            "screen_pass": _all_same(srows, "screen_pass"),
            "effect_verdict": _all_same(srows, "effect_verdict"),
            "screen_silent_wrong_rate": _rate(srows, "screen_silent_wrong"),
            "effect_silent_wrong_rate": _rate(srows, "effect_silent_wrong"),
            "records_after": _all_same(srows, "records_after"),
        }

    return {
        "n_runs": n,
        "n_wrong_effect": len(wrong_rows),
        "n_correct_effect": len(correct_rows),
        "screen": {
            "silent_wrong_action_rate": _rate(rows, "screen_silent_wrong"),
            "undetected_wrong_rate": undetected(wrong_rows, "screen_silent_wrong"),
            "false_abort_rate": undetected(correct_rows, "screen_false_abort"),
            "silent_wrong_count": sum(1 for r in rows if r["screen_silent_wrong"]),
            "false_abort_count": sum(1 for r in rows if r["screen_false_abort"]),
        },
        "effect": {
            "silent_wrong_action_rate": _rate(rows, "effect_silent_wrong"),
            "undetected_wrong_rate": undetected(wrong_rows, "effect_silent_wrong"),
            "false_abort_rate": undetected(correct_rows, "effect_false_abort"),
            "silent_wrong_count": sum(1 for r in rows if r["effect_silent_wrong"]),
            "false_abort_count": sum(1 for r in rows if r["effect_false_abort"]),
        },
        "per_scenario": per_scenario,
    }


def _all_same(rows: list[dict[str, Any]], key: str) -> Any:
    """The shared value of ``key`` across rows, or a ``"MIXED:..."`` marker if
    the runs disagree (surfaces any nondeterminism instead of hiding it)."""
    values = {r[key] for r in rows}
    if len(values) == 1:
        return next(iter(values))
    return "MIXED:" + ",".join(str(v) for v in sorted(values, key=str))


def run_benchmark(
    n: int = DEFAULT_N,
    *,
    log: Callable[[str], None] = print,
) -> dict[str, Any]:
    """Run the full fault suite ``n`` times each and assemble the results dict.

    Serves one ``fault_server`` and resets its store per run (the store is the
    system of record both the ground-truth judge and the effect verifier read).
    No model calls; localhost only.

    Args:
        n: Iterations per scenario.
        log: Progress logger.

    Returns:
        The results dict (also written to ``results.json`` by
        :func:`write_outputs`).
    """
    url, db, stop = fault_serve()
    base = url.rstrip("/")
    rows: list[dict[str, Any]] = []
    try:
        for sc in SCENARIOS:
            for i in range(n):
                row = run_one(base, db, sc)
                row["i"] = i
                rows.append(row)
            last = rows[-1]
            log(
                f"{sc.name:11s} gt={last['ground_truth_fault']:15s} "
                f"screen={'PASS' if last['screen_pass'] else 'fail'} "
                f"effect={last['effect_verdict']:13s} "
                f"screen_silent={last['screen_silent_wrong']} "
                f"effect_silent={last['effect_silent_wrong']}"
            )
    finally:
        stop()

    metrics = aggregate(rows)
    return {
        "instrument": "silent-wrong-action-rate (screen-verify vs effect-verify)",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "target": "MockMed transactional-fault suite (mockmed.fault_server)",
        "system_of_record": "GET /api/db (in-process FaultDB ground truth)",
        "effect_contract": (
            "record_written (exactly once) AND field_equals (note), verified "
            "by RestRecordVerifier (#63) against the system of record"
        ),
        "screen_oracle": (
            "app.js saveViaBackend banner rule applied to the real server "
            "response(s) — the weak vision-style oracle"
        ),
        "n_per_scenario": n,
        "scenarios": [sc.name for sc in SCENARIOS],
        "platform": platform.platform(),
        "metrics": metrics,
        "runs": rows,
    }


def render_markdown(results: dict[str, Any]) -> str:
    """Render ``SILENT_WRONG_ACTION.md`` from the results dict."""
    m = results["metrics"]
    s = m["screen"]
    e = m["effect"]
    date = results["generated_at"][:10]

    header = (
        "| scenario | ground-truth effect | screen-verify | effect-verify | "
        "silent under screen? |\n"
        "|---|---|---|---|---|\n"
    )
    body = ""
    for name in results["scenarios"]:
        ps = m["per_scenario"][name]
        gt = (
            "correct"
            if ps["ground_truth_correct"] is True
            else (f"WRONG ({ps['ground_truth_fault']})")
        )
        screen = (
            "pass"
            if ps["screen_pass"] is True
            else ("fail" if ps["screen_pass"] is False else str(ps["screen_pass"]))
        )
        effect = ps["effect_verdict"]
        silent = (
            "YES — silent wrong-action" if ps["screen_silent_wrong_rate"] else ("—")
        )
        body += f"| `{name}` | {gt} | {screen} | {effect} | {silent} |\n"

    return f"""# Silent-wrong-action rate: screen-verify vs effect-verify (measured)

Date: {date}. This is the [silent wrong-action rate
instrument](../../docs/validation/SILENT_WRONG_ACTION_RATE.md) reduced to a
number and pointed at our OWN runtime — the transactional fault-class matrix
(`tests/test_effect_fault_matrix.py`) turned into a measured metric. Every
figure below comes from actually running the MockMed transactional-fault
suite (`mockmed.fault_server`) {results["n_per_scenario"]} times per scenario
and reading the real system of record; nothing is hardcoded. No model calls,
localhost only.

![silent-wrong-action and false-abort rate](silent_wrong_action.png)

## Headline

Over **{m["n_runs"]} runs** across {len(results["scenarios"])} transactional
fault scenarios ({m["n_wrong_effect"]} of which produced a genuinely wrong /
absent / duplicate business effect, judged independently against the system of
record):

| metric | screen-verify (weak oracle) | effect-verify (#63) |
|---|---|---|
| **silent-wrong-action rate** (wrong effect ∧ oracle says success, over all runs) | **{s["silent_wrong_action_rate"]:.1%}** ({s["silent_wrong_count"]}/{m["n_runs"]}) | **{e["silent_wrong_action_rate"]:.1%}** ({e["silent_wrong_count"]}/{m["n_runs"]}) |
| **undetected-wrong rate** (oracle says success \\| a wrong effect occurred) | **{s["undetected_wrong_rate"]:.1%}** | **{e["undetected_wrong_rate"]:.1%}** |
| **false-abort rate** (oracle halts \\| the effect was correct) | {s["false_abort_rate"]:.1%} ({s["false_abort_count"]} run(s)) | {e["false_abort_rate"]:.1%} ({e["false_abort_count"]} run(s)) |

The screen oracle silently passes a wrong write in **{s["undetected_wrong_rate"]:.0%}**
of the runs where one occurred; the effect verifier drives that to
**{e["undetected_wrong_rate"]:.0%}** by reading the record instead of the
pixels — and, as a bonus, converts the screen's `timeout` false-abort (the row
landed but the screen reported failure) into a correct CONFIRMED, so it also
has the lower false-abort rate.

## Per-scenario detail

Verdicts are deterministic per fault class (a `MIXED:` marker would flag any
run-to-run disagreement); N proves reproducibility.

{header}{body}
## What each column means

- **ground-truth effect** — computed straight off the system-of-record store
  (before vs after), never from an oracle. `correct` = exactly one `p1` /
  `Triage` encounter with this run's note and no pre-existing row destroyed.
- **screen-verify** — the documented `app.js` "saved banner" rule applied to
  the real server response(s): the weak, vision-style oracle. It `pass`es for
  every one of the five silent classes (`partial`, `optimistic`, `duplicate`,
  `double`, `stale`) — a partial save, a phantom optimistic success, a
  double-write, and a lost update all leave the banner painted.
- **effect-verify** — the #63 `RestRecordVerifier` consequential-save contract
  (`record_written` exactly once AND `field_equals` on the note) against
  `GET /api/db`. It `refuted`s every wrong effect and `confirmed`s the clean
  control, the idempotent fix, and the committed-then-timed-out write.

## Reproduce

```
.venv/bin/python -m openadapt_flow.benchmark.silent_wrong_action \\
    --out {DEFAULT_OUT_DIR} --n {results["n_per_scenario"]}
```

Serves `mockmed.fault_server` locally, drives each fault scenario, and reads
the real store. $0, no network beyond localhost, no model calls. The
qualitative claim (screen-verify has a nonzero silent rate; effect-verify
drives it to zero) is pinned in CI by
`tests/test_silent_wrong_action_benchmark.py`.
"""


def render_chart(results: dict[str, Any], out_png: Path) -> Path:
    """Render the silent-wrong-action and false-abort comparison chart.

    Two panels (screen-verify vs effect-verify): silent-wrong-action rate and
    false-abort rate. The metric a record system cares about — a wrong write
    that reports success — collapses from a large bar to zero.
    """
    from openadapt_flow.benchmark.chart_fonts import configure_bundled_font

    plt = configure_bundled_font()

    surface = "#fcfcfb"
    ink = "#0b0b0b"
    ink2 = "#52514e"
    danger = "#d64545"  # screen-verify: the blind oracle
    safe = "#1baf7a"  # effect-verify: reads the record

    m = results["metrics"]
    modes = ["screen-verify", "effect-verify"]
    colors = [danger, safe]

    fig, (ax_silent, ax_abort) = plt.subplots(
        1, 2, figsize=(9.6, 4.2), facecolor=surface
    )
    fig.suptitle(
        "Silent wrong-action rate — screen-verify vs effect-verify "
        "(MockMed transactional faults)",
        color=ink,
        fontsize=11.5,
    )

    def style(ax: Any, title: str) -> None:
        ax.set_facecolor(surface)
        ax.set_title(title, color=ink, fontsize=10)
        ax.set_ylabel("rate", color=ink2, fontsize=9)
        ax.tick_params(colors=ink2, labelsize=9)
        for spine in ("top", "right"):
            ax.spines[spine].set_visible(False)
        for spine in ("left", "bottom"):
            ax.spines[spine].set_color(ink2)
        ax.grid(axis="y", color="#e6e5e0", linewidth=0.8, zorder=0)
        ax.set_axisbelow(True)
        ax.set_ylim(0, 1.0)

    def panel(ax: Any, title: str, values: list[float]) -> None:
        bars = ax.bar(modes, values, color=colors, width=0.5, zorder=2)
        style(ax, title)
        for bar, value in zip(bars, values):
            ax.annotate(
                f"{value:.0%}",
                (bar.get_x() + bar.get_width() / 2, value),
                ha="center",
                va="bottom",
                fontsize=10,
                color=ink,
            )

    panel(
        ax_silent,
        "Undetected wrong-action rate\n(oracle says success | wrong effect)",
        [m["screen"]["undetected_wrong_rate"], m["effect"]["undetected_wrong_rate"]],
    )
    panel(
        ax_abort,
        "False-abort rate\n(oracle halts | effect correct)",
        [m["screen"]["false_abort_rate"], m["effect"]["false_abort_rate"]],
    )

    fig.tight_layout(rect=(0, 0, 1, 0.92))
    out_png.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_png, dpi=160, facecolor=surface)
    plt.close(fig)
    return out_png


def write_outputs(results: dict[str, Any], out_dir: Path) -> None:
    """Write ``results.json``, ``SILENT_WRONG_ACTION.md``, and the chart PNG."""
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "results.json").write_text(json.dumps(results, indent=2) + "\n")
    (out_dir / "SILENT_WRONG_ACTION.md").write_text(render_markdown(results))
    from openadapt_flow.benchmark.chart_fonts import safe_render

    safe_render(render_chart, results, out_dir / "silent_wrong_action.png")


def run_and_write(
    out_dir: Path | str = DEFAULT_OUT_DIR,
    *,
    n: int = DEFAULT_N,
    log: Callable[[str], None] = print,
) -> dict[str, Any]:
    """Run the benchmark and write all artifacts to ``out_dir``."""
    out = Path(out_dir)
    results = run_benchmark(n=n, log=log)
    write_outputs(results, out)
    m = results["metrics"]
    log(
        "\nsilent-wrong-action rate  screen="
        f"{m['screen']['silent_wrong_action_rate']:.1%}  "
        f"effect={m['effect']['silent_wrong_action_rate']:.1%}  "
        "(undetected-wrong screen="
        f"{m['screen']['undetected_wrong_rate']:.0%} "
        f"effect={m['effect']['undetected_wrong_rate']:.0%})"
    )
    log(
        f"Wrote {out / 'results.json'}, SILENT_WRONG_ACTION.md, silent_wrong_action.png"
    )
    return results


def main(argv: Optional[list[str]] = None) -> int:
    """CLI: ``python -m openadapt_flow.benchmark.silent_wrong_action``."""
    parser = argparse.ArgumentParser(
        description=(
            "Silent-wrong-action-rate benchmark: measure how often "
            "screen-verify silently passes a wrong/absent/duplicate write on "
            "the MockMed transactional-fault suite versus effect-verify "
            "(#63) catching it. $0, localhost only, no model calls."
        )
    )
    parser.add_argument(
        "--out",
        default=DEFAULT_OUT_DIR,
        help=f"output directory (default: {DEFAULT_OUT_DIR})",
    )
    parser.add_argument(
        "--n",
        type=int,
        default=DEFAULT_N,
        help=f"iterations per fault scenario (default: {DEFAULT_N})",
    )
    args = parser.parse_args(argv)
    run_and_write(args.out, n=args.n)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
