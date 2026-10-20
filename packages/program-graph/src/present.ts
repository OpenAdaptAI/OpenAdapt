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
