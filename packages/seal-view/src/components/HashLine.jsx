// The report's SHA-256 as the browser computes it. While the check runs, the
// line starts as the Seal's signature (base64) and resolves, left to right,
// into the digest of the file actually checked. Characters that differ from
// the digest the Seal signed are marked, so a changed copy shows exactly where.
import { useEffect, useRef, useState } from 'react'
import s from './SealPage.module.css'

const HEX = '0123456789abcdef'
const LEN = 64
const RESOLVE_MS = 1100

export default function HashLine({ label, from, to, against, running, still }) {
    const [text, setText] = useState(() => (from || '').slice(0, LEN))
    const [done, setDone] = useState(false)
    const toRef = useRef(to)
    toRef.current = to

    useEffect(() => {
        if (!running) {
            if (to) {
                setText(to)
                setDone(true)
            }
            return undefined
        }
        setDone(false)
        if (still) {
            if (to) {
                setText(to)
                setDone(true)
            }
            return undefined
        }
        const seed = (from || '').slice(0, LEN).padEnd(LEN, '0')
        let frame = 0
        let last = 0
        let resolveStart = null
        const tick = (now) => {
            const target = toRef.current
            if (target && resolveStart == null) resolveStart = now
            const k = resolveStart == null ? 0 : Math.min(1, (now - resolveStart) / RESOLVE_MS)
            if (now - last > 45 || k === 1) {
                last = now
                const fixed = Math.floor(k * LEN)
                let out = ''
                for (let i = 0; i < LEN; i += 1) {
                    if (target && i < fixed) out += target[i]
                    else if (i < fixed + 6 || !target) out += Math.random() < 0.5 ? seed[(i * 7 + Math.floor(now / 60)) % LEN] : HEX[Math.floor(Math.random() * 16)]
                    else out += seed[i]
                }
                setText(out)
            }
            if (k < 1) frame = requestAnimationFrame(tick)
            else setDone(true)
        }
        frame = requestAnimationFrame(tick)
        return () => cancelAnimationFrame(frame)
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [running, still])

    // A digest that arrives after the sweep (or with no animation) shows at once.
    useEffect(() => {
        if (!running && to) {
            setText(to)
            setDone(true)
        }
    }, [to, running])

    const final = done && to
    const matches = final && to === against
    return (
        <div className={s.hashRow} data-state={!final ? 'running' : matches ? 'match' : 'differs'}>
            <span className={s.proofLabel}>
                {label}
                {final ? <b>{matches ? ' · matches' : ' · differs'}</b> : null}
            </span>
            <code className={s.hex} aria-label={final ? `SHA-256 ${to}` : 'Computing the SHA-256'}>
                {final
                    ? [...to].map((ch, i) => (
                          <span key={i} data-diff={ch !== against[i] ? '1' : undefined}>
                              {ch}
                          </span>
                      ))
                    : text}
            </code>
        </div>
    )
}
