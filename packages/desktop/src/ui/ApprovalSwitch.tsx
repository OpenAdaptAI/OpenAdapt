// The switch "Only ask a person about problems" on a task card.
//
// Read-only, plus Turn off. Turning it on happens in OpenAdapt Cloud, by an
// owner or admin, and the clinic's privacy contact confirms. The panel shows
// the evidence behind the state: who turned it on and confirmed, what the
// switch is bound to, the local switch log, and the requirements.
import { useId, useState } from "react";
import {
  SWITCH_LABEL,
  SWITCH_SENTENCE,
  blockerWords,
  notAvailableLine,
  offAutomatically,
  offReasonSentence,
  requirementNext,
  requirementTitle,
  shortDate,
  switchStatus,
  turnedOnLine,
} from "../lib/approvalMode";
import type { ApprovalModeState } from "../lib/types";
import { Button } from "./primitives";
import { Status, plural } from "./simple";

function shortDigest(value: string | undefined): string {
  if (!value) return "";
  const hex = value.replace(/^sha256:/, "");
  return hex.slice(0, 12);
}

function Evidence({ mode }: { mode: ApprovalModeState }) {
  const evidence = mode.evidence;
  const requirements = mode.requirements;
  return (
    <details className="technical switch-evidence">
      <summary>{evidence ? "Evidence" : "What it needs"}</summary>
      <div className="technical-body">
        <p className="switch-sentence">{SWITCH_SENTENCE}</p>
        {evidence && (
          <dl className="technical-list">
            <dt>Turned on by</dt>
            <dd>
              {mode.turned_on
                ? `${mode.turned_on.name}, ${mode.turned_on.role === "owner" ? "owner" : "admin"}, ${shortDate(mode.turned_on.at)}`
                : "Not recorded"}
            </dd>
            <dt>Confirmed by</dt>
            <dd>
              {mode.confirmed
                ? `${mode.confirmed.name}, privacy contact, ${shortDate(mode.confirmed.at)}`
                : "Not recorded"}
            </dd>
            <dt>Version</dt>
            <dd className="mono">{shortDigest(evidence.bundle_digest)}</dd>
            <dt>Runner</dt>
            <dd className="mono">{evidence.runner_id}</dd>
            <dt>Record check</dt>
            <dd>
              {plural(evidence.verifiers, "bound verifier")}, read from your records system
            </dd>
            <dt>Switch log</dt>
            <dd>
              {evidence.log_intact ? "Intact" : "Broken"} on this computer,{" "}
              {plural(evidence.log_events, "event")}.{" "}
              {plural(evidence.items_without_person, "item")} saved without a person.
            </dd>
            <dt>Switch</dt>
            <dd className="mono">{evidence.switch_id}</dd>
          </dl>
        )}
        {mode.blockers.length > 0 && (
          <>
            <h4 className="switch-subhead">Missing on this computer</h4>
            <ul className="switch-list">
              {mode.blockers.map((code) => (
                <li key={code}>{blockerWords(code)}</li>
              ))}
            </ul>
          </>
        )}
        {requirements?.available && (
          <>
            <h4 className="switch-subhead">Requirements</h4>
            <ul className="switch-list switch-requirements">
              {requirements.items.map((item) => (
                <li key={item.code}>
                  <Status
                    tone={item.met ? "ok" : "neutral"}
                    icon={item.met ? "check" : "ring"}
                  >
                    {item.met ? "Met" : "Not confirmed here"}
                  </Status>
                  <span>
                    <strong>{requirementTitle(item.code)}.</strong>{" "}
                    {item.met ? "" : requirementNext(item)}
                    {item.checked_by === "person" && (
                      <em className="faint"> A person at the clinic decides this.</em>
                    )}
                    {item.checked_by === "software_and_person" && (
                      <em className="faint"> Part of this is a person's decision.</em>
                    )}
                  </span>
                </li>
              ))}
            </ul>
            {requirements.items.some((item) => !item.met) && (
              <p className="fine-print">
                Checked on this computer. OpenAdapt Cloud holds the admission, the
                runner setup and the people's decisions, so a requirement can be met
                there and still read “not confirmed here”.
              </p>
            )}
          </>
        )}
      </div>
    </details>
  );
}

export function ApprovalSwitch({
  mode,
  busy = false,
  onTurnOff,
  onOpenCloud,
}: {
  mode: ApprovalModeState;
  busy?: boolean;
  onTurnOff: () => void;
  onOpenCloud: () => void;
}) {
  const labelId = useId();
  const [confirming, setConfirming] = useState(false);
  const status = switchStatus(mode.state);
  const line = turnedOnLine(mode);

  let body: string;
  let note: string | null = null;
  switch (mode.state) {
    case "on":
      body = line;
      break;
    case "waiting_for_cloud":
      body = "Items wait for a person until OpenAdapt Cloud confirms the switch.";
      note = line;
      break;
    case "not_used_here":
      body = `On in OpenAdapt Cloud, but not used on this computer: ${
        mode.blockers[0] ? blockerWords(mode.blockers[0]) : "a piece it needs is missing."
      } This computer runs items the same way it did before the switch was turned on.`;
      note = line;
      break;
    case "off":
      body = offReasonSentence(mode.off_reason, {
        step: mode.off_step,
        byThisComputer: mode.off_by_this_computer,
      });
      note = offAutomatically(mode.off_reason)
        ? "An admin can turn it on again in OpenAdapt Cloud once the requirements are met."
        : "Turn it on again in OpenAdapt Cloud.";
      break;
    default:
      body = notAvailableLine(mode);
      note = "Turn it on in OpenAdapt Cloud.";
  }

  return (
    <section className="switch-panel" aria-labelledby={labelId}>
      <div className="switch-head">
        <h3 id={labelId}>{SWITCH_LABEL}</h3>
        <Status tone={status.tone} icon={status.icon}>
          {status.word}
        </Status>
      </div>
      <p className="switch-body">{body}</p>
      {note && <p className="switch-note">{note}</p>}
      {confirming ? (
        <div className="switch-confirm" role="group" aria-label="Turn off the switch">
          <p>
            Turn it off for this workflow? It applies before the next save, even
            offline. Turning it on again needs an admin and the privacy contact
            in OpenAdapt Cloud, and new items approved one by one.
          </p>
          <div className="row">
            <Button
              size="sm"
              variant="danger"
              disabled={busy}
              onClick={() => {
                setConfirming(false);
                onTurnOff();
              }}
            >
              {busy ? "Turning off…" : "Turn it off"}
            </Button>
            <Button size="sm" disabled={busy} onClick={() => setConfirming(false)}>
              Keep it on
            </Button>
          </div>
        </div>
      ) : (
        <div className="switch-actions">
          {mode.can_turn_off ? (
            <Button size="sm" disabled={busy} onClick={() => setConfirming(true)}>
              Turn off
            </Button>
          ) : (
            <Button size="sm" onClick={onOpenCloud}>
              Open Cloud
            </Button>
          )}
        </div>
      )}
      <Evidence mode={mode} />
    </section>
  );
}
