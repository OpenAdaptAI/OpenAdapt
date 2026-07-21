"""Compiled replay vs. computer-use agent — a comparison artifact.

This is a DETERMINISTIC generator. It reads the two *real* head-to-head
benchmark result files that already live in this repo — it invents no numbers
and runs nothing costly (zero Anthropic calls, zero network) — and lays their
figures out as a self-contained, theme-aware HTML page:

- ``benchmark/openemr/results.json`` — the LEAD result: the same task run
  against a real third-party application (the official OpenEMR public demo),
  20 compiled replays vs 10 ``claude-sonnet-5`` computer-use agent runs, both
  100%. This is a field result, not CI-reproducible (shared public instance).
- ``benchmark/results.json`` — the CI-reproducible MockMed anchor: 100 compiled
  vs 20 agent on the bundled demo clinic, both 100%, same orchestrator and same
  arm-independent OCR success check.

Every figure rendered on the page is pulled from one of those two
``results.json`` files. The single figure that exists only in prose (the
one-time human demonstration cost) is quoted from ``BENCHMARK.md`` and labelled
with its source. Nothing is hand-typed.

Regenerate both artifacts with::

    python -m benchmark.comparison_artifact.generate

It emits, into ``benchmark/comparison_artifact/``:

- ``comparison.html`` — the self-contained page (inline CSS, inline SVG charts,
  no external assets, no base64 needed; light/dark theme-aware). It shares the
  design vocabulary of the wrong-patient safety gallery so the two read as a
  matched set.
- ``comparison.json`` — the exact figures extracted from the source files (with
  their provenance), so the page is verifiable without eyeballing it.

The page leads with the honest wedge: compiled replay is model-free, ~$0/run,
and faster, at *parity* success on these tasks — with the small-n, shared-demo,
and not-a-capability-claim caveats stated up front, not buried.
"""

from __future__ import annotations

import argparse
import html
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent

OPENEMR_RESULTS = REPO_ROOT / "benchmark" / "openemr" / "results.json"
OPENEMR_MD = REPO_ROOT / "benchmark" / "openemr" / "BENCHMARK.md"
MOCKMED_RESULTS = REPO_ROOT / "benchmark" / "results.json"
MOCKMED_MD = REPO_ROOT / "benchmark" / "BENCHMARK.md"

# The one-time human demonstration cost is stated in prose only (both
# BENCHMARK.md files: "about a minute of human demonstration"). We surface it
# with this explicit provenance rather than pretending it came from a results
# file.
DEMO_COST_TEXT = "about a minute of human demonstration"
DEMO_COST_SOURCE = "benchmark/openemr/BENCHMARK.md + benchmark/BENCHMARK.md (prose)"


# ---------------------------------------------------------------------------
# Extraction (read the real results files; never re-derive a measured number)
# ---------------------------------------------------------------------------


@dataclass
class Arm:
    name: str          # "compiled" | "agent"
    n: int
    success_count: int
    success_rate: float
    p50_s: float
    p95_s: float
    cost_per_run: float
    cost_total: float
    model_calls_note: str  # e.g. "0 model calls" / "claude-sonnet-5"

    def to_json(self) -> dict:
        return {
            "n": self.n,
            "success_count": self.success_count,
            "success_rate": self.success_rate,
            "wall_s_p50": self.p50_s,
            "wall_s_p95": self.p95_s,
            "cost_usd_per_run": self.cost_per_run,
            "cost_usd_total": self.cost_total,
        }


@dataclass
class Benchmark:
    key: str
    label: str
    kind: str          # "field" | "reproducible"
    reproducible: bool
    source_file: str   # repo-relative path to the results.json
    task: str
    model: str
    workflow_steps: Optional[int]
    compiled: Arm
    agent: Arm
    extras: dict = field(default_factory=dict)

    def to_json(self) -> dict:
        return {
            "label": self.label,
            "kind": self.kind,
            "reproducible": self.reproducible,
            "source_file": self.source_file,
            "task": self.task,
            "model": self.model,
            "workflow_steps": self.workflow_steps,
            "arms": {"compiled": self.compiled.to_json(), "agent": self.agent.to_json()},
            "extras": self.extras,
        }


def _arm_from_json(name: str, blob: dict, model_calls_note: str) -> Arm:
    return Arm(
        name=name,
        n=int(blob["n"]),
        success_count=int(blob["success_count"]),
        success_rate=float(blob["success_rate"]),
        p50_s=float(blob["wall_s_p50"]),
        p95_s=float(blob["wall_s_p95"]),
        cost_per_run=float(blob["cost_usd_per_run"]),
        cost_total=float(blob["cost_usd_total"]),
        model_calls_note=model_calls_note,
    )


