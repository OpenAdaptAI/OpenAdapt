import { useEffect, useMemo, useState } from "react";
import { CMD, engineInvoke } from "../lib/engine";
import type {
  QualificationBusinessDecisionContract,
  QualificationBusinessDecisionOption,
  QualificationProject,
  QualificationResponse,
} from "../lib/types";
import { Button, Callout, Card, CardHead, Pill } from "./primitives";

interface OptionDraft {
  key: string;
  label: string;
  value: string;
  target: string;
  requiredEvidence: string[];
}

interface EvidenceDraft {
  key: string;
  id: string;
  label: string;
}

const blankOptions = (): OptionDraft[] => [
  {
    key: crypto.randomUUID(),
    label: "",
    value: "",
    target: "",
    requiredEvidence: [],
  },
  {
    key: crypto.randomUUID(),
    label: "",
    value: "",
    target: "",
    requiredEvidence: [],
  },
];

function optionDraft(option: QualificationBusinessDecisionOption): OptionDraft {
  return {
    key: option.id,
    label: option.label,
    value: option.value,
    target: option.target,
    requiredEvidence: [...option.required_evidence],
  };
}

function safeId(value: string, fallback: string): string {
  const id = value
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9._:-]+/g, "_")
    .replace(/^[_:.-]+|[_:.-]+$/g, "")
    .slice(0, 128);
  return id || fallback;
}

function decisionKey(graphId: string, stateId: string): string {
  return JSON.stringify([graphId, stateId]);
}

