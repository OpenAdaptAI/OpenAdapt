"""Example: Training dashboard using component library.

This shows how openadapt-ml can use the component library to
display training progress and evaluation results.

Usage:
    python -m openadapt_viewer.examples.training_example
"""

from __future__ import annotations

from pathlib import Path

from openadapt_viewer.builders import PageBuilder
from openadapt_viewer.components import (
    metrics_grid,
    screenshot_display,
)


def generate_training_dashboard(
    model_name: str = "Qwen3-VL-2B",
    capture_id: str = "turn-off-nightshift",
    epoch: int = 3,
    losses: list[dict] | None = None,
    evaluations: list[dict] | None = None,
    output_path: str | Path = "training_dashboard.html",
) -> Path:
    """Generate a training dashboard.

    Args:
        model_name: Name of the model being trained
        capture_id: ID of the capture being used
        epoch: Current epoch number
        losses: List of loss records [{epoch, step, loss, lr}]
        evaluations: List of evaluation samples
        output_path: Output HTML file path

    Returns:
        Path to generated HTML file
    """
    # Sample data if not provided
    if losses is None:
        losses = _generate_sample_losses()
    if evaluations is None:
        evaluations = _generate_sample_evaluations()

    # Calculate stats
    min_loss = min(loss["loss"] for loss in losses) if losses else 0
    avg_loss = sum(loss["loss"] for loss in losses) / len(losses) if losses else 0
    total_steps = len(losses)

    # Build page
    builder = PageBuilder(
        title=f"Training Dashboard - {capture_id}",
        include_alpine=True,
        include_chartjs=True,
    )

    # Header
    builder.add_header(
        title="Training Dashboard",
        subtitle=f"Model: {model_name} | Capture: {capture_id}",
        nav_tabs=[
            {"href": "dashboard.html", "label": "Training", "active": True},
            {"href": "viewer.html", "label": "Viewer"},
            {"href": "benchmark.html", "label": "Benchmarks"},
        ],
    )

    # Training metrics
    builder.add_section(
        metrics_grid([
            {"label": "Current Epoch", "value": epoch},
            {"label": "Total Steps", "value": total_steps},
            {"label": "Min Loss", "value": f"{min_loss:.4f}", "color": "success"},
            {"label": "Avg Loss", "value": f"{avg_loss:.4f}"},
        ]),
        title="Training Progress",
    )

    # Loss chart placeholder
    import json
    losses_json = json.dumps(losses)

    builder.add_section(f'''
        <div style="background: var(--oa-bg-secondary); border-radius: 12px; padding: 24px;">
            <h3 style="margin: 0 0 16px 0; font-size: 1rem;">Loss Curve</h3>
            <canvas id="lossChart" height="200"></canvas>
        </div>
        <script>
            document.addEventListener('DOMContentLoaded', function() {{
                const losses = {losses_json};
                const ctx = document.getElementById('lossChart').getContext('2d');
                new Chart(ctx, {{
                    type: 'line',
                    data: {{
                        labels: losses.map((_, i) => i + 1),
                        datasets: [{{
                            label: 'Loss',
                            data: losses.map(l => l.loss),
                            borderColor: '#00d4aa',
                            backgroundColor: 'rgba(0, 212, 170, 0.1)',
                            fill: true,
                            tension: 0.4,
                        }}]
                    }},
                    options: {{
                        responsive: true,
                        plugins: {{
                            legend: {{ display: false }}
                        }},
                        scales: {{
                            x: {{
                                title: {{ display: true, text: 'Step', color: '#888' }},
                                grid: {{ color: 'rgba(255,255,255,0.05)' }},
                                ticks: {{ color: '#888' }}
                            }},
                            y: {{
                                title: {{ display: true, text: 'Loss', color: '#888' }},
                                grid: {{ color: 'rgba(255,255,255,0.05)' }},
                                ticks: {{ color: '#888' }}
                            }}
                        }}
                    }}
                }});
            }});
        </script>
    ''', title="Loss Curve")

    # Evaluation samples
    eval_html_parts = []
    for eval_sample in evaluations[:6]:  # Show first 6
        correct = eval_sample.get("correct", False)
        badge_html = '<span class="oa-badge oa-badge-success">Correct</span>' if correct else '<span class="oa-badge oa-badge-error">Incorrect</span>'

        eval_html_parts.append(f'''
            <div style="background: var(--oa-bg-secondary); border-radius: 8px; padding: 12px;">
                {screenshot_display(
                    image_path=eval_sample.get("image_path"),
                    width=200,
                    height=120,
                    overlays=[
                        {"type": "click", "x": eval_sample.get("human_x", 0.5), "y": eval_sample.get("human_y", 0.5), "label": "H", "variant": "human"},
                        {"type": "click", "x": eval_sample.get("pred_x", 0.5), "y": eval_sample.get("pred_y", 0.5), "label": "AI", "variant": "predicted"},
                    ] if eval_sample.get("human_x") else None
                )}
                <div style="margin-top: 8px; display: flex; justify-content: space-between; align-items: center;">
                    <span style="font-size: 0.75rem; color: var(--oa-text-muted);">Epoch {eval_sample.get("epoch", 1)}</span>
                    {badge_html}
                </div>
            </div>
        ''')

    builder.add_section(f'''
        <div style="display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px;">
            {"".join(eval_html_parts)}
        </div>
    ''', title="Evaluation Samples")

    return builder.render_to_file(output_path)


def _generate_sample_losses() -> list[dict]:
    """Generate sample loss data."""
    import math

    losses = []
    for i in range(100):
        # Simulate decreasing loss with noise
        base_loss = 2.0 * math.exp(-i / 30) + 0.05
        noise = 0.1 * (0.5 - (i % 7) / 7)
        losses.append({
            "epoch": i // 25,
            "step": i,
            "loss": max(0.01, base_loss + noise),
            "lr": 0.0002 * (1 - i / 100),
        })

    return losses


def _generate_sample_evaluations() -> list[dict]:
    """Generate sample evaluation data."""
    import random

    evaluations = []
    for i in range(12):
        hx, hy = random.uniform(0.2, 0.8), random.uniform(0.2, 0.8)
        # Predicted with some error
        px = hx + random.uniform(-0.1, 0.1)
        py = hy + random.uniform(-0.1, 0.1)
        dist = ((hx - px) ** 2 + (hy - py) ** 2) ** 0.5

        evaluations.append({
            "epoch": i // 3 + 1,
            "sample_idx": i,
            "image_path": None,  # Would be real path in production
            "human_x": hx,
            "human_y": hy,
            "pred_x": px,
            "pred_y": py,
            "correct": dist < 0.05,
        })

    return evaluations


if __name__ == "__main__":
    output = generate_training_dashboard()
    print(f"Generated: {output}")
