import {
  useCallback,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type KeyboardEvent,
  type ReactNode,
} from "react";
import { deriveDisplayGraph, STOP_NODE_ID, type DisplayGraph, type FoldInfo, type FoldSpec, type StopExit } from "./fold.js";
import { layoutProgramGraph, type EdgeLayout, type LayoutOptions } from "./layout.js";
import {
  displayTitle,
  evidenceLanes,
  kindLabel,
  nodeDescription,
  nodeTone,
  stepChip,
  type LaneState,
} from "./present.js";
import { nodeEffects, nodeHalts, nodeKind, specEdges, specNodes, type GraphNode, type ProgramGraphSpec } from "./spec.js";
import { haltEvent, playbackDelay, traceStateAt, type NodeRunState, type RunTrace, type TraceState } from "./trace.js";

export type ProgramView = "program" | "evidence" | "stops";

export interface ProgramWorkbenchProps {
  /** The program-graph spec, as `openadapt-flow visualize --format json` emits it. */
  spec: ProgramGraphSpec;
  /** Heading over the map. Defaults to the bundle name. */
  title?: string;
  /** Small label over the heading. */
  kicker?: string;
  /** Show runs of steps as one card each; a viewer can unfold any card. */
  folds?: FoldSpec | null;
  /** Draw these stop rules as exits to a "Stop the run" card. */
  stopExits?: StopExit[] | null;
  /** A recorded run to play over the map. */
  trace?: RunTrace | null;
  /** Start playing the trace on mount (ignored when the viewer prefers reduced motion). */
  autoPlay?: boolean;
  /** Trace events applied on mount: 0 shows the run before it starts. */
  initialCursor?: number;
  /** Playback speed: recorded time divided by this. */
  speed?: number;
  /** Word for one loop iteration in the player, such as "referral". */
  iterationNoun?: string;
  /** Tabs to show, in order. */
  views?: ProgramView[];
  initialView?: ProgramView;
  /** Node selected on mount. Defaults to the first node. */
  initialSelectedId?: string;
  onSelect?: (nodeId: string) => void;
  /** Override a node's display title (for example, a public projection). */
  titleFor?: (node: GraphNode) => string;
  /** Override the notes under the map and the evidence table; null hides one. */
  notes?: { canvas?: string | null; evidence?: string | null };
  layout?: LayoutOptions;
  /** "dark" (default) or "light". Any --opg-* variable can also be set by the host. */
  theme?: "dark" | "light";
  className?: string;
  style?: CSSProperties;
}

const VIEW_LABEL: Record<ProgramView, string> = {
  program: "Program map",
  evidence: "Evidence lanes",
  stops: "Stop rules",
};

const DEFAULT_CANVAS_NOTE = "The map follows the emitted edge targets. It keeps each loop and exception route visible.";
const DEFAULT_EVIDENCE_NOTE = "These rows show the declared contract. They do not report a live verdict.";

const pad = (n: number) => String(n).padStart(2, "0");

function usePrefersReducedMotion(): boolean {
  const [reduced, setReduced] = useState(false);
  useEffect(() => {
    if (typeof window === "undefined" || !window.matchMedia) return;
    const query = window.matchMedia("(prefers-reduced-motion: reduce)");
    setReduced(query.matches);
    const change = () => setReduced(query.matches);
    query.addEventListener?.("change", change);
    return () => query.removeEventListener?.("change", change);
  }, []);
  return reduced;
}

