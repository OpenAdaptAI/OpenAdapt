"""Render human-readable Markdown reports from run and bench artifacts.

Two entry points:

- :func:`render_run_report` — reads ``<run_dir>/report.json`` (an
  :class:`openadapt_flow.ir.RunReport`) and writes ``<run_dir>/REPORT.md``
  with the outcome headline, per-step table, a per-step evidence section
  (a before/after frame for EVERY step alongside the resolution rung,
  identity-gate outcome, effect-check verdict, and heal/halt status, on
  relative paths so the report renders on GitHub), rung histogram, and
  totals. The generator links only retained image artifacts inside the run
  directory and never synthesizes pixels: a frame the run did not retain on
  disk is shown as absent.
- :func:`render_bench_report` — reads a ``bench.json`` produced by
  :func:`openadapt_flow.bench.run_bench` and writes a Markdown summary.
"""

from __future__ import annotations

import json
import warnings
from pathlib import Path
from urllib.parse import quote

from openadapt_flow.ir import RunReport, StepResult
from openadapt_flow.privacy import scrub_mode as _scrub_mode
from openadapt_flow.privacy import scrub_text as _scrub_phi
from openadapt_flow.privacy import text_scrubbing_enabled as _text_scrubbing_enabled


class PlaintextPHIWarning(UserWarning):
    """REPORT.md is being written with identity-like free text and no scrubber."""


def _report_has_identity_like_text(report: RunReport) -> bool:
    """True if the report carries free text that typically embeds PHI.

    Params values and step intents are the identifier-bearing free-text fields
    rendered into REPORT.md (patient name / DOB / MRN flow through here).
    """
    if any((v or "").strip() for v in report.params.values()):
        return True
    return any((r.intent or "").strip() for r in report.results)


def _warn_if_plaintext_phi(report: RunReport) -> None:
    """Warn (once) when REPORT.md will contain plaintext identity-like text.

    Fires only when scrubbing is *not* active (default ``auto`` with the
    ``privacy`` extra absent) and the report has identity-like free text.
    ``OPENADAPT_FLOW_SCRUB=off`` is a deliberate opt-out and stays silent;
    ``on`` fails closed upstream before reaching here. Not a behavior change —
    the report is still written; this only makes the plaintext write visible.
    ``warnings`` dedups per call site, so it is effectively one-time per process.
    """
    if _scrub_mode() == "off" or _text_scrubbing_enabled():
        return
    if not _report_has_identity_like_text(report):
        return
    warnings.warn(
        "Writing REPORT.md with PLAINTEXT identity-like text: PHI scrubbing is "
        "not active (OPENADAPT_FLOW_SCRUB=auto and the 'privacy' extra is not "
        "installed). This shareable report may contain patient name/DOB/MRN. "
        "Install it (pip install 'openadapt-flow[privacy]' && python -m spacy "
        "download en_core_web_sm) and set OPENADAPT_FLOW_SCRUB=on to scrub and "
        "fail closed.",
        PlaintextPHIWarning,
        stacklevel=2,
    )


# Ladder order for the rung histogram (cheapest first).
_RUNG_ORDER = ("template", "template_global", "ocr", "geometry", "grounder")


def _md_escape(text: str) -> str:
    """Escape characters that would break a Markdown table cell."""
    return text.replace("|", "\\|").replace("\n", " ")


def _md_phi(text: str) -> str:
    """Scrub PII/PHI (per the run's scrub posture) then Markdown-escape.

    Used for every FREE-TEXT field rendered into the shareable ``REPORT.md``
    (workflow name, param values, step intents, errors, unarmed reasons) —
    these carry patient identifiers (name / DOB / MRN) recorded from the app.
    A no-op when scrubbing is off/unavailable (see openadapt_flow.privacy).
    """
    return _md_escape(_scrub_phi(text) or "")


