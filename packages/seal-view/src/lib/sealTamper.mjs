// The changes a visitor can make to a copy of a run's files on its Seal page,
// to watch the check fail and see why. Each is one small edit: to the report
// (the check then fails on the report's digest) or to the Seal itself (the
// check then fails on the signature). The published files never change.

const OTHER = { VERIFIED: 'HALTED', HALTED: 'VERIFIED' }

/** The changes offered for one run, from its Seal and report outcome. */
export function tamperOptions(sealOutcome, reportOutcome) {
    const from = reportOutcome || sealOutcome
    const to = OTHER[from] || 'VERIFIED'
    const sealTo = OTHER[sealOutcome] || 'VERIFIED'
    return [
        {
            id: 'report-outcome',
            target: 'report',
            label: 'Change the outcome in the report',
            field: 'execution_outcome',
            from,
            to,
            text: [`"execution_outcome": "${from}"`, `"execution_outcome": "${to}"`],
        },
        {
            id: 'report-model-calls',
            target: 'report',
            label: 'Add one AI model call to the report',
            field: 'model_calls',
            from: '0',
            to: '1',
            text: ['"model_calls": 0', '"model_calls": 1'],
        },
        {
            id: 'seal-outcome',
            target: 'seal',
            label: 'Change the outcome in the Seal',
            field: 'outcome',
            from: sealOutcome,
            to: sealTo,
        },
    ]
}

/**
 * files: { report: Uint8Array, sealText, ... }. Returns a changed copy, or
 * throws when the value to change isn't in the file.
 */
export function applyTamper(files, option) {
    if (option.target === 'seal') {
        const seal = JSON.parse(files.sealText)
        if (seal[option.field] !== option.from)
            throw new Error('The value to change is not in the Seal.')
        return {
            ...files,
            sealText: JSON.stringify({ ...seal, [option.field]: option.to }, null, 2),
        }
    }
    const text = new TextDecoder().decode(files.report)
    const [from, to] = option.text
    if (!text.includes(from))
        throw new Error('The value to change is not in the report.')
    return { ...files, report: new TextEncoder().encode(text.replace(from, to)) }
}
