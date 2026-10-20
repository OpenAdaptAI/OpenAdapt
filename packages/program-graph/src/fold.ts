// Folding: show a run of compiled steps as one card.
//
// A long program (the OpenEMR referral body is 41 steps) reads better as the
// sections it fills. A fold names its member nodes; the folded card carries
// the members' combined checks and stop rules, and edges inside a fold
// disappear. Every member keeps its own id, so a run trace still lands on the
// right card. Nothing about the program's shape is invented here: the folded
// graph is a projection of the spec, and an unfolded group shows the exact
// nodes again.

import {
  edgeKind,
  nodeEffects,
  nodeHalts,
  nodeKind,
  nodePostconditions,
  specEdges,
  specNodes,
  type EdgeKind,
  type GraphEdge,
  type GraphNode,
  type GraphSection,
  type ProgramGraphSpec,
} from "./spec.js";

export interface FoldGroup {
  /** Id of the folded card. Must not collide with a spec node id. */
  id: string;
  /** Card title. Defaults to "Steps 01–09". */
  title?: string;
  /** One line under the title, such as "3 fields". */
  subtitle?: string;
  /** Member node ids, in program order. */
  nodes: string[];
}

export interface FoldSpec {
  groups: FoldGroup[];
}

/** A stop drawn as its own exit, so the stop is visible without the inspector. */
export interface StopExit {
  /** The spec node whose stop rule this exit draws. */
  node: string;
  /** Edge label, such as "chart not confirmed". */
  label?: string;
}

export interface FoldInfo {
  id: string;
  title: string;
  subtitle?: string;
  members: GraphNode[];
  /** 1-based step numbers of the first and last member. */
  first: number;
  last: number;
}

export interface DisplayGraph {
  spec: ProgramGraphSpec;
  /** Folded cards by id. */
  folds: Map<string, FoldInfo>;
  /** Spec node id -> id of the card that shows it (itself when unfolded). */
  ownerOf: Map<string, string>;
}

/** The id of the virtual stop card added by `stopExits`. */
export const STOP_NODE_ID = "__opg_stop__";

const pad = (n: number) => String(n).padStart(2, "0");

/** Problems with a fold spec, as sentences. Empty when the folds are sound. */
export function validateFolds(spec: ProgramGraphSpec, folds: FoldSpec): string[] {
  const problems: string[] = [];
  const ids = new Set(specNodes(spec).map((node) => node.id));
  const seen = new Map<string, string>();
  for (const group of folds.groups) {
    if (ids.has(group.id)) problems.push(`fold ${group.id} reuses a spec node id`);
    if (group.nodes.length === 0) problems.push(`fold ${group.id} has no members`);
    for (const id of group.nodes) {
      if (!ids.has(id)) problems.push(`fold ${group.id} names ${id}, which the spec does not have`);
      const other = seen.get(id);
      if (other) problems.push(`${id} is in both ${other} and ${group.id}`);
      seen.set(id, group.id);
    }
  }
  return problems;
}

type Numberer = (node: GraphNode) => number | null;
const byIndex: Numberer = (node) => node.index + 1;

function foldedNode(group: FoldGroup, members: GraphNode[], numberOf: Numberer): GraphNode {
  const num = (node: GraphNode) => numberOf(node) ?? node.index + 1;
  const identities = members.map((node) => node.identity).filter((id) => id?.applicable);
  const unique = (values: string[]) => [...new Set(values)];
  return {
    id: group.id,
    index: Math.min(...members.map((node) => node.index)),
    kind: members.every((node) => nodeKind(node) === "action") ? "action" : nodeKind(members[0]),
    title: group.title ?? `Steps ${pad(num(members[0]))}–${pad(num(members[members.length - 1]))}`,
    action: null,
    risk: members.some((node) => node.risk === "irreversible")
      ? "irreversible"
      : members.some((node) => node.risk)
        ? "reversible"
        : null,
    resolution: null,
    identity: identities.length
      ? {
          applicable: true,
          armed: identities.every((id) => id?.armed === true),
        }
      : null,
    effects: members.flatMap(nodeEffects),
    postconditions: members.flatMap(nodePostconditions),
    // Each member's rules, numbered by step: two members' "declared stop
    // rule 1" are two different rules.
    halts: members.flatMap((node) => nodeHalts(node).map((halt) => `step ${pad(num(node))}: ${halt}`)),
    badges: unique(members.flatMap((node) => node.badges ?? [])),
  };
}

