import { useEffect, useMemo, useState } from "react";
import { CMD, engineInvoke } from "../lib/engine";
import type {
  QualificationEffectKind,
  QualificationNode,
  QualificationProject,
  QualificationResponse,
  QualificationRisk,
  QualificationTargetKind,
} from "../lib/types";
import { Button, Callout, Card, CardHead, Pill } from "../ui/primitives";

const POLICY = "clinical-write";

function identityLabel(node: QualificationNode): string {
  if (!node.identity?.applicable) return "not applicable";
  if (node.identity.armed) return "armed";
  return node.identity.reason || "not armed";
}

function actionTitle(node: QualificationNode): string {
  if (/^(click|double click) at \(\d+,\s*\d+\)$/i.test(node.title.trim())) {
    return node.action === "double_click"
      ? "Double-click recorded target"
      : "Click recorded target";
  }
  return node.title;
}

function TargetEvidence({ node }: { node: QualificationNode }) {
  const rungs = node.resolution?.rungs.filter((rung) => rung.present) || [];
  if (!rungs.length) {
    return node.kind === "action" ? (
      <span className="page-sub">No durable target evidence</span>
    ) : (
      <span className="page-sub">Not applicable</span>
    );
  }
  return (
    <div>
      <div className="row">
        {rungs.map((rung) => (
          <Pill
            key={`${node.id}-resolution-${rung.name}`}
            tone={rung.name === node.resolution?.top_rung ? "ok" : "neutral"}
          >
            {rung.label}
          </Pill>
        ))}
      </div>
      {rungs
        .filter((rung) => rung.detail)
        .map((rung) => (
          <div
            className="page-sub"
            key={`${node.id}-resolution-detail-${rung.name}`}
          >
            {rung.label}: <span className="mono">{rung.detail}</span>
          </div>
        ))}
    </div>
  );
}

