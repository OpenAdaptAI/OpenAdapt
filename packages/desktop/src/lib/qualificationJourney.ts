import type { QualificationProject } from "./types";

export type QualificationJourneyState = "complete" | "current" | "waiting" | "ready";

export interface QualificationJourneyStep {
  id: string;
  label: string;
  detail: string;
  targetId: string;
  state: QualificationJourneyState;
}

interface JourneyCandidate extends Omit<QualificationJourneyStep, "state"> {
  complete: boolean;
}

/**
 * Derive the operator journey only from the signed qualification projection.
 * This is presentation logic. It does not create evidence or weaken a gate.
 */
export function qualificationJourney(
  project: QualificationProject,
): QualificationJourneyStep[] {
  const capabilityCoverage = project.capability_coverage || {
    required: [],
    observed: [],
    missing: [],
    satisfied: false,
    cases: [],
  };
  const actions = project.graph.nodes.filter((node) => node.kind === "action");
  const reviewedActions = actions.filter(
    (action) => project.controls.actions[action.id]?.classification?.operator_confirmed,
  ).length;
  const casesComplete =
    project.report.case_count > 0 &&
    project.report.passed_case_count >= project.report.case_count &&
    capabilityCoverage.satisfied;

  const candidates: JourneyCandidate[] = [
    {
      id: "environment",
      label: "Set environment",
      detail: project.project
        ? `${project.project.environment.application} ${project.project.environment.application_version} · ${project.project.environment.target_kind}`
        : "Name the application, version, surface, and runner requirements.",
      targetId: project.project
        ? "qualification-summary-section"
        : "qualification-environment-section",
      complete: Boolean(project.project) && !project.migration_required,
    },
    {
      id: "inspect",
      label: "Inspect workflow",
      detail: `${project.graph.nodes.length} graph nodes are available for review.`,
      targetId: "qualification-graph-section",
      complete: project.graph.nodes.length > 0,
    },
    {
      id: "risk",
      label: "Review risk",
      detail: `${reviewedActions} of ${actions.length} actions have an operator-confirmed risk.`,
      targetId: "qualification-actions-section",
      complete: actions.length > 0 && reviewedActions === actions.length,
    },
    {
      id: "identity",
      label: "Arm identity",
      detail: `${project.report.identity_covered_action_count} of ${project.report.consequential_action_count} consequential actions are covered.`,
      targetId: "qualification-contract-section",
      complete:
        project.report.identity_covered_action_count >=
        project.report.consequential_action_count,
    },
    {
      id: "effects",
      label: "Bind effects",
      detail: `${project.report.effect_covered_action_count} of ${project.report.effect_required_action_count} required effects are covered.`,
      targetId: "qualification-contract-section",
      complete:
        project.report.effect_covered_action_count >=
        project.report.effect_required_action_count,
    },
    {
      id: "cases",
      label: "Run cases",
      detail: capabilityCoverage.satisfied
        ? `${project.report.passed_case_count} of ${project.report.case_count} required cases passed this revision.`
        : capabilityCoverage.missing.length > 0
          ? `${project.report.passed_case_count} of ${project.report.case_count} cases passed; ${capabilityCoverage.missing.length} runner capabilities remain unobserved.`
          : `${project.report.passed_case_count} of ${project.report.case_count} cases passed with current signed runner evidence.`,
      targetId: "qualification-cases-section",
      complete: casesComplete,
    },
    {
      id: "certify",
      label: "Certify",
      detail: project.certification_current
        ? "The certification matches this exact project revision."
        : "Run the certification gate after every required contract and case passes.",
      targetId: "qualification-summary-section",
      complete: project.certification_current,
    },
    {
      id: "seal",
      label: "Seal",
      detail: project.graph.bundle.encrypted
        ? "This workflow version is sealed and encrypted."
        : "Create an immutable encrypted version for export or deployment.",
      targetId: "qualification-artifact-section",
      complete: project.graph.bundle.encrypted,
    },
  ];

  const firstIncomplete = candidates.findIndex((candidate) => !candidate.complete);
  const steps: QualificationJourneyStep[] = candidates.map((candidate, index) => ({
    id: candidate.id,
    label: candidate.label,
    detail: candidate.detail,
    targetId: candidate.targetId,
    state: candidate.complete
      ? ("complete" as const)
      : index === firstIncomplete
        ? ("current" as const)
        : ("waiting" as const),
  }));

  const deliveryReady =
    project.certification_current &&
    project.graph.bundle.encrypted &&
    capabilityCoverage.satisfied;
  steps.push({
    id: "deliver",
    label: "Export or deploy",
    detail: deliveryReady
      ? "The exact artifact is ready for local export or governed deployment."
      : "Complete certification, sealing, and runner compatibility first.",
    targetId: "qualification-artifact-section",
    state: deliveryReady ? "ready" : firstIncomplete < 0 ? "current" : "waiting",
  });
  return steps;
}
