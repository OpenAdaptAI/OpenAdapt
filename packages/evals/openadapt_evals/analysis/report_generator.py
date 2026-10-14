"""HTML report generator for trace analysis.

Produces a self-contained HTML file with inline CSS and minimal inline JS
(no external dependencies) that provides a professional dashboard view of
evaluation results.  Supports single-run reports and two-run comparison
reports.

Features:
- Interactive step viewer with expandable task rows and inline screenshots
- CSS bar chart with colour-coded score bars (green / yellow / red)
- Failure analysis section grouped by failure type with counts and examples
- Comparison view with side-by-side stat cards and delta highlighting
- Base64-embedded screenshots for fully self-contained HTML
- Summary statistics with percentile breakdowns and cost estimates
- Professional dark theme with hover effects and responsive layout
- Copy-as-Markdown button for pasting summaries into Slack / GitHub
- Sortable table columns (JS-based, no dependencies)

Usage::

    from openadapt_evals.analysis import TraceAnalyzer, generate_report

    analyzer = TraceAnalyzer("benchmark_results/full_eval.jsonl")
    generate_report(analyzer=analyzer, output_path=Path("report.html"))

    # Comparison report
    other = TraceAnalyzer("benchmark_results/full_eval_v2.jsonl")
    generate_report(analyzer=analyzer, output_path=Path("diff.html"), compare_with=other)
"""

from __future__ import annotations

import base64
import html
import logging
import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from openadapt_evals.analysis.trace_analyzer import TraceAnalyzer

logger = logging.getLogger(__name__)

# Maximum size of a screenshot to embed (5 MB).  Larger files are skipped.
_MAX_SCREENSHOT_BYTES = 5 * 1024 * 1024


def generate_report(
    analyzer: TraceAnalyzer,
    output_path: Path,
    compare_with: TraceAnalyzer | None = None,
) -> Path:
    """Generate an HTML report from trace analysis.

    Args:
        analyzer: The primary trace analyzer.
        output_path: Path for the output HTML file.
        compare_with: Optional second analyzer for comparison.

    Returns:
        Path to the generated HTML file.
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    summary = analyzer.summary()
    failures = analyzer.failure_modes()

    comparison = None
    if compare_with:
        comparison = analyzer.compare(compare_with)

    report_html = _build_html(
        summary=summary,
        failures=failures,
        episodes=analyzer.episodes,
        comparison=comparison,
        source_path=str(analyzer.path),
        compare_source=str(compare_with.path) if compare_with else None,
    )

    output_path.write_text(report_html, encoding="utf-8")
    logger.info(
        "Report written to %s (%d episodes)",
        output_path,
        len(analyzer.episodes),
    )
    return output_path


# ---------------------------------------------------------------------------
# Top-level builder
# ---------------------------------------------------------------------------


def _build_html(
    summary: dict[str, Any],
    failures: list[dict[str, Any]],
    episodes: list,
    comparison: dict[str, Any] | None,
    source_path: str,
    compare_source: str | None,
) -> str:
    """Build the full HTML document."""
    title = "Trace Analysis Report"
    if comparison:
        title = "Trace Comparison Report"

    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    sections = [
        _section_summary(summary, episodes),
        _section_score_chart(episodes),
        _section_failure_modes(failures, episodes),
        _section_episode_table(episodes),
    ]

    if comparison:
        sections.insert(1, _section_comparison(comparison))

    # Build step viewer for episodes that have step data
    episodes_with_steps = [ep for ep in episodes if ep.steps]
    if episodes_with_steps:
        sections.append(_section_step_viewer(episodes_with_steps))

    body = "\n".join(sections)
    markdown_data = _build_markdown_payload(summary, failures)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
{_css()}
</head>
<body>
<div class="container">
    <header class="header">
        <div class="header-top">
            <h1>{html.escape(title)}</h1>
            <button class="btn-copy" onclick="copyMarkdown()" title="Copy summary as Markdown">
                <svg width="16" height="16" viewBox="0 0 16 16" fill="none" xmlns="http://www.w3.org/2000/svg">
                    <rect x="5" y="1" width="9" height="11" rx="1.5" stroke="currentColor" stroke-width="1.5" fill="none"/>
                    <path d="M3 5v8.5A1.5 1.5 0 004.5 15H11" stroke="currentColor" stroke-width="1.5" fill="none" stroke-linecap="round"/>
                </svg>
                <span class="btn-label">Copy as Markdown</span>
            </button>
        </div>
        <div class="subtitle">
            Source: <code>{html.escape(source_path)}</code>
            {f'<br>Compare: <code>{html.escape(compare_source)}</code>' if compare_source else ''}
            <br>Generated: <code>{generated_at}</code>
        </div>
    </header>
    {body}
    <footer class="footer">
        Generated by <strong>openadapt-evals</strong> trace analysis
    </footer>
</div>
<!-- Lightbox overlay -->
<div class="lightbox" id="lightbox" onclick="closeLightbox()">
    <img id="lightbox-img" src="" alt="Screenshot">
</div>
<script>
// --- Markdown copy -------------------------------------------------------
var _md = {_js_safe_json(markdown_data)};
function copyMarkdown() {{
    navigator.clipboard.writeText(_md).then(function() {{
        var btn = document.querySelector('.btn-copy .btn-label');
        var orig = btn.textContent;
        btn.textContent = 'Copied!';
        setTimeout(function() {{ btn.textContent = orig; }}, 1500);
    }});
}}
// --- Table sorting --------------------------------------------------------
function sortTable(table, col) {{
    var tbody = table.querySelector('tbody');
    if (!tbody) return;
    var rows = Array.from(tbody.querySelectorAll('tr'));
    var th = table.querySelectorAll('th')[col];
    var dir = th.dataset.sortDir === 'asc' ? 'desc' : 'asc';
    // Reset all headers
    table.querySelectorAll('th').forEach(function(h) {{ h.dataset.sortDir = ''; h.classList.remove('sorted-asc','sorted-desc'); }});
    th.dataset.sortDir = dir;
    th.classList.add(dir === 'asc' ? 'sorted-asc' : 'sorted-desc');
    rows.sort(function(a, b) {{
        var va = a.cells[col].getAttribute('data-sort') || a.cells[col].textContent.trim();
        var vb = b.cells[col].getAttribute('data-sort') || b.cells[col].textContent.trim();
        var na = parseFloat(va);
        var nb = parseFloat(vb);
        if (!isNaN(na) && !isNaN(nb)) {{
            return dir === 'asc' ? na - nb : nb - na;
        }}
        return dir === 'asc' ? va.localeCompare(vb) : vb.localeCompare(va);
    }});
    rows.forEach(function(r) {{ tbody.appendChild(r); }});
}}
// --- Expander toggle ------------------------------------------------------
function toggleExpander(el) {{
    // Stop event from propagating to child elements
    el.classList.toggle('open');
}}
// --- Lightbox -------------------------------------------------------------
function openLightbox(src) {{
    event.stopPropagation();
    document.getElementById('lightbox-img').src = src;
    document.getElementById('lightbox').classList.add('active');
}}
function closeLightbox() {{
    document.getElementById('lightbox').classList.remove('active');
}}
document.addEventListener('keydown', function(e) {{
    if (e.key === 'Escape') closeLightbox();
}});
// --- Inline step viewer toggle (from episode table) -----------------------
function toggleInlineSteps(taskId) {{
    var row = document.getElementById('steps-' + taskId);
    if (!row) return;
    var arrow = document.getElementById('arrow-' + taskId);
    if (row.style.display === 'none' || row.style.display === '') {{
        row.style.display = 'table-row';
        if (arrow) arrow.classList.add('open');
    }} else {{
        row.style.display = 'none';
        if (arrow) arrow.classList.remove('open');
    }}
}}
</script>
</body>
</html>"""


