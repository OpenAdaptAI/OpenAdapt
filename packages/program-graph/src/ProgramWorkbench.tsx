import {
  createContext,
  useCallback,
  useContext,
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
  stopRuleWords,
  kindLabel,
  nodeDescription,
  nodeTone,
  indexNumber,
  stepChip,
  stepNumberer,
  type Numbering,
  type StepNumberer,
  type LaneState,
} from "./present.js";
import { nodeEffects, nodeHalts, nodeKind, specEdges, specNodes, type GraphNode, type ProgramGraphSpec } from "./spec.js";
import {
  haltEvent,
  playbackDelay,
  traceStateAt,
  traceStateFromPlayhead,
  type NodeRunState,
  type Playhead,
  type RunTrace,
  type TraceState,
} from "./trace.js";

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
  /** Controlled selection: the host owns it and updates it from onSelect. */
  selectedId?: string;
  onSelect?: (nodeId: string) => void;
  /** Controlled trace position: events applied. The host updates it from onCursorChange. */
  cursor?: number;
  onCursorChange?: (cursor: number) => void;
  /** Light one step without a trace: the host page drives the run. */
  playhead?: Playhead | null;
  /** Controlled folds: ids of the fold groups shown open. */
  expanded?: string[];
  onExpandedChange?: (expanded: string[]) => void;
  /** Show the title bar (default true). */
  showHeader?: boolean;
  /** Show the trace player when there is a trace (default true). */
  showPlayer?: boolean;
  /** Show the selected-step inspector beside the map (default true). */
  showInspector?: boolean;
  /** Word stop rules plainly (stopRuleWords) instead of Flow's rule names. */
  plainStopRules?: boolean;
  /** "index" (default): number every node; "actions": number only the actions, 1 to N. */
  numbering?: Numbering;
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

const NumberOf = createContext<StepNumberer>(indexNumber);

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

