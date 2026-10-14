// The short "Check it" path, derived from Flow's qualification projection.
//
// Every answer here comes from the engine: what each step can change, how the
// right-record check and the record check are set up, which test runs passed,
// and whether this version is approved and sealed. Nothing is inferred from
// the UI. The full contract stays in "Advanced: exact checks".

import { humanizeInput, plainStep, sentenceCase } from "./outcomes";
import type {
  QualificationEditableEffect,
  QualificationNode,
  QualificationProject,
} from "./types";

export type StepState = "done" | "todo" | "missing" | "not_needed";

export interface WriteStep {
  id: string;
  caption: string;
  effects: string[];
  confirmed: boolean;
  identityArmed: boolean;
  canArm: boolean;
  identityText: string;
  checked: boolean;
  checkText: string;
}

export interface CaseView {
  id: string;
  kind: string;
  label: string;
  explanation: string;
  status: "passed" | "failed" | "blocked" | "not_run";
  faultStepId: string | null;
}

export interface CheckPlan {
  hasProject: boolean;
  writes: WriteStep[];
  readCount: number;
  changes: StepState;
  rightRecord: StepState;
  recordCheck: StepState;
  practice: CaseView | null;
  practiceState: StepState;
  drills: CaseView[];
  drillsState: StepState;
  approved: boolean;
  sealed: boolean;
  /** Plain reasons the engine still refuses approval. */
  blockers: string[];
}

const TIER_TEXT: Record<number, string> = {
  1: "reads the record back from your system's records",
  2: "opens the record again in a separate session and reads it",
  3: "opens the saved record again on screen",
  4: "looks at the screen right after saving",
};

export function tierText(tier: number | null | undefined): string {
  return TIER_TEXT[tier ?? 0] ?? "checks the saved record";
}

export const TIER_CHOICES = [
  { tier: 1, label: "Read it back from your system's records", note: "Strongest. Needs IT to connect OpenAdapt to the records." },
  { tier: 2, label: "Read it back in a separate session", note: "Strong. Opens the record again without reusing the screen." },
  { tier: 3, label: "Open the saved record again on screen", note: "Use when there's no other way to read the record." },
];

function parameterName(expression: { source: string; value?: string | null } | null | undefined): string | null {
  if (!expression || expression.source !== "parameter" || !expression.value) return null;
  return humanizeInput(expression.value);
}

export function describeEffect(effect: QualificationEditableEffect): string {
  const match = Object.values(effect.match ?? {})
    .map(parameterName)
    .find(Boolean);
  const value = parameterName(effect.value);
  const record = match ? `the record for the ${match} you give it` : "the record";
  if (effect.kind === "field_equals" && effect.field) {
    return value
      ? `Sets ${humanizeInput(effect.field)} on ${record} to the ${value} you give it.`
      : `Sets ${humanizeInput(effect.field)} on ${record}.`;
  }
  return effect.field && value
    ? `Saves a ${humanizeInput(effect.field)} on ${record}, from the ${value} you give it.`
    : `Saves an entry on ${record}.`;
}

const CASE_COPY: Record<string, { label: string; explanation: string }> = {
  wrong_identity: {
    label: "Wrong record",
    explanation:
      "OpenAdapt is given a record that doesn't match the screen. It must stop before saving.",
  },
  missing_effect: {
    label: "Save didn't land",
    explanation:
      "The app says it saved, but the record stays empty. OpenAdapt must notice and stop.",
  },
  ambiguity: {
    label: "Two possible matches",
    explanation:
      "The screen offers more than one match. OpenAdapt must stop instead of guessing.",
  },
  stale_identity: {
    label: "Record changed mid-task",
    explanation:
      "The record changes after OpenAdapt finds it. It must find it again or stop.",
  },
  weak_effect: {
    label: "Weak record check",
    explanation:
      "The record check is weaker than agreed. OpenAdapt must refuse to call the item done.",
  },
};

function isPractice(kind: string): boolean {
  return kind === "representative" || kind === "nominal";
}

function caseView(
  project: NonNullable<QualificationProject["project"]>,
  item: NonNullable<QualificationProject["project"]>["cases"][number],
  firstWrite: string | null,
): CaseView {
  const current = [...(item.results ?? [])]
    .reverse()
    .find((result) => result.project_revision === project.revision);
  const copy = isPractice(item.kind)
    ? { label: "Practice run", explanation: item.description || "One test item from start to finish." }
    : (CASE_COPY[item.kind] ?? {
        label: "Safety drill",
        explanation: item.description || "OpenAdapt checks that it stops instead of guessing.",
      });
  return {
    id: item.id,
    kind: item.kind,
    label: copy.label,
    explanation: copy.explanation,
    status: current?.status ?? "not_run",
    faultStepId: item.fault_target?.step_id ?? firstWrite,
  };
}

