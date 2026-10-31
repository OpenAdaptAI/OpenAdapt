// A small, live Seal that links to its run's page: the same marks as the page
// (./SealArt.jsx), drawn bolder. It assembles when `play` turns true, in the
// order the run made its marks, and then rests. openadapt.ai uses it at the
// end of the home page's program map and in the home page's preview. `as`
// renders the link with the host's own link component (Next's Link, say).
import SealArt from './SealArt.jsx'
import s from './SealEmblem.module.css'

export default function SealEmblem({ seal, play, buildMs = 2400, kicker, children, linkRef, size = 'md', as: Link = 'a' }) {
    return (
        <Link
            ref={linkRef}
            href={seal.href /* nosemgrep: react-href-var -- sealPagePath() builds /seals/ plus 16 hex digits of the Seal's SHA-256. */}
            className={s.emblem}
            data-tone={seal.tone}
            data-play={play ? '1' : undefined}
            data-size={size}
        >
            <span className={s.art}>
                {play ? (
                    <SealArt rings={seal.rings} tone={seal.tone} digest={seal.digest} variant="emblem" phase="build" buildMs={buildMs} label="" />
                ) : null}
            </span>
            <span className={s.words}>
                {kicker ? <span className={s.kicker}>{kicker}</span> : null}
                {children}
            </span>
        </Link>
    )
}
