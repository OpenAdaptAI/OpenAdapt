// What a Seal page says about one run in words: its tone, its label, one line,
// and a plain reason for a stop. Read from the run's Seal, its report and the
// rings (./sealRings.mjs) only: outcomes, counts and the engine's halt text,
// never a recorded value. openadapt.ai's card wording (data/site/sealChecks.mjs
// cardFor) and stop reasons (lib/receiptSummary.mjs stopReason), unchanged.

/** The card for a run, from its Seal's outcome and the report's transaction outcome. */
export function cardFor(outcome, transaction, { readBack = false } = {}) {
    if (outcome === 'VERIFIED' && readBack) {
        return {
            tone: 'ok',
            label: 'Saved and checked',
            line: 'The entry reached the record, and reading the record back found it there.',
        }
    }
    if (outcome === 'VERIFIED') {
        return {
            tone: 'ok',
            label: 'Completed',
            line: "Every step's checks passed. This run didn't read the saved record back.",
        }
    }
    if (transaction === 'HALTED_BEFORE_EFFECT') {
        return {
            tone: 'halt',
            label: 'Stopped before saving',
            line: 'The run stopped before it changed anything, so a person decides what happens next.',
        }
    }
    if (transaction === 'RECONCILIATION_REQUIRED') {
        return {
            tone: 'halt',
            label: 'Stopped for a person to check',
            line: "The run stopped after a save it couldn't confirm, so a person checks the record before anything else runs.",
        }
    }
    return {
        tone: 'halt',
        label: 'Stopped',
        line: 'The run stopped, so a person decides what happens next.',
    }
}

// A plain reason for a stop, from the engine's halt text. Unknown stops keep
// the outcome's own wording.
const STOP_REASONS = [
    [/duplicate|already exists|1 of 1|existing (?:chart|record|patient)|No matches were found/i, 'possible duplicate patient'],
    [/0 records match|never reached the record|missing \/ phantom/i, 'the save never reached the record'],
    [/identity|wrong (?:record|patient)/i, "couldn't confirm the right record"],
]

// What the replay says at the stop, for each plain reason.
const STOP_NOTES = {
    'possible duplicate patient':
        'The screen check before saving expected the app to find no matching patient. It found one, so the run stopped before it changed anything.',
    'the save never reached the record':
        "The app showed its saved message, but reading the record back found nothing there, so the run stopped instead of reporting success.",
    "couldn't confirm the right record":
        "The run couldn't confirm it was on the right record, so it stopped before acting.",
}

export function stopReason(report) {
    const text = [report?.halt?.reason, report?.halt?.intent].filter(Boolean).join(' ')
    const hit = STOP_REASONS.find(([re]) => re.test(text))
    return hit ? hit[1] : null
}

/** { tone, label, line, headline, reason, note } for one run. */
export function sealSummary(report, seal, rings) {
    const card = cardFor(seal?.outcome, report?.transaction_outcome, {
        readBack: Boolean(rings?.counts?.readbacksPassed),
    })
    const reason = seal?.outcome === 'VERIFIED' ? null : stopReason(report)
    return {
        ...card,
        headline: reason ? `${card.label}: ${reason}` : card.label,
        reason,
        note: reason ? STOP_NOTES[reason] || null : null,
    }
}
