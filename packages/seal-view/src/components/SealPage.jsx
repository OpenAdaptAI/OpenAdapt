// One run's Seal: the signed report of a run, drawn as a living object from
// that report (./SealArt.jsx), with its own chrome. openadapt.ai shows each
// published run's at /seals/<seal id>; the offline viewer (../viewer) shows
// the run on this computer from the files embedded in one HTML file.
//
// On load the Seal assembles and the browser checks the run's Ed25519 Seal
// over the exact report file (lib/receiptCheck.mjs). The visitor can then play
// the run: one frame index (lib/sealReplay.mjs) drives everything at once, the
// lit ring and its playhead, the "Step X of Y" line, the step table, the
// spotlights (the first right-record check, the first screen check, the save
// and its read-backs, a stop) and the program map below. Scrubbing, "Next
// highlight", the keys and pointing at a step all move that same index.
//
// Reduced motion: nothing moves on its own (no assembly, sweep, parallax or
// glow), and the page opens on the finished Seal with "Play the run". What the
// visitor starts still plays, as fades and colour changes.
//
// Data: `seal`, the run's props (lib/sealProps.mjs sealPageProps). The host's
// own parts come in `host`, all optional:
//   loadFiles()  the run's files as the check reads them (default: fetch the
//                paths in seal.files, lib/sealFiles.mjs)
//   Link         the host's link component (default: a plain <a>)
//   links        { home: {href, label}, map: {href, label}, docs, keys: {href, label},
//                  file(name) -> href of one of the run's files, or null }
//   copyHref     the page's own address, for "Copy link" (none: no button)
//   live         ask the network once the offline proof passes: Rekor's live
//                entry and GitHub's key list (default true; the viewer: false)
//   kicker, footer, keyWords ("the pinned key"), unchanged ("The published
//   files are unchanged."), canonicalOrigin (rewritten to this page's origin
//   in the commands)
// No <head>: the host sets the title (sealPageMeta).
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { checkReceipt } from '../lib/receiptCheck.mjs'
import { sha256Hex } from '../lib/localSeal.mjs'
import { sealSha256 } from '../lib/sealAnchor.mjs'
import { loadSealFiles } from '../lib/sealFiles.mjs'
import { applyTamper } from '../lib/sealTamper.mjs'
import { checkWitnesses } from '../lib/sealWitnesses.mjs'
import { rekorLiveMatch, rekorSearchUrl } from '../lib/rekorLive.mjs'
import { frameOfStep, litAt, nextHighlight, replayFrames } from '../lib/sealReplay.mjs'
import { authorizedLine as authorizedWords, checkGithubIdentity } from '../lib/runAuthorization.mjs'
import { runDate } from '../lib/sealDates.mjs'
import SealArt from './SealArt.jsx'
import HashLine from './HashLine.jsx'
import OpenAdaptMark from './OpenAdaptMark.jsx'
import SealMap from './SealMap.jsx'
import s from './SealPage.module.css'

const BUILD_MS = 3400
const SWEEP_MS = 1700
const SPEEDS = [1, 2, 4]

// Each failure, in words a reader can act on. The code shows beside it.
const REASONS = {
    subject_mismatch: "The report doesn't match the SHA-256 the Seal signed.",
    bad_signature: "The Ed25519 signature doesn't match the signed fields.",
    malformed: "The Seal file isn't well formed.",
    unknown_key: "The Seal names a key this site doesn't pin.",
    revoked_key: 'The Seal names a revoked key.',
    key_mismatch: "The key in the Seal isn't the key this site pins.",
    ambiguous_key: 'The pinned key list names this key twice.',
}
const reasonText = (reason) =>
    REASONS[reason] ||
    (String(reason).startsWith('anchor_')
        ? "The public log's record doesn't back this signature."
        : String(reason).startsWith('auth')
          ? "The authorization doesn't cover this run."
          : 'The check failed.')

const secs = (ms) => `${(ms / 1000).toFixed(1)} s`
const cap = (t) => (t ? t.charAt(0).toUpperCase() + t.slice(1) : t)

function usePrefersReducedMotion() {
    const [reduced, setReduced] = useState(false)
    useEffect(() => {
        const mq = window.matchMedia('(prefers-reduced-motion: reduce)')
        const update = () => setReduced(mq.matches)
        update()
        mq.addEventListener('change', update)
        return () => mq.removeEventListener('change', update)
    }, [])
    return reduced
}

