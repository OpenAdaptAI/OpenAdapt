// The "watch it run" signature surface: a segmented compile/replay rail whose
// ticks fill L->R at step cadence, plus a selectable step log and an on-device
// Before/After step frame preview. All frames stay local (inert data:image/*
// URIs only, never uploaded or fetched over the network).
import { useEffect, useRef, useState } from "react";
import type { RunStep, StepFrame, StepState } from "../lib/types";

type FrameMode = "split" | "before" | "after";

const LOCAL_DATA_IMAGE_RE = /^data:image\/(?:png|jpeg|webp|svg\+xml);base64,[A-Za-z0-9+/=]+$/i;

function normalizeFrame(input: string | StepFrame | null | undefined): StepFrame | null {
  if (!input) return null;
  const raw = typeof input === "string" ? { data_url: input } : input;
  if (typeof raw.data_url !== "string" || !LOCAL_DATA_IMAGE_RE.test(raw.data_url.trim())) {
    return null;
  }
  return {
    data_url: raw.data_url.trim(),
    width: raw.width,
    height: raw.height,
    focus: raw.focus ?? null,
    captured_at: raw.captured_at ?? null,
  };
}

function stepHasFrames(step: RunStep): boolean {
  return Boolean(
    normalizeFrame(step.before_screenshot) || normalizeFrame(step.after_screenshot),
  );
}

function defaultSelectedIndex(steps: RunStep[]): number | null {
  if (!steps.length) return null;
  const withFrames = steps.filter(stepHasFrames);
  if (!withFrames.length) return null;
  const priority =
    withFrames.find((s) => s.state === "halted" || s.state === "failed") ??
    withFrames.find((s) => s.effect === "verified" || s.effect === "not_verified") ??
    withFrames[withFrames.length - 1];
  return priority ? priority.index : null;
}

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

