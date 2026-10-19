import type { QualificationProject } from "./types";

type QualificationGraph = QualificationProject["graph"];

export interface ProgramGraphNodeLayout {
  id: string;
  x: number;
  y: number;
  width: number;
  height: number;
  rank: number;
}

export interface ProgramGraphEdgeLayout {
  id: string;
  kind: string;
  label: string;
  path: string;
  labelX: number;
  labelY: number;
  backEdge: boolean;
}

export interface ProgramGraphLayout {
  width: number;
  height: number;
  nodes: ProgramGraphNodeLayout[];
  edges: ProgramGraphEdgeLayout[];
}

const NODE_WIDTH = 194;
const NODE_HEIGHT = 70;
const X_GAP = 42;
const Y_GAP = 48;
const MARGIN = 34;

function graphRanks(graph: QualificationGraph): Map<string, number> {
  const declaredIndex = new Map(
    graph.nodes.map((node, position) => [node.id, node.index ?? position]),
  );
  const incoming = new Map(graph.nodes.map((node) => [node.id, 0]));
  const outgoing = new Map(
    graph.nodes.map((node) => [node.id, [] as QualificationGraph["edges"]]),
  );

  for (const edge of graph.edges) {
    const sourceIndex = declaredIndex.get(edge.source);
    const targetIndex = declaredIndex.get(edge.target);
    if (sourceIndex == null || targetIndex == null) continue;
    if (edge.kind === "loop_body" || targetIndex <= sourceIndex) continue;
    incoming.set(edge.target, (incoming.get(edge.target) ?? 0) + 1);
    outgoing.get(edge.source)?.push(edge);
  }

  const queue = graph.nodes
    .filter((node) => (incoming.get(node.id) ?? 0) === 0)
    .sort((a, b) => a.index - b.index);
  const rank = new Map(graph.nodes.map((node) => [node.id, 0]));
  const visited = new Set<string>();

  while (queue.length > 0) {
    const node = queue.shift()!;
    visited.add(node.id);
    for (const edge of outgoing.get(node.id) ?? []) {
      rank.set(
        edge.target,
        Math.max(rank.get(edge.target) ?? 0, (rank.get(node.id) ?? 0) + 1),
      );
      incoming.set(edge.target, (incoming.get(edge.target) ?? 1) - 1);
      if (incoming.get(edge.target) === 0) {
        const target = graph.nodes.find((candidate) => candidate.id === edge.target);
        if (target) queue.push(target);
        queue.sort((a, b) => a.index - b.index);
      }
    }
  }

  for (const node of graph.nodes) {
    if (!visited.has(node.id)) {
      rank.set(node.id, Math.max(rank.get(node.id) ?? 0, node.index));
    }
  }
  return rank;
}

export function layoutQualificationGraph(
  graph: QualificationGraph,
): ProgramGraphLayout {
  const ranks = graphRanks(graph);
  const layers = new Map<number, QualificationGraph["nodes"]>();
  for (const node of graph.nodes) {
    const rank = ranks.get(node.id) ?? 0;
    const layer = layers.get(rank) ?? [];
    layer.push(node);
    layer.sort((a, b) => a.index - b.index);
    layers.set(rank, layer);
  }

  const maxLayerSize = Math.max(
    1,
    ...[...layers.values()].map((layer) => layer.length),
  );
  const width = Math.max(
    660,
    MARGIN * 2 + maxLayerSize * NODE_WIDTH + (maxLayerSize - 1) * X_GAP,
  );
  const maxRank = Math.max(0, ...ranks.values());
  const height = MARGIN * 2 + (maxRank + 1) * NODE_HEIGHT + maxRank * Y_GAP;
  const nodes: ProgramGraphNodeLayout[] = [];

  for (const [rank, layer] of [...layers.entries()].sort((a, b) => a[0] - b[0])) {
    const layerWidth =
      layer.length * NODE_WIDTH + Math.max(0, layer.length - 1) * X_GAP;
    const startX = (width - layerWidth) / 2;
    layer.forEach((node, position) => {
      nodes.push({
        id: node.id,
        x: startX + position * (NODE_WIDTH + X_GAP),
        y: MARGIN + rank * (NODE_HEIGHT + Y_GAP),
        width: NODE_WIDTH,
        height: NODE_HEIGHT,
        rank,
      });
    });
  }

  const nodeById = new Map(nodes.map((node) => [node.id, node]));
  const indexById = new Map(graph.nodes.map((node) => [node.id, node.index]));
  const edges: ProgramGraphEdgeLayout[] = [];

  graph.edges.forEach((edge, edgeIndex) => {
    const source = nodeById.get(edge.source);
    const target = nodeById.get(edge.target);
    if (!source || !target) return;
    const backEdge =
      edge.kind === "loop_body" ||
      (indexById.get(edge.target) ?? 0) <= (indexById.get(edge.source) ?? 0);
    const sourceX = source.x + source.width / 2;
    const sourceY = backEdge
      ? source.y + source.height / 2
      : source.y + source.height;
    const targetX = target.x + target.width / 2;
    const targetY = backEdge ? target.y + target.height / 2 : target.y;
    let path: string;
    let labelX: number;
    let labelY: number;

    if (backEdge) {
      const sideX = width - 16 - (edgeIndex % 3) * 12;
      path = `M ${sourceX} ${sourceY} C ${sideX} ${sourceY}, ${sideX} ${targetY}, ${targetX} ${targetY}`;
      labelX = sideX - 7;
      labelY = (sourceY + targetY) / 2;
    } else {
      const middleY = (sourceY + targetY) / 2;
      path = `M ${sourceX} ${sourceY} C ${sourceX} ${middleY}, ${targetX} ${middleY}, ${targetX} ${targetY}`;
      labelX = (sourceX + targetX) / 2;
      labelY = middleY - 6;
    }

    edges.push({
      id: `${edge.source}:${edge.target}:${edge.kind}:${edgeIndex}`,
      kind: edge.kind,
      label: edge.label,
      path,
      labelX,
      labelY,
      backEdge,
    });
  });

  return { width, height, nodes, edges };
}
