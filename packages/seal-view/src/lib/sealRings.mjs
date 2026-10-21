// What a Seal page draws for one run, read from that run's signed report and
// nothing else: every mark on the Seal is one thing the report recorded.
//
//   steps      one per program step, in order: passed, stopped (the step the
//              run stopped at) or not reached (after a stop; drawn ghosted)
//   checks     one per screen check the run evaluated
//              (outcome_envelope.postcondition_evidence), on its step
//   readbacks  one per read of the saved record the program requires
//              (governed_qualified_effect_requirements); confirmed only when
//              the save step's own effect evidence says so
//   identity   one per right-record check the program arms
//              (required_identity_step_ids)
//
// A program without governed read-back requirements (the quickstart's, for
// one) gets one read-back per record check its steps evaluated
// (results[i].effect_evidence), on the step that ran it.
//
// Times are the run's own (each step's elapsed_ms, in order), so the page can
// assemble the Seal in the order the run happened. The program's step list and
// names come from the program graph of the same bundle (programOf); a report
// from any other bundle is refused. No typed values, screen text or patient
// data pass through here: field names and counts only.
import { fieldName, stepName } from './programStepNames.mjs'

const FIELD = {
    title: 'Title',
    fname: 'First name',
    lname: 'Last name',
    DOB: 'Date of birth',
    sex: 'Sex',
    street: 'Street',
    city: 'City',
    state: 'State',
    state_label: 'State',
    postal_code: 'Postal code',
    phone_home: 'Home phone',
    email: 'Email',
    country_code: 'Country',
}

const bare = (id) => String(id).replace(/^.*::/, '')

function readbackLabel(text) {
    const field = /field '(\w+)' equals/.exec(text || '')
    if (field) return `${FIELD[field[1]] || fieldName(field[1])} matches`
    if (/record_written/.test(text || '')) return 'The new record exists'
    return null
}

/**
 * program: { digest, steps: [{ id, name, section }] } for the run's bundle.
 * Returns the rings, their counts and the run's timing.
 */
