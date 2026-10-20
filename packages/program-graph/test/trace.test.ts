import { describe, expect, it } from "vitest";
import { deriveDisplayGraph, haltEvent, isRunTrace, traceStateAt } from "../src/index.js";
import { referral } from "./fixtures.js";

describe("run trace", () => {
  const { trace } = referral;

  it("the fixture is a recorded run of four referrals that stops in the fourth", () => {
    expect(isRunTrace(trace)).toBe(true);
    expect(trace.outcome).toBe("halted");
    const iterations = new Set(trace.events.map((e) => e.iteration));
    expect([...iterations]).toEqual([1, 2, 3, 4]);
    const halt = haltEvent(trace)!;
    expect(halt).toMatchObject({ node: "for_each::s::step_039", iteration: 4, screen_check: "failed" });
    expect(trace.events.at(-1)).toBe(halt);
    const ids = new Set((referral.spec.nodes ?? []).map((n) => n.id));
    for (const event of trace.events) expect(ids.has(event.node)).toBe(true);
  });

  it("every completed referral confirmed all eleven read-back checks", () => {
    const saves = trace.events.filter((e) => e.node === "for_each::s::step_040");
    expect(saves).toHaveLength(3);
    for (const save of saves) expect(save.effect_checks).toEqual({ confirmed: 11, total: 11 });
  });

  it("replays to a cursor: done, active and pending steps", () => {
    const state = traceStateAt(trace, 3);
    expect(state.nodes.get("for_each")?.status).toBe("done");
    expect(state.nodes.get("for_each::s::step_000")?.status).toBe("done");
    expect(state.nodes.get("for_each::s::step_001")?.status).toBe("active");
    expect(state.nodes.get("for_each::s::step_002")).toBeUndefined();
    expect(state.iteration).toBe(1);
    expect(state.latestEdge).toBe("for_each::s::step_000>for_each::s::step_001");
  });

  it("folded cards light up for their members and the loop counts iterations", () => {
    const { ownerOf } = deriveDisplayGraph(referral.spec, { folds: referral.folds });
    const end = traceStateAt(trace, trace.events.length, ownerOf);
    expect(end.finished).toBe(true);
    expect(end.halted?.node).toBe("for_each::s::step_039");
    expect(end.nodes.get("save")?.status).toBe("halted");
    expect(end.nodes.get("for_each")?.iterations).toBe(4);
    expect(end.nodes.get("name")?.status).toBe("done");
    expect(end.taken.has("save>for_each")).toBe(true);
    expect(end.nodes.has("__loop_done__")).toBe(false);
  });

  it("clamps the cursor", () => {
    expect(traceStateAt(trace, -5).cursor).toBe(0);
    expect(traceStateAt(trace, 10_000).cursor).toBe(trace.events.length);
  });
});
