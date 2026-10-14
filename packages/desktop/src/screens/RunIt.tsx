// Run it: give a checked task a list of items and watch it work through them.
//
// Items run one at a time through the local engine's run_item command, which
// passes each item's values to Flow in a private file and records the result.
// The batch stops when an item's save may have gone through, because nothing
// may run blindly after an uncertain save. Items that stopped before saving go
// to Needs you and the batch moves on.
import { useEffect, useId, useMemo, useRef, useState } from "react";
import { CMD, engineInvoke, engineTry } from "../lib/engine";
import { humanizeInput, plainResult, resultCopy, sentenceCase } from "../lib/outcomes";
import { parseItems, type ParsedItem } from "../lib/parseItems";
import type {
  ExecutionTarget,
  QualificationParameter,
  RunItemResponse,
  TaskInputs,
  Workflow,
} from "../lib/types";
import { ExecutionTargetForm, TARGETS } from "../ui/ExecutionTargetForm";
import { Button, Callout, Field, SegControl } from "../ui/primitives";
import {
  Icon,
  PageHead,
  Status,
  iconForTone,
  plural,
} from "../ui/simple";

type Source = "paste" | "file" | "one";

type ItemState =
  | { state: "waiting" }
  | { state: "running" }
  | { state: "not_run" }
  | { state: "skipped"; reason: string }
  | { state: "finished"; key: ReturnType<typeof plainResult>["key"]; nothingWritten: boolean }
  | { state: "refused"; reason: string };

function targetSummary(target: ExecutionTarget | null): string {
  if (!target) return "Not set";
  const label = TARGETS[target.backend]?.label ?? target.backend;
  const where =
    target.url ||
    target.macos_app ||
    target.linux_app ||
    target.rdp_host ||
    target.rdp_window ||
    target.rdp_window_title ||
    target.agent_url ||
    "";
  return where ? `${label}: ${where}` : label;
}

