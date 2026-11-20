# Vendored widget fixtures

These assets are used only by the synthetic choice-widget benchmark. Existing
JavaScript and stylesheet bytes are unchanged by the notice correction.

## Exact upstream notices

The notice files are byte-for-byte copies of the full upstream licenses:

| Library | Version | License | Notice | SHA-256 |
| --- | --- | --- | --- | --- |
| jquery | 3.7.1 | MIT | [LICENSE.jquery.md](LICENSE.jquery.md) | `d4db9ebe6f29f5168eac45ad713f055623ac5d0dcd5ba92da23d650ae012020d` |
| tom-select | 2.3.1 | Apache-2.0 | [LICENSE.tom-select.md](LICENSE.tom-select.md) | `f15dc64ea1f373c8af0ee7c020064360497addf335c9c2bcf27c3a6f56a6f0bd` |

jQuery's license is pinned to the official
[3.7.1 source commit](https://github.com/jquery/jquery/blob/f79d5f1a337528940ab7029d4f8bbba72326f269/LICENSE.txt).
Tom Select's license is pinned to the official
[2.3.1 source commit](https://github.com/orchidjs/tom-select/blob/69180fa9e79060060f1824fbde85537579d0005d/LICENSE).
The downloaded version-tag and immutable-commit notices were identical.

The jQuery asset matches the official commit's `dist/jquery.min.js`. The
Tom Select assets and license match the official npm `tom-select@2.3.1`
tarball; its SHA-512 integrity was checked against the registry metadata.

## Existing asset hashes

| File | Bytes | SHA-256 |
| --- | ---: | --- |
| `jquery.min.js` | 87533 | `fc9a93dd241f6b045cbff0481cf4e1901becd0e12fb45166a8f17f95823f0b1a` |
| `tom-select.complete.min.js` | 50720 | `28d785eb15b9a3fb56d6869ee57952e0908d003a0cf911eaae7a14a8bea9bc76` |
| `tom-select.css` | 9752 | `c8168f6b45f8cf03ee444c7a0d2d61850899fd10dd13e2e523ca15e24fb1340c` |

The existing Chosen, Select2 and flatpickr notices remain alongside their
assets. The benchmark's vendored assets are excluded from Flow distributions.
