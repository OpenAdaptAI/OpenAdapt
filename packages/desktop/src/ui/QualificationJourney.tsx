import { qualificationJourney } from "../lib/qualificationJourney";
import type { QualificationProject } from "../lib/types";
import { Card, CardHead } from "./primitives";

function stateLabel(state: ReturnType<typeof qualificationJourney>[number]["state"]): string {
  if (state === "complete") return "Complete";
  if (state === "current") return "Next";
  if (state === "ready") return "Ready";
  return "Waiting";
}

export function QualificationJourney({
  project,
}: {
  project: QualificationProject;
}) {
  const steps = qualificationJourney(project);
  const active =
    steps.find((step) => step.state === "current") ||
    steps.find((step) => step.state === "ready") ||
    steps[steps.length - 1];

  function openStep(targetId: string) {
    document.getElementById(targetId)?.scrollIntoView({
      behavior: "smooth",
      block: "start",
    });
  }

  return (
    <Card className="qualification-journey" data-testid="qualification-journey">
      <CardHead
        eyebrow="Qualification journey"
        title={active ? `Next: ${active.label}` : "Qualification is ready"}
        sub={active?.detail}
      />
      <ol className="qualification-journey-steps">
        {steps.map((step, index) => (
          <li
            className={`qualification-journey-step ${step.state}`}
            key={step.id}
          >
            <button
              className="qualification-journey-button"
              type="button"
              title={step.detail}
              aria-label={`Open ${step.label}`}
              onClick={() => openStep(step.targetId)}
            >
              <span className="qualification-journey-index" aria-hidden="true">
                {step.state === "complete" ? "✓" : index + 1}
              </span>
              <span className="qualification-journey-copy">
                <strong>{step.label}</strong>
                <span className="qualification-journey-state">
                  {stateLabel(step.state)}
                </span>
              </span>
            </button>
          </li>
        ))}
      </ol>
    </Card>
  );
}
