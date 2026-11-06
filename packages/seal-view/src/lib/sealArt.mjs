// The Seal's drawing, as plain geometry on a 600 x 600 canvas, from the rings
// of one run (lib/sealRings.mjs). The page (components/seal/SealArt.js), the
// program map's emblem and the social images (scripts/render-seal-cards.mjs)
// all draw from this, so they show the same marks.
//
// From the outside in: the program's sections; one arc per program step; one
// tick per screen check, inside its step's arc; one dot per read of the saved
// record; one short arc per right-record check; and the core, whose fine
// pattern is drawn from the report's SHA-256, so a changed report draws a
// different pattern.

export const SIZE = 600
export const C = SIZE / 2
export const R = {
    section: 272,
    step: 238,
    stepWidth: 15,
    tickIn: 205,
    tickOut: 217,
    tickFailIn: 199,
    tickFailOut: 223,
    dot: 176,
    dotSize: 4.6,
    petal: 128,
    petalWidth: 6,
    core: 94,
}

const round = (n) => Math.round(n * 100) / 100

/** A point at `deg` degrees clockwise from the top, `r` from the centre. */
export function polar(r, deg) {
    const a = (deg * Math.PI) / 180
    return [round(C + r * Math.sin(a)), round(C - r * Math.cos(a))]
}

/** A circular arc path from a0 to a1 degrees (clockwise from the top). */
export function arc(r, a0, a1) {
    const [x0, y0] = polar(r, a0)
    const [x1, y1] = polar(r, a1)
    const large = a1 - a0 > 180 ? 1 : 0
    return `M${x0} ${y0}A${r} ${r} 0 ${large} 1 ${x1} ${y1}`
}

// A small deterministic spread for each piece when the Seal breaks, so the
// same tamper breaks it the same way every time.
function jitter(i, salt) {
    const x = Math.sin(i * 12.9898 + salt * 78.233) * 43758.5453
    return x - Math.floor(x)
}

function breakVector(deg, i, salt, reach) {
    const a = (deg * Math.PI) / 180
    const push = reach * (0.5 + jitter(i, salt))
    const side = (jitter(i, salt + 1) - 0.5) * reach * 0.8
    return {
        bx: round(Math.sin(a) * push + Math.cos(a) * side),
        by: round(-Math.cos(a) * push + Math.sin(a) * side),
        br: round((jitter(i, salt + 2) - 0.5) * 16),
    }
}

/**
 * Everything to draw, with each piece's angle as a fraction of the circle
 * (`at`, for the verification sweep) and its time in the run (`t`, in ms).
 */
