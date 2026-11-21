"""The flight recorder's run directory: timeline, frames, ``flight.json``.

Layout of one run directory::

    flight.json        openadapt.flight-run/v1 summary (below)
    timeline.jsonl     one event per line: model calls, actions, gate decisions
    frames/<sha>.png   scrubbed frame thumbnails the events name
    timeline.html      a static page rendered from timeline.jsonl

Nothing here is sent anywhere. Every frame and every on-screen value is
scrubbed before it is written; when no scrubber is available the frame is
not written and the value is replaced by ``[withheld]`` (fail closed).

``flight.json`` keeps exactly the keys the run ledger collects::

    {schema, task_id, executor_label, started_at, ended_at,
     model: {calls, input_tokens, output_tokens, cost_usd | null, cost_source},
     actions_count, gate_events: [{at, kind, reason}], timeline_path}

plus one optional ``details`` object (scrub posture, cache tokens, the
command's exit code, the guard's result) that a reader may ignore.
"""

from __future__ import annotations

import hashlib
import html
import io
import json
import threading
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

__all__ = [
    "FLIGHT_SCHEMA",
    "GATE_KINDS",
    "WITHHELD",
    "Timeline",
    "validate_flight",
]

FLIGHT_SCHEMA = "openadapt.flight-run/v1"
GATE_KINDS = ("held", "allowed", "refused")
WITHHELD = "[withheld]"
#: Thumbnails are at most this wide (pixels); enough to read a form.
THUMB_MAX_WIDTH = 640
_COST_SOURCES = ("price_table", "unknown", "no_calls")

ImageScrub = Callable[[bytes], bytes]
TextScrub = Callable[[str], str]


def _iso(ts: Optional[float] = None) -> str:
    when = datetime.fromtimestamp(ts if ts is not None else time.time(), timezone.utc)
    return when.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _thumbnail(png: bytes) -> bytes:
    from PIL import Image

    image = Image.open(io.BytesIO(png))
    image.load()
    if image.width > THUMB_MAX_WIDTH:
        height = max(1, round(image.height * THUMB_MAX_WIDTH / image.width))
        image = image.resize((THUMB_MAX_WIDTH, height))
    out = io.BytesIO()
    image.convert("RGB").save(out, format="PNG")
    return out.getvalue()


