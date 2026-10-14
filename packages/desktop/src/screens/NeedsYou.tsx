// Needs you: one inbox for every item OpenAdapt stopped on.
//
// Each card asks one plain question, says what was prevented, shows the screen
// where the run stopped, and offers only the choices the evidence allows. A
// retry is offered only when the run proved nothing was written; a possible
// save first needs a person to look at the record.
import { useEffect, useState } from "react";
import { CMD, engineInvoke, engineTry } from "../lib/engine";
import { attentionCopy } from "../lib/attention";
import {
  SWITCH_LABEL,
  confirmedButNeedsLook,
  offAutomatically,
  offReasonSentence,
  whyItCameToYou,
} from "../lib/approvalMode";
import { plainResult, resultCopy } from "../lib/outcomes";
import type {
  AttentionAction,
  AttentionItem,
  AttentionScreenshot,
  RunItemResponse,
  SwitchSummary,
  TaskInputs,
} from "../lib/types";
import { Callout } from "../ui/primitives";
import {
  ChoiceButton,
  Icon,
  PageHead,
  ScreenCrop,
  Status,
  TechnicalDetails,
  relativeTime,
} from "../ui/simple";

interface Notice {
  /** Green ("ok") only for a run that was done and checked. */
  tone: "ok" | "warn" | "neutral";
  text: string;
}

function ItemRow({
  item,
  selected,
  onSelect,
}: {
  item: AttentionItem;
  selected: boolean;
  onSelect: () => void;
}) {
  const uncertain = !item.nothing_written;
  const looked = confirmedButNeedsLook(item);
  return (
    <button
      type="button"
      className={`inbox-row ${selected ? "selected" : ""}`}
      aria-current={selected ? "true" : undefined}
      onClick={onSelect}
    >
      <span className={`inbox-row-icon ${uncertain ? "warn" : "ok"}`}>
        <Icon name={uncertain ? "search" : "attention"} />
      </span>
      <span className="inbox-row-text">
        <strong>{item.task_name || "A task"}</strong>
        <span>
          {item.label ? `${item.label}. ` : ""}
          {looked
            ? "Done and checked. Look at the record"
            : uncertain
              ? "Check the record"
              : "Stopped before saving"}
        </span>
      </span>
      <span className="inbox-row-age">{relativeTime(item.created_at)}</span>
    </button>
  );
}