export function BusinessDecisionAuthoring({
  workflowId,
  project,
  onProject,
}: {
  workflowId: string;
  project: QualificationProject;
  onProject: (project: QualificationProject) => void;
}) {
  const controls = project.controls.business_decisions || {
    available: false,
    required_flow_capability: "qualification.set_business_decision" as const,
    graphs: [],
  };
  const [graphId, setGraphId] = useState(controls.graphs[0]?.id || "");
  const [editingStateId, setEditingStateId] = useState("");
  const [insertBefore, setInsertBefore] = useState("");
  const [stateId, setStateId] = useState("review_decision");
  const [question, setQuestion] = useState("");
  const [roles, setRoles] = useState("operator, supervisor");
  const [outputParam, setOutputParam] = useState("review_outcome");
  const [expiryMinutes, setExpiryMinutes] = useState(60);
  const [revalidationKind, setRevalidationKind] = useState<
    "anchor_resolves" | "text_present"
  >("anchor_resolves");
  const [revalidationState, setRevalidationState] = useState("");
  const [revalidationText, setRevalidationText] = useState("");
  const [options, setOptions] = useState<OptionDraft[]>(blankOptions);
  const [evidence, setEvidence] = useState<EvidenceDraft[]>([]);
  const [editingIsEditable, setEditingIsEditable] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const graph = useMemo(
    () => controls.graphs.find((item) => item.id === graphId),
    [controls.graphs, graphId],
  );
  const insertionStates = useMemo(
    () => graph?.states.filter((state) => state.can_insert_before) || [],
    [graph],
  );
  const targetStates = useMemo(
    () => graph?.states.filter((state) => state.id !== editingStateId) || [],
    [editingStateId, graph],
  );
  const anchorStates = useMemo(
    () => graph?.states.filter((state) => state.has_revalidation_anchor) || [],
    [graph],
  );
  const existingDecisions = useMemo(
    () =>
      controls.graphs.flatMap((candidateGraph) =>
        candidateGraph.states
          .filter((state) => state.decision)
          .map((state) => ({ graph: candidateGraph, state })),
      ),
    [controls.graphs],
  );
  const normalizedEvidenceIds = evidence.map((item) => safeId(item.id, ""));
  const invalidEvidence =
    evidence.some((item) => !item.id.trim() || !item.label.trim()) ||
    new Set(normalizedEvidenceIds).size !== normalizedEvidenceIds.length;

  useEffect(() => {
    if (!insertionStates.some((state) => state.id === insertBefore)) {
      setInsertBefore(insertionStates[0]?.id || "");
    }
    if (!anchorStates.some((state) => state.id === revalidationState)) {
      setRevalidationState(anchorStates[0]?.id || "");
    }
    setOptions((current) =>
      current.map((option, index) => ({
        ...option,
        target: targetStates.some((state) => state.id === option.target)
          ? option.target
          : targetStates[index]?.id || targetStates[0]?.id || "",
      })),
    );
  }, [anchorStates, insertBefore, insertionStates, revalidationState, targetStates]);

  function loadDecision(
    nextGraphId: string,
    nextStateId: string,
    decision: QualificationBusinessDecisionContract,
  ) {
    setGraphId(nextGraphId);
    setEditingStateId(nextStateId);
    setStateId(nextStateId);
    setQuestion(decision.question);
    setRoles(decision.authorized_roles.join(", "));
    setOutputParam(decision.output_param);
    setExpiryMinutes(Math.max(1, Math.round(decision.expires_after_s / 60)));
    setOptions(decision.options.map(optionDraft));
    setEvidence(
      decision.evidence_requirements.map((item) => ({
        key: item.id,
        id: item.id,
        label: item.label,
      })),
    );
    setEditingIsEditable(decision.editable);
    if (decision.revalidation?.kind === "text_present") {
      setRevalidationKind("text_present");
      setRevalidationText(decision.revalidation.text || "");
    } else {
      setRevalidationKind("anchor_resolves");
      setRevalidationState(decision.revalidation?.state_id || "");
    }
    setError(decision.editable ? "" : "This decision uses a revalidation contract that this form cannot safely replace.");
  }

  function startNew() {
    setEditingStateId("");
    setStateId("review_decision");
    setQuestion("");
    setRoles("operator, supervisor");
    setOutputParam("review_outcome");
    setExpiryMinutes(60);
    setOptions(blankOptions());
    setEvidence([]);
    setEditingIsEditable(true);
    setError("");
  }

  function updateOption(key: string, update: Partial<OptionDraft>) {
    setOptions((current) =>
      current.map((option) => (option.key === key ? { ...option, ...update } : option)),
    );
  }

  async function save() {
    if (!graph) return;
    setBusy(true);
    setError("");
    try {
      const response = await engineInvoke<QualificationResponse>(
        CMD.AUTHOR_QUALIFICATION_BUSINESS_DECISION,
        {
          workflow_id: workflowId,
          policy: project.policy,
          graph_id: graph.id,
          state_id: stateId,
          insert_before_state_id: editingStateId ? undefined : insertBefore,
          question,
          authorized_roles: roles
            .split(",")
            .map((role) => role.trim())
            .filter(Boolean),
          output_param: outputParam,
          expires_after_s: expiryMinutes * 60,
          evidence_requirements: evidence.map((item, index) => ({
            id: safeId(item.id, `evidence_${index + 1}`),
            label: item.label,
          })),
          options: options.map((option, index) => ({
            id: safeId(option.value || option.label, `option_${index + 1}`),
            label: option.label,
            value: option.value,
            target: option.target,
            required_evidence: option.requiredEvidence.map((key) => {
              const item = evidence.find((candidate) => candidate.key === key);
              return item ? safeId(item.id, "evidence") : key;
            }),
          })),
          revalidation_kind: revalidationKind,
          revalidation_state_id:
            revalidationKind === "anchor_resolves" ? revalidationState : undefined,
          revalidation_text:
            revalidationKind === "text_present" ? revalidationText : undefined,
        },
      );
      if (!response.ok) {
        setError(response.error);
        return;
      }
      onProject(response);
      setEditingStateId(stateId);
    } catch (reason) {
      setError(String(reason));
    } finally {
      setBusy(false);
    }
  }

  if (!controls.available) {
    return (
      <Card id="qualification-business-decisions-section">
        <CardHead
          eyebrow="Human judgment"
          title="Qualified decision branches"
          sub="The qualification cockpit will use Flow's typed decision contract when the embedded runtime includes that capability."
        />
        <Callout tone="info" title="Runtime update required">
          Update the embedded OpenAdapt Flow runtime to author typed decisions. Risk,
          identity, effect, case, certification, and deployment controls remain
          available in this build.
        </Callout>
      </Card>
    );
  }

  return (
    <Card id="qualification-business-decisions-section">
      <CardHead
        eyebrow="Human judgment"
        title="Capture the choices that require institutional knowledge"
        sub="Add a reviewed question where the program must stop and ask an authorized person which qualified branch can continue."
      />

      <Callout tone="info" title="A decision selects a branch. It does not prove success.">
        One signed answer selects one declared successor for this run. The runner
        checks the live application again before it continues. The successor still
        needs its normal authorization, identity, postcondition, and effect checks.
        An answer does not change future workflow policy.
      </Callout>

      {existingDecisions.length > 0 && (
        <div className="field">
          <label htmlFor="qualification-existing-decision">Existing decisions</label>
          <div className="row">
            <select
              id="qualification-existing-decision"
              className="input"
              value={editingStateId ? decisionKey(graphId, editingStateId) : ""}
              onChange={(event) => {
                if (!event.target.value) return startNew();
                const [nextGraphId, nextStateId] = JSON.parse(event.target.value) as [
                  string,
                  string,
                ];
                const item = existingDecisions.find(
                  (candidate) =>
                    candidate.graph.id === nextGraphId &&
                    candidate.state.id === nextStateId,
                );
                if (item?.state.decision) {
                  loadDecision(nextGraphId, nextStateId, item.state.decision);
                }
              }}
            >
              <option value="">Add a new decision</option>
              {existingDecisions.map((item) => (
                <option
                  key={`${item.graph.id}-${item.state.id}`}
                  value={decisionKey(item.graph.id, item.state.id)}
                >
                  {item.graph.label}: {item.state.decision?.question}
                </option>
              ))}
            </select>
            {editingStateId && <Button onClick={startNew}>Add another</Button>}
          </div>
        </div>
      )}

      <div className="grid grid-2">
        <div className="field">
          <label htmlFor="qualification-decision-graph">Workflow section</label>
          <select
            id="qualification-decision-graph"
            className="input"
            value={graphId}
            disabled={Boolean(editingStateId)}
            onChange={(event) => setGraphId(event.target.value)}
          >
            {controls.graphs.map((item) => (
              <option key={item.id} value={item.id}>
                {item.label}
              </option>
            ))}
          </select>
        </div>
        <div className="field">
          <label htmlFor="qualification-decision-position">
            {editingStateId ? "Decision node" : "Ask before this step"}
          </label>
          {editingStateId ? (
            <input
              id="qualification-decision-position"
              className="input mono"
              value={stateId}
              disabled
            />
          ) : (
            <select
              id="qualification-decision-position"
              className="input"
              value={insertBefore}
              onChange={(event) => setInsertBefore(event.target.value)}
            >
              {insertionStates.map((state) => (
                <option key={state.id} value={state.id}>
                  {state.title}
                </option>
              ))}
            </select>
          )}
        </div>
      </div>

      <div className="field">
        <label htmlFor="qualification-decision-question">Question for the operator</label>
        <textarea
          id="qualification-decision-question"
          className="input"
          rows={3}
          value={question}
          onChange={(event) => setQuestion(event.target.value)}
          placeholder="Which reviewed path should this item follow?"
        />
      </div>

      <div className="grid grid-2">
        <div className="field">
          <label htmlFor="qualification-decision-roles">Authorized roles</label>
          <input
            id="qualification-decision-roles"
            className="input"
            value={roles}
            onChange={(event) => setRoles(event.target.value)}
            placeholder="operator, supervisor"
          />
          <span className="page-sub">Use role names from the authenticated operator route.</span>
        </div>
        <div className="field">
          <label htmlFor="qualification-decision-expiry">Answer window</label>
          <div className="row">
            <input
              id="qualification-decision-expiry"
              className="input"
              type="number"
              min={1}
              max={10080}
              value={expiryMinutes}
              onChange={(event) => setExpiryMinutes(Number(event.target.value))}
            />
            <span className="page-sub">minutes</span>
          </div>
        </div>
      </div>

      <details open>
        <summary>Qualified answers and branches</summary>
        <div style={{ marginTop: "var(--space-3)" }}>
          {options.map((option, index) => (
            <div className="card-inset" key={option.key} style={{ marginBottom: 12 }}>
              <div className="row" style={{ marginBottom: 8 }}>
                <Pill tone="neutral">Answer {index + 1}</Pill>
                <span className="spacer" />
                {options.length > 2 && (
                  <Button
                    onClick={() =>
                      setOptions((current) =>
                        current.filter((item) => item.key !== option.key),
                      )
                    }
                  >
                    Remove
                  </Button>
                )}
              </div>
              <div className="grid grid-3">
                <div className="field">
                  <label htmlFor={`decision-option-label-${option.key}`}>
                    Answer shown to the operator
                  </label>
                  <input
                    id={`decision-option-label-${option.key}`}
                    className="input"
                    value={option.label}
                    onChange={(event) =>
                      updateOption(option.key, { label: event.target.value })
                    }
                  />
                </div>
                <div className="field">
                  <label htmlFor={`decision-option-value-${option.key}`}>
                    Recorded value
                  </label>
                  <input
                    id={`decision-option-value-${option.key}`}
                    className="input mono"
                    value={option.value}
                    onChange={(event) =>
                      updateOption(option.key, { value: event.target.value })
                    }
                  />
                </div>
                <div className="field">
                  <label htmlFor={`decision-option-target-${option.key}`}>
                    Qualified next step
                  </label>
                  <select
                    id={`decision-option-target-${option.key}`}
                    className="input"
                    value={option.target}
                    onChange={(event) =>
                      updateOption(option.key, { target: event.target.value })
                    }
                  >
                    {targetStates.map((state) => (
                      <option key={state.id} value={state.id}>
                        {state.title}
                      </option>
                    ))}
                  </select>
                </div>
              </div>
              {evidence.length > 0 && (
                <div className="row" style={{ alignItems: "flex-start" }}>
                  <span className="page-sub">Required local evidence:</span>
                  {evidence.map((item) => {
                    return (
                      <label className="checkbox-row" key={`${option.key}-${item.key}`}>
                        <input
                          type="checkbox"
                          checked={option.requiredEvidence.includes(item.key)}
                          onChange={(event) =>
                            updateOption(option.key, {
                              requiredEvidence: event.target.checked
                                ? [...option.requiredEvidence, item.key]
                                : option.requiredEvidence.filter(
                                    (value) => value !== item.key,
                                  ),
                            })
                          }
                        />
                        {item.label || safeId(item.id, "evidence")}
                      </label>
                    );
                  })}
                </div>
              )}
            </div>
          ))}
          {options.length < 4 && (
            <Button
              onClick={() =>
                setOptions((current) => [
                  ...current,
                  {
                    key: crypto.randomUUID(),
                    label: "",
                    value: "",
                    target: targetStates[0]?.id || "",
                    requiredEvidence: [],
                  },
                ])
              }
            >
              Add answer
            </Button>
          )}
        </div>
      </details>

      <details>
        <summary>Optional evidence requirements</summary>
        <div style={{ marginTop: "var(--space-3)" }}>
          {evidence.map((item) => (
            <div className="grid grid-3" key={item.key}>
              <div className="field">
                <label htmlFor={`decision-evidence-id-${item.key}`}>Evidence id</label>
                <input
                  id={`decision-evidence-id-${item.key}`}
                  className="input mono"
                  value={item.id}
                  onChange={(event) =>
                    setEvidence((current) =>
                      current.map((candidate) =>
                        candidate.key === item.key
                          ? { ...candidate, id: event.target.value }
                          : candidate,
                      ),
                    )
                  }
                />
              </div>
              <div className="field">
                <label htmlFor={`decision-evidence-label-${item.key}`}>
                  What the operator must review
                </label>
                <input
                  id={`decision-evidence-label-${item.key}`}
                  className="input"
                  value={item.label}
                  onChange={(event) =>
                    setEvidence((current) =>
                      current.map((candidate) =>
                        candidate.key === item.key
                          ? { ...candidate, label: event.target.value }
                          : candidate,
                      ),
                    )
                  }
                />
              </div>
              <Button
                onClick={() => {
                  setOptions((current) =>
                    current.map((option) => ({
                      ...option,
                      requiredEvidence: option.requiredEvidence.filter(
                        (key) => key !== item.key,
                      ),
                    })),
                  );
                  setEvidence((current) =>
                    current.filter((candidate) => candidate.key !== item.key),
                  );
                }}
              >
                Remove
              </Button>
            </div>
          ))}
          <Button
            onClick={() =>
              setEvidence((current) => [
                ...current,
                {
                  key: crypto.randomUUID(),
                  id: `reviewed_evidence_${current.length + 1}`,
                  label: "",
                },
              ])
            }
          >
            Add evidence requirement
          </Button>
        </div>
      </details>

      <details open>
        <summary>Live check before the selected branch continues</summary>
        <div className="grid grid-2" style={{ marginTop: "var(--space-3)" }}>
          <div className="field">
            <label htmlFor="qualification-decision-revalidation-kind">Check</label>
            <select
              id="qualification-decision-revalidation-kind"
              className="input"
              value={revalidationKind}
              onChange={(event) =>
                setRevalidationKind(
                  event.target.value as "anchor_resolves" | "text_present",
                )
              }
            >
              <option value="anchor_resolves">A retained target still resolves</option>
              <option value="text_present">Reviewed text is still visible</option>
            </select>
          </div>
          {revalidationKind === "anchor_resolves" ? (
            <div className="field">
              <label htmlFor="qualification-decision-revalidation-state">Target</label>
              <select
                id="qualification-decision-revalidation-state"
                className="input"
                value={revalidationState}
                onChange={(event) => setRevalidationState(event.target.value)}
              >
                {anchorStates.map((state) => (
                  <option key={state.id} value={state.id}>
                    {state.title}
                  </option>
                ))}
              </select>
            </div>
          ) : (
            <div className="field">
              <label htmlFor="qualification-decision-revalidation-text">Visible text</label>
              <input
                id="qualification-decision-revalidation-text"
                className="input"
                value={revalidationText}
                onChange={(event) => setRevalidationText(event.target.value)}
              />
            </div>
          )}
        </div>
      </details>

      <details>
        <summary>Stable workflow identifiers</summary>
        <div className="grid grid-2" style={{ marginTop: "var(--space-3)" }}>
          <div className="field">
            <label htmlFor="qualification-decision-state-id">Decision state id</label>
            <input
              id="qualification-decision-state-id"
              className="input mono"
              value={stateId}
              disabled={Boolean(editingStateId)}
              onChange={(event) => setStateId(event.target.value)}
            />
          </div>
          <div className="field">
            <label htmlFor="qualification-decision-output">Decision output</label>
            <input
              id="qualification-decision-output"
              className="input mono"
              value={outputParam}
              disabled={Boolean(editingStateId)}
              onChange={(event) => setOutputParam(event.target.value)}
            />
          </div>
        </div>
      </details>

      {error && <Callout tone="crit" title="Decision contract not saved">{error}</Callout>}
      <div className="row" style={{ marginTop: "var(--space-4)" }}>
        <Button
          variant="primary"
          disabled={
            busy ||
            !editingIsEditable ||
            !question.trim() ||
            !roles.trim() ||
            !stateId.trim() ||
            !outputParam.trim() ||
            invalidEvidence ||
            options.some(
              (option) =>
                !option.label.trim() || !option.value.trim() || !option.target,
            ) ||
            (revalidationKind === "anchor_resolves" && !revalidationState) ||
            (revalidationKind === "text_present" && !revalidationText.trim())
          }
          onClick={() => void save()}
          data-testid="save-business-decision"
        >
          {busy ? "Saving…" : editingStateId ? "Save decision version" : "Add decision"}
        </Button>
        <span className="page-sub">
          Saving creates a new qualification revision. Existing case evidence must be
          run again before certification.
        </span>
      </div>
    </Card>
  );
}