def load_openemr(path: Path = OPENEMR_RESULTS) -> Benchmark:
    d = json.loads(path.read_text())
    arms = d["arms"]
    model = str(d.get("model", "claude-sonnet-5"))
    compiled = _arm_from_json("compiled", arms["compiled"], "0 model calls")
    agent = _arm_from_json("agent", arms["agent"], model)
    # A single compiled run self-flagged expected-screen drift and aborted; the
    # arm-independent OCR check confirmed the write landed. Report it honestly.
    self_flag = None
    for run in d.get("runs", {}).get("compiled", []):
        if run.get("replayer_success") is False:
            self_flag = {
                "i": run.get("i"),
                "success": run.get("success"),
                "first_failure_step": (run.get("first_failure") or {}).get("step"),
            }
            break
    return Benchmark(
        key="openemr",
        label="OpenEMR public demo — a real third-party EMR",
        kind="field",
        reproducible=False,
        source_file="benchmark/openemr/results.json",
        task=str(d["task"]),
        model=model,
        workflow_steps=int(d["workflow_steps"]) if "workflow_steps" in d else None,
        compiled=compiled,
        agent=agent,
        extras={
            "target": d.get("target"),
            "cost_caps_usd": d.get("cost_caps_usd"),
            "pricing_note": (d.get("pricing_usd_per_mtok") or {}).get("note"),
            "compiled_self_flag": self_flag,
        },
    )


def load_mockmed(path: Path = MOCKMED_RESULTS) -> Benchmark:
    d = json.loads(path.read_text())
    arms = d["arms"]
    model = str(d.get("model", "claude-sonnet-5"))
    compiled = _arm_from_json("compiled", arms["compiled"], "0 model calls")
    agent = _arm_from_json("agent", arms["agent"], model)
    drift = d.get("drift_theme")
    drift_extra = None
    if drift:
        dc, da = drift.get("compiled", {}), drift.get("agent", {})
        drift_extra = {
            "compiled_wall_s": dc.get("wall_s"),
            "compiled_heal_count": dc.get("heal_count"),
            "compiled_success": dc.get("success"),
            "agent_wall_s": da.get("wall_s"),
            "agent_cost_usd": da.get("cost_usd"),
            "agent_actions": da.get("actions"),
            "agent_success": da.get("success"),
        }
    return Benchmark(
        key="mockmed",
        label="MockMed — the bundled demo clinic (CI-reproducible anchor)",
        kind="reproducible",
        reproducible=True,
        source_file="benchmark/results.json",
        task=str(d["task"]),
        model=model,
        workflow_steps=None,
        compiled=compiled,
        agent=agent,
        extras={
            "pricing_note": (d.get("pricing_usd_per_mtok") or {}).get("note"),
            "drift_theme": drift_extra,
        },
    )


# ---------------------------------------------------------------------------
# Formatting helpers (presentation only — round for display, never invent digits)
# ---------------------------------------------------------------------------


def _e(text: str) -> str:
    return html.escape(str(text), quote=True)


def fmt_s(v: float) -> str:
    return f"{v:.1f} s"


def fmt_usd(v: float, places: int = 4) -> str:
    if v == 0:
        return "$0"
    return f"${v:.{places}f}"


def fmt_usd_short(v: float) -> str:
    if v == 0:
        return "$0"
    if v >= 100:
        return f"${v:,.0f}"
    if v >= 1:
        return f"${v:,.2f}"
    return f"${v:.4f}"


def nice_axis(vmax: float, target_ticks: int = 4) -> tuple[float, float]:
    """A 'nice' axis maximum and tick step just above ``vmax``."""
    if vmax <= 0:
        return 1.0, 0.5
    raw = vmax / target_ticks
    mag = 10 ** math.floor(math.log10(raw))
    step = mag
    for m in (1, 2, 2.5, 5, 10):
        if m * mag >= raw:
            step = m * mag
            break
    axis_max = math.ceil(vmax / step) * step
    return axis_max, step


# ---------------------------------------------------------------------------
# Inline SVG horizontal bar chart (axis, gridlines, emphasized endpoints,
# tabular value labels). No external assets, styled via CSS custom properties.
# ---------------------------------------------------------------------------

_SVG_W = 680
_GUTTER = 150       # left label column
_RIGHT_PAD = 78     # room for the value label past the bar end
_BAR_H = 26
_BAR_GAP = 12
_GROUP_GAP = 10
_TOP = 14
_AXIS_H = 34        # space under the bars for the axis + ticks


@dataclass
class BarRow:
    label: str
    value: float
    text: str          # value label drawn at the bar end
    cls: str           # "c" (compiled) | "a" (agent)
    emphasize: bool = False
    group_break: bool = False  # extra gap above this row (separates arms)


