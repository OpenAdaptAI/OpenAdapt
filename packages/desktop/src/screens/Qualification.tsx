import { useEffect, useMemo, useState } from "react";
import { CMD, engineInvoke } from "../lib/engine";
import type {
  QualificationNode,
  QualificationProject,
  QualificationResponse,
  QualificationRisk,
} from "../lib/types";
import { Button, Callout, Card, CardHead, Pill } from "../ui/primitives";

const POLICY = "clinical-write";

function identityLabel(node: QualificationNode): string {
  if (!node.identity?.applicable) return "not applicable";
  if (node.identity.armed) return "armed";
  return node.identity.reason || "not armed";
}

function certificationState(project: QualificationProject): {
  label: string;
  tone: "ok" | "warn" | "crit";
} {
  if (project.certification_current) {
    return { label: "certified", tone: "ok" };
  }
  if (project.certification.passed) {
    return { label: "ready to certify", tone: "warn" };
  }
  return { label: "needs review", tone: "crit" };
}

export function Qualification({
  workflowId,
  onBack,
}: {
  workflowId: string;
  onBack: () => void;
}) {
  const [project, setProject] = useState<QualificationProject | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");

  async function load() {
    setBusy("loading");
    setError("");
    try {
      const response = await engineInvoke<QualificationResponse>(
        CMD.GET_QUALIFICATION,
        { workflow_id: workflowId, policy: POLICY },
      );
      if (!response.ok) {
        setError(response.error);
        return;
      }
      setProject(response);
    } catch (reason) {
      setError(String(reason));
    } finally {
      setBusy("");
    }
  }

  useEffect(() => {
    void load();
    // The selected workflow is immutable for this mounted screen.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [workflowId]);

  const actions = useMemo(
    () => project?.graph.nodes.filter((node) => node.kind === "action") || [],
    [project],
  );
  const edgesBySource = useMemo(() => {
    const grouped = new Map<
      string,
      QualificationProject["graph"]["edges"]
    >();
    for (const edge of project?.graph.edges || []) {
      const edges = grouped.get(edge.source) || [];
      edges.push(edge);
      grouped.set(edge.source, edges);
    }
    return grouped;
  }, [project]);

  async function setRisk(stepId: string, risk: QualificationRisk) {
    setBusy(stepId);
    setError("");
    try {
      const response = await engineInvoke<QualificationResponse>(
        CMD.SET_QUALIFICATION_RISK,
        {
          workflow_id: workflowId,
          step_id: stepId,
          risk,
          policy: POLICY,
        },
      );
      if (!response.ok) {
        setError(response.error);
        return;
      }
      setProject(response);
    } catch (reason) {
      setError(String(reason));
    } finally {
      setBusy("");
    }
  }

  async function certify() {
    setBusy("certify");
    setError("");
    try {
      const response = await engineInvoke<QualificationResponse>(
        CMD.CERTIFY_QUALIFICATION,
        { workflow_id: workflowId, policy: POLICY },
      );
      if (!response.ok) {
        setError(response.error);
        return;
      }
      setProject(response);
    } catch (reason) {
      setError(String(reason));
    } finally {
      setBusy("");
    }
  }

  const state = project ? certificationState(project) : null;
  const digest = project?.graph.bundle.provenance.content_digest;

  return (
    <div className="content">
      <div className="page-head">
        <div className="titles">
          <p className="eyebrow">Qualification</p>
          <h1>{project?.graph.bundle.name || "Workflow review"}</h1>
          <span className="page-sub mono">{workflowId}</span>
        </div>
        <Button onClick={onBack}>Back to workflows</Button>
      </div>

      {error && (
        <Callout tone="crit" title="Qualification stopped">
          {error}
        </Callout>
      )}

      {!project ? (
        <Card>
          <p className="page-sub">
            {busy === "loading" ? "Opening the sealed workflow…" : "No workflow loaded."}
          </p>
        </Card>
      ) : (
        <>
          <Card>
            <CardHead
              eyebrow={project.policy}
              title="Qualification contract"
              sub={
                digest
                  ? `sealed bundle ${digest.slice(0, 16)}…`
                  : "bundle seal is being prepared"
              }
            />
            <div className="metrics">
              <div className="metric">
                <span className="label">Actions</span>
                <span className="metric-value">{project.graph.bundle.action_count}</span>
              </div>
              <div className="metric">
                <span className="label">Irreversible</span>
                <span className="metric-value">
                  {project.graph.bundle.irreversible_count}
                </span>
              </div>
              <div className="metric">
                <span className="label">Identity gates</span>
                <span className="metric-value">
                  {project.graph.bundle.identity_armed_count}
                </span>
              </div>
              <div className="metric">
                <span className="label">Effect contracts</span>
                <span className="metric-value">{project.graph.bundle.effect_count}</span>
              </div>
            </div>
            <div className="row" style={{ marginTop: "var(--space-4)" }}>
              {state && <Pill tone={state.tone}>{state.label}</Pill>}
              <span className="page-sub">
                {project.graph.bundle.encrypted
                  ? "sealed and encrypted at rest"
                  : "integrity sealed"}
              </span>
              <span className="spacer" />
              <Button
                variant="primary"
                disabled={busy === "certify"}
                onClick={certify}
              >
                {busy === "certify" ? "Certifying…" : "Run certification"}
              </Button>
            </div>
          </Card>

          <Card>
            <CardHead
              eyebrow="Graph review"
              title="Workflow structure"
              sub="Branches, loops, exception paths, and terminal outcomes from the compiled program."
            />
            <table>
              <thead>
                <tr>
                  <th className="num">#</th>
                  <th>Node</th>
                  <th>Type</th>
                  <th>Next</th>
                </tr>
              </thead>
              <tbody>
                {project.graph.nodes.map((node) => (
                  <tr key={`graph-${node.id}`}>
                    <td className="num">{node.index + 1}</td>
                    <td>
                      <strong>{node.title}</strong>
                      <div className="page-sub mono">{node.id}</div>
                      {node.badges.length > 0 && (
                        <div className="row" style={{ marginTop: "var(--space-1)" }}>
                          {node.badges.map((badge) => (
                            <Pill key={`${node.id}-${badge}`} tone="neutral">
                              {badge}
                            </Pill>
                          ))}
                        </div>
                      )}
                    </td>
                    <td>
                      <Pill tone={node.kind === "terminal" ? "ok" : "neutral"}>
                        {node.kind}
                      </Pill>
                    </td>
                    <td>
                      {(edgesBySource.get(node.id) || []).map((edge, index) => (
                        <div key={`${edge.source}-${edge.target}-${index}`}>
                          <span className="mono">{edge.target}</span>
                          {edge.label && (
                            <span className="page-sub"> · {edge.label}</span>
                          )}
                        </div>
                      ))}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Card>

          <Card>
            <CardHead
              eyebrow="Contract review"
              title="Actions, identity, and effects"
              sub="Risk corrections reseal the bundle and require certification again."
            />
            <table>
              <thead>
                <tr>
                  <th>Action</th>
                  <th>Risk</th>
                  <th>Identity</th>
                  <th>Effect verification</th>
                  <th>Postconditions</th>
                </tr>
              </thead>
              <tbody>
                {actions.map((node) => (
                  <tr key={node.id}>
                    <td>
                      <strong>{node.title}</strong>
                      <div className="page-sub mono">
                        {node.action} · {node.id}
                      </div>
                    </td>
                    <td>
                      <select
                        className="input"
                        aria-label={`Risk for ${node.title}`}
                        value={node.risk || "reversible"}
                        disabled={busy === node.id}
                        onChange={(event) =>
                          void setRisk(
                            node.id,
                            event.target.value as QualificationRisk,
                          )
                        }
                      >
                        <option value="reversible">Reversible</option>
                        <option value="irreversible">Irreversible</option>
                      </select>
                    </td>
                    <td>
                      <Pill tone={node.identity?.armed ? "ok" : "neutral"}>
                        {identityLabel(node)}
                      </Pill>
                    </td>
                    <td>
                      {node.effects.length ? (
                        node.effects.map((effect, index) => (
                          <div key={`${node.id}-effect-${index}`}>
                            <Pill
                              tone={
                                effect.needs_operator_confirmation ? "warn" : "ok"
                              }
                            >
                              {effect.kind}
                            </Pill>
                            <div className="page-sub">{effect.summary}</div>
                          </div>
                        ))
                      ) : (
                        <span className="page-sub">No declared effect</span>
                      )}
                    </td>
                    <td>
                      {node.postconditions.length ? (
                        node.postconditions.join(", ")
                      ) : (
                        <span className="page-sub">None</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Card>

          {(project.certification.violations.length > 0 ||
            project.lint.findings.length > 0) && (
            <Card>
              <CardHead
                eyebrow="Refusals"
                title="What must be resolved"
                sub="Every item names the exact action and contract that prevented certification."
              />
              {project.certification.violations.map((violation, index) => (
                <Callout
                  key={`${violation.rule}-${violation.step_id || index}`}
                  tone="warn"
                  title={violation.step_id || violation.rule}
                >
                  {violation.reason}
                </Callout>
              ))}
              {project.lint.findings
                .filter(
                  (finding) =>
                    !project.certification.violations.some(
                      (violation) =>
                        violation.step_id === finding.step_id &&
                        violation.reason.includes(finding.message),
                    ),
                )
                .map((finding, index) => (
                  <Callout
                    key={`${finding.code}-${finding.step_id || index}`}
                    tone={finding.severity === "error" ? "crit" : "info"}
                    title={finding.step_id || finding.code}
                  >
                    {finding.message}
                  </Callout>
                ))}
            </Card>
          )}
        </>
      )}
    </div>
  );
}