export function sealArt(rings) {
    const n = rings.steps.length
    const per = 360 / n
    const gap = Math.min(1.4, per * 0.12)
    const span = (i) => [i * per + gap / 2, (i + 1) * per - gap / 2]

    const steps = rings.steps.map((s, i) => {
        const [a0, a1] = span(i)
        return {
            i,
            d: arc(R.step, a0, a1),
            hit: arc(R.step, i * per, (i + 1) * per),
            status: s.status,
            at: round((a0 + a1) / 2 / 360),
            t: s.t0,
            ...breakVector((a0 + a1) / 2, i, 1, 26),
        }
    })

    // Section bands: the arc over each run of steps in one section, with its name.
    const sections = []
    rings.steps.forEach((s, i) => {
        const last = sections.at(-1)
        if (last && last.title === s.section) last.to = i
        else sections.push({ title: s.section, from: i, to: i })
    })
    const bands = sections
        .filter((b) => b.title)
        .map((b, k) => {
            const a0 = b.from * per + gap
            const a1 = (b.to + 1) * per - gap
            const mid = (a0 + a1) / 2
            // Names on the lower half read left to right along the bottom. The
            // name's path is wider than a short band, so a long name never clips.
            const flip = mid > 90 && mid < 270
            const half = Math.max((a1 - a0) / 2, 40)
            return {
                k,
                title: b.title,
                from: b.from,
                to: b.to,
                d: arc(R.section, a0, a1),
                text: flip
                    ? reverseArc(R.section + 9, mid - half, mid + half)
                    : arc(R.section - 4, mid - half, mid + half),
                mark: [polar(R.step + 12, b.from * per), polar(R.step + 20, b.from * per)],
                at: round(a0 / 360),
            }
        })

    const ticks = []
    rings.steps.forEach((s, i) => {
        const own = rings.checks.filter((c) => c.step === i)
        const [a0, a1] = span(i)
        own.forEach((c, j) => {
            const deg = a0 + ((j + 1) / (own.length + 1)) * (a1 - a0)
            const failed = c.result !== 'passed'
            const [x1, y1] = polar(failed ? R.tickFailIn : R.tickIn, deg)
            const [x2, y2] = polar(failed ? R.tickFailOut : R.tickOut, deg)
            ticks.push({
                i: ticks.length,
                step: i,
                x1,
                y1,
                x2,
                y2,
                result: c.result,
                at: round(deg / 360),
                t: c.t,
                ...breakVector(deg, ticks.length, 2, 18),
            })
        })
    })

    const nd = rings.readbacks.length
    const dots = rings.readbacks.map((r, k) => {
        const deg = (k / nd) * 360
        const [cx, cy] = polar(R.dot, deg)
        return {
            i: k,
            cx,
            cy,
            result: r.result,
            label: r.label,
            at: round(deg / 360),
            t: r.t,
            ...breakVector(deg, k, 3, 16),
        }
    })

    const np = rings.identity.length
    const pp = 360 / Math.max(1, np)
    const pg = Math.min(4, pp * 0.28)
    const petals = rings.identity.map((p, k) => {
        const a0 = k * pp + pg / 2
        const a1 = (k + 1) * pp - pg / 2
        return {
            i: k,
            step: p.step,
            d: arc(R.petal, a0, a1),
            result: p.result,
            at: round((a0 + a1) / 2 / 360),
            t: p.t,
            ...breakVector((a0 + a1) / 2, k, 4, 12),
        }
    })

    // The stop: a bar across the step ring at the end of the step the run stopped at.
    const stop =
        rings.stoppedAt == null
            ? null
            : (() => {
                  const deg = (rings.stoppedAt + 1) * per
                  const [x1, y1] = polar(R.step - 24, deg)
                  const [x2, y2] = polar(R.step + 18, deg)
                  return { x1, y1, x2, y2, deg: round(deg), at: round(deg / 360) }
              })()

    return { steps, bands, ticks, dots, petals, stop, per }
}

function reverseArc(r, a0, a1) {
    const [x0, y0] = polar(r, a1)
    const [x1, y1] = polar(r, a0)
    const large = a1 - a0 > 180 ? 1 : 0
    return `M${x0} ${y0}A${r} ${r} 0 ${large} 0 ${x1} ${y1}`
}

/** Bytes of a hex string (a SHA-256), for the core's pattern. */
function hexBytes(hex) {
    const out = []
    for (let i = 0; i + 1 < (hex || '').length; i += 2)
        out.push(parseInt(hex.slice(i, i + 2), 16) || 0)
    while (out.length < 8) out.push(0)
    return out
}

/**
 * The core's fine rosette, drawn from a SHA-256: the same digest always draws
 * the same pattern, and any other digest draws a visibly different one.
 */
export function rosette(hex, { radius = R.core - 12, strands = 9, points = 180 } = {}) {
    const b = hexBytes(hex)
    const n1 = 5 + (b[0] % 6)
    const n2 = 11 + (b[1] % 9)
    const a1 = 0.1 + (b[2] / 255) * 0.12
    const a2 = 0.05 + (b[3] / 255) * 0.08
    const ph = (b[4] / 255) * Math.PI * 2
    const twist = 0.4 + (b[5] / 255) * 0.9
    const paths = []
    for (let s = 0; s < strands; s += 1) {
        const rot = (s / strands) * ((Math.PI * 2) / n1)
        let d = ''
        for (let k = 0; k <= points; k += 1) {
            const t = (k / points) * Math.PI * 2
            const rr =
                radius *
                (1 - a1 - a2 + a1 * Math.cos(n1 * t) + a2 * Math.cos(n2 * t + ph))
            const ang = t + rot + twist * Math.sin(n1 * t) * 0.08
            const x = round(C + rr * Math.cos(ang))
            const y = round(C + rr * Math.sin(ang))
            d += `${k ? 'L' : 'M'}${x} ${y}`
        }
        paths.push(`${d}Z`)
    }
    return paths
}