function EvidenceState({ label, state, value }: { label: string; state: LaneState; value: string }) {
  return (
    <div className="opg-evidence-state" data-state={state}>
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function chipFor(node: GraphNode, fold: FoldInfo | undefined): string {
  if (fold) return fold.first === fold.last ? pad(fold.first) : `${pad(fold.first)}–${pad(fold.last)}`;
  return stepChip(node);
}

function RunFacts({ state }: { state: NodeRunState }) {
  const last = state.last;
  if (!last) return null;
  const rows: [string, string, LaneState][] = [];
  rows.push([
    "Reached",
    state.iterations > 1 ? `${state.iterations} iterations` : state.visits > 1 ? `${state.visits} times` : "Once",
    "none",
  ]);
  if (last.rung) rows.push(["Found by", last.rung, "present"]);
  if (last.identity) rows.push(["Right record", last.identity === "verified" ? "Verified" : "Not verified", last.identity === "verified" ? "present" : "warn"]);
  if (last.screen_check) rows.push(["Screen check", last.screen_check === "passed" ? "Passed" : "Failed", last.screen_check === "passed" ? "present" : "warn"]);
  if (last.effect_checks) {
    rows.push([
      "Read-back",
      `${last.effect_checks.confirmed} of ${last.effect_checks.total} confirmed`,
      last.effect_checks.confirmed === last.effect_checks.total ? "present" : "warn",
    ]);
  } else if (last.effect) {
    rows.push(["Read-back", last.effect, last.effect === "confirmed" ? "present" : "warn"]);
  }
  if (last.duration_ms != null) rows.push(["Took", `${(last.duration_ms / 1000).toFixed(1)} s`, "none"]);
  return (
    <div className="opg-run-facts" aria-label="This run at this step">
      <span>This run</span>
      {rows.map(([label, value, s]) => (
        <EvidenceState key={label} label={label} state={s} value={value} />
      ))}
      {state.status === "halted" && last.reason && (
        <div className="opg-stop-rule" data-run="halted">
          <span>Stopped here</span>
          <strong>{last.reason}</strong>
        </div>
      )}
    </div>
  );
}

function NodeInspector({
  node,
  fold,
  partOf,
  run,
  titleFor,
  onUnfold,
  onRefold,
  onPickMember,
}: {
  node: GraphNode;
  fold?: FoldInfo;
  partOf?: { id: string; title: string };
  run?: NodeRunState;
  titleFor: (node: GraphNode) => string;
  onUnfold: (id: string) => void;
  onRefold: (id: string) => void;
  onPickMember: (foldId: string, memberId: string) => void;
}) {
  const lanes = evidenceLanes(node);
  const memberLanes = fold?.members.map(evidenceLanes) ?? [];
  const isStop = node.id === STOP_NODE_ID;
  return (
    <aside className="opg-inspector" aria-label="Selected program step" aria-live="polite">
      <div className="opg-inspector-head">
        <span>{fold ? "Selected steps" : "Selected step"}</span>
        <code>{chipFor(node, fold)}</code>
      </div>
      <h3>{titleFor(node)}</h3>
      <p className="opg-mono">{fold ? (fold.subtitle ?? `${fold.members.length} steps`) : isStop ? "Where a stop rule ends the run" : node.id}</p>

      {!isStop && (
        <div className="opg-inspector-lanes">
          {fold ? (
            <>
              <EvidenceState
                label="Finds the field"
                state={memberLanes.some((l) => l.resolve.count) ? "present" : "none"}
                value={`${memberLanes.filter((l) => l.resolve.count).length} of ${fold.members.length} steps`}
              />
              <EvidenceState
                label="Right record"
                state={lanes.identity.state}
                value={`${memberLanes.filter((l) => l.identity.text === "Checked").length} checked`}
              />
              <EvidenceState
                label="Screen check"
                state={lanes.screen.state}
                value={lanes.screen.count ? `${lanes.screen.count} checks` : "Not declared"}
              />
              <EvidenceState
                label="Read-back"
                state={lanes.readback.state}
                value={lanes.readback.count ? `${lanes.readback.count} independent checks` : "Not declared"}
              />
            </>
          ) : (
            <>
              <EvidenceState
                label="Finds the field"
                state={lanes.resolve.state}
                value={lanes.resolve.count ? `${lanes.resolve.count} evidence types` : "Not applicable"}
              />
              <EvidenceState label="Right record" state={lanes.identity.state} value={lanes.identity.text} />
              <EvidenceState
                label="Screen check"
                state={lanes.screen.state}
                value={lanes.screen.count ? `${lanes.screen.count} checks` : "Not declared"}
              />
              <EvidenceState
                label="Read-back"
                state={lanes.readback.state}
                value={lanes.readback.count ? `${lanes.readback.count} independent checks` : "Not declared"}
              />
            </>
          )}
        </div>
      )}

      {!fold && node.resolution && (node.resolution.rungs?.length ?? 0) > 0 && (
        <div className="opg-ladder">
          <span>Recorded resolution evidence</span>
          <ol>
            {node.resolution.rungs!.map((rung) => (
              <li
                key={rung.name}
                data-present={rung.present || undefined}
                data-top={rung.name === node.resolution?.top_rung || undefined}
              >
                <i aria-hidden="true" />
                {rung.label}
                <span className="opg-sr-only">{rung.present ? " (recorded)" : " (not recorded)"}</span>
              </li>
            ))}
          </ol>
        </div>
      )}

      {fold && (
        <div className="opg-fold-members">
          <span>Steps in this card</span>
          <ol>
            {fold.members.map((member) => (
              <li key={member.id}>
                <button type="button" onClick={() => onPickMember(fold.id, member.id)}>
                  <code>{stepChip(member)}</code>
                  {titleFor(member)}
                </button>
              </li>
            ))}
          </ol>
          <button type="button" className="opg-fold-toggle" onClick={() => onUnfold(fold.id)}>
            Show the {fold.members.length} steps
          </button>
        </div>
      )}

      {partOf && (
        <button type="button" className="opg-fold-toggle" onClick={() => onRefold(partOf.id)}>
          Fold into “{partOf.title}”
        </button>
      )}

      {nodeHalts(node).length > 0 && (
        <div className="opg-stop-rule">
          <span>Stops before acting if</span>
          <strong>{nodeHalts(node).join("; ")}</strong>
        </div>
      )}

      {run && run.status !== "pending" && <RunFacts state={run} />}
    </aside>
  );
}

function edgeRunState(edge: EdgeLayout, trace: TraceState | null): "taken" | "latest" | undefined {
  if (!trace) return undefined;
  const key = `${edge.source}>${edge.target}`;
  if (trace.latestEdge === key) return "latest";
  if (trace.taken.has(key)) return "taken";
  return undefined;
}

function ProgramMap({
  display,
  fullSpec,
  folds,
  expanded,
  setExpanded,
  traceState,
  selectedId,
  setSelectedId,
  titleFor,
  canvasNote,
  layoutOptions,
}: {
  display: DisplayGraph;
  fullSpec: ProgramGraphSpec;
  folds?: FoldSpec | null;
  expanded: Set<string>;
  setExpanded: (next: Set<string>) => void;
  traceState: TraceState | null;
  selectedId: string;
  setSelectedId: (id: string) => void;
  titleFor: (node: GraphNode) => string;
  canvasNote: string | null;
  layoutOptions?: LayoutOptions;
}) {
  const layout = useMemo(() => layoutProgramGraph(display.spec, layoutOptions), [display.spec, layoutOptions]);
  const nodes = specNodes(display.spec);
  const byId = useMemo(() => new Map(nodes.map((node) => [node.id, node])), [nodes]);
  const markerId = `opg-arrow-${useId().replace(/[^a-zA-Z0-9_-]/g, "")}`;
  const buttons = useRef(new Map<string, HTMLButtonElement>());
  const focusAfterRender = useRef<string | null>(null);

  // A selection inside a folded card selects the card.
  const shownSelected = display.ownerOf.get(selectedId) ?? (byId.has(selectedId) ? selectedId : nodes[0]?.id);
  const selected = byId.get(shownSelected ?? "") ?? nodes[0];

  // Keep the card the run is at in view while the trace plays or steps.
  const viewport = useRef<HTMLDivElement>(null);
  const currentCard = traceState?.current ? display.ownerOf.get(traceState.current.node) : undefined;
  useEffect(() => {
    const box = viewport.current;
    const point = layout.nodes.find((node) => node.id === currentCard);
    if (!box || !point) return;
    const top = point.y;
    const bottom = point.y + point.height;
    if (top >= box.scrollTop && bottom <= box.scrollTop + box.clientHeight) return;
    const target = Math.max(0, top - (box.clientHeight - point.height) / 2);
    if (typeof box.scrollTo === "function") box.scrollTo({ top: target });
    else box.scrollTop = target;
  }, [currentCard, traceState?.cursor, layout]);

  useEffect(() => {
    const id = focusAfterRender.current;
    if (!id) return;
    focusAfterRender.current = null;
    buttons.current.get(id)?.focus();
  });

  if (!selected) return <p className="opg-empty">The compiled graph is empty.</p>;

  const fold = display.folds.get(selected.id);
  const parentFold = folds?.groups.find((group) => expanded.has(group.id) && group.nodes.includes(selected.id));
  const runFor = (id: string) => traceState?.nodes.get(id);
  const edgeCount = specEdges(fullSpec).length;
  const nodeCount = specNodes(fullSpec).length;
  const cardCount = nodes.filter((node) => node.id !== STOP_NODE_ID).length;

  const move = (event: KeyboardEvent<HTMLButtonElement>, id: string) => {
    const here = layout.nodes.find((point) => point.id === id);
    if (!here) return;
    let next: string | undefined;
    const byRank = (rank: number) => layout.nodes.filter((point) => point.rank === rank).sort((a, b) => a.x - b.x);
    const nearest = (rank: number) =>
      byRank(rank).sort((a, b) => Math.abs(a.x - here.x) - Math.abs(b.x - here.x))[0]?.id;
    const ranks = [...new Set(layout.nodes.map((point) => point.rank))].sort((a, b) => a - b);
    const at = ranks.indexOf(here.rank);
    switch (event.key) {
      case "ArrowDown":
        next = layout.edges.find((edge) => edge.source === id && !edge.backEdge)?.target ?? (at < ranks.length - 1 ? nearest(ranks[at + 1]) : undefined);
        break;
      case "ArrowUp":
        next = layout.edges.find((edge) => edge.target === id && !edge.backEdge)?.source ?? (at > 0 ? nearest(ranks[at - 1]) : undefined);
        break;
      case "ArrowRight":
      case "ArrowLeft": {
        const row = byRank(here.rank);
        const i = row.findIndex((point) => point.id === id) + (event.key === "ArrowRight" ? 1 : -1);
        next = row[i]?.id;
        break;
      }
      case "Home":
        next = layout.nodes.slice().sort((a, b) => a.rank - b.rank || a.x - b.x)[0]?.id;
        break;
      case "End":
        next = layout.nodes.slice().sort((a, b) => b.rank - a.rank || b.x - a.x)[0]?.id;
        break;
      default:
        return;
    }
    event.preventDefault();
    if (next) {
      setSelectedId(next);
      buttons.current.get(next)?.focus();
    }
  };

  return (
    <div className="opg-grid">
      <div className="opg-canvas-shell">
        <div className="opg-canvas-head">
          <span>Compiled topology</span>
          <span>
            {nodeCount} nodes · {edgeCount} exact edges
            {display.folds.size > 0 && ` · ${cardCount} cards`}
          </span>
        </div>
        <div className="opg-canvas-viewport" ref={viewport}>
          <div className="opg-canvas" style={{ width: layout.width, height: layout.height }}>
            <svg
              className="opg-edges"
              viewBox={`0 0 ${layout.width} ${layout.height}`}
              width={layout.width}
              height={layout.height}
              role="img"
              aria-label={`${layout.edges.length} compiled program edges`}
            >
              <defs>
                <marker id={markerId} markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto" markerUnits="strokeWidth">
                  <path d="M 0 0 L 8 4 L 0 8 z" />
                </marker>
              </defs>
              {layout.edges.map((edge) => (
                <g
                  key={edge.id}
                  data-kind={edge.kind}
                  data-back-edge={edge.backEdge || undefined}
                  data-lane={edge.lane ?? undefined}
                  data-run={edgeRunState(edge, traceState)}
                >
                  <path d={edge.path} markerEnd={`url(#${markerId})`} />
                  {edge.label && (
                    <text
                      x={edge.labelX}
                      y={edge.labelY}
                      textAnchor={edge.labelRotate ? "middle" : undefined}
                      dominantBaseline={edge.labelRotate ? "middle" : undefined}
                      transform={edge.labelRotate ? `rotate(-90 ${edge.labelX} ${edge.labelY})` : undefined}
                    >
                      {edge.label}
                    </text>
                  )}
                </g>
              ))}
            </svg>
            {layout.nodes.map((point) => {
              const node = byId.get(point.id)!;
              const isSelected = node.id === selected.id;
              const nodeFold = display.folds.get(node.id);
              const run = runFor(node.id);
              const title = titleFor(node);
              const runWords = run && run.status !== "pending"
                ? ` This run: ${run.status === "halted" ? "stopped here" : run.status === "active" ? "here now" : "reached"}${run.iterations > 1 ? `, ${run.iterations} iterations` : ""}.`
                : "";
              return (
                <button
                  key={node.id}
                  ref={(el) => {
                    if (el) buttons.current.set(node.id, el);
                    else buttons.current.delete(node.id);
                  }}
                  type="button"
                  className="opg-node"
                  data-tone={nodeTone(node)}
                  data-fold={nodeFold ? "folded" : undefined}
                  data-selected={isSelected || undefined}
                  data-run={run?.status}
                  style={{ left: point.x, top: point.y, width: point.width, height: point.height }}
                  aria-pressed={isSelected}
                  aria-label={
                    nodeFold
                      ? `Steps ${chipFor(node, nodeFold)}, ${nodeFold.members.length} steps: ${title}.${runWords}`
                      : `${nodeDescription(node, title)}${runWords}`
                  }
                  tabIndex={isSelected ? 0 : -1}
                  onClick={() => setSelectedId(node.id)}
                  onDoubleClick={() => {
                    if (nodeFold) {
                      setExpanded(new Set([...expanded, nodeFold.id]));
                      setSelectedId(nodeFold.members[0].id);
                      focusAfterRender.current = nodeFold.members[0].id;
                    }
                  }}
                  onKeyDown={(event) => move(event, node.id)}
                >
                  <span className="opg-node-index">{chipFor(node, nodeFold)}</span>
                  <span className="opg-node-copy">
                    <small>{nodeFold ? (nodeFold.subtitle ?? `${nodeFold.members.length} steps`) : kindLabel(node)}</small>
                    <strong>{title}</strong>
                  </span>
                  <span className="opg-node-signals" aria-hidden="true">
                    {nodeKind(node) === "loop" && run && run.iterations > 0 && <b>×{run.iterations}</b>}
                    {node.identity?.armed && <i title="Checks the right record">R</i>}
                    {nodeEffects(node).length > 0 && <i title="Reads the saved record back">C</i>}
                    {nodeHalts(node).length > 0 && <i title="Stop rules">H</i>}
                  </span>
                </button>
              );
            })}
          </div>
        </div>
        <ul className="opg-sr-only" aria-label="Program edges">
          {layout.edges.map((edge) => (
            <li key={edge.id}>
              {titleFor(byId.get(edge.source)!)} to {titleFor(byId.get(edge.target)!)}
              {edge.backEdge ? ", returns to an earlier step" : ""} ({edge.kind.replaceAll("_", " ")}
              {edge.label ? `: ${edge.label}` : ""})
            </li>
          ))}
        </ul>
        {canvasNote && <p className="opg-canvas-note">{canvasNote}</p>}
      </div>
      <NodeInspector
        node={selected}
        fold={fold}
        partOf={parentFold ? { id: parentFold.id, title: parentFold.title ?? parentFold.id } : undefined}
        run={runFor(selected.id)}
        titleFor={titleFor}
        onUnfold={(id) => {
          const group = display.folds.get(id);
          setExpanded(new Set([...expanded, id]));
          if (group) {
            setSelectedId(group.members[0].id);
            focusAfterRender.current = group.members[0].id;
          }
        }}
        onRefold={(id) => {
          const next = new Set(expanded);
          next.delete(id);
          setExpanded(next);
          setSelectedId(id);
          focusAfterRender.current = id;
        }}
        onPickMember={(foldId, memberId) => {
          setExpanded(new Set([...expanded, foldId]));
          setSelectedId(memberId);
          focusAfterRender.current = memberId;
        }}
      />
    </div>
  );
}

function EvidenceMatrix({ spec, titleFor, note }: { spec: ProgramGraphSpec; titleFor: (node: GraphNode) => string; note: string | null }) {
  return (
    <div className="opg-matrix-scroll">
      <table className="opg-matrix">
        <thead>
          <tr>
            <th scope="col">Step</th>
            <th scope="col">Finds the field</th>
            <th scope="col">Right record</th>
            <th scope="col">Screen check</th>
            <th scope="col">Read-back</th>
          </tr>
        </thead>
        <tbody>
          {specNodes(spec).map((node) => {
            const lanes = evidenceLanes(node);
            return (
              <tr key={`evidence-${node.id}`}>
                <td>
                  <strong>{titleFor(node)}</strong>
                  <small className="opg-mono">{node.id}</small>
                </td>
                <td data-state={lanes.resolve.state}>{lanes.resolve.count ? lanes.resolve.labels.join(", ") : "Not applicable"}</td>
                <td data-state={lanes.identity.state}>{lanes.identity.text}</td>
                <td data-state={lanes.screen.state}>{lanes.screen.count ? `${lanes.screen.count} declared` : "Not declared"}</td>
                <td data-state={lanes.readback.state}>{lanes.readback.count ? `${lanes.readback.count} independent` : "Not declared"}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
      {note && <p className="opg-boundary-note">{note}</p>}
    </div>
  );
}

function StopRules({ spec, titleFor, halt }: { spec: ProgramGraphSpec; titleFor: (node: GraphNode) => string; halt: TraceState["halted"] }) {
  const rules = specNodes(spec).filter((node) => nodeHalts(node).length > 0 || nodeKind(node) === "terminal");
  const stopped = halt ? specNodes(spec).find((node) => node.id === halt.node) : undefined;
  return (
    <div className="opg-stop-list">
      {halt && stopped && (
        <article data-run="halted">
          <span className="opg-mono">this run stopped at step {stopped.index + 1}</span>
          <strong>{titleFor(stopped)}</strong>
          <p>{halt.reason ?? "The run stopped at this step."}</p>
        </article>
      )}
      {rules.length === 0 ? (
        <p className="opg-empty">This graph does not declare a stop rule or terminal outcome.</p>
      ) : (
        rules.map((node) => (
          <article key={`stop-${node.id}`}>
            <span className="opg-mono">{nodeKind(node) === "terminal" ? "terminal" : `step ${node.index + 1}`}</span>
            <strong>{titleFor(node)}</strong>
            <p>{nodeHalts(node).length ? nodeHalts(node).join("; ") : "Declared terminal program state."}</p>
          </article>
        ))
      )}
    </div>
  );
}

function Icon({ d }: { d: string }) {
  return (
    <svg viewBox="0 0 14 14" width="12" height="12" aria-hidden="true" focusable="false">
      <path d={d} />
    </svg>
  );
}

function TracePlayer({
  trace,
  state,
  playing,
  setPlaying,
  setCursor,
  iterationNoun,
}: {
  trace: RunTrace;
  state: TraceState;
  playing: boolean;
  setPlaying: (playing: boolean) => void;
  setCursor: (cursor: number) => void;
  iterationNoun: string;
}) {
  const total = trace.events.length;
  const iterations = new Set(trace.events.map((event) => event.iteration).filter((n) => n != null)).size;
  const where = state.cursor === 0
    ? "Ready"
    : state.iteration != null
      ? `${iterationNoun[0].toUpperCase()}${iterationNoun.slice(1)} ${state.iteration} of ${iterations}`
      : `Step ${state.cursor} of ${total}`;
  const announce = state.halted && state.finished
    ? `Stopped. ${state.halted.reason ?? ""}`
    : state.finished
      ? "The run finished."
      : state.iteration != null
        ? `${iterationNoun} ${state.iteration}`
        : "";
  return (
    <div className="opg-player" role="group" aria-label="Run trace player">
      <button type="button" onClick={() => setCursor(0)} aria-label="Back to the start" disabled={state.cursor === 0}>
        ⏮
      </button>
      <button type="button" onClick={() => setCursor(state.cursor - 1)} aria-label="Step back" disabled={state.cursor === 0}>
        <Icon d="M11 2 4 7l7 5z" />
      </button>
      <button
        type="button"
        className="opg-play"
        onClick={() => {
          if (state.finished) setCursor(0);
          setPlaying(!playing);
        }}
        aria-label={playing ? "Pause" : state.finished ? "Replay" : "Play"}
      >
        {playing ? "Pause" : state.finished ? "Replay" : "Play"}
      </button>
      <button type="button" onClick={() => setCursor(state.cursor + 1)} aria-label="Step forward" disabled={state.finished}>
        <Icon d="M3 2l7 5-7 5z" />
      </button>
      <input
        type="range"
        min={0}
        max={total}
        value={state.cursor}
        onChange={(event) => {
          setPlaying(false);
          setCursor(Number(event.target.value));
        }}
        aria-label="Run position"
        aria-valuetext={`${where}, event ${state.cursor} of ${total}`}
      />
      <span className="opg-player-where">{where}</span>
      <span className="opg-sr-only" aria-live="polite">
        {announce}
      </span>
    </div>
  );
}

function TraceChip({ trace, state, playing }: { trace?: RunTrace | null; state: TraceState | null; playing: boolean }) {
  if (!trace || !state) {
    return (
      <span className="opg-trace-state">
        <i /> No live trace bound
      </span>
    );
  }
  const tone = state.halted && state.finished ? "halted" : state.finished ? "done" : playing ? "playing" : "ready";
  const words = tone === "halted" ? "Run stopped" : tone === "done" ? "Run finished" : tone === "playing" ? "Playing run trace" : "Run trace bound";
  return (
    <span className="opg-trace-state" data-run={tone}>
      <i /> {trace.label ? `${words} · ${trace.label}` : words}
    </span>
  );
}

/**
 * The program workbench: the map of a compiled program, its declared
 * evidence, its stop rules, and, given a run trace, what one run did.
 */
export function ProgramWorkbench({
  spec,
  title,
  kicker = "The steps",
  folds,
  stopExits,
  trace,
  autoPlay = false,
  initialCursor = 0,
  speed = 14,
  iterationNoun = "iteration",
  views = ["program", "evidence", "stops"],
  initialView,
  initialSelectedId,
  onSelect,
  titleFor = displayTitle,
  notes,
  layout,
  theme = "dark",
  className,
  style,
}: ProgramWorkbenchProps) {
  const [view, setView] = useState<ProgramView>(initialView ?? views[0] ?? "program");
  const [selectedId, setSelected] = useState(initialSelectedId ?? specNodes(spec)[0]?.id ?? "");
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set());
  const reducedMotion = usePrefersReducedMotion();
  const [cursor, setCursorRaw] = useState(initialCursor);
  const [playing, setPlaying] = useState(false);
  const baseId = useId().replace(/[^a-zA-Z0-9_-]/g, "");
  const tabs = useRef(new Map<ProgramView, HTMLButtonElement>());

  const total = trace?.events.length ?? 0;
  const setCursor = useCallback((next: number) => setCursorRaw(Math.max(0, Math.min(total, next))), [total]);

  useEffect(() => {
    setCursorRaw(Math.max(0, Math.min(trace?.events.length ?? 0, initialCursor)));
    setPlaying(Boolean(trace) && autoPlay && !reducedMotion);
  }, [trace, autoPlay, reducedMotion, initialCursor]);

  useEffect(() => {
    if (!playing || !trace) return;
    if (cursor >= total) {
      setPlaying(false);
      return;
    }
    const timer = setTimeout(() => setCursorRaw((c) => Math.min(total, c + 1)), playbackDelay(trace, cursor, speed));
    return () => clearTimeout(timer);
  }, [playing, cursor, total, trace, speed]);

  const halt = haltEvent(trace);
  const exits = useMemo(() => {
    const list = [...(stopExits ?? [])];
    if (halt && !list.some((stop) => stop.node === halt.node)) list.push({ node: halt.node, label: "stopped" });
    return list;
  }, [stopExits, halt]);
  const display = useMemo(
    () => deriveDisplayGraph(spec, { folds, expanded, stopExits: exits }),
    [spec, folds, expanded, exits],
  );
  const traceState = useMemo(
    () => (trace ? traceStateAt(trace, cursor, display.ownerOf) : null),
    [trace, cursor, display],
  );

  const setSelectedId = useCallback(
    (id: string) => {
      setSelected(id);
      onSelect?.(id);
    },
    [onSelect],
  );

  // When the run moves, select the step it reached, so the inspector shows
  // what happened there. A viewer's own selection stands until the next move.
  const followed = useRef(0);
  useEffect(() => {
    if (!traceState?.current || followed.current === cursor) return;
    followed.current = cursor;
    setSelected(traceState.current.node);
  }, [traceState, cursor]);

  const tabKey = (event: KeyboardEvent<HTMLButtonElement>, id: ProgramView) => {
    const i = views.indexOf(id);
    const next =
      event.key === "ArrowRight" ? views[(i + 1) % views.length]
      : event.key === "ArrowLeft" ? views[(i - 1 + views.length) % views.length]
      : event.key === "Home" ? views[0]
      : event.key === "End" ? views[views.length - 1]
      : undefined;
    if (!next) return;
    event.preventDefault();
    setView(next);
    tabs.current.get(next)?.focus();
  };

  const panel = (id: ProgramView, body: ReactNode) => (
    <div
      role="tabpanel"
      id={`${baseId}-panel-${id}`}
      aria-labelledby={`${baseId}-tab-${id}`}
      hidden={view !== id}
      tabIndex={-1}
    >
      {view === id && body}
    </div>
  );

  return (
    <div className={`opg-root${className ? ` ${className}` : ""}`} data-theme={theme} style={style}>
      <div className="opg-topbar">
        <div>
          <span className="opg-kicker">{kicker}</span>
          <strong>{title ?? spec.bundle.name}</strong>
        </div>
        <TraceChip trace={trace} state={traceState} playing={playing} />
      </div>
      {trace && traceState && (
        <TracePlayer
          trace={trace}
          state={traceState}
          playing={playing}
          setPlaying={setPlaying}
          setCursor={setCursor}
          iterationNoun={iterationNoun}
        />
      )}
      {views.length > 1 && (
        <div className="opg-tabs" role="tablist" aria-label="Program workbench views">
          {views.map((id) => (
            <button
              key={id}
              ref={(el) => {
                if (el) tabs.current.set(id, el);
              }}
              id={`${baseId}-tab-${id}`}
              type="button"
              role="tab"
              aria-selected={view === id}
              aria-controls={`${baseId}-panel-${id}`}
              tabIndex={view === id ? 0 : -1}
              onClick={() => setView(id)}
              onKeyDown={(event) => tabKey(event, id)}
            >
              {VIEW_LABEL[id]}
            </button>
          ))}
        </div>
      )}
      {views.includes("program") &&
        panel(
          "program",
          <ProgramMap
            display={display}
            fullSpec={spec}
            folds={folds}
            expanded={expanded}
            setExpanded={setExpanded}
            traceState={traceState}
            selectedId={selectedId}
            setSelectedId={setSelectedId}
            titleFor={titleFor}
            canvasNote={notes?.canvas === undefined ? DEFAULT_CANVAS_NOTE : notes.canvas}
            layoutOptions={layout}
          />,
        )}
      {views.includes("evidence") &&
        panel(
          "evidence",
          <EvidenceMatrix spec={spec} titleFor={titleFor} note={notes?.evidence === undefined ? DEFAULT_EVIDENCE_NOTE : notes.evidence} />,
        )}
      {views.includes("stops") &&
        panel("stops", <StopRules spec={spec} titleFor={titleFor} halt={traceState?.finished ? traceState.halted : null} />)}
    </div>
  );
}

