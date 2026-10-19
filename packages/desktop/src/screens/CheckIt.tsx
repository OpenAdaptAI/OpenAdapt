// Check it: the short readiness test that replaces the nine-step page as the
// default path.
//
// 1. What it changes. 2. How it makes sure it's the right record. 3. How it
// confirms the save. 4. A practice run on a test item. 5. Safety drills.
// 6. Approve. Each answer is pre-filled from the recording by Flow's
// qualification proposal; the person confirms. Anything the engine can't
// pre-fill is asked in plain words. The full contract stays reachable under
// "Advanced: exact checks", and every command here is the same engine command
// that page uses, so neither path weakens the other.
import { useEffect, useId, useMemo, useState } from "react";
import { CMD, engineInvoke, engineTry } from "../lib/engine";
import {
  TIER_CHOICES,
  checkPlan,
  missingPinQuestion,
  type CaseView,
  type CheckPlan,
  type Proposal,
  type StepState,
  type WriteStep,
} from "../lib/checkPlan";
import { humanizeInput, plainResultForReport, resultCopy, sentenceCase } from "../lib/outcomes";
import type {
  ExecutionTarget,
  QualificationParameter,
  QualificationProject,
  QualificationResponse,
  RunReport,
  TaskInputs,
  Workflow,
} from "../lib/types";
import { ExecutionTargetForm, TARGETS } from "../ui/ExecutionTargetForm";
import { Button, Callout, Field } from "../ui/primitives";
import { Icon, PageHead, Status, TechnicalDetails } from "../ui/simple";

const POLICY = "clinical-write";
const PRACTICE_CASE = "practice-1";

type Busy =
  | null
  | "loading"
  | "confirm"
  | "arm"
  | "effect"
  | "app"
  | "practice"
  | "drills"
  | "approve";

function StepBadge({
  state,
  index,
  passed,
}: {
  state: StepState;
  index: number;
  passed: boolean;
}) {
  if (state === "done") {
    return (
      <span className={`check-badge done${passed ? " passed" : ""}`} aria-label="Done">
        <Icon name="check" />
      </span>
    );
  }
  if (state === "missing") {
    return (
      <span className="check-badge missing" aria-label="Needs an answer">
        <Icon name="attention" />
      </span>
    );
  }
  if (state === "not_needed") {
    return (
      <span className="check-badge skip" aria-label="Not needed">
        <Icon name="minus" />
      </span>
    );
  }
  return (
    <span className="check-badge todo" aria-label="To do">
      {index}
    </span>
  );
}

function stateWord(state: StepState, doneWord = "Confirmed"): string {
  return state === "done"
    ? doneWord
    : state === "missing"
      ? "Needs an answer"
      : state === "not_needed"
        ? "Not needed"
        : "To confirm";
}

function CheckSection({
  index,
  title,
  state,
  doneWord,
  children,
}: {
  index: number;
  title: string;
  state: StepState;
  doneWord?: string;
  children: React.ReactNode;
}) {
  // Green marks a check that ran and passed (the practice run, the drills).
  // A part a person confirmed or approved stays ink.
  const passed = doneWord === "Passed";
  return (
    <section
      className={`check-step ${state}${passed ? " passed" : ""}`}
      aria-labelledby={`check-${index}`}
    >
      <StepBadge state={state} index={index} passed={passed} />
      <div className="check-step-body">
        {/* A finished part folds to its title so the open question stands out;
            the last part keeps its Run button in view. */}
        {state === "done" && index < 6 ? (
          <details className="check-step-fold">
            <summary className="check-step-head">
              <h2 id={`check-${index}`}>{title}</h2>
              <span className="check-step-state">{stateWord(state, doneWord)}</span>
            </summary>
            {children}
          </details>
        ) : (
          <>
            <div className="check-step-head">
              <h2 id={`check-${index}`}>{title}</h2>
              <span className="check-step-state">{stateWord(state, doneWord)}</span>
            </div>
            {children}
          </>
        )}
      </div>
    </section>
  );
}

function targetSummary(target: ExecutionTarget | null): string {
  if (!target) return "Not set";
  const label = TARGETS[target.backend]?.label ?? target.backend;
  const where = target.url || target.macos_app || target.linux_app || target.rdp_host || target.rdp_window || "";
  const summary = where ? `${label}: ${where}` : label;
  return attachedBrowser(target) ? `${summary}, in your signed-in browser` : summary;
}

/** Runs attach to the person's signed-in browser instead of opening a new one. */
function attachedBrowser(target: ExecutionTarget | null): boolean {
  return Boolean(target?.backend === "web" && target.browser_cdp_endpoint?.trim());
}