def _js_safe_json(s: str) -> str:
    """JSON-encode a string for embedding in a JS literal."""
    import json

    return json.dumps(s)


# ---------------------------------------------------------------------------
# CSS
# ---------------------------------------------------------------------------


def _css() -> str:
    """Return all inline CSS."""
    return """<style>
:root {
    --bg: #0f1117;
    --bg-card: #1a1b23;
    --bg-card-hover: #22232d;
    --bg-elevated: #252630;
    --border: rgba(255,255,255,0.08);
    --border-hover: rgba(255,255,255,0.15);
    --text: #e4e4e7;
    --text-secondary: #a1a1aa;
    --text-muted: #71717a;
    --accent: #6366f1;
    --accent-light: #818cf8;
    --success: #22c55e;
    --success-bg: rgba(34,197,94,0.1);
    --danger: #ef4444;
    --danger-bg: rgba(239,68,68,0.1);
    --warning: #f59e0b;
    --warning-bg: rgba(245,158,11,0.1);
    --info: #3b82f6;
    --info-bg: rgba(59,130,246,0.1);
    --radius: 10px;
    --shadow-sm: 0 1px 2px rgba(0,0,0,0.3);
    --shadow-md: 0 4px 12px rgba(0,0,0,0.4);
    --transition: 0.2s ease;
}

* { box-sizing: border-box; margin: 0; padding: 0; }

body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto,
                 'Helvetica Neue', Arial, sans-serif;
    background: var(--bg);
    color: var(--text);
    line-height: 1.6;
    -webkit-font-smoothing: antialiased;
}

.container {
    max-width: 1280px;
    margin: 0 auto;
    padding: 32px;
}

/* Header */
.header {
    margin-bottom: 36px;
    padding-bottom: 24px;
    border-bottom: 1px solid var(--border);
}

.header-top {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 8px;
}

.header h1 {
    font-size: 1.75rem;
    font-weight: 700;
    letter-spacing: -0.02em;
}

.btn-copy {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 8px 14px;
    border-radius: 8px;
    border: 1px solid var(--border);
    background: var(--bg-card);
    color: var(--text-secondary);
    font-size: 0.8rem;
    cursor: pointer;
    transition: all var(--transition);
    white-space: nowrap;
}

.btn-copy:hover {
    background: var(--bg-card-hover);
    border-color: var(--border-hover);
    color: var(--text);
}

.subtitle {
    color: var(--text-secondary);
    font-size: 0.85rem;
    line-height: 1.7;
}

.subtitle code {
    background: rgba(255,255,255,0.06);
    padding: 2px 6px;
    border-radius: 4px;
    font-size: 0.8rem;
}

/* Summary cards */
.cards {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(155px, 1fr));
    gap: 14px;
    margin-bottom: 32px;
}

.card {
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 18px 20px;
    transition: all var(--transition);
    box-shadow: var(--shadow-sm);
}

.card:hover {
    background: var(--bg-card-hover);
    border-color: var(--border-hover);
    transform: translateY(-1px);
    box-shadow: var(--shadow-md);
}

.card-label {
    font-size: 0.7rem;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--text-muted);
    margin-bottom: 6px;
}

.card-value {
    font-size: 1.4rem;
    font-weight: 700;
    letter-spacing: -0.02em;
}

.card-sub {
    font-size: 0.72rem;
    color: var(--text-muted);
    margin-top: 4px;
}

.card-value.success { color: var(--success); }
.card-value.danger { color: var(--danger); }
.card-value.warning { color: var(--warning); }
.card-value.info { color: var(--info); }
.card-value.accent { color: var(--accent-light); }

/* Section */
.section {
    margin-bottom: 40px;
}

.section-title {
    font-size: 1.1rem;
    font-weight: 600;
    margin-bottom: 16px;
    color: var(--text);
    display: flex;
    align-items: center;
    gap: 8px;
}

.section-title::before {
    content: '';
    display: inline-block;
    width: 4px;
    height: 20px;
    background: var(--accent);
    border-radius: 2px;
}

/* Score bar chart */
.score-chart {
    display: flex;
    flex-direction: column;
    gap: 6px;
}

.score-bar-row {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 4px 0;
    transition: background var(--transition);
    border-radius: 4px;
}

.score-bar-row:hover {
    background: rgba(255,255,255,0.02);
}

.score-bar-label {
    width: 180px;
    font-size: 0.8rem;
    color: var(--text-secondary);
    text-align: right;
    flex-shrink: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}

.score-bar-track {
    flex: 1;
    height: 20px;
    background: rgba(255,255,255,0.04);
    border-radius: 5px;
    overflow: hidden;
    position: relative;
}

.score-bar-fill {
    height: 100%;
    border-radius: 5px;
    transition: width 0.6s ease;
    min-width: 2px;
}

.score-bar-fill.score-high { background: var(--success); }
.score-bar-fill.score-mid { background: var(--warning); }
.score-bar-fill.score-low { background: var(--danger); }

.score-bar-value {
    width: 50px;
    font-size: 0.8rem;
    font-weight: 600;
    flex-shrink: 0;
    text-align: right;
}

.score-bar-value.score-high { color: var(--success); }
.score-bar-value.score-mid { color: var(--warning); }
.score-bar-value.score-low { color: var(--danger); }

/* Failure mode chart */
.bar-chart {
    display: flex;
    flex-direction: column;
    gap: 10px;
}

.bar-row {
    display: flex;
    align-items: center;
    gap: 12px;
}

.bar-label {
    width: 180px;
    font-size: 0.85rem;
    color: var(--text-secondary);
    text-align: right;
    flex-shrink: 0;
}

.bar-track {
    flex: 1;
    height: 24px;
    background: rgba(255,255,255,0.04);
    border-radius: 6px;
    overflow: hidden;
    position: relative;
}

.bar-fill {
    height: 100%;
    border-radius: 6px;
    transition: width 0.6s ease;
    min-width: 2px;
}

.bar-fill.mode-loop_detected { background: var(--warning); }
.bar-fill.mode-timeout { background: var(--info); }
.bar-fill.mode-server_error { background: var(--danger); }
.bar-fill.mode-agent_error { background: #f97316; }
.bar-fill.mode-planner_wrong_target { background: #a855f7; }
.bar-fill.mode-grounder_miss { background: #ec4899; }
.bar-fill.mode-task_incomplete { background: #06b6d4; }
.bar-fill.mode-unknown_failure { background: var(--text-muted); }

.bar-count {
    width: 70px;
    font-size: 0.85rem;
    color: var(--text-secondary);
    flex-shrink: 0;
}

/* Failure examples */
.failure-examples {
    margin-top: 12px;
    padding: 12px 16px;
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: var(--radius);
}

.failure-example-label {
    font-size: 0.72rem;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: var(--text-muted);
    margin-bottom: 6px;
}

.failure-example-ids {
    font-family: 'SF Mono', 'Fira Code', 'Consolas', monospace;
    font-size: 0.78rem;
    color: var(--text-secondary);
    word-break: break-word;
}

/* Table */
.table-wrap {
    overflow-x: auto;
    border: 1px solid var(--border);
    border-radius: var(--radius);
    box-shadow: var(--shadow-sm);
}

table {
    width: 100%;
    border-collapse: collapse;
    font-size: 0.85rem;
}

thead {
    background: rgba(255,255,255,0.03);
}

th {
    text-align: left;
    padding: 12px 14px;
    font-weight: 600;
    font-size: 0.72rem;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: var(--text-muted);
    border-bottom: 1px solid var(--border);
    cursor: pointer;
    user-select: none;
    white-space: nowrap;
    position: relative;
    transition: color var(--transition);
}

th:hover { color: var(--text); }

th.sorted-asc::after,
th.sorted-desc::after {
    position: absolute;
    right: 6px;
    font-size: 0.65rem;
    color: var(--accent-light);
}

th.sorted-asc::after { content: ' \\25B2'; }
th.sorted-desc::after { content: ' \\25BC'; }

td {
    padding: 10px 14px;
    border-bottom: 1px solid var(--border);
    vertical-align: middle;
}

tr:last-child td { border-bottom: none; }

tbody tr {
    transition: background var(--transition);
}

tbody tr:hover td {
    background: rgba(255,255,255,0.025);
}

.clickable-row {
    cursor: pointer;
}

.clickable-row:hover td {
    background: rgba(99,102,241,0.06);
}

/* Inline step detail row (hidden by default) */
.step-detail-row td {
    padding: 0;
    background: var(--bg-card);
}

.step-detail-content {
    padding: 16px 20px;
}

.badge {
    display: inline-block;
    padding: 2px 10px;
    border-radius: 20px;
    font-size: 0.72rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.03em;
}

.badge-pass { background: var(--success-bg); color: var(--success); }
.badge-fail { background: var(--danger-bg); color: var(--danger); }
.badge-error { background: var(--warning-bg); color: var(--warning); }
.badge-infra { background: var(--info-bg); color: var(--info); }

.task-id {
    font-family: 'SF Mono', 'Fira Code', monospace;
    font-size: 0.8rem;
    color: var(--text-secondary);
    max-width: 200px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}

.inline-arrow {
    display: inline-block;
    width: 14px;
    font-size: 0.65rem;
    color: var(--text-muted);
    transition: transform var(--transition);
    margin-right: 4px;
}

.inline-arrow.open {
    transform: rotate(90deg);
}

/* Comparison */
.diff-positive { color: var(--success); }
.diff-negative { color: var(--danger); }
.diff-neutral { color: var(--text-muted); }

.comparison-grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 16px;
    margin-bottom: 20px;
}

.comparison-card {
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 20px;
    box-shadow: var(--shadow-sm);
    transition: all var(--transition);
}

.comparison-card:hover {
    border-color: var(--border-hover);
    box-shadow: var(--shadow-md);
}

.comparison-card-title {
    font-size: 0.72rem;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: var(--text-muted);
    margin-bottom: 12px;
    padding-bottom: 8px;
    border-bottom: 1px solid var(--border);
}

.comparison-stat {
    display: flex;
    justify-content: space-between;
    padding: 4px 0;
    font-size: 0.85rem;
}

.comparison-stat-label {
    color: var(--text-secondary);
}

.comparison-stat-value {
    font-weight: 600;
}

/* Step viewer */
.step-viewer {
    margin-top: 12px;
}

.episode-expander {
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    margin-bottom: 8px;
    overflow: hidden;
    transition: border-color var(--transition);
}

.episode-expander:hover {
    border-color: var(--border-hover);
}

.episode-expander-header {
    padding: 12px 16px;
    cursor: pointer;
    display: flex;
    justify-content: space-between;
    align-items: center;
    font-size: 0.85rem;
    transition: background var(--transition);
}

.episode-expander-header:hover {
    background: var(--bg-card-hover);
}

.episode-expander-header .arrow {
    transition: transform var(--transition);
    color: var(--text-muted);
}

.episode-expander.open .arrow {
    transform: rotate(90deg);
}

.episode-expander-body {
    display: none;
    padding: 0 16px 16px;
}

.episode-expander.open .episode-expander-body {
    display: block;
}

.step-card {
    display: flex;
    gap: 16px;
    padding: 12px 0;
    border-bottom: 1px solid var(--border);
    align-items: flex-start;
}

.step-card:last-child { border-bottom: none; }

.step-number {
    font-size: 0.72rem;
    font-weight: 700;
    color: var(--accent-light);
    background: rgba(99,102,241,0.1);
    width: 32px;
    height: 32px;
    display: flex;
    align-items: center;
    justify-content: center;
    border-radius: 8px;
    flex-shrink: 0;
}

.step-details {
    flex: 1;
    min-width: 0;
}

.step-action {
    font-size: 0.85rem;
    font-weight: 600;
    margin-bottom: 4px;
}

.step-meta {
    font-size: 0.8rem;
    color: var(--text-secondary);
    word-break: break-word;
    line-height: 1.5;
}

.step-screenshot {
    flex-shrink: 0;
}

.step-screenshot img {
    max-width: 240px;
    max-height: 160px;
    border-radius: 6px;
    border: 1px solid var(--border);
    cursor: pointer;
    transition: all var(--transition);
}

.step-screenshot img:hover {
    transform: scale(1.03);
    border-color: var(--accent);
    box-shadow: 0 0 12px rgba(99,102,241,0.3);
}

/* Lightbox */
.lightbox {
    display: none;
    position: fixed;
    top: 0;
    left: 0;
    width: 100%;
    height: 100%;
    background: rgba(0,0,0,0.92);
    z-index: 1000;
    cursor: pointer;
    align-items: center;
    justify-content: center;
}

.lightbox.active {
    display: flex;
}

.lightbox img {
    max-width: 95vw;
    max-height: 95vh;
    border-radius: 8px;
    box-shadow: 0 0 40px rgba(0,0,0,0.5);
}

/* Footer */
.footer {
    margin-top: 48px;
    padding-top: 20px;
    border-top: 1px solid var(--border);
    text-align: center;
    font-size: 0.78rem;
    color: var(--text-muted);
}

/* Responsive */
@media (max-width: 768px) {
    .container { padding: 16px; }
    .cards { grid-template-columns: repeat(2, 1fr); }
    .bar-label { width: 120px; font-size: 0.75rem; }
    .score-bar-label { width: 120px; font-size: 0.73rem; }
    .comparison-grid { grid-template-columns: 1fr; }
    .step-card { flex-direction: column; }
    .step-screenshot img { max-width: 100%; }
    .header-top { flex-direction: column; align-items: flex-start; gap: 10px; }
}

@media print {
    body { background: #fff; color: #1a1a1a; }
    .container { max-width: none; }
    .btn-copy { display: none; }
    .card { border: 1px solid #ddd; }
}
</style>"""


