// The Seal of one run, drawn from its report (lib/sealRings.mjs, geometry in
// lib/sealArt.mjs): an arc per program step, a tick per screen check, a dot per
// read of the saved record, a short arc per right-record check, and a core
// whose fine pattern is drawn from the report's SHA-256.
//
// `phase` drives it: 'build' assembles the marks in the order the run made
// them (each mark's delay is its time in the run, compressed to `buildMs`),
// 'verify' runs a sweep over every mark, 'valid' closes the outer ring,
// 'broken' fractures it, 'rest' just shows it. `still` (reduced motion) shows
// the finished Seal with no movement at all. The full Seal has a focusable
// target per step (arrow keys move between them); the emblem is a picture.
import { useEffect, useId, useMemo, useRef } from 'react'
import { C, R, iris, polar, sealArt } from '../lib/sealArt.mjs'
import s from './SealArt.module.css'

const DELAY_CAP = 0.94

export function stepLabel(step, total) {
    const what = {
        passed: 'passed',
        stopped: 'the run stopped here',
        not_reached: 'not reached',
    }[step.status]
    return `Step ${step.i + 1} of ${total}: ${step.name}, ${what}`
}

export default function SealArt({
    rings,
    tone,
    digest,
    phase = 'rest',
    still = false,
    variant = 'full',
    buildMs = 3200,
    active = null,
    focusIndex = 0,
    onActive,
    onPick,
    onKey,
    ringFocus = null,
    witnesses = null,
    lit = null,
    spot = null,
    label,
}) {
    const art = useMemo(() => sealArt(rings), [rings])
    const full = variant === 'full'
    const broken = phase === 'broken'
    const irisLayers = useMemo(() => (full ? iris(digest, { radius: 100, disorder: broken ? 1 : 0 }) : null), [digest, broken, full])
    const anchor = rings.anchor
    const anchorPulse = Boolean(lit && anchor && lit.step === anchor.step && ['done', 'stop', 'readback'].includes(lit.stage))
    const raw = useId()
    const uid = `s${raw.replace(/[^a-zA-Z0-9]/g, '')}`
    const runMs = Math.max(1, rings.runMs)
    const delay = (t) =>
        t == null ? 0 : Math.round((Math.min(t / runMs, 1) * DELAY_CAP) * buildMs)
    const on = (step) => (active != null && step === active ? '1' : undefined)
    // During a replay every piece says whether the current frame has reached it.
    const L = (ok) => (lit ? (ok ? '1' : '0') : undefined)
    const pieceStyle = (p, extra = {}) => ({
        '--d': `${delay(p.t)}ms`,
        '--a': p.at,
        '--bx': `${p.bx}px`,
        '--by': `${p.by}px`,
        '--br': `${p.br}deg`,
        ...extra,
    })

    return (
        <div
            className={s.wrap}
            data-phase={phase}
            data-tone={tone}
            data-variant={variant}
            data-still={still ? '1' : undefined}
            data-focus={active != null ? '1' : undefined}
            data-ring={ringFocus || undefined}
            data-spot={spot || undefined}
            data-anchored={anchorPulse ? '1' : undefined}
            style={{ '--build': `${buildMs}ms` }}
        >
            <div className={s.halo} aria-hidden="true" />
            {irisLayers ? <Iris key={digest} layers={irisLayers} phase={phase} broken={broken} tone={tone} /> : null}
            <svg
                className={s.svg}
                viewBox="0 0 600 600"
                role={full ? 'group' : 'img'}
                aria-label={label}
            >
                <defs>
                    <linearGradient id={`${uid}g`} x1="80" y1="40" x2="520" y2="560" gradientUnits="userSpaceOnUse">
                        <stop offset="0" className={s.g0} />
                        <stop offset="1" className={s.g1} />
                    </linearGradient>
                    <radialGradient id={`${uid}c`} cx="300" cy="280" r="110" gradientUnits="userSpaceOnUse">
                        <stop offset="0" className={s.c0} />
                        <stop offset="1" className={s.c1} />
                    </radialGradient>
                </defs>

                <g className={s.layer} style={{ '--z': 1 }}>
                    <circle className={s.seal} cx={C} cy={C} r={R.section + 16} pathLength="1" />
                    <circle className={s.hair} cx={C} cy={C} r={R.step + 32} />
                    {art.bands.map((b) => (
                        <g key={b.k} className={s.band} style={{ '--d': `${Math.round(b.at * buildMs * 0.4)}ms` }}>
                            <path className={s.bandArc} d={b.d} pathLength="1" />
                            <line className={s.bandMark} x1={b.mark[0][0]} y1={b.mark[0][1]} x2={b.mark[1][0]} y2={b.mark[1][1]} />
                            {full ? (
                                <>
                                    <path id={`${uid}b${b.k}`} d={b.text} fill="none" />
                                    <text className={s.bandText}>
                                        <textPath href={`#${uid}b${b.k}`} startOffset="50%" textAnchor="middle">
                                            {b.title.toUpperCase()}
                                        </textPath>
                                    </text>
                                </>
                            ) : null}
                        </g>
                    ))}
                </g>

                {witnesses ? <Witnesses witnesses={witnesses} /> : null}
                {anchor ? <AnchorMark deg={(anchor.step + 0.5) * art.per} kind={anchor.kind} /> : null}

                <g className={s.layer} style={{ '--z': 0.8 }}>
                    {art.steps.map((p) => (
                        <path
                            key={`b${p.i}`}
                            className={s.bloom}
                            data-status={p.status}
                            data-on={on(p.i)}
                            data-lit={L(lit?.steps(p.i))}
                            d={p.d}
                            pathLength="1"
                            style={pieceStyle(p)}
                        />
                    ))}
                    {art.steps.map((p) => (
                        <path
                            key={p.i}
                            className={s.step}
                            data-status={p.status}
                            data-on={on(p.i)}
                            data-lit={L(lit?.steps(p.i))}
                            data-current={lit && lit.step === p.i ? '1' : undefined}
                            data-anchor={anchor && anchor.step === p.i ? anchor.kind : undefined}
                            data-i={p.i}
                            d={p.d}
                            pathLength="1"
                            stroke={p.status === 'passed' ? `url(#${uid}g)` : undefined}
                            style={pieceStyle(p)}
                        />
                    ))}
                    {art.stop ? (
                        <g
                            data-lit={L(lit && lit.stage === 'stop')}
                            className={s.stop}
                            style={{ '--d': `${delay(rings.steps[rings.stoppedAt].t1)}ms` }}
                        >
                            <line x1={art.stop.x1} y1={art.stop.y1} x2={art.stop.x2} y2={art.stop.y2} />
                        </g>
                    ) : null}
                </g>

                <g className={s.layer} style={{ '--z': 0.6 }}>
                    <circle className={s.hair} cx={C} cy={C} r={R.dot + 16} />
                    {art.ticks.map((k) => (
                        <line
                            key={k.i}
                            className={s.tick}
                            data-result={k.result}
                            data-on={on(k.step)}
                            data-lit={L(lit?.checks(k.step))}
                            x1={k.x1}
                            y1={k.y1}
                            x2={k.x2}
                            y2={k.y2}
                            style={pieceStyle(k)}
                        />
                    ))}
                </g>

                <g className={s.layer} style={{ '--z': 0.45 }}>
                    {art.dots.map((d) => (
                        <circle
                            key={d.i}
                            className={s.dot}
                            data-result={d.result}
                            data-lit={L(lit?.readback(d.i))}
                            data-on={active != null && rings.readbacks[d.i].step === active ? '1' : undefined}
                            cx={d.cx}
                            cy={d.cy}
                            r={R.dotSize}
                            style={pieceStyle(d)}
                            onMouseEnter={full && onActive ? () => onActive(rings.readbacks[d.i].step, { readback: d.i }) : undefined}
                        />
                    ))}
                    <circle className={s.hair} cx={C} cy={C} r={R.petal + 18} />
                </g>

                <g className={s.layer} style={{ '--z': 0.3 }}>
                    {art.petals.map((p) => (
                        <path
                            key={p.i}
                            className={s.petal}
                            data-result={p.result}
                            data-on={on(p.step)}
                            data-lit={L(lit?.identity(p.step))}
                            d={p.d}
                            pathLength="1"
                            style={pieceStyle(p)}
                            onMouseEnter={full && onActive ? () => onActive(p.step, { identity: p.i }) : undefined}
                        />
                    ))}
                </g>

                <g className={s.layer} style={{ '--z': 0.15 }}>
                    <circle className={s.core} cx={C} cy={C} r={R.core} fill={`url(#${uid}c)`} />
                    <g className={s.glyph}>
                        <path className={s.check} d={`M${C - 22} ${C + 1}l15 15 31-33`} pathLength="1" />
                        <rect className={s.square} x={C - 15} y={C - 15} width="30" height="30" rx="4" />
                        <path className={s.cross} d={`M${C - 17} ${C - 17}l34 34M${C + 17} ${C - 17}l-34 34`} pathLength="1" />
                    </g>
                </g>

                {lit ? (
                    <g className={s.playhead} style={{ '--deg': `${((lit.step + 0.5) * art.per).toFixed(2)}deg` }} aria-hidden="true">
                        <line x1={C} y1={C - R.core - 6} x2={C} y2={C - R.step - 22} />
                        <circle cx={C} cy={C - R.step - 22} r="4.5" />
                    </g>
                ) : null}

                {full ? (
                    <g className={s.hits} role="list" aria-label={`${rings.steps.length} program steps`}>
                        {art.steps.map((p) => (
                            <path
                                key={p.i}
                                role="listitem"
                                className={s.hit}
                                d={p.hit}
                                tabIndex={p.i === focusIndex ? 0 : -1}
                                aria-label={stepLabel(rings.steps[p.i], rings.steps.length)}
                                data-step={p.i}
                                onMouseEnter={onActive ? () => onActive(p.i) : undefined}
                                onFocus={onActive ? () => onActive(p.i, { focus: true }) : undefined}
                                onClick={onPick ? () => onPick(p.i) : undefined}
                                onKeyDown={onKey}
                            />
                        ))}
                    </g>
                ) : null}
            </svg>
            <div className={s.sweep} aria-hidden="true" />
        </div>
    )
}

