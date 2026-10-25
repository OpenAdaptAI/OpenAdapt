// Review stops: every stop as one card, answered with one key or tap.
//
// Each card shows the question, the item's own values next to the record or
// the form (differences marked), and the choices with what each one does. The
// engine rebuilds every card before it acts: a card that changed, expired or
// doesn't offer the choice changes nothing. Answers are signed with this
// computer's approver key (Touch ID on a Mac; one prompt per batch).
//
// Keys: 1-9 answer the current card, J/K or the arrow keys move, X adds the
// card to a batch of similar stops, Shift+1-9 answers the whole batch, Esc
// clears the batch, ? shows the keys.
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { CMD, engineInvoke, engineTry } from "../lib/engine";
import type {
  AttentionScreenshot,
  ItemApprovalState,
  StopCard,
  StopCardsResponse,
  StopAnswer,
} from "../lib/types";
import { ApproverKeySetup } from "../ui/ApproverKey";
import { Callout } from "../ui/primitives";
import { ChoiceButton, Icon, PageHead, ScreenCrop, TechnicalDetails } from "../ui/simple";
import { fieldLabel } from "./NeedsYou";

const POLL_MS = 3000;

interface Shot {
  dataUrl: string;
  width: number;
  height: number;
  caption: string;
}

function useScreenshot(card: StopCard | null): Shot | null {
  const [shot, setShot] = useState<Shot | null>(null);
  const ref = card?.evidence.screenshot ?? null;
  const key = ref ? JSON.stringify(ref) : "";
  useEffect(() => {
    let live = true;
    setShot(null);
    if (!ref) return;
    void (async () => {
      if ("item_id" in ref) {
        const frame = await engineTry<AttentionScreenshot>(
          CMD.GET_ATTENTION_SCREENSHOT,
          { item_id: ref.item_id },
          { ok: false },
        );
        if (live && frame.ok && frame.data_url) {
          setShot({
            dataUrl: frame.data_url,
            width: frame.width ?? 0,
            height: frame.height ?? 0,
            caption: "Where the run stopped",
          });
        }
      } else if (card) {
        const state = await engineTry<ItemApprovalState | null>(
          CMD.GET_ITEM_APPROVAL,
          { workflow_id: card.workflow_id },
          null,
        );
        const url = state?.request?.request_id === ref.approval ? state.request.screenshot : null;
        if (live && url) {
          setShot({ dataUrl: url, width: 0, height: 0, caption: "The form, before saving" });
        }
      }
    })();
    return () => {
      live = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);
  return shot;
}

function Screen({ card }: { card: StopCard }) {
  const shot = useScreenshot(card);
  if (!shot) {
    return card.evidence.screenshot ? <p className="simple-sub">Loading the screen…</p> : null;
  }
  return shot.width > 0 ? (
    <ScreenCrop
      dataUrl={shot.dataUrl}
      width={shot.width}
      height={shot.height}
      caption={shot.caption}
      region="top"
    />
  ) : (
    <img className="stop-shot" src={shot.dataUrl} alt={shot.caption} />
  );
}

/** The item next to each possible match, one row per field, differences marked. */
function Comparison({ card }: { card: StopCard }) {
  const matches = card.evidence.candidates.filter((c) => c.fields.length);
  const compared = new Set(matches.flatMap((c) => c.fields.map((f) => f.field)));
  const rows = card.evidence.source.filter((row) => compared.has(row.field));
  const rest = card.evidence.source.filter((row) => !compared.has(row.field));
  return (
    <section className="stop-side" aria-label="The item next to the possible matches">
      <table className="stop-compare">
        <thead>
          <tr>
            <th scope="col">Field</th>
            <th scope="col">The item</th>
            {matches.map((_, i) => (
              <th scope="col" key={i}>
                {matches.length > 1 ? `Possible match ${i + 1}` : "Possible match in the app"}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.field}>
              <th scope="row">{fieldLabel(row.field)}</th>
              <td>{row.value === "" ? <em>Empty</em> : row.value}</td>
              {matches.map((match, i) => {
                const cell = match.fields.find((f) => f.field === row.field);
                if (!cell) return <td key={i} />;
                return (
                  <td key={i} className={cell.same ? "stop-same" : "stop-differs"}>
                    {cell.value === "" ? <em>Empty</em> : cell.value}
                    <span className="stop-record-value">{cell.same ? "Same" : "Different"}</span>
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
      {rest.length > 0 && (
        <details className="technical">
          <summary>The item's other values ({rest.length})</summary>
          <dl className="technical-body">
            {rest.map((row) => (
              <div key={row.field}>
                <dt>{fieldLabel(row.field)}</dt>
                <dd>{row.value === "" ? <em>Left empty</em> : row.value}</dd>
              </div>
            ))}
          </dl>
        </details>
      )}
    </section>
  );
}

function Evidence({ card }: { card: StopCard }) {
  const structured = card.evidence.candidates.some((c) => c.fields.length);
  const differences = new Map(card.evidence.differences.map((d) => [d.field, d]));
  return (
    <div className="stop-evidence">
      {structured ? (
        <Comparison card={card} />
      ) : (
        <section className="stop-side" aria-label="The item">
          <h3>The item</h3>
          {card.evidence.source.length ? (
            <dl>
              {card.evidence.source.map((row) => {
                const diff = differences.get(row.field);
                const differs = diff ? !diff.same : false;
                return (
                  <div key={row.field} className={differs ? "stop-differs" : undefined}>
                    <dt>{fieldLabel(row.field)}</dt>
                    <dd>
                      {row.value === "" ? <em>Left empty</em> : row.value}
                      {diff && (
                        <span className="stop-record-value">
                          {differs ? "Record: " : "Record matches: "}
                          <strong>{diff.record === "" ? "empty" : diff.record}</strong>
                        </span>
                      )}
                    </dd>
                  </div>
                );
              })}
            </dl>
          ) : (
            <p className="simple-sub">No values to compare.</p>
          )}
        </section>
      )}
      {(!structured || card.evidence.screenshot) && (
        <section className="stop-side" aria-label="What the app shows">
          <h3>{card.kind === "approve_save" ? "The form" : "What the app shows"}</h3>
          {card.evidence.candidates
            .filter((c) => !c.fields.length)
            .map((c, i) => (
              <p key={i} className="stop-candidate-text">
                {c.text}
              </p>
            ))}
          <Screen card={card} />
        </section>
      )}
      {card.evidence.reason && (
        <TechnicalDetails>
          <p className="fine-print stop-reason">{card.evidence.reason}</p>
        </TechnicalDetails>
      )}
    </div>
  );
}

/** Where the queue reads cards and sends answers: the engine, or a practice set. */
export interface StopBackend {
  list(): Promise<StopCardsResponse>;
  answer(request: {
    cardIds: string[];
    choice: string;
    shownMs: Record<string, number>;
    approval: boolean;
  }): Promise<StopAnswer>;
}

export const ENGINE_BACKEND: StopBackend = {
  list: () =>
    engineTry<StopCardsResponse>(CMD.LIST_STOP_CARDS, {}, { ok: false, cards: [] }),
  answer: ({ cardIds, choice, shownMs, approval }) =>
    approval
      ? engineInvoke<StopAnswer>(CMD.ANSWER_STOP_APPROVAL, {
          card_id: cardIds[0],
          choice,
          shown_ms: shownMs,
        })
      : engineInvoke<StopAnswer>(CMD.ANSWER_STOP_CARDS, {
          card_ids: cardIds,
          choice,
          shown_ms: shownMs,
        }),
};

export function Stops({
  onDone,
  backend = ENGINE_BACKEND,
  title = "Review stops",
}: {
  onDone?: () => void;
  backend?: StopBackend;
  title?: string;
}) {
  const [cards, setCards] = useState<StopCard[] | null>(null);
  const [keyReady, setKeyReady] = useState<StopCardsResponse["approver_key"] | null>(null);
  const [index, setIndex] = useState(0);
  const [batch, setBatch] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [help, setHelp] = useState(false);
  // When each card was first shown, for the seconds-per-stop measure.
  const shownAt = useRef(new Map<string, number>());

  const refresh = useCallback(async () => {
    const response = await backend.list();
    if (!response.ok) return;
    setCards(response.cards);
    setKeyReady(response.approver_key ?? null);
    const ids = new Set(response.cards.map((c) => c.card_id));
    setBatch((current) => current.filter((id) => ids.has(id)));
  }, [backend]);

  useEffect(() => {
    void refresh();
    const timer = setInterval(() => void refresh(), POLL_MS);
    return () => clearInterval(timer);
  }, [refresh]);

  const list = useMemo(() => cards ?? [], [cards]);
  const current = list[Math.min(index, Math.max(0, list.length - 1))] ?? null;
  useEffect(() => {
    if (current && !shownAt.current.has(current.card_id)) {
      shownAt.current.set(current.card_id, Date.now());
    }
  }, [current]);

  const similar = useMemo(
    () =>
      current?.batch_key
        ? list.filter((c) => c.batch_key === current.batch_key)
        : [],
    [list, current],
  );

  const answer = useCallback(
    async (choiceId: string, ids: string[]) => {
      if (busy || !ids.length) return;
      const targets = list.filter((c) => ids.includes(c.card_id));
      if (targets.length !== ids.length) return;
      setBusy(true);
      setError("");
      setNotice("");
      const now = Date.now();
      const shown_ms = Object.fromEntries(
        ids.map((id) => [id, now - (shownAt.current.get(id) ?? now)]),
      );
      try {
        const approval = targets.length === 1 && targets[0].kind === "approve_save";
        const response = await backend.answer({
          cardIds: ids,
          choice: choiceId,
          shownMs: shown_ms,
          approval,
        });
        if (!response.ok) {
          setError(response.error || "Nothing changed.");
        } else if (choiceId !== "leave") {
          setNotice(
            ids.length > 1 ? `Answered ${response.changed ?? ids.length} stops.` : "Answered.",
          );
          setBatch([]);
        } else {
          setIndex((i) => i + 1);
        }
      } catch (failure) {
        setError(`Nothing changed. (${String(failure)})`);
      } finally {
        setBusy(false);
        await refresh();
      }
    },
    [backend, busy, list, refresh],
  );

  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      const target = event.target as HTMLElement | null;
      if (target && /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName)) return;
      if (!current) return;
      if (event.key === "?") {
        setHelp((h) => !h);
        return;
      }
      if (event.key === "j" || event.key === "ArrowDown") {
        setIndex((i) => Math.min(i + 1, list.length - 1));
        return;
      }
      if (event.key === "k" || event.key === "ArrowUp") {
        setIndex((i) => Math.max(i - 1, 0));
        return;
      }
      if (event.key === "Escape") {
        setBatch([]);
        return;
      }
      if (event.key === "x" && current.batch_key) {
        setBatch((ids) =>
          ids.includes(current.card_id)
            ? ids.filter((id) => id !== current.card_id)
            : ids.length && list.find((c) => c.card_id === ids[0])?.batch_key !== current.batch_key
              ? [current.card_id]
              : [...ids, current.card_id],
        );
        return;
      }
      // Shift+digit arrives as "!", "@" ... on most layouts; read the code.
      const digit = /^Digit([1-9])$/.exec(event.code)?.[1];
      if (!digit) return;
      const choice = current.choices[Number(digit) - 1];
      if (!choice) return;
      event.preventDefault();
      if (event.shiftKey && batch.length) {
        void answer(choice.id, batch);
      } else {
        void answer(choice.id, [current.card_id]);
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [answer, batch, current, list]);

  if (cards === null) {
    return (
      <div className="content simple">
        <PageHead title={title} sub="Reading the stops…" />
      </div>
    );
  }

  return (
    <div className="content simple stops">
      <PageHead
        title={title}
        sub={
          list.length
            ? `${list.length} waiting. Answer each with one key: 1 to ${Math.max(
                ...list.map((c) => c.choices.length),
              )}. Press ? for all keys.`
            : "Nothing is waiting for you."
        }
        action={
          onDone ? (
            <button type="button" className="btn btn-ghost" onClick={onDone}>
              Back to Needs you
            </button>
          ) : undefined
        }
      />
      {keyReady && !keyReady.ready && list.length > 0 && (
        keyReady.in_app ? (
          <ApproverKeySetup onReady={refresh} />
        ) : (
          <Callout tone="warn" title="Answers need your approver key">
            Set it up once in Settings &gt; Approver key. Until then, nothing here changes.
          </Callout>
        )
      )}
      {help && (
        <Callout tone="info" title="Keys">
          1-9 answer this stop. J or ↓ next, K or ↑ previous. X adds this stop to a batch
          of similar stops; Shift+1-9 answers the batch with one Touch ID. Esc clears the
          batch.
        </Callout>
      )}
      {notice && (
        <p className="notice ok" role="status">
          <Icon name="check" />
          <span>{notice}</span>
        </p>
      )}
      {error && (
        <p className="notice warn" role="alert">
          <Icon name="attention" />
          <span>{error}</span>
        </p>
      )}
      {current && (
        <article
          className="stop-card"
          aria-labelledby="stop-question"
          data-kind={current.kind}
        >
          <header className="stop-head">
            <p className="eyebrow">
              {index + 1} of {list.length}
              {current.task_name ? ` · ${current.task_name}` : ""}
              {current.label ? ` · ${current.label}` : ""}
            </p>
            <h2 id="stop-question">{current.question}</h2>
            <p className="simple-sub">{current.summary}</p>
          </header>
          <Evidence card={current} />
          <div className="stop-choices">
            {current.choices.map((choice, i) => (
              <ChoiceButton
                key={choice.id}
                title={`${i + 1}. ${choice.label}`}
                sub={choice.consequence}
                // No choice is styled as the default: the evidence decides.
                disabled={busy}
                onClick={() => void answer(choice.id, [current.card_id])}
              />
            ))}
          </div>
          {similar.length > 1 && (
            <section className="stop-batch" aria-label="Similar stops">
              <h3>
                {similar.length} similar stops
                {batch.length ? ` · ${batch.length} in this batch` : ""}
              </h3>
              <p className="fine-print">
                Check each one before you add it. A batch is answered with one choice and
                one Touch ID, and each stop is still checked on its own.
              </p>
              <ul>
                {similar.map((card) => (
                  <li key={card.card_id}>
                    <label className="record-check">
                      <input
                        type="checkbox"
                        checked={batch.includes(card.card_id)}
                        onChange={(event) =>
                          setBatch((ids) =>
                            event.target.checked
                              ? [...ids, card.card_id]
                              : ids.filter((id) => id !== card.card_id),
                          )
                        }
                      />
                      <span>
                        {card.label || card.source.id}
                        {": "}
                        {card.evidence.source
                          .slice(0, 3)
                          .map((row) => row.value)
                          .join(", ")}
                      </span>
                    </label>
                  </li>
                ))}
              </ul>
              {batch.length > 1 && (
                <div className="check-actions">
                  {current.choices
                    .filter((choice) => choice.id !== "leave")
                    .map((choice, i) => (
                      <button
                        key={choice.id}
                        type="button"
                        className="btn btn-ghost"
                        disabled={busy}
                        onClick={() => void answer(choice.id, batch)}
                      >
                        Shift+{i + 1} {choice.label} ({batch.length})
                      </button>
                    ))}
                </div>
              )}
            </section>
          )}
        </article>
      )}
    </div>
  );
}
