// The receipt page's file links (src/lib/safeFileHref.mjs): relative paths and
// https URLs only, never a protocol-relative or backslash form. The same cases
// as openadapt-web tests/safeFileHref.test.js, so the two copies agree.
import { expect, test } from 'vitest'
import { safeHref } from '../src/lib/safeFileHref.mjs'

test('file links: relative paths and https URLs pass; protocol-relative, backslash and other schemes fail', () => {
    for (const ok of ['/receipts/r3/report.json', 'report.json', './seal.json', '../r3/seal.json', 'https://openadapt.ai/receipts/site-keys.json', 'https://openadapt.ai:443/receipts/site-keys.json', 'https://openadapt.ai/a@b'])
        expect(safeHref(ok)).toBe(ok)
    for (const bad of ['//evil.example', '//evil.example/x', '///evil.example', '/\\evil.example', '\\\\evil.example', 'javascript:alert(1)', 'data:text/html,x', 'http://openadapt.ai/x', 'https:///evil', 'https://', 'https://openadapt.ai:@evil.example', 'https://openadapt.ai:.@evil.example', 'https://openadapt.ai:443@evil.example', 'https://openadapt.ai@evil.example', 'https://openadapt.ai:x/', ' /x', '', null, 42])
        expect(safeHref(bad)).toBe(null)
})
