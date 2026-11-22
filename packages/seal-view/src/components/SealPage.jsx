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
// Layout: one screen, like an app. From 900 px wide, the program map fills the
// left pane (drag or arrow-key the divider; "Expand map" gives it the whole
// width), the Seal and the verdict sit top right, and the details sit bottom
// right in four tabs: Summary (the counts and the signature check), Steps
// (every step), Evidence (the picked step: its checks, pictures and
// read-backs) and Raw (the signed fields, the files and the commands). Picking
// a step on the map, the ring or the list opens its evidence. Narrower, the
// page stacks: the Seal and the verdict, then the tabs, with the map as a
// fifth tab. The verdict stays in the top bar at every width and on every
// tab. The URL's fragment keeps the tab and the step (#tab=evidence&step=6),
// so a link can open one step.
//
// Theme: light or dark from the shared semantic roles (--oa-*), which the host
// provides (openadapt.ai's stylesheet; the offline viewer inlines it). The
// visitor's choice is the `data-theme` attribute on <html> and `oa-theme` in
// local storage, the same as openadapt.ai's own switch; with no choice, the
// system setting.
//
// Data: `seal`, the run's props (lib/sealProps.mjs sealPageProps). The host's
// own parts come in `host`, all optional:
//   loadFiles()  the run's files as the check reads them (default: fetch the
//                paths in seal.files, lib/sealFiles.mjs)
//   Link         the host's link component (default: a plain <a>)
//   links        { home: {href, label}, programMap: {href, label}, docs, keys: {href, label},
//                  file(name) -> href of one of the run's files, or null }
//   map          { kicker, title, resolveMedia(ref) } for the program workbench
//   copyHref     the page's own address, for "Copy link" (none: no button)
//   live         ask the network once the offline proof passes: Rekor's live
//                entry and GitHub's key list (default true; the viewer: false)
//   kicker, footer, trustWords (how the verdict names the key: "signed by
//   the pinned key"; the offline viewer: "checked against your computer's
//   signing key"), unchanged ("The published files are unchanged; this was a
//   copy in your browser."), signer ('this_computer' or 'demo'), mediaNote
//   (the caption under a step's screenshots), canonicalOrigin (rewritten to this page's origin
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
import { safeHref } from '../lib/safeFileHref.mjs'
import { sealOutcomeLine } from '../lib/sealOutcome.mjs'
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
    subject_mismatch: "The report doesn't match the SHA-256 the receipt signed.",
    bad_signature: "The Ed25519 signature doesn't match the signed fields.",
    malformed: "The receipt file isn't well formed.",
    unknown_key: "The receipt names a key this site doesn't pin.",
    revoked_key: 'The receipt names a revoked key.',
    key_mismatch: "The key in the receipt isn't the key this site pins.",
    ambiguous_key: 'The pinned key list names this key twice.',
}
const UNKNOWN_SIGNER = new Set(['unknown_key', 'key_mismatch', 'ambiguous_key'])
const reasonText = (reason) =>
    REASONS[reason] ||
    (String(reason).startsWith('anchor_')
        ? "The public log's record doesn't back this signature."
        : String(reason).startsWith('auth')
          ? "The authorization doesn't cover this run."
          : 'The check failed.')

// A host-given file link: a relative path or an https URL, never a javascript: URI.
const secs = (ms) => `${(ms / 1000).toFixed(1)} s`
// Required effect predicates can check record cardinality or individual fields.
// Their count describes checks, never a count of fields or separate reads.
const recordChecksLine = (confirmed, total) => `${confirmed} of ${total} record checks confirmed`
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

function useMediaQuery(query) {
    const [match, setMatch] = useState(false)
    useEffect(() => {
        const mq = window.matchMedia(query)
        const update = () => setMatch(mq.matches)
        update()
        mq.addEventListener('change', update)
        return () => mq.removeEventListener('change', update)
    }, [query])
    return match
}

const THEME_KEY = 'oa-theme'
const storedTheme = () => {
    try {
        const v = window.localStorage.getItem(THEME_KEY)
        return v === 'light' || v === 'dark' ? v : null
    } catch {
        return null
    }
}
const systemTheme = () => (window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark')
const htmlTheme = () => {
    const v = document.documentElement.getAttribute('data-theme')
    return v === 'light' || v === 'dark' ? v : null
}

// The page's theme: <html data-theme> when the host set one, else the saved
// choice, else the system's; it follows the system until the visitor picks.
// `own`: this page owns <html> (its own chrome), so it writes the attribute.
function useTheme(own) {
    const [theme, setTheme] = useState('dark')
    useEffect(() => {
        const resolve = () => htmlTheme() || storedTheme() || systemTheme()
        const apply = () => {
            const next = resolve()
            if (own && !htmlTheme()) document.documentElement.setAttribute('data-theme', next)
            setTheme(next)
        }
        apply()
        const mq = window.matchMedia('(prefers-color-scheme: light)')
        const onSystem = () => {
            if (storedTheme()) return
            if (own) document.documentElement.setAttribute('data-theme', systemTheme())
            apply()
        }
        mq.addEventListener('change', onSystem)
        const mo = new MutationObserver(() => setTheme(resolve()))
        mo.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] })
        return () => {
            mq.removeEventListener('change', onSystem)
            mo.disconnect()
        }
    }, [own])
    const flip = useCallback(() => {
        const next = theme === 'dark' ? 'light' : 'dark'
        document.documentElement.setAttribute('data-theme', next)
        const meta = document.querySelector('meta[name="theme-color"]')
        if (meta) meta.setAttribute('content', next === 'light' ? '#ffffff' : '#05080d')
        try {
            window.localStorage.setItem(THEME_KEY, next)
        } catch {
            // Storage can be refused (a private window); the switch still works for this visit.
        }
        setTheme(next)
    }, [theme])
    return [theme, flip]
}