/**
 * The Seal's iris: three interleaved layers of dots, guilloché-style, drawn
 * from a digest (the page uses SHA-256 of the report's digest and the Seal's
 * own digest). The digest's bytes set each layer's petal count, phase,
 * radius, depth, twist and dot density, so every Seal has its own iris and
 * the same Seal always draws the same one. `disorder` scatters the dots
 * (a Seal that fails its check). Returns layers of petals, each petal one SVG
 * path of small circles, in a box of side 2 * radius.
 */
export function iris(hex, { radius = 100, disorder = 0 } = {}) {
    // Every byte of the digest seeds the parameters (FNV-1a, then mulberry32),
    // so a change anywhere in the digest draws another iris.
    let h = 2166136261
    for (let i = 0; i + 1 < (hex || '').length; i += 2) {
        h ^= parseInt(hex.slice(i, i + 2), 16) || 0
        h = Math.imul(h, 16777619) >>> 0
    }
    const next = () => {
        h = (h + 0x6d2b79f5) >>> 0
        let t = h
        t = Math.imul(t ^ (t >>> 15), t | 1)
        t ^= t + Math.imul(t ^ (t >>> 7), t | 61)
        return ((t ^ (t >>> 14)) >>> 0) % 256
    }
    const b = Array.from({ length: 32 }, next)
    const layers = []
    for (let l = 0; l < 3; l += 1) {
        const o = 1 + l * 8
        const petals = 5 + (b[o] % 9)
        const phase = (b[o + 1] / 255) * Math.PI * 2
        const r = radius * (0.5 + 0.2 * l + (b[o + 2] / 255) * 0.08)
        const depth = 0.16 + (b[o + 3] / 255) * 0.24
        const twist = (b[o + 4] / 255 - 0.5) * 0.7
        const size = 1.2 + (b[o + 6] / 255) * 0.8
        // Dots at equal distances along each petal's curve, so each petal reads as
        // one dotted line; the digest sets the spacing.
        const spacing = size * (2.4 + (b[o + 5] / 255) * 1.6)
        const curve = (t) => {
            const rr = r * (1 - depth + depth * Math.cos(petals * t + phase))
            const ang = t + twist * Math.sin(petals * t)
            return [radius + rr * Math.cos(ang), radius + rr * Math.sin(ang)]
        }
        const list = []
        let dots = 0
        for (let p = 0; p < petals; p += 1) {
            let d = ''
            let carry = spacing / 2
            let prev = curve((p / petals) * Math.PI * 2)
            let n = 0
            for (let k = 1; k <= 240; k += 1) {
                const t = ((p + k / 240) / petals) * Math.PI * 2
                const pt = curve(t)
                let seg = Math.hypot(pt[0] - prev[0], pt[1] - prev[1])
                while (seg >= carry) {
                    const f = 1 - (seg - carry) / Math.max(1e-9, Math.hypot(pt[0] - prev[0], pt[1] - prev[1]))
                    let x = prev[0] + (pt[0] - prev[0]) * f
                    let y = prev[1] + (pt[1] - prev[1]) * f
                    if (disorder) {
                        x += (jitter(p * 97 + n, l + 11) - 0.5) * 5 * disorder
                        y += (jitter(p * 89 + n, l + 23) - 0.5) * 5 * disorder
                    }
                    x = round(x)
                    y = round(y)
                    d += `M${round(x - size)} ${y}a${round(size)} ${round(size)} 0 1 0 ${round(size * 2)} 0a${round(size)} ${round(size)} 0 1 0 ${round(-size * 2)} 0`
                    n += 1
                    seg -= carry
                    carry = spacing
                }
                carry -= seg
                prev = pt
            }
            dots = Math.max(dots, n)
            list.push({ d, at: round(p / petals) })
        }
        layers.push({ petals: list, count: petals, dots, dir: (l % 2 ? -1 : 1) * (b[0] % 2 ? 1 : -1) })
    }
    return layers
}

export const TONE = {
    ok: { main: '#4cf0b8', soft: '#9ff7d9', glow: 'rgba(76,240,184,0.32)' },
    halt: { main: '#ffb547', soft: '#ffd99c', glow: 'rgba(255,181,71,0.30)' },
    fail: { main: '#ff4d6a', soft: '#ff9aab', glow: 'rgba(255,77,106,0.30)' },
}

