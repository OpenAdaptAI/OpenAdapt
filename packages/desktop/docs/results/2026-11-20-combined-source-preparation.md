# Combined Desktop source preparation

This is a preparation branch off existing draft #2314, not a second release candidate. It preserves normal history for draft `c6885769c008c31b7d77ec9b0a88910414c59815`, accepted main `c005d5d6a7119fee78213e51d4648e0bfd6c29e3`, reviewed handler source #2324 at `2bc0ac83637044f051a252ee281a051fa1499d2f`, and reviewed theme source #2305 at `ab963290ac741ac7888c8200cfa7726371951cca`. Neither original feature branch nor the existing candidate is updated by this preparation.

Both feature merges were clean. Main produced three conflicts: Flow's CLI import, decomposition budgets and byte-contract test. Their resolutions retain the exact accepted-main files, including invocation kwargs, the binary opener, current size budgets and the copilot physical-byte regression. The resulting Flow tree and Desktop release workflow are byte-identical to accepted main. The reviewed handler source and theme vendor/generator files remain byte-identical to their reviewed heads. The candidate's shared descriptor-model CI selector is retained.

## Focused checks

All checks used the shared CPU gate. Existing Python 3.12.7 imports were bound to this checkout's Desktop, Types and Flow source, with source-contract mode, isolated test keys/recorder and a null keyring. This is not a bundled dependency-closure qualification.

- **63 source/static/intercepted cases passed**, zero failures/skips, in 0.70s; zero subprocesses forked pytest. The selection covers byte contracts, intercepted OS-handler URL allow/refuse behavior, static native-route delegation, phone palette/font bytes, generated overlay contracts and version synchronization.
- Node 22 theme-byte check passed for all eight pinned files at `f50e9c149cb868921e4ce414969dc57d44dc975b`; read-only phone generation check passed.
- Candidate version 0.30.7 remains synchronized across project/module/lock; no version or dependency pin was selected here.
- The real release-intent check **refuses** this composition: `version 0.30.7 is smaller than the minor bump these commits need (0.31.0)`. This is an unresolved release input, not a waived or passing check.

No GUI, real OS browser handler, native build, frozen archive, installer, provider or real application operation was used. The old component reviews and older 150 candidate checks remain scoped to their original subjects; they are not a final combined-candidate verdict.

## Inputs still required by the existing candidate policy

1. Select a separately authorized published Flow cut containing the required common-invocation and credential/reader-lifetime fixes. The old exact build pin and `REQUIRED_FLOW_VERSION` remain 2.4.1. This task does not choose or publish a replacement.
2. Choose the combined Desktop cut, at least 0.31.0 for the surviving theme feature, and synchronize all version sources/changelog plus the exact Flow pin, lock, notices, banners and runtime/type/overlay contracts together. Preserve the Capture 1.4 floor and existing platform ABI/license boundaries.
3. Finish the existing producer/visual decisions and final composition review, then the separately authorized frozen-archive/native-installer qualification and explicit release decision. The existing release workflow can publish after a qualifying version-bearing main merge; this prep branch does not authorize that merge or publication.

No per-application engine code was added. One history, byte and dependency/version model composes the reviewed source. Evidence is source/static/synthetic only; no held-out application, bundled runtime or release qualification is claimed.
