"""Comparison viewer for benchmark evaluation runs.

Generates a standalone HTML page that shows side-by-side comparisons
of two or more evaluation runs, with synchronized step replay,
click markers, action diffs, and behavioral analysis.

Usage:
    from openadapt_evals.benchmarks.comparison_viewer import generate_comparison_viewer
    from pathlib import Path

    generate_comparison_viewer(
        runs=[
            (Path("benchmark_results/zero_shot_run"), "Zero-Shot"),
            (Path("benchmark_results/demo_cond_run"), "Demo-Conditioned"),
        ],
        output_path=Path("comparison.html"),
    )
"""

from __future__ import annotations

import base64
import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _load_run_data(run_dir: Path) -> list[dict[str, Any]]:
    """Load all task data from a benchmark run directory.

    Returns list of dicts, one per task, with execution + task + screenshot paths.
    """
    tasks = []
    tasks_dir = run_dir / "tasks"
    if not tasks_dir.exists():
        return tasks

    for task_dir in sorted(tasks_dir.iterdir()):
        if not task_dir.is_dir():
            continue

        task_data: dict[str, Any] = {"task_id": task_dir.name}

        # Load task.json
        task_json = task_dir / "task.json"
        if task_json.exists():
            with open(task_json) as f:
                task_data["definition"] = json.load(f)

        # Load execution.json
        exec_json = task_dir / "execution.json"
        if exec_json.exists():
            with open(exec_json) as f:
                task_data["execution"] = json.load(f)

        # Collect screenshot relative paths
        screenshots_dir = task_dir / "screenshots"
        if screenshots_dir.exists():
            task_data["screenshot_paths"] = [
                str(p.relative_to(run_dir))
                for p in sorted(screenshots_dir.glob("step_*.png"))
            ]

        tasks.append(task_data)

    return tasks


def _match_tasks(
    runs: list[tuple[list[dict], str, Path]],
) -> dict[str, list[tuple[dict | None, str, Path]]]:
    """Match tasks across runs by task_id.

    Returns dict mapping task_id to list of (task_data, label, run_dir) tuples.
    """
    matched: dict[str, list[tuple[dict | None, str, Path]]] = {}

    for tasks, label, run_dir in runs:
        for task in tasks:
            tid = task["task_id"]
            if tid not in matched:
                matched[tid] = []
            matched[tid].append((task, label, run_dir))

    return matched