/** Derive the short path from one qualification projection. */
export function checkPlan(response: QualificationProject): CheckPlan {
  const actions = response.graph.nodes.filter(
    (node): node is QualificationNode => node.kind === "action",
  );
  const controls = response.controls.actions;
  const writes: WriteStep[] = [];
  for (const node of actions) {
    const control = controls[node.id];
    const classification = control?.classification?.classification;
    // A step that saves to the record. A "state_changing" step (typing into
    // a search box, opening a tab) changes the screen, not the record.
    const isWrite =
      node.risk === "irreversible" ||
      node.effects.length > 0 ||
      (control?.effects.length ?? 0) > 0 ||
      classification === "consequential" ||
      classification === "irreversible";
    if (!isWrite) continue;
    const signals = control?.identity.policy?.signals ?? [];
    const params = signals.flatMap((signal) => signal.params ?? []).map(humanizeInput);
    const identityArmed = Boolean(control?.identity.armed || node.identity?.armed);
    const effects = control?.effects ?? [];
    const tiers = effects
      .map((effect) => effect.verification_tier)
      .filter((tier): tier is number => typeof tier === "number");
    writes.push({
      id: node.id,
      caption: plainStep(node.title || node.action || node.id),
      effects: effects.length
        ? effects.map(describeEffect)
        : node.effects.map((effect) => sentenceCase(effect.summary || "Saves an entry.")),
      confirmed: Boolean(control?.classification?.operator_confirmed),
      identityArmed,
      canArm: Boolean(control?.identity.can_arm),
      identityText: params.length
        ? `Before saving, it checks that the record on screen matches the ${params.join(" and ")} you give it. If they differ, it stops and asks you.`
        : "Before saving, it checks the record on screen against the details it saw when you showed the task. If they differ, it stops and asks you.",
      checked: effects.length > 0 && tiers.length === effects.length,
      checkText: effects.length
        ? `After saving, it ${tierText(Math.min(...(tiers.length ? tiers : [0])))} to confirm the entry.`
        : "OpenAdapt doesn't know yet how to confirm this save.",
    });
  }
  const project = response.project;
  const report = response.report;
  const firstWrite = writes[0]?.id ?? null;
  const cases = project
    ? project.cases.map((item) => caseView(project, item, firstWrite))
    : [];
  const practice = cases.find((item) => isPractice(item.kind)) ?? null;
  const drills = cases.filter((item) => !isPractice(item.kind));
  const hasProject = Boolean(project) && !response.draft_environment;

  const changes: StepState =
    writes.length === 0 ? "not_needed" : hasProject && writes.every((step) => step.confirmed) ? "done" : "todo";
  const rightRecord: StepState =
    writes.length === 0
      ? "not_needed"
      : writes.every((step) => step.identityArmed)
        ? hasProject ? "done" : "todo"
        : writes.some((step) => !step.identityArmed && !step.canArm)
          ? "missing"
          : "todo";
  const recordCheck: StepState =
    writes.length === 0
      ? "not_needed"
      : writes.every((step) => step.checked)
        ? hasProject ? "done" : "todo"
        : "missing";
  const practiceState: StepState =
    practice?.status === "passed" ? "done" : "todo";
  const drillsState: StepState =
    drills.length === 0
      ? "not_needed"
      : drills.every((item) => item.status === "passed")
        ? "done"
        : "todo";
  const blockers = report.refusals.map((refusal) => refusal.message).filter(Boolean);
  return {
    hasProject,
    writes,
    readCount: actions.length - writes.length,
    changes,
    rightRecord,
    recordCheck,
    practice,
    practiceState,
    drills,
    drillsState,
    approved: Boolean(response.certification_current),
    sealed: Boolean(response.graph.bundle.encrypted),
    blockers,
  };
}

export interface ProposalPin {
  kind: "application" | "environment" | "identity" | "effect";
  status: "proposed" | "missing" | "confirmed";
  summary: string;
  halt_reason?: string | null;
  payload?: Record<string, unknown>;
}

export interface Proposal {
  status: "draft" | "halted" | "accepted";
  halt_reason?: string | null;
  pins: ProposalPin[];
  failure_matrix?: { id: string; kind: string; description: string; step_id?: string | null }[];
}

/** What a missing pin means for the person, in plain words. */
export function missingPinQuestion(pin: ProposalPin): string {
  switch (pin.kind) {
    case "application":
    case "environment":
      return "Which app is this, and which version? OpenAdapt couldn't tell from the recording.";
    case "identity":
      return "How should OpenAdapt recognize the right record? It didn't see the record's name or ID on the screen where it saves.";
    case "effect":
      return "How should OpenAdapt confirm the save? It didn't see where the saved entry lands in your system.";
  }
}