// Pointer parallax: the Seal's layers shift with the pointer, eased each frame.
function useParallax(ref, enabled) {
    useEffect(() => {
        const el = ref.current
        if (!el || !enabled) return undefined
        if (!window.matchMedia('(hover: hover) and (pointer: fine)').matches) return undefined
        let tx = 0
        let ty = 0
        let x = 0
        let y = 0
        let frame = 0
        const tick = () => {
            x += (tx - x) * 0.08
            y += (ty - y) * 0.08
            el.style.setProperty('--px', x.toFixed(4))
            el.style.setProperty('--py', y.toFixed(4))
            frame = Math.abs(tx - x) + Math.abs(ty - y) > 0.0008 ? requestAnimationFrame(tick) : 0
        }
        const move = (e) => {
            const r = el.getBoundingClientRect()
            tx = Math.max(-1, Math.min(1, ((e.clientX - r.left) / r.width) * 2 - 1))
            ty = Math.max(-1, Math.min(1, ((e.clientY - r.top) / r.height) * 2 - 1))
            if (!frame) frame = requestAnimationFrame(tick)
        }
        const leave = () => {
            tx = 0
            ty = 0
            if (!frame) frame = requestAnimationFrame(tick)
        }
        window.addEventListener('pointermove', move, { passive: true })
        document.documentElement.addEventListener('pointerleave', leave)
        return () => {
            window.removeEventListener('pointermove', move)
            document.documentElement.removeEventListener('pointerleave', leave)
            cancelAnimationFrame(frame)
            el.style.removeProperty('--px')
            el.style.removeProperty('--py')
        }
    }, [ref, enabled])
}

// Mount children once their place comes within a screen of view.
function WhenNear({ children, minHeight }) {
    const ref = useRef(null)
    const [near, setNear] = useState(false)
    useEffect(() => {
        const el = ref.current
        if (!el) return undefined
        const io = new IntersectionObserver(
            ([e]) => {
                if (e.isIntersecting) {
                    setNear(true)
                    io.disconnect()
                }
            },
            { rootMargin: '600px 0px' }
        )
        io.observe(el)
        return () => io.disconnect()
    }, [])
    return (
        <div ref={ref} style={{ minHeight }}>
            {near ? children : null}
        </div>
    )
}

/** The page's title and description, for the host's <head>. */
export function sealPageMeta(page) {
    const c = page.rings.counts
    return {
        title: `${page.label} · ${page.job} · OpenAdapt Seal`,
        description: `${page.app}, ${page.job}: ${page.label}. ${c.stepsPassed} of ${c.steps} steps, ${c.checksPassed} of ${c.checks} screen checks${c.readbacksPassed ? `, ${c.readbacksPassed} of ${c.readbacks} fields read back from the record` : ''}. A signed run report you can check in your browser.`,
    }
}