/**
 * Project the spec for display: fold the groups that are not expanded, and add
 * a stop card with an exit edge for each stop.
 */
export function deriveDisplayGraph(
  spec: ProgramGraphSpec,
  options: {
    folds?: FoldSpec | null;
    expanded?: ReadonlySet<string>;
    stopExits?: StopExit[] | null;
    stopLabel?: string;
    /** Step numbers for fold chips and titles (see stepNumberer). */
    numberOf?: Numberer;
  } = {},
): DisplayGraph {
  const numberOf = options.numberOf ?? byIndex;
  const nodes = specNodes(spec);
  const byId = new Map(nodes.map((node) => [node.id, node]));
  const ownerOf = new Map(nodes.map((node) => [node.id, node.id]));
  const folds = new Map<string, FoldInfo>();
  const replaced = new Map<string, GraphNode>();

  for (const group of options.folds?.groups ?? []) {
    if (options.expanded?.has(group.id) || byId.has(group.id)) continue;
    const members = group.nodes
      .map((id) => byId.get(id))
      .filter((node): node is GraphNode => node !== undefined && ownerOf.get(node.id) === node.id)
      .sort((a, b) => a.index - b.index);
    if (members.length === 0) continue;
    const card = foldedNode(group, members, numberOf);
    for (const member of members) ownerOf.set(member.id, group.id);
    replaced.set(members[0].id, card);
    folds.set(group.id, {
      id: group.id,
      title: card.title,
      subtitle: group.subtitle,
      members,
      first: numberOf(members[0]) ?? members[0].index + 1,
      last: numberOf(members[members.length - 1]) ?? members[members.length - 1].index + 1,
    });
  }

  const shown: GraphNode[] = [];
  for (const node of nodes) {
    const owner = ownerOf.get(node.id)!;
    if (owner === node.id) shown.push(node);
    else if (replaced.get(node.id)) shown.push(replaced.get(node.id)!);
  }

  const edges: GraphEdge[] = [];
  const seen = new Set<string>();
  for (const edge of specEdges(spec)) {
    const source = ownerOf.get(edge.source);
    const target = ownerOf.get(edge.target);
    if (!source || !target) continue;
    // An edge inside one fold disappears; an edge into a fold lands on the
    // fold, unless it is a loop's return into the fold's own first member.
    if (source === target && folds.has(source)) continue;
    const kind: EdgeKind = edgeKind(edge);
    const key = `${source}>${target}>${kind}>${edge.label ?? ""}`;
    if (seen.has(key)) continue;
    seen.add(key);
    edges.push({ ...edge, source, target, kind });
  }

  const stops = (options.stopExits ?? []).filter((stop) => ownerOf.has(stop.node));
  if (stops.length) {
    const terminalIndex = Math.max(-1, ...shown.map((node) => node.index)) + 1;
    shown.push({
      id: STOP_NODE_ID,
      index: terminalIndex,
      kind: "terminal",
      title: options.stopLabel ?? "Stop the run",
      outcome: "halt",
      halts: [],
      effects: [],
      postconditions: [],
    });
    for (const stop of stops) {
      const source = ownerOf.get(stop.node)!;
      const key = `${source}>${STOP_NODE_ID}`;
      if (seen.has(key)) continue;
      seen.add(key);
      edges.push({ source, target: STOP_NODE_ID, kind: "exception", label: stop.label ?? "stops" });
    }
  }

  return { spec: { ...spec, nodes: shown, edges }, folds, ownerOf };
}

/**
 * The compiled sections Flow ships with the spec (`spec.sections`, from the
 * bundle's sections.json), as folds: one card per section, titled from the
 * interface ("Contact", "Create New Patient") or by its fields. Null when the
 * spec has none.
 */
