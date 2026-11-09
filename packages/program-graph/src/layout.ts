// Layout and edge routing for the program map.
//
// Ranks come from the forward edges only. An edge is forward when it points to
// a later step (a higher `index`); every other edge closes a cycle, such as a
// loop's return, and is drawn as a back edge. A loop's body ranks below the
// loop, and the loop's exit ranks below the whole body, so the map reads top to
// bottom in program order.
//
// Routing keeps edges off the cards:
// - an edge between adjacent ranks is a curve from the bottom of one card to
//   the top of the next;
// - a forward edge that skips ranks (a loop exit, a branch to the end) runs in
//   a lane to the left of the cards it passes;
// - a back edge (a loop's return) runs in a lane to the right.
// Lanes are allocated so two edges never share a lane where they overlap.

import { edgeKind, specEdges, specNodes, nodeKind, type EdgeKind, type GraphEdge, type GraphNode, type ProgramGraphSpec } from "./spec.js";

export interface LayoutOptions {
  nodeWidth?: number;
  nodeHeight?: number;
  xGap?: number;
  yGap?: number;
  margin?: number;
  minWidth?: number;
  /** Distance between two lanes on one side. */
  laneGap?: number;
  /** Distance from the cards to the first lane. */
  laneInset?: number;
}

export interface NodeLayout {
  id: string;
  x: number;
  y: number;
  width: number;
  height: number;
  rank: number;
}

export type Lane = "left" | "right" | null;

export interface EdgeLayout {
  id: string;
  source: string;
  target: string;
  kind: EdgeKind;
  label: string;
  path: string;
  labelX: number;
  labelY: number;
  /** Lane labels run along the lane, rotated a quarter turn. */
  labelRotate: boolean;
  /** True for an edge that returns to an earlier step. */
  backEdge: boolean;
  lane: Lane;
}

export interface GraphLayout {
  width: number;
  height: number;
  nodes: NodeLayout[];
  edges: EdgeLayout[];
}

export const DEFAULT_LAYOUT: Required<LayoutOptions> = {
  nodeWidth: 194,
  nodeHeight: 70,
  xGap: 42,
  yGap: 48,
  margin: 34,
  minWidth: 0,
  laneGap: 14,
  laneInset: 22,
};

const R = 6; // corner radius of a lane route

function order(nodes: GraphNode[]): Map<string, number> {
  return new Map(nodes.map((node, position) => [node.id, Number.isFinite(node.index) ? node.index : position]));
}

/** True when the edge returns to the same or an earlier step. */
export function isBackEdge(edge: GraphEdge, position: Map<string, number>): boolean {
  const s = position.get(edge.source);
  const t = position.get(edge.target);
  if (s === undefined || t === undefined) return false;
  return t <= s;
}