// The outside witnesses, outside the Seal: Sigstore Rekor (the public log, a
// stack of entries) at the lower left and Sigstore's Timestamp Authority (a
// clock) at the lower right. Each shows only when the run's files have it, and
// lights when its check passes.
const WITNESS = {
    rekor: { deg: 226, short: 'PUBLIC LOG', name: 'Sigstore Rekor: the Seal is in the public transparency log' },
    tsa: { deg: 134, short: 'TIMESTAMP', name: 'Sigstore Timestamp Authority: a trusted time for the Seal' },
}
function Witnesses({ witnesses }) {
    return (
        <g className={s.layer} style={{ '--z': 1.15 }}>
            {Object.entries(WITNESS).map(([id, w]) => {
                const state = witnesses[id]
                if (!state) return null
                const [x, y] = polar(R.section + 32, w.deg)
                const [x1, y1] = polar(R.section + 6, w.deg)
                const [x2, y2] = polar(R.section + 17, w.deg)
                return (
                    <g key={id} className={s.witness} data-state={state} data-witness={id}>
                        <title>{`${w.name}${state === 'pass' ? ', checked' : state === 'fail' ? ", doesn't check" : ''}`}</title>
                        <line className={s.witnessLink} x1={x1} y1={y1} x2={x2} y2={y2} />
                        <circle className={s.witnessPing} cx={x} cy={y} r="15" />
                        <circle className={s.witnessDot} cx={x} cy={y} r="15" />
                        {id === 'rekor' ? (
                            <path className={s.witnessGlyph} d={`M${x - 6.5} ${y - 5}h13M${x - 6.5} ${y}h13M${x - 6.5} ${y + 5}h13`} />
                        ) : (
                            <path className={s.witnessGlyph} d={`M${x} ${y - 6.5}v6.5l4 3M${x - 7.5} ${y}a7.5 7.5 0 1 0 15 0a7.5 7.5 0 1 0 -15 0`} />
                        )}
                        <text className={s.witnessText} x={x} y={y + 30} textAnchor="middle">
                            {w.short}
                        </text>
                    </g>
                )
            })}
        </g>
    )
}