function FrameCard({
  label,
  frame,
  step,
  blurred,
}: {
  label: "Before" | "After";
  frame: StepFrame | null;
  step: RunStep;
  blurred: boolean;
}) {
  const validDims = Boolean(frame && frame.width && frame.width > 0 && frame.height && frame.height > 0);
  const focus = frame?.focus;
  const hasFocus = Boolean(
    focus &&
      validDims &&
      frame?.width &&
      frame?.height &&
      focus.x >= 0 &&
      focus.y >= 0 &&
      focus.x <= frame.width &&
      focus.y <= frame.height,
  );
  const fxPct = hasFocus && focus && frame?.width ? (focus.x / frame.width) * 100 : 0;
  const fyPct = hasFocus && focus && frame?.height ? (focus.y / frame.height) * 100 : 0;

  return (
    <figure className="step-frame-card">
      <div className="step-frame-card-head">
        <span className="step-frame-label">{label}</span>
        {label === "Before" && (
          <span className="step-frame-target mono">
            {step.action} &apos;{step.target}&apos;
          </span>
        )}
        {label === "After" && step.effect === "verified" && (
          <span className="step-frame-verdict verified">read-back matched</span>
        )}
        {label === "After" && step.effect === "not_verified" && (
          <span className="step-frame-verdict halted">not verified</span>
        )}
      </div>
      <div className="step-frame-viewport">
        {frame ? (
          <>
            <img
              src={frame.data_url}
              alt={`${label} frame for step ${step.index + 1}: ${step.action} ${step.target}`}
              className={blurred ? "step-frame-img frame-img-blurred" : "step-frame-img"}
            />
            {hasFocus && (
              <span
                className="step-frame-focus"
                aria-hidden="true"
                style={{ left: `${fxPct}%`, top: `${fyPct}%` }}
              >
                <span className="step-frame-focus-callout">
                  {step.action} &apos;{step.target}&apos;
                </span>
              </span>
            )}
          </>
        ) : (
          <div className="step-frame-missing">
            No {label.toLowerCase()} frame captured
          </div>
        )}
      </div>
    </figure>
  );
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
  const [selectedIndex, setSelectedIndex] = useState<number | null>(() =>
    defaultSelectedIndex(steps),
  );
  const [frameMode, setFrameMode] = useState<FrameMode>("split");
  const [blurSensitive, setBlurSensitive] = useState(false);
  const rowRefs = useRef<Record<number, HTMLButtonElement | null>>({});

  useEffect(() => {
    setSelectedIndex((current) => {
      if (current != null && steps.some((s) => s.index === current)) {
        return current;
      }
      return defaultSelectedIndex(steps);
    });
  }, [steps]);

  const done = steps.filter(
    (s) => s.state === "verified" || s.state === "failed",
  ).length;

  // Build the rail: one tick per known step, padded to `total`.
  const ticks: StepState[] = [];
  for (let i = 0; i < total; i++) {
    ticks.push(steps[i]?.state ?? "pending");
  }

  const selectedStep =
    selectedIndex != null
      ? steps.find((s) => s.index === selectedIndex) ?? null
      : null;
  const beforeFrame = selectedStep ? normalizeFrame(selectedStep.before_screenshot) : null;
  const afterFrame = selectedStep ? normalizeFrame(selectedStep.after_screenshot) : null;
  const hasAnyFrame = Boolean(beforeFrame || afterFrame);

  function moveSelection(pos: number, delta: number) {
    if (!steps.length) return;
    const nextPos = Math.max(0, Math.min(steps.length - 1, pos + delta));
    const nextStep = steps[nextPos];
    if (!nextStep) return;
    setSelectedIndex(nextStep.index);
    rowRefs.current[nextStep.index]?.focus();
  }

  return (
    <div className="replay-monitor">
      <div className="monitor-head">
        <div className="monitor-head-title">
          <strong>{workflowName}</strong>
          <div className="monitor-provenance">
            <span className="prov-chip">compiled-plan</span>
            <span className="prov-chip">deterministic</span>
            <span className="prov-chip prov-chip-accent">Veto-4B-r6</span>
          </div>
        </div>
        <span className="monitor-count tnum">
          {done}/{total} steps
        </span>
      </div>

      <div className="rail-bar" aria-label="replay progress rail">
        {ticks.map((state, i) => (
          <div key={i} className={`rail-tick ${state}`} />
        ))}
      </div>

      <div className="log" role="list" aria-label="Replay steps">
        {steps.length === 0 && (
          <div className="log-line log-line-empty">
            <span className="step">—</span>
            <span className="action-tag">waiting</span>
            <span className="target">no steps yet</span>
            <span />
            <span className="lat" />
          </div>
        )}
        {steps.map((s, pos) => {
          const isSelected = selectedStep?.index === s.index;
          const hasFrames = stepHasFrames(s);
          return (
            <div key={s.index} role="listitem" className="log-row">
              <button
                ref={(node) => {
                  rowRefs.current[s.index] = node;
                }}
                type="button"
                aria-pressed={isSelected}
                className={[
                  "log-line",
                  "log-line-btn",
                  `log-line-${s.state}`,
                  isSelected ? "log-line-selected" : "",
                ]
                  .filter(Boolean)
                  .join(" ")}
                onClick={() =>
                  setSelectedIndex((curr) => (curr === s.index ? null : s.index))
                }
                onKeyDown={(event) => {
                  if (event.key === "ArrowDown") {
                    event.preventDefault();
                    moveSelection(pos, 1);
                  } else if (event.key === "ArrowUp") {
                    event.preventDefault();
                    moveSelection(pos, -1);
                  } else if (event.key === "Home") {
                    event.preventDefault();
                    moveSelection(0, 0);
                  } else if (event.key === "End") {
                    event.preventDefault();
                    moveSelection(steps.length - 1, 0);
                  }
                }}
              >
                <span className="step tnum">{String(s.index + 1).padStart(2, "0")}</span>
                <span className="action-tag">{s.action}</span>
                <span className="target">
                  {s.target}
                  {hasFrames && (
                    <span className="frame-dot" title="Local Before/After frames available" aria-hidden="true">
                      ◫
                    </span>
                  )}
                </span>
                {effectChip(s)}
                <span className="lat tnum">
                  {s.latency_ms != null ? `${s.latency_ms} ms` : "—"}
                </span>
              </button>
            </div>
          );
        })}
      </div>

      {selectedStep && (
        <section
          className="step-frame-panel"
          aria-label={`Step ${selectedStep.index + 1} frame preview`}
        >
          <div className="step-frame-bar">
            <div className="step-frame-meta">
              <strong className="tnum">
                Step {String(selectedStep.index + 1).padStart(2, "0")} · {selectedStep.action} {selectedStep.target}
              </strong>
              <span className="step-frame-local">Local only · stays on device</span>
            </div>

            <div className="step-frame-controls">
              {hasAnyFrame && (
                <>
                  <div className="seg seg-compact" role="group" aria-label="Frame view mode">
                    {(
                      [
                        { id: "split", label: "Side by side" },
                        { id: "before", label: "Before" },
                        { id: "after", label: "After" },
                      ] as const
                    ).map((opt) => (
                      <button
                        key={opt.id}
                        type="button"
                        className={frameMode === opt.id ? "active" : ""}
                        aria-pressed={frameMode === opt.id}
                        onClick={() => setFrameMode(opt.id)}
                      >
                        {opt.label}
                      </button>
                    ))}
                  </div>
                  <button
                    type="button"
                    className={
                      blurSensitive
                        ? "btn btn-sm btn-primary step-blur-toggle"
                        : "btn btn-sm btn-ghost step-blur-toggle"
                    }
                    aria-pressed={blurSensitive}
                    onClick={() => setBlurSensitive((prev) => !prev)}
                  >
                    {blurSensitive ? "Sensitive text blurred" : "Blur sensitive text"}
                  </button>
                </>
              )}
            </div>
          </div>

          {hasAnyFrame ? (
            <div
              className={
                frameMode === "split"
                  ? "step-frame-grid step-frame-grid-split"
                  : "step-frame-grid"
              }
            >
              {(frameMode === "split" || frameMode === "before") && (
                <FrameCard
                  label="Before"
                  frame={beforeFrame}
                  step={selectedStep}
                  blurred={blurSensitive}
                />
              )}
              {(frameMode === "split" || frameMode === "after") && (
                <FrameCard
                  label="After"
                  frame={afterFrame}
                  step={selectedStep}
                  blurred={blurSensitive}
                />
              )}
            </div>
          ) : (
            <div className="step-frame-empty">
              No local frames were retained on disk for this step. Run evidence and
              signed read-back checks remain available below.
            </div>
          )}
        </section>
      )}
    </div>
  );
}
