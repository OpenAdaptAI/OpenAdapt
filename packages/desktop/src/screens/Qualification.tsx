import { useEffect, useMemo, useState } from "react";
import { CMD, engineInvoke } from "../lib/engine";
import type {
  QualificationEffectKind,
  QualificationIdentityEnforcement,
  QualificationIdentityMatch,
  QualificationIdentityNormalizer,
  QualificationIdentitySignalKey,
  QualificationIdentitySourceKind,
  QualificationNode,
  QualificationProject,
  QualificationResponse,
  QualificationRisk,
  QualificationTargetKind,
} from "../lib/types";
import { Button, Callout, Card, CardHead, Pill } from "../ui/primitives";
import { QualificationJourney } from "../ui/QualificationJourney";
import { BusinessDecisionAuthoring } from "../ui/BusinessDecisionAuthoring";
import { QualificationLifecycle } from "./QualificationLifecycle";

const POLICY = "clinical-write";
const IDENTITY_NORMALIZERS: {
  value: QualificationIdentityNormalizer;
  label: string;
}[] = [
  { value: "unicode_nfkc", label: "Unicode NFKC" },
  { value: "casefold", label: "Case-insensitive" },
  { value: "collapse_whitespace", label: "Collapse whitespace" },
  { value: "strip_punctuation", label: "Ignore punctuation" },
];
const IDENTITY_REFUSALS = new Set([
  "step_identity_unarmed",
  "identity_policy_missing",
  "identity_policy_unenforced",
  "identity_signal_unavailable",
]);
const IDENTITY_KEYS: {
  value: QualificationIdentitySignalKey;
  label: string;
}[] = [
  { value: "subject_name", label: "Subject name" },
  { value: "record_id", label: "Record identifier" },
  { value: "secondary_identifier", label: "Secondary identifier" },
  { value: "application", label: "Application" },
  { value: "session", label: "Session" },
  { value: "workflow_state", label: "Workflow state" },
];
const DEDICATED_IDENTITY_SOURCES = new Set<QualificationIdentitySourceKind>([
  "application",
  "session",
  "workflow_state",
]);
const EFFECT_REFUSALS = new Set([
  "effect_contract_missing",
  "effect_policy_missing",
  "effect_contract_changed",
  "effect_tier_insufficient",
  "high_risk_screen_only",
]);

interface IdentitySignalDraft {
  id: string;
  key: QualificationIdentitySignalKey;
  source: QualificationIdentitySourceKind;
  match: QualificationIdentityMatch;
  normalizers: QualificationIdentityNormalizer[];
  region: string;
  extractPattern: string;
  expectedValue: string;
  params: string;
}

function defaultIdentityKey(
  source: QualificationIdentitySourceKind,
): QualificationIdentitySignalKey {
  if (source === "application" || source === "session" || source === "workflow_state") {
    return source;
  }
  return "record_id";
}

function encodedRegion(
  region?: [number, number, number, number] | null,
): string {
  return region?.join(", ") || "";
}

function parsedRegion(value: string): [number, number, number, number] | null {
  const parts = value
    .split(",")
    .map((item) => Number(item.trim()));
  if (
    parts.length !== 4 ||
    parts.some((item) => !Number.isInteger(item)) ||
    parts[2] <= 0 ||
    parts[3] <= 0
  ) {
    return null;
  }
  return parts as [number, number, number, number];
}

function identityKeysForSource(
  source: QualificationIdentitySourceKind,
): QualificationIdentitySignalKey[] {
  if (source === "application" || source === "session" || source === "workflow_state") {
    return [source];
  }
  return ["subject_name", "record_id", "secondary_identifier"];
}

function signalNeedsRegion(source: QualificationIdentitySourceKind): boolean {
  return source === "identifier_region" || source === "captured_context";
}

function signalNeedsExtraction(source: QualificationIdentitySourceKind): boolean {
  return source === "structured" || source === "captured_context";
}

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

