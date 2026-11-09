// The package's spec types against the schema Flow publishes. Flow's own
// suite (packages/flow/tests/test_program_graph_package.py) checks the same
// from the other side, against its pydantic models.
import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { EDGE_KINDS, NODE_KINDS, SPEC_VERSION, isProgramGraphSpec, isRunTrace } from "../src/index.js";
import { referral } from "./fixtures.js";

const read = (path: string) => readFileSync(new URL(path, import.meta.url), "utf8");
type Def = { properties?: Record<string, unknown>; required?: string[]; enum?: string[] };
const schema = JSON.parse(read("../schema/program-graph-v2.json")) as Def & { $defs: Record<string, Def> };
const source = read("../src/spec.ts");

function tsInterfaces(): Record<string, Record<string, boolean>> {
  const out: Record<string, Record<string, boolean>> = {};
  for (const [, name, body] of source.matchAll(/export interface (\w+) \{([\s\S]*?)\n\}/g)) {
    out[name] = Object.fromEntries([...body.matchAll(/^\s+(\w+)(\??):/gm)].map(([, key, q]) => [key, q === "?"]));
  }
  return out;
}

describe("spec types", () => {
  it("the schema is a byte-identical copy of Flow's", () => {
    expect(read("../schema/program-graph-v2.json")).toBe(read("../../flow/schemas/program-graph-v2.json"));
  });

  it("every interface has the schema's keys, optional exactly when the schema has a default", () => {
    const interfaces = tsInterfaces();
    const defs: Record<string, Def> = { ...schema.$defs, ProgramGraphSpec: schema };
    for (const [name, def] of Object.entries(defs)) {
      if (def.enum) continue;
      const required = new Set(def.required ?? []);
      const expected = Object.fromEntries(Object.keys(def.properties ?? {}).map((key) => [key, !required.has(key)]));
      expect(interfaces[name], name).toEqual(expected);
    }
  });

  it("the kind unions are the schema's enums", () => {
    expect([...NODE_KINDS]).toEqual(schema.$defs.NodeKind.enum);
    expect([...EDGE_KINDS]).toEqual(schema.$defs.EdgeKind.enum);
    expect(SPEC_VERSION).toBe((schema.properties!.spec_version as { default: number }).default);
  });

  it("recognizes a spec and a trace", () => {
    expect(isProgramGraphSpec(referral.spec)).toBe(true);
    expect(isProgramGraphSpec({ nodes: [] })).toBe(false);
    expect(isRunTrace(referral.trace)).toBe(true);
    expect(isRunTrace(referral.spec)).toBe(false);
  });
});
