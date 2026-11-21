// The compiled program this run ran, under the Seal: the full program
// workbench (@openadapt/program-graph), with its sections, inspector,
// evidence lanes, stop rules, zoom, fit, Expand, the step pictures the host
// resolves and a guided tour. It follows the Seal page's one frame index: the
// step the page shows is lit and selected here, its section unfolded; picking
// a step here picks it on the Seal. While the inspector shows the step the
// Seal certifies, a card at its top says so and goes back up to the Seal.
//
// program: { spec, folds?, titles? } (sealPageProps fills them in);
// map: { kicker?, title?, resolveMedia? } from the host.
// theme: 'dark' or 'light'. fill: the workbench's stage grows to fill the
// height of the pane it sits in (the Seal page's split view), measured as the
// pane resizes; otherwise the package's own fixed stage height.
import { useEffect, useMemo, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { ProgramWorkbench, defaultTourStops } from '@openadapt/program-graph'
import { InspectorSealCard, useInspectorHost } from './InspectorSeal.jsx'
import s from './SealPage.module.css'

const NO_GROUPS = []

export default function SealMap({
    program,
    map = {},
    step,
    status,
    onPick,
    anchor = null,
    sealRun = null,
    sealLine = '',
    onBackToSeal,
    theme = 'dark',
    fill = false,
}) {
    const { spec, folds = null, titles = {} } = program
    const groups = folds?.groups || NO_GROUPS
    const actions = useMemo(() => spec.nodes.filter((n) => n.kind === 'action').map((n) => n.id), [spec])
    const groupOf = useMemo(() => Object.fromEntries(groups.flatMap((g) => g.nodes.map((id) => [id, g.id]))), [groups])
    const titleFor = useMemo(() => (node) => titles[node.id] || node.title, [titles])
    const tour = useMemo(() => defaultTourStops(spec, { title: titleFor }), [spec, titleFor])
    const box = useRef(null)
    // The Seal's step on the map (the save, or the stop): marked on its card, or
    // on its section's card while the section is folded. The package owns the
    // cards, so the mark is an attribute set after each render.
    useEffect(() => {
        const root = box.current
        if (!root) return
        for (const el of root.querySelectorAll('[data-seal-anchor]')) delete el.dataset.sealAnchor
        if (!anchor) return
        const id = actions[anchor.step]
        const title = titles[id]
        const group = groups.find((g) => g.nodes.includes(id))
        const cards = [...root.querySelectorAll('.opg-node')]
        const label = (el) => el.getAttribute('aria-label') || ''
        const card =
            (title ? cards.find((el) => !el.dataset.fold && label(el).includes(title)) : null) ||
            (group ? cards.find((el) => el.dataset.fold && label(el).includes(`: ${group.title}.`)) : null)
        if (card) card.dataset.sealAnchor = anchor.kind
    })
    // Fill: the stage takes the pane's height less the workbench's own chrome.
    useEffect(() => {
        const root = box.current
        if (!root || !fill || typeof ResizeObserver === 'undefined') return undefined
        let frame = 0
        const size = () => {
            frame = 0
            const wb = root.querySelector('.opg-root')
            const view = root.querySelector('.opg-canvas-viewport')
            if (!wb || !view) return
            const chrome = wb.offsetHeight - view.offsetHeight
            const stage = Math.max(240, Math.floor(root.clientHeight - chrome - 2))
            if (root.style.getPropertyValue('--seal-stage-h') !== `${stage}px`) root.style.setProperty('--seal-stage-h', `${stage}px`)
        }
        const ro = new ResizeObserver(() => {
            if (!frame) frame = requestAnimationFrame(size)
        })
        ro.observe(root)
        size()
        return () => {
            ro.disconnect()
            cancelAnimationFrame(frame)
            root.style.removeProperty('--seal-stage-h')
        }
    }, [fill])
    const node = step != null ? actions[step] : null
    // A folded card or the stop card the visitor picked, until the replay moves on.
    const [own, setOwn] = useState(null)
    useEffect(() => setOwn(null), [node])
    // The visitor's own unfolded sections, plus the current step's.
    const [open, setOpen] = useState([])
    const expanded = useMemo(
        () => [...new Set([...open, ...(node && groupOf[node] ? [groupOf[node]] : [])])],
        [open, node, groupOf]
    )
    const playhead = useMemo(() => (node ? { node, iteration: 1, status: status || 'running' } : null), [node, status])
    const anchorId = anchor ? actions[anchor.step] : null
    const anchorIds = useMemo(() => (anchorId ? [anchorId] : []), [anchorId])
    const sealHost = useInspectorHost(box, anchorIds, groups).host
    return (
        <div className={s.map} data-step={step ?? ''} data-fill={fill ? '1' : undefined} ref={box}>
            <ProgramWorkbench
                spec={spec}
                folds={folds}
                stopExits={folds?.stops}
                compact
                numbering="actions"
                kicker={map.kicker || 'Compiled program'}
                title={map.title}
                titleFor={titleFor}
                resolveMedia={map.resolveMedia}
                tour={tour}
                tourSeen
                plainStopRules
                theme={theme}
                playhead={playhead}
                selectedId={own || node || actions[0]}
                onSelect={(id, origin) => {
                    // Following the playhead must not seek it. An explicit pick,
                    // including the current step, still navigates to that step.
                    if (origin === 'follow') return
                    // The folded card holding the Seal's step: that step, with its Seal card.
                    if (anchorId && groupOf[anchorId] === id && groups.some((g) => g.id === id)) {
                        setOwn(null)
                        onPick(anchor.step)
                        return
                    }
                    const i = actions.indexOf(id)
                    if (i >= 0) {
                        setOwn(null)
                        onPick(i)
                    } else setOwn(id)
                }}
                expanded={expanded}
                onExpandedChange={setOpen}
            />
            {sealHost && sealRun
                ? createPortal(
                      <InspectorSealCard seals={[{ ...sealRun, line: sealLine, anchor }]} onBack={onBackToSeal} />,
                      sealHost
                  )
                : null}
        </div>
    )
}