// Where the Seal belongs on its run: the step where the outcome was decided (the
// save its read-backs prove, or the stop), marked just outside the ring.
function AnchorMark({ deg, kind }) {
    const [x, y] = polar(R.section + 2, deg)
    const [x1, y1] = polar(R.step + 10, deg)
    return (
        <g className={s.layer} style={{ '--z': 1 }}>
            <g className={s.anchorMark} data-kind={kind}>
                <title>{kind === 'stop' ? 'Sealed here: the run stopped at this step' : 'Sealed here: the save its read-backs prove'}</title>
                <line x1={x1} y1={y1} x2={x} y2={y} />
                <circle cx={x} cy={y} r="8" />
                <circle cx={x} cy={y} r="3" className={s.anchorDot} />
            </g>
        </g>
    )
}

// The iris, one canvas per layer. Each layer is drawn once from its cached
// geometry (lib/sealArt.mjs, iris) and turned by CSS, so it spins as a plain
// texture with no repainting. While the Seal is checked, the petals brighten
// one by one: that redraw runs only for the check's sweep.
const SWEEP_MS = 1700
function Iris({ layers, phase, broken, tone }) {
    const box = useRef(null)
    const canvases = useRef([])
    useEffect(() => {
        const root = box.current
        if (!root) return undefined
        const css = getComputedStyle(root)
        const color = (name, fallback) => css.getPropertyValue(name).trim() || fallback
        const fills = broken
            ? [color('--fail', '#ff4d6a'), color('--fail', '#ff4d6a'), color('--fail', '#ff4d6a')]
            : [color('--t0', '#4cf0b8'), color('--soft', '#b9fbe5'), color('--t0', '#4cf0b8')]
        const alphas = broken ? [0.3, 0.26, 0.22] : [0.62, 0.45, 0.35]
        const paths = layers.map((layer) => layer.petals.map((p) => ({ path: new Path2D(p.d), at: p.at })))
        const draw = (progress) => {
            canvases.current.forEach((canvas, l) => {
                if (!canvas) return
                const size = canvas.clientWidth
                const dpr = Math.min(2, window.devicePixelRatio || 1)
                if (canvas.width !== Math.round(size * dpr)) {
                    canvas.width = Math.round(size * dpr)
                    canvas.height = Math.round(size * dpr)
                }
                const ctx = canvas.getContext('2d')
                ctx.setTransform((size * dpr) / 200, 0, 0, (size * dpr) / 200, 0, 0)
                ctx.clearRect(0, 0, 200, 200)
                for (const p of paths[l]) {
                    const lift = progress == null ? 0 : Math.max(0, 1 - Math.abs(progress - p.at) * 6)
                    ctx.globalAlpha = Math.min(1, alphas[l] + lift * 0.6)
                    ctx.fillStyle = lift > 0.3 ? '#ffffff' : fills[l]
                    ctx.fill(p.path)
                }
            })
        }
        draw(null)
        if (phase !== 'verify' || window.matchMedia('(prefers-reduced-motion: reduce)').matches) return undefined
        const start = performance.now()
        let frame = requestAnimationFrame(function tick(now) {
            const k = (now - start) / SWEEP_MS
            draw(k > 1.1 ? null : k)
            if (k <= 1.1) frame = requestAnimationFrame(tick)
        })
        return () => cancelAnimationFrame(frame)
    }, [layers, phase, broken, tone])
    return (
        <div className={s.iris} ref={box} aria-hidden="true">
            {layers.map((layer, l) => (
                <canvas
                    key={l}
                    ref={(el) => {
                        canvases.current[l] = el
                    }}
                    className={s.irisLayer}
                    style={{ '--spin': `${layer.dir * (140 + l * 50)}s` }}
                />
            ))}
            <div className={s.irisGlow} />
        </div>
    )
}
