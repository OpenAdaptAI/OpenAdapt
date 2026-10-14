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
  /** Flow's executable risk; "irreversible" is a floor a confirmation can't lower. */
  executableRisk: "reversible" | "irreversible";
  confirmed: boolean;
  identityArmed: boolean;
  canArm: boolean;
  identityText: string;
  checked: boolean;
  checkText: string;
  /** The program already says what this step saves (it has record checks). */
  hasRecordChecks: boolean;
}

export interface CaseView {
  id: string;
  kind: string;
  label: string;
  explanation: string;
  status: "passed" | "failed" | "blocked" | "not_run";
  faultStepId: string | null;
}

/** A step the program can't yet prove leaves the record alone. */
export interface UnprovenStep {
  id: string;
  caption: string;
}

export interface CheckPlan {
  hasProject: boolean;
  writes: WriteStep[];
  /** Steps Flow has classified as read-only: they only read or move around. */
  readCount: number;
  /**
   * Steps that aren't known to save but aren't proven read-only either.
   * Check it never calls a task read-only while any remain.
   */
  unproven: UnprovenStep[];
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

// What each drill changes, as Flow's local drill campaign makes it (B276):
// right before the save, in the page or in the record check it's handed.
const CASE_COPY: Record<string, { label: string; explanation: string }> = {
  wrong_identity: {
    label: "Wrong record",
    explanation:
      "The record shown on screen is swapped for a different one. OpenAdapt must stop before saving.",
  },
  ambiguity: {
    label: "Two possible matches",
    explanation:
      "A second copy of the save button appears on the page. OpenAdapt must stop instead of guessing.",
  },
  stale_identity: {
    label: "Record changed mid-task",
    explanation:
      "The record on screen changes after OpenAdapt checked it. OpenAdapt must stop before saving.",
  },
  missing_effect: {
    label: "No record check",
    explanation:
      "The record check is taken away. OpenAdapt must refuse to save without it.",
  },
  weak_effect: {
    label: "Weak record check",
    explanation:
      "The record check is swapped for one that only looks at the screen. OpenAdapt must refuse to save with it.",
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

/**
 * Graph node kinds whose steps the graph itself shows: an action is a step,
 * and a loop's body is drawn inline. Anything else (a sub-task call, a child
 * program, a kind this version doesn't know) may run steps Check it can't see.
 */
const SHOWN_NODE_KINDS = new Set(["action", "terminal", "branch", "loop", "business_decision"]);
const HIDDEN_STEPS_CAPTION = "Runs a part of the task that isn't shown step by step here";

/** Derive the short path from one qualification projection. */
export function checkPlan(response: QualificationProject): CheckPlan {
  const actions = response.graph.nodes.filter(
    (node): node is QualificationNode => node.kind === "action",
  );
  const controls = response.controls.actions;
  const writes: WriteStep[] = [];
  const unproven: UnprovenStep[] = [];
  let readCount = 0;
  for (const node of actions) {
    const control = controls[node.id];
    const classification = control?.classification?.classification;
    // A step that saves to the record. Flow requires a record check for every
    // state-changing, consequential, or irreversible step, so all three count.
    const isWrite =
      node.risk === "irreversible" ||
      node.effects.length > 0 ||
      (control?.effects.length ?? 0) > 0 ||
      classification === "state_changing" ||
      classification === "consequential" ||
      classification === "irreversible";
    if (!isWrite) {
      // Only a confirmed read-only classification proves a step leaves the
      // record alone. An unknown or unreviewed step may still save, so it is
      // never counted among the steps that "only read".
      if (classification === "read_only" && control?.classification?.operator_confirmed) {
        readCount += 1;
      } else {
        unproven.push({ id: node.id, caption: plainStep(node.title || node.action || node.id) });
      }
      continue;
    }
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
        : node.effects.length
          ? node.effects.map((effect) => sentenceCase(effect.summary || "Saves an entry."))
          : ["OpenAdapt doesn't know yet what this step saves."],
      executableRisk: node.risk === "irreversible" ? "irreversible" : "reversible",
      confirmed: Boolean(control?.classification?.operator_confirmed),
      identityArmed,
      canArm: Boolean(control?.identity.can_arm),
      identityText: params.length
        ? `Before saving, it checks that the record on screen matches the ${params.join(" and ")} you give it. If they differ, it stops and asks you.`
        : "Before saving, it checks the record on screen against the details it saw when you showed the task. If they differ, it stops and asks you.",
      checked: effects.length > 0 && tiers.length === effects.length,
      hasRecordChecks: effects.length > 0 || node.effects.length > 0,
      checkText: effects.length
        ? `After saving, it ${tierText(Math.min(...(tiers.length ? tiers : [0])))} to confirm the entry.`
        : "OpenAdapt doesn't know yet how to confirm this save.",
    });
  }
  const project = response.project;
  const report = response.report;
  // The graph draws a call into a reusable sub-task as one node, without the
  // steps inside it, so a save there never reaches the loop above. Flow's
  // report still counts and checks every step the program can run. A step
  // Flow knows about but Check it can't see is never proven read-only.
  for (const node of response.graph.nodes) {
    if (!SHOWN_NODE_KINDS.has(node.kind)) {
      unproven.push({ id: node.id, caption: HIDDEN_STEPS_CAPTION });
    }
  }
  const seen = new Set(actions.map((node) => controls[node.id]?.step_id ?? node.id));
  const hidden = new Set(
    (report.refusals ?? [])
      .map((refusal) => refusal.step_id)
      .filter((id): id is string => typeof id === "string" && id !== "" && !seen.has(id)),
  );
  const unseen = Math.max(
    hidden.size,
    typeof report.action_count === "number" ? report.action_count - seen.size : 0,
  );
  if (unseen > 0 && !unproven.some((step) => step.caption === HIDDEN_STEPS_CAPTION)) {
    unproven.push({ id: "__unseen_steps__", caption: HIDDEN_STEPS_CAPTION });
  }
  const firstWrite = writes[0]?.id ?? null;
  const cases = project
    ? project.cases.map((item) => caseView(project, item, firstWrite))
    : [];
  const practice = cases.find((item) => isPractice(item.kind)) ?? null;
  const drills = cases.filter((item) => !isPractice(item.kind));
  const hasProject = Boolean(project) && !response.draft_environment;

  // "Not needed" is a claim that the task can't change a record. Make it only
  // when every step is proven read-only. With no known save but unproven
  // steps left, the honest answer is that a person still has to say.
  const provenReadOnly = writes.length === 0 && unproven.length === 0;
  const unknownWrites = writes.length === 0 && unproven.length > 0;
  const changes: StepState = provenReadOnly
    ? "not_needed"
    : unknownWrites
      ? "missing"
      : hasProject && writes.every((step) => step.confirmed)
        ? "done"
        : "todo";
  const rightRecord: StepState = provenReadOnly
    ? "not_needed"
    : unknownWrites
      ? "missing"
      : writes.every((step) => step.identityArmed)
        ? hasProject ? "done" : "todo"
        : writes.some((step) => !step.identityArmed && !step.canArm)
          ? "missing"
          : "todo";
  const recordCheck: StepState = provenReadOnly
    ? "not_needed"
    : unknownWrites
      ? "missing"
      : writes.every((step) => step.checked)
        ? hasProject ? "done" : "todo"
        : "missing";
  const practiceState: StepState =
    practice?.status === "passed" ? "done" : "todo";
  // Drills are needed whenever a step may save. Flow sets them up from the
  // practice item (``qualify drills``), so a task with a save has none until
  // then: that is "to do", never "not needed".
  const drillsState: StepState =
    drills.length === 0
      ? provenReadOnly
        ? "not_needed"
        : "todo"
      : drills.every((item) => item.status === "passed")
        ? "done"
        : "todo";
  const blockers = report.refusals.map((refusal) => refusal.message).filter(Boolean);
  return {
    hasProject,
    writes,
    readCount,
    unproven,
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