function chipFor(node: GraphNode, fold: FoldInfo | undefined, numberOf: StepNumberer): string {
  if (fold) return fold.first === fold.last ? pad(fold.first) : `${pad(fold.first)}–${pad(fold.last)}`;
  return stepChip(node, numberOf);
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
  plainStopRules,
}: {
  plainStopRules?: boolean;
  node: GraphNode;
  fold?: FoldInfo;
  partOf?: { id: string; title: string };
  run?: NodeRunState;
  titleFor: (node: GraphNode) => string;
  onUnfold: (id: string) => void;
  onRefold: (id: string) => void;
  onPickMember: (foldId: string, memberId: string) => void;
}) {
  const numberOf = useContext(NumberOf);
  const lanes = evidenceLanes(node);
  const memberLanes = fold?.members.map(evidenceLanes) ?? [];
  const isStop = node.id === STOP_NODE_ID;
  return (
    <aside className="opg-inspector" aria-label="Selected program step" aria-live="polite">
      <div className="opg-inspector-head">
        <span>{fold ? "Selected steps" : "Selected step"}</span>
        <code>{chipFor(node, fold, numberOf)}</code>
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
                  <code>{stepChip(member, numberOf)}</code>
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

      {nodeHalts(node).length > 0 &&
        (plainStopRules ? (
          <div className="opg-stop-rule">
            <span>Stop rules</span>
            {(fold ? fold.members.flatMap(stopRuleWords) : stopRuleWords(node))
              .filter((rule, i, all) => all.findIndex((other) => other.text === rule.text) === i)
              .map((rule, i) => (
              <strong key={i} data-kind={rule.kind}>
                {rule.text}
              </strong>
            ))}
          </div>
        ) : (
          <div className="opg-stop-rule">
            <span>Stops before acting if</span>
            <strong>{nodeHalts(node).join("; ")}</strong>
          </div>
        ))}

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

function MapPanel({
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
  showInspector = true,
  plainStopRules,
}: {
  showInspector?: boolean;
  plainStopRules?: boolean;
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
  const numberOf = useContext(NumberOf);
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
    // The canvas is centred in the viewport when it is narrower than it.
    const canvas = box.firstElementChild as HTMLElement | null;
    const offsetX = canvas?.offsetLeft ?? 0;
    const left = point.x + offsetX;
    const inY = point.y >= box.scrollTop && point.y + point.height <= box.scrollTop + box.clientHeight;
    const inX = left >= box.scrollLeft && left + point.width <= box.scrollLeft + box.clientWidth;
    if (inY && inX) return;
    const top = inY ? box.scrollTop : Math.max(0, point.y - (box.clientHeight - point.height) / 2);
    const leftTarget = inX ? box.scrollLeft : Math.max(0, left - (box.clientWidth - point.width) / 2);
    if (typeof box.scrollTo === "function") box.scrollTo({ top, left: leftTarget });
    else {
      box.scrollTop = top;
      box.scrollLeft = leftTarget;
    }
  }, [currentCard, traceState?.cursor, layout]);

  useEffect(() => {
    const id = focusAfterRender.current;
    if (!id) return;
    focusAfterRender.current = null;
    buttons.current.get(id)?.focus();
  });

  if (!selected) return <p className="opg-empty">The compiled graph is empty.</p>;
  const inspector = (
    <NodeInspector
      node={selected}
      fold={display.folds.get(selected.id)}
      partOf={(() => {
        const group = folds?.groups.find((g) => expanded.has(g.id) && g.nodes.includes(selected.id));
        return group ? { id: group.id, title: group.title ?? group.id } : undefined;
      })()}
      run={traceState?.nodes.get(selected.id)}
      titleFor={titleFor}
      plainStopRules={plainStopRules}
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
  );

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
    <div className="opg-grid" data-inspector={showInspector || undefined}>
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
                      ? `Steps ${chipFor(node, nodeFold, numberOf)}, ${nodeFold.members.length} steps: ${title}.${runWords}`
                      : `${nodeDescription(node, title, numberOf)}${runWords}`
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
                  <span className="opg-node-index">{chipFor(node, nodeFold, numberOf)}</span>
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
      {showInspector && inspector}
    </div>
  );
}