class Timeline:
    """One flight run's recorder. Thread-safe; the proxy and gate share it."""

    def __init__(
        self,
        run_dir: Path | str,
        *,
        task_id: str,
        executor_label: str,
        scrub_image: Optional[ImageScrub] = None,
        scrub_text: Optional[TextScrub] = None,
        scrub_record: Optional[Mapping[str, Any]] = None,
    ) -> None:
        from openadapt_flow import at_rest

        self.run_dir = Path(run_dir)
        # The timeline and its frames can't be encrypted yet: named on
        # stderr and in the ledger, or refused under strict mode.
        at_rest.unsealed_write("the flight timeline and frames", self.run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        (self.run_dir / "frames").mkdir(exist_ok=True)
        self.task_id = task_id
        self.executor_label = executor_label
        self._scrub_image = scrub_image
        self._scrub_text = scrub_text
        self._scrub_record = dict(scrub_record or {})
        self._lock = threading.Lock()
        self._path = self.run_dir / "timeline.jsonl"
        self._path.touch()
        self.started_at = _iso()
        self._t0 = time.monotonic()
        self._frames: dict[str, str] = {}
        self.calls = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.cache_creation_input_tokens = 0
        self.cache_read_input_tokens = 0
        self._cost = 0.0
        self.unpriced_models: set[str] = set()
        self.model_actions = 0
        self.executor_actions = 0
        self.gate_events: list[dict[str, Any]] = []
        self.refused_requests: list[dict[str, Any]] = []

    # -- scrubbing for local writes -------------------------------------
    def local_text(self, text: Optional[str]) -> Optional[str]:
        """``text`` scrubbed for the timeline, or ``[withheld]`` (fail closed)."""
        if text is None or text == "":
            return text
        if self._scrub_text is None:
            return WITHHELD
        try:
            return self._scrub_text(text)
        except Exception:  # noqa: BLE001 - a failed scrub withholds the value
            return WITHHELD

    def frame(self, png: bytes, *, scrubbed: bool = False) -> Optional[str]:
        """Write a scrubbed thumbnail of ``png``; return its relative path.

        ``scrubbed`` says the bytes already went through the scrubber (the
        proxy's forwarded copy). Returns None, and writes nothing, when the
        frame can't be scrubbed or decoded.
        """
        if not png:
            return None
        key = hashlib.sha256(png).hexdigest()
        with self._lock:
            if key in self._frames:
                return self._frames[key]
        try:
            clean = png if scrubbed else None
            if clean is None:
                if self._scrub_image is None:
                    return None
                clean = self._scrub_image(png)
            thumb = _thumbnail(clean)
        except Exception:  # noqa: BLE001 - an unscrubbable frame is not written
            return None
        rel = f"frames/{hashlib.sha256(thumb).hexdigest()[:20]}.png"
        (self.run_dir / rel).write_bytes(thumb)
        with self._lock:
            self._frames[key] = rel
        return rel

    # -- events -----------------------------------------------------------
    def record(self, kind: str, **fields: Any) -> dict[str, Any]:
        event = {
            "at": _iso(),
            "t": round(time.monotonic() - self._t0, 3),
            "kind": kind,
            **fields,
        }
        line = json.dumps(event, sort_keys=True, default=str)
        with self._lock:
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        return event

    def model_call(
        self,
        *,
        provider: str,
        model: Optional[str],
        usage: Mapping[str, int],
        cost_usd: Optional[float],
        streamed: bool,
        status: int,
        seconds: float,
    ) -> None:
        with self._lock:
            self.calls += 1
            self.input_tokens += int(usage.get("input_tokens", 0))
            self.output_tokens += int(usage.get("output_tokens", 0))
            self.cache_creation_input_tokens += int(
                usage.get("cache_creation_input_tokens", 0)
            )
            self.cache_read_input_tokens += int(usage.get("cache_read_input_tokens", 0))
            if cost_usd is None:
                self.unpriced_models.add(model or "unknown")
            else:
                self._cost += cost_usd
        self.record(
            "model_call",
            provider=provider,
            model=model,
            usage=dict(usage),
            cost_usd=cost_usd,
            streamed=streamed,
            status=status,
            seconds=round(seconds, 3),
        )

    def refused_request(self, *, provider: str, status: int, reason: str) -> None:
        with self._lock:
            self.refused_requests.append(
                {"provider": provider, "status": status, "reason": reason}
            )
        self.record("request_refused", provider=provider, status=status, reason=reason)

    def model_action(
        self,
        *,
        provider: str,
        dialect: str,
        action: Mapping[str, Any],
        ops: list[dict[str, Any]],
        note: Optional[str] = None,
    ) -> None:
        with self._lock:
            self.model_actions += 1
        self.record(
            "model_action",
            provider=provider,
            dialect=dialect,
            action=self.scrub_action(action),
            ops=[self.scrub_action(op) for op in ops],
            note=note,
        )

    def executor_action(
        self,
        *,
        dialect: str,
        action: Mapping[str, Any],
        commit_class: bool,
        label: Optional[str],
        label_source: str,
        decision: str,
        frame_png: Optional[bytes] = None,
    ) -> None:
        with self._lock:
            self.executor_actions += 1
        frame = self.frame(frame_png) if frame_png else None
        self.record(
            "executor_action",
            dialect=dialect,
            action=self.scrub_action(action),
            commit_class=commit_class,
            label=self.local_text(label),
            label_source=label_source,
            decision=decision,
            frame=frame,
        )

    def gate_event(
        self,
        kind: str,
        reason: str,
        *,
        label: Optional[str] = None,
        frame_png: Optional[bytes] = None,
    ) -> dict[str, Any]:
        if kind not in GATE_KINDS:
            raise ValueError(f"gate event kind {kind!r}")
        entry = {"at": _iso(), "kind": kind, "reason": self.local_text(reason) or ""}
        with self._lock:
            self.gate_events.append(entry)
        frame = self.frame(frame_png) if frame_png else None
        self.record(
            "gate", decision=kind, reason=entry["reason"], label=self.local_text(label),
            frame=frame,
        )
        return entry

    def scrub_action(self, action: Mapping[str, Any]) -> dict[str, Any]:
        """A copy of an action with its typed text scrubbed for the timeline."""
        out: dict[str, Any] = {}
        for key, value in action.items():
            if key in ("text", "keys_text") and isinstance(value, str):
                out[key] = self.local_text(value)
            elif isinstance(value, (str, int, float, bool)) or value is None:
                out[key] = value
            elif isinstance(value, (list, tuple)):
                out[key] = [v if isinstance(v, (int, float, str)) else str(v) for v in value]
            else:
                out[key] = str(value)
        return out

    # -- summary ----------------------------------------------------------
    def total_cost(self) -> Optional[float]:
        with self._lock:
            return None if self.unpriced_models else self._cost

    def priced_cost(self) -> float:
        with self._lock:
            return self._cost

    def summary(
        self,
        *,
        ended_at: Optional[str] = None,
        details: Optional[Mapping[str, Any]] = None,
    ) -> dict[str, Any]:
        with self._lock:
            if self.calls == 0:
                cost: Optional[float] = 0.0
                source = "no_calls"
            elif self.unpriced_models:
                cost, source = None, "unknown"
            else:
                cost, source = round(self._cost, 6), "price_table"
            extra = {
                "scrub": dict(self._scrub_record),
                "cache_creation_input_tokens": self.cache_creation_input_tokens,
                "cache_read_input_tokens": self.cache_read_input_tokens,
                "priced_cost_usd": round(self._cost, 6),
                "unpriced_models": sorted(self.unpriced_models),
                "model_actions": self.model_actions,
                "executor_actions": self.executor_actions,
                "refused_requests": list(self.refused_requests),
            }
            summary = {
                "schema": FLIGHT_SCHEMA,
                "task_id": self.task_id,
                "executor_label": self.executor_label,
                "started_at": self.started_at,
                "ended_at": ended_at or _iso(),
                "model": {
                    "calls": self.calls,
                    "input_tokens": self.input_tokens,
                    "output_tokens": self.output_tokens,
                    "cost_usd": cost,
                    "cost_source": source,
                },
                "actions_count": max(self.model_actions, self.executor_actions),
                "gate_events": [dict(e) for e in self.gate_events],
                "timeline_path": "timeline.jsonl",
            }
        extra.update(details or {})
        summary["details"] = extra
        return summary

    def finish(self, *, details: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
        """Write ``flight.json`` and ``timeline.html``; return the summary."""
        summary = self.summary(details=details)
        problems = validate_flight(summary)
        if problems:  # pragma: no cover - a bug in this module
            raise ValueError("flight.json is invalid: " + "; ".join(problems))
        (self.run_dir / "flight.json").write_text(
            json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        render_html(self.run_dir)
        return summary


def validate_flight(data: Any) -> list[str]:
    """Problems with a ``flight.json`` value (empty when it is valid)."""
    problems: list[str] = []
    if not isinstance(data, Mapping):
        return ["not an object"]
    required = {
        "schema",
        "task_id",
        "executor_label",
        "started_at",
        "ended_at",
        "model",
        "actions_count",
        "gate_events",
        "timeline_path",
    }
    missing = required - set(data)
    if missing:
        problems.append(f"missing {sorted(missing)}")
    extra = set(data) - required - {"details"}
    if extra:
        problems.append(f"unexpected keys {sorted(extra)}")
    if data.get("schema") != FLIGHT_SCHEMA:
        problems.append("schema is not " + FLIGHT_SCHEMA)
    for key in ("task_id", "executor_label", "started_at", "ended_at", "timeline_path"):
        if key in data and not isinstance(data[key], str):
            problems.append(f"{key} is not a string")
    model = data.get("model")
    if not isinstance(model, Mapping):
        problems.append("model is not an object")
    else:
        keys = {"calls", "input_tokens", "output_tokens", "cost_usd", "cost_source"}
        if set(model) != keys:
            problems.append(f"model keys are {sorted(model)}")
        for key in ("calls", "input_tokens", "output_tokens"):
            value = model.get(key)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                problems.append(f"model.{key} is not a count")
        cost = model.get("cost_usd")
        if cost is not None and (isinstance(cost, bool) or not isinstance(cost, (int, float))):
            problems.append("model.cost_usd is not a number or null")
        if model.get("cost_source") not in _COST_SOURCES:
            problems.append("model.cost_source is not one of " + ", ".join(_COST_SOURCES))
        if model.get("cost_source") == "unknown" and cost is not None:
            problems.append("an unknown cost must be null")
    count = data.get("actions_count")
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        problems.append("actions_count is not a count")
    events = data.get("gate_events")
    if not isinstance(events, list):
        problems.append("gate_events is not a list")
    else:
        for i, event in enumerate(events):
            if not isinstance(event, Mapping) or set(event) != {"at", "kind", "reason"}:
                problems.append(f"gate_events[{i}] keys")
            elif event["kind"] not in GATE_KINDS:
                problems.append(f"gate_events[{i}].kind")
    details = data.get("details")
    if details is not None and not isinstance(details, Mapping):
        problems.append("details is not an object")
    return problems


# -- the static viewer ------------------------------------------------------

_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Flight timeline</title>
<style>
:root {{ --bg:#fff; --fg:#1b1b1f; --muted:#5f6368; --line:#e3e3e8;
  --held:#b3261e; --allowed:#1e6b35; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#141418; --fg:#ececf1;
  --muted:#a0a0aa; --line:#2c2c34; --held:#ff8a80; --allowed:#81c995; }} }}
body {{ margin:0; padding:16px; background:var(--bg); color:var(--fg);
  font:14px/1.45 system-ui, sans-serif; }}
h1 {{ font-size:20px; margin:0 0 4px; }}
p.meta {{ color:var(--muted); margin:0 0 16px; }}
table {{ border-collapse:collapse; width:100%; }}
td, th {{ border-top:1px solid var(--line); padding:6px 8px; text-align:left;
  vertical-align:top; }}
th {{ color:var(--muted); font-weight:500; }}
td.t {{ white-space:nowrap; color:var(--muted); }}
.held, .refused {{ color:var(--held); font-weight:600; }}
.allowed {{ color:var(--allowed); font-weight:600; }}
img {{ max-width:min(320px, 80vw); border:1px solid var(--line); }}
code {{ font-size:12px; word-break:break-word; }}
</style></head><body>
<h1>Flight timeline</h1>
<p class="meta">{meta}</p>
<table><thead><tr><th>Time</th><th>Event</th><th>Detail</th><th>Frame</th></tr></thead>
<tbody>
{rows}
</tbody></table>
</body></html>
"""


def _describe(event: Mapping[str, Any]) -> tuple[str, str]:
    kind = str(event.get("kind", ""))
    if kind == "model_call":
        usage = event.get("usage") or {}
        cost = event.get("cost_usd")
        cost_text = "unpriced" if cost is None else f"${cost:.4f}"
        return kind, (
            f"{event.get('provider')} {event.get('model')}: "
            f"{usage.get('input_tokens', 0)} in / {usage.get('output_tokens', 0)} out, "
            f"{cost_text}, HTTP {event.get('status')}"
        )
    if kind == "gate":
        return kind, f"{event.get('decision')}: {event.get('reason')} ({event.get('label')})"
    if kind in ("model_action", "executor_action"):
        detail = json.dumps(event.get("action"), sort_keys=True)
        if kind == "executor_action":
            detail += f"; label {event.get('label')!r}, {event.get('decision')}"
        return kind, detail
    rest = {k: v for k, v in event.items() if k not in ("at", "t", "kind")}
    return kind, json.dumps(rest, sort_keys=True, default=str)


def render_html(run_dir: Path | str) -> Path:
    """Render ``timeline.html`` from ``timeline.jsonl`` (and ``flight.json``)."""
    run_dir = Path(run_dir)
    rows: list[str] = []
    path = run_dir / "timeline.jsonl"
    lines = path.read_text("utf-8").splitlines() if path.exists() else []
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        kind, detail = _describe(event)
        css = ""
        if kind == "gate":
            css = str(event.get("decision", ""))
        elif kind == "executor_action" and event.get("decision") in GATE_KINDS:
            css = str(event.get("decision"))
        frame = event.get("frame")
        image = (
            f'<img loading="lazy" alt="scrubbed frame" src="{html.escape(frame)}">'
            if isinstance(frame, str) and frame.startswith("frames/")
            else ""
        )
        rows.append(
            f'<tr><td class="t">{html.escape(str(event.get("t", "")))}s</td>'
            f'<td class="{html.escape(css)}">{html.escape(kind)}</td>'
            f"<td><code>{html.escape(detail)}</code></td><td>{image}</td></tr>"
        )
    meta = ""
    summary_path = run_dir / "flight.json"
    if summary_path.exists():
        try:
            summary = json.loads(summary_path.read_text("utf-8"))
            model = summary.get("model", {})
            cost = model.get("cost_usd")
            meta = (
                f"Task {summary.get('task_id')} - {summary.get('executor_label')} - "
                f"{model.get('calls')} model calls, "
                f"{'cost unknown' if cost is None else f'${cost:.4f}'} - "
                f"{summary.get('actions_count')} actions - "
                f"{len(summary.get('gate_events', []))} gate events"
            )
        except (ValueError, AttributeError):
            meta = ""
    out = run_dir / "timeline.html"
    out.write_text(
        _PAGE.format(meta=html.escape(meta), rows="\n".join(rows)), encoding="utf-8"
    )
    return out