export default function SealPage({ seal: page, host = {} }) {
    const {
        loadFiles = null,
        Link = 'a',
        links = {},
        copyHref = null,
        live: liveChecks = true,
        kicker: hostKicker = null,
        footer = null,
        keyWords = 'the pinned key',
        unchanged = 'The published files are unchanged.',
        canonicalOrigin = null,
        chrome = true,
    } = host
    const {
        rings,
        seal,
        tone,
        label,
        line,
        job,
        app,
        startedAt,
        stopReason,
        media,
        tamper,
        runs,
        referral,
        referrals,
        files,
        keys,
        authorization,
        authorizedLine: claimedLine,
        authTiming: givenTiming,
        stopNote,
        program,
        commands: canonicalCommands = null,
        irisSeed: publishedSeed,
    } = page
    const reduced = usePrefersReducedMotion()
    const [phase, setPhase] = useState('build')
    const [result, setResult] = useState(null)
    const [digest, setDigest] = useState(null)
    // The iris is drawn from the files actually checked: the published ones, or a changed copy.
    const [seed, setSeed] = useState(publishedSeed)
    const hasWitness = Boolean(files.anchor || files.tsa)
    const [witness, setWitness] = useState(null)
    const [live, setLive] = useState(null)
    const [change, setChange] = useState(null)
    const [selected, setSelected] = useState(null)
    const [hovered, setHovered] = useState(null)
    const [focusIndex, setFocusIndex] = useState(0)
    const [ring, setRing] = useState(null)
    const [copied, setCopied] = useState(false)
    const [tamperOpen, setTamperOpen] = useState(false)
    const [commands, setCommands] = useState(canonicalCommands)
    const filesRef = useRef(null)
    const stageRef = useRef(null)
    const runId = useRef(0)
    useParallax(stageRef, !reduced)

    // The replay: one index into the run's frames is the only playback state.
    const frames = useMemo(() => replayFrames(rings), [rings])
    const [frame, setFrame] = useState(null) // null: not replaying
    const [playing, setPlaying] = useState(false)
    const [speed, setSpeed] = useState(1)
    const replaying = frame !== null
    const current = replaying ? frames[frame] : null
    const lit = useMemo(() => (current ? litAt(current, rings) : null), [current, rings])

    // The page's own chrome: a dark page under the Seal, edge to edge.
    useEffect(() => {
        if (!chrome) return undefined
        const { body, documentElement: html } = document
        const prev = [body.style.background, html.style.background, html.style.colorScheme]
        body.style.background = '#030507'
        html.style.background = '#030507'
        html.style.colorScheme = 'dark'
        return () => {
            body.style.background = prev[0]
            html.style.background = prev[1]
            html.style.colorScheme = prev[2]
        }
    }, [chrome])

    // The command fetches the files from the site serving this page (a preview or openadapt.ai).
    useEffect(() => {
        const origin = window.location.origin
        if (canonicalCommands && canonicalOrigin && /^https?:/.test(origin) && origin !== canonicalOrigin)
            setCommands(canonicalCommands.split(canonicalOrigin).join(origin))
    }, [canonicalCommands, canonicalOrigin])

    const verify = useCallback(
        async (option) => {
            const id = ++runId.current
            setChange(option || null)
            setResult(null)
            setDigest(null)
            setWitness(null)
            setFrame(null)
            setPlaying(false)
            setPhase('verify')
            const swept = new Promise((r) => setTimeout(r, reduced ? 0 : SWEEP_MS))
            let next
            let seen = null
            try {
                filesRef.current = filesRef.current || (await (loadFiles ? loadFiles() : loadSealFiles(files, authorization)))
                const copy = option ? applyTamper(filesRef.current, option) : filesRef.current
                next = await checkReceipt(copy, keys, {
                    requireAnchor: Boolean(files.anchor),
                    requireAuthorization: Boolean(authorization),
                })
                seen = hasWitness ? await checkWitnesses(copy, keys) : null
                try {
                    const sealDigest = await sealSha256(JSON.parse(copy.sealText))
                    const nextSeed = await sha256Hex(new TextEncoder().encode(`${await sha256Hex(copy.report)}${sealDigest}`))
                    if (id === runId.current) setSeed(nextSeed)
                } catch {
                    // A Seal file that doesn't parse keeps the last picture; the verdict says why.
                }
            } catch {
                next = { ok: false, reason: 'not_loaded', steps: [] }
            }
            // The digest resolves on screen during the sweep; the verdict waits for it.
            if (id === runId.current) setDigest(next.subject?.sha256 ?? null)
            await swept
            if (id !== runId.current) return
            setWitness(seen)
            setResult(next)
            setPhase(next.ok ? 'valid' : next.reason === 'unsupported' || next.reason === 'not_loaded' ? 'rest' : 'broken')
        },
        [files, keys, authorization, reduced, hasWitness, loadFiles]
    )

    // Who signed the authorization: "@login" only when GitHub itself lists the
    // signing key for that login, asked live; until then, a self-asserted claim.
    const [identity, setIdentity] = useState('unverified')
    useEffect(() => {
        if (!liveChecks || !authorization || !result?.ok || !result.authorizedBy?.publicKey) return
        let gone = false
        checkGithubIdentity(authorization.login, result.authorizedBy.publicKey).then((r) => {
            if (!gone) setIdentity(r)
        })
        return () => {
            gone = true
        }
    }, [authorization, result, liveChecks])
    // The order of authorization and run, given by the host or found by the check.
    const authTiming = givenTiming ?? result?.timing ?? null
    const timed = authTiming === 'before_run' || authTiming === 'after_run'
    const authorizedLine =
        authorization && (claimedLine || (timed && result?.ok))
            ? authorizedWords(authorization.login, authTiming, authorization.anchorTime ?? result?.authAnchorTime, identity)
            : null

    // A second look at the log entry, live from the pinned Rekor host, once the
    // offline proof has passed. Any failure leaves the offline proof standing.
    const liveAsked = useRef(false)
    useEffect(() => {
        if (!liveChecks || !witness?.rekor?.ok || change || liveAsked.current) return
        liveAsked.current = true
        setLive('checking')
        rekorLiveMatch(witness.rekor.entry).then(setLive)
    }, [witness, change, liveChecks])

    // On load: assemble, then verify. Reduced motion: the finished Seal, verified at once.
    useEffect(() => {
        if (reduced) setPhase('rest')
        const t = setTimeout(() => verify(null), reduced ? 0 : BUILD_MS + 450)
        return () => clearTimeout(t)
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [reduced])

    // The clock: each frame holds for its dwell, then the next one shows.
    useEffect(() => {
        if (!replaying || !playing) return undefined
        if (frame >= frames.length - 1) {
            setPlaying(false)
            return undefined
        }
        const t = setTimeout(() => setFrame((f) => Math.min(frames.length - 1, f + 1)), frames[frame].dwell / speed)
        return () => clearTimeout(t)
    }, [replaying, playing, frame, frames, speed])

    const play = useCallback(() => {
        runId.current += 1
        setChange(null)
        setPhase('replay')
        setSelected(null)
        setHovered(null)
        setFrame((f) => (f === null || f >= frames.length - 1 ? 0 : f))
        setPlaying(true)
    }, [frames.length])
    const seek = useCallback(
        (f, keepPlaying = false) => {
            setPhase('replay')
            setFrame(Math.max(0, Math.min(frames.length - 1, f)))
            if (!keepPlaying) setPlaying(false)
        },
        [frames.length]
    )
    const stopReplay = () => {
        setFrame(null)
        setPlaying(false)
        setPhase(result?.ok ? 'valid' : result ? 'broken' : 'rest')
    }
    const toNextHighlight = () => seek(nextHighlight(frames, frame ?? -1), playing)
    const stepBy = (d) => {
        const at = current ? current.step : -1
        const target = Math.max(0, Math.min(frames.at(-1).step, at + d))
        seek(frameOfStep(frames, target))
    }

    // Keys for the replay, anywhere on the page but in a form field: Space plays
    // or pauses, the arrows step, Shift+Right goes to the next highlight.
    useEffect(() => {
        const onKey = (e) => {
            const t = e.target
            if (t && (t.closest?.('input, textarea, select, [contenteditable]') || e.metaKey || e.ctrlKey || e.altKey)) return
            const onButton = t?.closest?.('button, a, summary')
            if (e.key === ' ' && !onButton && !t?.closest?.('[data-step]')) {
                e.preventDefault()
                if (playing) setPlaying(false)
                else play()
            } else if (replaying && e.key === 'ArrowRight' && !t?.closest?.('[data-step]')) {
                e.preventDefault()
                if (e.shiftKey) toNextHighlight()
                else stepBy(1)
            } else if (replaying && e.key === 'ArrowLeft' && !t?.closest?.('[data-step]')) {
                e.preventDefault()
                stepBy(-1)
            } else if (replaying && e.key === 'Escape') stopReplay()
        }
        window.addEventListener('keydown', onKey)
        return () => window.removeEventListener('keydown', onKey)
    })

    // The step the page shows: the replay's, else the one picked or pointed at.
    const shown = replaying ? current.step : (selected ?? hovered)
    const step = shown != null ? rings.steps[shown] : null

    const onActive = useCallback(
        (i, how = {}) => {
            if (how.focus) setFocusIndex(i)
            // During a replay, pointing at a step moves the replay there (the same index).
            if (frame !== null) seek(frameOfStep(frames, i))
            else setHovered(i)
        },
        [frame, frames, seek]
    )
    const pick = useCallback(
        (i) => {
            if (frame !== null) seek(frameOfStep(frames, i))
            else setSelected((p) => (p === i ? null : i))
        },
        [frame, frames, seek]
    )
    const onRingKey = (e) => {
        const n = rings.steps.length
        const moves = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 }
        let next = null
        if (e.key in moves) next = (focusIndex + moves[e.key] + n) % n
        else if (e.key === 'Home') next = 0
        else if (e.key === 'End') next = n - 1
        else if (e.key === 'Escape') {
            setSelected(null)
            setHovered(null)
            e.currentTarget.blur()
            return
        } else if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault()
            pick(focusIndex)
            return
        } else return
        e.preventDefault()
        setFocusIndex(next)
        onActive(next)
        stageRef.current?.querySelector(`[data-step="${next}"]`)?.focus()
    }

    const copyLink = async () => {
        try {
            await navigator.clipboard.writeText(copyHref)
            setCopied(true)
            setTimeout(() => setCopied(false), 2200)
        } catch {
            setCopied(false)
        }
    }

    const c = rings.counts
    const stats = [
        { ring: 'steps', big: `${c.stepsPassed}/${c.steps}`, small: rings.stoppedAt == null ? 'steps passed' : `steps, stopped at ${rings.stoppedAt + 1}` },
        { ring: 'checks', big: `${c.checksPassed}/${c.checks}`, small: c.checksFailed ? `screen checks, ${c.checksFailed} failed` : 'screen checks' },
        { ring: 'readbacks', big: `${c.readbacksPassed}/${c.readbacks}`, small: c.readbacksFailed ? `record reads, ${c.readbacksFailed} failed` : c.readbacksPassed ? 'read back from the record' : 'record reads, not reached' },
        { ring: 'identity', big: `${c.identityPassed}/${c.identity}`, small: 'right-record checks' },
    ]
    const when = startedAt ? runDate(startedAt) : null
    const kicker = hostKicker ?? (referral ? `Referral ${referral} of ${referrals}` : null)
    const tampered = Boolean(change)
    const verdict =
        phase === 'verify' || phase === 'build'
            ? null
            : result?.ok
              ? 'valid'
              : result?.reason === 'unsupported'
                ? 'unsupported'
                : result?.reason === 'not_loaded'
                  ? 'not_loaded'
                  : result
                    ? 'fails'
                    : null
    const witnessState = (present, w) => (!present ? null : !w ? 'pending' : w.ok ? 'pass' : 'fail')
    const witnesses = hasWitness
        ? { rekor: witnessState(files.anchor, witness?.rekor), tsa: witnessState(files.tsa, witness?.tsa) }
        : null
    const ariaSeal = `The Seal of this run: ${stats.map((l) => `${l.big} ${l.small}`).join('; ')}.`
    const busy = phase === 'verify' || phase === 'build'
    const stepLine = step
        ? `Step ${step.i + 1} of ${rings.steps.length}${step.section ? ` · ${step.section}` : ''}${step.t0 != null ? ` · at ${secs(step.t0)}` : ''}`
        : null

    return (
        <>
            <a className={s.skip} href="#main">
                Skip to content
            </a>
            <div className={s.page} data-tone={tone} data-phase={phase}>
                <div className={s.grid} aria-hidden="true" />
                <header className={s.top}>
                    {links.home ? (
                        <Link href={links.home.href} className={s.home}>
                            <OpenAdaptMark className={s.mark} />
                            <span>{links.home.label}</span>
                        </Link>
                    ) : (
                        <span className={s.home}>
                            <OpenAdaptMark className={s.mark} />
                            <span>OpenAdapt</span>
                        </span>
                    )}
                    {runs?.length ? (
                    <nav className={s.runs} aria-label="The other runs">
                        {runs.map((r) => (
                            <Link
                                key={r.sealId}
                                href={r.href /* nosemgrep: react-href-var -- sealPagePath() builds /seals/ plus 16 hex digits of the Seal's SHA-256. */}
                                className={s.run}
                                data-tone={r.tone}
                                aria-current={r.sealId === page.sealId ? 'page' : undefined}
                                title={`Referral ${r.referral}: ${r.label}`}
                            >
                                <span className={s.runSeal}>
                                    <SealArt rings={r.rings} tone={r.tone} digest={r.digest} variant="emblem" phase="rest" still label="" />
                                </span>
                                <span className={s.runText}>
                                    <b>{r.referral}</b>
                                    <span>{r.label}</span>
                                </span>
                            </Link>
                        ))}
                    </nav>
                    ) : null}
                </header>

                <main id="main" className={s.hero}>
                    <section className={s.text} aria-labelledby="seal-h1">
                        <p className={s.kicker}>
                            <span>Signed run report</span>
                            {kicker ? <span>{kicker}</span> : null}
                        </p>
                        <h1 id="seal-h1" className={s.outcome}>
                            {label}
                        </h1>
                        {stopReason ? <p className={s.reason}>{cap(stopReason)}</p> : null}
                        <p className={s.meta}>
                            <span>{job}</span>
                            <span>{app}</span>
                            {when ? <span>{when}</span> : null}
                        </p>
                        <p className={s.line}>{line}</p>
                        {authorization && authorizedLine ? (
                            <p className={s.by}>
                                <a href={`https://github.com/${authorization.login}`} rel="noopener noreferrer">
                                    {authorizedLine}
                                </a>
                            </p>
                        ) : null}

                        <dl className={s.stats} aria-label="What the Seal shows">
                            {stats.map((l) => (
                                <div key={l.ring} data-ring={l.ring} onMouseEnter={() => setRing(l.ring)} onMouseLeave={() => setRing(null)}>
                                    <dt>
                                        <span className={s.swatch} aria-hidden="true" />
                                        {l.small}
                                    </dt>
                                    <dd>{l.big}</dd>
                                </div>
                            ))}
                        </dl>
                        <p className={s.calls}>
                            {c.modelCalls ?? '–'} AI model calls · {secs(rings.runMs)} of program time
                        </p>

                        <button type="button" className={s.playCta} onClick={playing ? () => setPlaying(false) : play} disabled={busy}>
                            <span className={s.playIcon} aria-hidden="true">
                                {playing ? '❚❚' : '▶'}
                            </span>
                            <span>
                                <b>{playing ? 'Pause the run' : replaying ? 'Resume the run' : 'Play the run'}</b>
                                <small>
                                    {rings.steps.filter((x) => x.status !== 'not_reached').length} steps, every check in order
                                    {rings.stoppedAt != null ? ', to the stop' : ', to the save'}
                                </small>
                            </span>
                        </button>

                        <div className={s.proof} aria-live="polite">
                            <HashLine
                                label={tampered && change.target === 'report' ? 'This copy of report.json' : 'report.json, in this browser'}
                                from={seal.signature}
                                to={digest}
                                against={seal.subject_sha256}
                                running={phase === 'verify'}
                                still={reduced}
                            />
                            <div className={s.signed}>
                                <span className={s.proofLabel}>Signed in the Seal</span>
                                <code className={s.hex}>{seal.subject_sha256}</code>
                            </div>
                            <p className={s.verdict} data-verdict={verdict || 'pending'}>
                                {verdict === null ? (
                                    <span className={s.pending}>{phase === 'build' ? 'Assembling the Seal from the report' : 'Checking the Ed25519 signature in your browser'}</span>
                                ) : verdict === 'valid' ? (
                                    <>
                                        <b>Valid.</b> This is the report exactly as the run wrote it, signed by {keyWords}.
                                    </>
                                ) : verdict === 'unsupported' ? (
                                    <>
                                        <b>The report matches.</b> This browser can&apos;t check Ed25519 signatures; use the commands below.
                                    </>
                                ) : verdict === 'not_loaded' ? (
                                    <>
                                        <b>The files didn&apos;t load,</b> so nothing was checked here. Use the commands below.
                                    </>
                                ) : (
                                    <>
                                        <b>Fails.</b> {reasonText(result.reason)} <code className={s.code}>{result.reason}</code>
                                    </>
                                )}
                            </p>
                            {tampered && verdict === 'fails' ? (
                                <p className={s.change}>
                                    Changed in a copy of the {change.target === 'seal' ? 'Seal' : 'report'}: <code>{change.field}</code>{' '}
                                    <del>{change.from}</del> → <ins>{change.to}</ins>. {unchanged}
                                </p>
                            ) : null}
                        </div>

                        <div className={s.actions}>
                            <button type="button" className={s.primary} onClick={() => verify(null)} disabled={busy}>
                                {tampered ? 'Restore and verify' : 'Verify'}
                            </button>
                            <button
                                type="button"
                                className={s.ghost}
                                aria-expanded={tamperOpen}
                                aria-controls="seal-tamper"
                                onClick={() => setTamperOpen((o) => !o)}
                            >
                                Tamper with it
                            </button>
                            {copyHref ? (
                                <button type="button" className={s.quiet} onClick={copyLink}>
                                    {copied ? 'Link copied' : 'Copy link'}
                                </button>
                            ) : null}
                        </div>
                        <div id="seal-tamper" className={s.tamper} hidden={!tamperOpen}>
                            <p className={s.tamperHead}>Change one thing in a copy of the files, then check the copy:</p>
                            <div className={s.chips}>
                                {tamper.map((t) => (
                                    <button
                                        key={t.id}
                                        type="button"
                                        className={s.chip}
                                        aria-pressed={change?.id === t.id}
                                        disabled={busy}
                                        onClick={() => verify(t)}
                                    >
                                        <span>{t.label}</span>
                                        <code>
                                            {t.from} → {t.to}
                                        </code>
                                    </button>
                                ))}
                            </div>
                        </div>

                        <RecordTable seal={seal} files={files} links={links} witnesses={witnesses} witness={witness} live={change ? null : live} authorization={authorization} authorizedLine={authorizedLine} />
                        {commands ? (
                            <details className={s.fields}>
                                <summary>{page.commandsLabel || 'Check the same files on your computer'}</summary>
                                <pre className={s.cmds}>
                                    <code>{commands}</code>
                                </pre>
                            </details>
                        ) : null}
                    </section>

                    <section className={s.stage} ref={stageRef} aria-label="The Seal">
                        <div className={s.tilt}>
                            <SealArt
                                rings={rings}
                                tone={tone}
                                digest={seed}
                                phase={phase}
                                buildMs={BUILD_MS}
                                active={replaying ? null : shown}
                                focusIndex={focusIndex}
                                onActive={onActive}
                                onPick={pick}
                                onKey={onRingKey}
                                ringFocus={ring}
                                witnesses={witnesses}
                                lit={lit}
                                spot={current?.spotlight || null}
                                label={ariaSeal}
                            />
                        </div>

                        <Player
                            frames={frames}
                            frame={frame}
                            playing={playing}
                            speed={speed}
                            busy={busy}
                            rings={rings}
                            onPlay={play}
                            onPause={() => setPlaying(false)}
                            onSeek={seek}
                            onNext={toNextHighlight}
                            onSpeed={setSpeed}
                            onClose={stopReplay}
                        />

                        <div className={s.dock}>
                            <p className={s.stepLine} data-step={step ? step.i : ''} aria-live="polite">
                                {stepLine || (verdict === 'valid' ? '✓ Signature valid over this exact report' : verdict === 'fails' ? `✗ ${reasonText(result?.reason)}` : ' ')}
                            </p>
                            {step ? (
                                <StepPanel
                                    step={step}
                                    rings={rings}
                                    media={media[step.i]}
                                    frame={current}
                                    stopReason={stopReason}
                                    stopNote={stopNote}
                                    closable={!replaying && selected != null}
                                    onClose={() => setSelected(null)}
                                    onLeave={() => setHovered(null)}
                                />
                            ) : null}
                        </div>
                    </section>
                </main>

                <section className={s.mapBand} aria-labelledby="map-h">
                    <div className={s.mapHead}>
                        <h2 id="map-h">The program this run ran</h2>
                        {links.map ? (
                            <Link href={links.map.href} className={s.mapLink}>
                                {links.map.label}
                            </Link>
                        ) : null}
                    </div>
                    <WhenNear minHeight={420}>
                        <SealMap
                            program={program}
                            step={shown}
                            status={step ? (step.status === 'stopped' ? 'stopped' : current?.stage === 'done' ? 'saved' : 'running') : null}
                            onPick={(i) => (replaying ? seek(frameOfStep(frames, i)) : setSelected(i))}
                            anchor={rings.anchor}
                        />
                    </WhenNear>
                </section>

                <footer className={s.foot}>
                    <p>
                        {footer ||
                            "The signature comes from the machine that ran it: it shows the report hasn't changed since the run."}
                    </p>
                    <p className={s.footLinks}>
                        {links.programMap ? <Link href={links.programMap.href}>{links.programMap.label}</Link> : null}
                        <a href={links.docs || 'https://docs.openadapt.ai/reference/receipts-and-seals/'}>How Seals work</a>
                    </p>
                </footer>
            </div>
        </>
    )
}