/** Where a case run stopped and Flow's reason, for the technical details. */
function stopReason(run: RunReport | undefined, flowWords?: unknown): string {
  if (typeof flowWords === "string" && flowWords) return flowWords;
  if (!run) return "";
  const halt = run.halt;
  if (halt?.reason) {
    return halt.step_intent ? `${halt.step_intent}: ${halt.reason}` : halt.reason;
  }
  return run.error ?? "";
}

function caseResult(response: { case_run?: RunReport } & Record<string, unknown>) {
  const run = response.case_run;
  if (!run) return null;
  return resultCopy(plainResultForReport(run));
}

interface EffectAnswer {
  kind: "record_written" | "field_equals";
  matchParam: string;
  matchField: string;
  field: string;
  valueParam: string;
  tier: number;
}

/**
 * Plain questions for a save OpenAdapt can't confirm from the recording.
 * The answers become one effect binding, the same one the exact checks make.
 */
function EffectQuestion({
  idPrefix,
  inputs,
  busy,
  onSave,
}: {
  idPrefix: string;
  inputs: QualificationParameter[];
  busy: boolean;
  onSave: (answer: EffectAnswer) => void;
}) {
  const [answer, setAnswer] = useState<EffectAnswer>({
    kind: "field_equals",
    matchParam: inputs[0]?.name ?? "",
    matchField: "",
    field: "",
    valueParam: inputs[1]?.name ?? inputs[0]?.name ?? "",
    tier: 1,
  });
  const set = (next: Partial<EffectAnswer>) => setAnswer({ ...answer, ...next });
  const ready =
    answer.matchParam &&
    answer.matchField.trim() &&
    (answer.kind === "record_written" || (answer.field.trim() && answer.valueParam));
  return (
    <div className="question effect-question">
      <p>
        OpenAdapt didn't see where this save lands in your system. Answer these
        so it can read the record back. Ask your IT team if you're not sure of a
        name; they connect OpenAdapt to the records once.
      </p>
      <fieldset className="plain-choices">
        <legend>What does this step save?</legend>
        <label>
          <input
            type="radio"
            name={`${idPrefix}-kind`}
            checked={answer.kind === "field_equals"}
            onChange={() => set({ kind: "field_equals" })}
          />
          It changes a field on an existing record
        </label>
        <label>
          <input
            type="radio"
            name={`${idPrefix}-kind`}
            checked={answer.kind === "record_written"}
            onChange={() => set({ kind: "record_written" })}
          />
          It adds a new entry to a record
        </label>
      </fieldset>
      <div className="single-form">
        <Field label="Which value identifies the record?" htmlFor={`${idPrefix}-match-param`}>
          <select
            id={`${idPrefix}-match-param`}
            className="input"
            value={answer.matchParam}
            onChange={(event) => set({ matchParam: event.target.value })}
          >
            {inputs.map((input) => (
              <option key={input.name} value={input.name}>
                {sentenceCase(humanizeInput(input.name))}
              </option>
            ))}
          </select>
        </Field>
        <Field
          label="What is that ID called in your system's records?"
          hint="The field or column name, for example member_id."
          htmlFor={`${idPrefix}-match-field`}
        >
          <input
            id={`${idPrefix}-match-field`}
            className="input"
            value={answer.matchField}
            onChange={(event) => set({ matchField: event.target.value })}
          />
        </Field>
        <Field
          label={answer.kind === "field_equals" ? "Which field gets the new value?" : "Which field holds the entry? (optional)"}
          htmlFor={`${idPrefix}-field`}
        >
          <input
            id={`${idPrefix}-field`}
            className="input"
            value={answer.field}
            onChange={(event) => set({ field: event.target.value })}
          />
        </Field>
        <Field label="Which value goes there?" htmlFor={`${idPrefix}-value-param`}>
          <select
            id={`${idPrefix}-value-param`}
            className="input"
            value={answer.valueParam}
            onChange={(event) => set({ valueParam: event.target.value })}
          >
            {answer.kind === "record_written" && <option value="">Not needed</option>}
            {inputs.map((input) => (
              <option key={input.name} value={input.name}>
                {sentenceCase(humanizeInput(input.name))}
              </option>
            ))}
          </select>
        </Field>
      </div>
      <fieldset className="plain-choices">
        <legend>How should OpenAdapt read the record back?</legend>
        {TIER_CHOICES.map((choice) => (
          <label key={choice.tier}>
            <input
              type="radio"
              name={`${idPrefix}-tier`}
              checked={answer.tier === choice.tier}
              onChange={() => set({ tier: choice.tier })}
            />
            <span>
              {choice.label}
              <small>{choice.note}</small>
            </span>
          </label>
        ))}
      </fieldset>
      <Button size="sm" variant="primary" disabled={busy || !ready} onClick={() => onSave(answer)}>
        Save how it confirms the save
      </Button>
    </div>
  );
}