/** Rank every node: forward edges point down, loop exits wait for the body. */
export function rankNodes(spec: ProgramGraphSpec): Map<string, number> {
  const nodes = [...specNodes(spec)];
  const position = order(nodes);
  nodes.sort((a, b) => position.get(a.id)! - position.get(b.id)!);
  const ids = new Set(nodes.map((node) => node.id));
  const edges = specEdges(spec).filter((edge) => ids.has(edge.source) && ids.has(edge.target));
  const forward = edges.filter((edge) => !isBackEdge(edge, position));
  const into = new Map<string, GraphEdge[]>(nodes.map((node) => [node.id, []]));
  const out = new Map<string, GraphEdge[]>(nodes.map((node) => [node.id, []]));
  for (const edge of forward) {
    into.get(edge.target)!.push(edge);
    out.get(edge.source)!.push(edge);
  }

  // Each loop's body: everything reachable from its body edges, without
  // passing back through the loop or one of its exits, that can get back to
  // the loop. A stop or an end the body can reach is not in the body.
  const backTo = (target: string) => {
    const seen = new Set([target]);
    const queue = [target];
    while (queue.length) {
      const id = queue.shift()!;
      for (const edge of edges) {
        if (edge.target === id && !seen.has(edge.source)) {
          seen.add(edge.source);
          queue.push(edge.source);
        }
      }
    }
    return seen;
  };
  const loops: { exits: string[]; body: Set<string> }[] = [];
  for (const node of nodes) {
    const bodyEdges = out.get(node.id)!.filter((edge) => edgeKind(edge) === "loop_body");
    if (nodeKind(node) !== "loop" && bodyEdges.length === 0) continue;
    const exits = out.get(node.id)!.filter((edge) => edgeKind(edge) !== "loop_body").map((edge) => edge.target);
    if (bodyEdges.length === 0 || exits.length === 0) continue;
    const stop = new Set([node.id, ...exits]);
    const returns = backTo(node.id);
    const body = new Set<string>();
    const queue = bodyEdges.map((edge) => edge.target).filter((id) => !stop.has(id));
    while (queue.length) {
      const id = queue.shift()!;
      if (body.has(id)) continue;
      body.add(id);
      for (const edge of out.get(id)!) if (!stop.has(edge.target)) queue.push(edge.target);
    }
    for (const id of body) if (!returns.has(id)) body.delete(id);
    loops.push({ exits, body });
  }

  const rank = new Map(nodes.map((node) => [node.id, 0]));
  // Forward edges always point to a later step, so program order is a
  // topological order. Loop constraints can point backwards in that order, so
  // repeat until nothing moves (bounded by the node count).
  for (let pass = 0; pass <= nodes.length; pass += 1) {
    let moved = false;
    const raise = (id: string, value: number) => {
      if (value > rank.get(id)!) {
        rank.set(id, value);
        moved = true;
      }
    };
    for (const node of nodes) {
      for (const edge of into.get(node.id)!) raise(node.id, rank.get(edge.source)! + 1);
    }
    for (const loop of loops) {
      if (loop.body.size === 0) continue;
      const deepest = Math.max(...[...loop.body].map((id) => rank.get(id)!));
      for (const exit of loop.exits) raise(exit, deepest + 1);
    }
    if (!moved) break;
  }
  return rank;
}

interface LaneSlot {
  from: number;
  to: number;
}

function allocateLane(slots: LaneSlot[][], from: number, to: number): number {
  const lo = Math.min(from, to);
  const hi = Math.max(from, to);
  for (let lane = 0; ; lane += 1) {
    slots[lane] ??= [];
    if (slots[lane].every((slot) => hi < slot.from || lo > slot.to)) {
      slots[lane].push({ from: lo, to: hi });
      return lane;
    }
  }
}

