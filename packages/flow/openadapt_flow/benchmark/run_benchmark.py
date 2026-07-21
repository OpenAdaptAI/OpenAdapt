"""Benchmark orchestrator: compiled replay vs. computer-use agent.

Records and compiles the MockMed triage demo once, then runs two arms
against identical fresh MockMed instances (fresh chromium browser + page per
run; MockMed state lives entirely in the page):

- **compiled**: replay the compiled bundle ``n_compiled`` times.
- **agent**: a Claude computer-use agent (``agent_baseline``) given the same
  task as natural language, ``n_agent`` times.

Both arms are judged by the same criterion (``verify.verify_encounter_saved``
on a screenshot of the final state). Each arm also runs once against
``?drift=theme`` (compiled with healing enabled as always; agent as-is).

Outputs written to ``out_dir``: ``results.json`` (per-run rows +
aggregates), ``BENCHMARK.md`` (methodology + numbers + caveats), and
``latency_cost.png``.
"""

from __future__ import annotations

import json
import platform
import statistics
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from openadapt_flow.bench import _percentile
from openadapt_flow.benchmark import agent_baseline
from openadapt_flow.benchmark.verify import verify_encounter_saved

N_COMPILED = 100
N_AGENT = 20
NOTE_TEXT = "Follow-up in 2 weeks; BP recheck."
WORKFLOW_NAME = "triage-benchmark"


def _compiled_run(
    bundle_dir: Path,
    url: str,
    run_dir: Path,
    note_text: str,
    *,
    verify_fn: Callable[[bytes, str], Any] = verify_encounter_saved,
    save_final_to: Path | None = None,
    headed: bool = False,
) -> dict[str, Any]:
    """One compiled-replay run against a fresh browser; verified via OCR.

    Args:
        bundle_dir: Compiled workflow bundle.
        url: Target app URL (may carry a drift query).
        run_dir: Scratch run directory for replay artifacts.
        note_text: Note parameter value.
        verify_fn: Arm-independent success check applied to the final
            screenshot; extra fields of its result (beyond ``success``)
            are merged into the row.
        save_final_to: Optional path to save the final screenshot to (for
            post-hoc audit of the OCR verdict).
        headed: Run the browser headed.

    Returns:
        A per-run row dict (arm, wall_s, success, token/cost fields = 0).
    """
    from openadapt_flow.backends.playwright_backend import PlaywrightBackend
    from openadapt_flow.ir import Workflow
    from openadapt_flow.runtime import Replayer

    workflow = Workflow.load(bundle_dir)
    backend, close = PlaywrightBackend.launch(url, headless=not headed)
    try:
        start = time.monotonic()
        report = Replayer(backend).run(
            workflow,
            params={"note": note_text},
            bundle_dir=bundle_dir,
            run_dir=run_dir,
        )
        wall_s = time.monotonic() - start
        final_png = backend.screenshot()
        verdict = verify_fn(final_png, note_text)
        if save_final_to is not None:
            save_final_to.parent.mkdir(parents=True, exist_ok=True)
            save_final_to.write_bytes(final_png)
    finally:
        close()
    failed = [r for r in report.results if not r.ok]
    row = {
        "arm": "compiled",
        "wall_s": wall_s,
        "success": verdict.success,
        "replayer_success": report.success,
        "heal_count": report.heal_count,
        # Identity-protection coverage of the bundle (constant across
        # runs of the same bundle; aggregated into the arm summary and
        # surfaced in BENCHMARK.md methodology): unarmed clicks proceed
        # with NO identity verification (docs/LIMITS.md).
        "identity_applicable_steps": report.identity_applicable_steps,
        "identity_armed_steps": report.identity_armed_steps,
        "identity_unarmed": [
            {"step_id": u.step_id, "reason": u.reason} for u in report.identity_unarmed
        ],
        "actions": len(report.results),
        "first_failure": (
            {"step": failed[0].step_id, "error": failed[0].error} if failed else None
        ),
        "api_calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "cost_usd": 0.0,
        "error": None,
    }
    row.update(verdict.model_dump(exclude={"success"}))
    return row


