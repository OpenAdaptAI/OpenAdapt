// Local teach-the-fix (spec §2, byoc-required). The halted run + a fix
// (re-recorded or corrected) go to the engine's `teach_fix`, which runs
// `openadapt-flow teach` LOCALLY and promotes a new bundle if gated. Nothing
// leaves the machine — this is the regulated-lane correction surface.
import { useEffect, useState } from "react";
import { CMD, engineInvoke, engineTry } from "../lib/engine";
import type { RunReport } from "../lib/types";
import { Button, Card, CardHead, Callout, Field } from "../ui/primitives";

export function Teach({
  workflowId,
  onDone,
}: {
  workflowId: string;
  onDone: () => void;
}) {
  const [report, setReport] = useState<RunReport | null>(null);
  const [note, setNote] = useState("");
  const [mode, setMode] = useState<"record" | "describe">("record");
  const [phase, setPhase] = useState<"idle" | "recording" | "teaching">("idle");
  const [result, setResult] = useState<string | null>(null);

  useEffect(() => {
    engineTry<RunReport | null>(
      CMD.GET_RUN_REPORT,
      { workflow_id: workflowId },
      null,
    ).then(setReport);
  }, [workflowId]);

  async function recordFix() {
    setPhase("recording");
    try {
      await engineInvoke(CMD.START_RECORDING, { purpose: "teach_fix" });
    } catch {
      setPhase("idle");
    }
  }
  async function stopFix() {
    try {
      await engineInvoke(CMD.STOP_RECORDING, {});
    } finally {
      setPhase("idle");
    }
  }

  async function teach() {
    setPhase("teaching");
    setResult(null);
    try {
      const r = await engineInvoke<{ promoted?: boolean; message?: string }>(
        CMD.TEACH_FIX,
        { workflow_id: workflowId, note, mode },
      );
      setResult(
        r?.promoted
          ? "Fix accepted and promoted locally."
          : r?.message ?? "Teach submitted.",
      );
    } catch (e) {
      setResult(String(e));
    } finally {
      setPhase("idle");
    }
  }

  return (
    <div className="content">
      <div className="page-head">
        <div className="titles">
          <p className="eyebrow">Correct · stays local</p>
          <h1>Teach the fix</h1>
        </div>
      </div>

      {report?.halt && (
        <Card>
          <CardHead eyebrow="Halted step" title={report.halt.step_intent} />
          <Callout tone="warn">{report.halt.reason}</Callout>
        </Card>
      )}

      <Card>
        <CardHead
          eyebrow="Your correction"
          title="Show OpenAdapt the right action"
          sub="Re-demonstrate the halted step, or describe the correct target."
        />

        <div className="row" style={{ marginBottom: "var(--space-4)" }}>
          <Button
            variant={mode === "record" ? "primary" : "ghost"}
            size="sm"
            onClick={() => setMode("record")}
          >
            Re-record the step
          </Button>
          <Button
            variant={mode === "describe" ? "primary" : "ghost"}
            size="sm"
            onClick={() => setMode("describe")}
          >
            Describe the fix
          </Button>
        </div>

        {mode === "record" ? (
          <div className="row">
            {phase === "recording" ? (
              <>
                <span className="rec-dot" />
                <Button variant="danger" onClick={stopFix}>
                  Stop fix recording
                </Button>
              </>
            ) : (
              <Button variant="ghost" onClick={recordFix}>
                Record the correct action
              </Button>
            )}
          </div>
        ) : (
          <Field
            label="What should this step do?"
            hint="No PHI — describe the target (e.g. 'the Save button in the toolbar')."
          >
            <textarea
              className="input"
              value={note}
              onChange={(e) => setNote(e.target.value)}
            />
          </Field>
        )}

        <div className="row" style={{ marginTop: "var(--space-5)" }}>
          <Button
            variant="primary"
            disabled={phase === "teaching"}
            onClick={teach}
          >
            {phase === "teaching" ? "Teaching…" : "Submit fix"}
          </Button>
          <Button variant="ghost" onClick={onDone}>
            Back to library
          </Button>
        </div>

        {result && (
          <div style={{ marginTop: "var(--space-4)" }}>
            <Callout tone="info">{result}</Callout>
          </div>
        )}
      </Card>
    </div>
  );
}
