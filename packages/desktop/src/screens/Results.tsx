// Results: what got done and checked, what needed a person, what OpenAdapt
// prevented, and an estimate of time saved that shows its own baseline.
//
// Only items whose run read the record back (transaction outcome VERIFIED)
// count as done. The time estimate uses minutes per item that the reader can
// change; the default is the length of their own recording of the task.
import { useEffect, useMemo, useState } from "react";
import {
  SWITCH_LABEL,
  confirmedButNeedsLook,
  offReasonSentence,
  shortDate,
  switchStatus,
} from "../lib/approvalMode";
import { CMD, engineTry } from "../lib/engine";
import { plainResult, resultCopy } from "../lib/outcomes";
import { groupTasks, taskCounts } from "../lib/tasks";
import type { ResultsSummary, SwitchSummary, Workflow } from "../lib/types";
import {
  PageHead,
  StatTile,
  Status,
  formatDuration,
  iconForTone,
  relativeTime,
} from "../ui/simple";

const BASELINE_KEY = "openadapt.desktop.results.minutes-per-item.v1";

function loadBaselines(): Record<string, number> {
  try {
    const raw = window.localStorage.getItem(BASELINE_KEY);
    const parsed = raw ? JSON.parse(raw) : {};
    return parsed && typeof parsed === "object" ? parsed : {};
  } catch {
    return {};
  }
}

function saveBaselines(value: Record<string, number>) {
  try {
    window.localStorage.setItem(BASELINE_KEY, JSON.stringify(value));
  } catch {
    /* a storage-restricted window still works for this session */
  }
}