def _agent_run(
    url: str,
    note_text: str,
    *,
    task: str | None = None,
    verify_fn: Callable[[bytes, str], Any] = verify_encounter_saved,
    save_final_to: Path | None = None,
    client: Any = None,
    headed: bool = False,
    max_actions: int = agent_baseline.MAX_ACTIONS,
    max_cost_usd: float = agent_baseline.MAX_COST_USD,
    log: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """One computer-use-agent run against a fresh browser; verified via OCR.

    Args:
        url: Target app URL (may carry a drift query).
        note_text: Note the agent is asked to enter.
        task: Task prompt; defaults to the MockMed triage prompt.
        verify_fn: Arm-independent success check applied to the final
            screenshot; extra fields of its result (beyond ``success``)
            are merged into the row.
        save_final_to: Optional path to save the final screenshot to (for
            post-hoc audit of the OCR verdict).
        client: Optional injected Anthropic client (tests).
        headed: Run the browser headed.
        max_actions: Action budget forwarded to the agent loop.
        max_cost_usd: Per-run cost cap forwarded to the agent loop.
        log: Per-API-call usage logger forwarded to the agent loop.

    Returns:
        A per-run row dict. API failures are recorded as failed rows with
        ``error`` set, never raised; a failed row still carries whatever
        usage/cost the run paid for before crashing (via
        :class:`~openadapt_flow.benchmark.agent_baseline.UsageLedger`), so
        crashed runs' real spend counts in aggregates and cost ceilings.
    """
    from openadapt_flow.backends.playwright_backend import PlaywrightBackend

    if task is None:
        task = agent_baseline.triage_task_prompt(note_text)
    # The ledger is updated by run_agent after every paid API call, so the
    # except branch below can still account for a crashed run's spend.
    ledger = agent_baseline.UsageLedger()
    backend, close = PlaywrightBackend.launch(url, headless=not headed)
    try:
        try:
            result = agent_baseline.run_agent(
                backend,
                task,
                client=client,
                max_actions=max_actions,
                max_cost_usd=max_cost_usd,
                log=log,
                ledger=ledger,
            )
            verdict = verify_fn(result.final_screenshot, note_text)
            if save_final_to is not None:
                save_final_to.parent.mkdir(parents=True, exist_ok=True)
                save_final_to.write_bytes(result.final_screenshot)
        except Exception as exc:  # noqa: BLE001 - a failed run is a data point
            return {
                "arm": "agent",
                "wall_s": 0.0,
                "success": False,
                "actions": 0,
                "api_calls": ledger.api_calls,
                "input_tokens": ledger.input_tokens,
                "output_tokens": ledger.output_tokens,
                "cache_creation_input_tokens": (ledger.cache_creation_input_tokens),
                "cache_read_input_tokens": ledger.cache_read_input_tokens,
                "cost_usd": ledger.cost_usd,
                "stopped": "error",
                "model_stop_reason": None,
                "error": f"{type(exc).__name__}: {exc}",
            }
    finally:
        try:
            close()
        except Exception:  # noqa: BLE001, S110
            # Teardown failure must not discard the row (and with it the
            # run's recorded spend) by replacing the return with a raise.
            pass
    row = {
        "arm": "agent",
        "wall_s": result.wall_s,
        "success": verdict.success,
        "actions": result.actions,
        "api_calls": result.api_calls,
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
        "cache_creation_input_tokens": result.cache_creation_input_tokens,
        "cache_read_input_tokens": result.cache_read_input_tokens,
        "cost_usd": result.cost_usd,
        "stopped": result.stopped,
        "model_stop_reason": result.model_stop_reason,
        "error": None,
    }
    row.update(verdict.model_dump(exclude={"success"}))
    return row


def _arm_aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate per-run rows for one arm.

    Args:
        rows: Per-run row dicts (as produced by the run helpers).

    Returns:
        Aggregate dict: n, success counts/rate, latency percentiles, action
        mean, token totals, cost per run and total.
    """
    n = len(rows)
    successes = sum(1 for r in rows if r["success"])
    walls = [r["wall_s"] for r in rows]
    costs = [r["cost_usd"] for r in rows]
    return {
        "n": n,
        "success_count": successes,
        "success_rate": (successes / n) if n else 0.0,
        "wall_s_p50": _percentile(walls, 50.0),
        "wall_s_p95": _percentile(walls, 95.0),
        "wall_s_mean": statistics.fmean(walls) if walls else 0.0,
        "actions_mean": (statistics.fmean(r["actions"] for r in rows) if rows else 0.0),
        "input_tokens_total": sum(r["input_tokens"] for r in rows),
        "output_tokens_total": sum(r["output_tokens"] for r in rows),
        "cache_creation_input_tokens_total": sum(
            r.get("cache_creation_input_tokens", 0) for r in rows
        ),
        "cache_read_input_tokens_total": sum(
            r.get("cache_read_input_tokens", 0) for r in rows
        ),
        "cost_usd_per_run": statistics.fmean(costs) if costs else 0.0,
        "cost_usd_total": sum(costs),
        **_identity_coverage_aggregate(rows),
    }


def _identity_coverage_aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Identity-protection coverage summary for a compiled arm.

    The coverage is a property of the BUNDLE (constant across runs), so
    the first row that carries it speaks for the arm. Agent rows (and
    results.json files produced before 2026-07-10) carry no coverage
    fields and yield an empty dict — the markdown renderers then note the
    metric was not captured.
    """
    for r in rows:
        if "identity_applicable_steps" in r:
            return {
                "identity_applicable_steps": r["identity_applicable_steps"],
                "identity_armed_steps": r["identity_armed_steps"],
                "identity_unarmed": r.get("identity_unarmed", []),
            }
    return {}


def identity_coverage_block(compiled_agg: dict[str, Any]) -> str:
    """Markdown methodology bullet for identity-protection coverage.

    Shared by the MockMed and OpenEMR BENCHMARK.md renderers.
    """
    if "identity_applicable_steps" not in compiled_agg:
        return (
            "- **Identity-protection coverage: not captured in this "
            "results.json.** The armed-coverage metric was added to the "
            "generator on 2026-07-10; future runs report how many click "
            "steps carry the pre-click identity check and list the "
            "unarmed steps (which proceed with NO identity verification "
            "— see docs/LIMITS.md)."
        )
    applicable = compiled_agg["identity_applicable_steps"]
    armed = compiled_agg["identity_armed_steps"]
    unarmed = compiled_agg.get("identity_unarmed", [])
    lines = [
        f"- **Identity-protection coverage (compiled arm): {armed} of "
        f"{applicable} click steps identity-armed.** Unarmed clicks "
        "proceed with NO identity verification (docs/LIMITS.md); the "
        "success rates above therefore measure task completion, not "
        "wrong-target immunity, on the unarmed steps."
    ]
    for u in unarmed:
        lines.append(f"  - unarmed `{u['step_id']}`: {u['reason']}")
    return "\n".join(lines)


def aggregate_results(
    compiled_rows: list[dict[str, Any]],
    agent_rows: list[dict[str, Any]],
    drift: dict[str, Any],
    *,
    note_text: str,
) -> dict[str, Any]:
    """Assemble the full results document from per-run rows.

    Args:
        compiled_rows: Rows from the compiled arm.
        agent_rows: Rows from the agent arm.
        drift: ``{"compiled": row, "agent": row}`` from the drift=theme pass.
        note_text: The note both arms entered.

    Returns:
        The results dict serialized to ``results.json``.
    """
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "task": (
            "MockMed triage: login, open first referral, create a Triage "
            "encounter, enter the note, save"
        ),
        "note_text": note_text,
        "model": agent_baseline.MODEL,
        "computer_tool": agent_baseline.COMPUTER_TOOL_TYPE,
        "beta_header": agent_baseline.COMPUTER_USE_BETA,
        "pricing_usd_per_mtok": {
            "input": agent_baseline.INPUT_USD_PER_MTOK,
            "output": agent_baseline.OUTPUT_USD_PER_MTOK,
            "note": (
                "list price; an introductory $2/$10 rate applies through 2026-08-31"
            ),
        },
        "platform": platform.platform(),
        "arms": {
            "compiled": _arm_aggregate(compiled_rows),
            "agent": _arm_aggregate(agent_rows),
        },
        "drift_theme": drift,
        "runs": {"compiled": compiled_rows, "agent": agent_rows},
    }