function certificationState(project: QualificationProject): {
  label: string;
  tone: "ok" | "warn" | "crit";
} {
  if (project.certification_current) {
    return { label: "certified", tone: "ok" };
  }
  if (project.report.passed) {
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
  const [verificationTier, setVerificationTier] = useState(3);
  const [targetKind, setTargetKind] = useState<QualificationTargetKind>("web");
  const [application, setApplication] = useState("");
  const [applicationVersion, setApplicationVersion] = useState("");
  const [environmentLabel, setEnvironmentLabel] = useState("");
  const [capabilities, setCapabilities] = useState("");
  const [minimumTier, setMinimumTier] = useState(3);

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
      if (response.migration_required && !application) {
        setApplication(response.graph.bundle.name);
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

  async function initializeQualification() {
    setBusy("initialize");
    setError("");
    try {
      const response = await engineInvoke<QualificationResponse>(
        CMD.INITIALIZE_QUALIFICATION,
        {
          workflow_id: workflowId,
          target_kind: targetKind,
          application,
          application_version: applicationVersion,
          environment_label: environmentLabel,
          required_capabilities: capabilities
            .split(",")
            .map((value) => value.trim())
            .filter(Boolean),
          minimum_effect_tier: minimumTier,
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
          explanation: `Operator reviewed this action as ${risk.replaceAll("_", " ")}`,
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
          verification_tier: verificationTier,
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
          {project.migration_required && (
            <Card>
              <CardHead
                eyebrow="Environment boundary"
                title="Start the qualification project"
                sub="Bind this compiled workflow to the application and operator-defined environment contract it will be qualified in. Desktop hashes the trimmed identifier locally; it does not claim to measure the machine automatically."
              />
              <div className="grid grid-2">
                <div className="field">
                  <label htmlFor="qualification-target">Execution surface</label>
                  <select
                    id="qualification-target"
                    className="input"
                    value={targetKind}
                    onChange={(event) => {
                      const target = event.target.value as QualificationTargetKind;
                      setTargetKind(target);
                    }}
                  >
                    <option value="web">Browser</option>
                    <option value="windows">Windows</option>
                    <option value="macos">macOS</option>
                    <option value="linux">Linux</option>
                    <option value="rdp">RDP</option>
                    <option value="citrix">Citrix</option>
                  </select>
                </div>
                <div className="field">
                  <label htmlFor="qualification-app">Application</label>
                  <input
                    id="qualification-app"
                    className="input"
                    value={application}
                    onChange={(event) => setApplication(event.target.value)}
                    placeholder="Accuro"
                  />
                </div>
                <div className="field">
                  <label htmlFor="qualification-app-version">
                    Application version
                  </label>
                  <input
                    id="qualification-app-version"
                    className="input"
                    value={applicationVersion}
                    onChange={(event) => setApplicationVersion(event.target.value)}
                    placeholder="2026.1"
                  />
                </div>
                <div className="field">
                  <label htmlFor="qualification-environment">
                    Operator-defined environment identifier
                  </label>
                  <input
                    id="qualification-environment"
                    className="input"
                    value={environmentLabel}
                    onChange={(event) => setEnvironmentLabel(event.target.value)}
                    placeholder="clinic-test-citrix-vda"
                  />
                  <span className="page-sub">
                    SHA-256 of the trimmed UTF-8 identifier. Configure the
                    qualification runner with this same identifier, or initialize
                    through the API with a measured environment digest.
                  </span>
                </div>
                <div className="field">
                  <label htmlFor="qualification-capabilities">
                    Required runner capabilities
                  </label>
                  <input
                    id="qualification-capabilities"
                    className="input"
                    value={capabilities}
                    onChange={(event) => setCapabilities(event.target.value)}
                    placeholder="Use names advertised by the selected runner"
                  />
                  <span className="page-sub">
                    Optional, comma-separated, and operator-reviewed. Desktop does
                    not invent capability names; signed case evidence must advertise
                    every value entered here.
                  </span>
                </div>
                <div className="field">
                  <label htmlFor="qualification-minimum-tier">
                    Minimum verification strength
                  </label>
                  <select
                    id="qualification-minimum-tier"
                    className="input"
                    value={minimumTier}
                    onChange={(event) => setMinimumTier(Number(event.target.value))}
                  >
                    <option value={1}>Tier 1 · independent system</option>
                    <option value={2}>Tier 2 · independent session</option>
                    <option value={3}>Tier 3 · persisted-state reacquisition</option>
                    <option value={4}>Tier 4 · immediate screen</option>
                  </select>
                </div>
              </div>
              <Callout tone="info" title="Existing bundles migrate safely">
                Starting the project keeps the compiled graph, parameters, identity
                evidence, effects, and encryption mode. Any older policy-only
                certification is invalidated because it does not contain this
                versioned environment and evidence contract.
              </Callout>
              <Button
                variant="primary"
                disabled={
                  busy === "initialize" ||
                  !application.trim() ||
                  !applicationVersion.trim() ||
                  !environmentLabel.trim()
                }
                onClick={() => void initializeQualification()}
              >
                {busy === "initialize"
                  ? "Starting qualification…"
                  : "Start qualification project"}
              </Button>
            </Card>
          )}

          <Card>
            <CardHead
              eyebrow={project.qualification_schema}
              title="Qualification contract"
              sub={
                digest
                  ? `sealed bundle ${digest.slice(0, 16)}…`
                  : "bundle seal is being prepared"
              }
            />
            <div className="metrics">
              <div className="metric">
                <span className="label">Actions reviewed</span>
                <span className="metric-value">
                  {project.project
                    ? Object.keys(project.controls.actions).filter(
                        (id) =>
                          project.controls.actions[id].classification
                            ?.operator_confirmed,
                      ).length
                    : 0}
                  /{project.report.action_count}
                </span>
              </div>
              <div className="metric">
                <span className="label">Consequential</span>
                <span className="metric-value">
                  {project.report.consequential_action_count}
                </span>
              </div>
              <div className="metric">
                <span className="label">Identity coverage</span>
                <span className="metric-value">
                  {project.report.identity_covered_action_count}/
                  {project.report.consequential_action_count}
                </span>
              </div>
              <div className="metric">
                <span className="label">Effect coverage</span>
                <span className="metric-value">
                  {project.report.effect_covered_action_count}/
                  {project.report.effect_required_action_count}
                </span>
              </div>
            </div>
            <div className="row" style={{ marginTop: "var(--space-4)" }}>
              {state && <Pill tone={state.tone}>{state.label}</Pill>}
              <span className="page-sub">
                {project.project
                  ? `${project.project.environment.application} ${project.project.environment.application_version} · ${project.project.environment.target_kind}`
                  : "environment boundary not initialized"}
              </span>
              <span className="spacer" />
              <Button
                variant="primary"
                disabled={busy === "certify" || project.migration_required}
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
                  <th>Target evidence</th>
                  <th>Next</th>
                </tr>
              </thead>
              <tbody>
                {project.graph.nodes.map((node) => (
                  <tr key={`graph-${node.id}`}>
                    <td className="num">{node.index + 1}</td>
                    <td>
                      <strong>{actionTitle(node)}</strong>
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
                      <TargetEvidence node={node} />
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
                      <strong>{actionTitle(node)}</strong>
                      <div className="page-sub mono">
                        {node.action} · {node.id}
                      </div>
                      <TargetEvidence node={node} />
                    </td>
                    <td>
                      <select
                        className="input"
                        aria-label={`Risk for ${actionTitle(node)}`}
                        value={
                          project.controls.actions[node.id]?.classification
                            ?.classification === "unknown"
                            ? ""
                            : project.controls.actions[node.id]?.classification
                                ?.classification || ""
                        }
                        disabled={busy === node.id}
                        onChange={(event) =>
                          void setRisk(
                            node.id,
                            event.target.value as QualificationRisk,
                          )
                        }
                      >
                        <option value="" disabled>
                          Review required
                        </option>
                        <option value="read_only">Read-only</option>
                        <option value="state_changing">State-changing</option>
                        <option value="consequential">Consequential</option>
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
                    {project.controls.actions[action.id]?.classification
                      ?.classification === "consequential" ||
                    project.controls.actions[action.id]?.classification
                      ?.classification === "irreversible"
                      ? "Consequential: "
                      : ""}
                    {actionTitle(action)}
                  </option>
                ))}
              </select>
            </div>

            {selectedAction && selectedControls && project.project && (
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
                      <div className="field">
                        <label htmlFor="effect-tier">Verification strength</label>
                        <select
                          id="effect-tier"
                          className="input"
                          value={verificationTier}
                          onChange={(event) =>
                            setVerificationTier(Number(event.target.value))
                          }
                        >
                          <option value={1}>Tier 1 · independent system</option>
                          <option value={2}>Tier 2 · independent session</option>
                          <option value={3}>
                            Tier 3 · persisted-state reacquisition
                          </option>
                          <option value={4}>Tier 4 · immediate screen</option>
                        </select>
                        <span className="page-sub">
                          The project requires Tier{" "}
                          {project.project?.minimum_effect_tier ?? minimumTier} or
                          stronger. Consequential writes cannot qualify from the
                          current screen alone.
                        </span>
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

          {project.project && (
            <Card>
              <CardHead
                eyebrow={`Revision ${project.project.revision}`}
                title="Qualification cases"
                sub="Representative runs must finish VERIFIED. Ambiguity, wrong or stale identity, and weak or missing effects must halt with signed evidence from the declared runner."
              />
              <div className="metrics">
                <div className="metric">
                  <span className="label">Required cases</span>
                  <span className="metric-value">{project.report.case_count}</span>
                </div>
                <div className="metric">
                  <span className="label">Passed this revision</span>
                  <span className="metric-value">
                    {project.report.passed_case_count}
                  </span>
                </div>
                <div className="metric">
                  <span className="label">Environment</span>
                  <span className="metric-value mono">
                    {project.project.environment.environment_digest.slice(0, 10)}…
                  </span>
                </div>
                <div className="metric">
                  <span className="label">Runtime</span>
                  <span className="metric-value">
                    {project.project.environment.runtime_version}
                  </span>
                </div>
              </div>
              <div className="page-sub mono" style={{ marginTop: "var(--space-3)" }}>
                environment contract SHA-256{" "}
                {project.project.environment.environment_digest}
              </div>
              <table>
                <thead>
                  <tr>
                    <th>Case</th>
                    <th>Expected</th>
                    <th>Current evidence</th>
                  </tr>
                </thead>
                <tbody>
                  {project.project.cases
                    .filter((item) => item.required)
                    .map((item) => (
                      <tr key={item.id}>
                        <td>
                          <strong>{item.kind.replaceAll("_", " ")}</strong>
                          <div className="page-sub mono">{item.id}</div>
                        </td>
                        <td>{item.expected_outcome.toUpperCase()}</td>
                        <td>
                          <Pill tone={item.results.length ? "ok" : "warn"}>
                            {item.results.length
                              ? `${item.results.length} imported`
                              : "run required"}
                          </Pill>
                        </td>
                      </tr>
                    ))}
                </tbody>
              </table>
            </Card>
          )}

          {(project.report.refusals.length > 0 ||
            project.lint.findings.length > 0) && (
            <Card>
              <CardHead
                eyebrow="Refusals"
                title="What must be resolved"
                sub="Every item names the exact action and contract that prevented certification."
              />
              {project.report.refusals.map((refusal, index) => (
                <Callout
                  key={`${refusal.code}-${refusal.step_id || refusal.case_id || index}`}
                  tone="warn"
                  title={
                    refusal.step_id ||
                    refusal.case_id ||
                    refusal.code.replaceAll("_", " ")
                  }
                >
                  {refusal.message}
                  <div className="page-sub mono">{refusal.path}</div>
                </Callout>
              ))}
              {project.lint.findings
                .filter(
                  (finding) =>
                    !project.report.refusals.some(
                      (refusal) =>
                        refusal.step_id === finding.step_id &&
                        refusal.message.includes(finding.message),
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