def _retained_image_target(run: Path, rel_path: str) -> str | None:
    """Return a safe Markdown target for a retained run image.

    Reports are shareable artifacts, so an untrusted path from ``report.json``
    must never make them link outside the run directory.  Require a relative,
    regular file, reject parent traversal and symlink components, and URL-quote
    the target for Markdown.
    """
    relative = Path(rel_path)
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        return None

    try:
        root = run.resolve(strict=True)
        candidate = root
        for part in relative.parts:
            candidate /= part
            if candidate.is_symlink():
                return None
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
    except (FileNotFoundError, OSError, ValueError):
        return None

    if not resolved.is_file():
        return None
    return quote(relative.as_posix(), safe="/._-~")


def _img(run: Path, rel_path: str | None, alt: str) -> str:
    """Markdown image for a run-dir-relative path.

    Emits the image only when the frame is actually present on disk under
    ``run``; an honest artifact never links a frame the run did not retain
    (some run bundles keep only a final frame). A ``None`` path renders as an
    em dash; a path whose file is missing renders as an italic "not retained"
    note instead of a broken image link.
    """
    if not rel_path:
        return "&mdash;"
    target = _retained_image_target(run, rel_path)
    if target is None:
        return "_frame not retained_"
    escaped_alt = _md_escape(alt).replace("[", "\\[").replace("]", "\\]")
    return f"![{escaped_alt}]({target})"


def _before_after_table(run: Path, result: StepResult) -> list[str]:
    """Before/after screenshots side by side as a two-column table."""
    return [
        "| Before | After |",
        "| --- | --- |",
        f"| {_img(run, result.before_png, f'{result.step_id} before')} "
        f"| {_img(run, result.after_png, f'{result.step_id} after')} |",
    ]


def _verified_parts(result: StepResult) -> list[str]:
    """Governance markers for a step: identity-gate, typed-input, and effect
    verdicts, in that order. Shared by the per-step table's ``Verified`` column
    and the per-step evidence section so both read identically.

    An ``id ⚠`` marker (abstain / unreadable identity) means the step proceeded
    on positional evidence alone and is flagged, never silent.
    """
    parts: list[str] = []
    if result.identity is not None:
        parts.append(
            {
                "verified": "id ✓",
                "mismatch": "id ✗",
                "abstain": "id ⚠",
                "unreadable": "id ⚠",
            }[result.identity.status]
        )
    if result.input_verified is not None:
        marker = "input ✓" if result.input_verified else "input ✗"
        if result.input_retried:
            marker += " (retried)"
        parts.append(marker)
    if result.effect_verified is True:
        parts.append("effect ✓")
    elif result.effect_verified is False:
        parts.append("effect ✗")
    elif result.effect_approved_unverified:
        parts.append("effect ⚠ approved")
    return parts


def _step_evidence_line(result: StepResult) -> str:
    """One compact, scannable governance line for a step's evidence block:
    resolution rung + confidence + resolved point, the identity/input/effect
    gate verdicts, heal status, and the pass/halt/skip outcome.
    """
    res = result.resolution
    if res is not None:
        rung_part = (
            f"**Rung** `{res.rung}` "
            f"(conf {res.confidence:.2f}, resolved ({res.point[0]}, {res.point[1]}))"
        )
    else:
        rung_part = "**Rung** &mdash; (keyboard / wait step, no anchor)"
    gates = _verified_parts(result)
    gate_part = (
        f"**Gates** {', '.join(gates)}" if gates else "**Gates** none on this step"
    )
    heal_part = (
        f"**Heal** healed via `{result.heal.rung_used}`"
        if result.heal
        else "**Heal** none"
    )
    if result.skipped:
        outcome_part = "**Outcome** ⏭️ skipped (guard unmet)"
    elif result.ok:
        outcome_part = "**Outcome** ✅ ok"
    elif result.safety_halt:
        outcome_part = "**Outcome** ❌ HALTED (governed refusal)"
    else:
        outcome_part = "**Outcome** ❌ halted"
    return " · ".join([rung_part, gate_part, heal_part, outcome_part])


