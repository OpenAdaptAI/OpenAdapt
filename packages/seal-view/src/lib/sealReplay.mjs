// The replay of one run on its Seal page: the run's steps in order, as a list
// of frames. One index into this list is the page's only playback state; the
// lit ring, the playhead, the "Step X of Y" line, the step inspector, the
// spotlight and the program map all read the frame at that index.
//
// Each frame is a moment of the run: a step starting, its right-record check,
// its screen checks, one read of the saved record, or the stop. Its dwell is
// the step's real duration, compressed and kept readable, and the moments that
// matter most hold longer and are spotlit: the first right-record check before
// acting, the first screen check, the save with each read-back, and the stop.

const STEP_MS = [700, 1500] // a step's frame at 1x: its real time x0.5, within these bounds
const SPOT_MS = { identity: 2600, checks: 2200, save: 1800, readback: 650, stop: 4200, done: 1600 }

/**
 * frames: [{ i, step, stage: 'step' | 'identity' | 'checks' | 'readback' | 'stop' | 'done',
 *            readbacks (how many read-backs are shown), spotlight: null | kind,
 *            highlight (where "Next highlight" stops), dwell }]
 * `stage` order within a step: step < identity < checks < readback.
 */
export function replayFrames(rings) {
    const frames = []
    const push = (f) => frames.push({ i: frames.length, readbacks: 0, spotlight: null, highlight: false, ...f })
    let firstIdentity = true
    let firstChecks = true
    for (const s of rings.steps) {
        if (s.status === 'not_reached') break
        const ms = Math.min(STEP_MS[1], Math.max(STEP_MS[0], ((s.t1 ?? 0) - (s.t0 ?? 0)) * 0.5))
        const reads = rings.readbacks.filter((r) => r.step === s.i && r.result !== 'not_reached')
        const isSave = reads.length > 0
        push({ step: s.i, stage: 'step', dwell: isSave ? SPOT_MS.save : ms, spotlight: isSave ? 'save' : null, highlight: isSave })
        if (s.identity === 'passed' || s.identity === 'failed') {
            const spot = firstIdentity
            firstIdentity = false
            push({ step: s.i, stage: 'identity', dwell: spot ? SPOT_MS.identity : Math.max(350, Math.round(ms * 0.45)), spotlight: spot ? 'identity' : null, highlight: spot })
        }
        if (s.checks.passed + s.checks.failed > 0 && s.status !== 'stopped') {
            const spot = firstChecks
            firstChecks = false
            push({ step: s.i, stage: 'checks', dwell: spot ? SPOT_MS.checks : Math.max(350, Math.round(ms * 0.45)), spotlight: spot ? 'checks' : null, highlight: spot })
        }
        reads.forEach((r, k) =>
            push({ step: s.i, stage: 'readback', readbacks: k + 1, dwell: SPOT_MS.readback, spotlight: 'save' })
        )
        if (s.status === 'stopped') push({ step: s.i, stage: 'stop', dwell: SPOT_MS.stop, spotlight: 'stop', highlight: true })
    }
    const last = frames.at(-1)
    if (last && last.stage !== 'stop') push({ step: last.step, stage: 'done', readbacks: last.readbacks, dwell: SPOT_MS.done })
    return frames
}

const STAGE = { step: 0, identity: 1, checks: 2, readback: 3, stop: 3, done: 4 }

/**
 * What is lit at a frame, for the ring: steps before the current one are done;
 * the current step shows what its stage has reached. Every piece of the Seal
 * asks this one function, so nothing drifts from the frame.
 */
export function litAt(frame, rings) {
    if (!frame) return null
    const stage = STAGE[frame.stage]
    const doneStep = (i) => i < frame.step || (i === frame.step && frame.stage === 'done')
    return {
        step: frame.step,
        stage: frame.stage,
        steps: (i) => i <= frame.step,
        identity: (i) => doneStep(i) || (i === frame.step && stage >= 1),
        checks: (i) => doneStep(i) || (i === frame.step && (stage >= 2 || frame.stage === 'stop')),
        readback: (k) => {
            const r = rings.readbacks[k]
            if (!r || r.step == null) return false
            if (r.step < frame.step) return true
            if (r.step > frame.step) return false
            const order = rings.readbacks.filter((x) => x.step === r.step).indexOf(r)
            return order < frame.readbacks
        },
    }
}

/** The frame a step starts at (seek on hover, click, arrow keys, the map). */
export const frameOfStep = (frames, step) => {
    const f = frames.find((x) => x.step === step)
    if (f) return f.i
    // A step the run never reached: the last frame.
    return frames.length - 1
}

/** The next highlight after `at`, wrapping to the first: the first right-record
 * check, the first screen check, the save, the stop. */
export function nextHighlight(frames, at) {
    const spots = frames.filter((f) => f.highlight)
    const next = spots.find((f) => f.i > at)
    return (next || spots[0] || frames[0]).i
}

export const replayMs = (frames) => frames.reduce((n, f) => n + f.dwell, 0)