def render_chart(results: dict[str, Any], out_png: Path) -> Path:
    """Render the latency + cost comparison chart to a PNG.

    Two panels (one measure per axis, no dual axes): latency per run on a
    log scale (the arms differ by orders of magnitude) and cost per run on
    a linear scale (the compiled arm is exactly $0).

    Args:
        results: The aggregate results dict.
        out_png: Output PNG path.

    Returns:
        The written PNG path.
    """
    from openadapt_flow.benchmark.chart_fonts import configure_bundled_font

    plt = configure_bundled_font()

    surface = "#fcfcfb"
    ink = "#0b0b0b"
    ink2 = "#52514e"
    series = {"compiled": "#2a78d6", "agent": "#1baf7a"}

    compiled = results["arms"]["compiled"]
    agent = results["arms"]["agent"]

    fig, (ax_lat, ax_cost) = plt.subplots(1, 2, figsize=(9.6, 4.2), facecolor=surface)
    fig.suptitle(
        "Compiled replay vs. computer-use agent — same task, same check",
        color=ink,
        fontsize=12,
    )

    def style(ax: Any, title: str, ylabel: str) -> None:
        ax.set_facecolor(surface)
        ax.set_title(title, color=ink, fontsize=10)
        ax.set_ylabel(ylabel, color=ink2, fontsize=9)
        ax.tick_params(colors=ink2, labelsize=9)
        for spine in ("top", "right"):
            ax.spines[spine].set_visible(False)
        for spine in ("left", "bottom"):
            ax.spines[spine].set_color(ink2)
        ax.grid(axis="y", color="#e6e5e0", linewidth=0.8, zorder=0)
        ax.set_axisbelow(True)

    # Latency panel (log scale: the arms differ by orders of magnitude).
    labels = ["compiled\np50", "compiled\np95", "agent\np50", "agent\np95"]
    values = [
        compiled["wall_s_p50"],
        compiled["wall_s_p95"],
        agent["wall_s_p50"],
        agent["wall_s_p95"],
    ]
    colors = [
        series["compiled"],
        series["compiled"],
        series["agent"],
        series["agent"],
    ]
    bars = ax_lat.bar(labels, values, color=colors, width=0.55, zorder=2)
    ax_lat.set_yscale("log")
    style(ax_lat, "Latency per run (log scale)", "seconds")
    for bar, value in zip(bars, values):
        ax_lat.annotate(
            f"{value:.1f}s",
            (bar.get_x() + bar.get_width() / 2, value),
            ha="center",
            va="bottom",
            fontsize=9,
            color=ink,
        )

    # Cost panel (linear: compiled is exactly $0, which log cannot show).
    cost_labels = ["compiled", "agent"]
    cost_values = [compiled["cost_usd_per_run"], agent["cost_usd_per_run"]]
    bars = ax_cost.bar(
        cost_labels,
        cost_values,
        color=[series["compiled"], series["agent"]],
        width=0.45,
        zorder=2,
    )
    style(ax_cost, "Model cost per run", "USD")
    for bar, value in zip(bars, cost_values):
        ax_cost.annotate(
            f"${value:.4f}" if value else "$0",
            (bar.get_x() + bar.get_width() / 2, value),
            ha="center",
            va="bottom",
            fontsize=9,
            color=ink,
        )

    fig.tight_layout(rect=(0, 0, 1, 0.94))
    out_png.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_png, dpi=160, facecolor=surface)
    plt.close(fig)
    return out_png


