// The compiled program this run ran, as the shared program map
// (@openadapt/program-graph), read-only, under the Seal. It follows the Seal
// page's one frame index: the step the page shows is lit and selected here,
// its section unfolded; picking a step here picks it on the Seal.
// `program` is { spec, folds?, titles? }: the run's program graph, its
// sections and a name per node id (sealPageProps fills them in).
import { useEffect, useMemo, useRef, useState } from 'react'
import { ProgramMap } from '@openadapt/program-graph'
import s from './SealPage.module.css'

export default function SealMap({ program, step, status, onPick, anchor = null }) {
    const { spec, folds = null, titles = {} } = program
    const actions = useMemo(() => spec.nodes.filter((n) => n.kind === 'action').map((n) => n.id), [spec])
    const groupOf = useMemo(
        () => Object.fromEntries((folds?.groups || []).flatMap((g) => g.nodes.map((id) => [id, g.id]))),
        [folds]
    )
    const titleFor = useMemo(() => (node) => titles[node.id] || node.title, [titles])
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
        const group = (folds?.groups || []).find((g) => g.nodes.includes(id))
        const cards = [...root.querySelectorAll('.opg-node')]
        const label = (el) => el.getAttribute('aria-label') || ''
        const card =
            (title ? cards.find((el) => !el.dataset.fold && label(el).includes(title)) : null) ||
            (group ? cards.find((el) => el.dataset.fold && label(el).includes(`: ${group.title}.`)) : null)
        if (card) card.dataset.sealAnchor = anchor.kind
    })
    const node = step != null ? actions[step] : null
    // The visitor's own unfolded sections, plus the current step's.
    const [open, setOpen] = useState([])
    const expanded = useMemo(
        () => [...new Set([...open, ...(node && groupOf[node] ? [groupOf[node]] : [])])],
        [open, node, groupOf]
    )
    const playhead = useMemo(() => (node ? { node, iteration: 1, status: status || 'running' } : null), [node, status])
    return (
        <div className={s.map} data-step={step ?? ''} ref={box}>
            <ProgramMap
                spec={spec}
                folds={folds}
                stopExits={folds?.stops}
                compact
                numbering="actions"
                titleFor={titleFor}
                theme="dark"
                playhead={playhead}
                selectedId={node || actions[0]}
                onSelect={(id) => {
                    // The map selects the step its playhead reaches on its own;
                    // only a different step is the visitor picking one.
                    if (id === node) return
                    const i = actions.indexOf(id)
                    if (i >= 0) onPick(i)
                }}
                expanded={expanded}
                onExpandedChange={setOpen}
            />
        </div>
    )
}