export function sealRings(report, program) {
    if (!report || typeof report !== 'object') throw new Error('no report')
    if (report.bundle_content_digest !== program.digest)
        throw new Error('the report is from a different program')
    const results = Array.isArray(report.results) ? report.results : []
    if (results.length > program.steps.length)
        throw new Error('the report has more steps than the program')
    results.forEach((r, i) => {
        if (bare(r.step_id) !== bare(program.steps[i].id))
            throw new Error(`step ${i + 1}: the report and the program differ`)
    })

    // Each step's start and end, in ms from the first step, from the report.
    let clock = 0
    const timed = results.map((r) => {
        const ms = Math.max(0, Number(r.elapsed_ms) || 0)
        const span = { t0: clock, t1: clock + ms }
        clock += ms
        return span
    })
    const runMs = clock

    const evidence = report.outcome_envelope?.postcondition_evidence || []
    const byStep = new Map()
    for (const e of evidence) {
        const list = byStep.get(e.result_index) || []
        list.push(e)
        byStep.set(e.result_index, list)
    }

    const steps = program.steps.map((p, i) => {
        const r = results[i]
        const status = !r ? 'not_reached' : r.ok ? 'passed' : 'stopped'
        const own = byStep.get(i) || []
        return {
            i,
            id: bare(p.id),
            name: p.name,
            section: p.section,
            status,
            t0: r ? timed[i].t0 : null,
            t1: r ? timed[i].t1 : null,
            checks: {
                passed: own.filter((e) => e.verdict === 'passed').length,
                failed: own.filter((e) => e.verdict !== 'passed').length,
            },
            identity: !r
                ? null
                : r.identity?.status === 'verified'
                  ? 'passed'
                  : r.identity
                    ? 'failed'
                    : null,
        }
    })

    const checks = evidence.map((e) => {
        const s = steps[e.result_index]
        if (!s || s.status === 'not_reached')
            throw new Error('a screen check on a step the run never reached')
        return {
            step: e.result_index,
            result: e.verdict === 'passed' ? 'passed' : 'failed',
            t: s.t1,
        }
    })

    // What the run read back from the record: the governed requirements
    // (each names its step and its place in that step's evidence), else every
    // record check a step evaluated.
    const governed = Array.isArray(report.governed_qualified_effect_requirements)
        ? report.governed_qualified_effect_requirements
        : []
    const required = governed.length
        ? governed.map((req) => ({
              step: steps.findIndex((s) => s.id === bare(req.step_id ?? '')),
              at: Number.isInteger(req.effect_index) ? req.effect_index : -1,
          }))
        : results.flatMap((r, i) =>
              (Array.isArray(r.effect_evidence) ? r.effect_evidence : []).map((_, at) => ({ step: i, at }))
          )
    const readbacks = required.map((req, k) => {
        const save = results[req.step]
        const verdicts = Array.isArray(save?.effect_evidence) ? save.effect_evidence : []
        const texts = Array.isArray(save?.effect_results) ? save.effect_results : []
        // effect_index is the effect's place in its step's own evidence list
        // (the two lists hash their contracts differently).
        const ev = verdicts[req.at]
        const span = save ? timed[req.step] : null
        const onStep = required.filter((x) => x.step === req.step)
        const order = onStep.indexOf(req)
        return {
            i: k,
            step: req.step,
            result: !ev
                ? 'not_reached'
                : ev.final_verdict === 'confirmed'
                  ? 'passed'
                  : 'failed',
            label: (req.at >= 0 && readbackLabel(texts[req.at])) || null,
            t:
                ev && span
                    ? span.t0 +
                      (span.t1 - span.t0) * (0.55 + (0.45 * order) / onStep.length)
                    : null,
        }
    })

    const identityIds = Array.isArray(report.required_identity_step_ids)
        ? report.required_identity_step_ids.map(bare)
        : []
    const identity = identityIds.map((id) => {
        const s = steps.find((x) => x.id === id)
        if (!s) throw new Error(`a right-record check on an unknown step ${id}`)
        return {
            step: s.i,
            result: s.identity ?? 'not_reached',
            t: s.t0 == null ? null : s.t0 + (s.t1 - s.t0) * 0.3,
        }
    })

    const stopped = steps.find((s) => s.status === 'stopped') || null
    const count = (list, result) => list.filter((x) => x.result === result).length
    // The step where the run's outcome was decided, and so where its Seal
    // belongs: the stop, else the save its read-backs prove, else its last step.
    const saved = readbacks.find((r) => r.result === 'passed')
    const lastReached = [...steps].reverse().find((s) => s.status !== 'not_reached')
    const anchor = stopped
        ? { step: stopped.i, kind: 'stop' }
        : saved
          ? { step: saved.step, kind: 'save' }
          : lastReached
            ? { step: lastReached.i, kind: 'end' }
            : null
    return {
        outcome: report.execution_outcome,
        transaction: report.transaction_outcome ?? null,
        runMs,
        totalMs: typeof report.total_ms === 'number' ? report.total_ms : runMs,
        startedAt: report.started_at ?? null,
        stoppedAt: stopped ? stopped.i : null,
        anchor,
        steps,
        checks,
        readbacks,
        identity,
        counts: {
            steps: steps.length,
            stepsPassed: steps.filter((s) => s.status === 'passed').length,
            checks: checks.length,
            checksPassed: count(checks, 'passed'),
            checksFailed: count(checks, 'failed'),
            readbacks: readbacks.length,
            readbacksPassed: count(readbacks, 'passed'),
            readbacksFailed: count(readbacks, 'failed'),
            identity: identity.length,
            identityPassed: count(identity, 'passed'),
            modelCalls: report.model_calls ?? null,
        },
    }
}

/**
 * The program's steps for the rings, from a program graph (the spec that
 * `openadapt-flow visualize --format json` writes for the run's bundle): its
 * action steps in order, each placed in its section when `folds` has one.
 * A step's name is `titles[id]`, else what the run's own report says the step
 * did ("click 'Save Encounter'" reads "Click Save Encounter"), else the
 * graph's title. The digest is the bundle's content digest, which a report
 * must match.
 */
export function programOf(spec, { folds = null, report = null, titles = null, digest = null } = {}) {
    const actions = (spec?.nodes || []).filter((n) => n.kind === 'action')
    const intents = new Map(
        (Array.isArray(report?.results) ? report.results : []).map((r) => [bare(r.step_id), r.intent])
    )
    const sectionOf = new Map()
    for (const g of folds?.groups || []) for (const id of g.nodes) sectionOf.set(id, g.title)
    return {
        digest:
            digest ||
            spec?.bundle?.provenance?.content_digest ||
            folds?.source?.body_content_digest ||
            null,
        steps: actions.map((n) => {
            const intent = intents.get(bare(n.id))
            return {
                id: n.id,
                name: titles?.[n.id] || (intent ? stepName(intent) : n.title),
                section: sectionOf.get(n.id) || null,
            }
        }),
    }
}

/**
 * The same for openadapt.ai's referral program, named from a recorded run of
 * it (its 29 steps' intents, in order).
 */
export function programSteps(spec, folds, run) {
    const actions = spec.nodes.filter((n) => n.kind === 'action')
    if (actions.length !== run.steps.length)
        throw new Error('the run and the program differ in step count')
    const titles = Object.fromEntries(actions.map((n, i) => [n.id, stepName(run.steps[i].intent)]))
    return programOf(spec, { folds, titles, digest: folds.source?.body_content_digest })
}