// Play, pause, the next highlight, speed and a scrubber, all over the one frame index.
function Player({ frames, frame, playing, speed, busy, rings, onPlay, onPause, onSeek, onNext, onSpeed, onClose }) {
    const at = frame ?? 0
    const f = frames[at]
    const t = f ? rings.steps[f.step]?.t0 ?? 0 : 0
    return (
        <div className={s.player} data-active={frame !== null ? '1' : undefined}>
            <button type="button" className={s.pBtn} onClick={playing ? onPause : onPlay} disabled={busy} aria-label={playing ? 'Pause' : 'Play the run'}>
                {playing ? '❚❚' : '▶'}
            </button>
            <button type="button" className={s.pBtn} onClick={onNext} disabled={busy} aria-label="Next highlight" title="Next highlight (Shift + →)">
                ⏭
            </button>
            <input
                className={s.scrub}
                type="range"
                min="0"
                max={frames.length - 1}
                value={at}
                disabled={busy}
                aria-label="Position in the run"
                aria-valuetext={f ? `Step ${f.step + 1} of ${rings.steps.length}` : undefined}
                onChange={(e) => onSeek(Number(e.target.value))}
            />
            <span className={s.pTime}>
                {secs(t)} / {secs(rings.runMs)}
            </span>
            <span className={s.speeds} role="group" aria-label="Speed">
                {SPEEDS.map((x) => (
                    <button key={x} type="button" aria-pressed={speed === x} onClick={() => onSpeed(x)}>
                        {x}×
                    </button>
                ))}
            </span>
            {frame !== null ? (
                <button type="button" className={s.pBtn} onClick={onClose} aria-label="Leave the replay">
                    ×
                </button>
            ) : null}
        </div>
    )
}

