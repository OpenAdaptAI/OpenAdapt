import type { QualificationProject } from "../lib/types";
import { ProgramWorkbench } from "./ProgramWorkbench";

const graph = {
  bundle: {
    name: "Process incoming records",
    action_count: 4,
    irreversible_count: 1,
    identity_armed_count: 1,
    identity_unarmed_count: 0,
    effect_count: 1,
    encrypted: false,
    provenance: { content_digest: "7a4c1b2d9e0f" },
  },
  nodes: [
    {
      id: "open_worklist",
      index: 0,
      kind: "action",
      title: "Open worklist",
      action: "click",
      resolution: {
        top_rung: "structural",
        rungs: [
          { name: "structural", label: "Structural", present: true, detail: "" },
          { name: "template", label: "Template", present: true, detail: "" },
          { name: "ocr", label: "OCR anchor", present: false, detail: "" },
          { name: "geometry", label: "Geometry", present: false, detail: "" },
        ],
      },
      risk: "reversible",
      identity: null,
      effects: [],
      postconditions: ["worklist is visible"],
      halts: ["the worklist cannot be resolved uniquely"],
      badges: [],
    },
    {
      id: "for_each_record",
      index: 1,
      kind: "loop",
      title: "For each record",
      action: null,
      resolution: null,
      risk: null,
      identity: null,
      effects: [],
      postconditions: [],
      halts: [],
      badges: [],
    },
    {
      id: "open_record",
      index: 2,
      kind: "action",
      title: "Open current record",
      action: "click",
      resolution: {
        top_rung: "template",
        rungs: [
          { name: "structural", label: "Structural", present: false, detail: "" },
          { name: "template", label: "Template", present: true, detail: "" },
          { name: "ocr", label: "OCR anchor", present: true, detail: "" },
          { name: "geometry", label: "Geometry", present: true, detail: "" },
        ],
      },
      risk: "reversible",
      identity: {
        applicable: true,
        armed: true,
        phi_free: true,
        has_structured: true,
        has_identifier_crop: true,
      },
      effects: [],
      postconditions: ["record view is visible"],
      halts: ["identity does not match", "target evidence is ambiguous"],
      badges: ["identity armed"],
    },
    {
      id: "write_update",
      index: 3,
      kind: "action",
      title: "Write declared update",
      action: "type",
      resolution: {
        top_rung: "structural",
        rungs: [
          { name: "structural", label: "Structural", present: true, detail: "" },
          { name: "template", label: "Template", present: true, detail: "" },
          { name: "ocr", label: "OCR anchor", present: false, detail: "" },
          { name: "geometry", label: "Geometry", present: false, detail: "" },
        ],
      },
      risk: "irreversible",
      identity: {
        applicable: true,
        armed: true,
        phi_free: true,
        has_structured: true,
        has_identifier_crop: true,
      },
      effects: [
        { kind: "field_equals", risk: "irreversible", needs_operator_confirmation: false },
      ],
      postconditions: ["confirmation is visible"],
      halts: ["fresh frame changed", "effect cannot be verified independently"],
      badges: ["consequential"],
    },
    {
      id: "record_complete",
      index: 4,
      kind: "branch",
      title: "More records?",
      action: null,
      resolution: null,
      risk: null,
      identity: null,
      effects: [],
      postconditions: [],
      halts: [],
      badges: [],
    },
    {
      id: "done",
      index: 5,
      kind: "terminal",
      title: "End of declared steps",
      action: null,
      resolution: null,
      risk: null,
      identity: null,
      effects: [],
      postconditions: [],
      halts: [],
      badges: [],
    },
  ],
  edges: [
    { source: "open_worklist", target: "for_each_record", kind: "next", label: "ready" },
    { source: "for_each_record", target: "open_record", kind: "loop_body", label: "next record" },
    { source: "for_each_record", target: "done", kind: "loop_exit", label: "worklist empty" },
    { source: "open_record", target: "write_update", kind: "next", label: "identity verified" },
    { source: "write_update", target: "record_complete", kind: "next", label: "effect verified" },
    { source: "record_complete", target: "for_each_record", kind: "branch", label: "more" },
    { source: "record_complete", target: "done", kind: "branch", label: "complete" },
  ],
} as QualificationProject["graph"];

export function ProgramWorkbenchPreview() {
  return (
    <div className="app">
      <header className="desktop-shell">
        <div className="desktop-shell-inner">
          <div className="product-brand product-brand-static">
            <span className="brand-open">Open</span>
            <span className="brand-adapt">Adapt</span>
            <span className="brand-product">Desktop</span>
          </div>
          <div className="desktop-shell-spacer" />
          <div className="desktop-status"><span><i className="status-dot ok" /><strong>Engine ready</strong></span></div>
        </div>
      </header>
      <main>
        <div className="content">
          <div className="page-head">
            <div className="titles">
              <p className="eyebrow">Qualification</p>
              <h1>Process incoming records</h1>
              <span className="page-sub mono">bundle 7a4c1b2d9e0f</span>
            </div>
          </div>
          <div className="card">
            <div className="card-head">
              <p className="eyebrow">Program workbench</p>
              <h2>Compiled structure and evidence contract</h2>
              <span className="page-sub">Inspect the exact topology, recorded target evidence, identity gates, independent checks, and stop rules before you qualify this version.</span>
            </div>
            <ProgramWorkbench graph={graph} />
          </div>
        </div>
      </main>
    </div>
  );
}