function EvidenceTable({ spec, titleFor, note }: { spec: ProgramGraphSpec; titleFor: (node: GraphNode) => string; note: string | null }) {
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

function StopRulesList({
  spec,
  titleFor,
  halt,
  plain,
}: {
  spec: ProgramGraphSpec;
  titleFor: (node: GraphNode) => string;
  halt: TraceState["halted"];
  plain?: boolean;
}) {
  const numberOf = useContext(NumberOf);
  const rules = specNodes(spec).filter((node) => nodeHalts(node).length > 0 || nodeKind(node) === "terminal");
  const stopped = halt ? specNodes(spec).find((node) => node.id === halt.node) : undefined;
  return (
    <div className="opg-stop-list">
      {halt && stopped && (
        <article data-run="halted">
          <span className="opg-mono">this run stopped at step {numberOf(stopped) ?? stopped.index + 1}</span>
          <strong>{titleFor(stopped)}</strong>
          <p>{halt.reason ?? "The run stopped at this step."}</p>
        </article>
      )}
      {rules.length === 0 ? (
        <p className="opg-empty">This graph does not declare a stop rule or terminal outcome.</p>
      ) : (
        rules.map((node) => (
          <article key={`stop-${node.id}`}>
            <span className="opg-mono">
              {nodeKind(node) === "terminal"
                ? "terminal"
                : numberOf(node) === null
                  ? kindLabel(node)
                  : `step ${numberOf(node)}`}
            </span>
            <strong>{titleFor(node)}</strong>
            <p>
              {nodeHalts(node).length
                ? plain
                  ? stopRuleWords(node).map((rule) => rule.text).join(" ")
                  : nodeHalts(node).join("; ")
                : "Declared terminal program state."}
            </p>
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


/** Selection, folds and trace position, each either owned here or by the host. */
function useWorkbenchState(props: ProgramWorkbenchProps) {
  const { spec, folds, stopExits, trace, playhead, autoPlay = false, initialCursor = 0, speed = 14, numbering } = props;
  const numberOf = useMemo(() => stepNumberer(spec, numbering), [spec, numbering]);
  const [ownSelected, setOwnSelected] = useState(props.initialSelectedId ?? specNodes(spec)[0]?.id ?? "");
  const selectedId = props.selectedId ?? ownSelected;
  const [ownExpanded, setOwnExpanded] = useState<Set<string>>(() => new Set());
  const expanded = useMemo(
    () => (props.expanded ? new Set(props.expanded) : ownExpanded),
    [props.expanded, ownExpanded],
  );
  const reducedMotion = usePrefersReducedMotion();
  const [ownCursor, setOwnCursor] = useState(initialCursor);
  const cursor = props.cursor ?? ownCursor;
  const [playing, setPlaying] = useState(false);
  const total = trace?.events.length ?? 0;

  const { onSelect, onCursorChange, onExpandedChange } = props;
  const setSelectedId = useCallback(
    (id: string) => {
      setOwnSelected(id);
      onSelect?.(id);
    },
    [onSelect],
  );
  const setExpanded = useCallback(
    (next: Set<string>) => {
      setOwnExpanded(next);
      onExpandedChange?.([...next]);
    },
    [onExpandedChange],
  );
  const cursorRef = useRef(cursor);
  cursorRef.current = cursor;
  const setCursor = useCallback(
    (next: number) => {
      const clamped = Math.max(0, Math.min(total, next));
      setOwnCursor(clamped);
      onCursorChange?.(clamped);
    },
    [total, onCursorChange],
  );

  useEffect(() => {
    if (props.cursor === undefined) setOwnCursor(Math.max(0, Math.min(trace?.events.length ?? 0, initialCursor)));
    setPlaying(Boolean(trace) && autoPlay && !reducedMotion);
    // A new trace starts over; a controlled cursor is the host's.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [trace, autoPlay, reducedMotion, initialCursor]);

  useEffect(() => {
    if (!playing || !trace) return;
    if (cursor >= total) {
      setPlaying(false);
      return;
    }
    const timer = setTimeout(() => setCursor(cursorRef.current + 1), playbackDelay(trace, cursor, speed));
    return () => clearTimeout(timer);
  }, [playing, cursor, total, trace, speed, setCursor]);

  const halt = trace ? haltEvent(trace) : playhead?.status === "stopped" ? { node: playhead.node, status: "halted" as const } : null;
  const exits = useMemo(() => {
    const list = [...(stopExits ?? [])];
    if (halt && !list.some((stop) => stop.node === halt.node)) list.push({ node: halt.node, label: "stopped" });
    return list;
  }, [stopExits, halt?.node]); // eslint-disable-line react-hooks/exhaustive-deps
  const display = useMemo(
    () => deriveDisplayGraph(spec, { folds, expanded, stopExits: exits, numberOf }),
    [spec, folds, expanded, exits, numberOf],
  );
  const traceState = useMemo(
    () =>
      trace
        ? traceStateAt(trace, cursor, display.ownerOf)
        : playhead
          ? traceStateFromPlayhead(display.spec, playhead, display.ownerOf)
          : null,
    [trace, cursor, playhead, display],
  );

  // When the run moves, select the step it reached, so the inspector shows
  // what happened there. A viewer's own selection stands until the next move.
  const followed = useRef<string | null>(null);
  const moveKey = trace ? `c${cursor}` : playhead ? `p${playhead.node}:${playhead.iteration ?? ""}:${playhead.status}` : null;
  useEffect(() => {
    if (!traceState?.current || moveKey === null || followed.current === moveKey) return;
    const first = followed.current === null && cursor === 0 && Boolean(trace);
    followed.current = moveKey;
    if (!first) setSelectedId(traceState.current.node);
  }, [traceState, moveKey, cursor, trace, setSelectedId]);

  return { selectedId, setSelectedId, expanded, setExpanded, cursor, setCursor, playing, setPlaying, display, traceState, numberOf };
}

/**
 * The program workbench: the map of a compiled program, its declared
 * evidence, its stop rules, and, given a run trace, what one run did.
 */
export function ProgramWorkbench(props: ProgramWorkbenchProps) {
  const {
    spec,
    title,
    kicker = "The steps",
    folds,
    trace,
    iterationNoun = "iteration",
    views = ["program", "evidence", "stops"],
    initialView,
    titleFor = displayTitle,
    notes,
    layout,
    theme = "dark",
    className,
    style,
    showHeader = true,
    showPlayer = true,
    showInspector = true,
    plainStopRules = false,
  } = props;
  const state = useWorkbenchState(props);
  const { traceState } = state;
  const [view, setView] = useState<ProgramView>(initialView ?? views[0] ?? "program");
  const baseId = useId().replace(/[^a-zA-Z0-9_-]/g, "");
  const tabs = useRef(new Map<ProgramView, HTMLButtonElement>());
  const tabbed = views.length > 1;

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

  const panel = (id: ProgramView, body: ReactNode) =>
    tabbed ? (
      <div
        role="tabpanel"
        id={`${baseId}-panel-${id}`}
        aria-labelledby={`${baseId}-tab-${id}`}
        hidden={view !== id}
        tabIndex={-1}
      >
        {view === id && body}
      </div>
    ) : (
      view === id && <div>{body}</div>
    );

  return (
    <NumberOf.Provider value={state.numberOf}>
    <div className={`opg-root${className ? ` ${className}` : ""}`} data-theme={theme} style={style}>
      {showHeader && (
        <div className="opg-topbar">
          <div>
            <span className="opg-kicker">{kicker}</span>
            <strong>{title ?? spec.bundle.name}</strong>
          </div>
          <TraceChip trace={trace} state={trace ? traceState : null} playing={state.playing} />
        </div>
      )}
      {showPlayer && trace && traceState && (
        <TracePlayer
          trace={trace}
          state={traceState}
          playing={state.playing}
          setPlaying={state.setPlaying}
          setCursor={state.setCursor}
          iterationNoun={iterationNoun}
        />
      )}
      {tabbed && (
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
          <MapPanel
            display={state.display}
            fullSpec={spec}
            folds={folds}
            expanded={state.expanded}
            setExpanded={state.setExpanded}
            traceState={traceState}
            selectedId={state.selectedId}
            setSelectedId={state.setSelectedId}
            titleFor={titleFor}
            canvasNote={notes?.canvas === undefined ? DEFAULT_CANVAS_NOTE : notes.canvas}
            layoutOptions={layout}
            showInspector={showInspector}
            plainStopRules={plainStopRules}
          />,
        )}
      {views.includes("evidence") &&
        panel(
          "evidence",
          <EvidenceTable spec={spec} titleFor={titleFor} note={notes?.evidence === undefined ? DEFAULT_EVIDENCE_NOTE : notes.evidence} />,
        )}
      {views.includes("stops") &&
        panel(
          "stops",
          <StopRulesList spec={spec} titleFor={titleFor} halt={traceState?.finished ? traceState.halted : null} plain={plainStopRules} />,
        )}
    </div>
    </NumberOf.Provider>
  );
}

/** The map alone, with no title bar, tabs, player or inspector. */
export function ProgramMap(props: Omit<ProgramWorkbenchProps, "views" | "showHeader" | "showPlayer">) {
  return <ProgramWorkbench showInspector={false} {...props} views={["program"]} showHeader={false} showPlayer={false} />;
}

type PanelProps = {
  spec: ProgramGraphSpec;
  numbering?: Numbering;
  titleFor?: (node: GraphNode) => string;
  theme?: "dark" | "light";
  className?: string;
  style?: CSSProperties;
};

const Root = ({ spec, numbering, theme = "dark", className, style, children }: PanelProps & { children: ReactNode }) => {
  const numberOf = useMemo(() => stepNumberer(spec, numbering), [spec, numbering]);
  return (
    <NumberOf.Provider value={numberOf}>
      <div className={`opg-root${className ? ` ${className}` : ""}`} data-theme={theme} style={style}>
        {children}
      </div>
    </NumberOf.Provider>
  );
};

/** The inspector for one step, outside the workbench. */
export function StepInspector(
  props: PanelProps & {
    nodeId: string;
    folds?: FoldSpec | null;
    expanded?: string[];
    onExpandedChange?: (expanded: string[]) => void;
    onSelect?: (nodeId: string) => void;
    trace?: RunTrace | null;
    cursor?: number;
    playhead?: Playhead | null;
    stopExits?: StopExit[] | null;
    plainStopRules?: boolean;
  },
) {
  const { spec, nodeId, folds, trace, cursor = 0, playhead, plainStopRules, titleFor = displayTitle } = props;
  const expanded = useMemo(() => new Set(props.expanded ?? []), [props.expanded]);
  const halt = trace ? haltEvent(trace) : playhead?.status === "stopped" ? playhead : null;
  const display = useMemo(() => {
    const exits = [...(props.stopExits ?? [])];
    if (halt && !exits.some((stop) => stop.node === halt.node)) exits.push({ node: halt.node });
    return deriveDisplayGraph(spec, { folds, expanded, stopExits: exits, numberOf: stepNumberer(spec, props.numbering) });
  }, [spec, folds, expanded, props.stopExits, halt?.node, props.numbering]); // eslint-disable-line react-hooks/exhaustive-deps
  const traceState = trace
    ? traceStateAt(trace, cursor, display.ownerOf)
    : playhead
      ? traceStateFromPlayhead(display.spec, playhead, display.ownerOf)
      : null;
  const shown = display.ownerOf.get(nodeId) ?? nodeId;
  const node = specNodes(display.spec).find((n) => n.id === shown);
  if (!node) return null;
  const group = folds?.groups.find((g) => expanded.has(g.id) && g.nodes.includes(node.id));
  const change = (next: Set<string>) => props.onExpandedChange?.([...next]);
  return (
    <Root {...props}>
      <NodeInspector
        node={node}
        fold={display.folds.get(node.id)}
        partOf={group ? { id: group.id, title: group.title ?? group.id } : undefined}
        run={traceState?.nodes.get(node.id)}
        titleFor={titleFor}
        plainStopRules={plainStopRules}
        onUnfold={(id) => {
          change(new Set([...expanded, id]));
          const members = display.folds.get(id)?.members;
          if (members?.length) props.onSelect?.(members[0].id);
        }}
        onRefold={(id) => {
          const next = new Set(expanded);
          next.delete(id);
          change(next);
          props.onSelect?.(id);
        }}
        onPickMember={(foldId, memberId) => {
          change(new Set([...expanded, foldId]));
          props.onSelect?.(memberId);
        }}
      />
    </Root>
  );
}

/** The evidence-lanes table, outside the workbench. */
export function EvidenceLanes(props: PanelProps & { note?: string | null }) {
  return (
    <Root {...props}>
      <EvidenceTable
        spec={props.spec}
        titleFor={props.titleFor ?? displayTitle}
        note={props.note === undefined ? DEFAULT_EVIDENCE_NOTE : props.note}
      />
    </Root>
  );
}

/** The stop rules, outside the workbench; with a trace, the stop it hit first. */
export function StopRules(props: PanelProps & { trace?: RunTrace | null; plainStopRules?: boolean }) {
  return (
    <Root {...props}>
      <StopRulesList
        spec={props.spec}
        titleFor={props.titleFor ?? displayTitle}
        halt={haltEvent(props.trace)}
        plain={props.plainStopRules}
      />
    </Root>
  );
}
