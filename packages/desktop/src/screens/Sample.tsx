// See it work on a sample app, before granting any permission.
//
// The engine runs Flow's built-in tutorial: a synthetic clinic app with
// made-up patients, served and driven in a hidden browser on this computer.
// It runs the sample task twice. The first time, the app saves normally. The
// second time, the app shows "saved" but drops the entry, so the reader sees
// what OpenAdapt does when the screen and the record disagree.
import { useEffect, useState } from "react";
import { CMD, EVT, engineInvoke, onEngineEvent } from "../lib/engine";
import { humanizeInput, plainStep, resultCopy, sentenceCase } from "../lib/outcomes";
import type { SampleResponse, SampleRun } from "../lib/types";
import { Button, Callout } from "../ui/primitives";
import {
  Icon,
  PageHead,
  ScreenCrop,
  Status,
  TechnicalDetails,
  iconForTone,
} from "../ui/simple";

const PROVENANCE =
  "Run on this computer with OpenAdapt's built-in sample app. All patients and data are made up.";

function SampleRunCard({ run }: { run: SampleRun }) {
  const copy = resultCopy({ key: run.result, nothingWritten: run.nothing_written });
  const normal = run.kind === "normal";
  const typed = Object.entries(run.inputs)
    .map(([name, value]) => `${sentenceCase(humanizeInput(name))}: “${value}”`)
    .join(", ");
  return (
    <article className={`sample-run ${normal ? "ok" : "warn"}`}>
      <header>
        <p className="sample-run-kicker">
          {normal ? "Run 1: the app saves normally" : "Run 2: the app drops the save"}
        </p>
        <Status tone={copy.tone} icon={iconForTone(copy.tone)}>
          {copy.label}
        </Status>
      </header>
      <p className="sample-run-story">
        {normal && run.result === "done_checked"
          ? `OpenAdapt checked it had the right patient, typed the note, and saved it. Then it read the record back: ${run.record_checks_confirmed} of ${run.record_checks_confirmed + run.record_checks_failed} checks matched.`
          : !normal && run.result === "check_record"
            ? "The app showed “Encounter saved”, but when OpenAdapt read the record back, the note wasn't there. OpenAdapt stopped and flagged the record for a person to check. It doesn't try again on its own."
            : copy.sentence}
      </p>
      {run.screen?.data_url && (
        <ScreenCrop
          dataUrl={run.screen.data_url}
          width={run.screen.width ?? 0}
          height={run.screen.height ?? 0}
          caption={normal ? "The screen after saving" : "The screen said saved. The record didn't."}
          region="top"
        />
      )}
      <ol className="sample-steps">
        {run.steps.map((step) => (
          <li key={step.index} className={step.ok ? "ok" : "stopped"}>
            <Icon name={step.ok ? "check" : "search"} />
            <span>
              {plainStep(step.intent)}
              {step.saves && <em> (saves)</em>}
            </span>
          </li>
        ))}
      </ol>
      <TechnicalDetails>
        <dl className="technical-list">
          <dt>Execution outcome</dt>
          <dd className="mono">{run.execution_outcome || "none"}</dd>
          <dt>Transaction outcome</dt>
          <dd className="mono">{run.transaction_outcome || "none"}</dd>
          <dt>Right-record checks passed</dt>
          <dd className="tnum">{run.right_record_checks}</dd>
          <dt>AI model calls</dt>
          <dd className="tnum">{run.model_calls ?? "not reported"}</dd>
          {typed && (
            <>
              <dt>Values typed</dt>
              <dd>{typed}</dd>
            </>
          )}
        </dl>
      </TechnicalDetails>
    </article>
  );
}

export function Sample({
  onShow,
  onTasks,
  onRunningChange,
}: {
  onShow: () => void;
  onTasks: () => void;
  onRunningChange?: (running: boolean) => void;
}) {
  const [state, setState] = useState<"idle" | "running" | "done" | "error">("idle");
  const [detail, setDetail] = useState("");
  const [response, setResponse] = useState<SampleResponse | null>(null);

  useEffect(() => {
    const unlisten = onEngineEvent(
      EVT.SAMPLE_PROGRESS,
      (payload: { state?: string; detail?: string }) => {
        if (payload?.state === "browser") {
          setDetail(payload.detail || "Preparing the browser…");
        } else if (payload?.state === "running") {
          setDetail("Running the sample task twice…");
        }
      },
    );
    return () => {
      void unlisten.then((stop) => stop()).catch(() => {});
    };
  }, []);

  useEffect(() => {
    onRunningChange?.(state === "running");
  }, [state, onRunningChange]);

  async function run() {
    setState("running");
    setDetail("Starting…");
    try {
      const result = await engineInvoke<SampleResponse>(CMD.RUN_SAMPLE, {});
      setResponse(result);
      setState(result.ok ? "done" : "error");
    } catch (error) {
      setResponse({ ok: false, error: String(error) });
      setState("error");
    }
  }

  return (
    <div className="content simple">
      <PageHead
        title="See it work on a sample app"
        sub="OpenAdapt adds a follow-up note to a made-up patient in a built-in sample clinic app, then reads the record back to check it saved."
      />

      {state !== "done" && (
        <section className="sample-intro">
          <ol className="sample-plan">
            <li>
              <strong>Run 1.</strong> The sample app saves normally. OpenAdapt
              confirms the note is in the record.
            </li>
            <li>
              <strong>Run 2.</strong> The sample app shows “saved” but quietly
              drops the note. You'll see what OpenAdapt does when the screen and
              the record disagree.
            </li>
          </ol>
          <p className="simple-sub">
            It runs on this computer in a hidden browser. No account, no
            permissions, and nothing leaves this computer. It takes 1 to 3
            minutes; the first time can take longer while OpenAdapt prepares its
            browser.
          </p>
          <div className="row">
            <Button variant="primary" disabled={state === "running"} onClick={() => void run()}>
              {state === "running" ? "Running the sample…" : "Run the sample"}
            </Button>
            {state === "running" && (
              <Status tone="run" icon="dot">
                {detail}
              </Status>
            )}
          </div>
          {state === "error" && (
            <Callout tone="warn" title="The sample didn't run">
              {response?.error || "Check that the OpenAdapt engine is running, then try again."}
            </Callout>
          )}
        </section>
      )}

      {state === "done" && response?.runs && (
        <>
          <div className="sample-grid">
            {response.runs.map((item) => (
              <SampleRunCard key={item.kind} run={item} />
            ))}
          </div>
          <p className="provenance-line">{PROVENANCE}</p>
          <section className="sample-next">
            <h2>Now try your own task</h2>
            <p>
              Show OpenAdapt a task in the app where you already do it.
              Use a test record.
            </p>
            <div className="row">
              <Button variant="primary" onClick={onShow}>
                Show OpenAdapt your task
              </Button>
              <Button onClick={onTasks}>Back to Tasks</Button>
            </div>
          </section>
        </>
      )}
    </div>
  );
}