# ---------------------------------------------------------------------------
# Markdown payload
# ---------------------------------------------------------------------------


def _build_markdown_payload(
    summary: dict[str, Any],
    failures: list[dict[str, Any]],
) -> str:
    """Build a Markdown-formatted summary for clipboard copy."""
    sr = summary["success_rate"]
    cost = summary["cost_estimate_usd"]
    model = summary.get("model") or "unknown"

    lines = [
        "## Trace Analysis Summary",
        "",
        "| Metric | Value |",
        "|--------|-------|",
        f"| Model | {model} |",
        f"| Episodes | {summary['total_episodes']} |",
        f"| Success Rate | {sr:.1%} |",
        f"| Avg Score | {summary['avg_score']:.3f} |",
        f"| Total Steps | {summary['total_steps']} |",
        f"| Avg Steps/Ep | {summary['avg_steps_per_episode']:.1f} |",
        f"| Est. Cost | ${cost:.2f} |",
    ]

    if failures:
        lines.append("")
        lines.append("### Failure Modes")
        lines.append("")
        lines.append("| Mode | Count | % |")
        lines.append("|------|-------|---|")
        for fm in failures:
            label = fm["mode"].replace("_", " ").title()
            lines.append(f"| {label} | {fm['count']} | {fm['percentage']:.0f}% |")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Screenshot embedding helper
