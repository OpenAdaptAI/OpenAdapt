import { useEffect, useMemo, useRef, useState } from "react";
import { CMD, engineInvoke } from "../lib/engine";
import type {
  ExecutionTarget,
  QualificationCaseKind,
  QualificationProject,
  QualificationResponse,
} from "../lib/types";
import { ExecutionTargetForm } from "../ui/ExecutionTargetForm";
import { Button, Callout, Card, CardHead, Field, Pill } from "../ui/primitives";

const POLICY = "clinical-write";

function secretEnvironmentReference(name: string): string {
  return `OPENADAPT_FLOW_SECRET_${name.replace(/[^a-zA-Z0-9]/g, "_").toUpperCase()}`;
}

function targetForProject(project: QualificationProject): ExecutionTarget {
  const backend = project.project?.environment.target_kind || "web";
  return backend === "rdp" ? { backend, rdp_host: "" } : { backend };
}

export function QualificationLifecycle({
  workflowId,
  project,
  onProject,
  onOpenWorkflow,
}: {
  workflowId: string;
  project: QualificationProject;
  onProject: (project: QualificationProject) => void;
  onOpenWorkflow: (workflowId: string) => void;
}) {
  const [busy, setBusy] = useState("");
  const [issue, setIssue] = useState("");
  const [notice, setNotice] = useState("");
  const [caseId, setCaseId] = useState("representative-1");
  const [caseKind, setCaseKind] =
    useState<QualificationCaseKind>("representative");
  const [description, setDescription] = useState(
    "Representative production-shaped case",
  );
  const [selectedCaseId, setSelectedCaseId] = useState("");
  const [parametersJson, setParametersJson] = useState("{}");
  const [target, setTarget] = useState<ExecutionTarget>(() =>
    targetForProject(project),
  );
  const [deploymentConfig, setDeploymentConfig] = useState("");
  const importInput = useRef<HTMLInputElement>(null);

  const cases = project.project?.cases || [];
  const selectedCase = useMemo(
    () => cases.find((item) => item.id === selectedCaseId) || cases[0],
    [cases, selectedCaseId],
  );

  useEffect(() => {
    if (!selectedCaseId && cases[0]) setSelectedCaseId(cases[0].id);
  }, [cases, selectedCaseId]);

  useEffect(() => setTarget(targetForProject(project)), [workflowId]);

  async function mutate(
    command: string,
    params: Record<string, unknown>,
    operation: string,
  ) {
    setBusy(operation);
    setIssue("");
    setNotice("");
    try {
      const response = await engineInvoke<QualificationResponse>(command, {
        workflow_id: workflowId,
        policy: POLICY,
        ...params,
      });
      if (!response.ok) {
        setIssue(response.error);
        return;
      }
      onProject(response);
      setNotice(
        operation === "run"
          ? `${String(params.case_id)} retained as signed evidence.`
          : "Qualification project updated.",
      );
    } catch (error) {
      setIssue(String(error));
    } finally {
      setBusy("");
    }
  }

  async function addCase() {
    await mutate(
      CMD.ADD_QUALIFICATION_CASE,
      {
        case_id: caseId.trim(),
        kind: caseKind,
        description: description.trim(),
        parameters_json: parametersJson,
      },
      "add",
    );
  }

  async function runCase() {
    if (!selectedCase) return;
    await mutate(
      CMD.RUN_QUALIFICATION_CASE,
      {
        case_id: selectedCase.id,
        parameters_json: parametersJson,
        target,
        ...(deploymentConfig.trim()
          ? { deployment_config: deploymentConfig.trim() }
          : {}),
      },
      "run",
    );
  }

  async function importResults(file: File | undefined) {
    if (!file) return;
    setBusy("import");
    setIssue("");
    setNotice("");
    try {
      const response = await engineInvoke<QualificationResponse>(
        CMD.IMPORT_QUALIFICATION_RESULTS,
        {
          workflow_id: workflowId,
          policy: POLICY,
          signed_results_json: await file.text(),
        },
      );
      if (!response.ok) {
        setIssue(response.error);
        return;
      }
      onProject(response);
      setNotice("Signed results and their local evidence hashes were verified.");
    } catch (error) {
      setIssue(String(error));
    } finally {
      setBusy("");
    }
  }

  async function artifactAction(command: string, operation: string) {
    setBusy(operation);
    setIssue("");
    setNotice("");
    try {
      const response = await engineInvoke<{
        ok: boolean;
        workflow_id: string;
        error?: string;
        path?: string;
        sha256?: string;
        deployed?: boolean;
        pending_review?: boolean;
        dashboard_url?: string;
      }>(command, { workflow_id: workflowId });
      if (!response.ok) {
        setIssue(response.error || "The artifact operation was refused.");
        return;
      }
      if (
        (operation === "version" || operation === "seal") &&
        response.workflow_id !== workflowId
      ) {
        onOpenWorkflow(response.workflow_id);
        return;
      }
      if (operation === "export") {
        setNotice(`Exported ${response.path} · SHA-256 ${response.sha256}`);
      } else if (response.pending_review) {
        setNotice(
          "A sanitized derivative is ready for local review; no artifact was uploaded yet.",
        );
      } else if (response.deployed) {
        setNotice(`Deployed successfully. ${response.dashboard_url || ""}`);
      }
    } catch (error) {
      setIssue(String(error));
    } finally {
      setBusy("");
    }
  }

  return (
    <>
      {issue && (
        <Callout tone="crit" title="Qualification action stopped">
          {issue}
        </Callout>
      )}
      {notice && (
        <Callout tone="info" title="Qualification lifecycle updated">
          {notice}
        </Callout>
      )}

      <Card>
        <CardHead
          eyebrow={`Revision ${project.project?.revision || 0}`}
          title="Qualification cases"
          sub="Run representative work to VERIFIED and fault campaigns to HALTED. Desktop retains the report locally, hashes it, and signs the exact project, workflow, environment, and runner contract."
        />
        <div className="metrics">
          <div className="metric">
            <span className="label">Required cases</span>
            <span className="metric-value">{project.report.case_count}</span>
          </div>
          <div className="metric">
            <span className="label">Passed this revision</span>
            <span className="metric-value">{project.report.passed_case_count}</span>
          </div>
          <div className="metric">
            <span className="label">Environment</span>
            <span className="metric-value mono">
              {project.project?.environment.environment_digest.slice(0, 10)}…
            </span>
          </div>
        </div>

        <table>
          <thead>
            <tr>
              <th>Case</th>
              <th>Expected</th>
              <th>Evidence</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {cases.map((item) => (
              <tr key={item.id}>
                <td>
                  <strong>{item.kind.replaceAll("_", " ")}</strong>
                  <div className="page-sub mono">{item.id}</div>
                  {item.description && (
                    <div className="page-sub">{item.description}</div>
                  )}
                </td>
                <td>{item.expected_outcome.toUpperCase()}</td>
                <td>
                  <Pill tone={item.results.length ? "ok" : "warn"}>
                    {item.results.length
                      ? `${item.results.length} signed`
                      : "run required"}
                  </Pill>
                </td>
                <td className="num">
                  <Button
                    size="sm"
                    onClick={() => setSelectedCaseId(item.id)}
                  >
                    {selectedCase?.id === item.id ? "Selected" : "Select"}
                  </Button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>

        <div className="grid grid-2" style={{ marginTop: "var(--space-5)" }}>
          <div>
            <h3>Run {selectedCase?.id || "a case"}</h3>
            <ExecutionTargetForm
              target={target}
              onChange={setTarget}
              idPrefix="qualification-case-target"
              disabled={Boolean(busy)}
            />
            <Field
              label="Deployment config"
              hint="Optional local YAML/JSON with policy, verifier, and secret references."
              htmlFor="qualification-case-config"
            >
              <input
                id="qualification-case-config"
                className="input mono"
                value={deploymentConfig}
                onChange={(event) => setDeploymentConfig(event.target.value)}
                placeholder="/path/to/deployment.yaml"
              />
            </Field>
          </div>
          <div>
            <Field
              label="Case parameters"
              hint="Stored locally outside the workflow; secret values never enter the qualification project."
              htmlFor="qualification-case-parameters"
            >
              <textarea
                id="qualification-case-parameters"
                className="input mono"
                rows={8}
                value={parametersJson}
                onChange={(event) => setParametersJson(event.target.value)}
              />
            </Field>
            {project.controls.parameters.some((parameter) => parameter.secret) && (
              <Callout tone="info" title="Runner secret references">
                {project.controls.parameters
                  .filter((parameter) => parameter.secret)
                  .map((parameter) => (
                    <div className="mono" key={parameter.name}>
                      {parameter.name} → {secretEnvironmentReference(parameter.name)}
                    </div>
                  ))}
                <span className="page-sub">
                  Supply these through the runner credential boundary. Secret values are
                  refused in case JSON.
                </span>
              </Callout>
            )}
            <div className="row">
              <Button
                variant="primary"
                disabled={!selectedCase || Boolean(busy)}
                onClick={() => void runCase()}
              >
                {busy === "run" ? "Running case…" : "Run and sign case"}
              </Button>
              <Button
                disabled={Boolean(busy)}
                onClick={() => importInput.current?.click()}
              >
                {busy === "import" ? "Importing…" : "Import signed results"}
              </Button>
              <input
                ref={importInput}
                type="file"
                accept="application/json,.json"
                hidden
                disabled={Boolean(busy)}
                onChange={(event) => {
                  void importResults(event.currentTarget.files?.[0]);
                  event.currentTarget.value = "";
                }}
              />
            </div>
          </div>
        </div>

        <details style={{ marginTop: "var(--space-5)" }}>
          <summary>Add another qualification case</summary>
          <div className="grid grid-2" style={{ marginTop: "var(--space-3)" }}>
            <Field label="Case id" htmlFor="qualification-new-case-id">
              <input
                id="qualification-new-case-id"
                className="input mono"
                value={caseId}
                onChange={(event) => setCaseId(event.target.value)}
              />
            </Field>
            <Field label="Case type" htmlFor="qualification-new-case-kind">
              <select
                id="qualification-new-case-kind"
                className="input"
                value={caseKind}
                onChange={(event) =>
                  setCaseKind(event.target.value as QualificationCaseKind)
                }
              >
                <option value="representative">Representative</option>
                <option value="ambiguity">Ambiguity</option>
                <option value="wrong_identity">Wrong identity</option>
                <option value="stale_identity">Stale identity</option>
                <option value="weak_effect">Weak effect</option>
                <option value="missing_effect">Missing effect</option>
              </select>
            </Field>
          </div>
          <Field label="Description" htmlFor="qualification-new-case-description">
            <input
              id="qualification-new-case-description"
              className="input"
              value={description}
              onChange={(event) => setDescription(event.target.value)}
            />
          </Field>
          <Button disabled={Boolean(busy)} onClick={() => void addCase()}>
            {busy === "add" ? "Adding…" : "Add case"}
          </Button>
        </details>
      </Card>

      <Card>
        <CardHead
          eyebrow="Artifact lifecycle"
          title="Version, seal, export, or deploy"
          sub="Prior versions remain intact. Sealing creates an encrypted version and stores only its key reference in the operating system credential store. Run cases and certify the exact sealed version before export or deployment."
        />
        <div className="row">
          <Pill tone={project.graph.bundle.encrypted ? "ok" : "warn"}>
            {project.graph.bundle.encrypted ? "encrypted" : "not yet encrypted"}
          </Pill>
          <Pill tone={project.certification_current ? "ok" : "warn"}>
            {project.certification_current ? "certified" : "certification required"}
          </Pill>
        </div>
        <div className="row" style={{ marginTop: "var(--space-4)" }}>
          <Button
            disabled={Boolean(busy)}
            onClick={() =>
              void artifactAction(CMD.VERSION_QUALIFICATION_WORKFLOW, "version")
            }
          >
            Create working version
          </Button>
          <Button
            disabled={Boolean(busy)}
            onClick={() =>
              void artifactAction(CMD.SEAL_QUALIFICATION_WORKFLOW, "seal")
            }
          >
            Seal and encrypt version
          </Button>
          <Button
            disabled={Boolean(busy)}
            onClick={() =>
              void artifactAction(CMD.EXPORT_QUALIFICATION_WORKFLOW, "export")
            }
          >
            Export qualified artifact
          </Button>
          <Button
            variant="primary"
            disabled={Boolean(busy)}
            onClick={() =>
              void artifactAction(CMD.DEPLOY_QUALIFICATION_WORKFLOW, "deploy")
            }
          >
            Deploy qualified artifact
          </Button>
        </div>
      </Card>
    </>
  );
}