export function layoutProgramGraph(spec: ProgramGraphSpec, options: LayoutOptions = {}): GraphLayout {
  const o = { ...DEFAULT_LAYOUT, ...options };
  const nodes = specNodes(spec);
  const position = order(nodes);
  const ranks = rankNodes(spec);
  const ids = new Set(nodes.map((node) => node.id));
  const edges = specEdges(spec).filter((edge) => ids.has(edge.source) && ids.has(edge.target));

  const layers = new Map<number, GraphNode[]>();
  for (const node of nodes) {
    const r = ranks.get(node.id) ?? 0;
    const layer = layers.get(r) ?? [];
    layer.push(node);
    layers.set(r, layer);
  }
  for (const layer of layers.values()) layer.sort((a, b) => position.get(a.id)! - position.get(b.id)!);
  const maxRank = Math.max(0, ...ranks.values());
  const maxLayer = Math.max(1, ...[...layers.values()].map((layer) => layer.length));
  const rowY = (r: number) => o.margin + r * (o.nodeHeight + o.yGap);

  // Classify edges first: lanes need room beside the cards.
  type Plan = { edge: GraphEdge; index: number; back: boolean; lane: Lane; slot: number };
  const plans: Plan[] = edges.map((edge, index) => {
    const back = isBackEdge(edge, position);
    const span = (ranks.get(edge.target) ?? 0) - (ranks.get(edge.source) ?? 0);
    const lane: Lane = back ? "right" : span === 1 ? null : "left";
    return { edge, index, back, lane, slot: 0 };
  });
  const leftSlots: LaneSlot[][] = [];
  const rightSlots: LaneSlot[][] = [];
  for (const plan of plans) {
    if (plan.lane === null) continue;
    const from = ranks.get(plan.edge.source) ?? 0;
    const to = ranks.get(plan.edge.target) ?? 0;
    plan.slot = allocateLane(plan.lane === "left" ? leftSlots : rightSlots, from, to);
  }
  const laneRoom = (count: number) => (count ? o.laneInset + (count - 1) * o.laneGap + 12 : 0);
  const contentWidth = maxLayer * o.nodeWidth + (maxLayer - 1) * o.xGap;
  const width = Math.max(
    o.minWidth,
    o.margin * 2 + contentWidth + 2 * Math.max(laneRoom(leftSlots.length), laneRoom(rightSlots.length)),
  );
  const height = o.margin * 2 + (maxRank + 1) * o.nodeHeight + maxRank * o.yGap;

  const placed: NodeLayout[] = [];
  for (const [r, layer] of [...layers.entries()].sort((a, b) => a[0] - b[0])) {
    const layerWidth = layer.length * o.nodeWidth + (layer.length - 1) * o.xGap;
    const startX = (width - layerWidth) / 2;
    layer.forEach((node, i) => {
      placed.push({
        id: node.id,
        x: startX + i * (o.nodeWidth + o.xGap),
        y: rowY(r),
        width: o.nodeWidth,
        height: o.nodeHeight,
        rank: r,
      });
    });
  }
  const at = new Map(placed.map((node) => [node.id, node]));

  // The lanes sit outside every card in the ranks an edge spans.
  const extent = (from: number, to: number) => {
    const lo = Math.min(from, to);
    const hi = Math.max(from, to);
    const spanned = placed.filter((node) => node.rank >= lo && node.rank <= hi);
    return {
      left: Math.min(...spanned.map((node) => node.x)),
      right: Math.max(...spanned.map((node) => node.x + node.width)),
    };
  };

  const routed: EdgeLayout[] = plans.map(({ edge, index, back, lane, slot }) => {
    const s = at.get(edge.source)!;
    const t = at.get(edge.target)!;
    const kind = edgeKind(edge);
    const label = edge.label ?? "";
    const id = `${edge.source}:${edge.target}:${kind}:${index}`;
    const sy = s.y + s.height / 2;
    const ty = t.y + t.height / 2;
    if (lane === null) {
      const sx = s.x + s.width / 2;
      const tx = t.x + t.width / 2;
      const y0 = s.y + s.height;
      const y1 = t.y;
      const mid = (y0 + y1) / 2;
      return {
        id, source: edge.source, target: edge.target, kind, label,
        path: `M ${sx} ${y0} C ${sx} ${mid}, ${tx} ${mid}, ${tx} ${y1}`,
        labelX: (sx + tx) / 2 + 6,
        labelY: mid + 3,
        labelRotate: false,
        backEdge: back,
        lane,
      };
    }
    const span = extent(s.rank, t.rank);
    if (lane === "right") {
      const x = span.right + o.laneInset + slot * o.laneGap;
      const x0 = s.x + s.width;
      const x1 = t.x + t.width;
      const up = ty < sy;
      const r = Math.min(R, Math.abs(sy - ty) / 2 || R);
      const path = edge.source === edge.target
        ? `M ${x0} ${sy - 10} H ${x - R} Q ${x} ${sy - 10} ${x} ${sy - 10 + R} V ${sy + 10 - R} Q ${x} ${sy + 10} ${x - R} ${sy + 10} H ${x1}`
        : `M ${x0} ${sy} H ${x - r} Q ${x} ${sy} ${x} ${sy + (up ? -r : r)} V ${ty + (up ? r : -r)} Q ${x} ${ty} ${x - r} ${ty} H ${x1}`;
      return {
        id, source: edge.source, target: edge.target, kind, label, path,
        labelX: x + 8,
        labelY: (sy + ty) / 2,
        labelRotate: true,
        backEdge: back,
        lane,
      };
    }
    const x = span.left - o.laneInset - slot * o.laneGap;
    const x0 = s.x;
    const x1 = t.x;
    const r = Math.min(R, Math.abs(ty - sy) / 2 || R);
    const down = ty > sy;
    return {
      id, source: edge.source, target: edge.target, kind, label,
      path: `M ${x0} ${sy} H ${x + r} Q ${x} ${sy} ${x} ${sy + (down ? r : -r)} V ${ty + (down ? -r : r)} Q ${x} ${ty} ${x + r} ${ty} H ${x1}`,
      labelX: x - 8,
      labelY: (sy + ty) / 2,
      labelRotate: true,
      backEdge: back,
      lane,
    };
  });

  return { width, height, nodes: placed, edges: routed };
}
