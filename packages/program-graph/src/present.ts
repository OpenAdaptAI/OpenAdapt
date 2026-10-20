// Words and tones for nodes. Every value is read from the spec.

import { nodeEffects, nodeHalts, nodeKind, nodePostconditions, type GraphNode } from "./spec.js";

export type NodeTone = "default" | "governed" | "branch" | "halt" | "terminal" | "stop";

/** A recorded raw click ("click at (412, 88)") reads as its intent instead. */
export function displayTitle(node: GraphNode): string {
  if (/^(click|double click) at \(\d+,\s*\d+\)$/i.test(node.title.trim())) {
    return node.action === "double_click" ? "Double-click recorded target" : "Click recorded target";
  }
  return node.title;
}

export function nodeTone(node: GraphNode): NodeTone {
  const kind = nodeKind(node);
  if (kind === "terminal") {
    const outcome = node.outcome?.toLowerCase();
    return outcome === "halt" || outcome === "halted" || outcome === "escalate" ? "stop" : "terminal";
  }
  if (node.risk === "irreversible") return "halt";
  if (kind === "branch" || kind === "loop") return "branch";
  if (node.identity?.armed || nodeEffects(node).length > 0) return "governed";
  return "default";
}

const pad = (n: number) => String(n).padStart(2, "0");

/** The chip on a card: "03", "END", "STOP". */
export function stepChip(node: GraphNode): string {
  if (nodeKind(node) === "terminal") return nodeTone(node) === "stop" ? "STOP" : "END";
  return pad(node.index + 1);
}

export function kindLabel(node: GraphNode): string {
  return nodeKind(node).replaceAll("_", " ");
}

export type LaneState = "present" | "warn" | "none";

export interface EvidenceLanes {
  resolve: { state: LaneState; count: number; labels: string[] };
  identity: { state: LaneState; text: "Checked" | "Not checked" | "Not needed" };
  screen: { state: LaneState; count: number };
  readback: { state: LaneState; count: number };
}

export function evidenceLanes(node: GraphNode): EvidenceLanes {
  const rungs = node.resolution?.rungs?.filter((rung) => rung.present) ?? [];
  const identity = node.identity;
  return {
    resolve: { state: rungs.length ? "present" : "none", count: rungs.length, labels: rungs.map((rung) => rung.label) },
    identity: {
      state: identity?.armed ? "present" : identity?.applicable ? "warn" : "none",
      text: identity?.armed ? "Checked" : identity?.applicable ? "Not checked" : "Not needed",
    },
    screen: { state: nodePostconditions(node).length ? "present" : "none", count: nodePostconditions(node).length },
    readback: { state: nodeEffects(node).length ? "present" : "none", count: nodeEffects(node).length },
  };
}

/** One sentence for a screen reader: what the card is and what it checks. */
export function nodeDescription(node: GraphNode, title = displayTitle(node)): string {
  const parts = [`${stepChip(node) === "END" || stepChip(node) === "STOP" ? stepChip(node).toLowerCase() : `step ${stepChip(node)}`}, ${kindLabel(node)}: ${title}.`];
  if (node.identity?.armed) parts.push("Checks the right record.");
  if (nodeEffects(node).length) parts.push("Reads the saved record back.");
  const halts = nodeHalts(node).length;
  if (halts) parts.push(`${halts} stop ${halts === 1 ? "rule" : "rules"}.`);
  return parts.join(" ");
}

export type StopRuleKind = "run" | "review" | "decision" | "end";

export interface StopRuleWords {
  /** run: the run stops; review: a person reviews before the program runs on its own. */
  kind: StopRuleKind;
  text: string;
}

const PLAIN_HALTS: [RegExp, StopRuleWords][] = [
  [/^halts until an operator binds the placeholder effect$/, { kind: "run", text: "Waits until a person names the check for this save." }],
  [/^halts if the system-of-record effect is not confirmed$/, { kind: "run", text: "Stops if reading the saved record back doesn’t confirm it." }],
  [/^halts if precondition unmet/, { kind: "run", text: "Stops if the screen isn’t in the expected state before this step." }],
  [/^halts if not ready in time/, { kind: "run", text: "Stops if the screen isn’t ready in time." }],
  [/^irreversible write WITHOUT an armed identity gate$/, { kind: "review", text: "Flagged for review: this action can’t be undone, and this step has no right-record check of its own." }],
  [/^certification halts until action risk is explicitly reviewed$/, { kind: "review", text: "A person reviews this action before the program is approved to run on its own." }],
  [/^halts until an authenticated answer is signed$/, { kind: "decision", text: "Waits for a signed answer from a person." }],
  [/^terminal: (halt|escalate)$/, { kind: "end", text: "The program ends here and hands the item to a person." }],
];

/**
 * A step's stop rules in plain words, in the spec's order.
 *
 * Flow's full projection names each rule; the plain words map those names.
 * The public projection replaces them with "declared stop rule N"; then the
 * rules are rebuilt from the public fields by the rules Flow's builder uses
 * (openadapt_flow/visualize/builder.py), in its order. A rule the public
 * fields cannot show (a wait whose predicate was removed) is placed where the
 * builder puts it. When the count still disagrees, each rule stays generic.
 */
export function stopRuleWords(node: GraphNode): StopRuleWords[] {
  const halts = nodeHalts(node);
  if (halts.length === 0) return [];
  if (!halts.every((halt) => /^declared stop rule \d+$/.test(halt))) {
    return halts.map(
      (halt) => PLAIN_HALTS.find(([pattern]) => pattern.test(halt))?.[1] ?? { kind: "run", text: halt },
    );
  }
  const plain = (pattern: RegExp) => PLAIN_HALTS.find(([p]) => p.source === pattern.source)![1];
  const kind = nodeKind(node);
  if (kind === "terminal") return halts.map(() => PLAIN_HALTS[PLAIN_HALTS.length - 1][1]);
  if (kind === "business_decision") return halts.map(() => PLAIN_HALTS[6][1]);
  const before: StopRuleWords[] = [];
  const after: StopRuleWords[] = [];
  const effects = nodeEffects(node);
  if (effects.length) {
    before.push(plain(effects.some((e) => e.needs_operator_confirmation) ? PLAIN_HALTS[0][0] : PLAIN_HALTS[1][0]));
  }
  if (node.guard_on_unmet === "halt") before.push(plain(PLAIN_HALTS[2][0]));
  if (node.risk === "irreversible" && node.identity?.applicable && node.identity.armed === false) {
    after.push(plain(PLAIN_HALTS[4][0]));
  }
  if (node.risk_review_required) after.push(plain(PLAIN_HALTS[5][0]));
  const wait = node.wait_until != null ? [plain(PLAIN_HALTS[3][0])] : [];
  let words = [...before, ...wait, ...after];
  // The public projection removes the wait predicate, so a wait rule shows
  // only as one rule more than the public fields explain.
  if (words.length === halts.length - 1 && !node.wait_until) {
    words = [...before, plain(PLAIN_HALTS[3][0]), ...after];
  }
  if (words.length !== halts.length) return halts.map(() => ({ kind: "run", text: "Stops if a declared check fails." }));
  return words;
}