// The step being shown, as a compact table, with its read-backs and pictures.
function StepPanel({ step, rings, media, frame, stopReason, stopNote, closable, onClose, onLeave }) {
    const reads = rings.readbacks.filter((r) => r.step === step.i)
    const petal = rings.identity.find((p) => p.step === step.i)
    const n = step.checks.passed + step.checks.failed
    const replay = Boolean(frame)
    const shownReads = replay ? (frame.step === step.i ? frame.readbacks : 0) : reads.length
    const spot = replay ? frame.spotlight : null
    const rows = [
        ['Action', step.name],
        [
            'Result',
            step.status === 'passed' ? '✓ Passed' : step.status === 'stopped' ? `■ Stopped here${stopReason ? `: ${stopReason}` : ''}` : 'Not reached',
            step.status === 'passed' ? 'ok' : step.status === 'stopped' ? 'halt' : 'ghost',
            'stop',
        ],
        petal
            ? ['Right record', petal.result === 'passed' ? '✓ Confirmed before acting' : petal.result === 'not_reached' ? 'Not reached' : '✗ Not confirmed', petal.result === 'passed' ? 'ok' : 'ghost', 'identity']
            : null,
        n ? ['Screen checks', step.checks.failed ? `■ ${step.checks.failed} of ${n} failed` : `✓ ${step.checks.passed} of ${n} passed`, step.checks.failed ? 'halt' : 'ok', 'checks'] : null,
        reads.length ? ['Read back', `${reads.filter((r) => r.result === 'passed').length} of ${reads.length} from the record`, 'ok', 'save'] : null,
    ].filter(Boolean)
    return (
        <aside className={s.inspector} data-status={step.status} data-step={step.i} data-spot={spot || undefined} aria-live="polite" onMouseLeave={onLeave}>
            <div className={s.inMain}>
                <table className={s.kv}>
                    <tbody>
                        {rows.map(([k, v, tone, key]) => (
                            <tr key={k} data-tone={tone} data-spot={spot && spot === key ? '1' : undefined}>
                                <th scope="row">{k}</th>
                                <td>{v}</td>
                            </tr>
                        ))}
                    </tbody>
                </table>
                {spot === 'stop' && stopNote ? <p className={s.spotNote}>{stopNote}</p> : null}
                {media && (media.crop || media.frame) ? (
                <div className={s.shots}>
                    {media.crop ? (
                        <figure>
                            {/* eslint-disable-next-line @next/next/no-img-element */}
                            <img src={media.crop} alt="The step's target as recorded, synthetic data" width="160" height="64" loading="lazy" />
                            <figcaption>Recorded</figcaption>
                        </figure>
                    ) : null}
                    {media.frame ? (
                        <figure>
                            {/* eslint-disable-next-line @next/next/no-img-element */}
                            <img src={media.frame} alt="The screen after this step in this run, synthetic data" width="640" height="400" loading="lazy" />
                            <figcaption>This run</figcaption>
                        </figure>
                    ) : null}
                </div>
            ) : null}
            </div>
            {reads.length ? (
                    <table className={s.readTable} data-count={shownReads}>
                        <caption>Each saved field, read back from the record after the save. Values aren&apos;t shown.</caption>
                        <thead>
                            <tr>
                                <th scope="col">Field</th>
                                <th scope="col">Expected</th>
                                <th scope="col">Read back</th>
                                <th scope="col" aria-label="Result" />
                            </tr>
                        </thead>
                        <tbody>
                            {reads.map((r, k) => {
                                const done = k < shownReads && r.result !== 'not_reached'
                                const record = /record exists/i.test(r.label || '')
                                return (
                                    <tr key={r.i} data-done={done ? '1' : '0'} data-new={replay && k === shownReads - 1 ? '1' : undefined}>
                                        <td>{record ? 'The new record' : (r.label || `Read ${k + 1}`).replace(/ matches$/, '')}</td>
                                        <td>{record ? 'exactly 1' : 'as entered'}</td>
                                        <td>{done ? (record ? '1 found' : 'equal') : '…'}</td>
                                        <td>{done ? (r.result === 'passed' ? '✓' : '✗') : ''}</td>
                                    </tr>
                                )
                            })}
                        </tbody>
                    </table>
                ) : null}
            {closable ? (
                <button type="button" className={s.inClose} onClick={onClose} aria-label="Close the step">
                    ×
                </button>
            ) : null}
        </aside>
    )
}

