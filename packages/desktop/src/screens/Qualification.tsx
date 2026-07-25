import { useEffect, useMemo, useState } from "react";
import { CMD, engineInvoke } from "../lib/engine";
import type {
  QualificationEffectKind,
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
  const [contractActionId, setContractActionId] = useState("");
  const [effectKind, setEffectKind] =
    useState<QualificationEffectKind>("record_written");
  const [matchField, setMatchField] = useState("record_id");
  const [matchParam, setMatchParam] = useState("");
  const [effectField, setEffectField] = useState("");
  const [valueParam, setValueParam] = useState("");
  const [idempotencyParam, setIdempotencyParam] = useState("");
  const [keyField, setKeyField] = useState("key");
  const [expectedCount, setExpectedCount] = useState(1);
  const [countNewOnly, setCountNewOnly] = useState(true);

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
  const parameters = useMemo(
    () => project?.controls.parameters.filter((parameter) => !parameter.secret) || [],
    [project],
  );
  const selectedActionId =
    contractActionId ||
    actions.find((action) => action.risk === "irreversible")?.id ||
    actions[0]?.id ||
    "";
  const selectedAction = actions.find((action) => action.id === selectedActionId);
  const selectedControls = selectedActionId
    ? project?.controls.actions[selectedActionId]
    : undefined;
  const selectedMatchParam = matchParam || parameters[0]?.name || "";
  const selectedValueParam = valueParam || parameters[0]?.name || "";

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

  async function armIdentity() {
    if (!selectedActionId) return;
    setBusy("identity");
    setError("");
    try {
      const response = await engineInvoke<QualificationResponse>(
        CMD.ARM_QUALIFICATION_IDENTITY,
        {
          workflow_id: workflowId,
          step_id: selectedActionId,
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

  async function bindEffect() {
    if (!selectedActionId || !selectedMatchParam) return;
    setBusy("effect");
    setError("");
    const placeholder = selectedControls?.effects.find(
      (effect) => effect.needs_operator_confirmation,
    );
    try {
      const response = await engineInvoke<QualificationResponse>(
        CMD.BIND_QUALIFICATION_EFFECT,
        {
          workflow_id: workflowId,
          step_id: selectedActionId,
          kind: effectKind,
          match_field: matchField,
          match_param: selectedMatchParam,
          field: effectKind === "field_equals" ? effectField : undefined,
          value_param:
            effectKind === "field_equals" ? selectedValueParam : undefined,
          idempotency_param: idempotencyParam || undefined,
          key_field: keyField,
          expected_count: expectedCount,
          count_new_only:
            effectKind === "record_written" ? countNewOnly : false,
          effect_index: placeholder?.index,
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

          <Card>
            <CardHead
              eyebrow="Contract authoring"
              title="Arm identity and bind the business effect"
              sub="These controls edit Flow's sealed workflow contract directly. Every change invalidates prior certification and reseals the bundle."
            />
            <div className="field">
              <label htmlFor="qualification-action">Action</label>
              <select
                id="qualification-action"
                className="input"
                value={selectedActionId}
                onChange={(event) => setContractActionId(event.target.value)}
              >
                {actions.map((action) => (
                  <option key={`contract-${action.id}`} value={action.id}>
                    {action.risk === "irreversible" ? "Consequential: " : ""}
                    {action.title}
                  </option>
                ))}
              </select>
            </div>

            {selectedAction && selectedControls && (
              <div className="grid grid-2">
                <div>
                  <h3>Identity before actuation</h3>
                  <p className="page-sub">
                    OpenAdapt uses every retained source from strongest to weakest and
                    halts when they conflict or cannot establish the record identity.
                  </p>
                  <div
                    className="row"
                    style={{
                      marginTop: "var(--space-3)",
                      marginBottom: "var(--space-3)",
                    }}
                  >
                    <Pill tone={selectedControls.identity.armed ? "ok" : "warn"}>
                      {selectedControls.identity.armed ? "armed" : "not armed"}
                    </Pill>
                    <span className="page-sub">
                      {selectedControls.identity.sources.length} retained source
                      {selectedControls.identity.sources.length === 1 ? "" : "s"}
                    </span>
                  </div>
                  {selectedControls.identity.sources.map((source) => (
                    <div key={`${selectedActionId}-${source.kind}`}>
                      <strong>{source.label}</strong>
                      <div className="page-sub">{source.match}</div>
                      {source.region && (
                        <div className="page-sub mono">
                          region {source.region.join(", ")}
                        </div>
                      )}
                    </div>
                  ))}
                  {!selectedControls.identity.sources.length && (
                    <Callout tone="warn" title="Identity evidence required">
                      Record or teach this action with structured identity, a marked
                      identifier region, or a captured row identity.
                    </Callout>
                  )}
                  <div style={{ marginTop: "var(--space-4)" }}>
                    <Button
                      variant="primary"
                      disabled={
                        busy === "identity" ||
                        selectedControls.identity.armed ||
                        !selectedControls.identity.can_arm
                      }
                      onClick={() => void armIdentity()}
                    >
                      {busy === "identity" ? "Arming…" : "Arm identity check"}
                    </Button>
                  </div>
                </div>

                <div>
                  <h3>Persisted business effect</h3>
                  <p className="page-sub">
                    Bind the record identity and intended result to workflow parameters.
                    The runtime still needs a matching read-only verifier at deployment.
                  </p>
                  {!parameters.length ? (
                    <Callout tone="warn" title="Workflow parameters required">
                      Add typed workflow parameters before binding a reusable effect.
                      OpenAdapt will not seal a patient or account literal into this form.
                    </Callout>
                  ) : (
                    <>
                      <div className="field">
                        <label htmlFor="effect-kind">Effect</label>
                        <select
                          id="effect-kind"
                          className="input"
                          value={effectKind}
                          onChange={(event) =>
                            setEffectKind(
                              event.target.value as QualificationEffectKind,
                            )
                          }
                        >
                          <option value="record_written">Record written exactly once</option>
                          <option value="field_equals">Persisted field equals value</option>
                        </select>
                      </div>
                      <div className="grid grid-2">
                        <div className="field">
                          <label htmlFor="match-field">Record identifier field</label>
                          <input
                            id="match-field"
                            className="input"
                            value={matchField}
                            onChange={(event) => setMatchField(event.target.value)}
                          />
                        </div>
                        <div className="field">
                          <label htmlFor="match-param">Value from parameter</label>
                          <select
                            id="match-param"
                            className="input"
                            value={selectedMatchParam}
                            onChange={(event) => setMatchParam(event.target.value)}
                          >
                            {parameters.map((parameter) => (
                              <option key={`match-${parameter.name}`} value={parameter.name}>
                                {parameter.name}
                              </option>
                            ))}
                          </select>
                        </div>
                      </div>
                      {effectKind === "field_equals" && (
                        <div className="grid grid-2">
                          <div className="field">
                            <label htmlFor="effect-field">Persisted field</label>
                            <input
                              id="effect-field"
                              className="input"
                              value={effectField}
                              onChange={(event) => setEffectField(event.target.value)}
                            />
                          </div>
                          <div className="field">
                            <label htmlFor="value-param">Expected value parameter</label>
                            <select
                              id="value-param"
                              className="input"
                              value={selectedValueParam}
                              onChange={(event) => setValueParam(event.target.value)}
                            >
                              {parameters.map((parameter) => (
                                <option key={`value-${parameter.name}`} value={parameter.name}>
                                  {parameter.name}
                                </option>
                              ))}
                            </select>
                          </div>
                        </div>
                      )}
                      <div className="grid grid-2">
                        <div className="field">
                          <label htmlFor="idempotency-param">
                            Idempotency key parameter
                          </label>
                          <select
                            id="idempotency-param"
                            className="input"
                            value={idempotencyParam}
                            onChange={(event) =>
                              setIdempotencyParam(event.target.value)
                            }
                          >
                            <option value="">Not declared</option>
                            {parameters.map((parameter) => (
                              <option key={`key-${parameter.name}`} value={parameter.name}>
                                {parameter.name}
                              </option>
                            ))}
                          </select>
                        </div>
                        <div className="field">
                          <label htmlFor="key-field">Idempotency field</label>
                          <input
                            id="key-field"
                            className="input"
                            value={keyField}
                            onChange={(event) => setKeyField(event.target.value)}
                          />
                        </div>
                      </div>
                      {effectKind === "record_written" && (
                        <>
                          <div className="field">
                            <label htmlFor="expected-count">Required record count</label>
                            <input
                              id="expected-count"
                              className="input"
                              type="number"
                              min={0}
                              value={expectedCount}
                              onChange={(event) =>
                                setExpectedCount(Number(event.target.value))
                              }
                            />
                          </div>
                          <label className="check-row">
                            <input
                              type="checkbox"
                              checked={countNewOnly}
                              onChange={(event) =>
                                setCountNewOnly(event.target.checked)
                              }
                            />
                            <span>Count only records created by this action</span>
                          </label>
                        </>
                      )}
                      <div style={{ marginTop: "var(--space-4)" }}>
                        <Button
                          variant="primary"
                          disabled={
                            busy === "effect" ||
                            !matchField.trim() ||
                            !selectedMatchParam ||
                            (effectKind === "field_equals" &&
                              (!effectField.trim() || !selectedValueParam))
                          }
                          onClick={() => void bindEffect()}
                        >
                          {busy === "effect"
                            ? "Binding…"
                            : selectedControls.effects.some(
                                  (effect) => effect.needs_operator_confirmation,
                                )
                              ? "Complete effect binding"
                              : "Add effect contract"}
                        </Button>
                      </div>
                    </>
                  )}
                </div>
              </div>
            )}
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
