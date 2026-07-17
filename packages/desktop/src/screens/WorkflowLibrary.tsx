// Workflow library — local compiled workflows, their last-run state, halts, and
// sync status. Push to cloud (cloud lane) or open the local teach view (byoc).
import { useEffect, useState } from "react";
import { CMD, engineInvoke, engineTry } from "../lib/engine";
import type { Workflow } from "../lib/types";
import {
  Button,
  Card,
  CardHead,
  EmptyState,
  Pill,
  StatePill,
} from "../ui/primitives";

export function WorkflowLibrary({
  onWatch,
  onTeach,
  onRecord,
}: {
  onWatch: (id: string) => void;
  onTeach: (id: string) => void;
  onRecord: () => void;
}) {
  const [workflows, setWorkflows] = useState<Workflow[]>([]);
  const [loading, setLoading] = useState(true);
  const [pushing, setPushing] = useState<string | null>(null);

  async function refresh() {
    const list = await engineTry<Workflow[]>(CMD.GET_WORKFLOWS, {}, []);
    setWorkflows(list);
    setLoading(false);
  }

  useEffect(() => {
    void refresh();
  }, []);

  async function push(id: string) {
    setPushing(id);
    try {
      await engineInvoke(CMD.PUSH_WORKFLOW, { workflow_id: id });
      await refresh();
    } catch {
      /* surfaced via sync state elsewhere */
    } finally {
      setPushing(null);
    }
  }

  return (
    <div className="content">
      <div className="page-head">
        <div className="titles">
          <p className="eyebrow">Local library</p>
          <h1>Workflows</h1>
        </div>
        <Button variant="primary" onClick={onRecord}>
          Record new
        </Button>
      </div>

      <Card>
        <CardHead
          eyebrow="Compiled"
          title="Your workflows"
          sub="Recorded, compiled, and replayable on this machine."
        />
        {loading ? (
          <p className="page-sub">Loading…</p>
        ) : workflows.length === 0 ? (
          <EmptyState
            title="No workflows yet"
            body="Record a demonstration and OpenAdapt compiles it into a workflow you can watch run."
            action={
              <Button variant="primary" onClick={onRecord}>
                Record your first workflow
              </Button>
            }
          />
        ) : (
          <table>
            <thead>
              <tr>
                <th>Name</th>
                <th className="num">Steps</th>
                <th>Last run</th>
                <th>Sync</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {workflows.map((w) => (
                <tr key={w.id}>
                  <td>
                    {w.name}
                    {w.open_halts ? (
                      <span style={{ marginLeft: "var(--space-2)" }}>
                        <Pill tone="warn">{w.open_halts} halt</Pill>
                      </span>
                    ) : null}
                  </td>
                  <td className="num">{w.steps}</td>
                  <td>
                    {w.last_run_state ? (
                      <StatePill state={w.last_run_state} />
                    ) : (
                      <span className="page-sub">—</span>
                    )}
                  </td>
                  <td>
                    <Pill tone={w.synced ? "ok" : "neutral"}>
                      {w.synced ? "synced" : "local"}
                    </Pill>
                  </td>
                  <td className="num">
                    <div className="row" style={{ justifyContent: "flex-end" }}>
                      <Button size="sm" onClick={() => onWatch(w.id)}>
                        Watch run
                      </Button>
                      {w.open_halts ? (
                        <Button size="sm" onClick={() => onTeach(w.id)}>
                          Teach fix
                        </Button>
                      ) : (
                        <Button
                          size="sm"
                          disabled={pushing === w.id}
                          onClick={() => push(w.id)}
                        >
                          {pushing === w.id ? "Pushing…" : "Push"}
                        </Button>
                      )}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>
    </div>
  );
}