/** Armed saving steps grouped by their identical sentence, in first-seen order. */
function armedIdentityGroups<T extends { identityArmed: boolean; identityText: string }>(
  writes: T[],
): { text: string; count: number }[] {
  const groups = new Map<string, number>();
  for (const step of writes) {
    if (!step.identityArmed) continue;
    groups.set(step.identityText, (groups.get(step.identityText) ?? 0) + 1);
  }
  return [...groups].map(([text, count]) => ({ text, count }));
}

export function CheckIt({
  workflowId,
  onExactChecks,
  onRun,
  onReplaced,
}: {
  workflowId: string;
  onExactChecks: (workflowId: string) => void;
  onRun: (workflowId: string) => void;
  /** Approving creates a sealed version with a new id. */
  onReplaced?: (workflowId: string) => void;
}) {
  const idPrefix = useId();
  const [name, setName] = useState("");
  const [project, setProject] = useState<QualificationProject | null>(null);
  const [proposal, setProposal] = useState<Proposal | null>(null);
  const [loadError, setLoadError] = useState("");
  const [issue, setIssue] = useState("");
  const [issueDetail, setIssueDetail] = useState("");
  const [busy, setBusy] = useState<Busy>("loading");
  const [inputs, setInputs] = useState<QualificationParameter[]>([]);
  const [values, setValues] = useState<Record<string, string>>({});
  const [target, setTarget] = useState<ExecutionTarget | null>(null);
  const [deploymentConfig, setDeploymentConfig] = useState("");
  const [attested, setAttested] = useState(false);
  const [practiceNote, setPracticeNote] = useState("");
  const [drillNote, setDrillNote] = useState("");
  const [approvedId, setApprovedId] = useState<string | null>(null);
  const [appName, setAppName] = useState("");
  const [appVersion, setAppVersion] = useState("");

  const plan: CheckPlan | null = useMemo(
    () => (project ? checkPlan(project) : null),
    [project],
  );

  function fail(message: string, detail = "") {
    setIssue(message);
    setIssueDetail(detail);
  }

  async function load() {
    setBusy("loading");
    setLoadError("");
    try {
      const [list, details, response] = await Promise.all([
        engineTry<Workflow[]>(CMD.GET_WORKFLOWS, {}, []),
        engineTry<TaskInputs>(CMD.GET_TASK_INPUTS, { workflow_id: workflowId }, {
          ok: false,
          workflow_id: workflowId,
        }),
        engineInvoke<QualificationResponse>(CMD.GET_QUALIFICATION, {
          workflow_id: workflowId,
          policy: POLICY,
        }),
      ]);
      const workflow = (Array.isArray(list) ? list : []).find((item) => item.id === workflowId);
      setName(workflow?.name ?? "");
      const declared = (details.inputs ?? []).filter((item) => !item.secret);
      setInputs(declared);
      setValues((current) => {
        const next = { ...current };
        for (const input of declared) {
          if (next[input.name] === undefined && input.example != null) {
            next[input.name] = String(input.example);
          }
        }
        return next;
      });
      setTarget(details.target ?? { backend: "web" });
      if (!response.ok) {
        setLoadError(response.error);
        return;
      }
      setProject(response);
      if (!response.project || response.draft_environment) {
        const draft = await engineTry<{ ok: boolean; proposal?: Proposal; error?: string }>(
          CMD.PROPOSE_QUALIFICATION_FROM_DEMO,
          { workflow_id: workflowId, policy_pack: "community", accept: false },
          { ok: false },
        );
        setProposal(draft.proposal ?? null);
      }
    } catch (error) {
      setLoadError(String(error));
    } finally {
      setBusy(null);
    }
  }

  useEffect(() => {
    void load();
    // The task is fixed for this mounted screen.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [workflowId]);

  async function call(cmd: string, params: Record<string, unknown>): Promise<QualificationResponse & Record<string, unknown>> {
    return engineInvoke<QualificationResponse & Record<string, unknown>>(cmd, {
      workflow_id: workflowId,
      policy: POLICY,
      ...params,
    });
  }

  async function confirmFound() {
    if (!plan || !project) return;
    setBusy("confirm");
    fail("");
    try {
      if (!plan.hasProject) {
        const response = await call(CMD.PROPOSE_QUALIFICATION_FROM_DEMO, {
          policy_pack: "community",
          accept: true,
        });
        if (!response.ok) {
          const draft = (response as { proposal?: Proposal }).proposal;
          if (draft) setProposal(draft);
          fail(
            "OpenAdapt needs a few answers before it can use these checks.",
            String(response.error || ""),
          );
          return;
        }
        setProject(response as QualificationProject);
        setProposal(null);
        return;
      }
      let latest: QualificationProject = project;
      for (const step of plan.writes.filter((item) => !item.confirmed)) {
        const control = latest.controls.actions[step.id];
        const current = control?.classification?.classification;
        // Never confirm a save as weaker than Flow's executable floor: an
        // irreversible step confirmed as "consequential" is a conflict Flow
        // refuses to approve.
        const risk =
          step.executableRisk === "irreversible"
            ? "irreversible"
            : current && current !== "unknown" && current !== "read_only"
              ? current
              : "consequential";
        const response = await call(CMD.SET_QUALIFICATION_RISK, {
          step_id: step.id,
          risk,
          explanation: "Confirmed in Check it.",
        });
        if (!response.ok) {
          fail("OpenAdapt couldn't save that confirmation.", String(response.error || ""));
          return;
        }
        latest = response as QualificationProject;
      }
      setProject(latest);
    } catch (error) {
      fail("OpenAdapt couldn't save that confirmation.", String(error));
    } finally {
      setBusy(null);
    }
  }

  async function armIdentity(step: WriteStep) {
    setBusy("arm");
    fail("");
    try {
      const response = await call(CMD.ARM_QUALIFICATION_IDENTITY, { step_id: step.id });
      if (!response.ok) {
        fail("OpenAdapt couldn't use the record details it saw.", String(response.error || ""));
        return;
      }
      setProject(response as QualificationProject);
    } finally {
      setBusy(null);
    }
  }

  async function setUpApp() {
    setBusy("app");
    fail("");
    try {
      const response = await call(CMD.INITIALIZE_QUALIFICATION, {
        target_kind: target?.backend ?? "web",
        application: appName.trim(),
        application_version: appVersion.trim() || "recorded",
        environment_label: `${appName.trim()} on this computer`,
        required_capabilities: [],
      });
      if (!response.ok) {
        fail("OpenAdapt couldn't save the app details.", String(response.error || ""));
        return;
      }
      setProject(response as QualificationProject);
      setProposal(null);
    } finally {
      setBusy(null);
    }
  }

  async function useRecordedApp() {
    const application = proposal?.pins.find((pin) => pin.kind === "application");
    const environment = proposal?.pins.find((pin) => pin.kind === "environment");
    if (!application?.payload || !environment?.payload) return;
    setBusy("app");
    fail("");
    try {
      const response = await call(CMD.INITIALIZE_QUALIFICATION, {
        target_kind: String(application.payload.target_kind ?? target?.backend ?? "web"),
        application: String(application.payload.application ?? ""),
        application_version: String(application.payload.application_version ?? "recorded"),
        environment_digest: String(environment.payload.environment_digest ?? ""),
        required_capabilities: Array.isArray(environment.payload.required_capabilities)
          ? environment.payload.required_capabilities
          : [],
      });
      if (!response.ok) {
        fail("OpenAdapt couldn't start the check from the recording.", String(response.error || ""));
        return;
      }
      setProject(response as QualificationProject);
      setProposal(null);
    } finally {
      setBusy(null);
    }
  }

  async function bindEffect(step: WriteStep, answer: EffectAnswer) {
    setBusy("effect");
    fail("");
    try {
      const response = await call(CMD.BIND_QUALIFICATION_EFFECT, {
        step_id: step.id,
        kind: answer.kind,
        match_field: answer.matchField.trim(),
        match_param: answer.matchParam,
        ...(answer.field.trim() ? { field: answer.field.trim() } : {}),
        ...(answer.valueParam ? { value_param: answer.valueParam } : {}),
        count_new_only: answer.kind === "record_written",
        expected_count: 1,
        verification_tier: answer.tier,
      });
      if (!response.ok) {
        fail("OpenAdapt couldn't save how it confirms the save.", String(response.error || ""));
        return;
      }
      setProject(response as QualificationProject);
    } finally {
      setBusy(null);
    }
  }

  function caseParameters(): string {
    const clean: Record<string, string> = {};
    for (const input of inputs) {
      const value = values[input.name];
      if (value !== undefined && value !== "") clean[input.name] = value;
    }
    return JSON.stringify(clean);
  }

  /** The deployment settings file IT gave for the record check, if any. */
  function runSettings(): { deployment_config?: string } {
    const path = deploymentConfig.trim();
    return path ? { deployment_config: path } : {};
  }

  async function runPractice() {
    if (!plan || !target) return;
    setBusy("practice");
    fail("");
    setPracticeNote("");
    try {
      const parameters = caseParameters();
      let caseId = plan.practice?.id ?? null;
      if (!caseId) {
        const added = await call(CMD.ADD_QUALIFICATION_CASE, {
          case_id: PRACTICE_CASE,
          kind: "representative",
          description: "Practice run on a test item",
          parameters_json: parameters,
        });
        if (!added.ok) {
          fail("OpenAdapt couldn't set up the practice run.", String(added.error || ""));
          return;
        }
        setProject(added as QualificationProject);
        caseId = PRACTICE_CASE;
      }
      const response = await call(CMD.RUN_QUALIFICATION_CASE, {
        case_id: caseId,
        parameters_json: parameters,
        target,
        ...runSettings(),
      });
      if (response.not_approved) {
        // Nothing was saved and nothing was recorded: say why in plain words.
        setPracticeNote(String(response.error || "The save wasn't approved, so nothing was saved."));
        return;
      }
      const copy = caseResult(response);
      if (copy) setPracticeNote(`${copy.label}. ${copy.sentence}`);
      if (!response.ok) {
        fail("The practice run didn't pass.", String(response.error || ""));
        return;
      }
      setProject(response as QualificationProject);
      const ran = checkPlan(response as QualificationProject).practice;
      if (ran && ran.status !== "passed") {
        // Say where and why it stopped: the result word alone doesn't.
        fail("The practice run didn't pass.", stopReason(response.case_run as RunReport | undefined, response.stop_reason));
      }
    } catch (error) {
      fail("The practice run didn't finish.", String(error));
    } finally {
      setBusy(null);
    }
  }

  async function runDrills() {
    if (!plan || !target) return;
    setBusy("drills");
    fail("");
    setDrillNote("");
    let latest: QualificationProject | null = null;
    try {
      const parameters = caseParameters();
      const pending = plan.drills.filter((item) => item.status !== "passed");
      // In the signed-in browser every run starts where the last one ended,
      // so run one drill and let the person reopen the first screen.
      const batch = attachedBrowser(target) ? pending.slice(0, 1) : pending;
      for (const drill of batch) {
        const response = await call(CMD.RUN_QUALIFICATION_CASE, {
          case_id: drill.id,
          parameters_json: parameters,
          target,
          ...runSettings(),
          ...(drill.faultStepId
            ? { fault_target: { step_id: drill.faultStepId, actuation_path: "gui" } }
            : {}),
        });
        if (!response.ok) {
          fail(`The "${drill.label}" drill didn't pass.`, String(response.error || ""));
          break;
        }
        latest = response as QualificationProject;
        const result = checkPlan(latest).drills.find((item) => item.id === drill.id);
        if (result && result.status !== "passed") {
          fail(`The "${drill.label}" drill didn't pass.`, stopReason(response.case_run as RunReport | undefined, response.stop_reason));
          break;
        }
      }
      if (latest) {
        setProject(latest);
        setDrillNote(
          batch.length < pending.length
            ? "That drill ended the way its description says. Open the screen where the workflow starts, then run the next drill."
            : "Every drill ended the way its description says.",
        );
      }
    } catch (error) {
      fail("The drills didn't finish.", String(error));
    } finally {
      setBusy(null);
    }
  }

  async function approve() {
    if (!plan) return;
    setBusy("approve");
    fail("");
    try {
      let current = project;
      if (!plan.approved) {
        const certified = await call(CMD.CERTIFY_QUALIFICATION, {});
        if (!certified.ok || !(certified as QualificationProject).certification_current) {
          if (certified.ok) setProject(certified as QualificationProject);
          fail(
            "OpenAdapt can't approve this workflow yet. Open the exact checks to see what's still open.",
            String(certified.error || ""),
          );
          return;
        }
        current = certified as QualificationProject;
        setProject(current);
      }
      if (current && !current.graph.bundle.encrypted) {
        const sealed = await engineInvoke<{ ok: boolean; workflow_id?: string; error?: string }>(
          CMD.SEAL_QUALIFICATION_WORKFLOW,
          { workflow_id: workflowId },
        );
        if (!sealed.ok || !sealed.workflow_id) {
          fail("OpenAdapt approved the workflow but couldn't lock this version.", String(sealed.error || ""));
          return;
        }
        setApprovedId(sealed.workflow_id);
        onReplaced?.(sealed.workflow_id);
        return;
      }
      setApprovedId(workflowId);
    } catch (error) {
      fail("OpenAdapt couldn't approve this workflow.", String(error));
    } finally {
      setBusy(null);
    }
  }

  const missingPins = (proposal?.pins ?? []).filter((pin) => pin.status === "missing");
  const appMissing = missingPins.some((pin) => pin.kind === "application" || pin.kind === "environment");
  const foundState = (state: StepState): StepState =>
    !plan?.hasProject && state === "done" ? "todo" : state;

  const firstThreeDone =
    plan !== null &&
    plan.hasProject &&
    [plan.changes, plan.rightRecord, plan.recordCheck].every(
      (state) => state === "done" || state === "not_needed",
    );
  const valuesReady = inputs.every(
    (input) => !input.required || (values[input.name] ?? "").trim() !== "",
  );
  const approved = approvedId !== null || (plan?.approved && plan.sealed);

  const practiceForm = plan ? (
    <>
      {inputs.length > 0 && (
        <div className="single-form">
          {inputs.map((input) => (
            <Field
              key={input.name}
              label={sentenceCase(humanizeInput(input.name))}
              hint={input.example != null ? "From your recording" : undefined}
              htmlFor={`${idPrefix}-value-${input.name}`}
            >
              <input
                id={`${idPrefix}-value-${input.name}`}
                className="input"
                value={values[input.name] ?? ""}
                onChange={(event) =>
                  setValues({ ...values, [input.name]: event.target.value })
                }
              />
            </Field>
          ))}
        </div>
      )}
      <p className="simple-sub">
        Runs in: {targetSummary(target)}
        {deploymentConfig.trim() ? ", with your deployment settings file" : ""}
      </p>
      {attachedBrowser(target) && (
        <p className="simple-sub">
          It starts from the screen your signed-in browser shows. Open the
          screen where the workflow starts before you run it.
        </p>
      )}
      <details className="technical">
        <summary>Change where it runs</summary>
        <div className="technical-body">
          {target && (
            <ExecutionTargetForm
              target={target}
              onChange={setTarget}
              idPrefix={`${idPrefix}-target`}
              disabled={busy !== null}
            />
          )}
          <Field
            label="Deployment settings file (for IT)"
            hint="Optional. A local YAML or JSON file with record-check and policy settings. The practice item and the drills use it."
            htmlFor={`${idPrefix}-config`}
          >
            <input
              id={`${idPrefix}-config`}
              className="input mono"
              value={deploymentConfig}
              disabled={busy !== null}
              spellCheck={false}
              placeholder="/path/to/deployment.yaml"
              onChange={(event) => setDeploymentConfig(event.target.value)}
            />
          </Field>
        </div>
      </details>
      <label className="record-check">
        <input
          type="checkbox"
          checked={attested}
          onChange={(event) => setAttested(event.target.checked)}
        />
        <span>These are test values, and I'll keep the app in view while it runs.</span>
      </label>
      {practiceNote && <p className="result-line">{practiceNote}</p>}
      <div className="check-actions">
        <Button
          variant={plan.practiceState === "done" ? "ghost" : "primary"}
          disabled={busy !== null || !attested || !valuesReady || !firstThreeDone || !target}
          onClick={() => void runPractice()}
        >
          {busy === "practice"
            ? "Running the practice item…"
            : plan.practiceState === "done"
              ? "Run the practice item again"
              : "Run the practice item"}
        </Button>
        {!firstThreeDone && (
          <span className="simple-sub">Confirm the first three parts first.</span>
        )}
      </div>
    </>
  ) : null;

  return (
    <div className="content simple">
      <PageHead
        title={name ? `Check it: ${name}` : "Check it"}
        sub="OpenAdapt filled this in from your recording. Confirm each part. It won't guess anything you leave unconfirmed."
        action={
          <Button size="sm" onClick={() => onExactChecks(workflowId)}>
            Advanced: exact checks
          </Button>
        }
      />

      {loadError && (
        <Callout tone="warn" title="Desktop couldn't read this workflow's checks">
          {loadError}
          <div className="callout-actions">
            <Button size="sm" onClick={() => void load()}>
              Try again
            </Button>
          </div>
        </Callout>
      )}
      {busy === "loading" && !plan && <p className="simple-sub">Reading your recording…</p>}

      {issue && (
        <div className="notice warn" role="alert">
          <Icon name="attention" />
          <div>
            <span>{issue}</span>
            {issueDetail && (
              <TechnicalDetails>
                <p className="mono technical-text">{issueDetail}</p>
              </TechnicalDetails>
            )}
          </div>
        </div>
      )}

      {plan && (
        <div className="check-list">
          <CheckSection index={1} title="What it changes" state={foundState(plan.changes)}>
            {plan.writes.length === 0 && plan.unproven.length === 0 ? (
              <p>
                This workflow only reads. None of its steps saves anything, so there's
                no record to check.
              </p>
            ) : plan.writes.length === 0 ? (
              <p>
                OpenAdapt can't tell yet whether this workflow saves anything, so it
                won't treat it as a workflow that only reads.
              </p>
            ) : (
              <>
                <ul className="write-list">
                  {plan.writes.map((step) => (
                    <li key={step.id}>
                      <strong>{step.caption}</strong>
                      {step.effects.map((effect) => (
                        <span key={effect}>{effect}</span>
                      ))}
                    </li>
                  ))}
                </ul>
                {plan.readCount > 0 && (
                  <p className="simple-sub">
                    {plan.readCount} other step{plan.readCount === 1 ? " only reads or moves" : "s only read or move"}{" "}
                    around in the app.
                  </p>
                )}
              </>
            )}
            {plan.unproven.length > 0 && (
              <div className="unproven-steps">
                <p className="simple-sub">
                  {plan.writes.length === 0
                    ? `${plan.unproven.length} step${plan.unproven.length === 1 ? "" : "s"} might change something.`
                    : `OpenAdapt can't tell yet whether ${plan.unproven.length} other step${plan.unproven.length === 1 ? "" : "s"} change${plan.unproven.length === 1 ? "s" : ""} anything.`}{" "}
                  Before you approve the workflow, say what each one does in Advanced:
                  exact checks.
                </p>
                <details className="technical">
                  <summary>
                    Show {plan.unproven.length === 1 ? "it" : `the ${plan.unproven.length} steps`}
                  </summary>
                  <ul className="technical-body">
                    {plan.unproven.map((step) => (
                      <li key={step.id}>{step.caption}</li>
                    ))}
                  </ul>
                </details>
              </div>
            )}
          </CheckSection>

          <CheckSection
            index={2}
            title="How it makes sure it's the right record"
            state={foundState(plan.rightRecord)}
          >
            {plan.writes.length === 0 && plan.unproven.length > 0 && (
              <p>
                This depends on part 1. Once OpenAdapt knows which step saves,
                it asks how to recognize the right record.
              </p>
            )}
            {/* Saving steps that check the record the same way say it once. */}
            {armedIdentityGroups(plan.writes).map((group) => (
              <div className="check-detail" key={`armed-${group.text}`}>
                <p>{group.text}</p>
                {group.count > 1 && (
                  <p className="check-detail-count">For each of its {group.count} saving steps.</p>
                )}
              </div>
            ))}
            {plan.writes.filter((step) => !step.identityArmed).map((step) => (
              <div className="check-detail" key={step.id}>
                <p>{step.identityText}</p>
                {!step.identityArmed && step.canArm && plan.hasProject && (
                  <Button size="sm" disabled={busy !== null} onClick={() => void armIdentity(step)}>
                    {busy === "arm" ? "Saving…" : "Use the record details OpenAdapt saw"}
                  </Button>
                )}
                {!step.identityArmed && !step.canArm && (
                  <p className="question">
                    OpenAdapt didn't see the record's name or ID on the screen
                    where it saves. Show the workflow again with the record's name
                    or ID visible before you save, or set this up in the exact
                    checks.
                  </p>
                )}
              </div>
            ))}
          </CheckSection>

          <CheckSection
            index={3}
            title="How it confirms the save"
            state={foundState(plan.recordCheck)}
          >
            {plan.writes.length === 0 && plan.unproven.length > 0 && (
              <p>
                This depends on part 1. Once OpenAdapt knows which step saves,
                it asks how to confirm the save.
              </p>
            )}
            {plan.writes.map((step) => (
              <div className="check-detail" key={step.id}>
                <p>{step.checkText}</p>
                {!step.checked && plan.hasProject && (
                  <EffectQuestion
                    idPrefix={`${idPrefix}-${step.id}`}
                    inputs={inputs}
                    busy={busy !== null}
                    onSave={(answer) => void bindEffect(step, answer)}
                  />
                )}
                {!step.checked && !plan.hasProject && step.hasRecordChecks && (
                  <p className="question">
                    OpenAdapt will read the saved record back to check the
                    entries listed in part 1. Confirm the parts above to set
                    up how it reads the record.
                  </p>
                )}
                {!step.checked && !plan.hasProject && !step.hasRecordChecks && (
                  <p className="question">
                    OpenAdapt didn't see where this save lands in your system,
                    so it will ask you a few questions about it. Start the check
                    first.
                  </p>
                )}
              </div>
            ))}
            {/* Nobody can vouch for "what it changes" while OpenAdapt can't
                say which step saves; that answer goes in the exact checks. */}
            {(!plan.hasProject || plan.changes === "todo") &&
              !(plan.writes.length === 0 && plan.unproven.length > 0) && (
              <div className="check-actions">
                <Button
                  variant="primary"
                  disabled={busy !== null}
                  onClick={() => void confirmFound()}
                >
                  {busy === "confirm" ? "Saving…" : "These look right"}
                </Button>
              </div>
            )}
            {missingPins.length > 0 && (
              <div className="missing-answers">
                <h3>OpenAdapt needs a few answers</h3>
                <ul>
                  {missingPins.map((pin) => (
                    <li key={pin.kind}>{missingPinQuestion(pin)}</li>
                  ))}
                </ul>
                {!appMissing && !plan.hasProject && (
                  <div className="check-actions">
                    <Button
                      variant="primary"
                      disabled={busy !== null}
                      onClick={() => void useRecordedApp()}
                    >
                      Start the check with the app from your recording
                    </Button>
                    <span className="simple-sub">
                      Then answer the questions above for each save.
                    </span>
                  </div>
                )}
                {appMissing && (
                  <div className="single-form">
                    <Field label="App name" htmlFor={`${idPrefix}-app`}>
                      <input
                        id={`${idPrefix}-app`}
                        className="input"
                        value={appName}
                        placeholder="Your EMR or portal"
                        onChange={(event) => setAppName(event.target.value)}
                      />
                    </Field>
                    <Field label="Version (if you know it)" htmlFor={`${idPrefix}-version`}>
                      <input
                        id={`${idPrefix}-version`}
                        className="input"
                        value={appVersion}
                        onChange={(event) => setAppVersion(event.target.value)}
                      />
                    </Field>
                    <Button
                      size="sm"
                      variant="primary"
                      disabled={busy !== null || !appName.trim()}
                      onClick={() => void setUpApp()}
                    >
                      Save the app details
                    </Button>
                  </div>
                )}
              </div>
            )}
          </CheckSection>

          <CheckSection
            index={4}
            title="Practice run on a test item"
            doneWord="Passed"
            state={plan.writes.length === 0 && !plan.practice ? "todo" : plan.practiceState}
          >
            <p>
              OpenAdapt does the workflow once, start to finish, on a test record you
              choose, with every check on. Before it saves, it shows you the
              filled form and waits for you to approve. Then it saves for real
              in the app, so use a test record. Keep the app where you can see
              it.
            </p>
            {plan.practiceState === "done" && practiceNote === "" ? (
              <details className="technical">
                <summary>Run the practice item again</summary>
                <div className="technical-body">{practiceForm}</div>
              </details>
            ) : (
              practiceForm
            )}
          </CheckSection>

          <CheckSection index={5} title="Safety drills" doneWord="Passed" state={plan.drillsState}>
            {plan.drills.length === 0 ? (
              <p>
                {plan.drillsState === "not_needed"
                  ? "No drills are needed, because no step saves anything."
                  : "OpenAdapt sets up five drills from your practice item when you run it. Each one makes the workflow go wrong on purpose right before the save, and must stop before saving."}
              </p>
            ) : (
              <>
                <p>
                  Each drill runs the workflow with your test values and changes one
                  thing on purpose right before the save. A drill passes only
                  when OpenAdapt's own check catches the change and stops before
                  saving. Nothing is saved, and no drill asks you to approve.
                </p>
                <ul className="drill-list">
                  {plan.drills.map((drill: CaseView) => (
                    <li key={drill.id}>
                      <Status
                        tone={drill.status === "passed" ? "ok" : drill.status === "failed" ? "warn" : "neutral"}
                        icon={drill.status === "passed" ? "check" : drill.status === "failed" ? "attention" : "ring"}
                      >
                        {drill.label}
                      </Status>
                      <span>{drill.explanation}</span>
                    </li>
                  ))}
                </ul>
                {drillNote && <p className="result-line">{drillNote}</p>}
                {plan.drillsState !== "done" && (
                <div className="check-actions">
                  <Button
                    disabled={busy !== null || plan.practiceState !== "done"}
                    onClick={() => void runDrills()}
                  >
                    {busy === "drills"
                      ? "Running the drills…"
                      : attachedBrowser(target)
                        ? "Run the next drill"
                        : "Run the drills"}
                  </Button>
                  {plan.practiceState !== "done" && (
                    <span className="simple-sub">Run the practice item first.</span>
                  )}
                </div>
                )}
              </>
            )}
          </CheckSection>

          <CheckSection
            index={6}
            title="Approve this workflow"
            doneWord="Approved"
            state={approved ? "done" : "todo"}
          >
            {approved ? (
              <div className="approved">
                <Status tone="ok" icon="check">
                  Approved and locked
                </Status>
                <p>
                  This version is checked, approved, and encrypted on this
                  computer. It&rsquo;s ready to run on real items, and it stops
                  to ask you only when something doesn&rsquo;t match.
                </p>
                <Button variant="primary" onClick={() => onRun(approvedId ?? workflowId)}>
                  Run it
                </Button>
              </div>
            ) : (
              <>
                <p>
                  You approve a workflow once. After that it runs on its own and
                  stops to ask a person only when something doesn&rsquo;t match.
                  Approving locks this version, so any change is checked again.
                  OpenAdapt lets you approve only when every check above has
                  passed.
                </p>
                {plan.blockers.length > 0 && (
                  <TechnicalDetails>
                    <p>Still open in the exact checks:</p>
                    <ul className="technical-text">
                      {plan.blockers.slice(0, 8).map((blocker) => (
                        <li key={blocker}>{blocker}</li>
                      ))}
                    </ul>
                  </TechnicalDetails>
                )}
                <div className="check-actions">
                  <Button
                    variant="primary"
                    disabled={
                      busy !== null ||
                      plan.practiceState !== "done" ||
                      plan.drillsState === "todo"
                    }
                    onClick={() => void approve()}
                  >
                    {busy === "approve" ? "Approving…" : "Approve this workflow"}
                  </Button>
                </div>
              </>
            )}
          </CheckSection>
        </div>
      )}
      {plan && (
        <p className="fine-print">
          OpenAdapt records when this version was approved. The full record of
          each check, test run, and approval is under Advanced: exact checks.
        </p>
      )}
    </div>
  );
}
