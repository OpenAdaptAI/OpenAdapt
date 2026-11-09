// Advanced: every expert tool, one click behind the gear. Nothing here was
// removed from the engine; the operator screens use the same commands.
import { useEffect, useId, useState } from "react";
import { CMD, engineTry } from "../lib/engine";
import { groupTasks } from "../lib/tasks";
import type { Workflow } from "../lib/types";
import { Button, Field } from "../ui/primitives";
import { PageHead } from "../ui/simple";

export function Advanced({
  onExactChecks,
  onLibrary,
  onRecord,
  onRunner,
  onPhone,
  onSetup,
}: {
  onExactChecks: (workflowId: string) => void;
  onLibrary: () => void;
  onRecord: () => void;
  onRunner: () => void;
  onPhone: () => void;
  onSetup: () => void;
}) {
  const id = useId();
  const [workflows, setWorkflows] = useState<Workflow[]>([]);
  const [chosen, setChosen] = useState("");
  useEffect(() => {
    void engineTry<Workflow[]>(CMD.GET_WORKFLOWS, {}, []).then((list) =>
      setWorkflows(Array.isArray(list) ? list : []),
    );
  }, []);
  const tasks = groupTasks(workflows);
  const selected = chosen || tasks[0]?.current.id || "";

  const tools = [
    {
      title: "All workflow versions",
      body: "Every version of every workflow with replay, governed run, run detail, and teach controls.",
      action: onLibrary,
      label: "Open all versions",
    },
    {
      title: "Record with all options",
      body: "Every app type and target field, the recording review, privacy scrubbing, and the presentation export.",
      action: onRecord,
      label: "Open recording",
    },
    {
      title: "Runner",
      body: "Let OpenAdapt Cloud send approved runs to this computer. Needs a Cloud sign-in.",
      action: onRunner,
      label: "Open runner",
    },
    {
      title: "Phone",
      body: "Pair a phone and answer stopped runs from it, including ingress settings for IT.",
      action: onPhone,
      label: "Open phone pairing",
    },
    {
      title: "Recording setup",
      body: "Operating-system permissions and the local video engine for desktop-app recordings.",
      action: onSetup,
      label: "Open setup",
    },
  ];

  return (
    <div className="content simple">
      <PageHead
        title="Advanced"
        sub="For automation engineers and IT. These tools work on the same workflows as the main screens."
      />
      <section className="advanced-tool">
        <div>
          <h2>Exact checks</h2>
          <p>
            The full readiness test for one workflow: the program map, each step's
            risk, right-record and record-check settings, test cases, approval,
            locking, export, and deploy.
          </p>
        </div>
        {tasks.length > 0 ? (
          <div className="advanced-picker">
            <Field label="Workflow" htmlFor={`${id}-task`}>
              <select
                id={`${id}-task`}
                className="input"
                value={selected}
                onChange={(event) => setChosen(event.target.value)}
              >
                {tasks.map((task) => (
                  <option key={task.key} value={task.current.id}>
                    {task.current.name}
                  </option>
                ))}
              </select>
            </Field>
            <Button disabled={!selected} onClick={() => onExactChecks(selected)}>
              Open exact checks
            </Button>
          </div>
        ) : (
          <p className="simple-sub">Record a workflow first.</p>
        )}
      </section>
      {tools.map((tool) => (
        <section className="advanced-tool" key={tool.title}>
          <div>
            <h2>{tool.title}</h2>
            <p>{tool.body}</p>
          </div>
          <Button onClick={tool.action}>{tool.label}</Button>
        </section>
      ))}
    </div>
  );
}