def generate_comparison_viewer(
    runs: list[tuple[Path, str]],
    output_path: Path | None = None,
    demo_prompts_dir: Path | None = None,
    embed_screenshots: bool = True,
) -> Path:
    """Generate an HTML comparison viewer for multiple benchmark runs.

    Args:
        runs: List of (run_directory, label) tuples.
        output_path: Where to write the HTML. Defaults to first run's parent.
        demo_prompts_dir: Optional dir containing demo prompt .txt files.
        embed_screenshots: Whether to embed screenshots as base64 data URIs
            (True = standalone HTML, False = relative file paths).

    Returns:
        Path to the generated HTML file.
    """
    if output_path is None:
        output_path = runs[0][0].parent / "comparison.html"

    # Load all run data
    loaded_runs = []
    for run_dir, label in runs:
        tasks = _load_run_data(run_dir)
        loaded_runs.append((tasks, label, run_dir))

    # Match tasks across runs
    matched = _match_tasks(loaded_runs)

    # Load demo prompts if available.
    # When both {id}.txt and {id}_multilevel.txt exist, prefer multilevel.
    demo_prompts: dict[str, str] = {}
    if demo_prompts_dir and demo_prompts_dir.exists():
        for f in sorted(demo_prompts_dir.glob("*.txt")):
            stem = f.stem
            # Strip _multilevel suffix for the lookup key so multilevel
            # demos replace plain ones (sorted order: plain before _multilevel).
            key = stem.removesuffix("_multilevel")
            with open(f) as fh:
                demo_prompts[key] = fh.read()

    # Build the data structure for JS
    comparison_data: list[dict[str, Any]] = []
    for task_id, task_entries in sorted(matched.items()):
        task_group: dict[str, Any] = {
            "task_id": task_id,
            "instruction": "",
            "runs": [],
        }

        for task_data, label, run_dir in task_entries:
            if task_data is None:
                continue

            defn = task_data.get("definition", {})
            execution = task_data.get("execution", {})

            if not task_group["instruction"] and defn.get("instruction"):
                task_group["instruction"] = defn["instruction"]

            # Build screenshot paths (base64 data URIs or relative file paths)
            screenshot_paths = []
            for sp in task_data.get("screenshot_paths", []):
                abs_path = Path(run_dir) / sp
                if embed_screenshots and abs_path.exists():
                    img_bytes = abs_path.read_bytes()
                    b64 = base64.b64encode(img_bytes).decode("ascii")
                    screenshot_paths.append(f"data:image/png;base64,{b64}")
                else:
                    try:
                        screenshot_paths.append(
                            str(abs_path.relative_to(output_path.parent))
                        )
                    except ValueError:
                        screenshot_paths.append(str(abs_path))

            # Extract actions
            actions = []
            for step in execution.get("steps", []):
                action = step.get("action", {})
                raw = action.get("raw_action", {})
                code = ""
                if isinstance(raw, dict):
                    code = raw.get("code", "") or raw.get("waa_action", "")
                actions.append({
                    "type": action.get("type", "?"),
                    "code": code,
                    "x": action.get("x"),
                    "y": action.get("y"),
                })

            # Check for demo in agent_logs
            has_demo = False
            for step in execution.get("steps", []):
                al = step.get("agent_logs") or {}
                if al.get("demo_included"):
                    has_demo = True
                    break

            # Find matching demo prompt by task_id prefix
            demo_text = ""
            for key, text in demo_prompts.items():
                if task_id.lower().startswith(key[:8].lower()) or key.lower().startswith(task_id[:8].lower()):
                    demo_text = text
                    break

            run_info = {
                "label": label,
                "score": execution.get("score", 0),
                "num_steps": execution.get("num_steps", 0),
                "total_time": execution.get("total_time_seconds", 0),
                "actions": actions,
                "screenshots": screenshot_paths,
                "has_demo": has_demo,
                "demo_text": demo_text,
                "viewer_path": str(run_dir / "viewer.html"),
            }
            task_group["runs"].append(run_info)

        if task_group["runs"]:
            comparison_data.append(task_group)

    data_json = json.dumps(comparison_data)

    # Generate HTML
    html = _build_comparison_html(data_json, [label for _, label in runs])

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        f.write(html)

    logger.info(f"Generated comparison viewer: {output_path}")
    return output_path


def _build_comparison_html(data_json: str, labels: list[str]) -> str:
    """Build the standalone HTML for the comparison viewer."""
    labels_json = json.dumps(labels)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Evaluation Comparison</title>