def bar_chart_svg(
    title: str,
    unit: str,
    rows: list[BarRow],
    fmt_tick: Callable[[float], str],
    caption: str = "",
) -> str:
    """Render a horizontal bar chart as inline SVG.

    A zero-valued bar is drawn as an emphasized endpoint dot at the origin (the
    "$0, every run" moment) rather than an invisible zero-width rect.
    """
    plot_w = _SVG_W - _GUTTER - _RIGHT_PAD
    vmax = max((r.value for r in rows), default=0.0)
    axis_max, step = nice_axis(vmax)

    def x(v: float) -> float:
        return _GUTTER + (v / axis_max) * plot_w if axis_max else _GUTTER

    # Vertical layout.
    y = _TOP
    ys: list[float] = []
    for r in rows:
        if r.group_break:
            y += _GROUP_GAP
        ys.append(y)
        y += _BAR_H + _BAR_GAP
    plot_bottom = y - _BAR_GAP + 6
    height = plot_bottom + _AXIS_H

    parts: list[str] = []
    parts.append(
        f'<svg class="chart" viewBox="0 0 {_SVG_W} {height:.0f}" '
        f'role="img" width="100%" preserveAspectRatio="xMinYMin meet" '
        f'aria-label="{_e(title)} ({_e(unit)})">'
    )

    # Gridlines + tick labels.
    n_ticks = int(round(axis_max / step))
    for i in range(n_ticks + 1):
        tv = i * step
        tx = x(tv)
        parts.append(
            f'<line class="grid" x1="{tx:.1f}" y1="{_TOP - 6:.1f}" '
            f'x2="{tx:.1f}" y2="{plot_bottom:.1f}"/>'
        )
        parts.append(
            f'<text class="tick" x="{tx:.1f}" y="{plot_bottom + 16:.1f}" '
            f'text-anchor="middle">{_e(fmt_tick(tv))}</text>'
        )
    # Axis unit label.
    parts.append(
        f'<text class="axis-unit" x="{_GUTTER + plot_w:.1f}" '
        f'y="{plot_bottom + 30:.1f}" text-anchor="end">{_e(unit)}</text>'
    )
    # Baseline.
    parts.append(
        f'<line class="axis" x1="{_GUTTER:.1f}" y1="{plot_bottom:.1f}" '
        f'x2="{_GUTTER + plot_w:.1f}" y2="{plot_bottom:.1f}"/>'
    )

    for r, ry in zip(rows, ys):
        cy = ry + _BAR_H / 2
        # Row label.
        parts.append(
            f'<text class="row-label" x="{_GUTTER - 12:.1f}" y="{cy:.1f}" '
            f'text-anchor="end" dominant-baseline="central">{_e(r.label)}</text>'
        )
        bx = x(r.value)
        if r.value <= 0:
            # Emphasized zero endpoint: a ring at the origin + bold value.
            parts.append(
                f'<circle class="zero-dot bar-{r.cls}" cx="{_GUTTER:.1f}" '
                f'cy="{cy:.1f}" r="6"/>'
            )
            parts.append(
                f'<text class="val emph" x="{_GUTTER + 14:.1f}" y="{cy:.1f}" '
                f'dominant-baseline="central">{_e(r.text)}</text>'
            )
        else:
            emph = " emph" if r.emphasize else ""
            parts.append(
                f'<rect class="bar bar-{r.cls}{emph}" x="{_GUTTER:.1f}" '
                f'y="{ry:.1f}" width="{max(bx - _GUTTER, 1):.1f}" '
                f'height="{_BAR_H}" rx="3"/>'
            )
            parts.append(
                f'<text class="val{(" emph" if r.emphasize else "")}" '
                f'x="{bx + 8:.1f}" y="{cy:.1f}" '
                f'dominant-baseline="central">{_e(r.text)}</text>'
            )

    parts.append("</svg>")
    cap = f'<figcaption class="chart-cap">{_e(caption)}</figcaption>' if caption else ""
    return (
        f'<figure class="chart-fig">'
        f'<figcaption class="chart-title">{_e(title)}</figcaption>'
        f'{"".join(parts)}{cap}</figure>'
    )


# ---------------------------------------------------------------------------
# Page building blocks
# ---------------------------------------------------------------------------


def _cost_chart(b: Benchmark) -> str:
    rows = [
        BarRow("compiled replay", b.compiled.cost_per_run, fmt_usd(b.compiled.cost_per_run),
               "c", emphasize=True),
        BarRow("computer-use agent", b.agent.cost_per_run, fmt_usd(b.agent.cost_per_run),
               "a"),
    ]
    return bar_chart_svg(
        "Model cost per run",
        "USD / run (list price)",
        rows,
        fmt_tick=lambda v: fmt_usd_short(v),
        caption=(
            "In this measured sample, compiled replay made zero model calls and "
            "incurred $0 in model API charges."
        ),
    )


def _latency_chart(b: Benchmark) -> str:
    rows = [
        BarRow("compiled p50", b.compiled.p50_s, fmt_s(b.compiled.p50_s), "c", emphasize=True),
        BarRow("compiled p95", b.compiled.p95_s, fmt_s(b.compiled.p95_s), "c"),
        BarRow("agent p50", b.agent.p50_s, fmt_s(b.agent.p50_s), "a", group_break=True),
        BarRow("agent p95", b.agent.p95_s, fmt_s(b.agent.p95_s), "a"),
    ]
    return bar_chart_svg(
        "Latency (wall-clock)",
        "seconds",
        rows,
        fmt_tick=lambda v: f"{v:.0f}",
        caption="Per-run wall-clock around the replay / agent loop only.",
    )


