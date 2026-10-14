// Tasks: the home screen. One card per task, each with one status word and
// one next step (Show it, Check it, or Run it). Everything else for a task is
// one click deeper under "More".
import { useEffect, useState } from "react";
import { CMD, engineTry } from "../lib/engine";
import { resultCopy } from "../lib/outcomes";
import { groupTasks, taskCounts, taskStatus, type Task } from "../lib/tasks";
import type { ResultsSummary, Workflow } from "../lib/types";
import { Button } from "../ui/primitives";
import {
  ChoiceButton,
  PageHead,
  Status,
  iconForTone,
  plural,
} from "../ui/simple";

export interface TaskActions {
  onShow: () => void;
  onSample: () => void;
  onCheck: (workflowId: string) => void;
  onRun: (workflowId: string) => void;
  onNeedsYou: () => void;
  onWatch: (workflowId: string) => void;
  onExactChecks: (workflowId: string) => void;
  onTeach: (workflowId: string) => void;
}

function LastResult({ workflow }: { workflow: Workflow }) {
  if (!workflow.last_result) return null;
  // Only the label shows here, and no label claims anything about a write.
  const copy = resultCopy({ key: workflow.last_result, nothingWritten: false });
  return (
    <span className="task-meta-item">
      Last run: <Status tone={copy.tone} icon={iconForTone(copy.tone)}>{copy.label}</Status>
    </span>
  );
}

function TaskCard({
  task,
  summary,
  actions,
}: {
  task: Task;
  summary: ResultsSummary | null;
  actions: TaskActions;
}) {
  const status = taskStatus(task);
  const counts = taskCounts(task, summary);
  const id = task.current.id;
  const description = task.current.task?.trim();
  const go = () => {
    if (status.next === "run") actions.onRun(id);
    else if (status.next === "check") actions.onCheck(id);
    else if (status.next === "needs") actions.onNeedsYou();
    else actions.onTeach(id);
  };
  return (
    <article className="task-card" aria-label={task.current.name}>
      <div className="task-main">
        <div className="task-title-row">
          <h2>{task.current.name}</h2>
          <Status tone={status.tone} icon={status.icon}>
            {status.word}
          </Status>
        </div>
        {description && description !== task.current.name && (
          <p className="task-description">{description}</p>
        )}
        <p className="task-detail">{status.detail}</p>
        <div className="task-meta">
          <LastResult workflow={task.current} />
          {counts.done_checked > 0 && (
            <span className="task-meta-item">
              {plural(counts.done_checked, "item")} done and checked
            </span>
          )}
          {counts.wrong_record_prevented > 0 && (
            <span className="task-meta-item">
              {plural(counts.wrong_record_prevented, "wrong-record save")} prevented
            </span>
          )}
        </div>
        <details className="task-more">
          <summary>More</summary>
          <div className="task-more-body">
            <Button size="sm" onClick={() => actions.onWatch(id)}>
              See the last run
            </Button>
            <Button size="sm" onClick={() => actions.onExactChecks(id)}>
              Exact checks
            </Button>
            <Button size="sm" onClick={() => actions.onTeach(id)}>
              Teach a fix
            </Button>
            {status.next !== "check" && (
              <Button size="sm" onClick={() => actions.onCheck(id)}>
                Check it again
              </Button>
            )}
            <Button size="sm" onClick={actions.onShow}>
              Show it again
            </Button>
          </div>
        </details>
      </div>
      <div className="task-action">
        <Button className="btn-soft" onClick={go}>
          {status.nextLabel}
        </Button>
      </div>
    </article>
  );
}

export function Tasks({
  attention,
  ...actions
}: TaskActions & { attention: number }) {
  const [workflows, setWorkflows] = useState<Workflow[] | null>(null);
  const [summary, setSummary] = useState<ResultsSummary | null>(null);

  useEffect(() => {
    void (async () => {
      const [list, results] = await Promise.all([
        engineTry<Workflow[]>(CMD.GET_WORKFLOWS, {}, []),
        engineTry<ResultsSummary | null>(CMD.GET_RESULTS_SUMMARY, {}, null),
      ]);
      setWorkflows(Array.isArray(list) ? list : []);
      setSummary(results && results.ok ? results : null);
    })();
  }, [attention]);

  const tasks = groupTasks(workflows ?? []);

  return (
    <div className="content simple">
      <PageHead
        title="Tasks"
        sub="What OpenAdapt does for you, and what each task needs next."
        action={
          tasks.length > 0 ? (
            <Button variant="primary" onClick={actions.onShow}>
              Show OpenAdapt a new task
            </Button>
          ) : undefined
        }
      />

      {workflows === null ? (
        <p className="simple-sub">Loading…</p>
      ) : tasks.length === 0 ? (
        <section className="first-task" aria-label="Get started">
          <h2>Hand off your first task</h2>
          <p>
            Show OpenAdapt a task once, in the app where you already do it.
            OpenAdapt repeats it, checks each saved record, and stops to ask you
            when something doesn't match.
          </p>
          <div className="choice-row">
            <ChoiceButton
              tone="primary"
              title="Show OpenAdapt your task"
              sub="Do it once on test data while OpenAdapt watches."
              onClick={actions.onShow}
            />
            <ChoiceButton
              title="See it work on a sample app"
              sub="A built-in clinic app with made-up patients. No setup."
              onClick={actions.onSample}
            />
          </div>
        </section>
      ) : (
        <div className="task-list">
          {tasks.map((task) => (
            <TaskCard key={task.key} task={task} summary={summary} actions={actions} />
          ))}
        </div>
      )}
    </div>
  );
}