def _drift_line(row: dict[str, Any]) -> str:
    """One-line outcome summary for a drift row."""
    outcome = "succeeded" if row["success"] else "FAILED"
    extra = ""
    if row["arm"] == "compiled":
        extra = f", {row['heal_count']} heals"
    else:
        extra = f", {row['actions']} actions, ${row['cost_usd']:.4f}"
    return f"{outcome} in {row['wall_s']:.1f}s{extra}"


def render_markdown(results: dict[str, Any]) -> str:
    """Render ``BENCHMARK.md`` from the results dict.

    Args:
        results: The aggregate results dict.

    Returns:
        The markdown document as a string.
    """
    c = results["arms"]["compiled"]
    a = results["arms"]["agent"]
    drift = results["drift_theme"]
    date = results["generated_at"][:10]
    identity_block = identity_coverage_block(c)
    return f"""# Benchmark: compiled replay vs. computer-use agent

Date: {date}. One task, two ways to automate it, one success check.

**Task** (MockMed, the bundled demo clinic app; fake data only): sign in as
`nurse.demo`, open the first referral task, create a New Encounter of type
Triage, enter a note, save.

![latency and cost](latency_cost.png)

| | compiled replay | computer-use agent |
|---|---|---|
| runs | {c["n"]} | {a["n"]} |
| success rate | {c["success_rate"]:.0%} ({c["success_count"]}/{c["n"]}) \
| {a["success_rate"]:.0%} ({a["success_count"]}/{a["n"]}) |
| latency p50 | {c["wall_s_p50"]:.1f} s | {a["wall_s_p50"]:.1f} s |
| latency p95 | {c["wall_s_p95"]:.1f} s | {a["wall_s_p95"]:.1f} s |
| model cost / run | $0 | ${a["cost_usd_per_run"]:.4f} |
| total model cost | $0 | ${a["cost_usd_total"]:.2f} |
| tokens (uncached in / out, total) | 0 / 0 | {a["input_tokens_total"]:,} / \
{a["output_tokens_total"]:,} |

## Drift (`?drift=theme`, one run per arm)

MockMed re-rendered with a dark palette, which invalidates every recorded
template crop:

- compiled (healing on): {_drift_line(drift["compiled"])}
- agent (as-is): {_drift_line(drift["agent"])}

## Methodology

- **Record + compile once.** The demo is recorded through the Playwright
  demo driver and compiled into a vision-anchored bundle
  (`openadapt-flow demo-record` + `compile`). Recording and compiling are a
  one-time cost and are not included in per-run latency.
- **Identical environments.** Each run of either arm gets a fresh chromium
  browser + page against the same locally served MockMed app (app state
  lives entirely in the page, so a fresh page is a fresh instance).
- **Same interface.** Both arms drive the same `PlaywrightBackend`,
  vision-only: PNG screenshots in, pixel-coordinate clicks / typed text /
  key presses out. Neither arm uses DOM selectors at run time.
- **Agent arm.** Model `{results["model"]}` with the
  `{results["computer_tool"]}` computer-use tool (beta header
  `{results["beta_header"]}`), a 25-action budget, and history bounded to
  the last 3 screenshots. The task prompt states user intent (the numbered
  task above), not steps or coordinates. Every executed action returns a
  settled screenshot, using the same settle logic the replayer uses.
- **Same success criterion.** After each run, a screenshot of the final
  state is checked by OCR (`openadapt_flow.vision.find_text`): the
  `Encounter saved — <note>` banner AND the `Triage — <note>` encounter row
  must both be visible. Neither arm's self-reported success is used.
- **Latency** is wall-clock around the replay / agent loop only (browser
  and server startup excluded for both arms).
- **Cost** is computed from API `usage` token counts at list pricing
  (${results["pricing_usd_per_mtok"]["input"]:.2f} /
  ${results["pricing_usd_per_mtok"]["output"]:.2f} per MTok input/output
  for {results["model"]}). An introductory $2/$10 rate applies through
  2026-08-31, so billed cost today is about a third lower than reported.
  Compiled replay makes zero model calls.
{identity_block}

## Caveats — read before quoting these numbers

- **MockMed is a simple app.** Five screens, no scrolling, no popups, high
  contrast, big labels. It is close to a best case for both arms; harder
  apps would slow and likely degrade both, plausibly at different rates.
- **The agent arm has a smaller N** ({a["n"]} vs {c["n"]}) because agent
  runs cost real money and minutes. Its success rate carries wider error
  bars.
- **Model version pinned.** Results describe `{results["model"]}` with the
  `{results["computer_tool"]}` tool on {date}; newer models will differ.
- **The compiled arm needs a demonstration first.** The one-time
  record + compile step (about a minute of human demonstration) is the
  price of the fast replays; the agent needs only the prompt.
- **Drift is n=1 per arm** — an existence result, not a rate.
- **Latency includes deliberate settle waits** (screenshot stability
  polling) in both arms; a tuned production loop could shave both.
- Single machine ({results["platform"]}), local server, no network
  variance in the compiled arm; agent latency includes real API round
  trips.

## Reproduce

```
openadapt-flow benchmark --n-compiled {c["n"]} --n-agent {a["n"]} --out benchmark/
```

Requires `ANTHROPIC_API_KEY` (or `~/.anthropic/api_key`). The agent arm
costs real money (about ${a["cost_usd_total"]:.2f} at list price for
{a["n"]} runs when this was generated).
"""


