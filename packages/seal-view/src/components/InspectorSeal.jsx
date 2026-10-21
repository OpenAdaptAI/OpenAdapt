// The Seal card at the top of the program map's step inspector, shown exactly
// while the inspector shows a step a Seal certifies (the save its read-backs
// prove, or the stop), or a folded card holding that step. Nothing else gates
// it: not playback, not the end of the runs. The package
// (@openadapt/program-graph) owns the inspector's markup and has no slot for
// it, so useInspectorHost watches the inspector, puts one container first in
// it while it shows such a step, and the card renders into it with a portal
// (as the landing map's run chips do).
//
// On openadapt.ai's landing page the card opens the Seal of the run that ended
// there (a chooser when several did); on a Seal page it says this is the step
// the Seal certifies and goes back up to the Seal. `Link` is the host's link
// component (default: a plain <a>).
import { useEffect, useState } from 'react'
import { MiniIris } from './SealArt.jsx'
import s from './InspectorSeal.module.css'

/** What the inspector shows: { step: node id } for one step, { fold: title } for a folded card, or null. */
export function inspectorShows(inspector) {
    const head = inspector.querySelector('.opg-inspector-head span')?.textContent || ''
    if (head === 'Selected step') {
        const id = inspector.querySelector(':scope > p.opg-mono')?.textContent || null
        return id ? { step: id } : null
    }
    if (head === 'Selected steps') {
        const title = inspector.querySelector(':scope > h3')?.textContent || null
        return title ? { fold: title } : null
    }
    return null
}

/**
 * The anchor node ids the inspector shows: the step itself, or every anchor in
 * the folded card it shows. anchors: node ids; groups: the folds' groups.
 */
export function anchorsShown(shows, anchors, groups) {
    if (!shows) return []
    if (shows.step) return anchors.includes(shows.step) ? [shows.step] : []
    const group = groups.find((g) => (g.title ?? g.id) === shows.fold)
    return group ? anchors.filter((id) => group.nodes.includes(id)) : []
}

/** A host first in the inspector while it shows any of `anchors`, and which ones. */
export function useInspectorHost(root, anchors, groups) {
    const [state, setState] = useState({ host: null, ids: [] })
    const key = anchors.join(' ')
    useEffect(() => {
        const el = root.current
        if (!el || !key) return undefined
        const list = key.split(' ')
        let box = null
        let last = ''
        const sync = () => {
            const inspector = el.querySelector('.opg-inspector')
            const ids = inspector ? anchorsShown(inspectorShows(inspector), list, groups) : []
            if (!ids.length) {
                if (box) {
                    box.remove()
                    box = null
                    last = ''
                    setState({ host: null, ids: [] })
                }
                return
            }
            if (!box) {
                box = document.createElement('div')
                box.className = s.host
                box.dataset.sealCardHost = ''
            }
            if (inspector.firstChild !== box) inspector.prepend(box)
            const now = ids.join(' ')
            if (now !== last) {
                last = now
                setState({ host: box, ids })
            }
        }
        const mo = new MutationObserver(sync)
        mo.observe(el, { childList: true, subtree: true, characterData: true })
        sync()
        return () => {
            mo.disconnect()
            box?.remove()
            setState({ host: null, ids: [] })
        }
    }, [root, key, groups])
    return state
}

/**
 * seals: [{ n, href, irisSeed, tone, line, anchor }], the first one primary.
 * Landing: links to the Seals. Seal page: onBack scrolls up to the Seal.
 */
export function InspectorSealCard({ seals, onBack = null, Link = 'a' }) {
    const [primary, ...others] = seals
    const stop = primary.anchor?.kind === 'stop'
    const landing = !onBack
    return (
        <section className={s.card} data-tone={primary.tone} data-seal-card={landing ? 'open' : 'here'} aria-label="This step's Seal">
            <MiniIris seed={primary.irisSeed} tone={primary.tone} size={58} />
            <div className={s.body}>
                <p className={s.kicker}>
                    {landing ? `Sealed here${primary.n ? ` · referral ${primary.n}` : ''}` : 'This is the step the Seal certifies'}
                </p>
                <p className={s.line}>{primary.line}</p>
                <p className={s.sub}>
                    {stop
                        ? 'The run stopped here, before saving, and was signed.'
                        : 'The save its read-backs prove, signed on the machine that ran it.'}
                </p>
                {landing ? (
                    <Link
                        className={s.action}
                        data-seal-open=""
                        href={
                            primary.href /* nosemgrep: react-href-var -- sealPagePath() builds /seals/ plus 16 hex digits of the Seal's SHA-256. */
                        }
                    >
                        Open the Seal →
                    </Link>
                ) : (
                    <button type="button" className={s.action} onClick={onBack}>
                        Back to the Seal ↑
                    </button>
                )}
                {others.length ? (
                    <p className={s.others}>
                        <span>Also sealed here:</span>
                        {others.map((o) => (
                            <Link
                                key={o.n}
                                href={
                                    o.href /* nosemgrep: react-href-var -- sealPagePath() builds /seals/ plus 16 hex digits of the Seal's SHA-256. */
                                }
                            >
                                Referral {o.n}
                            </Link>
                        ))}
                    </p>
                ) : null}
            </div>
        </section>
    )
}