def render_run_report(run_dir: Path | str) -> Path:
    """Render ``REPORT.md`` inside ``run_dir`` from its ``report.json``.

    Args:
        run_dir: Run directory containing ``report.json`` (and the
            ``steps/`` / ``heals/`` image folders it references).

    Returns:
        Path to the written ``REPORT.md``.

    Raises:
        FileNotFoundError: If ``run_dir/report.json`` does not exist.
    """
    run = Path(run_dir)
    report = RunReport.model_validate_json(
        (run / "report.json").read_text(encoding="utf-8")
    )
    _warn_if_plaintext_phi(report)

    ok_count = sum(1 for r in report.results if r.ok)
    icon = "✅" if report.success else "❌"
    outcome = "success" if report.success else "FAILED"

    lines: list[str] = []
    lines.append(f"# {icon} {_md_phi(report.workflow_name)} — {outcome}")
    lines.append("")
    lines.append(f"- **Started:** {report.started_at}")
    lines.append(f"- **Steps:** {ok_count}/{len(report.results)} ok")
    lines.append(f"- **Heals:** {report.heal_count}")
    # Egress transparency (PHI audit REM-3): make it unmistakable whether a
    # screenshot could have left the box on this run.
    if report.screenshots_may_leave_box:
        lines.append(
            "- **Data egress:** ⚠️ a model-grounding component was wired — "
            "screenshots COULD have left the box this run"
        )
    else:
        lines.append(
            "- **Data egress:** none — fully local replay (zero screenshots "
            "left the box)"
        )
    if report.governed_authorization_id:
        lines.append(
            "- **Governed authorization:** `"
            f"{_md_escape(report.governed_authorization_id)}` "
            f"({_md_escape(report.governed_approval_source or 'unspecified')})"
        )
        lines.append(
            "- **Admitted policy:** "
            f"{_md_escape(report.governed_policy_name or 'unspecified')}; "
            "runtime inputs bound to `"
            f"{_md_escape(report.governed_runtime_inputs_digest or 'unspecified')}`"
        )
        if report.approved_unverified_effect_step_ids:
            steps = ", ".join(
                f"`{_md_escape(step_id)}`"
                for step_id in report.approved_unverified_effect_step_ids
            )
            lines.append(
                "- **Approved without independent effect verification:** "
                f"{steps}; screen postconditions still applied"
            )
    lines.append("")

    # -- Parameters -----------------------------------------------------
    lines.append("## Parameters")
    lines.append("")
    if report.params:
        lines.append("| Param | Value |")
        lines.append("| --- | --- |")
        for key, value in report.params.items():
            lines.append(f"| `{_md_escape(key)}` | {_md_phi(value)} |")
    else:
        lines.append("_No parameters._")
    lines.append("")

    # -- Identity-protection coverage ------------------------------------
    # Stated on every report: identity verification covers ONLY armed
    # steps; an unarmed click proceeds with no identity check at all
    # (docs/LIMITS.md). Computed over the whole bundle at run start, so
    # the numbers cover steps the run never reached.
    lines.append("## Identity protection coverage")
    lines.append("")
    if report.identity_applicable_steps:
        lines.append(
            f"**{report.identity_armed_steps} of "
            f"{report.identity_applicable_steps} click steps "
            "identity-armed.** Unarmed clicks proceed with **no identity "
            "verification** (see docs/LIMITS.md)."
        )
        if report.identity_unarmed:
            lines.append("")
            lines.append("| Unarmed step | Intent | Reason |")
            lines.append("| --- | --- | --- |")
            for unarmed in report.identity_unarmed:
                lines.append(
                    f"| `{_md_escape(unarmed.step_id)}` "
                    f"| {_md_phi(unarmed.intent)} "
                    f"| {_md_phi(unarmed.reason)} |"
                )
    else:
        lines.append(
            "_No identity-applicable (anchored click/type) steps in this workflow._"
        )
    lines.append("")

    # -- Effect-verification coverage -------------------------------------
    # Stated on every report (kit): which EXECUTED steps carried a
    # system-of-record effect contract and how each fared. Steps with no
    # contract fall back to screen evidence for their writes — the exact gap
    # `openadapt-flow lint` / `certify --policy` measure over the whole
    # bundle (per-consequential-step effect coverage %).
    lines.append("## Effect verification (system of record)")
    lines.append("")
    executed = [r for r in report.results if not r.skipped]
    with_contracts = [r for r in executed if r.effect_contract_hashes]
    if with_contracts:
        confirmed = sum(1 for r in with_contracts if r.effect_verified is True)
        halted = sum(1 for r in with_contracts if r.effect_verified is False)
        approved = sum(1 for r in with_contracts if r.effect_approved_unverified)
        lines.append(
            f"**{len(with_contracts)} of {len(executed)} executed step(s) "
            "carried a system-of-record effect contract** — "
            f"{confirmed} confirmed, {halted} halted, "
            f"{approved} approved-unverified. Steps without a contract fall "
            "back to screen evidence for their writes (run "
            "`openadapt-flow lint` for the bundle's per-consequential-step "
            "effect coverage)."
        )
    else:
        lines.append(
            "_No executed step carried a system-of-record effect contract — "
            "every write on this run was verified from screen evidence only. "
            "Run `openadapt-flow lint` to see the bundle's consequential-step "
            "effect coverage._"
        )
    lines.append("")

    # -- Per-step table ---------------------------------------------------
    lines.append("## Steps")
    lines.append("")
    lines.append(
        "| # | Step | Intent | Rung | Confidence | Verified | ms | Healed | OK |"
    )
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for i, result in enumerate(report.results, start=1):
        rung = result.resolution.rung if result.resolution else "&mdash;"
        conf = f"{result.resolution.confidence:.2f}" if result.resolution else "&mdash;"
        healed = "\U0001fa79" if result.heal else ""
        status = "✅" if result.ok else "❌"
        # Identity (clicks) / typed-input (TYPE) / effect verification outcome:
        # "unreadable" identity means the step proceeded on positional
        # evidence alone and is flagged, never silent.
        verified = ", ".join(_verified_parts(result)) or "&mdash;"
        lines.append(
            f"| {i} | `{_md_escape(result.step_id)}` "
            f"| {_md_phi(result.intent)} | {rung} | {conf} "
            f"| {verified} | {result.elapsed_ms:.0f} | {healed} | {status} |"
        )
    lines.append("")

    # -- Per-step evidence: a before/after frame for EVERY step -------------
    # One block per step (not just heals/failures/the final step), so the whole
    # governed run is legible: the before/after frames alongside the resolution
    # rung, identity-gate and effect-check verdicts, and heal/halt status.
    # Link only retained run artifacts; a step whose frame the run did not
    # retain shows the frame as absent (see _img), never a fabricated one.
    final_id = report.results[-1].step_id if report.results else None
    if report.results:
        lines.append("## Per-step evidence")
        lines.append("")
        lines.append(
            "Every step below shows the frame **before** and **after** the "
            "action next to the resolution rung, the identity-gate and "
            "effect-check verdicts, and whether the step healed or halted. "
            "The generator links only retained run artifacts and never "
            "synthesizes pixels. If image redaction was enabled when a frame "
            "was persisted, that redaction is already burned into its pixels; "
            "a frame the run did not retain is marked _not retained_."
        )
        lines.append("")
        for i, result in enumerate(report.results, start=1):
            tags = []
            if result.step_id == final_id:
                tags.append("final step")
            if result.heal:
                tags.append("healed")
            if not result.ok and not result.skipped:
                tags.append("halted")
            tag_suffix = f" ({', '.join(tags)})" if tags else ""
            lines.append(
                f"### {i}. `{_md_escape(result.step_id)}` — "
                f"{_md_phi(result.intent)}{tag_suffix}"
            )
            lines.append("")
            if result.error:
                lines.append(f"> ❌ **Error:** {_md_phi(result.error)}")
                lines.append("")
            lines.append(_step_evidence_line(result))
            lines.append("")
            lines.extend(_before_after_table(run, result))
            lines.append("")
            if result.heal:
                heal = result.heal
                applied = "applied" if heal.applied else "not applied"
                lines.append(
                    f"**Heal detail** (`{heal.kind}` via `{heal.rung_used}`, {applied}):"
                )
                lines.append("")
                lines.append(
                    f"- anchor `{heal.old_anchor.template}` → "
                    f"`{heal.new_anchor.template}`"
                )
                if heal.screenshot:
                    lines.append("")
                    lines.append("| Healed frame |")
                    lines.append("| --- |")
                    lines.append(
                        f"| {_img(run, heal.screenshot, f'{heal.step_id} heal')} |"
                    )
                lines.append("")

    # -- Rung histogram -----------------------------------------------------
    lines.append("## Rung histogram")
    lines.append("")
    lines.append("| Rung | Count | |")
    lines.append("| --- | --- | --- |")
    extras = [r for r in report.rung_counts if r not in _RUNG_ORDER]
    for rung in (*_RUNG_ORDER, *extras):
        count = report.rung_counts.get(rung, 0)
        bar = "█" * count
        lines.append(f"| `{rung}` | {count} | {bar} |")
    lines.append("")

    # -- Totals ---------------------------------------------------------------
    lines.append("## Totals")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("| --- | --- |")
    lines.append(f"| Total time | {report.total_ms:.0f} ms |")
    lines.append(f"| Steps ok | {ok_count}/{len(report.results)} |")
    lines.append(f"| Heals | {report.heal_count} |")
    lines.append(f"| model_calls | {report.model_calls} |")
    lines.append(f"| est_model_cost_usd | ${report.est_model_cost_usd:.4f} |")
    lines.append("")

    out = run / "REPORT.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    return out


