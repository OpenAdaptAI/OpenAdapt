// The "watch it run" signature surface: a segmented compile/replay rail whose
// ticks fill L->R at step cadence, plus a mono live log revealed one line at a
// time (scanline). Colors come from tokens only (verified=--ok, running=
// --progress, halted=--attention, failed=--danger).
import type { RunStep, StepState } from "../lib/types";

function effectChip(step: RunStep) {
  if (step.effect === "verified")
    return <span className="chip verified">✓ verified</span>;
  if (step.effect === "not_verified")
    return <span className="chip notverified">✕ not verified</span>;
  if (step.effect === "checking" || step.state === "running")
    return <span className="chip checking">◐ checking…</span>;
  if (step.state === "halted")
    return <span className="chip halted">⚠ halted</span>;
  return <span className="chip" />;
}

export function ReplayMonitor({
  workflowName,
  steps,
  total,
}: {
  workflowName: string;
  steps: RunStep[];
  total: number;
}) {
  const done = steps.filter(
    (s) => s.state === "verified" || s.state === "failed",
  ).length;

  // Build the rail: one tick per known step, padded to `total`.
  const ticks: StepState[] = [];
  for (let i = 0; i < total; i++) {
    ticks.push(steps[i]?.state ?? "pending");
  }

  return (
    <div>
      <div className="monitor-head">
        <span>
          REPLAY ▸ {workflowName}
        </span>
        <span className="tnum">
          {done}/{total} steps
        </span>
      </div>

      <div className="rail-bar" aria-label="replay progress rail">
        {ticks.map((state, i) => (
          <div key={i} className={`rail-tick ${state}`} />
        ))}
      </div>

      <div className="log">
        {steps.length === 0 && (
          <div className="log-line">
            <span className="step">—</span>
            <span>waiting</span>
            <span className="target">no steps yet</span>
            <span />
            <span className="lat" />
          </div>
        )}
        {steps.map((s) => (
          <div className="log-line" key={s.index}>
            <span className="step">{s.index}</span>
            <span>{s.action}</span>
            <span className="target">{s.target}</span>
            {effectChip(s)}
            <span className="lat">
              {s.latency_ms != null ? `${s.latency_ms}ms` : ""}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}
