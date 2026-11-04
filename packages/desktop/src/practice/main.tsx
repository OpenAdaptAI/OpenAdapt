// Stop queue practice: the real Review stops screen on recorded soak stops.
//
// Dev-only (not in the app bundle). Serve the package with `npx vite` and open
// /benchmark/stop_queue/practice.html. Cards come from the drill file that
// benchmark/stop_queue/build_drill.py writes. Nothing is signed or sent: each
// answer is kept in this page with the seconds its card was on screen.
//
// The score is accuracy only, with the hard cards called out (a possible
// match that is a different person, a save that went in wrong). Time is
// recorded for the measurement and never scored or ranked.
import React, { useMemo, useState } from "react";
import ReactDOM from "react-dom/client";
import "@openadapt/program-graph/styles.css";
import "../styles/app.css";
import { Stops, type StopBackend } from "../screens/Stops";
import type { StopCard } from "../lib/types";

interface DrillStop {
  item_id: string;
  card: StopCard;
  expected: string[];
  hard: boolean;
  exception: string;
}

interface Answer {
  item_id: string;
  kind: string;
  choice: string;
  expected: string[];
  correct: boolean;
  hard: boolean;
  shown_ms: number;
  batch: number;
  answered_at: string;
}

const DRILL_URL = new URLSearchParams(location.search).get("drill") ??
  "/benchmark/stop_queue/drill-2026-10-24.json";

function Results({ answers, stops }: { answers: Answer[]; stops: DrillStop[] }) {
  const correct = answers.filter((a) => a.correct).length;
  const hard = answers.filter((a) => a.hard);
  const caught = hard.filter((a) => a.correct).length;
  const misses = answers.filter((a) => !a.correct);
  const blob = useMemo(
    () =>
      URL.createObjectURL(
        new Blob(
          [
            JSON.stringify(
              {
                schema: "openadapt.stop-queue-practice/v1",
                drill: DRILL_URL,
                finished_at: new Date().toISOString(),
                answers,
              },
              null,
              2,
            ),
          ],
          { type: "application/json" },
        ),
      ),
    [answers],
  );
  return (
    <div className="content simple">
      <header className="simple-head">
        <div>
          <h1>
            {correct} of {stops.length} right
          </h1>
          <p className="simple-sub">
            You caught {caught} of {hard.length} hard stops: a possible match that was a
            different person, or a save that went in wrong.
          </p>
        </div>
      </header>
      {misses.length > 0 && (
        <section>
          <h3>To look at again</h3>
          <ul>
            {misses.map((a) => (
              <li key={a.item_id}>
                {a.item_id}: you chose "{a.choice}"; right was {a.expected.join(" or ")}.
              </li>
            ))}
          </ul>
        </section>
      )}
      <p>
        <a className="btn btn-primary" href={blob} download="stop-queue-practice.json">
          Download the results
        </a>
      </p>
      <p className="fine-print">
        The file holds each answer and how long its card was on screen, for the
        seconds-per-stop measurement.
      </p>
    </div>
  );
}

function Practice() {
  const [stops, setStops] = useState<DrillStop[] | null>(null);
  const [answers, setAnswers] = useState<Answer[]>([]);
  const [error, setError] = useState("");

  React.useEffect(() => {
    fetch(DRILL_URL)
      .then((r) => r.json())
      .then((drill) => setStops(drill.stops as DrillStop[]))
      .catch((e) => setError(String(e)));
  }, []);

  const backend = useMemo<StopBackend | null>(() => {
    if (!stops) return null;
    const answered = new Set<string>();
    const byCard = new Map(stops.map((s) => [s.card.card_id, s]));
    return {
      list: async () => ({
        ok: true,
        cards: stops.filter((s) => !answered.has(s.card.card_id)).map((s) => s.card),
        approver_key: { ready: true, in_app: true, touch_id: true },
      }),
      answer: async ({ cardIds, choice, shownMs }) => {
        if (choice === "leave") return { ok: true, changed: 0 };
        const now = new Date().toISOString();
        const recorded = cardIds.map((id) => {
          const stop = byCard.get(id) as DrillStop;
          answered.add(id);
          return {
            item_id: stop.item_id,
            kind: stop.card.kind,
            choice,
            expected: stop.expected,
            correct: stop.expected.includes(choice),
            hard: stop.hard,
            shown_ms: shownMs[id] ?? 0,
            batch: cardIds.length,
            answered_at: now,
          };
        });
        setAnswers((current) => [...current, ...recorded]);
        return { ok: true, changed: cardIds.length };
      },
    };
  }, [stops]);

  if (error) return <p className="notice warn">The drill didn't load: {error}</p>;
  if (!stops || !backend) return <p className="simple-sub">Loading the drill…</p>;
  if (answers.length >= stops.length) return <Results answers={answers} stops={stops} />;
  return <Stops backend={backend} title="Practice: review stops" />;
}

ReactDOM.createRoot(document.getElementById("root") as HTMLElement).render(
  <React.StrictMode>
    <Practice />
  </React.StrictMode>,
);