def write_outputs(results: dict[str, Any], out_dir: Path) -> None:
    """Write ``results.json``, ``BENCHMARK.md``, and the chart PNG.

    Args:
        results: The aggregate results dict.
        out_dir: Output directory (created if needed).
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "results.json").write_text(json.dumps(results, indent=2) + "\n")
    from openadapt_flow.benchmark.chart_fonts import safe_render

    safe_render(render_chart, results, out_dir / "latency_cost.png")
    (out_dir / "BENCHMARK.md").write_text(render_markdown(results))


def run_benchmark(
    out_dir: Path | str,
    *,
    n_compiled: int = N_COMPILED,
    n_agent: int = N_AGENT,
    note_text: str = NOTE_TEXT,
    headed: bool = False,
    agent_client: Any = None,
    log: Callable[[str], None] = print,
) -> dict[str, Any]:
    """Run the full benchmark and write all outputs.

    Args:
        out_dir: Directory for ``results.json`` / ``BENCHMARK.md`` / chart.
        n_compiled: Compiled-replay iterations.
        n_agent: Agent iterations.
        note_text: Note both arms enter.
        headed: Run browsers headed (debugging).
        agent_client: Optional injected Anthropic client (tests).
        log: Progress logger (default ``print``).

    Returns:
        The results dict (also written to ``results.json``).
    """
    from openadapt_flow.compiler import compile_recording
    from openadapt_flow.demo_driver import record_triage_demo
    from openadapt_flow.mockmed.server import serve

    out = Path(out_dir)
    url, stop = serve(port=0)
    try:
        with tempfile.TemporaryDirectory(prefix="oaf-bench-") as tmp_str:
            tmp = Path(tmp_str)
            log("Recording demo...")
            recording = record_triage_demo(
                url, tmp / "recording", note_text=note_text, headed=headed
            )
            bundle = tmp / "bundle"
            compile_recording(recording, bundle, name=WORKFLOW_NAME)
            log(f"Compiled bundle: {bundle}")

            compiled_rows: list[dict[str, Any]] = []
            for i in range(n_compiled):
                row = _compiled_run(
                    bundle,
                    url,
                    tmp / "runs" / f"compiled_{i:03d}",
                    note_text,
                    headed=headed,
                )
                row["i"] = i
                compiled_rows.append(row)
                log(
                    f"compiled {i + 1}/{n_compiled}: "
                    f"success={row['success']} {row['wall_s']:.1f}s"
                )

            agent_rows: list[dict[str, Any]] = []
            for i in range(n_agent):
                row = _agent_run(url, note_text, client=agent_client, headed=headed)
                row["i"] = i
                agent_rows.append(row)
                log(
                    f"agent {i + 1}/{n_agent}: success={row['success']} "
                    f"{row['wall_s']:.1f}s ${row['cost_usd']:.4f} "
                    f"actions={row['actions']} err={row['error']}"
                )

            drift_target = f"{url.rstrip('/')}/?drift=theme"
            log("drift=theme: compiled arm...")
            drift_compiled = _compiled_run(
                bundle,
                drift_target,
                tmp / "runs" / "drift_compiled",
                note_text,
                headed=headed,
            )
            log(f"drift compiled: {_drift_line(drift_compiled)}")
            log("drift=theme: agent arm...")
            drift_agent = _agent_run(
                drift_target, note_text, client=agent_client, headed=headed
            )
            log(f"drift agent: {_drift_line(drift_agent)}")
    finally:
        stop()

    results = aggregate_results(
        compiled_rows,
        agent_rows,
        {"compiled": drift_compiled, "agent": drift_agent},
        note_text=note_text,
    )
    write_outputs(results, out)
    log(f"Wrote {out / 'results.json'}, BENCHMARK.md, latency_cost.png")
    return results
