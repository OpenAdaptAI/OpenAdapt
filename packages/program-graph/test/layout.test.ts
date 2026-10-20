import { describe, expect, it } from "vitest";
import { deriveDisplayGraph, layoutProgramGraph, rankNodes, type GraphLayout } from "../src/index.js";
import { processRecords, referral } from "./fixtures.js";

/** Points along an SVG path made of M/H/V/Q/C commands (corners only). */
function corners(path: string): [number, number][] {
  const tokens = path.match(/[MHVQC]|-?\d+(\.\d+)?/g)!;
  const points: [number, number][] = [];
  let x = 0;
  let y = 0;
  for (let i = 0; i < tokens.length; ) {
    const cmd = tokens[i++];
    const n = () => Number(tokens[i++]);
    if (cmd === "M") [x, y] = [n(), n()];
    else if (cmd === "H") x = n();
    else if (cmd === "V") y = n();
    else if (cmd === "Q") { n(); n(); [x, y] = [n(), n()]; }
    else if (cmd === "C") { n(); n(); n(); n(); [x, y] = [n(), n()]; }
    points.push([x, y]);
  }
  return points;
}

/** True when a lane edge's vertical run passes through a card. */
function laneCrossesCard(layout: GraphLayout): string[] {
  const bad: string[] = [];
  for (const edge of layout.edges.filter((e) => e.lane)) {
    const pts = corners(edge.path);
    for (let i = 1; i < pts.length; i++) {
      const [x0, y0] = pts[i - 1];
      const [x1, y1] = pts[i];
      if (x0 !== x1) continue;
      const lo = Math.min(y0, y1);
      const hi = Math.max(y0, y1);
      for (const node of layout.nodes) {
        if (x0 > node.x && x0 < node.x + node.width && hi > node.y && lo < node.y + node.height) {
          bad.push(`${edge.id} crosses ${node.id}`);
        }
      }
    }
  }
  return bad;
}

describe("rankNodes", () => {
  it("puts a loop body below the loop and the loop's exit below the body", () => {
    const ranks = rankNodes(processRecords);
    expect(Object.fromEntries(ranks)).toEqual({
      open_worklist: 0,
      for_each_record: 1,
      open_record: 2,
      write_update: 3,
      record_complete: 4,
      done: 5,
    });
  });

  it("ranks the 41-step referral body in program order", () => {
    const ranks = rankNodes(referral.spec);
    expect(ranks.get("for_each")).toBe(0);
    expect(ranks.get("for_each::s::step_000")).toBe(1);
    expect(ranks.get("for_each::s::step_040")).toBe(41);
    expect(ranks.get("__loop_done__")).toBe(42);
  });
});

describe("layoutProgramGraph", () => {
  it("routes the loop return right and the loop exit left, clear of every card", () => {
    const layout = layoutProgramGraph(processRecords);
    const byKey = (s: string, t: string) => layout.edges.find((e) => e.source === s && e.target === t)!;
    expect(byKey("record_complete", "for_each_record")).toMatchObject({ backEdge: true, lane: "right" });
    expect(byKey("for_each_record", "done")).toMatchObject({ backEdge: false, lane: "left" });
    expect(byKey("open_record", "write_update")).toMatchObject({ backEdge: false, lane: null });
    expect(laneCrossesCard(layout)).toEqual([]);
  });

  it("gives overlapping lane edges separate lanes", () => {
    const spec = {
      ...processRecords,
      edges: [
        ...(processRecords.edges ?? []),
        { source: "write_update", target: "open_worklist", kind: "exception" as const, label: "retry" },
      ],
    };
    const layout = layoutProgramGraph(spec);
    const right = layout.edges.filter((e) => e.lane === "right").map((e) => e.labelX);
    expect(new Set(right).size).toBe(right.length);
    expect(laneCrossesCard(layout)).toEqual([]);
  });

  it("lays the folded referral out as seven cards and a stop", () => {
    const display = deriveDisplayGraph(referral.spec, { folds: referral.folds, stopExits: referral.folds.stops });
    const layout = layoutProgramGraph(display.spec);
    expect(layout.nodes.map((n) => [n.id, n.rank])).toEqual([
      ["for_each", 0],
      ["name", 1],
      ["birth", 2],
      ["address", 3],
      ["contact", 4],
      ["save", 5],
      ["__loop_done__", 6],
      ["__opg_stop__", 6],
    ]);
    expect(laneCrossesCard(layout)).toEqual([]);
  });

  it("is stable: the same spec lays out to the same geometry", () => {
    expect(layoutProgramGraph(processRecords)).toMatchSnapshot();
  });
});