def _speedup(b: Benchmark) -> float:
    return b.agent.p50_s / b.compiled.p50_s if b.compiled.p50_s else float("nan")


def _stat_tiles(b: Benchmark) -> str:
    speed = _speedup(b)
    tiles = [
        (
            "success — parity",
            f"{b.compiled.success_count}/{b.compiled.n} &middot; "
            f"{b.agent.success_count}/{b.agent.n}",
            "compiled &middot; agent — both pass the same arm-independent OCR check",
            "ok",
        ),
        (
            "model cost / run",
            f"{fmt_usd(b.compiled.cost_per_run)} <span class='vs'>vs</span> "
            f"{fmt_usd(b.agent.cost_per_run)}",
            f"agent arm total {fmt_usd(b.agent.cost_total, 2)} over {b.agent.n} runs",
            "cost",
        ),
        (
            "latency p50",
            f"{fmt_s(b.compiled.p50_s)} <span class='vs'>vs</span> {fmt_s(b.agent.p50_s)}",
            f"compiled is {speed:.1f}&times; faster at the median",
            "speed",
        ),
    ]
    cells = "".join(
        f'<div class="tile {cls}"><div class="tile-k">{k}</div>'
        f'<div class="tile-v">{v}</div><div class="tile-s">{s}</div></div>'
        for k, v, s, cls in tiles
    )
    return f'<div class="tiles">{cells}</div>'


def _projected_cost_panel(b: Benchmark) -> str:
    """Illustrative arithmetic projection, explicitly not a new measurement."""
    per = b.agent.cost_per_run
    rows = "".join(
        f"<tr><td class='n'>{n:,}</td><td class='c'>$0</td>"
        f"<td class='a'>{fmt_usd_short(per * n)}</td></tr>"
        for n in (1, 100, 1000, 10000)
    )
    note = b.extras.get("pricing_note") or "list price"
    return (
        '<div class="forever">'
        '<div class="forever-h">Illustrative repeat-run model cost '
        '<span class="derived">(measured model $/run &times; N; not new runs)</span>'
        '</div>'
        '<table class="forever-t"><thead><tr>'
        '<th>modeled runs</th><th>compiled model API</th><th>agent model API</th></tr></thead>'
        f'<tbody>{rows}</tbody></table>'
        f'<p class="forever-note">Agent values multiply the measured per-run '
        f'model charge ({fmt_usd(per)}) at {_e(note)}. The compiled column '
        f'projects the sample\'s zero model calls only. It excludes authoring, '
        f'maintenance, compute, storage, review, and exception-handling cost and '
        f'assumes the measured task and rates remain unchanged. The one-time '
        f'demonstration was {_e(DEMO_COST_TEXT)}.</p>'
        '</div>'
    )


def _self_flag_note(b: Benchmark) -> str:
    sf = b.extras.get("compiled_self_flag")
    if not sf:
        return ""
    return (
        '<p class="honest-inline">One compiled run (#{i}) self-flagged '
        'expected-screen drift at <code>{step}</code> and aborted — yet the '
        'arm-independent OCR check confirmed the note saved, so it counts as a '
        'success. On a shared instance the message list grows under every '
        "visitor, so a postcondition can drift <em>after</em> the write lands; "
        'the self-flag halting instead of improvising is the point.</p>'
    ).format(i=_e(sf.get("i")), step=_e(sf.get("first_failure_step")))


def _drift_note(b: Benchmark) -> str:
    d = b.extras.get("drift_theme")
    if not d:
        return ""
    return (
        '<p class="honest-inline">Under a hostile theme swap '
        '(<code>?drift=theme</code>, which invalidates every recorded template '
        'crop), compiled still succeeded in '
        f'{fmt_s(float(d["compiled_wall_s"]))} with {d["compiled_heal_count"]} '
        'self-heals; the agent succeeded in '
        f'{fmt_s(float(d["agent_wall_s"]))} at {fmt_usd(float(d["agent_cost_usd"]), 4)}. '
        'This drift row is <strong>n=1 per arm</strong> — an existence result, '
        'not a rate.</p>'
    )