function AttentionCard({
  item,
  busy,
  onResolve,
}: {
  item: AttentionItem;
  busy: AttentionAction | null;
  onResolve: (action: AttentionAction, recordChecked: boolean) => void;
}) {
  const copy = attentionCopy(item);
  const why = whyItCameToYou(item);
  const [screen, setScreen] = useState<AttentionScreenshot | null>(null);
  const [recordChecked, setRecordChecked] = useState(false);

  useEffect(() => {
    setScreen(null);
    setRecordChecked(false);
    if (item.source !== "item") return;
    void engineTry<AttentionScreenshot>(
      CMD.GET_ATTENTION_SCREENSHOT,
      { item_id: item.item_id },
      { ok: false },
    ).then(setScreen);
  }, [item.item_id, item.source]);

  const needsCheck = copy.choices.some((choice) => choice.needsRecordCheck);
  const plain = item.result
    ? plainResult(item.execution_outcome, item.transaction_outcome ?? null, {
        preActionRefusal: item.result === "did_not_start",
      })
    : null;

  return (
    <article className="attention-card" aria-labelledby={`q-${item.item_id}`}>
      <p className="attention-context">
        {item.task_name || "A task"}
        {item.label ? <span className="attention-label">{item.label}</span> : null}
        {item.approval?.mode === "problems_only" && (
          <span className="mode-tag">From a list with “{SWITCH_LABEL}” on</span>
        )}
      </p>
      <h2 id={`q-${item.item_id}`}>{copy.question}</h2>
      <p className="attention-sentence">{copy.sentence}</p>
      {why && why !== copy.sentence && <p className="attention-why">{why}</p>}
      <p className={`attention-prevented ${copy.preventedTone}`}>
        <Icon name={copy.preventedTone === "ok" ? "shield" : "search"} />
        <span>{copy.prevented}</span>
      </p>

      {item.halt_kind === "wrong_record" && (item.expected || item.observed) && (
        <div className="compare">
          <div>
            <span>OpenAdapt was given</span>
            <strong className="mono">{item.expected || "Not recorded"}</strong>
          </div>
          <div className="compare-diff">
            <span>The screen showed</span>
            <strong className="mono">{item.observed || "Not recorded"}</strong>
          </div>
        </div>
      )}

      {screen?.ok && screen.data_url && (
        <ScreenCrop
          dataUrl={screen.data_url}
          width={screen.width ?? 0}
          height={screen.height ?? 0}
          focus={screen.focus}
          caption="The screen when OpenAdapt stopped"
          provenance="Saved on this computer only"
        />
      )}

      {needsCheck && (
        <label className="record-check">
          <input
            type="checkbox"
            checked={recordChecked}
            onChange={(event) => setRecordChecked(event.target.checked)}
          />
          <span>I looked at the record in the app.</span>
        </label>
      )}

      <div className="choices" role="group" aria-label="What should OpenAdapt do?">
        {copy.choices.map((choice) => (
          <ChoiceButton
            key={choice.action}
            tone={choice.primary ? "primary" : "default"}
            title={busy === choice.action ? "Working…" : choice.title}
            sub={choice.sub}
            disabled={busy !== null || (choice.needsRecordCheck === true && !recordChecked)}
            onClick={() => onResolve(choice.action, recordChecked)}
          />
        ))}
      </div>

      <TechnicalDetails>
        <dl className="technical-list">
          <dt>Result</dt>
          <dd>{plain ? resultCopy(plain).label : "Not recorded"}</dd>
          <dt>Execution outcome</dt>
          <dd className="mono">{item.execution_outcome || "none"}</dd>
          <dt>Transaction outcome</dt>
          <dd className="mono">{item.transaction_outcome || "none"}</dd>
          {item.approval && (
            <>
              <dt>Approval mode</dt>
              <dd className="mono">
                {[
                  item.approval.mode ?? "unknown",
                  item.approval.decision ?? "unknown",
                  item.approval.reason,
                  item.approval.failed_check
                    ? `${item.approval.failed_check.code}:${item.approval.failed_check.detail}`
                    : null,
                  item.approval.switch_off_reason
                    ? `switch_off:${item.approval.switch_off_reason}`
                    : null,
                ]
                  .filter(Boolean)
                  .join(" · ")}
              </dd>
            </>
          )}
          {item.halt_step && (
            <>
              <dt>Stopped at</dt>
              <dd className="mono">{item.halt_step}</dd>
            </>
          )}
          {item.halt_reason && (
            <>
              <dt>Engine reason</dt>
              <dd>{item.halt_reason}</dd>
            </>
          )}
          {item.run_id && (
            <>
              <dt>Run</dt>
              <dd className="mono">{item.run_id}</dd>
            </>
          )}
        </dl>
      </TechnicalDetails>
    </article>
  );
}