// SVG markup for the PNG renderers, one element at a time, with every attribute
// and text value escaped.
const esc = (s) =>
    String(s).replace(
        /[&<>"']/g,
        (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]
    )
const OPEN = String.fromCharCode(60)
export function el(name, attrs = {}, inner = null) {
    const a = Object.entries(attrs)
        .filter(([, v]) => v != null)
        .map(([k, v]) => ` ${k}="${esc(v)}"`)
        .join('')
    return inner == null ? `${OPEN}${name}${a}/>` : `${OPEN}${name}${a}>${inner}${OPEN}/${name}>`
}
export const textNode = (value) => esc(value)

/**
 * The finished Seal as standalone SVG markup (no animation), for the social
 * images. `tone` is 'ok' or 'halt'; `digest` draws the core.
 */
export function sealSvg(rings, { tone, digest, x = 0, y = 0, size = SIZE, labels = true }) {
    const art = sealArt(rings)
    const t = TONE[tone]
    const ghost = 'rgba(255,255,255,0.08)'
    const on = (status) => (status === 'passed' ? t.main : status === 'stopped' ? TONE.halt.main : ghost)
    const ring = (r, stroke) => el('circle', { cx: C, cy: C, r, fill: 'none', stroke })
    const parts = [ring(R.step + 34, 'rgba(255,255,255,0.06)'), ring(R.dot + 16, 'rgba(255,255,255,0.05)'), ring(R.petal + 18, 'rgba(255,255,255,0.05)')]
    for (const b of art.bands) {
        parts.push(el('path', { d: b.d, fill: 'none', stroke: 'rgba(255,255,255,0.14)', 'stroke-width': 1 }))
        parts.push(el('line', { x1: b.mark[0][0], y1: b.mark[0][1], x2: b.mark[1][0], y2: b.mark[1][1], stroke: 'rgba(255,255,255,0.35)' }))
        if (labels) {
            parts.push(el('path', { id: `b${b.k}`, d: b.text, fill: 'none' }))
            const path = el('textPath', { 'xlink:href': `#b${b.k}`, href: `#b${b.k}`, startOffset: '50%', 'text-anchor': 'middle' }, textNode(b.title.toUpperCase()))
            parts.push(el('text', { 'font-family': 'SFMono-Regular, Menlo, Consolas, monospace', 'font-size': 10, 'letter-spacing': 2.4, fill: 'rgba(255,255,255,0.5)' }, path))
        }
    }
    for (const st of art.steps)
        parts.push(el('path', { d: st.d, fill: 'none', stroke: on(st.status), 'stroke-width': R.stepWidth, 'stroke-linecap': 'butt' }))
    for (const k of art.ticks)
        parts.push(el('line', { x1: k.x1, y1: k.y1, x2: k.x2, y2: k.y2, stroke: k.result === 'passed' ? t.soft : TONE.halt.main, 'stroke-width': k.result === 'passed' ? 1.6 : 2.6 }))
    for (const d of art.dots)
        parts.push(
            d.result === 'passed'
                ? el('circle', { cx: d.cx, cy: d.cy, r: R.dotSize, fill: t.main })
                : el('circle', { cx: d.cx, cy: d.cy, r: R.dotSize, fill: 'none', stroke: ghost, 'stroke-width': 1.4 })
        )
    for (const p of art.petals) parts.push(el('path', { d: p.d, fill: 'none', stroke: on(p.result), 'stroke-width': R.petalWidth }))
    parts.push(el('circle', { cx: C, cy: C, r: R.core, fill: '#060a0e', stroke: 'rgba(255,255,255,0.1)' }))
    for (const d of rosette(digest)) parts.push(el('path', { d, fill: 'none', stroke: t.main, 'stroke-opacity': 0.42, 'stroke-width': 0.6 }))
    if (art.stop) {
        const st = art.stop
        parts.push(el('line', { x1: st.x1, y1: st.y1, x2: st.x2, y2: st.y2, stroke: TONE.halt.main, 'stroke-width': 5, 'stroke-linecap': 'round' }))
    }
    parts.push(
        tone === 'ok'
            ? el('path', { d: `M${C - 22} ${C + 1}l15 15 31-33`, fill: 'none', stroke: t.soft, 'stroke-width': 7, 'stroke-linecap': 'round', 'stroke-linejoin': 'round' })
            : el('rect', { x: C - 15, y: C - 15, width: 30, height: 30, rx: 4, fill: t.soft })
    )
    return el('g', { transform: `translate(${x} ${y}) scale(${size / SIZE})` }, parts.join(''))
}