def render_bench_report(bench_json_path: Path | str, out_path: Path | str) -> Path:
    """Render a Markdown summary of a bench run.

    Args:
        bench_json_path: Path to the ``bench.json`` written by
            :func:`openadapt_flow.bench.run_bench`.
        out_path: Destination path for the Markdown report.

    Returns:
        Path to the written Markdown file.
    """
    bench = json.loads(Path(bench_json_path).read_text())
    out = Path(out_path)

    n = bench.get("n", 0)
    successes = bench.get("success_count", 0)
    rate = bench.get("success_rate", 0.0)
    icon = "✅" if successes == n and n > 0 else "❌"

    lines: list[str] = []
    lines.append(f"# {icon} Bench — {_md_phi(str(bench.get('workflow_name', '')))}")
    lines.append("")
    lines.append(f"- **Bundle:** `{bench.get('bundle', '')}`")
    lines.append(f"- **Iterations:** {n}")
    lines.append(f"- **Success rate:** {rate:.0%} ({successes}/{n})")
    lines.append(f"- **Total ms p50:** {bench.get('total_ms_p50', 0.0):.0f}")
    lines.append(f"- **Total ms p95:** {bench.get('total_ms_p95', 0.0):.0f}")
    lines.append(f"- **Heals (total):** {bench.get('heal_count', 0)}")
    lines.append(f"- **model_calls (total):** {bench.get('model_calls', 0)}")
    lines.append(
        f"- **est_model_cost_usd (total):** ${bench.get('est_model_cost_usd', 0.0):.4f}"
    )
    lines.append("")

    lines.append("## Rung histogram (aggregate)")
    lines.append("")
    lines.append("| Rung | Count | |")
    lines.append("| --- | --- | --- |")
    rung_counts = bench.get("rung_counts", {})
    extras = [r for r in rung_counts if r not in _RUNG_ORDER]
    for rung in (*_RUNG_ORDER, *extras):
        count = rung_counts.get(rung, 0)
        lines.append(f"| `{rung}` | {count} | {'█' * count} |")
    lines.append("")

    lines.append("## Iterations")
    lines.append("")
    lines.append("| # | Success | Total ms | Heals | Run dir |")
    lines.append("| --- | --- | --- | --- | --- |")
    for it in bench.get("iterations", []):
        status = "✅" if it.get("success") else "❌"
        lines.append(
            f"| {it.get('i', '?')} | {status} | {it.get('total_ms', 0.0):.0f} "
            f"| {it.get('heal_count', 0)} | `{it.get('run_dir', '')}` |"
        )
    lines.append("")

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")
    return out