def _bench_section(b: Benchmark, *, lead: bool) -> str:
    badge = (
        '<span class="tag field">field result — real third-party app</span>'
        if b.kind == "field"
        else '<span class="tag repro">CI-reproducible anchor</span>'
    )
    lead_tag = '<span class="tag lead">LEAD</span>' if lead else ""
    steps = (
        f' &middot; {b.workflow_steps} compiled steps' if b.workflow_steps else ""
    )
    repro_line = (
        "Not CI-reproducible: a single shared public instance that every "
        "internet visitor mutates and that resets daily. Treat as a field result."
        if not b.reproducible
        else "Anyone can rerun this deterministically — same orchestrator, same "
        "agent harness, same style of OCR success check, on a local app."
    )
    return f"""
    <section class="bench {'lead' if lead else ''}">
      <header class="bench-head">
        <div class="bench-titles">
          <div class="bench-tags">{lead_tag}{badge}</div>
          <h2>{_e(b.label)}</h2>
          <p class="bench-task">{_e(b.task)}{steps}</p>
        </div>
      </header>
      {_stat_tiles(b)}
      <div class="charts">
        <div class="chart-cell">{_cost_chart(b)}</div>
        <div class="chart-cell">{_latency_chart(b)}</div>
      </div>
      {_projected_cost_panel(b)}
      <p class="bench-method">Both arms drive the same vision-only interface
        (screenshots in; pixel clicks / typed text / scrolls out) against the
        same target; the agent is <code>{_e(b.model)}</code> with the
        computer-use tool, prompted with user intent, not steps. Success is one
        arm-independent OCR check applied to both arms — neither arm's
        self-report is trusted. {_e(repro_line)}
        <span class="src">figures: <code>{_e(b.source_file)}</code></span></p>
      {_self_flag_note(b)}
      {_drift_note(b)}
    </section>
    """


# The caveats mirror the safety gallery's "What still slips" tone: disclose,
# don't sell. Each item is pulled from the two BENCHMARK.md caveat sections.
def _caveats(oe: Benchmark, mm: Benchmark) -> list[tuple[str, str]]:
    caps = oe.extras.get("cost_caps_usd") or {}
    per_cap = caps.get("per_run")
    tot_cap = caps.get("total")
    price_note = oe.extras.get("pricing_note") or "an introductory rate applies"
    return [
        (
            "Small N, wide error bars.",
            f"The agent arm is N={oe.agent.n} on OpenEMR and N={mm.agent.n} on "
            "MockMed, because agent runs cost real money, real minutes, and real "
            "load on a shared public service. A 100% success rate over ten runs "
            "is not a five-nines claim — its confidence interval is wide. The "
            f"compiled arm (N={oe.compiled.n} / {mm.compiled.n}) is cheap to "
            "repeat, so its bars are tighter, but the honest comparison is "
            "still small-sample on the agent side.",
        ),
        (
            "The lead result is a field result, not CI-reproducible.",
            "OpenEMR is a single shared public demo that anyone on the internet "
            "can mutate and that resets daily; every successful run also appends "
            "a message that grows the dashboard for the next run. Numbers depend "
            "on that instance's state and load on the day. The MockMed row is the "
            "reproducible anchor — treat OpenEMR as a real-world sighting, not a "
            "repeatable measurement.",
        ),
        (
            "Cost is list price with hard caps — billed cost is lower.",
            f"Costs are computed from API token counts at list pricing. {_e(price_note)}, "
            "so the amount actually billed today is about a third lower than the "
            "figures shown. Every agent run is capped at "
            f"{fmt_usd(per_cap, 2) if per_cap is not None else 'a per-run ceiling'} "
            "and the whole agent arm at "
            f"{fmt_usd(tot_cap, 2) if tot_cap is not None else 'a total ceiling'} "
            "(list price); the caps stop the arm and the truncation is disclosed "
            "in the source data rather than hidden.",
        ),
        (
            "Success is one OCR check on both arms — and it errs conservative.",
            "A run passes only if the run's own note text is read back out of the "
            "final screenshot by OCR, identically for compiled and agent. On dense "
            "EMR text the OCR sometimes drops the exact line it is looking for, so "
            "a 'failed' verification can be a measurement miss with the note "
            "plainly on screen. The check is identical for both arms, so it cannot "
            "favour one — but it under-counts rather than over-counts success.",
        ),
        (
            "This measures cost and latency at PARITY success — not capability.",
            "Both arms passed every task here, so the story is purely how much "
            "cheaper and faster the compiled path is at the same outcome. It is "
            "NOT a claim that compiled replay is more capable, more robust to novel "
            "situations, or a substitute for an agent on a task it has never seen. "
            "MockMed is also a deliberately simple app; harder surfaces would slow "
            "and likely degrade both arms, plausibly at different rates.",
        ),
    ]