const utc = (seconds) => new Date(seconds * 1000).toISOString().replace('T', ' ').replace(/\.\d+Z$/, ' UTC')
const isoUtc = (iso) => String(iso).replace('T', ' ').replace(/(\.\d+)?Z$/, ' UTC')
const short = (hex, n = 16) => `${String(hex).slice(0, n)}…`
const WITNESS_REASONS = {
    anchor_mismatch: "The log entry isn't for this Seal.",
    anchor_key_mismatch: "The log entry isn't signed by the Seal's key.",
    anchor_bad_signature: "The log entry's signature doesn't match.",
    anchor_unknown_log: "The entry names a log this site doesn't pin.",
    anchor_bad_timestamp: "The log's signed timestamp doesn't verify.",
    anchor_bad_inclusion: "The inclusion proof doesn't reach the log's tree.",
    anchor_bad_checkpoint: "The log's signed checkpoint doesn't verify.",
    anchor_malformed: "The log entry isn't well formed.",
    tsa_imprint_mismatch: "The timestamp stamps a different digest from this Seal's.",
    tsa_bad_signature: "The timestamp's signature doesn't verify.",
    tsa_untrusted: "The timestamp doesn't chain to the pinned Sigstore root.",
    tsa_expired: "The timestamp's certificate wasn't valid at that time.",
    tsa_not_timestamping: "The signing certificate isn't for time stamping.",
    tsa_content_mismatch: "The timestamp's signed content doesn't match.",
    tsa_malformed: "The timestamp isn't well formed.",
}