export function RunIt({
  workflowId,
  onCheck,
  onNeedsYou,
  onResults,
  onRunningChange,
}: {
  workflowId: string;
  onCheck: (workflowId: string) => void;
  onNeedsYou: () => void;
  onResults: () => void;
  onRunningChange?: (running: boolean) => void;
}) {
  const idPrefix = useId();
  const [workflow, setWorkflow] = useState<Workflow | null>(null);
  const [inputs, setInputs] = useState<QualificationParameter[] | null>(null);
  const [loadError, setLoadError] = useState("");
  const [target, setTarget] = useState<ExecutionTarget | null>(null);
  const [deploymentConfig, setDeploymentConfig] = useState("");
  const [source, setSource] = useState<Source>("paste");
  const [pasted, setPasted] = useState("");
  const [fileName, setFileName] = useState("");
  const [single, setSingle] = useState<Record<string, string>>({});
  const [states, setStates] = useState<ItemState[]>([]);
  const [running, setRunning] = useState(false);
  const [finished, setFinished] = useState(false);
  const [batchNote, setBatchNote] = useState("");
  const stopRequested = useRef(false);

  useEffect(() => {
    void (async () => {
      const [list, details] = await Promise.all([
        engineTry<Workflow[]>(CMD.GET_WORKFLOWS, {}, []),
        engineTry<TaskInputs>(
          CMD.GET_TASK_INPUTS,
          { workflow_id: workflowId },
          { ok: false, workflow_id: workflowId, error: "The local engine isn't answering." },
        ),
      ]);
      setWorkflow((Array.isArray(list) ? list : []).find((item) => item.id === workflowId) ?? null);
      if (!details.ok) {
        setLoadError(details.error || "Desktop couldn't read this task's inputs.");
        setInputs([]);
        return;
      }
      setInputs((details.inputs ?? []).filter((item) => !item.secret));
      setTarget(details.target ?? { backend: "web" });
    })();
  }, [workflowId]);

  useEffect(() => {
    onRunningChange?.(running);
  }, [running, onRunningChange]);

  const declared = inputs ?? [];
  const parsed = useMemo(() => {
    if (source === "one") {
      const values: Record<string, string> = {};
      for (const input of declared) {
        if (single[input.name] !== undefined && single[input.name] !== "") {
          values[input.name] = single[input.name];
        }
      }
      const problems = declared
        .filter((input) => input.required && !(values[input.name] ?? "").trim())
        .map((input) => `No ${humanizeInput(input.name)}`);
      const touched = Object.keys(values).length > 0;
      return {
        items: touched ? [{ position: 0, values, problems }] : [],
        header: false,
        error: null,
        ignored: [],
      };
    }
    return parseItems(pasted, declared);
  }, [source, pasted, single, declared]);

  const ready = parsed.items.filter((item) => item.problems.length === 0);
  const blocked = Boolean(workflow?.blocked);
  const notReady = workflow !== null && workflow.readiness !== "ready";
  const canRun =
    !running &&
    !finished &&
    ready.length > 0 &&
    !blocked &&
    !notReady &&
    target !== null &&
    !parsed.error;

  async function readFile(file: File) {
    setFileName(file.name);
    const text = await file.text();
    setPasted(text);
  }

  async function runAll() {
    if (!canRun || !target) return;
    stopRequested.current = false;
    setRunning(true);
    setFinished(false);
    setBatchNote("");
    const batchId = `batch-${Date.now().toString(36)}`;
    const next: ItemState[] = parsed.items.map((item) =>
      item.problems.length
        ? { state: "skipped", reason: item.problems.join(", ") }
        : { state: "waiting" },
    );
    setStates([...next]);
    try {
      for (const item of parsed.items) {
        if (item.problems.length) continue;
        if (stopRequested.current) {
          setBatchNote("Stopped after the item that was running. The rest didn't run.");
          break;
        }
        next[item.position] = { state: "running" };
        setStates([...next]);
        const params: Record<string, unknown> = {
          workflow_id: workflowId,
          inputs: item.values,
          target,
          batch_id: batchId,
          position: item.position,
        };
        if (deploymentConfig.trim()) params.deployment_config = deploymentConfig.trim();
        let response: RunItemResponse;
        try {
          response = await engineInvoke<RunItemResponse>(CMD.RUN_ITEM, params);
        } catch (error) {
          // The engine call failed after it may have started the run. Treat
          // the item as a record to check and stop the batch.
          next[item.position] = { state: "finished", key: "check_record", nothingWritten: false };
          setStates([...next]);
          setBatchNote(
            `OpenAdapt lost track of item ${item.position + 1} (${String(error)}). Check that record before you run more.`,
          );
          break;
        }
        if (response.pre_action_refusal || !response.item) {
          next[item.position] = {
            state: "refused",
            reason: response.error || "OpenAdapt didn't start this item.",
          };
          setStates([...next]);
          if (/may have gone through/i.test(response.error ?? "")) {
            setBatchNote(response.error ?? "");
            break;
          }
          continue;
        }
        const plain = plainResult(
          response.item.execution_outcome,
          response.item.transaction_outcome ?? null,
          { preActionRefusal: response.item.result === "did_not_start" },
        );
        next[item.position] = {
          state: "finished",
          key: plain.key,
          nothingWritten: plain.nothingWritten,
        };
        setStates([...next]);
        if (plain.key !== "done_checked" && !plain.nothingWritten) {
          setBatchNote(
            `The save for item ${item.position + 1} may have gone through. OpenAdapt stopped the list so a person can check that record first.`,
          );
          break;
        }
      }
    } finally {
      setStates(
        next.map((state): ItemState =>
          state.state === "waiting" || state.state === "running" ? { state: "not_run" } : state,
        ),
      );
      setRunning(false);
      setFinished(true);
    }
  }

  // A finished list never runs again as it stands: its done items are already
  // saved, and its stopped items wait in Needs you. Running it again would
  // enter the same items twice, so the next step is a new list.
  function startNewList() {
    setPasted("");
    setFileName("");
    setSingle({});
    setStates([]);
    setFinished(false);
    setBatchNote("");
  }

  const done = states.filter((item) => item.state === "finished" && item.key === "done_checked").length;
  const needYou = states.filter(
    (item) => item.state === "finished" && item.key !== "done_checked",
  ).length;
  const notRun = states.filter(
    (item) =>
      item.state === "refused" ||
      item.state === "skipped" ||
      item.state === "not_run" ||
      (!running && item.state === "waiting"),
  ).length;
  const attempted = states.filter((item) => item.state === "finished").length;

  const sources = [
    { value: "paste" as const, label: "Paste a list" },
    { value: "file" as const, label: "Open a spreadsheet" },
    { value: "one" as const, label: "Type one" },
  ];

  return (
    <div className="content simple">
      <PageHead
        title={workflow ? `Run: ${workflow.name}` : "Run it"}
        sub="Give OpenAdapt the items to work on. It does them one at a time, checks each record after it saves, and stops to ask you when something doesn't match."
      />

      {loadError && (
        <Callout tone="warn" title="Desktop couldn't read this task">
          {loadError}
        </Callout>
      )}
      {notReady && (
        <Callout tone="info" title="Check this task first">
          OpenAdapt runs a task on real items only after it's checked and
          approved.
          <div className="callout-actions">
            <Button size="sm" variant="primary" onClick={() => onCheck(workflowId)}>
              Check it
            </Button>
          </div>
        </Callout>
      )}
      {blocked && (
        <Callout tone="warn" title="Check a record before running more">
          A save from an earlier item may have gone through. Look at that
          record and answer it in Needs you. Then come back here.
          <div className="callout-actions">
            <Button size="sm" onClick={onNeedsYou}>
              Open Needs you
            </Button>
          </div>
        </Callout>
      )}

      <section className="run-panel" aria-labelledby={`${idPrefix}-items`}>
        <div className="run-panel-head">
          <h2 id={`${idPrefix}-items`}>Items</h2>
          <SegControl options={sources} value={source} onChange={setSource} />
        </div>
        {declared.length > 0 && (
          <p className="input-chips">
            <span>Each item needs:</span>
            {declared.map((input) => (
              <span className="chip-plain" key={input.name}>
                {sentenceCase(humanizeInput(input.name))}
                {!input.required && <em> (optional)</em>}
              </span>
            ))}
          </p>
        )}

        {source === "paste" && (
          <Field
            label="Paste rows from a spreadsheet or a list"
            hint="Put the column names in the first row. For a task with one value, one item per line is enough."
            htmlFor={`${idPrefix}-paste`}
          >
            <textarea
              id={`${idPrefix}-paste`}
              className="input textarea"
              rows={7}
              disabled={running || finished}
              value={pasted}
              spellCheck={false}
              placeholder={
                declared.length
                  ? [
                      declared.map((input) => input.name).join(", "),
                      declared.map((input) => input.example ?? "…").join(", "),
                    ].join("\n")
                  : ""
              }
              onChange={(event) => setPasted(event.target.value)}
            />
          </Field>
        )}
        {source === "file" && (
          <Field
            label="Open a spreadsheet"
            hint="Save the sheet as CSV first (File, Save as, CSV). The first row names the columns. The file is read here and isn't uploaded."
            htmlFor={`${idPrefix}-file`}
          >
            <input
              id={`${idPrefix}-file`}
              className="input"
              type="file"
              accept=".csv,.tsv,.txt,text/csv,text/tab-separated-values"
              disabled={running || finished}
              onChange={(event) => {
                const file = event.target.files?.[0];
                if (file) void readFile(file);
              }}
            />
            {fileName && <span className="hint">Opened {fileName}</span>}
          </Field>
        )}
        {source === "one" && (
          <div className="single-form">
            {declared.map((input) => (
              <Field
                key={input.name}
                label={sentenceCase(humanizeInput(input.name))}
                htmlFor={`${idPrefix}-one-${input.name}`}
              >
                <input
                  id={`${idPrefix}-one-${input.name}`}
                  className="input"
                  disabled={running || finished}
                  value={single[input.name] ?? ""}
                  placeholder={input.example ?? ""}
                  onChange={(event) =>
                    setSingle({ ...single, [input.name]: event.target.value })
                  }
                />
              </Field>
            ))}
          </div>
        )}

        {parsed.error && (
          <Callout tone="info" title="OpenAdapt can't read this list yet">
            {parsed.error}
          </Callout>
        )}

        {parsed.items.length > 0 && (
          <ItemTable items={parsed.items} inputs={declared} states={states} />
        )}
        {/* How the list reads applies before it runs; once it starts, each
            row's status and the summary below say what happened. */}
        {parsed.items.length > 0 && states.length === 0 && (
          <p className="simple-sub">
            {plural(parsed.items.length, "item")}.{" "}
            {ready.length === parsed.items.length
              ? "All ready."
              : `${ready.length} ready. ${plural(parsed.items.length - ready.length, "item")} with a problem won't run.`}
            {parsed.ignored.length > 0 &&
              ` Ignored column${parsed.ignored.length === 1 ? "" : "s"}: ${parsed.ignored.join(", ")}.`}
          </p>
        )}
      </section>

      <section className="run-panel" aria-labelledby={`${idPrefix}-where`}>
        <div className="run-panel-head">
          <h2 id={`${idPrefix}-where`}>Where it runs</h2>
          <span className="simple-sub">{targetSummary(target)}</span>
        </div>
        <details className="technical">
          <summary>Change where it runs</summary>
          <div className="technical-body">
            {target && (
              <ExecutionTargetForm
                target={target}
                onChange={setTarget}
                idPrefix={`${idPrefix}-target`}
                disabled={running}
              />
            )}
            <Field
              label="Deployment settings file (for IT)"
              hint="Optional. A local YAML or JSON file with record-check and policy settings."
              htmlFor={`${idPrefix}-config`}
            >
              <input
                id={`${idPrefix}-config`}
                className="input mono"
                value={deploymentConfig}
                disabled={running}
                spellCheck={false}
                placeholder="/path/to/deployment.yaml"
                onChange={(event) => setDeploymentConfig(event.target.value)}
              />
            </Field>
          </div>
        </details>
      </section>

      <div className="run-bar">
        {running ? (
          <>
            <Status tone="run" icon="dot">
              {`Running: ${attempted} of ${ready.length} done`}
              {needYou > 0 ? `, ${needYou} need you` : ""}
            </Status>
            <Button onClick={() => (stopRequested.current = true)}>
              Stop after this item
            </Button>
          </>
        ) : finished ? (
          <Button onClick={startNewList}>Start a new list</Button>
        ) : (
          <Button variant="primary" disabled={!canRun} onClick={() => void runAll()}>
            {ready.length > 0 ? `Run ${plural(ready.length, "item")}` : "Run"}
          </Button>
        )}
      </div>

      {finished && !running && (
        <section className="run-summary" aria-live="polite">
          <h2>
            {needYou === 0 && notRun === 0
              ? "All done and checked"
              : "Finished this list"}
          </h2>
          <p>
            {plural(done, "item")} done and checked.
            {needYou > 0 && ` ${plural(needYou, "item")} need${needYou === 1 ? "s" : ""} you.`}
            {notRun > 0 && ` ${plural(notRun, "item")} didn't run.`}
          </p>
          {batchNote && (
            <p className="run-summary-note">
              <Icon name="search" />
              <span>{batchNote}</span>
            </p>
          )}
          <div className="row">
            {needYou > 0 && (
              <Button variant="primary" onClick={onNeedsYou}>
                Open Needs you
              </Button>
            )}
            <Button onClick={onResults}>See results</Button>
          </div>
        </section>
      )}
    </div>
  );
}