# ---------------------------------------------------------------------------


def _embed_screenshot(path_str: str | None) -> str | None:
    """Convert a screenshot file path to a base64 data URI.

    Returns None if the file doesn't exist, is too large, or can't be read.
    """
    if not path_str:
        return None

    screenshot_path = Path(path_str)
    if not screenshot_path.exists():
        return None

    try:
        size = screenshot_path.stat().st_size
        if size > _MAX_SCREENSHOT_BYTES:
            logger.debug("Skipping large screenshot (%d bytes): %s", size, path_str)
            return None

        img_data = screenshot_path.read_bytes()
        b64 = base64.b64encode(img_data).decode("ascii")

        # Determine MIME type from extension
        suffix = screenshot_path.suffix.lower()
        mime_map = {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".gif": "image/gif",
            ".webp": "image/webp",
            ".bmp": "image/bmp",
        }
        mime = mime_map.get(suffix, "image/png")

        return f"data:{mime};base64,{b64}"
    except Exception:
        logger.debug("Failed to embed screenshot: %s", path_str, exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Section: Summary (with percentile breakdown)
# ---------------------------------------------------------------------------


def _section_summary(summary: dict[str, Any], episodes: list) -> str:
    """Build the summary cards section with percentile breakdowns."""
    success_rate = summary["success_rate"]
    sr_class = "success" if success_rate >= 0.5 else ("warning" if success_rate >= 0.2 else "danger")

    cost = summary["cost_estimate_usd"]
    cost_str = f"${cost:.2f}" if cost < 100 else f"${cost:.0f}"

    total_time = summary["total_time"]
    if total_time > 3600:
        time_str = f"{total_time / 3600:.1f}h"
    elif total_time > 60:
        time_str = f"{total_time / 60:.1f}m"
    else:
        time_str = f"{total_time:.0f}s"

    model = summary.get("model") or "unknown"

    # Compute percentile breakdown
    scores = sorted([ep.score for ep in episodes]) if episodes else []
    times = sorted([ep.elapsed_seconds for ep in episodes if ep.elapsed_seconds > 0])

    if scores:
        median_score = statistics.median(scores)
        p25_score = _percentile(scores, 25)
        p75_score = _percentile(scores, 75)
        score_sub = f"P25: {p25_score:.2f} | P75: {p75_score:.2f}"
    else:
        median_score = 0.0
        score_sub = ""

    if times:
        median_time = statistics.median(times)
        p25_time = _percentile(times, 25)
        p75_time = _percentile(times, 75)
        time_sub = f"P25: {_fmt_time(p25_time)} | P75: {_fmt_time(p75_time)}"
    else:
        median_time = 0.0
        time_sub = ""

    return f"""
<div class="section">
    <h2 class="section-title">Summary</h2>
    <div class="cards">
        <div class="card">
            <div class="card-label">Episodes</div>
            <div class="card-value accent">{summary['total_episodes']}</div>
        </div>
        <div class="card">
            <div class="card-label">Success Rate</div>
            <div class="card-value {sr_class}">{success_rate:.1%}</div>
        </div>
        <div class="card">
            <div class="card-label">Avg Score</div>
            <div class="card-value">{summary['avg_score']:.3f}</div>
            {f'<div class="card-sub">{score_sub}</div>' if score_sub else ''}
        </div>
        <div class="card">
            <div class="card-label">Median Score</div>
            <div class="card-value">{median_score:.3f}</div>
        </div>
        <div class="card">
            <div class="card-label">Total Steps</div>
            <div class="card-value">{summary['total_steps']}</div>
        </div>
        <div class="card">
            <div class="card-label">Avg Steps / Episode</div>
            <div class="card-value">{summary['avg_steps_per_episode']:.1f}</div>
        </div>
        <div class="card">
            <div class="card-label">Total Time</div>
            <div class="card-value">{time_str}</div>
            {f'<div class="card-sub">{time_sub}</div>' if time_sub else ''}
        </div>
        <div class="card">
            <div class="card-label">Median Time</div>
            <div class="card-value">{_fmt_time(median_time)}</div>
        </div>
        <div class="card">
            <div class="card-label">Est. Cost</div>
            <div class="card-value info">{cost_str}</div>
        </div>
        <div class="card">
            <div class="card-label">Model</div>
            <div class="card-value" style="font-size:0.85rem">{html.escape(model)}</div>
        </div>
    </div>
</div>"""


def _percentile(sorted_values: list[float], pct: int) -> float:
    """Compute the *pct*-th percentile from a pre-sorted list."""
    if not sorted_values:
        return 0.0
    k = (len(sorted_values) - 1) * (pct / 100.0)
    f = int(k)
    c = f + 1
    if c >= len(sorted_values):
        return sorted_values[-1]
    d = k - f
    return sorted_values[f] + d * (sorted_values[c] - sorted_values[f])


def _fmt_time(seconds: float) -> str:
    """Format seconds into a human-readable duration string."""
    if seconds > 3600:
        return f"{seconds / 3600:.1f}h"
    if seconds > 60:
        return f"{seconds / 60:.1f}m"
    return f"{seconds:.0f}s"


# ---------------------------------------------------------------------------
# Section: Score bar chart
# ---------------------------------------------------------------------------


def _section_score_chart(episodes: list) -> str:
    """Build a CSS-only bar chart of scores per task."""
    if not episodes:
        return ""

    bars = []
    for ep in episodes:
        score = ep.score
        pct = score * 100

        if score > 0.75:
            color_class = "score-high"
        elif score >= 0.25:
            color_class = "score-mid"
        else:
            color_class = "score-low"

        task_label = ep.task_id
        if len(task_label) > 24:
            task_label = task_label[:22] + ".."

        bars.append(f"""
        <div class="score-bar-row">
            <div class="score-bar-label" title="{html.escape(ep.task_id)}">{html.escape(task_label)}</div>
            <div class="score-bar-track">
                <div class="score-bar-fill {color_class}" style="width:{max(pct, 1):.1f}%"></div>
            </div>
            <div class="score-bar-value {color_class}">{score:.2f}</div>
        </div>""")

    return f"""
<div class="section">
    <h2 class="section-title">Score Distribution</h2>
    <div class="score-chart">
        {''.join(bars)}
    </div>
</div>"""


# ---------------------------------------------------------------------------
# Section: Failure modes (with example episode IDs)
# ---------------------------------------------------------------------------


def _section_failure_modes(
    failures: list[dict[str, Any]], episodes: list
) -> str:
    """Build the failure modes bar chart section with example episode IDs."""
    if not failures:
        return """
<div class="section">
    <h2 class="section-title">Failure Analysis</h2>
    <p style="color:var(--text-secondary);font-size:0.9rem;">No failures detected.</p>
</div>"""

    max_count = max(f["count"] for f in failures) if failures else 1

    bars = []
    for fm in failures:
        pct = fm["count"] / max_count * 100
        mode_label = fm["mode"].replace("_", " ").title()

        # Show up to 5 example episode IDs
        example_ids = fm.get("episode_ids", [])[:5]
        examples_html = ""
        if example_ids:
            remaining = fm["count"] - len(example_ids)
            ids_str = ", ".join(html.escape(eid[:24]) for eid in example_ids)
            if remaining > 0:
                ids_str += f" (+{remaining} more)"
            examples_html = f"""
            <div class="failure-examples">
                <div class="failure-example-label">Example Episodes</div>
                <div class="failure-example-ids">{ids_str}</div>
            </div>"""

        bars.append(f"""
        <div style="margin-bottom:8px">
            <div class="bar-row">
                <div class="bar-label">{html.escape(mode_label)}</div>
                <div class="bar-track">
                    <div class="bar-fill mode-{fm['mode']}" style="width:{pct:.1f}%"></div>
                </div>
                <div class="bar-count">{fm['count']} ({fm['percentage']:.0f}%)</div>
            </div>
            {examples_html}
        </div>""")

    return f"""
<div class="section">
    <h2 class="section-title">Failure Analysis</h2>
    <div class="bar-chart">
        {''.join(bars)}
    </div>
</div>"""


# ---------------------------------------------------------------------------
# Section: Episode table with inline expandable steps
# ---------------------------------------------------------------------------


def _section_episode_table(episodes: list) -> str:
    """Build the sortable episode table with inline step expansion."""
    rows = []
    for i, ep in enumerate(episodes):
        if ep.success:
            badge = '<span class="badge badge-pass">Pass</span>'
        elif ep.error_type == "infrastructure":
            badge = '<span class="badge badge-infra">Infra</span>'
        elif ep.error:
            badge = '<span class="badge badge-error">Error</span>'
        else:
            badge = '<span class="badge badge-fail">Fail</span>'

        task_display = ep.task_id
        if len(task_display) > 24:
            task_display = task_display[:22] + ".."

        time_str = f"{ep.elapsed_seconds:.1f}s" if ep.elapsed_seconds else "-"
        time_sort = f"{ep.elapsed_seconds:.4f}" if ep.elapsed_seconds else "0"

        instruction = html.escape(ep.task_instruction[:80]) if ep.task_instruction else ""
        if ep.task_instruction and len(ep.task_instruction) > 80:
            instruction += "..."

        error_cell = ""
        if ep.error:
            error_text = str(ep.error)[:60]
            error_cell = f'<span style="color:var(--text-muted);font-size:0.75rem">{html.escape(error_text)}</span>'

        # Determine if this row is clickable (has steps)
        has_steps = bool(ep.steps)
        row_class = "clickable-row" if has_steps else ""
        row_onclick = f'onclick="toggleInlineSteps(\'ep-{i}\')"' if has_steps else ""
        arrow_html = f'<span class="inline-arrow" id="arrow-ep-{i}">&#9654;</span>' if has_steps else '<span style="display:inline-block;width:18px"></span>'

        # Score color
        score_class = ""
        if ep.score > 0.75:
            score_class = 'style="color:var(--success)"'
        elif ep.score >= 0.25:
            score_class = 'style="color:var(--warning)"'
        elif ep.score < 0.25 and not ep.success:
            score_class = 'style="color:var(--danger)"'

        rows.append(f"""
        <tr class="{row_class}" {row_onclick}>
            <td class="task-id" title="{html.escape(ep.task_id)}">{arrow_html}{html.escape(task_display)}</td>
            <td>{instruction}</td>
            <td style="text-align:center" data-sort="{1 if ep.success else 0}">{badge}</td>
            <td style="text-align:right" data-sort="{ep.score:.4f}" {score_class}>{ep.score:.2f}</td>
            <td style="text-align:right">{ep.num_steps}</td>
            <td style="text-align:right" data-sort="{time_sort}">{time_str}</td>
            <td>{error_cell}</td>
        </tr>""")

        # Inline step detail row (hidden by default)
        if has_steps:
            step_html = _build_inline_steps(ep)
            rows.append(f"""
        <tr id="steps-ep-{i}" class="step-detail-row" style="display:none">
            <td colspan="7">
                <div class="step-detail-content">
                    {step_html}
                </div>
            </td>
        </tr>""")

    table_id = "episode-table"

    return f"""
<div class="section">
    <h2 class="section-title">Episodes ({len(episodes)})</h2>
    <div class="table-wrap">
        <table id="{table_id}">
            <thead>
                <tr>
                    <th onclick="sortTable(document.getElementById('{table_id}'), 0)">Task ID</th>
                    <th onclick="sortTable(document.getElementById('{table_id}'), 1)">Instruction</th>
                    <th onclick="sortTable(document.getElementById('{table_id}'), 2)" style="text-align:center">Status</th>
                    <th onclick="sortTable(document.getElementById('{table_id}'), 3)" style="text-align:right">Score</th>
                    <th onclick="sortTable(document.getElementById('{table_id}'), 4)" style="text-align:right">Steps</th>
                    <th onclick="sortTable(document.getElementById('{table_id}'), 5)" style="text-align:right">Time</th>
                    <th>Error</th>
                </tr>
            </thead>
            <tbody>
                {''.join(rows)}
            </tbody>
        </table>
    </div>
</div>"""


def _build_inline_steps(ep: "Episode") -> str:  # noqa: F821
    """Build inline step cards for the expandable row in the episode table."""
    step_cards = []
    for step in ep.steps:
        action_label = step.action_type or "unknown"
        target_label = step.target or ""

        meta_parts = []
        if step.instruction:
            meta_parts.append(
                f"<strong>Instruction:</strong> {html.escape(step.instruction)}"
            )
        if step.reasoning:
            reason_text = step.reasoning[:200]
            if len(step.reasoning) > 200:
                reason_text += "..."
            meta_parts.append(
                f"<strong>Reasoning:</strong> {html.escape(reason_text)}"
            )
        if step.decision:
            meta_parts.append(
                f"<strong>Decision:</strong> {html.escape(step.decision)}"
            )

        meta_html = "<br>".join(meta_parts) if meta_parts else ""

        screenshot_html = ""
        data_uri = _embed_screenshot(step.screenshot_path)
        if data_uri:
            screenshot_html = f"""
            <div class="step-screenshot">
                <img src="{data_uri}"
                     alt="Step {step.step_index}"
                     onclick="openLightbox(this.src)"
                     loading="lazy">
            </div>"""

        step_cards.append(f"""
        <div class="step-card">
            <div class="step-number">{step.step_index}</div>
            <div class="step-details">
                <div class="step-action">{html.escape(action_label)} {html.escape(target_label)}</div>
                <div class="step-meta">{meta_html}</div>
            </div>
            {screenshot_html}
        </div>""")

    return "".join(step_cards)


# ---------------------------------------------------------------------------
# Section: Comparison
# ---------------------------------------------------------------------------


def _section_comparison(comparison: dict[str, Any]) -> str:
    """Build the comparison section with side-by-side stat cards and delta highlighting."""
    sd = comparison["summary_diff"]
    old = sd["old"]
    new = sd["new"]

    sr_delta = sd["success_rate_delta"]
    sr_class = "diff-positive" if sr_delta > 0 else ("diff-negative" if sr_delta < 0 else "diff-neutral")
    sr_arrow = "+" if sr_delta > 0 else ""

    score_delta = sd["avg_score_delta"]
    sc_class = "diff-positive" if score_delta > 0 else ("diff-negative" if score_delta < 0 else "diff-neutral")
    sc_arrow = "+" if score_delta > 0 else ""

    # Steps delta
    old_steps = old.get("avg_steps_per_episode", 0)
    new_steps = new.get("avg_steps_per_episode", 0)
    steps_delta = new_steps - old_steps
    # Fewer steps is better
    steps_class = "diff-positive" if steps_delta < 0 else ("diff-negative" if steps_delta > 0 else "diff-neutral")
    steps_arrow = "+" if steps_delta > 0 else ""

    # Cost delta
    old_cost = old.get("cost_estimate_usd", 0)
    new_cost = new.get("cost_estimate_usd", 0)
    cost_delta = new_cost - old_cost
    cost_class = "diff-positive" if cost_delta < 0 else ("diff-negative" if cost_delta > 0 else "diff-neutral")
    cost_arrow = "+" if cost_delta > 0 else ""

    # Side-by-side stat cards
    side_by_side = f"""
    <div class="comparison-grid">
        <div class="comparison-card">
            <div class="comparison-card-title">Baseline (Run A)</div>
            <div class="comparison-stat">
                <span class="comparison-stat-label">Episodes</span>
                <span class="comparison-stat-value">{old['total_episodes']}</span>
            </div>
            <div class="comparison-stat">
                <span class="comparison-stat-label">Success Rate</span>
                <span class="comparison-stat-value">{old['success_rate']:.1%}</span>
            </div>
            <div class="comparison-stat">
                <span class="comparison-stat-label">Avg Score</span>
                <span class="comparison-stat-value">{old['avg_score']:.3f}</span>
            </div>
            <div class="comparison-stat">
                <span class="comparison-stat-label">Avg Steps</span>
                <span class="comparison-stat-value">{old_steps:.1f}</span>
            </div>
            <div class="comparison-stat">
                <span class="comparison-stat-label">Est. Cost</span>
                <span class="comparison-stat-value">${old_cost:.2f}</span>
            </div>
            <div class="comparison-stat">
                <span class="comparison-stat-label">Model</span>
                <span class="comparison-stat-value" style="font-size:0.8rem">{html.escape(old.get('model') or 'unknown')}</span>
            </div>
        </div>
        <div class="comparison-card">
            <div class="comparison-card-title">New Run (Run B)</div>
            <div class="comparison-stat">
                <span class="comparison-stat-label">Episodes</span>
                <span class="comparison-stat-value">{new['total_episodes']}</span>
            </div>
            <div class="comparison-stat">
                <span class="comparison-stat-label">Success Rate</span>
                <span class="comparison-stat-value">{new['success_rate']:.1%}</span>
            </div>
            <div class="comparison-stat">
                <span class="comparison-stat-label">Avg Score</span>
                <span class="comparison-stat-value">{new['avg_score']:.3f}</span>
            </div>
            <div class="comparison-stat">
                <span class="comparison-stat-label">Avg Steps</span>
                <span class="comparison-stat-value">{new_steps:.1f}</span>
            </div>
            <div class="comparison-stat">
                <span class="comparison-stat-label">Est. Cost</span>
                <span class="comparison-stat-value">${new_cost:.2f}</span>
            </div>
            <div class="comparison-stat">
                <span class="comparison-stat-label">Model</span>
                <span class="comparison-stat-value" style="font-size:0.8rem">{html.escape(new.get('model') or 'unknown')}</span>
            </div>
        </div>
    </div>"""

    # Delta cards
    delta_cards = f"""
    <div class="cards" style="grid-template-columns: repeat(4, 1fr);">
        <div class="card">
            <div class="card-label">Success Rate Delta</div>
            <div class="card-value {sr_class}">{sr_arrow}{sr_delta:.1%}</div>
        </div>
        <div class="card">
            <div class="card-label">Avg Score Delta</div>
            <div class="card-value {sc_class}">{sc_arrow}{score_delta:.3f}</div>
        </div>
        <div class="card">
            <div class="card-label">Improved</div>
            <div class="card-value success">{len(comparison['improved'])}</div>
        </div>
        <div class="card">
            <div class="card-label">Regressed</div>
            <div class="card-value danger">{len(comparison['regressed'])}</div>
        </div>
        <div class="card">
            <div class="card-label">Avg Steps Delta</div>
            <div class="card-value {steps_class}">{steps_arrow}{steps_delta:.1f}</div>
        </div>
        <div class="card">
            <div class="card-label">Est. Cost Delta</div>
            <div class="card-value {cost_class}">{cost_arrow}${cost_delta:.2f}</div>
        </div>
    </div>"""

    # Task-level diff table
    diff_rows = ""
    all_diffs = (
        [(item, "improved") for item in comparison["improved"]]
        + [(item, "regressed") for item in comparison["regressed"]]
        + [(item, "unchanged") for item in comparison["unchanged"]]
    )
    # Sort by absolute delta descending
    all_diffs.sort(key=lambda x: abs(x[0]["score_delta"]), reverse=True)

    for item, change_type in all_diffs[:30]:
        delta = item["score_delta"]
        if change_type == "improved":
            delta_class = "diff-positive"
            delta_str = f"+{delta:.2f}"
        elif change_type == "regressed":
            delta_class = "diff-negative"
            delta_str = f"{delta:.2f}"
        else:
            delta_class = "diff-neutral"
            delta_str = f"{delta:.2f}"

        tid = item["task_id"]
        if len(tid) > 24:
            tid = tid[:22] + ".."

        # Step delta (fewer is better)
        step_delta = item.get("new_steps", 0) - item.get("old_steps", 0)
        step_delta_class = "diff-positive" if step_delta < 0 else ("diff-negative" if step_delta > 0 else "diff-neutral")
        step_delta_str = f"+{step_delta}" if step_delta > 0 else str(step_delta)

        diff_rows += f"""
        <tr>
            <td class="task-id" title="{html.escape(item['task_id'])}">{html.escape(tid)}</td>
            <td style="text-align:right">{item['old_score']:.2f}</td>
            <td style="text-align:right">{item['new_score']:.2f}</td>
            <td style="text-align:right" class="{delta_class}">{delta_str}</td>
            <td style="text-align:right">{item.get('old_steps', '-')}</td>
            <td style="text-align:right">{item.get('new_steps', '-')}</td>
            <td style="text-align:right" class="{step_delta_class}">{step_delta_str}</td>
        </tr>"""

    total_compared = len(comparison["improved"]) + len(comparison["regressed"]) + len(comparison["unchanged"])
    diff_table = ""
    if diff_rows:
        diff_table = f"""
    <div style="margin-top:20px">
        <h3 style="font-size:0.9rem;color:var(--text-secondary);margin-bottom:10px">
            Task-Level Changes ({total_compared} tasks, showing top {min(total_compared, 30)} by delta)
        </h3>
        <div class="table-wrap">
            <table>
                <thead>
                    <tr>
                        <th>Task</th>
                        <th style="text-align:right">Old Score</th>
                        <th style="text-align:right">New Score</th>
                        <th style="text-align:right">Score Delta</th>
                        <th style="text-align:right">Old Steps</th>
                        <th style="text-align:right">New Steps</th>
                        <th style="text-align:right">Steps Delta</th>
                    </tr>
                </thead>
                <tbody>{diff_rows}</tbody>
            </table>
        </div>
    </div>"""

    return f"""
<div class="section">
    <h2 class="section-title">Run Comparison</h2>
    {side_by_side}
    {delta_cards}
    {diff_table}
</div>"""


# ---------------------------------------------------------------------------
# Section: Step-by-step viewer (standalone, for episodes with step data)
# ---------------------------------------------------------------------------


def _section_step_viewer(episodes: list) -> str:
    """Build the step-by-step viewer for episodes with step data."""
    expanders = []

    for ep in episodes[:50]:  # Limit to 50 episodes for performance
        if not ep.steps:
            continue

        if ep.success:
            badge = '<span class="badge badge-pass">Pass</span>'
        else:
            badge = '<span class="badge badge-fail">Fail</span>'

        step_cards = []
        for step in ep.steps:
            action_label = step.action_type or "unknown"
            target_label = step.target or ""

            meta_parts = []
            if step.instruction:
                meta_parts.append(f"<strong>Instruction:</strong> {html.escape(step.instruction)}")
            if step.reasoning:
                reason_text = step.reasoning[:200]
                if len(step.reasoning) > 200:
                    reason_text += "..."
                meta_parts.append(f"<strong>Reasoning:</strong> {html.escape(reason_text)}")
            if step.decision:
                meta_parts.append(f"<strong>Decision:</strong> {html.escape(step.decision)}")

            meta_html = "<br>".join(meta_parts) if meta_parts else ""

            screenshot_html = ""
            data_uri = _embed_screenshot(step.screenshot_path)
            if data_uri:
                screenshot_html = f"""
                        <div class="step-screenshot">
                            <img src="{data_uri}"
                                 alt="Step {step.step_index}"
                                 onclick="openLightbox(this.src)"
                                 loading="lazy">
                        </div>"""

            step_cards.append(f"""
            <div class="step-card">
                <div class="step-number">{step.step_index}</div>
                <div class="step-details">
                    <div class="step-action">{html.escape(action_label)} {html.escape(target_label)}</div>
                    <div class="step-meta">{meta_html}</div>
                </div>
                {screenshot_html}
            </div>""")

        eid_display = ep.episode_id
        if len(eid_display) > 30:
            eid_display = eid_display[:28] + ".."

        expanders.append(f"""
        <div class="episode-expander" onclick="toggleExpander(this)">
            <div class="episode-expander-header">
                <span>
                    {badge}
                    <span style="margin-left:8px;font-family:monospace;font-size:0.8rem">{html.escape(eid_display)}</span>
                    <span style="color:var(--text-muted);margin-left:8px">{len(ep.steps)} steps</span>
                    <span style="color:var(--text-muted);margin-left:8px">Score: {ep.score:.2f}</span>
                </span>
                <span class="arrow">&#9654;</span>
            </div>
            <div class="episode-expander-body">
                {''.join(step_cards)}
            </div>
        </div>""")

    return f"""
<div class="section">
    <h2 class="section-title">Step-by-Step Viewer</h2>
    <div class="step-viewer">
        {''.join(expanders)}
    </div>
</div>"""