function scrollToQualificationSection(id: string) {
  window.requestAnimationFrame(() =>
    document.getElementById(id)?.scrollIntoView({
      behavior: "smooth",
      block: "start",
    }),
  );
}

export function Qualification({
  workflowId,
  onBack,
  onOpenWorkflow = () => undefined,
}: {
  workflowId: string;
  onBack: () => void;
  onOpenWorkflow?: (workflowId: string) => void;
}) {
  const [project, setProject] = useState<QualificationProject | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");
  const [contractActionId, setContractActionId] = useState("");
  const [identityEnforcement, setIdentityEnforcement] =
    useState<QualificationIdentityEnforcement>("canonical_ladder");
  const [identitySignals, setIdentitySignals] = useState<IdentitySignalDraft[]>([]);
  const [identityQuorum, setIdentityQuorum] = useState(1);
  const [identityDraftActionId, setIdentityDraftActionId] = useState("");
  const [selectedEffectIndex, setSelectedEffectIndex] = useState("");
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
  const [bindingVerificationTier, setBindingVerificationTier] = useState(3);
  const [targetKind, setTargetKind] = useState<QualificationTargetKind>("web");
  const [application, setApplication] = useState("");
  const [applicationVersion, setApplicationVersion] = useState("");
  const [environmentLabel, setEnvironmentLabel] = useState("");
  const [capabilities, setCapabilities] = useState("");
  const [minimumTier, setMinimumTier] = useState(3);
  const [projectMinimumTier, setProjectMinimumTier] = useState(3);

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
  const selectedEffect = selectedControls?.effects.find(
    (effect) => String(effect.index) === selectedEffectIndex,
  );
  const selectedMatchParam = matchParam || parameters[0]?.name || "";
  const selectedValueParam = valueParam || parameters[0]?.name || "";
  const uncoveredConsequentialActions = useMemo(() => {
    if (!project) return [];
    return actions
      .map((action) => {
        const controls = project.controls.actions[action.id];
        if (
          controls?.classification?.classification !== "consequential" &&
          controls?.classification?.classification !== "irreversible"
        ) {
          return null;
        }
        const refusals = project.report.refusals.filter(
          (refusal) =>
            refusal.step_id === controls.step_id &&
            (IDENTITY_REFUSALS.has(refusal.code) ||
              EFFECT_REFUSALS.has(refusal.code)),
        );
        return refusals.length ? { action, refusals } : null;
      })
      .filter(
        (
          item,
        ): item is {
          action: QualificationNode;
          refusals: QualificationProject["report"]["refusals"];
        } => item !== null,
      );
  }, [actions, project]);

  useEffect(() => {
    if (!selectedControls || identityDraftActionId === selectedActionId) return;
    const policy = selectedControls.identity.policy;
    setIdentityEnforcement(policy?.enforcement || "canonical_ladder");
    setIdentitySignals(
      (policy?.signals || []).map((signal, index) => ({
        id: `${selectedActionId}-${signal.key}-${index}`,
        key: signal.key,
        source: signal.source,
        match: signal.match,
        normalizers: signal.normalizers,
        region: encodedRegion(signal.region),
        extractPattern: signal.extract_pattern || "",
        expectedValue: signal.expected_value || "",
        params: (signal.params || []).join(", "),
      })),
    );
    setIdentityQuorum(Math.max(1, policy?.quorum || 1));
    const firstEffect =
      selectedControls.effects.find((effect) => effect.verification_tier == null) ||
      selectedControls.effects[0];
    setSelectedEffectIndex(firstEffect ? String(firstEffect.index) : "");
    setVerificationTier(
      firstEffect?.verification_tier || project?.project?.minimum_effect_tier || 3,
    );
    setIdentityDraftActionId(selectedActionId);
  }, [
    identityDraftActionId,
    project?.project?.minimum_effect_tier,
    selectedActionId,
    selectedControls,
  ]);

  useEffect(() => {
    if (project?.project) {
      setProjectMinimumTier(project.project.minimum_effect_tier);
      setBindingVerificationTier(project.project.minimum_effect_tier);
    }
  }, [project?.project?.minimum_effect_tier]);

  useEffect(() => {
    if (
      project?.project &&
      selectedEffect &&
      selectedEffect.verification_tier == null
    ) {
      setVerificationTier(project.project.minimum_effect_tier);
    }
  }, [project?.project?.minimum_effect_tier, selectedEffect]);

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

  function addIdentitySignal() {
    const source = selectedControls?.identity.sources[0]?.kind;
    if (!source) return;
    const duplicateCount = identitySignals.filter(
      (signal) => signal.source === source,
    ).length;
    setIdentitySignals((current) => [
      ...current,
      {
        id: `${selectedActionId}-${source}-${Date.now()}-${duplicateCount}`,
        key: defaultIdentityKey(source),
        source,
        match: "exact",
        normalizers: [],
        region: encodedRegion(
          selectedControls?.identity.sources.find(
            (candidate) => candidate.kind === source,
          )?.region,
        ),
        extractPattern: "",
        expectedValue: "",
        params: "",
      },
    ]);
  }

  function updateIdentitySignal(
    id: string,
    update: Partial<Omit<IdentitySignalDraft, "id">>,
  ) {
    setIdentitySignals((current) =>
      current.map((signal) =>
        signal.id === id ? { ...signal, ...update } : signal,
      ),
    );
  }

  async function saveIdentity() {
    if (!selectedActionId) return;
    setBusy("identity");
    setError("");
    try {
      const response = await engineInvoke<QualificationResponse>(
        CMD.SET_QUALIFICATION_IDENTITY,
        {
          workflow_id: workflowId,
          step_id: selectedActionId,
          enforcement: identityEnforcement,
          signals:
            identityEnforcement === "signal_quorum"
              ? identitySignals.map((signal) => ({
                  key: signal.key,
                  source: signal.source,
                  match: signal.match,
                  normalizers:
                    signal.match === "normalized" ? signal.normalizers : [],
                  region:
                    signal.source === "identifier_region" ||
                    signal.source === "captured_context"
                      ? parsedRegion(signal.region)
                      : null,
                  extract_pattern:
                    signal.source === "structured" ||
                    signal.source === "captured_context"
                      ? signal.extractPattern.trim()
                      : null,
                  expected_value: DEDICATED_IDENTITY_SOURCES.has(signal.source)
                    ? signal.expectedValue.trim()
                    : null,
                  params: DEDICATED_IDENTITY_SOURCES.has(signal.source)
                    ? []
                    : signal.params
                        .split(",")
                        .map((value) => value.trim())
                        .filter(Boolean),
                }))
              : [],
          quorum: identityEnforcement === "signal_quorum" ? identityQuorum : 0,
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

  async function setEffectVerification() {
    if (!selectedActionId || !selectedEffect) return;
    setBusy("effect-verification");
    setError("");
    try {
      const response = await engineInvoke<QualificationResponse>(
        CMD.SET_QUALIFICATION_EFFECT_VERIFICATION,
        {
          workflow_id: workflowId,
          step_id: selectedActionId,
          effect_index: selectedEffect.index,
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

  async function setMinimumEffectTier() {
    setBusy("minimum-tier");
    setError("");
    try {
      const response = await engineInvoke<QualificationResponse>(
        CMD.SET_QUALIFICATION_MINIMUM_EFFECT_TIER,
        {
          workflow_id: workflowId,
          minimum_effect_tier: projectMinimumTier,
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
          verification_tier: bindingVerificationTier,
          policy: POLICY,
        },
      );
      if (!response.ok) {
        setError(response.error);
        return;
      }
      setProject(response);
      const effects = response.controls.actions[selectedActionId]?.effects || [];
      const bound =
        effects.find((effect) => effect.index === placeholder?.index) ||
        effects[effects.length - 1];
      if (bound) {
        setSelectedEffectIndex(String(bound.index));
        setVerificationTier(
          bound.verification_tier ||
            response.project?.minimum_effect_tier ||
            verificationTier,
        );
      }
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

  function openActionContract(stepId: string) {
    setContractActionId(stepId);
    scrollToQualificationSection("qualification-contract-section");
  }

  function openRefusal(
    refusal: QualificationProject["report"]["refusals"][number],
  ) {
    if (refusal.step_id) {
      openActionContract(refusal.step_id);
      return;
    }
    scrollToQualificationSection(
      refusal.case_id
        ? "qualification-cases-section"
        : "qualification-summary-section",
    );
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
          <QualificationJourney project={project} />

          {project.migration_required && (
            <Card id="qualification-environment-section">
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

          <Card id="qualification-summary-section">
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
            {project.project && (
              <div
                className="row"
                style={{
                  marginTop: "var(--space-4)",
                  paddingTop: "var(--space-4)",
                  borderTop: "1px solid var(--border)",
                }}
              >
                <div className="field" style={{ marginBottom: 0, minWidth: 280 }}>
                  <label htmlFor="project-minimum-effect-tier">
                    Workflow minimum verification tier
                  </label>
                  <select
                    id="project-minimum-effect-tier"
                    className="input"
                    value={projectMinimumTier}
                    onChange={(event) =>
                      setProjectMinimumTier(Number(event.target.value))
                    }
                  >
                    <option value={1}>Tier 1 · independent system</option>
                    <option value={2}>Tier 2 · independent session</option>
                    <option value={3}>
                      Tier 3 · persisted-state reacquisition
                    </option>
                    <option value={4}>Tier 4 · immediate screen</option>
                  </select>
                </div>
                <Button
                  disabled={
                    busy === "minimum-tier" ||
                    projectMinimumTier === project.project.minimum_effect_tier
                  }
                  onClick={() => void setMinimumEffectTier()}
                >
                  {busy === "minimum-tier" ? "Saving…" : "Save minimum"}
                </Button>
                <span className="page-sub">
                  Changing this advances the project revision and invalidates
                  certification and case evidence from the prior revision.
                </span>
              </div>
            )}
          </Card>

          {uncoveredConsequentialActions.length > 0 && (
            <Card>
              <CardHead
                eyebrow="Consequential coverage"
                title="Actions still missing identity or effect coverage"
                sub="Open the exact action below to complete its retained identity and independent effect-verification contract."
              />
              {uncoveredConsequentialActions.map(({ action, refusals }) => (
                <div
                  className="row"
                  key={`uncovered-${action.id}`}
                  style={{
                    alignItems: "flex-start",
                    marginBottom: "var(--space-3)",
                  }}
                >
                  <div style={{ flex: 1 }}>
                    <strong>{actionTitle(action)}</strong>
                    <div className="page-sub mono">{action.id}</div>
                    {refusals.map((refusal) => (
                      <div
                        className="page-sub"
                        key={`${action.id}-${refusal.code}`}
                      >
                        {refusal.code.replaceAll("_", " ")} · {refusal.message}
                      </div>
                    ))}
                  </div>
                  <Button onClick={() => openActionContract(action.id)}>
                    Edit contract
                  </Button>
                </div>
              ))}
            </Card>
          )}

          <Card id="qualification-graph-section">
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

          {project.project && (
            <BusinessDecisionAuthoring
              workflowId={workflowId}
              project={project}
              onProject={setProject}
            />
          )}

          <Card id="qualification-actions-section">
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

          <Card id="qualification-contract-section">
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
                    Choose the shipped identity ladder or declare named signals with
                    explicit comparison and quorum semantics.
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
                    {selectedControls.identity.policy && (
                      <Pill
                        tone={
                          selectedControls.identity.policy.enforcement ===
                          "canonical_ladder"
                            ? "ok"
                            : "warn"
                        }
                      >
                        {selectedControls.identity.policy.enforcement.replaceAll(
                          "_",
                          " ",
                        )}
                      </Pill>
                    )}
                    <span className="page-sub">
                      {selectedControls.identity.sources.length} identity source
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
                  {selectedControls.identity.sources.length > 0 && (
                    <>
                      <div className="field" style={{ marginTop: "var(--space-4)" }}>
                        <label htmlFor="identity-enforcement">Identity semantics</label>
                        <select
                          id="identity-enforcement"
                          className="input"
                          value={identityEnforcement}
                          onChange={(event) => {
                            const enforcement = event.target
                              .value as QualificationIdentityEnforcement;
                            setIdentityEnforcement(enforcement);
                            if (
                              enforcement === "signal_quorum" &&
                              identitySignals.length === 0
                            ) {
                              addIdentitySignal();
                            }
                          }}
                        >
                          <option value="canonical_ladder">
                            Canonical ladder · strongest retained evidence first
                          </option>
                          <option value="signal_quorum">
                            Named signals · explicit quorum
                          </option>
                        </select>
                      </div>

                      {identityEnforcement === "signal_quorum" && (
                        <>
                          {identitySignals.map((signal, index) => {
                            const retained = selectedControls.identity.sources.find(
                              (source) => source.kind === signal.source,
                            );
                            return (
                              <div
                                key={signal.id}
                                style={{
                                  borderTop: "1px solid var(--border)",
                                  marginTop: "var(--space-4)",
                                  paddingTop: "var(--space-4)",
                                }}
                              >
                                <div className="row">
                                  <strong>Identity field {index + 1}</strong>
                                  <span className="spacer" />
                                  <Button
                                    onClick={() => {
                                      setIdentitySignals((current) =>
                                        current.filter(
                                          (candidate) =>
                                            candidate.id !== signal.id,
                                        ),
                                      );
                                      setIdentityQuorum((current) =>
                                        Math.max(
                                          1,
                                          Math.min(
                                            current,
                                            identitySignals.length - 1,
                                          ),
                                        ),
                                      );
                                    }}
                                  >
                                    Remove
                                  </Button>
                                </div>
                                <div className="grid grid-2">
                                  <div className="field">
                                    <label htmlFor={`identity-key-${signal.id}`}>
                                      Identity signal
                                    </label>
                                    <select
                                      id={`identity-key-${signal.id}`}
                                      className="input"
                                      value={signal.key}
                                      onChange={(event) =>
                                        updateIdentitySignal(signal.id, {
                                          key: event.target
                                            .value as QualificationIdentitySignalKey,
                                        })
                                      }
                                    >
                                      {IDENTITY_KEYS.filter((key) =>
                                        identityKeysForSource(signal.source).includes(
                                          key.value,
                                        ),
                                      ).map((key) => (
                                        <option
                                          key={`${signal.id}-${key.value}`}
                                          value={key.value}
                                        >
                                          {key.label}
                                        </option>
                                      ))}
                                    </select>
                                  </div>
                                  <div className="field">
                                    <label htmlFor={`identity-source-${signal.id}`}>
                                      Evidence source
                                    </label>
                                    <select
                                      id={`identity-source-${signal.id}`}
                                      className="input"
                                      value={signal.source}
                                      onChange={(event) => {
                                        const source = event.target
                                          .value as QualificationIdentitySourceKind;
                                        const sourceEvidence =
                                          selectedControls.identity.sources.find(
                                            (candidate) =>
                                              candidate.kind === source,
                                          );
                                        updateIdentitySignal(signal.id, {
                                          source,
                                          key: defaultIdentityKey(source),
                                          region: encodedRegion(
                                            sourceEvidence?.region,
                                          ),
                                          extractPattern: "",
                                          expectedValue: "",
                                          params: "",
                                        });
                                      }}
                                    >
                                      {selectedControls.identity.sources.map(
                                        (source) => (
                                          <option
                                            key={`${signal.id}-${source.kind}`}
                                            value={source.kind}
                                          >
                                            {source.label}
                                          </option>
                                        ),
                                      )}
                                    </select>
                                    {retained?.region && (
                                      <span className="page-sub mono">
                                        retained region {retained.region.join(", ")}
                                      </span>
                                    )}
                                  </div>
                                  <div className="field">
                                    <label htmlFor={`identity-match-${signal.id}`}>
                                      Comparison
                                    </label>
                                    <select
                                      id={`identity-match-${signal.id}`}
                                      className="input"
                                      value={signal.match}
                                      onChange={(event) => {
                                        const match = event.target
                                          .value as QualificationIdentityMatch;
                                        updateIdentitySignal(signal.id, {
                                          match,
                                          normalizers:
                                            match === "normalized" &&
                                            signal.normalizers.length === 0
                                              ? [
                                                  "unicode_nfkc",
                                                  "collapse_whitespace",
                                                ]
                                              : signal.normalizers,
                                        });
                                      }}
                                    >
                                      <option value="exact">Exact</option>
                                      <option value="normalized">
                                        Normalized with explicit transforms
                                      </option>
                                    </select>
                                  </div>
                                  {DEDICATED_IDENTITY_SOURCES.has(signal.source) && (
                                    <div className="field">
                                      <label htmlFor={`identity-expected-${signal.id}`}>
                                        Qualified expected value
                                      </label>
                                      <input
                                        id={`identity-expected-${signal.id}`}
                                        className="input mono"
                                        value={signal.expectedValue}
                                        onChange={(event) =>
                                          updateIdentitySignal(signal.id, {
                                            expectedValue: event.target.value,
                                          })
                                        }
                                        placeholder={
                                          signal.source === "session"
                                            ? "64-character session digest"
                                            : signal.source === "application"
                                              ? "accuro or https://app.example"
                                              : "record-review"
                                        }
                                        spellCheck={false}
                                      />
                                      <span className="page-sub">
                                        PHI-free value compared with the live runtime
                                        observation immediately before actuation.
                                      </span>
                                    </div>
                                  )}
                                  {signalNeedsExtraction(signal.source) && (
                                    <div className="field">
                                      <label htmlFor={`identity-extract-${signal.id}`}>
                                        Value extraction pattern
                                      </label>
                                      <input
                                        id={`identity-extract-${signal.id}`}
                                        className="input mono"
                                        value={signal.extractPattern}
                                        onChange={(event) =>
                                          updateIdentitySignal(signal.id, {
                                            extractPattern: event.target.value,
                                          })
                                        }
                                        placeholder="Record ID:\\s*(?P<value>[A-Z0-9-]+)"
                                        spellCheck={false}
                                      />
                                      <span className="page-sub">
                                        One regular-expression group named value keeps
                                        unrelated text out of this identity vote.
                                      </span>
                                    </div>
                                  )}
                                  {!DEDICATED_IDENTITY_SOURCES.has(signal.source) && (
                                    <div className="field">
                                      <label htmlFor={`identity-params-${signal.id}`}>
                                        Parameter bindings
                                      </label>
                                      <input
                                        id={`identity-params-${signal.id}`}
                                        className="input mono"
                                        value={signal.params}
                                        onChange={(event) =>
                                          updateIdentitySignal(signal.id, {
                                            params: event.target.value,
                                          })
                                        }
                                        placeholder="record_id, date_of_birth"
                                        spellCheck={false}
                                      />
                                      <span className="page-sub">
                                        Optional comma-separated workflow parameters
                                        represented by this evidence.
                                      </span>
                                    </div>
                                  )}
                                  {signalNeedsRegion(signal.source) && (
                                    <div className="field">
                                      <label htmlFor={`identity-region-${signal.id}`}>
                                        Qualified screen region
                                      </label>
                                      <input
                                        id={`identity-region-${signal.id}`}
                                        className="input mono"
                                        value={signal.region}
                                        readOnly={signal.source === "identifier_region"}
                                        onChange={(event) =>
                                          updateIdentitySignal(signal.id, {
                                            region: event.target.value,
                                          })
                                        }
                                        placeholder="x, y, width, height"
                                        spellCheck={false}
                                      />
                                      {signal.source === "identifier_region" && (
                                        <span className="page-sub">
                                          Bound to the identifier region retained by
                                          the demonstration.
                                        </span>
                                      )}
                                    </div>
                                  )}
                                </div>
                                {signal.match === "normalized" && (
                                  <div className="field">
                                    <label>Allowed normalizers</label>
                                    {IDENTITY_NORMALIZERS.map((normalizer) => (
                                      <label
                                        className="check-row"
                                        key={`${signal.id}-${normalizer.value}`}
                                      >
                                        <input
                                          type="checkbox"
                                          checked={signal.normalizers.includes(
                                            normalizer.value,
                                          )}
                                          onChange={(event) => {
                                            const normalizers = event.target.checked
                                              ? [
                                                  ...signal.normalizers,
                                                  normalizer.value,
                                                ]
                                              : signal.normalizers.filter(
                                                  (value) =>
                                                    value !== normalizer.value,
                                                );
                                            updateIdentitySignal(signal.id, {
                                              normalizers,
                                            });
                                          }}
                                        />
                                        <span>{normalizer.label}</span>
                                      </label>
                                    ))}
                                  </div>
                                )}
                              </div>
                            );
                          })}
                          <div className="row" style={{ marginTop: "var(--space-4)" }}>
                            <Button onClick={addIdentitySignal}>
                              Add identity field
                            </Button>
                            <div className="field" style={{ marginBottom: 0 }}>
                              <label htmlFor="identity-quorum">
                                Required matching signals
                              </label>
                              <select
                                id="identity-quorum"
                                className="input"
                                value={Math.min(
                                  identityQuorum,
                                  Math.max(1, identitySignals.length),
                                )}
                                onChange={(event) =>
                                  setIdentityQuorum(Number(event.target.value))
                                }
                              >
                                {identitySignals.map((_, index) => (
                                  <option
                                    key={`identity-quorum-${index + 1}`}
                                    value={index + 1}
                                  >
                                    {index + 1} of {identitySignals.length}
                                  </option>
                                ))}
                              </select>
                            </div>
                          </div>
                        </>
                      )}
                    </>
                  )}
                  <div style={{ marginTop: "var(--space-4)" }}>
                    <Button
                      variant="primary"
                      disabled={
                        busy === "identity" ||
                        !selectedControls.identity.can_arm ||
                        (identityEnforcement === "signal_quorum" &&
                          (identitySignals.length === 0 ||
                            identitySignals.some(
                              (signal) =>
                                (signal.match === "normalized" &&
                                  signal.normalizers.length === 0) ||
                                (signalNeedsExtraction(signal.source) &&
                                  !signal.extractPattern.trim()) ||
                                (DEDICATED_IDENTITY_SOURCES.has(signal.source) &&
                                  !signal.expectedValue.trim()) ||
                                (signalNeedsRegion(signal.source) &&
                                  parsedRegion(signal.region) === null),
                            ) ||
                            new Set(identitySignals.map((signal) => signal.key))
                              .size !== identitySignals.length ||
                            new Set(identitySignals.map((signal) => signal.source))
                              .size !== identitySignals.length ||
                            identityQuorum < 1 ||
                            identityQuorum > identitySignals.length))
                      }
                      onClick={() => void saveIdentity()}
                    >
                      {busy === "identity"
                        ? "Saving…"
                        : selectedControls.identity.policy
                          ? "Save identity policy"
                          : "Arm identity check"}
                    </Button>
                  </div>
                </div>

                <div>
                  <h3>Persisted business effect</h3>
                  <p className="page-sub">
                    Bind the record identity and intended result to workflow parameters.
                    Required evidence and the concrete deployment verifier are reviewed
                    separately.
                  </p>
                  {selectedControls.effects.length > 0 ? (
                    <>
                      <div className="field">
                        <label htmlFor="declared-effect">Declared business effect</label>
                        <select
                          id="declared-effect"
                          className="input"
                          value={selectedEffectIndex}
                          onChange={(event) => {
                            const index = event.target.value;
                            const effect = selectedControls.effects.find(
                              (candidate) => String(candidate.index) === index,
                            );
                            setSelectedEffectIndex(index);
                            setVerificationTier(
                              effect?.verification_tier ||
                                project.project?.minimum_effect_tier ||
                                3,
                            );
                          }}
                        >
                          {selectedControls.effects.map((effect) => (
                            <option
                              key={`declared-effect-${effect.index}`}
                              value={effect.index}
                            >
                              #{effect.index + 1} {effect.kind.replaceAll("_", " ")}
                              {effect.needs_operator_confirmation
                                ? " · binding required"
                                : ""}
                            </option>
                          ))}
                        </select>
                        {selectedEffect && (
                          <>
                            <span className="page-sub mono">
                              {selectedEffect.effect_contract_hash}
                            </span>
                            <span className="page-sub">
                              Required verification tier:{" "}
                              {selectedEffect.verification_tier
                                ? `Tier ${selectedEffect.verification_tier}`
                                : "not assigned"}
                            </span>
                            <span className="page-sub">
                              Concrete verifier binding: not configured by this
                              requirement. Qualification must still exercise a matching
                              deployment verifier.
                            </span>
                          </>
                        )}
                      </div>
                      <div className="field">
                        <label htmlFor="declared-effect-tier">
                          Minimum evidence required for this effect
                        </label>
                        <select
                          id="declared-effect-tier"
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
                          {project.project.minimum_effect_tier} or stronger.
                          {project.project.minimum_effect_tier < 4
                            ? " Immediate-screen evidence is below the current minimum."
                            : " The current minimum permits immediate-screen evidence."}
                        </span>
                      </div>
                      <Button
                        disabled={
                          busy === "effect-verification" ||
                          !selectedEffect ||
                          selectedEffect.verification_tier === verificationTier
                        }
                        onClick={() => void setEffectVerification()}
                      >
                        {busy === "effect-verification"
                          ? "Saving…"
                          : "Save required tier"}
                      </Button>
                    </>
                  ) : (
                    <Callout tone="warn" title="No declared effect">
                      This action has no executable business-effect contract. Bind one
                      below before it can be covered.
                    </Callout>
                  )}
                  <div
                    style={{
                      borderTop: "1px solid var(--border)",
                      marginTop: "var(--space-4)",
                      paddingTop: "var(--space-4)",
                    }}
                  >
                    <strong>
                      {selectedControls.effects.some(
                        (effect) => effect.needs_operator_confirmation,
                      )
                        ? "Complete a retained effect"
                        : "Add an effect contract"}
                    </strong>
                  </div>
                  {!parameters.length ? (
                    <Callout tone="warn" title="Workflow parameters required">
                      Add typed workflow parameters before binding a reusable effect.
                      OpenAdapt will not seal a live identity value into this form.
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
                        <label htmlFor="effect-tier">
                          Minimum evidence required for this effect
                        </label>
                        <select
                          id="effect-tier"
                          className="input"
                          value={bindingVerificationTier}
                          onChange={(event) =>
                            setBindingVerificationTier(Number(event.target.value))
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
            <QualificationLifecycle
              workflowId={workflowId}
              project={project}
              onProject={setProject}
              onOpenWorkflow={onOpenWorkflow}
            />
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
                  <Button
                    size="sm"
                    style={{ marginTop: "var(--space-2)" }}
                    onClick={() => openRefusal(refusal)}
                  >
                    Open required control
                  </Button>
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