// The signed fields and the outside witnesses, one table. A witness the files
// don't have has no row.
function RecordTable({ seal, files, links, witnesses, witness, live, authorization, authorizedLine }) {
    const r = witness?.rekor
    const t = witness?.tsa
    const rows = [
        ['Outcome', <code key="o">{seal.outcome}</code>, null],
        ['Report SHA-256', <code key="d" title={seal.subject_sha256}>{short(seal.subject_sha256, 24)}</code>, null],
        ['Signed by', <code key="k">{seal.key_id}</code>, null],
        ['Signature', <code key="s" title={seal.signature}>{short(seal.signature, 24)}</code>, 'Ed25519'],
        ['Issued', isoUtc(seal.issued_at), null],
        ...(seal.scope_item ? [['Scope', <code key="sc">{seal.scope_item}</code>, null]] : []),
    ]
    if (witnesses?.rekor) {
        rows.push([
            'Public log',
            witnesses.rekor === 'pending' ? (
                'Checking…'
            ) : r?.ok ? (
                <span key="r">
                    Sigstore Rekor entry{' '}
                    <a href={rekorSearchUrl(r.logIndex)} rel="noopener noreferrer">
                        {r.logIndex}
                    </a>
                    , logged {utc(r.integratedTime)}, log <code>{short(r.logId, 8)}</code>. Inclusion proof, signed timestamp and
                    checkpoint checked against the pinned Rekor key.
                    {live === 'match' ? ' Matches the public log.' : live === 'differs' ? ' The public log serves a different entry.' : ''}
                </span>
            ) : (
                <span key="rf">
                    {WITNESS_REASONS[r?.reason] || "The log entry doesn't verify."} <code>{r?.reason}</code>
                </span>
            ),
            witnesses.rekor,
        ])
    }
    if (witnesses?.tsa) {
        rows.push([
            'Timestamp',
            witnesses.tsa === 'pending' ? (
                'Checking…'
            ) : t?.ok ? (
                <span key="t">
                    Sigstore Timestamp Authority (RFC 3161): {isoUtc(t.genTime)}, over this Seal&apos;s SHA-256, chained to the pinned{' '}
                    <a href="https://timestamp.sigstore.dev/api/v1/timestamp/certchain" rel="noopener noreferrer">
                        Sigstore TSA root
                    </a>
                    .
                </span>
            ) : (
                <span key="tf">
                    {WITNESS_REASONS[t?.reason] || "The timestamp doesn't verify."} <code>{t?.reason}</code>
                </span>
            ),
            witnesses.tsa,
        ])
    }
    if (authorization && authorizedLine) {
        rows.push([
            'Authorized',
            <span key="a">
                {authorizedLine}, with an SSH key{' '}
                <a href={`https://github.com/${authorization.login}.keys`} rel="noopener noreferrer">
                    GitHub lists for @{authorization.login}
                </a>
                .
            </span>,
            'pass',
        ])
    }
    return (
        <section className={s.record} aria-labelledby="record-h">
            <h2 id="record-h" className={s.recordH}>
                The signed record
            </h2>
            <table className={s.recordTable}>
                <tbody>
                    {rows.map(([k, v, state]) => (
                        <tr key={k} data-state={state || undefined}>
                            <th scope="row">
                                {state === 'pass' || state === 'fail' || state === 'pending' ? <span className={s.wMark} aria-hidden="true" /> : null}
                                {k}
                            </th>
                            <td>{v}</td>
                        </tr>
                    ))}
                </tbody>
            </table>
            {links.file ? (
                <p className={s.fileLinks}>
                    {['report.json', 'seal.json', files.anchor ? 'seal.anchor.json' : null, files.tsa ? 'seal.tsa.json' : null]
                        .filter(Boolean)
                        .map((name) => [name, links.file(name)])
                        .filter(([, href]) => href)
                        .map(([name, href]) => (
                            <a key={name} href={href}>
                                {name}
                            </a>
                        ))}
                    {links.keys ? <a href={links.keys.href}>{links.keys.label}</a> : null}
                </p>
            ) : null}
        </section>
    )
}
