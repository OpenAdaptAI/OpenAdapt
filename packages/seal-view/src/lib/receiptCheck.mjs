// The whole check behind a receipt's Verify button, in the order a reader
// would ask, each step the engine's own rule:
//   1. is this the report the run signed (the Seal over the exact report.json
//      shown, lib/localSeal.mjs);
//   2. did a person authorize this program and run (the SSH-signed run
//      authorization and its fit to this run, lib/runAuthorization.mjs), and
//      is the authorization in the public log;
//   3. is the run's Seal in the public log (lib/sealAnchor.mjs);
// and, from the two log times, whether the authorization came before the Seal.
// The first failure stops the check and names its reason. Pure: it takes the
// files' contents, so the page and the tests run the same code.
import {
    base64Bytes,
    duplicateKey,
    registryKeys,
    verifySealFile,
} from './localSeal.mjs'
import {
    authorizationTiming,
    authorizationVsSeal,
    checkRun,
    verifyAuthorization,
} from './runAuthorization.mjs'
import {
    REKOR_PUBLIC_KEY_PEM,
    logKeyFromPem,
    verifyAnchor,
    verifyRecordAnchor,
} from './sealAnchor.mjs'

/** Whether an authorization's log anchor is by one of the Seal keys it names (seal_key_ids). */
export const authAnchorInScope = (statement, anchor) =>
    Boolean(anchor && statement?.scope?.seal_key_ids?.includes(anchor.key_id))

function parseStrict(text) {
    if (text == null || duplicateKey(text) !== null) return null
    try {
        return JSON.parse(text)
    } catch {
        return null
    }
}

/**
 * files: { report: Uint8Array, sealText, anchorText?,
 *          auth?: { statement, sig, signers, anchorText? } }
 * keys: the pinned Seal key registry. logKeys: pinned log keys (default: Rekor).
 * Returns { ok, reason, steps, subject, seal, authorizedBy, timing }.
 */
export async function checkReceipt(
    files,
    keys,
    { requireAnchor = false, requireAuthorization = false, logKeys = null } = {}
) {
    const sealResult = await verifySealFile(
        files.sealText,
        files.report,
        keys,
        'report.json'
    )
    let steps = [...sealResult.steps]
    const result = (ok, reason, extra = {}) => ({
        ...sealResult,
        ok,
        reason,
        steps,
        ...extra,
    })
    if (!sealResult.ok) return result(false, sealResult.reason)
    const logs = logKeys || [await logKeyFromPem(REKOR_PUBLIC_KEY_PEM)]
    const pinnedRaw = (keyId) => {
        const k = registryKeys(keys).filter(
            (x) => x.key_id === keyId && x.status === 'active'
        )
        return k.length === 1 ? base64Bytes(k[0].public_key) : null
    }

    let authorizedBy = null
    let authTime = null
    let startedAt = null
    if (files.auth) {
        const a = await verifyAuthorization(files.auth.statement, files.auth.sig)
        steps = [...steps, ...a.steps]
        if (!a.ok) return result(false, a.reason)
        let report = null
        try {
            report = JSON.parse(new TextDecoder().decode(files.report))
        } catch {
            report = null
        }
        startedAt = report?.started_at ?? null
        const fit = checkRun(sealResult.seal, report, a)
        const fitStep = {
            auth_not_referenced: 'authRef',
            auth_reference_mismatch: 'authRef',
            auth_scope_mismatch: 'authScope',
            auth_bundle_mismatch: 'authBundle',
        }
        if (fit) {
            const failed = fitStep[fit] || 'authRef'
            for (const id of ['authRef', 'authScope', 'authBundle']) {
                if (id === failed) break
                steps = [...steps, { id, result: 'pass' }]
            }
            steps = [...steps, { id: failed, result: 'fail' }]
            return result(false, fit)
        }
        steps = [
            ...steps,
            { id: 'authRef', result: 'pass' },
            { id: 'authScope', result: 'pass' },
            { id: 'authBundle', result: 'pass' },
        ]
        // Who holds the key is checked apart (checkGithubIdentity); here, only the key.
        authorizedBy = {
            login: a.login,
            fingerprint: a.fingerprint,
            sha256: a.sha256,
            publicKey: a.publicKey,
        }
        if (files.auth.anchorText != null) {
            const anchor = parseStrict(files.auth.anchorText)
            // The authorization's anchor must be by a Seal key it names.
            const inScope = authAnchorInScope(a.statement, anchor)
            const raw = inScope ? pinnedRaw(anchor.key_id) : null
            const r = !anchor
                ? { ok: false, reason: 'anchor_malformed' }
                : !raw
                  ? { ok: false, reason: 'anchor_key_mismatch' }
                  : await verifyRecordAnchor(
                        {
                            run_authorization_sha256: a.sha256,
                            signature_sha256: a.signatureSha256,
                        },
                        anchor,
                        raw,
                        logs
                    )
            steps = [
                ...steps,
                { id: 'authLogged', result: r.ok ? 'pass' : 'fail' },
            ]
            if (!r.ok)
                return result(false, `authorization_${r.reason}`, {
                    authorizedBy,
                })
            authTime = r.integratedTime
        }
    } else if (requireAuthorization) {
        steps = [...steps, { id: 'authShape', result: 'fail' }]
        return result(false, 'auth_missing')
    }

    if (files.anchorText == null) {
        const timing = authorizationTiming(authTime, startedAt, null)
        if (!requireAnchor) return result(true, null, { authorizedBy, timing })
        steps = [...steps, { id: 'anchorEntry', result: 'fail' }]
        return result(false, 'anchor_missing', { authorizedBy, timing })
    }
    const anchor = parseStrict(files.anchorText)
    if (!anchor) {
        steps = [...steps, { id: 'anchorEntry', result: 'fail' }]
        return result(false, 'anchor_malformed', { authorizedBy })
    }
    const raw = pinnedRaw(sealResult.seal.key_id)
    const a = await verifyAnchor(sealResult.seal, anchor, raw, logs)
    steps = [...steps, ...a.steps]
    const timing = authorizationTiming(authTime, startedAt, a.ok ? a.integratedTime : null)
    const vsSeal = authorizationVsSeal(authTime, a.ok ? a.integratedTime : null)
    return result(a.ok, a.reason, {
        authorizedBy,
        timing,
        authAnchorTime: authTime,
        authorizationVsSeal: vsSeal,
        anchor: a.ok
            ? {
                  uuid: a.uuid,
                  logIndex: a.logIndex,
                  integratedTime: a.integratedTime,
                  logId: a.logId,
              }
            : null,
    })
}