<style>
:root {{
  --bg: #0d1117; --bg2: #161b22; --bg3: #21262d;
  --fg: #c9d1d9; --fg2: #8b949e; --accent: #58a6ff;
  --green: #3fb950; --red: #f85149; --yellow: #d29922; --purple: #a371f7;
  --border: #30363d;
}}
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: var(--bg); color: var(--fg); }}
.header {{ background: var(--bg2); border-bottom: 1px solid var(--border); padding: 20px 32px; }}
.header h1 {{ font-size: 22px; font-weight: 600; }}
.header .sub {{ color: var(--fg2); margin-top: 4px; font-size: 13px; }}
.tabs {{ display: flex; gap: 0; padding: 0 32px; background: var(--bg2); border-bottom: 1px solid var(--border); }}
.tab {{ padding: 10px 20px; cursor: pointer; color: var(--fg2); border-bottom: 2px solid transparent; font-size: 13px; font-weight: 500; }}
.tab:hover {{ color: var(--fg); background: var(--bg3); }}
.tab.active {{ color: var(--accent); border-bottom-color: var(--accent); }}
.panel {{ display: none; padding: 24px 32px; }}
.panel.active {{ display: block; }}
.task-header {{ margin-bottom: 16px; }}
.task-header h2 {{ font-size: 18px; margin-bottom: 6px; }}
.task-header .instr {{ color: var(--fg2); font-style: italic; padding: 10px 14px; background: var(--bg2); border-radius: 6px; border-left: 3px solid var(--accent); font-size: 13px; }}
.side-by-side {{ display: grid; grid-template-columns: repeat({len(labels)}, 1fr); gap: 20px; margin-top: 16px; }}
.run-col {{ background: var(--bg2); border: 1px solid var(--border); border-radius: 8px; overflow: hidden; }}
.run-col .col-hdr {{ padding: 10px 14px; font-weight: 600; font-size: 13px; border-bottom: 1px solid var(--border); display: flex; justify-content: space-between; align-items: center; }}
.run-col .col-hdr .badge {{ font-size: 11px; padding: 2px 8px; border-radius: 12px; font-weight: 600; }}
.badge.fail {{ background: rgba(248,81,73,0.2); color: var(--red); }}
.badge.pass {{ background: rgba(63,185,80,0.2); color: var(--green); }}
.ss-area {{ padding: 12px; position: relative; }}
.ss-area img {{ width: 100%; border-radius: 4px; border: 1px solid var(--border); display: block; }}
.click-dot {{ position: absolute; width: 16px; height: 16px; border-radius: 50%; border: 2px solid var(--red); background: rgba(248,81,73,0.3); transform: translate(-50%, -50%); pointer-events: none; z-index: 10; }}
.action-line {{ font-size: 11px; font-family: 'SFMono-Regular', Consolas, monospace; padding: 6px 14px; border-top: 1px solid var(--border); color: var(--fg2); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
.atype {{ font-weight: 700; display: inline-block; min-width: 40px; }}
.atype.click {{ color: var(--accent); }} .atype.type {{ color: var(--green); }}
.atype.key {{ color: var(--yellow); }} .atype.done {{ color: var(--purple); }}
.stats-row {{ display: flex; gap: 14px; padding: 6px 14px; border-top: 1px solid var(--border); font-size: 11px; color: var(--fg2); }}
.action-log {{ max-height: 260px; overflow-y: auto; border-top: 1px solid var(--border); }}
.log-row {{ padding: 3px 14px; font-size: 10px; font-family: monospace; border-bottom: 1px solid rgba(48,54,61,0.4); display: flex; gap: 6px; }}
.log-row:hover {{ background: var(--bg3); }}
.log-row .sn {{ color: var(--fg2); min-width: 20px; }}
.playback {{ padding: 14px 32px; background: var(--bg2); border-top: 1px solid var(--border); position: sticky; bottom: 0; display: flex; align-items: center; gap: 14px; z-index: 100; }}
.playback input[type=range] {{ flex: 1; accent-color: var(--accent); }}
.play-btn {{ background: var(--accent); color: #fff; border: none; padding: 7px 18px; border-radius: 6px; cursor: pointer; font-size: 13px; font-weight: 600; min-width: 70px; }}
.play-btn:hover {{ opacity: 0.9; }}
.links {{ margin-top: 12px; display: flex; gap: 10px; }}
.links a {{ color: var(--accent); text-decoration: none; font-size: 12px; padding: 4px 10px; border: 1px solid var(--border); border-radius: 6px; }}
.links a:hover {{ background: var(--bg3); }}
/* Analysis */
table {{ width: 100%; border-collapse: collapse; font-size: 12px; }}
th {{ text-align: left; padding: 8px; border-bottom: 1px solid var(--border); color: var(--fg2); font-weight: 600; }}
td {{ padding: 8px; border-bottom: 1px solid var(--border); }}
.chart-bar {{ height: 18px; border-radius: 3px; display: inline-block; min-width: 2px; }}
.demo-box {{ background: var(--bg2); border: 1px solid var(--border); border-radius: 8px; padding: 14px; font-size: 12px; line-height: 1.6; white-space: pre-wrap; max-height: 350px; overflow-y: auto; margin-top: 10px; }}
.diverge {{ background: rgba(248,81,73,0.08); border-left: 3px solid var(--red); padding: 8px 12px; margin: 12px 0; font-size: 12px; border-radius: 4px; }}
.heatmap-area {{ position: relative; }}
.heatmap-area canvas {{ position: absolute; top: 0; left: 0; width: 100%; height: 100%; pointer-events: none; border-radius: 4px; }}
@media (max-width: 900px) {{ .side-by-side {{ grid-template-columns: 1fr; }} }}
</style>
</head>
<body>
<div class="header">
  <h1>Evaluation Comparison</h1>
  <div class="sub" id="subtitle"></div>
</div>
<div class="tabs" id="tabs"></div>
<div id="panels"></div>
<div class="playback">
  <button class="play-btn" id="playBtn" onclick="togglePlay()">&#9654; Play</button>
  <input type="range" id="slider" min="0" max="19" value="0" oninput="setStep(+this.value)">
  <span id="stepLbl" style="color:var(--fg2);font-size:12px;min-width:70px;text-align:right;">0 / 0</span>
</div>
<script>
const DATA = {data_json};
const LABELS = {labels_json};
let curTask = 0, curStep = 0, playing = false, playTimer = null;

function esc(s) {{ const d = document.createElement('div'); d.textContent = s; return d.innerHTML; }}

function init() {{
  const tabsEl = document.getElementById('tabs');
  const panelsEl = document.getElementById('panels');
  const sub = document.getElementById('subtitle');
  sub.textContent = LABELS.join(' vs ') + ' \\u00b7 ' + DATA.length + ' tasks';

  DATA.forEach((tg, i) => {{
    // Tab
    const tab = document.createElement('div');
    tab.className = 'tab' + (i === 0 ? ' active' : '');
    tab.textContent = tg.task_id.substring(0, 8) + '...';
    tab.title = tg.instruction || tg.task_id;
    tab.onclick = () => showTask(i);
    tabsEl.appendChild(tab);

    // Panel
    const panel = document.createElement('div');
    panel.className = 'panel' + (i === 0 ? ' active' : '');
    panel.dataset.idx = i;

    let h = '<div class="task-header">';
    h += '<h2>' + esc(tg.task_id) + '</h2>';
    if (tg.instruction) h += '<div class="instr">' + esc(tg.instruction) + '</div>';
    h += '<div class="links">';
    tg.runs.forEach(r => {{ h += '<a href="' + r.viewer_path + '" target="_blank">' + esc(r.label) + ' Viewer</a>'; }});
    h += '</div></div>';

    h += '<div class="side-by-side">';
    tg.runs.forEach((r, ri) => {{
      const key = i + '_' + ri;
      h += '<div class="run-col">';
      h += '<div class="col-hdr"><span>' + esc(r.label) + (r.has_demo ? ' (demo)' : '') + '</span>';
      h += '<span class="badge ' + (r.score > 0 ? 'pass' : 'fail') + '">Score: ' + r.score.toFixed(2) + '</span></div>';
      h += '<div class="ss-area heatmap-area" id="ss_' + key + '"><img id="img_' + key + '" src="' + (r.screenshots[0] || '') + '">';
      h += '<div class="click-dot" id="dot_' + key + '" style="display:none;"></div>';
      h += '<canvas id="heat_' + key + '" style="display:none;"></canvas></div>';
      h += '<div class="action-line" id="act_' + key + '">--</div>';
      h += '<div class="stats-row"><span id="stp_' + key + '">0/0</span><span>' + r.num_steps + ' steps</span><span>' + Math.round(r.total_time) + 's</span></div>';
      h += '<div class="action-log">';
      r.actions.forEach((a, ai) => {{
        h += '<div class="log-row"><span class="sn">' + ai + '</span>';
        h += '<span class="atype ' + a.type + '">' + a.type.toUpperCase() + '</span>';
        h += '<span>' + esc(a.code) + '</span></div>';
      }});
      h += '</div>';

      // Demo text
      if (r.demo_text) {{
        h += '<details style="padding:8px 14px;border-top:1px solid var(--border);"><summary style="font-size:11px;color:var(--fg2);cursor:pointer;">Demo Prompt (' + r.demo_text.length + ' chars)</summary>';
        h += '<div class="demo-box">' + esc(r.demo_text) + '</div></details>';
      }}
      h += '</div>';
    }});
    h += '</div>';

    // Divergence analysis
    if (tg.runs.length >= 2) {{
      const a0 = tg.runs[0].actions;
      const a1 = tg.runs[1].actions;
      let firstDiff = -1;
      for (let s = 0; s < Math.min(a0.length, a1.length); s++) {{
        if (a0[s].type !== a1[s].type || a0[s].code !== a1[s].code) {{ firstDiff = s; break; }}
      }}
      if (firstDiff === -1 && a0.length !== a1.length) firstDiff = Math.min(a0.length, a1.length);
      if (firstDiff >= 0) {{
        h += '<div class="diverge">First divergence at <strong>step ' + firstDiff + '</strong>';
        if (firstDiff < a0.length) h += ': ' + esc(tg.runs[0].label) + ' → <code>' + esc(a0[firstDiff].type + ' ' + a0[firstDiff].code) + '</code>';
        if (firstDiff < a1.length) h += ' vs ' + esc(tg.runs[1].label) + ' → <code>' + esc(a1[firstDiff].type + ' ' + a1[firstDiff].code) + '</code>';
        h += '</div>';
      }}

      // Action type distribution bars
      h += '<div style="margin-top:16px;"><h3 style="font-size:14px;margin-bottom:10px;">Action Type Distribution</h3>';
      h += '<table><tr><th>Condition</th><th>Clicks</th><th>Types</th><th>Keys</th><th>Done</th><th>Total</th></tr>';
      tg.runs.forEach(r => {{
        const counts = {{click:0, type:0, key:0, done:0}};
        r.actions.forEach(a => {{ counts[a.type] = (counts[a.type] || 0) + 1; }});
        const total = r.actions.length || 1;
        h += '<tr>';
        h += '<td style="font-weight:600;">' + esc(r.label) + '</td>';
        ['click','type','key','done'].forEach(t => {{
          const pct = (counts[t]/total*100).toFixed(0);
          const colors = {{click:'var(--accent)',type:'var(--green)',key:'var(--yellow)',done:'var(--purple)'}};
          h += '<td>' + counts[t] + ' <span class="chart-bar" style="width:' + pct + 'px;background:' + colors[t] + ';"></span></td>';
        }});
        h += '<td>' + r.actions.length + '</td></tr>';
      }});
      h += '</table></div>';
    }}

    panel.innerHTML = h;
    panelsEl.appendChild(panel);
  }});

  // Add analysis tab
  const analysisTab = document.createElement('div');
  analysisTab.className = 'tab';
  analysisTab.textContent = 'Analysis';
  analysisTab.onclick = () => showTask(-1);
  tabsEl.appendChild(analysisTab);

  const analysisPanel = document.createElement('div');
  analysisPanel.className = 'panel';
  analysisPanel.dataset.idx = -1;
  analysisPanel.innerHTML = buildAnalysis();
  panelsEl.appendChild(analysisPanel);

  updateStep();
}}

function buildAnalysis() {{
  let h = '<h2 style="margin-bottom:16px;">Cross-Task Analysis</h2>';
  h += '<table><tr><th>Task</th><th>Condition</th><th>Score</th><th>Steps</th><th>Time</th><th>Clicks</th><th>Types</th><th>Keys</th><th>Done</th></tr>';
  DATA.forEach(tg => {{
    tg.runs.forEach(r => {{
      const counts = {{click:0,type:0,key:0,done:0}};
      r.actions.forEach(a => {{ counts[a.type] = (counts[a.type] || 0) + 1; }});
      h += '<tr><td>' + tg.task_id.substring(0,8) + '...</td>';
      h += '<td style="font-weight:600;">' + esc(r.label) + '</td>';
      h += '<td style="color:' + (r.score > 0 ? 'var(--green)' : 'var(--red)') + ';">' + r.score.toFixed(2) + '</td>';
      h += '<td>' + r.num_steps + '</td><td>' + Math.round(r.total_time) + 's</td>';
      h += '<td style="color:var(--accent);">' + counts.click + '</td>';
      h += '<td style="color:var(--green);">' + counts.type + '</td>';
      h += '<td style="color:var(--yellow);">' + counts.key + '</td>';
      h += '<td style="color:var(--purple);">' + counts.done + '</td></tr>';
    }});
  }});
  h += '</table>';
  return h;
}}

function showTask(idx) {{
  curTask = idx; curStep = 0;
  document.querySelectorAll('.tab').forEach((t, i) => t.classList.toggle('active', i === idx || (idx === -1 && i === DATA.length)));
  document.querySelectorAll('.panel').forEach(p => p.classList.toggle('active', +p.dataset.idx === idx));
  document.querySelector('.playback').style.display = idx >= 0 ? 'flex' : 'none';
  if (idx >= 0) updateStep();
}}

function getMaxSteps() {{
  if (curTask < 0 || curTask >= DATA.length) return 0;
  return Math.max(...DATA[curTask].runs.map(r => r.screenshots.length)) - 1;
}}

function setStep(s) {{ curStep = Math.max(0, Math.min(s, getMaxSteps())); updateStep(); }}

function updateStep() {{
  if (curTask < 0 || curTask >= DATA.length) return;
  const tg = DATA[curTask];
  const mx = getMaxSteps();
  document.getElementById('slider').max = mx;
  document.getElementById('slider').value = curStep;
  document.getElementById('stepLbl').textContent = curStep + ' / ' + mx;

  tg.runs.forEach((r, ri) => {{
    const key = curTask + '_' + ri;
    const img = document.getElementById('img_' + key);
    const dot = document.getElementById('dot_' + key);
    const act = document.getElementById('act_' + key);
    const stp = document.getElementById('stp_' + key);

    if (stp) stp.textContent = curStep + '/' + (r.screenshots.length - 1);

    if (act && curStep < r.actions.length) {{
      const a = r.actions[curStep];
      act.innerHTML = '<span class="atype ' + a.type + '">' + a.type.toUpperCase() + '</span> ' + esc(a.code);
    }} else if (act) {{ act.innerHTML = '--'; }}

    // Position click dot after image loads to ensure correct dimensions
    const placeDot = () => {{
      if (dot && curStep < r.actions.length) {{
        const a = r.actions[curStep];
        const imgEl = document.getElementById('img_' + key);
        const ssEl = document.getElementById('ss_' + key);
        if (a.x != null && a.y != null && a.type === 'click' && imgEl && ssEl) {{
          let nx = a.x, ny = a.y;
          const code = a.code || '';
          const cm = code.match(/computer\\.(?:click|double_click|right_click)\\((\\d+),\\s*(\\d+)\\)/);
          if (cm && imgEl.naturalWidth > 0) {{
            const rawX = parseInt(cm[1]), rawY = parseInt(cm[2]);
            const tol = 5;
            if (Math.abs(a.x * imgEl.naturalWidth - rawX) > tol || Math.abs(a.y * imgEl.naturalHeight - rawY) > tol) {{
              nx = rawX / imgEl.naturalWidth;
              ny = rawY / imgEl.naturalHeight;
            }}
          }}
          const imgR = imgEl.getBoundingClientRect();
          const ssR = ssEl.getBoundingClientRect();
          if (imgR.width > 0) {{
            dot.style.display = 'block';
            dot.style.left = (imgR.left - ssR.left + nx * imgR.width) + 'px';
            dot.style.top = (imgR.top - ssR.top + ny * imgR.height) + 'px';
          }} else {{ dot.style.display = 'none'; }}
        }} else {{ dot.style.display = 'none'; }}
      }} else if (dot) {{ dot.style.display = 'none'; }}
    }};

    if (img && curStep < r.screenshots.length) {{
      img.onload = placeDot;
      img.src = r.screenshots[curStep];
      if (img.complete && img.naturalWidth > 0) placeDot();
    }}
  }});
}}

function togglePlay() {{
  playing = !playing;
  const btn = document.getElementById('playBtn');
  if (playing) {{
    btn.innerHTML = '&#10074;&#10074; Pause';
    playTimer = setInterval(() => {{ if (curStep >= getMaxSteps()) {{ togglePlay(); return; }} setStep(curStep+1); }}, 1200);
  }} else {{ btn.innerHTML = '&#9654; Play'; clearInterval(playTimer); }}
}}

document.addEventListener('keydown', e => {{
  if (e.key === 'ArrowRight') {{ e.preventDefault(); setStep(curStep+1); }}
  if (e.key === 'ArrowLeft') {{ e.preventDefault(); setStep(curStep-1); }}
  if (e.key === ' ') {{ e.preventDefault(); togglePlay(); }}
  if (e.key === 'Home') {{ e.preventDefault(); setStep(0); }}
  if (e.key === 'End') {{ e.preventDefault(); setStep(getMaxSteps()); }}
}});

init();
</script>
</body>
</html>"""
