import { useId, useMemo, useState } from "react";
import { layoutQualificationGraph } from "../lib/programGraphLayout";
import type { QualificationNode, QualificationProject } from "../lib/types";

type ProgramView = "program" | "evidence" | "stops";
type QualificationGraph = QualificationProject["graph"];

function displayTitle(node: QualificationNode): string {
  if (/^(click|double click) at \(\d+,\s*\d+\)$/i.test(node.title.trim())) {
    return node.action === "double_click"
      ? "Double-click recorded target"
      : "Click recorded target";
  }
  return node.title;
}

function nodeTone(node: QualificationNode): string {
  if (node.kind === "terminal") return "terminal";
  if (node.risk === "irreversible") return "halt";
  if (node.kind === "branch" || node.kind === "loop") return "branch";
  if (node.identity?.armed || node.effects.length > 0) return "governed";
  return "default";
}

function EvidenceState({
  label,
  state,
  value,
}: {
  label: string;
  state: "present" | "warn" | "none";
  value: string;
}) {
  return (
    <div className="program-evidence-state" data-state={state}>
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function NodeInspector({ node }: { node: QualificationNode }) {
  const targetRungs = node.resolution?.rungs.filter((rung) => rung.present) ?? [];
  return (
    <aside className="program-inspector" aria-label="Selected program step">
      <div className="program-inspector-head">
        <span>Selected step</span>
        <code>{node.kind === "terminal" ? "END" : String(node.index + 1).padStart(2, "0")}</code>
      </div>
      <h3>{displayTitle(node)}</h3>
      <p className="mono">{node.id}</p>

      <div className="program-inspector-lanes">
        <EvidenceState
          label="Resolve"
          state={targetRungs.length ? "present" : "none"}
          value={targetRungs.length ? `${targetRungs.length} evidence types` : "Not applicable"}
        />
        <EvidenceState
          label="Identity"
          state={node.identity?.armed ? "present" : node.identity?.applicable ? "warn" : "none"}
          value={node.identity?.armed ? "Armed" : node.identity?.applicable ? "Not armed" : "Not applicable"}
        />
        <EvidenceState
          label="Screen"
          state={node.postconditions.length ? "present" : "none"}
          value={node.postconditions.length ? `${node.postconditions.length} checks` : "Not declared"}
        />
        <EvidenceState
          label="Effect"
          state={node.effects.length ? "present" : "none"}
          value={node.effects.length ? `${node.effects.length} independent checks` : "Not declared"}
        />
      </div>

      {node.resolution && (
        <div className="program-ladder">
          <span>Recorded resolution evidence</span>
          <ol>
            {node.resolution.rungs.map((rung) => (
              <li
                key={rung.name}
                data-present={rung.present || undefined}
                data-top={rung.name === node.resolution?.top_rung || undefined}
              >
                <i />
                {rung.label}
              </li>
            ))}
          </ol>
        </div>
      )}

      {node.halts.length > 0 && (
        <div className="program-stop-rule">
          <span>Stops before acting if</span>
          <strong>{node.halts.join("; ")}</strong>
        </div>
      )}
    </aside>
  );
}

function ProgramMap({ graph }: { graph: QualificationGraph }) {
  const layout = useMemo(() => layoutQualificationGraph(graph), [graph]);
  const [selectedId, setSelectedId] = useState(graph.nodes[0]?.id ?? "");
  const selected = graph.nodes.find((node) => node.id === selectedId) ?? graph.nodes[0];
  const markerId = useId().replaceAll(":", "");

  if (!selected) return <p className="page-sub">The compiled graph is empty.</p>;

  return (
    <div className="program-workbench-grid">
      <div className="program-canvas-shell">
        <div className="program-canvas-head">
          <span>Compiled topology</span>
          <span>{graph.nodes.length} nodes · {graph.edges.length} exact edges</span>
        </div>
        <div className="program-canvas-viewport">
          <div
            className="program-canvas"
            style={{ width: layout.width, height: layout.height }}
          >
            <svg
              className="program-edges"
              viewBox={`0 0 ${layout.width} ${layout.height}`}
              width={layout.width}
              height={layout.height}
              role="img"
              aria-label="Exact compiled program edges"
            >
              <defs>
                <marker
                  id={markerId}
                  markerWidth="8"
                  markerHeight="8"
                  refX="7"
                  refY="4"
                  orient="auto"
                  markerUnits="strokeWidth"
                >
                  <path d="M 0 0 L 8 4 L 0 8 z" />
                </marker>
              </defs>
              {layout.edges.map((edge) => (
                <g key={edge.id} data-kind={edge.kind} data-back-edge={edge.backEdge || undefined}>
                  <path d={edge.path} markerEnd={`url(#${markerId})`} />
                  {edge.label && <text x={edge.labelX} y={edge.labelY}>{edge.label}</text>}
                </g>
              ))}
            </svg>
            {layout.nodes.map((point) => {
              const node = graph.nodes.find((candidate) => candidate.id === point.id)!;
              const isSelected = node.id === selected.id;
              return (
                <button
                  key={node.id}
                  type="button"
                  className="program-node"
                  data-tone={nodeTone(node)}
                  data-selected={isSelected || undefined}
                  style={{
                    left: point.x,
                    top: point.y,
                    width: point.width,
                    height: point.height,
                  }}
                  aria-pressed={isSelected}
                  onClick={() => setSelectedId(node.id)}
                >
                  <span className="program-node-index">
                    {node.kind === "terminal" ? "END" : String(node.index + 1).padStart(2, "0")}
                  </span>
                  <span className="program-node-copy">
                    <small>{node.kind.replaceAll("_", " ")}</small>
                    <strong>{displayTitle(node)}</strong>
                  </span>
                  <span className="program-node-signals" aria-label="Step controls">
                    {node.identity?.armed && <i title="Identity gate">I</i>}
                    {node.effects.length > 0 && <i title="Effect check">E</i>}
                    {node.halts.length > 0 && <i title="Stop rules">H</i>}
                  </span>
                </button>
              );
            })}
          </div>
        </div>
        <p className="program-canvas-note">
          The map follows the emitted edge targets. It keeps each loop and exception route visible.
        </p>
      </div>
      <NodeInspector node={selected} />
    </div>
  );
}

function EvidenceMatrix({ graph }: { graph: QualificationGraph }) {
  return (
    <div className="program-matrix-scroll">
      <table className="program-matrix">
        <thead>
          <tr>
            <th>Program step</th>
            <th>Target evidence</th>
            <th>Identity gate</th>
            <th>Screen check</th>
            <th>Effect check</th>
          </tr>
        </thead>
        <tbody>
          {graph.nodes.map((node) => {
            const rungs = node.resolution?.rungs.filter((rung) => rung.present) ?? [];
            return (
              <tr key={`evidence-${node.id}`}>
                <td><strong>{displayTitle(node)}</strong><small className="mono">{node.id}</small></td>
                <td data-state={rungs.length ? "present" : "none"}>{rungs.length ? rungs.map((rung) => rung.label).join(", ") : "Not applicable"}</td>
                <td data-state={node.identity?.armed ? "present" : node.identity?.applicable ? "warn" : "none"}>{node.identity?.armed ? "Armed" : node.identity?.applicable ? "Not armed" : "Not applicable"}</td>
                <td data-state={node.postconditions.length ? "present" : "none"}>{node.postconditions.length ? `${node.postconditions.length} declared` : "Not declared"}</td>
                <td data-state={node.effects.length ? "present" : "none"}>{node.effects.length ? `${node.effects.length} independent` : "Not declared"}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
      <p className="program-boundary-note">
        These rows show the declared contract. They do not report a live verdict. Desktop binds live evidence only after the runtime retains an exact run trace.
      </p>
    </div>
  );
}

function StopRules({ graph }: { graph: QualificationGraph }) {
  const rules = graph.nodes.filter((node) => node.halts.length > 0 || node.kind === "terminal");
  return (
    <div className="program-stop-list">
      {rules.length === 0 ? (
        <p className="page-sub">This graph does not declare a stop rule or terminal outcome.</p>
      ) : rules.map((node) => (
        <article key={`stop-${node.id}`}>
          <span className="mono">{node.kind === "terminal" ? "terminal" : `step ${node.index + 1}`}</span>
          <strong>{displayTitle(node)}</strong>
          <p>{node.halts.length ? node.halts.join("; ") : "Declared terminal program state."}</p>
        </article>
      ))}
    </div>
  );
}

export function ProgramWorkbench({ graph }: { graph: QualificationGraph }) {
  const [view, setView] = useState<ProgramView>("program");
  return (
    <div className="program-workbench surface-phosphor">
      <div className="program-workbench-topbar">
        <div>
          <span className="program-kicker">Local compiled program</span>
          <strong>{graph.bundle.name}</strong>
        </div>
        <span className="program-trace-state"><i /> No live trace bound</span>
      </div>
      <div className="program-tabs" role="tablist" aria-label="Program workbench views">
        {([
          ["program", "Program map"],
          ["evidence", "Evidence lanes"],
          ["stops", "Stop rules"],
        ] as const).map(([id, label]) => (
          <button
            key={id}
            type="button"
            role="tab"
            aria-selected={view === id}
            onClick={() => setView(id)}
          >
            {label}
          </button>
        ))}
      </div>
      {view === "program" && <ProgramMap graph={graph} />}
      {view === "evidence" && <EvidenceMatrix graph={graph} />}
      {view === "stops" && <StopRules graph={graph} />}
    </div>
  );
}