def render_html(oe: Benchmark, mm: Benchmark) -> str:
    speed_oe = _speedup(oe)
    headline = (
        f"Same task, same success ({oe.compiled.success_count}/{oe.compiled.n} "
        f"vs {oe.agent.success_count}/{oe.agent.n}) &mdash; compiled replay costs "
        f"{fmt_usd(oe.compiled.cost_per_run)} and runs {speed_oe:.1f}&times; faster "
        f"than the {oe.model} agent"
    )
    caveats = "\n".join(
        f'<li><strong>{_e(t)}</strong><p>{b}</p></li>' for t, b in _caveats(oe, mm)
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Compiled Replay vs. Computer-Use Agent &mdash; openadapt-flow</title>
<style>
{_CSS}
</style>
</head>
<body>
<main>
  <header class="page-head">
    <h1>Compiled Replay vs. Computer-Use Agent</h1>
    <p class="sub">One clinical task, two ways to automate it, one success check.
      The wedge in one line: for a task you have already demonstrated, a compiled
      replay is <strong>model-free</strong>, <strong>~$0 per run</strong>, and
      <strong>faster</strong> &mdash; at <strong>parity</strong> success. Generated
      straight from the repo's real benchmark <code>results.json</code> files; no
      number here is hand-typed.</p>
    <div class="headline ok">{headline}</div>
    <div class="isnt">
      <p><strong>What this is.</strong> The economics of the 500<sup>th</sup> run of
        a <em>known</em> task: once a workflow has been demonstrated once, replaying
        it should not cost a model call.</p>
      <p><strong>What this isn't.</strong> A capability claim. Compiled replay is the
        wrong tool for a task nobody has automated yet &mdash; exploring an unfamiliar
        screen is exactly the agent's job. The agent explores; the compiled bundle
        exploits a demonstration.</p>
    </div>
    <p class="method">Both arms drive the same vision-only interface against the same
      app; the only difference is whether a model is in the per-action loop.
      Reproduce with <code>python -m benchmark.comparison_artifact.generate</code>.</p>
  </header>

  {_bench_section(oe, lead=True)}
  {_bench_section(mm, lead=False)}

  <section class="limits">
    <h2>Read before quoting these numbers</h2>
    <p>This comparison would be dishonest without the caveats it does <em>not</em>
      wave away. Pulled straight from the two <code>BENCHMARK.md</code> methodology
      sections:</p>
    <ul>
      {caveats}
    </ul>
    <p class="foot">Bottom line: at parity success on these tasks, the compiled path
      removes the model from the loop &mdash; {fmt_usd(oe.compiled.cost_per_run)} and
      {speed_oe:.1f}&times; faster on the real EMR, and a tighter, reproducible
      version of the same gap on MockMed. That is a cost/latency result on known
      tasks, disclosed with its limits &mdash; not a general capability claim.</p>
  </section>

  <footer class="page-foot">
    Generated by <code>benchmark.comparison_artifact.generate</code> from
    <code>benchmark/openemr/results.json</code> and
    <code>benchmark/results.json</code>. Zero model calls, zero network,
    deterministic. Model pinned: <code>{_e(oe.model)}</code> on 2026-07-08.
  </footer>
</main>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# CSS — shares the safety gallery's token system (custom-property palette,
# dual light/dark theme, card / mono / honest-limits patterns), extended with
# chart tokens so the two pages read as siblings.
# ---------------------------------------------------------------------------

_CSS = """
:root{
  --bg:#f6f7f9; --fg:#161a1f; --muted:#5b6570; --card:#ffffff; --line:#e2e6ea;
  --safe:#137a3f; --safe-bg:#e7f5ec; --unsafe:#b3261e; --unsafe-bg:#fdeceb;
  --halt:#8a5a00; --halt-bg:#fbf3e2; --verify:#137a3f; --verify-bg:#e7f5ec;
  --code:#0b3d2e; --code-bg:#eef4f1; --accent:#1f3a5f;
  --compiled:#137a3f; --compiled-soft:#e7f5ec;
  --agent:#1f3a5f; --agent-soft:#e8eef6;
  --grid:#e9edf1;
}
@media (prefers-color-scheme: dark){
  :root{
    --bg:#0f1720; --fg:#e6edf3; --muted:#9fb0c0; --card:#161d26; --line:#263039;
    --safe:#4ade80; --safe-bg:#12321f; --unsafe:#ff6b61; --unsafe-bg:#3a1512;
    --halt:#e7b84b; --halt-bg:#332a12; --verify:#4ade80; --verify-bg:#12321f;
    --code:#a5f3d0; --code-bg:#10231b; --accent:#8fb2e0;
    --compiled:#4ade80; --compiled-soft:#12321f;
    --agent:#8fb2e0; --agent-soft:#182432;
    --grid:#222c36;
  }
}
:root[data-theme="dark"]{
  --bg:#0f1720; --fg:#e6edf3; --muted:#9fb0c0; --card:#161d26; --line:#263039;
  --safe:#4ade80; --safe-bg:#12321f; --unsafe:#ff6b61; --unsafe-bg:#3a1512;
  --halt:#e7b84b; --halt-bg:#332a12; --verify:#4ade80; --verify-bg:#12321f;
  --code:#a5f3d0; --code-bg:#10231b; --accent:#8fb2e0;
  --compiled:#4ade80; --compiled-soft:#12321f;
  --agent:#8fb2e0; --agent-soft:#182432;
  --grid:#222c36;
}
:root[data-theme="light"]{
  --bg:#f6f7f9; --fg:#161a1f; --muted:#5b6570; --card:#ffffff; --line:#e2e6ea;
  --safe:#137a3f; --safe-bg:#e7f5ec; --unsafe:#b3261e; --unsafe-bg:#fdeceb;
  --halt:#8a5a00; --halt-bg:#fbf3e2; --verify:#137a3f; --verify-bg:#e7f5ec;
  --code:#0b3d2e; --code-bg:#eef4f1; --accent:#1f3a5f;
  --compiled:#137a3f; --compiled-soft:#e7f5ec;
  --agent:#1f3a5f; --agent-soft:#e8eef6;
  --grid:#e9edf1;
}
*{box-sizing:border-box;}
body{margin:0;background:var(--bg);color:var(--fg);
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  line-height:1.5;}
main{max-width:1040px;margin:0 auto;padding:32px 20px 64px;}
.page-head h1{font-size:30px;margin:0 0 6px;letter-spacing:-0.02em;}
.sub{color:var(--muted);margin:0 0 16px;max-width:74ch;}
.sub strong{color:var(--fg);}
.method{color:var(--muted);font-size:14px;max-width:80ch;}
code{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  background:var(--code-bg);color:var(--code);padding:1px 5px;border-radius:4px;
  font-size:0.9em;}
.headline{display:block;font-weight:700;font-size:18px;padding:12px 16px;
  border-radius:8px;margin:6px 0 16px;}
.headline.ok{background:var(--safe-bg);color:var(--safe);border:1px solid var(--safe);}
.isnt{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin:6px 0 16px;}
@media (max-width:680px){.isnt{grid-template-columns:1fr;}}
.isnt p{margin:0;background:var(--card);border:1px solid var(--line);
  border-left:4px solid var(--accent);border-radius:8px;padding:11px 14px;
  font-size:14px;color:var(--muted);}
.isnt strong{color:var(--fg);}

.bench{background:var(--card);border:1px solid var(--line);border-radius:12px;
  padding:20px 20px 18px;margin:22px 0;box-shadow:0 1px 2px rgba(0,0,0,0.04);}
.bench.lead{border-left:5px solid var(--compiled);}
.bench-head{margin-bottom:6px;}
.bench-tags{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:6px;}
.tag{display:inline-block;font-size:11px;font-weight:800;letter-spacing:0.05em;
  text-transform:uppercase;padding:3px 9px;border-radius:999px;}
.tag.lead{background:var(--compiled);color:var(--bg);}
.tag.field{background:var(--halt-bg);color:var(--halt);border:1px solid var(--halt);}
.tag.repro{background:var(--agent-soft);color:var(--accent);border:1px solid var(--accent);}
.bench h2{margin:2px 0 4px;font-size:21px;letter-spacing:-0.01em;}
.bench-task{color:var(--muted);font-size:14px;margin:0 0 6px;max-width:86ch;}

.tiles{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin:14px 0 18px;}
@media (max-width:680px){.tiles{grid-template-columns:1fr;}}
.tile{border:1px solid var(--line);border-radius:10px;padding:12px 14px;
  background:var(--bg);}
.tile-k{font-size:11px;text-transform:uppercase;letter-spacing:0.05em;
  color:var(--muted);font-weight:700;}
.tile-v{font-size:22px;font-weight:800;margin:4px 0 3px;letter-spacing:-0.01em;
  font-variant-numeric:tabular-nums;}
.tile-v .vs{font-size:13px;font-weight:600;color:var(--muted);
  text-transform:lowercase;letter-spacing:0;padding:0 3px;}
.tile-s{font-size:12px;color:var(--muted);}
.tile.ok{border-left:4px solid var(--safe);}
.tile.cost{border-left:4px solid var(--compiled);}
.tile.speed{border-left:4px solid var(--accent);}

.charts{display:grid;grid-template-columns:1fr 1fr;gap:18px;margin:8px 0 4px;}
@media (max-width:760px){.charts{grid-template-columns:1fr;}}
.chart-cell{min-width:0;border:1px solid var(--line);border-radius:10px;
  padding:12px 14px 10px;background:var(--bg);}
.chart-fig{margin:0;}
.chart-title{font-size:13px;font-weight:700;color:var(--fg);margin-bottom:4px;}
.chart-cap{font-size:12px;color:var(--muted);margin-top:6px;}
svg.chart{display:block;overflow:visible;}
svg.chart .grid{stroke:var(--grid);stroke-width:1;}
svg.chart .axis{stroke:var(--muted);stroke-width:1.2;}
svg.chart .tick{fill:var(--muted);font-size:11px;
  font-family:ui-monospace,Menlo,monospace;font-variant-numeric:tabular-nums;}
svg.chart .axis-unit{fill:var(--muted);font-size:10.5px;text-transform:uppercase;
  letter-spacing:0.04em;}
svg.chart .row-label{fill:var(--fg);font-size:12.5px;}
svg.chart .val{fill:var(--fg);font-size:12.5px;font-weight:600;
  font-family:ui-monospace,Menlo,monospace;font-variant-numeric:tabular-nums;}
svg.chart .val.emph{font-weight:800;}
svg.chart .bar{opacity:0.92;}
svg.chart .bar.emph{opacity:1;}
svg.chart .bar-c{fill:var(--compiled);}
svg.chart .bar-a{fill:var(--agent);}
svg.chart circle.zero-dot{stroke:var(--bg);stroke-width:2;}

.forever{margin:12px 0 6px;border:1px dashed var(--line);border-radius:10px;
  padding:12px 14px;background:var(--compiled-soft);}
.forever-h{font-size:14px;font-weight:800;color:var(--fg);}
.forever-h .derived{font-size:11.5px;font-weight:600;color:var(--muted);}
.forever-t{border-collapse:collapse;margin:8px 0 4px;font-size:14px;}
.forever-t th{text-align:left;font-size:11px;text-transform:uppercase;
  letter-spacing:0.04em;color:var(--muted);padding:3px 22px 4px 0;font-weight:700;}
.forever-t td{padding:3px 22px 3px 0;font-variant-numeric:tabular-nums;
  font-family:ui-monospace,Menlo,monospace;}
.forever-t td.c{color:var(--compiled);font-weight:800;}
.forever-t td.a{color:var(--accent);font-weight:700;}
.forever-note{font-size:12.5px;color:var(--muted);margin:6px 0 0;max-width:88ch;}

.bench-method{font-size:13px;color:var(--muted);margin:14px 0 0;max-width:92ch;}
.bench-method .src{display:block;margin-top:4px;}
.honest-inline{font-size:13px;color:var(--muted);margin:10px 0 0;max-width:92ch;
  border-left:3px solid var(--halt);padding-left:12px;}
.honest-inline strong,.honest-inline em{color:var(--fg);}

.limits{margin-top:40px;border-top:2px solid var(--line);padding-top:22px;}
.limits h2{font-size:22px;margin:0 0 8px;}
.limits > p{color:var(--muted);max-width:82ch;}
.limits ul{list-style:none;padding:0;margin:16px 0;display:flex;
  flex-direction:column;gap:14px;}
.limits li{background:var(--card);border:1px solid var(--line);
  border-left:4px solid var(--halt);border-radius:8px;padding:12px 16px;}
.limits li strong{display:block;font-size:15px;margin-bottom:4px;}
.limits li p{margin:0;color:var(--muted);font-size:14px;max-width:90ch;}
.foot{font-size:13px;color:var(--muted);margin-top:18px;max-width:90ch;}
.page-foot{margin-top:34px;padding-top:14px;border-top:1px solid var(--line);
  font-size:12px;color:var(--muted);}
"""


# ---------------------------------------------------------------------------
# Build + CLI
# ---------------------------------------------------------------------------


def build(outdir: Path = HERE) -> dict:
    """Read the real results files, write ``comparison.html`` + ``comparison.json``.

    Returns the extracted-figures payload (also used by the test as a machine
    check that the emitted HTML carries the real numbers)."""
    outdir.mkdir(parents=True, exist_ok=True)
    oe = load_openemr()
    mm = load_mockmed()

    payload = {
        "generated_by": "benchmark.comparison_artifact.generate",
        "model_calls_compiled": 0,
        "network_calls": 0,
        "provenance": (
            "Every figure is read from the source results.json files; the only "
            "prose-sourced figure is the one-time human demonstration cost, "
            f"quoted from {DEMO_COST_SOURCE}."
        ),
        "source_files": {
            "openemr": "benchmark/openemr/results.json",
            "mockmed": "benchmark/results.json",
        },
        "demonstration_cost": {
            "text": DEMO_COST_TEXT,
            "source": DEMO_COST_SOURCE,
        },
        "benchmarks": {"openemr": oe.to_json(), "mockmed": mm.to_json()},
        "derived": {
            "openemr_speedup_p50": round(_speedup(oe), 2),
            "mockmed_speedup_p50": round(_speedup(mm), 2),
        },
    }
    (outdir / "comparison.json").write_text(json.dumps(payload, indent=2) + "\n")
    (outdir / "comparison.html").write_text(render_html(oe, mm))
    return payload


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(HERE),
                        help="output directory (default: this module's directory)")
    args = parser.parse_args(argv)

    payload = build(Path(args.out))
    oe = payload["benchmarks"]["openemr"]["arms"]
    print(
        "comparison artifact: OpenEMR compiled "
        f"{oe['compiled']['success_count']}/{oe['compiled']['n']} @ "
        f"${oe['compiled']['cost_usd_per_run']:.0f}/run vs agent "
        f"{oe['agent']['success_count']}/{oe['agent']['n']} @ "
        f"${oe['agent']['cost_usd_per_run']:.4f}/run"
    )
    print(f"  wrote comparison.html + comparison.json to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