export function sectionFolds(spec: ProgramGraphSpec): FoldSpec | null {
  const sections = spec.sections ?? [];
  if (!sections.length) return null;
  return {
    groups: sections.map((section) => ({
      id: section.id,
      title: section.title,
      subtitle: section.subtitle,
      nodes: [...section.nodes],
    })),
  };
}

/** A person's change to the compiled sections, in the bundle's terms. Flow
 *  stores it in sections.json (`openadapt-flow sections`), so execution and
 *  the bundle digests never change. */
export type SectionEdit =
  | { op: "rename"; section: string; title: string }
  | { op: "merge"; section: string; other: string }
  | { op: "split"; section: string; at: string };

/** What a reviewer may do with the section behind one card or step. */
export interface SectionReview {
  section: GraphSection;
  /** The next section in the same straight run, when the two may merge. */
  mergeWith?: GraphSection;
  /** The step id a split at the selected member would start the new section at. */
  splitAt?: string;
}

/**
 * The review actions for fold `foldId` (and, for an unfolded section, the
 * selected member `nodeId`). The save section is never merged with field
 * sections or split; Flow refuses both too.
 */
export function sectionReview(spec: ProgramGraphSpec, foldId: string, nodeId?: string): SectionReview | null {
  const sections = spec.sections ?? [];
  const index = sections.findIndex((section) => section.id === foldId);
  if (index < 0) return null;
  const section = sections[index];
  const next = sections[index + 1];
  const nodes = specNodes(spec);
  const adjacent =
    next !== undefined &&
    (next.kind === "save") === (section.kind === "save") &&
    specEdges(spec).some(
      (edge) => edge.source === section.nodes[section.nodes.length - 1] && edge.target === next.nodes[0],
    ) &&
    nodes.some((node) => node.id === next.nodes[0]);
  const at = nodeId ? section.nodes.indexOf(nodeId) : -1;
  return {
    section,
    mergeWith: adjacent ? next : undefined,
    splitAt: section.kind !== "save" && at > 0 ? section.steps[at] : undefined,
  };
}

/**
 * Fold every straight run of at least `minLength` sequence-linked actions
 * into cards of at most `maxLength` steps. For a program that ships without
 * a hand-written fold spec.
 */
export function foldSequences(
  spec: ProgramGraphSpec,
  options: { minLength?: number; maxLength?: number } = {},
): FoldSpec {
  const minLength = options.minLength ?? 6;
  const maxLength = options.maxLength ?? 10;
  const nodes = [...specNodes(spec)].sort((a, b) => a.index - b.index);
  const incoming = new Map<string, GraphEdge[]>();
  const outgoing = new Map<string, GraphEdge[]>();
  for (const edge of specEdges(spec)) {
    incoming.set(edge.target, [...(incoming.get(edge.target) ?? []), edge]);
    outgoing.set(edge.source, [...(outgoing.get(edge.source) ?? []), edge]);
  }
  const linked = (a: GraphNode, b: GraphNode) => {
    const out = outgoing.get(a.id) ?? [];
    const into = incoming.get(b.id) ?? [];
    return (
      out.length === 1 &&
      into.length === 1 &&
      out[0].target === b.id &&
      edgeKind(out[0]) === "sequence" &&
      nodeKind(a) === "action" &&
      nodeKind(b) === "action"
    );
  };
  const runs: GraphNode[][] = [];
  let run: GraphNode[] = [];
  for (const node of nodes) {
    if (run.length && linked(run[run.length - 1], node)) run.push(node);
    else {
      if (run.length) runs.push(run);
      run = nodeKind(node) === "action" ? [node] : [];
    }
  }
  if (run.length) runs.push(run);

  const groups: FoldGroup[] = [];
  for (const chain of runs) {
    if (chain.length < minLength) continue;
    const parts = Math.ceil(chain.length / maxLength);
    const size = Math.ceil(chain.length / parts);
    for (let start = 0; start < chain.length; start += size) {
      const part = chain.slice(start, start + size);
      if (part.length < 2) continue;
      groups.push({
        id: `steps-${pad(part[0].index + 1)}-${pad(part[part.length - 1].index + 1)}`,
        nodes: part.map((node) => node.id),
      });
    }
  }
  return { groups };
}
