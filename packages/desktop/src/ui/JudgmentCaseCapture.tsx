import { useMemo, useState } from "react";
import type {
  JudgmentFactFieldV1,
  JudgmentCaseCaptureContextV1,
  JudgmentCaseV1,
  JudgmentDispositionV1,
  LocalEvidenceRefV1,
} from "../lib/types";
import { Button, Callout, Card, CardHead, Pill, SegControl } from "./primitives";

type FactValue = boolean | number | string;

type JudgmentSource = "demonstration" | "counterfactual" | "policy_review" | "fault";

const dispositionOptions: { value: JudgmentDispositionV1; label: string }[] = [
  { value: "automatic_rule", label: "Rule candidate" },
  { value: "human_node", label: "Keep human decision" },
  { value: "more_evidence_required", label: "More examples" },
];

function caseId(): string {
  return `judgment_case_${crypto.randomUUID()}`;
}

function initialFacts(context: JudgmentCaseCaptureContextV1): Record<string, FactValue> {
  return Object.fromEntries(
    Object.entries(context.fact_schema.fields).map(([name, field]) => [
      name,
      field.type === "boolean" ? false : field.type === "integer" || field.type === "number" ? 0 : "",
    ]),
  );
}

function factFingerprint(facts: Record<string, FactValue>): string {
  return JSON.stringify(
    Object.entries(facts).sort(([left], [right]) => left.localeCompare(right)),
  );
}

function newRef(): LocalEvidenceRefV1 {
  return { relative_path: "", sha256: "", kind: "document" };
}

/**
 * Collect reviewed examples and counterfactuals for a single existing Flow
 * decision. This component has no runner, signer, or remote-delivery path.
 */