// The tab and step in the URL's fragment: #tab=evidence&step=6 (step from 1).
const TABS = ['summary', 'steps', 'evidence', 'raw']
const TAB_LABELS = { summary: 'Summary', program: 'Program', steps: 'Steps', evidence: 'Evidence', raw: 'Raw' }
function readFragment(stepCount) {
    const params = new URLSearchParams(window.location.hash.replace(/^#/, ''))
    const tab = params.get('tab')
    const n = Number.parseInt(params.get('step') || '', 10)
    return {
        tab: TABS.includes(tab) || tab === 'program' ? tab : null,
        step: Number.isInteger(n) && n >= 1 && n <= stepCount ? n - 1 : null,
    }
}
function fragmentOf(tab, step) {
    const parts = []
    if (tab && tab !== 'summary') parts.push(`tab=${tab}`)
    if (step != null) parts.push(`step=${step + 1}`)
    return parts.length ? `#${parts.join('&')}` : ''
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

/** The page's title and description, for the host's <head>. */
export function sealPageMeta(page) {
    const c = page.rings.counts
    return {
        title: `${page.label} · ${page.job} · OpenAdapt Receipt`,
        description: `${page.app}, ${page.job}: ${page.label}. ${c.stepsPassed} of ${c.steps} steps, ${c.checksPassed} of ${c.checks} screen checks${c.readbacksPassed ? `, ${c.readbacksPassed} of ${c.readbacks} record checks confirmed` : ''}. A signed run report you can check in your browser.`,
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
        trustWords = 'signed by the pinned key',
        unchanged = 'The published files are unchanged; this was a copy in your browser.',
        canonicalOrigin = null,
        chrome = true,
        map = {},
        // Who signed it, as the host knows from its pinned keys: 'this_computer'
        // only when the host checked against this computer's own key, or 'demo'.
        // Trust-label class: no claim the host didn't make, so the default
        // names no signer.
        signer: signerRole = null,
        // Screenshots are never part of a receipt; a host shows them only from a local run folder.
        mediaNote = "From this computer's run folder: not part of the receipt, never uploaded",
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
    const [anotherOpen, setAnotherOpen] = useState(false)
    const [selected, setSelected] = useState(null)
    const [hovered, setHovered] = useState(null)
    const [focusIndex, setFocusIndex] = useState(0)
    const [ring, setRing] = useState(null)
    const [copied, setCopied] = useState(false)
    const [commands, setCommands] = useState(canonicalCommands)
    const [theme, flipTheme] = useTheme(chrome)
    // The split view from 900 px; below it, one column with the map as a tab.
    const wide = useMediaQuery('(min-width: 900px)')
    const [tab, setTab] = useState('summary')
    const [mapOpened, setMapOpened] = useState(false)
    const [expanded, setExpanded] = useState(false)
    const [split, setSplit] = useState(50)
    const [hashReady, setHashReady] = useState(false)
    const filesRef = useRef(null)
    const stageRef = useRef(null)
    const runId = useRef(0)
    useParallax(stageRef, !reduced)
    const tiltRef = useRef(null)

    // The replay: one index into the run's frames is the only playback state.
    const frames = useMemo(() => replayFrames(rings), [rings])
    const [frame, setFrame] = useState(null) // null: not replaying
    const [playing, setPlaying] = useState(false)
    const [speed, setSpeed] = useState(1)
    const replaying = frame !== null
    const current = replaying ? frames[frame] : null
    const lit = useMemo(() => (current ? litAt(current, rings) : null), [current, rings])

    // The page's own chrome: the theme's canvas under the app, edge to edge.
    useEffect(() => {
        if (!chrome) return undefined
        const { body, documentElement: html } = document
        const prev = [body.style.background, html.style.background]
        body.style.background = 'var(--oa-canvas)'
        html.style.background = 'var(--oa-canvas)'
        return () => {
            body.style.background = prev[0]
            html.style.background = prev[1]
        }
    }, [chrome])

    // The divider's place, kept for this visitor (local storage, if allowed).
    useEffect(() => {
        try {
            const v = Number(window.localStorage.getItem('oa-seal-split'))
            if (v >= 30 && v <= 70) setSplit(v)
        } catch {
            // No storage: the default split.
        }
    }, [])
    const saveSplit = useCallback((v) => {
        const next = Math.max(30, Math.min(70, Math.round(v)))
        setSplit(next)
        try {
            window.localStorage.setItem('oa-seal-split', String(next))
        } catch {
            // No storage: the split holds for this visit.
        }
    }, [])

    // The tab and step from the URL's fragment, on load and when it changes.
    useEffect(() => {
        const load = () => {
            const f = readFragment(rings.steps.length)
            if (f.tab) setTab(f.tab)
            if (f.step != null) {
                setSelected(f.step)
                setFocusIndex(f.step)
                if (!f.tab) setTab('evidence')
            }
        }
        load()
        setHashReady(true)
        window.addEventListener('hashchange', load)
        return () => window.removeEventListener('hashchange', load)
    }, [rings.steps.length])
    useEffect(() => {
        if (tab === 'program') setMapOpened(true)
    }, [tab])

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
        setTab((t) => (t === 'program' ? t : 'evidence'))
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
            // Widgets with their own arrow keys keep them: the tabs, the divider,
            // the step list and the program map.
            const inWidget = Boolean(t?.closest?.('[role="tablist"], [role="separator"], [data-steplist], .opg-root'))
            const onButton = t?.closest?.('button, a, summary')
            if (inWidget && e.key !== 'j' && e.key !== 'k') return
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
            else if ((e.key === 'j' || e.key === 'k') && !e.shiftKey) {
                // j and k: the next or the previous step, from anywhere but a field.
                e.preventDefault()
                const d = e.key === 'j' ? 1 : -1
                if (replaying) stepBy(d)
                else {
                    const n = rings.steps.length
                    const at = selected ?? (d > 0 ? -1 : n)
                    pick(Math.max(0, Math.min(n - 1, at + d)))
                }
            }
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
    // Picking a step (the ring, the map or the list) opens its evidence.
    const pick = useCallback(
        (i, { keepTab = false } = {}) => {
            if (frame !== null) seek(frameOfStep(frames, i))
            else setSelected(i)
            setFocusIndex(i)
            if (!keepTab) setTab((t) => (t === 'program' ? t : 'evidence'))
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

    // The tab and step go into the URL's fragment when the visitor picks them,
    // not on every frame of a replay.
    const viewTab = tab === 'program' && wide ? 'summary' : tab
    const urlStep = replaying ? null : selected
    useEffect(() => {
        if (!hashReady || playing) return
        const next = fragmentOf(viewTab, urlStep)
        if (next === window.location.hash || (!next && !window.location.hash)) return
        try {
            window.history.replaceState(window.history.state, '', `${window.location.pathname}${window.location.search}${next}`)
        } catch {
            // Some file:// contexts refuse it; the page still works.
        }
    }, [viewTab, urlStep, hashReady, playing])

    const copyLink = async () => {
        try {
            await navigator.clipboard.writeText(`${copyHref}${fragmentOf(viewTab, urlStep)}`)
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
        { ring: 'readbacks', big: `${c.readbacksPassed}/${c.readbacks}`, small: c.readbacksFailed ? `record checks, ${c.readbacksFailed} failed` : c.readbacksPassed ? 'record checks confirmed' : 'record checks, not reached' },
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
    const ariaSeal = `The receipt of this run: ${stats.map((l) => `${l.big} ${l.small}`).join('; ')}.`
    const busy = phase === 'verify' || phase === 'build'
    const lineStep = step
    const stepLine = lineStep
        ? `Step ${lineStep.i + 1} of ${rings.steps.length}${lineStep.section ? ` · ${lineStep.section}` : ''}${lineStep.t0 != null ? ` · at ${secs(lineStep.t0)}` : ''}`
        : null
    // The step the Evidence tab shows: the replay's, else the one picked.
    const evidenceIndex = replaying ? current.step : selected
    const evidenceStep = evidenceIndex != null ? rings.steps[evidenceIndex] : null
    const reached = rings.steps.filter((x) => x.status !== 'not_reached').length
    // The verdict in a few words, for the top bar: always in view.
    const chip =
        verdict === null
            ? { state: 'pending', text: phase === 'build' ? 'Assembling' : 'Checking signature' }
            : verdict === 'valid'
              ? { state: 'valid', text: tampered ? 'Restored · signature valid' : 'Signature valid' }
              : verdict === 'fails'
                ? { state: 'fails', text: tampered ? 'Changed copy fails' : 'Signature check fails' }
                : verdict === 'unsupported'
                  ? { state: 'unchecked', text: 'Report matches · signature not checked here' }
                  : { state: 'unchecked', text: 'Not checked here' }
    // Every tab is in the page; CSS hides "Program" from 900 px, where the map has its own pane.
    const allTabs = ['summary', 'program', 'steps', 'evidence', 'raw']
    const tabs = wide ? TABS : allTabs
    const onTabKey = (e) => {
        const i = tabs.indexOf(viewTab)
        const moves = { ArrowRight: 1, ArrowLeft: -1 }
        let next = null
        if (e.key in moves) next = tabs[(i + moves[e.key] + tabs.length) % tabs.length]
        else if (e.key === 'Home') next = tabs[0]
        else if (e.key === 'End') next = tabs[tabs.length - 1]
        else return
        e.preventDefault()
        setTab(next)
        e.currentTarget.querySelector(`[data-tab="${next}"]`)?.focus()
    }
    const appRef = useRef(null)
    // The top bar's height (it wraps on a phone), for the sticky tabs under it.
    const topRef = useRef(null)
    const pageRef = useRef(null)
    useEffect(() => {
        const top = topRef.current
        const root = pageRef.current
        if (!top || !root || typeof ResizeObserver === 'undefined') return undefined
        const ro = new ResizeObserver(() => root.style.setProperty('--top-h', `${top.offsetHeight}px`))
        ro.observe(top)
        return () => ro.disconnect()
    }, [])
    const onSplitKey = (e) => {
        const moves = { ArrowLeft: -2, ArrowRight: 2 }
        if (e.key in moves) saveSplit(split + moves[e.key])
        else if (e.key === 'Home') saveSplit(30)
        else if (e.key === 'End') saveSplit(70)
        else if (e.key === 'Enter') setExpanded((x) => !x)
        else return
        e.preventDefault()
    }
    const onSplitDown = (e) => {
        const box = appRef.current
        if (!box || e.button !== 0) return
        e.preventDefault()
        const el = e.currentTarget
        el.setPointerCapture?.(e.pointerId)
        const r = box.getBoundingClientRect()
        const move = (ev) => saveSplit(((ev.clientX - r.left) / r.width) * 100)
        const up = () => {
            el.removeEventListener('pointermove', move)
            el.removeEventListener('pointerup', up)
            el.removeEventListener('pointercancel', up)
        }
        el.addEventListener('pointermove', move)
        el.addEventListener('pointerup', up)
        el.addEventListener('pointercancel', up)
    }
    const showMap = wide || viewTab === 'program' || mapOpened

    // The signer line, always shown: who signed this receipt, or why it isn't verified.
    const demo = signerRole === 'demo' || seal?.issuer === 'demo'
    const signer =
        verdict === null
            ? { state: 'pending', text: 'Checking who signed it' }
            : verdict === 'valid'
              ? demo
                  ? { state: 'demo', text: 'Demo key: checks only in this demo' }
                  : signerRole === 'this_computer'
                    ? { state: 'signed', text: 'Signed by this computer' }
                    : { state: 'signed', text: 'Signed by a pinned key' }
              : UNKNOWN_SIGNER.has(result?.reason)
                ? { state: 'unknown', text: 'Not verified: unknown signer' }
                : verdict === 'fails'
                  ? { state: 'fails', text: 'Not verified: the signature check failed' }
                  : { state: 'unchecked', text: 'Not verified here' }
    const legacy = typeof seal?.schema === 'string' && /^openadapt\.(local-seal|public-seal)\//.test(seal.schema)

    const verdictText =
        verdict === null ? (
            <span className={s.pending}>{phase === 'build' ? 'Assembling the receipt from the report' : 'Checking the Ed25519 signature in your browser'}</span>
        ) : verdict === 'valid' ? (
            <>
                <b>Valid.</b> This is the report exactly as the run wrote it, {trustWords}.
            </>
        ) : verdict === 'unsupported' ? (
            <>
                <b>The report matches.</b> This browser can&apos;t check Ed25519 signatures; {commands ? 'use the commands under Raw.' : 'check it from a terminal: openadapt verify RUN_FOLDER.'}
            </>
        ) : verdict === 'not_loaded' ? (
            <>
                <b>The files didn&apos;t load,</b> so nothing was checked here. {commands ? 'Use the commands under Raw.' : 'Check it from a terminal: openadapt verify RUN_FOLDER.'}
            </>
        ) : (
            <>
                <b>Fails.</b> {reasonText(result.reason)} <code className={s.code}>{result.reason}</code>
            </>
        )

    return (
        <>
            <a
                className={s.skip}
                href="#main"
                onClick={(e) => {
                    // Keep the fragment's tab and step: move focus, not the URL.
                    e.preventDefault()
                    document.getElementById('main')?.focus()
                }}
            >
                Skip to content
            </a>
            <div
                ref={pageRef}
                className={s.page}
                data-tone={tone}
                data-phase={phase}
                // With its own chrome the page sets the theme on <html> and inherits it,
                // so the server's HTML already has the visitor's theme; embedded, it sets its own.
                data-theme={chrome ? undefined : theme}
                data-layout={wide ? 'split' : 'stack'}
                data-expanded={wide && expanded ? '1' : undefined}
                data-view={viewTab}
                style={{ '--split': `${split}%` }}
            >
                <header className={s.top} ref={topRef}>
                    {links.home ? (
                        <Link href={links.home.href} className={s.home} aria-label={links.home.label}>
                            <OpenAdaptMark className={s.mark} />
                            <span>{links.home.label}</span>
                        </Link>
                    ) : (
                        <span className={s.home}>
                            <OpenAdaptMark className={s.mark} />
                            <span>OpenAdapt</span>
                        </span>
                    )}
                    <span className={s.crumb}>
                        <span>Signed run report</span>
                        {kicker ? <span>{kicker}</span> : null}
                    </span>
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
                                    <span className={s.runSeal} aria-hidden="true">
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
                    <span className={s.topEnd}>
                        <span className={s.chip} data-chip={chip.state} role="status">
                            <span className={s.chipDot} aria-hidden="true" />
                            {chip.text}
                        </span>
                        {copyHref ? (
                            <button type="button" className={s.iconBtn} onClick={copyLink} title="Copy a link to this view" aria-label={copied ? 'Link copied' : 'Copy link'}>
                                <LinkIcon />
                                <span className={s.wideOnly}>{copied ? 'Copied' : 'Copy link'}</span>
                            </button>
                        ) : null}
                        <button
                            type="button"
                            className={s.iconBtn}
                            onClick={flipTheme}
                            aria-label={`Switch to the ${theme === 'dark' ? 'light' : 'dark'} theme`}
                            title={`Switch to the ${theme === 'dark' ? 'light' : 'dark'} theme`}
                        >
                            <ThemeIcon theme={theme} />
                        </button>
                    </span>
                </header>

                <div className={s.app} ref={appRef}>
                    <main id="main" tabIndex={-1} className={s.hero} aria-labelledby="seal-h1">
                        <section className={s.stage} ref={stageRef} aria-label="The receipt" tabIndex={-1} onMouseLeave={() => setHovered(null)}>
                            <div className={s.tilt} ref={tiltRef}>
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
                        </section>
                        <div className={s.heading}>
                            <h1 id="seal-h1" className={s.outcome}>
                                {label}
                            </h1>
                            {stopReason ? <p className={s.reason}>{cap(stopReason)}</p> : null}
                            <p className={s.meta}>
                                <span>{job}</span>
                                <span>{app}</span>
                                {when ? <span>{when}</span> : null}
                            </p>
                            <p className={s.verdict} data-verdict={verdict || 'pending'} aria-live="polite">
                                {verdictText}
                            </p>
                            <p className={s.signer} data-signer={signer.state}>
                                <span>{signer.text}</span>
                            </p>
                            {tampered && verdict === 'fails' ? (
                                <div className={s.changed} data-changed="">
                                    <p className={s.changeHead}>
                                        <b>This changed copy fails:</b> {change.field === 'model_calls' ? 'model calls' : 'outcome'} {change.from} → {change.to}. The receipt no longer matches.
                                    </p>
                                    <p className={s.change}>
                                        Changed in a copy of the {change.target === 'seal' ? 'receipt' : 'report'}: <code>{change.field}</code>{' '}
                                        <del>{change.from}</del> → <ins>{change.to}</ins>.
                                    </p>
                                    <p className={s.change}>{unchanged}</p>
                                    <div className={s.actions}>
                                        <button type="button" className={s.primary} onClick={() => verify(null)} disabled={busy}>
                                            Restore
                                        </button>
                                        <button type="button" className={s.ghost} aria-expanded={anotherOpen} aria-controls="receipt-changes" onClick={() => setAnotherOpen((o) => !o)}>
                                            Try another change
                                        </button>
                                    </div>
                                    <div id="receipt-changes" className={s.another} role="group" aria-label="Other changes" hidden={!anotherOpen}>
                                        {tamper
                                            .filter((t) => t.id !== change.id)
                                            .map((t) => (
                                                <button key={t.id} type="button" className={s.tchip} disabled={busy} onClick={() => verify(t)}>
                                                    <span>{t.label}</span>
                                                    <code>
                                                        {t.from} → {t.to}
                                                    </code>
                                                </button>
                                            ))}
                                    </div>
                                </div>
                            ) : null}
                            <div className={s.actions}>
                                <button type="button" className={s.primary} onClick={playing ? () => setPlaying(false) : play} disabled={busy}>
                                    <span aria-hidden="true">{playing ? '❚❚' : '▶'}</span>
                                    {playing ? 'Pause the run' : replaying ? 'Resume the run' : 'Play the run'}
                                </button>
                                <button type="button" className={s.ghost} onClick={() => verify(null)} disabled={busy}>
                                    Verify
                                </button>
                                {tampered && verdict === 'fails' ? null : (
                                    <button
                                        type="button"
                                        className={s.ghost}
                                        onClick={() => {
                                            setAnotherOpen(false)
                                            verify(tamper[0])
                                        }}
                                        disabled={busy || !tamper.length}
                                    >
                                        Try changing it
                                    </button>
                                )}
                            </div>
                        </div>
                        <div className={s.dock}>
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
                            <p className={s.stepLine} data-step={step ? step.i : ''} aria-live="polite">
                                {stepLine ||
                                    (verdict === 'valid'
                                        ? `✓ Signature valid over this exact report · ${reached} steps`
                                        : verdict === 'fails'
                                          ? `✗ ${reasonText(result?.reason)}`
                                          : ' ')}
                            </p>
                        </div>
                    </main>

                    <section
                        className={s.mapPane}
                        aria-labelledby="map-h"
                        id="seal-panel-program"
                        role={wide ? undefined : 'tabpanel'}
                    >
                        <div className={s.paneHead}>
                            <h2 id="map-h">Program steps for this run</h2>
                            {wide ? (
                                <button type="button" className={s.iconBtn} aria-pressed={expanded} onClick={() => setExpanded((x) => !x)}>
                                    {expanded ? 'Show details' : 'Expand map'}
                                </button>
                            ) : null}
                        </div>
                        {showMap ? (
                            <SealMap
                                program={program}
                                map={{ title: `${job} · ${app}`, ...map }}
                                step={shown}
                                status={step ? (step.status === 'stopped' ? 'stopped' : current?.stage === 'done' ? 'saved' : 'running') : null}
                                onPick={(i) => pick(i)}
                                anchor={rings.anchor}
                                sealRun={{ rings, tone, irisSeed: seed, referral }}
                                sealLine={sealOutcomeLine({ rings, label, stopReason }, verdict === 'valid')}
                                theme={theme}
                                fill={wide}
                            />
                        ) : null}
                    </section>

                    <div
                        className={s.resizer}
                        role="separator"
                        aria-orientation="vertical"
                        aria-label="Resize the program map"
                        aria-valuenow={split}
                        aria-valuemin={30}
                        aria-valuemax={70}
                        tabIndex={0}
                        onKeyDown={onSplitKey}
                        onPointerDown={onSplitDown}
                        onDoubleClick={() => saveSplit(50)}
                        title="Drag, or use the arrow keys. Enter expands the map."
                    />

                    <div className={s.tabs} role="tablist" aria-label="Details" onKeyDown={onTabKey}>
                        {allTabs.map((t) => (
                            <button
                                key={t}
                                type="button"
                                role="tab"
                                id={`seal-tab-${t}`}
                                data-tab={t}
                                aria-selected={viewTab === t}
                                aria-controls={`seal-panel-${t}`}
                                tabIndex={viewTab === t ? 0 : -1}
                                onClick={() => setTab(t)}
                            >
                                {TAB_LABELS[t]}
                                {t === 'steps' ? <span className={s.count}>{rings.steps.length}</span> : null}
                                {t === 'evidence' && evidenceStep ? <span className={s.count}>{evidenceStep.i + 1}</span> : null}
                            </button>
                        ))}
                    </div>

                    <div className={s.panels}>
                        <section id="seal-panel-summary" role="tabpanel" aria-labelledby="seal-tab-summary" hidden={viewTab !== 'summary'} className={s.panel}>
                            <p className={s.line}>{line}</p>
                            {authorization && authorizedLine ? (
                                <p className={s.by}>
                                    <a href={`https://github.com/${authorization.login}`} rel="noopener noreferrer">
                                        {authorizedLine}
                                    </a>
                                </p>
                            ) : null}
                            <dl className={s.stats} aria-label="What the receipt shows">
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
                                {c.modelCalls == null
                                    ? `The program ran for ${secs(rings.runMs)}.`
                                    : `The program ran for ${secs(rings.runMs)} and made ${c.modelCalls} AI model calls.`}
                            </p>

                            <h2 className={s.sub}>The signature check</h2>
                            <div className={s.proof}>
                                <HashLine
                                    label={tampered && change.target === 'report' ? 'This copy of report.json' : 'report.json, in this browser'}
                                    from={seal.signature}
                                    to={digest}
                                    against={seal.subject_sha256}
                                    running={phase === 'verify'}
                                    still={reduced}
                                />
                                <div className={s.signed}>
                                    <span className={s.proofLabel}>Signed in the receipt</span>
                                    <code className={s.hex}>{seal.subject_sha256}</code>
                                </div>
                            </div>

                            <p className={s.foot}>
                                {footer || "The signature comes from the machine that ran it: it shows the report hasn't changed since the run."}{' '}
                                {links.programMap ? <Link href={links.programMap.href}>{links.programMap.label}</Link> : null}{' '}
                                <a href={links.docs || 'https://docs.openadapt.ai/reference/receipts-and-seals/'}>How receipts work</a>
                            </p>
                        </section>

                        <section id="seal-panel-steps" role="tabpanel" aria-labelledby="seal-tab-steps" hidden={viewTab !== 'steps'} className={s.panel}>
                            <StepList rings={rings} active={evidenceIndex} onPick={(i) => pick(i)} onFocusStep={(i) => pick(i, { keepTab: true })} />
                        </section>

                        <section id="seal-panel-evidence" role="tabpanel" aria-labelledby="seal-tab-evidence" hidden={viewTab !== 'evidence'} className={s.panel}>
                            {evidenceStep ? (
                                <>
                                    <div className={s.evHead}>
                                        <p className={s.evTitle}>
                                            <span className={s.evNum}>Step {evidenceStep.i + 1} of {rings.steps.length}</span>
                                            {evidenceStep.section ? <span>{evidenceStep.section}</span> : null}
                                            {evidenceStep.t0 != null ? <span>at {secs(evidenceStep.t0)}</span> : null}
                                        </p>
                                        <span className={s.evNav}>
                                            <button type="button" className={s.iconBtn} onClick={() => (replaying ? stepBy(-1) : pick(Math.max(0, evidenceStep.i - 1)))} disabled={evidenceStep.i === 0} aria-label="Previous step" title="Previous step (k)">
                                                ‹
                                            </button>
                                            <button type="button" className={s.iconBtn} onClick={() => (replaying ? stepBy(1) : pick(Math.min(rings.steps.length - 1, evidenceStep.i + 1)))} disabled={evidenceStep.i === rings.steps.length - 1} aria-label="Next step" title="Next step (j)">
                                                ›
                                            </button>
                                        </span>
                                    </div>
                                    <StepPanel
                                        step={evidenceStep}
                                        rings={rings}
                                        media={media[evidenceStep.i]}
                                        frame={current}
                                        stopReason={stopReason}
                                        stopNote={stopNote}
                                        mediaNote={mediaNote}
                                    />
                                </>
                            ) : (
                                <div className={s.empty}>
                                    <p>Pick a step on the map, on the receipt or in Steps to see its checks{media.some((m) => m && (m.crop || m.frame)) ? ', its pictures' : ''} and its record checks.</p>
                                    {rings.anchor ? (
                                        <button type="button" className={s.ghost} onClick={() => pick(rings.anchor.step)}>
                                            Show the step the receipt covers (step {rings.anchor.step + 1})
                                        </button>
                                    ) : null}
                                </div>
                            )}
                        </section>

                        <section id="seal-panel-raw" role="tabpanel" aria-labelledby="seal-tab-raw" hidden={viewTab !== 'raw'} className={s.panel}>
                            {legacy ? (
                                <p className={s.older} data-format="">
                                    Format: {seal.schema.replace(/^openadapt\./, '')}, still verifies
                                </p>
                            ) : null}
                            <RecordTable seal={seal} files={files} links={links} witnesses={witnesses} witness={witness} live={change ? null : live} authorization={authorization} authorizedLine={authorizedLine} />
                            {commands ? (
                                <>
                                    <h2 className={s.sub}>{page.commandsLabel || 'Check the same files on your computer'}</h2>
                                    {/* Focusable, so the keyboard can scroll it. */}
                                    <pre className={s.cmds} tabIndex={0}>
                                        <code>{commands}</code>
                                    </pre>
                                </>
                            ) : null}
                        </section>
                    </div>
                </div>
            </div>
        </>
    )
}

function LinkIcon() {
    return (
        <svg viewBox="0 0 24 24" width="15" height="15" aria-hidden="true">
            <path d="M10 14a4.5 4.5 0 0 0 6.4 0l3-3a4.5 4.5 0 0 0-6.4-6.4l-1 1M14 10a4.5 4.5 0 0 0-6.4 0l-3 3a4.5 4.5 0 0 0 6.4 6.4l1-1" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
        </svg>
    )
}

function ThemeIcon({ theme }) {
    return theme === 'dark' ? (
        <svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true">
            <circle cx="12" cy="12" r="4.2" fill="none" stroke="currentColor" strokeWidth="1.8" />
            <path d="M12 2.5v2.2M12 19.3v2.2M2.5 12h2.2M19.3 12h2.2M5.3 5.3l1.6 1.6M17.1 17.1l1.6 1.6M5.3 18.7l1.6-1.6M17.1 6.9l1.6-1.6" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
        </svg>
    ) : (
        <svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true">
            <path d="M20 14.5A8.5 8.5 0 0 1 9.5 4a8.5 8.5 0 1 0 10.5 10.5Z" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinejoin="round" />
        </svg>
    )
}

// Every step in one list: its number, name, section, result and checks. One
// row is in the tab order; the arrow keys move between rows and show each
// row's evidence when Enter or a click picks it.
function StepList({ rings, active, onPick, onFocusStep }) {
    const list = useRef(null)
    const [focus, setFocus] = useState(active ?? 0)
    useEffect(() => {
        if (active != null) setFocus(active)
    }, [active])
    const n = rings.steps.length
    const onKey = (e) => {
        const moves = { ArrowDown: 1, ArrowUp: -1 }
        let next = null
        if (e.key in moves) next = Math.max(0, Math.min(n - 1, focus + moves[e.key]))
        else if (e.key === 'Home') next = 0
        else if (e.key === 'End') next = n - 1
        else return
        e.preventDefault()
        setFocus(next)
        onFocusStep(next)
        list.current?.querySelector(`[data-row="${next}"]`)?.focus()
    }
    return (
        <ol className={s.steps} ref={list} data-steplist="" onKeyDown={onKey} aria-label="The run's steps">
            {rings.steps.map((st) => {
                const petal = rings.identity.find((p) => p.step === st.i)
                const reads = rings.readbacks.filter((r) => r.step === st.i)
                const checks = st.checks.passed + st.checks.failed
                return (
                    <li key={st.i}>
                        <button
                            type="button"
                            data-row={st.i}
                            data-status={st.status}
                            aria-current={active === st.i ? 'step' : undefined}
                            tabIndex={focus === st.i ? 0 : -1}
                            onClick={() => onPick(st.i)}
                        >
                            <span className={s.sNum}>{String(st.i + 1).padStart(2, '0')}</span>
                            <span className={s.sMark} aria-hidden="true">
                                {st.status === 'passed' ? '✓' : st.status === 'stopped' ? '■' : '·'}
                            </span>
                            <span className={s.sName}>
                                {st.name}
                                {st.section ? <small>{st.section}</small> : null}
                            </span>
                            <span className={s.sTags}>
                                {petal ? <span data-ok={petal.result === 'passed' ? '1' : '0'}>record</span> : null}
                                {checks ? <span data-ok={st.checks.failed ? '0' : '1'}>{checks} {checks === 1 ? 'check' : 'checks'}</span> : null}
                                {reads.length ? <span data-ok={reads.some((r) => r.result === 'failed') ? '0' : '1'}>{reads.length} record {reads.length === 1 ? 'check' : 'checks'}</span> : null}
                            </span>
                            <span className={s.srOnly}>
                                {st.status === 'passed' ? ', passed' : st.status === 'stopped' ? ', stopped here' : ', not reached'}
                                {petal ? ', checks the right record' : ''}
                            </span>
                        </button>
                    </li>
                )
            })}
        </ol>
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
function StepPanel({ step, rings, media, frame, stopReason, stopNote, mediaNote }) {
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
        reads.length
            ? [
                  'Read-back',
                  recordChecksLine(reads.filter((r) => r.result === 'passed').length, reads.length),
                  reads.some((r) => r.result === 'failed') ? 'halt' : reads.every((r) => r.result === 'passed') ? 'ok' : 'ghost',
                  'save',
              ]
            : null,
    ].filter(Boolean)
    return (
        <aside className={s.inspector} data-status={step.status} data-step={step.i} data-spot={spot || undefined} aria-live="polite">
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
                    {mediaNote ? <p className={s.shotsNote} data-media-note="">{mediaNote}</p> : null}
                </div>
            ) : null}
            </div>
            {reads.length ? (
                    <table className={s.readTable} data-count={shownReads}>
                        <caption>Each saved field was read back from the record after the save. The values aren&apos;t published.</caption>
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
                                    <tr key={r.i} data-done={done ? '1' : '0'} data-result={done ? r.result : undefined} data-new={replay && k === shownReads - 1 ? '1' : undefined}>
                                        <td>{record ? 'The new record' : (r.label || `Read ${k + 1}`).replace(/ matches$/, '')}</td>
                                        <td>{record ? 'exactly 1' : 'as entered'}</td>
                                        <td>{done ? (r.result === 'passed' ? (record ? '1 found' : 'equal') : record ? 'not exactly 1' : 'not equal') : '…'}</td>
                                        <td>{done ? (r.result === 'passed' ? '✓' : '✗') : ''}</td>
                                    </tr>
                                )
                            })}
                        </tbody>
                    </table>
                ) : null}
        </aside>
    )
}

const utc = (seconds) => new Date(seconds * 1000).toISOString().replace('T', ' ').replace(/\.\d+Z$/, ' UTC')
const isoUtc = (iso) => String(iso).replace('T', ' ').replace(/(\.\d+)?Z$/, ' UTC')
const short = (hex, n = 16) => `${String(hex).slice(0, n)}…`
const WITNESS_REASONS = {
    anchor_mismatch: "The log entry isn't for this receipt.",
    anchor_key_mismatch: "The log entry isn't signed by the receipt's key.",
    anchor_bad_signature: "The log entry's signature doesn't match.",
    anchor_unknown_log: "The entry names a log this site doesn't pin.",
    anchor_bad_timestamp: "The log's signed timestamp doesn't verify.",
    anchor_bad_inclusion: "The inclusion proof doesn't reach the log's tree.",
    anchor_bad_checkpoint: "The log's signed checkpoint doesn't verify.",
    anchor_malformed: "The log entry isn't well formed.",
    tsa_imprint_mismatch: "The timestamp stamps a different digest from this receipt's.",
    tsa_bad_signature: "The timestamp's signature doesn't verify.",
    tsa_untrusted: "The timestamp doesn't chain to the pinned Sigstore root.",
    tsa_expired: "The timestamp's certificate wasn't valid at that time.",
    tsa_not_timestamping: "The signing certificate isn't for time stamping.",
    tsa_content_mismatch: "The timestamp's signed content doesn't match.",
    tsa_malformed: "The timestamp isn't well formed.",
    tsa_token_missing: "The timestamp's token file is missing.",
    tsa_token_mismatch: "The timestamp's token file isn't the one its record names.",
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
                    , logged {utc(r.integratedTime)}, log <code>{short(r.logId, 8)}</code>. The inclusion proof, signed timestamp and
                    checkpoint verify against the pinned Rekor key.
                    {live === 'match' ? ' This entry matches the public log.' : live === 'differs' ? ' The public log serves a different entry.' : ''}
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
                    Sigstore Timestamp Authority (RFC 3161): {isoUtc(t.genTime)}, over this receipt&apos;s SHA-256, chained to the pinned{' '}
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
                    {['report.json', 'seal.json', files.anchor ? 'seal.anchor.json' : null, files.tsa ? 'seal.tsa.json' : null, files.tsr ? 'seal.tsr' : null]
                        .filter(Boolean)
                        .map((name) => [name, safeHref(links.file(name))])
                        .filter(([, href]) => href)
                        .map(([name, href]) => (
                            <a key={name} href={href}>
                                {name}
                            </a>
                        ))}
                    {links.keys && safeHref(links.keys.href) ? <a href={safeHref(links.keys.href) /* nosemgrep: react-href-var -- safeHref allows only a relative path or an https URL. */}>{links.keys.label}</a> : null}
                </p>
            ) : null}
        </section>
    )
}