function ItemTable({
  items,
  inputs,
  states,
}: {
  items: ParsedItem[];
  inputs: QualificationParameter[];
  states: ItemState[];
}) {
  const shown = items.slice(0, 50);
  return (
    <div className="table-scroll">
      <table className="items-table">
        <thead>
          <tr>
            <th className="num">#</th>
            {inputs.map((input) => (
              <th key={input.name}>{sentenceCase(humanizeInput(input.name))}</th>
            ))}
            <th>Status</th>
          </tr>
        </thead>
        <tbody>
          {shown.map((item) => {
            const state = states[item.position];
            return (
              <tr key={item.position}>
                <td className="num tnum">{item.position + 1}</td>
                {inputs.map((input) => (
                  <td key={input.name} className="mono">
                    {item.values[input.name] ?? ""}
                  </td>
                ))}
                <td>
                  <ItemStatus item={item} state={state} />
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      {items.length > shown.length && (
        <p className="fine-print">
          Showing the first {shown.length} of {items.length}. All of them run.
        </p>
      )}
    </div>
  );
}

function ItemStatus({ item, state }: { item: ParsedItem; state?: ItemState }) {
  if (!state) {
    return item.problems.length ? (
      <Status tone="warn" icon="attention">
        {item.problems.join(", ")}
      </Status>
    ) : (
      <Status tone="neutral" icon="ring">
        Ready
      </Status>
    );
  }
  switch (state.state) {
    case "waiting":
      return (
        <Status tone="neutral" icon="ring">
          Waiting
        </Status>
      );
    case "running":
      return (
        <Status tone="run" icon="dot">
          Running
        </Status>
      );
    case "not_run":
      return (
        <Status tone="neutral" icon="minus">
          Not run
        </Status>
      );
    case "skipped":
      return (
        <Status tone="neutral" icon="minus">
          {`Skipped: ${state.reason}`}
        </Status>
      );
    case "refused":
      return (
        <Status tone="neutral" icon="minus">
          {`Didn't start: ${state.reason}`}
        </Status>
      );
    case "finished": {
      const copy = resultCopy({ key: state.key, nothingWritten: state.nothingWritten });
      return (
        <Status tone={copy.tone} icon={iconForTone(copy.tone)}>
          {copy.label}
        </Status>
      );
    }
  }
}
