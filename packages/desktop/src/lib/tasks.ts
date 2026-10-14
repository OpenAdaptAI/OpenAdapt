// One task per recording. Checking and approving a task makes new versions of
// it (the sealed, approved one is a new version), so the Tasks screen shows the
// newest version of each recording and adds up what every version did.

import type { ResultCounts, ResultsSummary, Workflow } from "./types";

export interface Task {
  /** Stable key: the recording the versions came from. */
  key: string;
  /** The newest version: the one Check it and Run it act on. */
  current: Workflow;
  versions: Workflow[];
  openItems: number;
  teachingItems: number;
  blocked: boolean;
}

export type NextStep = "show" | "check" | "run" | "needs";

export interface TaskStatus {
  word: string;
  tone: "ok" | "warn" | "neutral";
  icon: "check" | "attention" | "ring" | "half" | "play" | "search";
  next: NextStep;
  nextLabel: string;
  detail: string;
}

function newer(left: Workflow, right: Workflow): number {
  const version = (right.version ?? 1) - (left.version ?? 1);
  if (version !== 0) return version;
  return String(right.updated_at ?? "").localeCompare(String(left.updated_at ?? ""));
}

export function groupTasks(workflows: Workflow[]): Task[] {
  const groups = new Map<string, Workflow[]>();
  for (const workflow of workflows) {
    const key = workflow.capture_id || workflow.id;
    groups.set(key, [...(groups.get(key) ?? []), workflow]);
  }
  return [...groups.entries()]
    .map(([key, versions]) => {
      const sorted = [...versions].sort(newer);
      return {
        key,
        current: sorted[0],
        versions: sorted,
        openItems: sorted.reduce(
          (sum, item) => sum + (item.open_items ?? 0) + (item.open_halts ?? 0),
          0,
        ),
        teachingItems: sorted.reduce((sum, item) => sum + (item.teaching_items ?? 0), 0),
        blocked: sorted.some((item) => item.blocked),
      };
    })
    .sort((left, right) => {
      // What needs a person comes first, then what is ready to run.
      const rank = priority(left) - priority(right);
      if (rank !== 0) return rank;
      return String(right.current.updated_at ?? "").localeCompare(
        String(left.current.updated_at ?? ""),
      );
    });
}

function priority(task: Task): number {
  if (task.blocked || task.openItems > 0) return 0;
  const readiness = task.current.readiness ?? "new";
  return readiness === "ready" ? 1 : readiness === "checking" ? 2 : 3;
}

/**
 * One status word and one next step for a task card.
 *
 * `onlyProblems` is true only when this computer applies "Only ask a person
 * about problems" to the task (the engine reports the switch as "on").
 */
export function taskStatus(
  task: Task,
  { onlyProblems = false }: { onlyProblems?: boolean } = {},
): TaskStatus {
  if (task.blocked) {
    return {
      word: "Check the record",
      tone: "warn",
      icon: "search",
      next: "needs",
      nextLabel: "Open Needs you",
      detail:
        "A save may have gone through. Check that record before OpenAdapt runs more items.",
    };
  }
  if (task.teachingItems > 0) {
    return {
      word: "Needs a fix",
      tone: "warn",
      icon: "attention",
      next: "show",
      nextLabel: "Show it",
      detail:
        "OpenAdapt couldn't do a step the way you showed it. Show it the right way once; a person approves the fix before it's used.",
    };
  }
  if (task.openItems > 0) {
    return {
      word: "Needs you",
      tone: "warn",
      icon: "attention",
      next: "needs",
      nextLabel: "Open Needs you",
      detail:
        task.openItems === 1
          ? "1 item is waiting for your decision."
          : `${task.openItems} items are waiting for your decision.`,
    };
  }
  const readiness = task.current.readiness ?? "new";
  if (readiness === "ready") {
    return {
      word: "Ready",
      tone: "ok",
      icon: "play",
      next: "run",
      nextLabel: "Run it",
      detail: onlyProblems
        ? "Checked and approved. Only problems come to you."
        : "Checked and approved. Give it a list of items to work through.",
    };
  }
  if (readiness === "checking") {
    return {
      word: "Checking",
      tone: "neutral",
      icon: "half",
      next: "check",
      nextLabel: "Continue checking",
      detail: "Finish checking this task before it runs on real items.",
    };
  }
  return {
    word: "Not checked",
    tone: "neutral",
    icon: "ring",
    next: "check",
    nextLabel: "Check it",
    detail: "Confirm what it changes and how it checks the record. About 5 minutes.",
  };
}

const EMPTY: Required<Pick<ResultCounts, "saved_without_person">> & ResultCounts = {
  done_checked: 0,
  saved_without_person: 0,
  needed_person: 0,
  waiting: 0,
  wrong_record_prevented: 0,
  done_by_person: 0,
  skipped: 0,
  declined: 0,
  items: 0,
};

/** Add up the result counts of every version of one task. */
export function taskCounts(task: Task, summary: ResultsSummary | null): ResultCounts {
  const total = { ...EMPTY };
  if (!summary) return total;
  for (const version of task.versions) {
    const counts = summary.tasks[version.id];
    if (!counts) continue;
    for (const key of Object.keys(EMPTY) as (keyof typeof EMPTY)[]) {
      total[key] += counts[key] ?? 0;
    }
  }
  return total;
}
