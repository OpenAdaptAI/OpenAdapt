// The one line a Seal card says about its run, from its report: what the save
// proved (its label and the read-backs), or why the run stopped; and whether
// its signature has been checked. The landing map's inspector card and the
// Seal page's both use it.

/**
 * run: { rings, label, headline, stopReason? }. verified: true once the
 * signature checked out (at build time on the landing page, in the browser
 * on the Seal page).
 */
export function sealOutcomeLine(run, verified) {
    const c = run.rings.counts
    const what =
        run.rings.anchor?.kind === 'stop'
            ? `Stopped: ${run.stopReason || String(run.headline || '').replace(/^[^:]*:\s*/, '') || 'before saving'}`
            : `${run.label} · ${c.readbacksPassed} of ${c.readbacks} read back`
    return `${what}${verified ? ' · signature valid' : ''}`
}

/** The program node a Seal belongs to: the action at its anchor step. */
export function anchorNodeId(spec, rings) {
    const step = rings?.anchor?.step
    if (step == null) return null
    return spec.nodes.filter((n) => n.kind === 'action')[step]?.id ?? null
}