export function JudgmentCaseCapture({
  context,
  onCapture,
}: {
  context: JudgmentCaseCaptureContextV1;
  onCapture: (caseItems: JudgmentCaseV1[]) => Promise<void>;
}) {
  const [source, setSource] = useState<JudgmentSource>("demonstration");
  const [sourceRefSha256, setSourceRefSha256] = useState("");
  const [facts, setFacts] = useState<Record<string, FactValue>>(() => initialFacts(context));
  const [optionId, setOptionId] = useState("");
  const [reviewedRuleId, setReviewedRuleId] = useState("");
  const [reviewerRole, setReviewerRole] = useState(context.authorized_roles[0] || "");
  const [reviewerPrincipalRef, setReviewerPrincipalRef] = useState("");
  const [addContrast, setAddContrast] = useState(false);
  const [contrastFacts, setContrastFacts] = useState<Record<string, FactValue>>(() =>
    initialFacts(context),
  );
  const [contrastOptionId, setContrastOptionId] = useState("");
  const [disposition, setDisposition] =
    useState<JudgmentDispositionV1>("human_node");
  const [evidence, setEvidence] = useState<LocalEvidenceRefV1[]>([]);
  const [note, setNote] = useState<LocalEvidenceRefV1 | null>(null);
  const [contrastCaseIds, setContrastCaseIds] = useState<string[]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const conflicts = useMemo(() => {
    const groups = new Map<string, Set<string>>();
    for (const item of context.cases) {
      if (!item.option_id) continue;
      const options = groups.get(factFingerprint(item.facts)) || new Set<string>();
      options.add(item.option_id);
      groups.set(factFingerprint(item.facts), options);
    }
    return [...groups.values()].filter((options) => options.size > 1).length;
  }, [context.cases]);
  const missingContrast = useMemo(
    () => contrastCaseIds.filter((id) => !context.cases.some((item) => item.id === id)),
    [context.cases, contrastCaseIds],
  );

  function validateReference(reference: LocalEvidenceRefV1, label: string): string | null {
    if (!reference.relative_path.trim() || !reference.sha256.trim() || !reference.kind.trim()) {
      return `${label} needs a local path, SHA-256, and kind.`;
    }
    if (!/^[a-f0-9]{64}$/i.test(reference.sha256.trim())) {
      return `${label} SHA-256 must contain 64 hexadecimal characters.`;
    }
    return null;
  }

  async function capture() {
    setError("");
    const pairedRule = disposition === "automatic_rule" && addContrast;
    if (!/^[a-f0-9]{64}$/i.test(sourceRefSha256.trim())) {
      setError("The local source reference needs a SHA-256 digest.");
      return;
    }
    if (!reviewerRole || !context.authorized_roles.includes(reviewerRole)) {
      setError("Select a reviewer role that the qualified decision permits.");
      return;
    }
    if (!/^[a-f0-9]{64}$/i.test(reviewerPrincipalRef.trim())) {
      setError("The local reviewer reference needs a SHA-256 digest.");
      return;
    }
    if (disposition === "automatic_rule" && (!optionId || !reviewedRuleId.trim())) {
      setError("A rule candidate needs a reviewed rule id and a qualified branch.");
      return;
    }
    if (!evidence.length) {
      setError("Add at least one local evidence reference for this reviewed case.");
      return;
    }
    for (const [index, reference] of evidence.entries()) {
      const invalid = validateReference(reference, `Evidence reference ${index + 1}`);
      if (invalid) return setError(invalid);
    }
    if (note) {
      const invalid = validateReference(note, "The local review note");
      if (invalid) return setError(invalid);
    }
    if (missingContrast.length) {
      setError("A contrast case must refer to a saved local case.");
      return;
    }
    if (pairedRule && factFingerprint(facts) === factFingerprint(contrastFacts)) {
      setError("A contrasting case must change at least one reviewed fact.");
      return;
    }
    if (pairedRule && !contrastOptionId) {
      setError("Select the qualified branch for the contrasting case.");
      return;
    }
    setBusy(true);
    try {
      const primaryId = caseId();
      const contrastId = pairedRule ? caseId() : null;
      const common = {
        decision: context.decision,
        fact_schema_sha256: context.fact_schema_sha256,
        local_evidence: evidence,
        review_note_ref: note,
        reviewed_rule_id: disposition === "automatic_rule" ? reviewedRuleId.trim() : null,
      };
      const primary: JudgmentCaseV1 = {
        ...common,
        id: primaryId,
        facts,
        provenance: {
          source,
          source_ref_sha256: sourceRefSha256.trim(),
          reviewer_role: reviewerRole,
          reviewer_principal_ref_sha256: reviewerPrincipalRef.trim(),
        },
        disposition,
        option_id: disposition === "automatic_rule" ? optionId : null,
        contrast_case_ids: contrastId ? [...contrastCaseIds, contrastId] : contrastCaseIds,
      };
      const pair = contrastId
        ? [{
            ...common,
            id: contrastId,
            facts: contrastFacts,
            provenance: {
              source: "counterfactual" as const,
              source_ref_sha256: sourceRefSha256.trim(),
              reviewer_role: reviewerRole,
              reviewer_principal_ref_sha256: reviewerPrincipalRef.trim(),
            },
            disposition: "automatic_rule" as const,
            option_id: contrastOptionId,
            contrast_case_ids: [primaryId],
          } satisfies JudgmentCaseV1]
        : [];
      await onCapture([primary, ...pair]);
      setFacts(initialFacts(context));
      setOptionId("");
      setReviewedRuleId("");
      setEvidence([]);
      setNote(null);
      setContrastCaseIds([]);
      setAddContrast(false);
      setContrastFacts(initialFacts(context));
      setContrastOptionId("");
    } catch (reason) {
      setError(String(reason));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card id="qualification-judgment-cases-section">
      <CardHead
        eyebrow="Judgment cases"
        title="Capture reviewed examples before you automate a choice"
        sub="Use this optional path when the choice depends on institutional policy, precedence, or local context. Direct branch authoring remains the fast path."
      />
      <Callout tone="info" title="An example is not a new policy.">
        A saved case can support a reviewed rule candidate. It cannot change a production
        branch. A qualified reviewer must approve the exact rule after coverage and fault
        checks pass. Otherwise, Flow keeps the human decision node.
      </Callout>

      <div className="grid grid-2" style={{ marginTop: "var(--space-4)" }}>
        <div className="field">
          <label>Case source</label>
          <SegControl
            value={source}
            onChange={setSource}
            options={[
              { value: "demonstration", label: "demonstration" },
              { value: "counterfactual", label: "counterfactual" },
              { value: "policy_review", label: "policy review" },
              { value: "fault", label: "fault" },
            ]}
          />
        </div>
        <div className="field">
          <label htmlFor="judgment-reviewer-role">Reviewer role</label>
          <select
            id="judgment-reviewer-role"
            className="input"
            value={reviewerRole}
            onChange={(event) => setReviewerRole(event.target.value)}
          >
            <option value="">Select an authorized role</option>
            {context.authorized_roles.map((role) => <option key={role} value={role}>{role}</option>)}
          </select>
        </div>
        <div className="field">
          <label htmlFor="judgment-reviewer-digest">Local reviewer reference SHA-256</label>
          <input
            id="judgment-reviewer-digest"
            className="input mono"
            value={reviewerPrincipalRef}
            onChange={(event) => setReviewerPrincipalRef(event.target.value)}
            placeholder="Digest of the authenticated local reviewer reference"
          />
        </div>
        <div className="field">
          <label htmlFor="judgment-source-digest">Local source SHA-256</label>
          <input
            id="judgment-source-digest"
            className="input mono"
            value={sourceRefSha256}
            onChange={(event) => setSourceRefSha256(event.target.value)}
            placeholder="Digest of the local demo, shadow run, or counterfactual source"
          />
          <span className="page-sub">The source stays local. Flow records only this reference.</span>
        </div>
        <div className="field">
          <label htmlFor="judgment-option">Qualified branch for a rule candidate</label>
          <select
            id="judgment-option"
            className="input"
            value={optionId}
            onChange={(event) => setOptionId(event.target.value)}
            disabled={disposition !== "automatic_rule"}
          >
            <option value="">Select a qualified branch</option>
            {context.options.map((option) => (
              <option key={option.id} value={option.id}>
                {option.label}
              </option>
            ))}
          </select>
        </div>
      </div>

      <details open>
        <summary>Reviewed typed facts</summary>
        <div className="grid grid-2" style={{ marginTop: "var(--space-3)" }}>
          {Object.entries(context.fact_schema.fields).map(([name, field]) => (
            <div className="field" key={name}>
              <label htmlFor={`judgment-fact-${name}`}>{name.replace(/_/g, " ")}</label>
              {field.type === "boolean" ? (
                <select
                  id={`judgment-fact-${name}`}
                  className="input"
                  value={String(facts[name])}
                  onChange={(event) =>
                    setFacts((current) => ({ ...current, [name]: event.target.value === "true" }))
                  }
                >
                  <option value="false">No</option>
                  <option value="true">Yes</option>
                </select>
              ) : field.type === "enum" ? (
                <select
                  id={`judgment-fact-${name}`}
                  className="input"
                  value={String(facts[name])}
                  onChange={(event) => setFacts((current) => ({ ...current, [name]: event.target.value }))}
                >
                  <option value="">Select a value</option>
                  {(field.allowed_values || []).map((value) => (
                    <option key={value} value={value}>{value}</option>
                  ))}
                </select>
              ) : (
                <input
                  id={`judgment-fact-${name}`}
                  className="input"
                  type={field.type === "integer" || field.type === "number" ? "number" : "text"}
                  value={String(facts[name])}
                  onChange={(event) =>
                    setFacts((current) => ({
                      ...current,
                      [name]: field.type === "integer" || field.type === "number"
                        ? Number(event.target.value)
                        : event.target.value,
                    }))
                  }
                />
              )}
              <span className="page-sub mono">{field.type}</span>
            </div>
          ))}
        </div>
      </details>

      <details>
        <summary>Local evidence and optional review note</summary>
        <p className="page-sub">
          Store raw screenshots, record values, and free text on this device. This form only
          sends local content references to Flow. It does not send the content to Cloud.
        </p>
        {evidence.map((reference, index) => (
          <EvidenceReference
            key={`evidence-${index}`}
            label={`Evidence reference ${index + 1}`}
            value={reference}
            onChange={(next) => setEvidence((current) => current.map((item, itemIndex) => itemIndex === index ? next : item))}
            onRemove={() => setEvidence((current) => current.filter((_, itemIndex) => itemIndex !== index))}
          />
        ))}
        <Button onClick={() => setEvidence((current) => [...current, newRef()])}>Add local evidence</Button>
        <div style={{ marginTop: "var(--space-3)" }}>
          {note ? (
            <EvidenceReference
              label="Optional local review note"
              value={note}
              onChange={setNote}
              onRemove={() => setNote(null)}
            />
          ) : (
            <Button onClick={() => setNote(newRef())}>Add local review note</Button>
          )}
        </div>
      </details>

      <details open>
        <summary>What should qualification do with this case?</summary>
        <div style={{ marginTop: "var(--space-3)" }}>
          <SegControl
            value={disposition}
            onChange={(next) => {
              setDisposition(next);
              if (next !== "automatic_rule") setAddContrast(false);
            }}
            options={dispositionOptions}
          />
          <p className="page-sub">
            {disposition === "automatic_rule" && "This requests a rule candidate for review. It does not enable automatic execution."}
            {disposition === "human_node" && "This preserves a permanent human decision node for this case."}
            {disposition === "more_evidence_required" && "This records that the current facts do not justify a branch. Add a contrast case."}
          </p>
          {disposition === "automatic_rule" && (
            <>
              <div className="field">
              <label htmlFor="judgment-reviewed-rule">Reviewed rule id</label>
              <input
                id="judgment-reviewed-rule"
                className="input mono"
                value={reviewedRuleId}
                onChange={(event) => setReviewedRuleId(event.target.value)}
                placeholder="A reviewed policy identifier, not a natural-language rule"
              />
              </div>
              <label className="checkbox-row" style={{ marginTop: "var(--space-3)" }}>
                <input
                  type="checkbox"
                  checked={addContrast}
                  onChange={(event) => setAddContrast(event.target.checked)}
                />
                Add a contrasting reviewed case now
              </label>
              <span className="page-sub">
                Flow requires reciprocal contrasting cases before a rule candidate can pass coverage.
              </span>
              {addContrast && (
                <div className="card-inset" style={{ marginTop: "var(--space-3)" }}>
                  <strong>Contrasting case</strong>
                  <div className="grid grid-2" style={{ marginTop: "var(--space-3)" }}>
                    {Object.entries(context.fact_schema.fields).map(([name, field]) => (
                      <FactInput
                        key={name}
                        name={name}
                        field={field}
                        facts={contrastFacts}
                        onChange={(next) => setContrastFacts((current) => ({ ...current, [name]: next }))}
                        prefix="contrast"
                      />
                    ))}
                  </div>
                  <div className="field">
                    <label htmlFor="judgment-contrast-option">Qualified branch for contrasting case</label>
                    <select
                      id="judgment-contrast-option"
                      className="input"
                      value={contrastOptionId}
                      onChange={(event) => setContrastOptionId(event.target.value)}
                    >
                      <option value="">Select a qualified branch</option>
                      {context.options.map((option) => <option key={option.id} value={option.id}>{option.label}</option>)}
                    </select>
                  </div>
                </div>
              )}
            </>
          )}
        </div>
        {context.cases.length > 0 && (
          <div className="field" style={{ marginTop: "var(--space-3)" }}>
            <label>Contrast cases</label>
            {context.cases.map((item) => (
              <label className="checkbox-row" key={item.id}>
                <input
                  type="checkbox"
                  checked={contrastCaseIds.includes(item.id)}
                  onChange={(event) => setContrastCaseIds((current) => event.target.checked ? [...current, item.id] : current.filter((id) => id !== item.id))}
                />
                {item.id}
              </label>
            ))}
          </div>
        )}
      </details>

      <div className="card-inset" style={{ marginTop: "var(--space-4)" }}>
        <div className="row">
          <strong>Coverage and conflicts</strong>
          <span className="spacer" />
          <Pill tone={conflicts ? "warn" : "ok"}>{conflicts ? `${conflicts} conflict${conflicts === 1 ? "" : "s"}` : "No conflicts"}</Pill>
        </div>
        <p className="page-sub">
          {context.cases.length} saved local case{context.cases.length === 1 ? "" : "s"}. A conflict means identical reviewed facts support different branches. Qualification must keep the decision human or request a missing fact.
        </p>
      </div>

      {error && <Callout tone="crit" title="Case not saved">{error}</Callout>}
      <div className="row" style={{ marginTop: "var(--space-4)" }}>
        <Button variant="primary" disabled={busy} onClick={() => void capture()} data-testid="capture-judgment-case">
          {busy ? "Saving…" : "Save reviewed case locally"}
        </Button>
        <span className="page-sub">Flow seals the case into the next qualification revision. This does not create a runtime task.</span>
      </div>
    </Card>
  );
}

function FactInput({
  name,
  field,
  facts,
  onChange,
  prefix,
}: {
  name: string;
  field: JudgmentFactFieldV1;
  facts: Record<string, FactValue>;
  onChange: (value: FactValue) => void;
  prefix: string;
}) {
  const inputId = `judgment-${prefix}-fact-${name}`;
  return (
    <div className="field">
      <label htmlFor={inputId}>{name.replace(/_/g, " ")}</label>
      {field.type === "boolean" ? (
        <select id={inputId} className="input" value={String(facts[name])} onChange={(event) => onChange(event.target.value === "true")}>
          <option value="false">No</option>
          <option value="true">Yes</option>
        </select>
      ) : field.type === "enum" ? (
        <select id={inputId} className="input" value={String(facts[name])} onChange={(event) => onChange(event.target.value)}>
          <option value="">Select a value</option>
          {(field.allowed_values || []).map((value) => <option key={value} value={value}>{value}</option>)}
        </select>
      ) : (
        <input
          id={inputId}
          className="input"
          type={field.type === "integer" || field.type === "number" ? "number" : "text"}
          value={String(facts[name])}
          onChange={(event) => onChange(field.type === "integer" || field.type === "number" ? Number(event.target.value) : event.target.value)}
        />
      )}
      <span className="page-sub mono">{field.type}</span>
    </div>
  );
}

function EvidenceReference({
  label,
  value,
  onChange,
  onRemove,
}: {
  label: string;
  value: LocalEvidenceRefV1;
  onChange: (value: LocalEvidenceRefV1) => void;
  onRemove: () => void;
}) {
  return (
    <div className="card-inset" style={{ marginBottom: "var(--space-3)" }}>
      <div className="grid grid-3">
        <div className="field">
          <label>Local path</label>
          <input className="input mono" aria-label={`${label} local path`} value={value.relative_path} onChange={(event) => onChange({ ...value, relative_path: event.target.value })} />
        </div>
        <div className="field">
          <label>SHA-256</label>
          <input className="input mono" aria-label={`${label} SHA-256`} value={value.sha256} onChange={(event) => onChange({ ...value, sha256: event.target.value })} />
        </div>
        <div className="field">
          <label>Kind</label>
          <select
            className="input"
            aria-label={`${label} kind`}
            value={value.kind}
            onChange={(event) =>
              onChange({
                ...value,
                kind: event.target.value as LocalEvidenceRefV1["kind"],
              })
            }
          >
            <option value="frame">frame</option>
            <option value="recording">recording</option>
            <option value="report">report</option>
            <option value="document">document</option>
            <option value="system_read">system read</option>
          </select>
        </div>
      </div>
      <Button onClick={onRemove}>Remove</Button>
    </div>
  );
}