export function Results({ onNeedsYou }: { onNeedsYou: () => void }) {
  const [summary, setSummary] = useState<ResultsSummary | null>(null);
  const [workflows, setWorkflows] = useState<Workflow[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [minutes, setMinutes] = useState<Record<string, number>>(loadBaselines);

  useEffect(() => {
    void (async () => {
      const [results, list] = await Promise.all([
        engineTry<ResultsSummary | null>(CMD.GET_RESULTS_SUMMARY, {}, null),
        engineTry<Workflow[]>(CMD.GET_WORKFLOWS, {}, []),
      ]);
      setSummary(results && results.ok ? results : null);
      setWorkflows(Array.isArray(list) ? list : []);
      setLoaded(true);
    })();
  }, []);

  const rows = useMemo(
    () =>
      groupTasks(workflows).map((task) => {
        const counts = taskCounts(task, summary);
        const recorded = task.versions.find((item) => item.demo_seconds)?.demo_seconds ?? null;
        const chosen = minutes[task.key];
        const perItemSeconds =
          typeof chosen === "number" && chosen > 0 ? chosen * 60 : recorded;
        return {
          task,
          counts,
          recorded,
          perItemSeconds,
          custom: typeof chosen === "number" && chosen > 0,
          saved: perItemSeconds ? counts.done_checked * perItemSeconds : null,
        };
      }),
    [workflows, summary, minutes],
  );

  const total = summary?.total;
  const withoutPerson = total?.saved_without_person ?? 0;
  const recentItems = (summary?.recent ?? []).slice(0, 20);
  // "No person" marks an item Flow saved with every check passed; "Went to a
  // person" a confirmed save that still waits for one. Nothing else is tagged.
  const recentTags = recentItems.map((item) => {
    const closedByPerson =
      item.status === "confirmed_saved" || item.status === "done_by_person";
    if (item.saved_without_person) return "No person";
    if (confirmedButNeedsLook(item) && !closedByPerson) return "Went to a person";
    return null;
  });
  const switches = summary?.switches ?? [];
  const savedSeconds = rows.reduce((sum, row) => sum + (row.saved ?? 0), 0);
  const anyEstimate = rows.some((row) => row.saved !== null && row.counts.done_checked > 0);

  function setTaskMinutes(key: string, value: string) {
    const next = { ...minutes };
    const number = Number(value);
    if (value === "" || !Number.isFinite(number) || number <= 0) delete next[key];
    else next[key] = Math.min(600, number);
    setMinutes(next);
    saveBaselines(next);
  }

  if (!loaded) {
    return (
      <div className="content simple">
        <PageHead title="Results" />
        <p className="simple-sub">Loading…</p>
      </div>
    );
  }

  if (!total || total.items === 0) {
    return (
      <div className="content simple">
        <PageHead
          title="Results"
          sub="What OpenAdapt finished, what needed a person, and what it prevented."
        />
        <div className="calm-empty">
          <Status tone="neutral" icon="ring">
            No results yet
          </Status>
          <p>
            Run a checked workflow on a list of items. Each item shows up here, and
            only items OpenAdapt read back from the record count as done.
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className="content simple wide">
      <PageHead
        title="Results"
        sub="What OpenAdapt finished, what needed a person, and what it prevented. OpenAdapt doesn't send these results anywhere. If you turn on the hosted runner, it reports only a PHI-free outcome for each Cloud job it runs."
      />

      <div className="stats">
        <StatTile
          label="Done and checked"
          icon="check"
          tone="ok"
          value={total.done_checked}
          note={
            withoutPerson > 0 ? (
              <>
                Saved, then read back from the record.{" "}
                <span className="stat-sub">
                  {withoutPerson} of them without a person: every check passed.
                </span>
              </>
            ) : (
              "Saved, then read back from the record"
            )
          }
        />
        <StatTile
          label="Needed a person"
          icon="attention"
          tone={total.waiting > 0 ? "warn" : "neutral"}
          value={total.needed_person}
          note={
            total.waiting > 0 ? (
              <button type="button" className="link-button" onClick={onNeedsYou}>
                {total.waiting} still waiting
              </button>
            ) : (
              "All decided"
            )
          }
        />
        <StatTile
          label="Wrong-record saves prevented"
          icon="shield"
          tone="neutral"
          value={total.wrong_record_prevented}
          note="Stopped before saving on a record that didn't match"
        />
        <StatTile
          label="Time saved (estimate)"
          icon="half"
          tone="neutral"
          value={anyEstimate ? `about ${formatDuration(savedSeconds)}` : "Not enough data"}
          note="Items done and checked × minutes per item by hand. See the table."
        />
      </div>

      {switches.length > 0 && <SwitchSection switches={switches} />}

      <section className="results-section" aria-labelledby="by-task">
        <h2 id="by-task">By workflow</h2>
        <div className="table-scroll">
          <table className="results-table">
            <thead>
              <tr>
                <th>Workflow</th>
                <th className="num">Done and checked</th>
                <th className="num">Needed a person</th>
                <th className="num">Prevented</th>
                <th>Minutes per item by hand</th>
                <th className="num">Time saved (estimate)</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.task.key}>
                  <td>{row.task.current.name}</td>
                  <td className="num tnum">
                    {row.counts.done_checked}
                    {(row.counts.saved_without_person ?? 0) > 0 && (
                      <span className="stat-sub">
                        {" "}
                        ({row.counts.saved_without_person} without a person)
                      </span>
                    )}
                  </td>
                  <td className="num tnum">{row.counts.needed_person}</td>
                  <td className="num tnum">{row.counts.wrong_record_prevented}</td>
                  <td>
                    <input
                      className="input input-number"
                      type="number"
                      min="0.1"
                      step="0.5"
                      inputMode="decimal"
                      aria-label={`Minutes per item by hand for ${row.task.current.name}`}
                      placeholder="Set"
                      value={
                        row.custom
                          ? String(minutes[row.task.key])
                          : row.recorded
                            ? String(Math.round((row.recorded / 60) * 10) / 10)
                            : ""
                      }
                      onChange={(event) => setTaskMinutes(row.task.key, event.target.value)}
                    />
                    <span className="baseline-source">
                      {row.custom
                        ? "Your number"
                        : row.recorded
                          ? `From your recording (${formatDuration(row.recorded)})`
                          : "No baseline yet"}
                    </span>
                  </td>
                  <td className="num tnum">
                    {row.saved !== null && row.counts.done_checked > 0
                      ? `about ${formatDuration(row.saved)}`
                      : "None yet"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <p className="fine-print">
          The time saved is an estimate, not a measurement. It multiplies the
          items OpenAdapt finished and checked by the minutes each one takes by
          hand. The default is how long your recording of the workflow took; type
          your team's own number if it's different. Time spent answering Needs
          you isn't subtracted.
        </p>
      </section>

      <section className="results-section" aria-labelledby="recent">
        <h2 id="recent">Recent items</h2>
        <ul className={`recent-list${recentTags.some(Boolean) ? " with-tags" : ""}`}>
          {recentItems.map((item, index) => {
            const plain = item.result
              ? plainResult(item.execution_outcome, item.transaction_outcome ?? null, {
                  preActionRefusal: item.result === "did_not_start",
                })
              : null;
            const copy = plain ? resultCopy(plain) : null;
            const closedByPerson =
              item.status === "confirmed_saved" || item.status === "done_by_person";
            const tag = recentTags[index];
            return (
              <li key={item.item_id}>
                <span className="recent-task">{item.task_name}</span>
                <span className="recent-label mono">{item.label || "Item"}</span>
                {copy ? (
                  <Status tone={copy.tone} icon={iconForTone(copy.tone)}>
                    {closedByPerson ? "Finished by a person" : copy.label}
                  </Status>
                ) : (
                  <Status tone="neutral" icon="ring">
                    Waiting to run
                  </Status>
                )}
                {recentTags.some(Boolean) &&
                  (tag ? <span className="mode-tag">{tag}</span> : <span />)}
                <span className="recent-time">
                  {relativeTime(item.finished_at || item.created_at)}
                </span>
              </li>
            );
          })}
        </ul>
        <p className="fine-print">
          Only items that OpenAdapt saved and then read back from the record
          count as done and checked. Items a person finished or confirmed count
          under Needed a person. “No person” marks an item saved with every
          check passed while “{SWITCH_LABEL}” was on.
        </p>
      </section>
    </div>
  );
}

function switchDetail(entry: SwitchSummary): string {
  switch (entry.state) {
    case "on":
      return entry.turned_on ? `Since ${shortDate(entry.turned_on.at)}.` : "";
    case "waiting_for_cloud":
      return "Items wait for a person until OpenAdapt Cloud confirms it.";
    case "not_used_here":
      return "On in OpenAdapt Cloud. This computer doesn't use it yet.";
    case "off": {
      const since = entry.off_at ? `Since ${shortDate(entry.off_at)}. ` : "";
      return `${since}${offReasonSentence(entry.off_reason, {
        step: entry.off_step,
        byThisComputer: entry.off_by_this_computer,
      })}`;
    }
    default:
      return "";
  }
}

function SwitchSection({ switches }: { switches: SwitchSummary[] }) {
  return (
    <section className="results-section" aria-labelledby="switches">
      <h2 id="switches">{SWITCH_LABEL}</h2>
      <ul className="switch-summary">
        {switches.map((entry) => {
          const status = switchStatus(entry.state);
          return (
            <li key={entry.workflow_id}>
              <span className="switch-summary-task">{entry.task_name || "A workflow"}</span>
              <Status tone={status.tone} icon={status.icon}>
                {status.word}
              </Status>
              <span className="faint">{switchDetail(entry)}</span>
            </li>
          );
        })}
      </ul>
    </section>
  );
}