export function NeedsYou({
  onTeach,
  onChanged,
}: {
  onTeach: (workflowId: string) => void;
  onChanged: () => void;
}) {
  const [items, setItems] = useState<AttentionItem[] | null>(null);
  const [switches, setSwitches] = useState<SwitchSummary[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [busy, setBusy] = useState<AttentionAction | null>(null);
  const [notice, setNotice] = useState<Notice | null>(null);

  async function refresh(): Promise<AttentionItem[]> {
    const response = await engineTry<{
      ok: boolean;
      items: AttentionItem[];
      switches?: SwitchSummary[];
    }>(CMD.LIST_ATTENTION_ITEMS, {}, { ok: false, items: [] });
    const next = response.items ?? [];
    setItems(next);
    setSwitches(response.switches ?? []);
    setSelectedId((current) =>
      current && next.some((item) => item.item_id === current)
        ? current
        : (next[0]?.item_id ?? null),
    );
    return next;
  }

  useEffect(() => {
    void refresh();
  }, []);

  const selected = items?.find((item) => item.item_id === selectedId) ?? null;

  async function resolve(item: AttentionItem, action: AttentionAction, recordChecked: boolean) {
    setBusy(action);
    setNotice(null);
    try {
      const extra: Record<string, unknown> = {};
      if (action === "retry" || action === "retry_after_check") {
        const inputs = await engineTry<TaskInputs>(
          CMD.GET_TASK_INPUTS,
          { workflow_id: item.workflow_id },
          { ok: false, workflow_id: item.workflow_id },
        );
        if (inputs.target) extra.target = inputs.target;
      }
      const response = await engineInvoke<RunItemResponse & { resolved?: unknown }>(
        CMD.RESOLVE_ATTENTION_ITEM,
        {
          item_id: item.item_id,
          action,
          record_checked: recordChecked,
          ...extra,
        },
      );
      if (!response.ok) {
        setNotice({
          tone: "warn",
          text: response.error || "OpenAdapt couldn't do that. Nothing changed.",
        });
        return;
      }
      if (action === "teach") {
        onTeach(item.workflow_id);
        return;
      }
      if (response.item) {
        const copy = resultCopy(
          plainResult(response.item.execution_outcome, response.item.transaction_outcome ?? null, {
            preActionRefusal: response.item.result === "did_not_start",
          }),
        );
        setNotice({
          tone: copy.tone === "ok" ? "ok" : "warn",
          text: `Ran it again: ${copy.label}. ${copy.sentence}`,
        });
      } else {
        // A person closed the item; nothing was checked, so no green.
        setNotice({ tone: "neutral", text: "Done. This item is closed." });
      }
      await refresh();
      onChanged();
    } catch (error) {
      setNotice({ tone: "warn", text: String(error) });
    } finally {
      setBusy(null);
    }
  }

  const onlyProblems = switches.some((entry) => entry.state === "on");
  const switchedOff = switches.filter(
    (entry) => entry.state === "off" && offAutomatically(entry.off_reason),
  );

  return (
    <div className="content simple wide">
      <PageHead
        title="Needs you"
        sub={
          onlyProblems
            ? `With “${SWITCH_LABEL}” on, only problems come here. Items that passed every check were saved and read back from the record without you.`
            : "OpenAdapt stopped on these items and is waiting for a person. None of them runs again until you decide."
        }
      />
      {switchedOff.map((entry) => (
        <div className="attention-banner switch-banner" role="status" key={entry.workflow_id}>
          <Icon name="attention" />
          <span>
            <strong>
              {SWITCH_LABEL} turned off{entry.task_name ? ` for ${entry.task_name}` : ""}.
            </strong>{" "}
            {offReasonSentence(entry.off_reason, { step: entry.off_step })} An admin
            can turn it on again in OpenAdapt Cloud once the requirements are met.
          </span>
        </div>
      ))}
      {notice && (
        <div className={`notice ${notice.tone}`} role="status">
          <Icon name={notice.tone === "warn" ? "attention" : "check"} />
          <span>{notice.text}</span>
        </div>
      )}
      {items === null ? (
        <p className="simple-sub">Loading…</p>
      ) : items.length === 0 ? (
        <div className="calm-empty">
          <Status tone="neutral" icon="check">
            Nothing needs you right now
          </Status>
          <p>
            When OpenAdapt stops on an item, it shows up here with the screen it
            saw and one question to answer.
          </p>
        </div>
      ) : (
        <div className="inbox">
          <nav className="inbox-list" aria-label="Items waiting for you">
            {items.map((item) => (
              <ItemRow
                key={item.item_id}
                item={item}
                selected={item.item_id === selectedId}
                onSelect={() => setSelectedId(item.item_id)}
              />
            ))}
          </nav>
          <div className="inbox-detail">
            {selected ? (
              <AttentionCard
                key={selected.item_id}
                item={selected}
                busy={busy}
                onResolve={(action, checked) => void resolve(selected, action, checked)}
              />
            ) : (
              <Callout title="Choose an item">Pick an item on the left.</Callout>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
