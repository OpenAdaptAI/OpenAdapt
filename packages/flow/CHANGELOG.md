# CHANGELOG

> **Note on the v1.0.0 - v1.33.0 range.** Those entries were absent from this
> file until 2026-08-28 and were reconstructed on that date. On 2026-07-14 this
> repository moved from python-semantic-release 9.15.2 to 10.6.1. Version 10
> changed the default changelog mode from `init` to `update`, and `update` mode
> writes nothing unless the file carries the version-list insertion flag (the
> HTML comment directly below this note). This file, written under version 9,
> had no such flag, so every release from v1.0.0 onward bumped the version and
> wrote no changelog entry, without an error. The reconstruction re-ran the same
> generator over the same tagged commits; it invents nothing. It does differ
> from the v0.x entries in one visible way: version 10's default template emits
> the commit summary line only, so the reconstructed entries carry no commit
> bodies. The v0.x entries below are the originals and are unmodified.

<!-- version list -->

## v1.35.0 (2026-09-02)

### Bug Fixes

- Bind updated process graph schema ([#444](https://github.com/OpenAdaptAI/openadapt-flow/pull/444),
  [`98588e8`](https://github.com/OpenAdaptAI/openadapt-flow/commit/98588e8ac82355ed33c2984473d2708c8e4c3b30))

- Keep process graph schema in sync ([#444](https://github.com/OpenAdaptAI/openadapt-flow/pull/444),
  [`98588e8`](https://github.com/OpenAdaptAI/openadapt-flow/commit/98588e8ac82355ed33c2984473d2708c8e4c3b30))

- Pin execute extra to openadapt-types 0.13 so uv lock resolves
  ([#442](https://github.com/OpenAdaptAI/openadapt-flow/pull/442),
  [`60f455d`](https://github.com/OpenAdaptAI/openadapt-flow/commit/60f455d5a2948e64636454905007ff37d6d48fbf))

- Populate Execute receipt fields required by types 0.13
  ([#442](https://github.com/OpenAdaptAI/openadapt-flow/pull/442),
  [`60f455d`](https://github.com/OpenAdaptAI/openadapt-flow/commit/60f455d5a2948e64636454905007ff37d6d48fbf))

- Preserve X11 library name casing ([#426](https://github.com/OpenAdaptAI/openadapt-flow/pull/426),
  [`4f4d0a9`](https://github.com/OpenAdaptAI/openadapt-flow/commit/4f4d0a9a27a0af14d35e37d3c6965681729a0ee8))

- Retain fail-closed qualification summaries
  ([#427](https://github.com/OpenAdaptAI/openadapt-flow/pull/427),
  [`d5f391c`](https://github.com/OpenAdaptAI/openadapt-flow/commit/d5f391c277021302f311731d9198dfbb5d770566))

- Stamp VERIFIED qualification campaigns at the SoR floor
  ([#435](https://github.com/OpenAdaptAI/openadapt-flow/pull/435),
  [`0584c24`](https://github.com/OpenAdaptAI/openadapt-flow/commit/0584c24210a73386168371a0a785d5000cdcf5c6))

- **ci**: Format compose help and refresh claims inventory
  ([#430](https://github.com/OpenAdaptAI/openadapt-flow/pull/430),
  [`751a888`](https://github.com/OpenAdaptAI/openadapt-flow/commit/751a888f9b40d91fe02d7031b399ebd749b016c1))

- **compiler**: Type REST binding kind as Literal for mypy
  ([#432](https://github.com/OpenAdaptAI/openadapt-flow/pull/432),
  [`e47ee6e`](https://github.com/OpenAdaptAI/openadapt-flow/commit/e47ee6e38a2f2e384724f840a55aaebc4f4c6b82))

- **hosted**: Bind result-loss delivery identity
  ([#412](https://github.com/OpenAdaptAI/openadapt-flow/pull/412),
  [`275b9e0`](https://github.com/OpenAdaptAI/openadapt-flow/commit/275b9e044a3fd5b32942b9acc8cf3510d9e26673))

- **hosted**: Fence every managed result loss
  ([#412](https://github.com/OpenAdaptAI/openadapt-flow/pull/412),
  [`275b9e0`](https://github.com/OpenAdaptAI/openadapt-flow/commit/275b9e044a3fd5b32942b9acc8cf3510d9e26673))

- **hosted**: Fence managed result-loss terminalization
  ([#412](https://github.com/OpenAdaptAI/openadapt-flow/pull/412),
  [`275b9e0`](https://github.com/OpenAdaptAI/openadapt-flow/commit/275b9e044a3fd5b32942b9acc8cf3510d9e26673))

- **hosted**: Keep proofless v2 refusals non-closing
  ([#412](https://github.com/OpenAdaptAI/openadapt-flow/pull/412),
  [`275b9e0`](https://github.com/OpenAdaptAI/openadapt-flow/commit/275b9e044a3fd5b32942b9acc8cf3510d9e26673))

- **hosted**: Sign unresolved delivery reconciliation
  ([#412](https://github.com/OpenAdaptAI/openadapt-flow/pull/412),
  [`275b9e0`](https://github.com/OpenAdaptAI/openadapt-flow/commit/275b9e044a3fd5b32942b9acc8cf3510d9e26673))

- **paper**: Drop the ORCID that 404s
  ([#450](https://github.com/OpenAdaptAI/openadapt-flow/pull/450),
  [`dad7b04`](https://github.com/OpenAdaptAI/openadapt-flow/commit/dad7b04fe46270959f8b1c478213c496a95be084))

- **process**: Bind ExecuteClient and repair CI
  ([#434](https://github.com/OpenAdaptAI/openadapt-flow/pull/434),
  [`a43e005`](https://github.com/OpenAdaptAI/openadapt-flow/commit/a43e005c5e7f2a19e4910b908f80bad7a0cb84d4))

- **release**: Create the GitHub Release with the App instead of semantic-release
  ([#422](https://github.com/OpenAdaptAI/openadapt-flow/pull/422),
  [`fbfa76b`](https://github.com/OpenAdaptAI/openadapt-flow/commit/fbfa76b3c2b69f2e32885f8581e6ef29b7ef458d))

- **reward**: Trainer adapters live in openadapt_evals.reward; flow keeps the worker and the client
  ([#453](https://github.com/OpenAdaptAI/openadapt-flow/pull/453),
  [`a6cdd2c`](https://github.com/OpenAdaptAI/openadapt-flow/commit/a6cdd2c7b59e0ed44abfdbc891310d465c9ac18f))

- **vectors**: Emit valid terminal callback events
  ([#412](https://github.com/OpenAdaptAI/openadapt-flow/pull/412),
  [`275b9e0`](https://github.com/OpenAdaptAI/openadapt-flow/commit/275b9e044a3fd5b32942b9acc8cf3510d9e26673))

### Build System

- Pin openadapt-types 0.17 and add the reward extra
  ([#452](https://github.com/OpenAdaptAI/openadapt-flow/pull/452),
  [`f90f723`](https://github.com/OpenAdaptAI/openadapt-flow/commit/f90f723bb3cae7b453a902e518dc5b2724c16983))

### Code Style

- Format terminal closure vector checks
  ([#412](https://github.com/OpenAdaptAI/openadapt-flow/pull/412),
  [`275b9e0`](https://github.com/OpenAdaptAI/openadapt-flow/commit/275b9e044a3fd5b32942b9acc8cf3510d9e26673))

- Match CI formatter ([#444](https://github.com/OpenAdaptAI/openadapt-flow/pull/444),
  [`98588e8`](https://github.com/OpenAdaptAI/openadapt-flow/commit/98588e8ac82355ed33c2984473d2708c8e4c3b30))

- Ruff-format admit status tests ([#443](https://github.com/OpenAdaptAI/openadapt-flow/pull/443),
  [`a2181f9`](https://github.com/OpenAdaptAI/openadapt-flow/commit/a2181f913d5c5f7560d9dc065289e6b12b4d352c))

### Continuous Integration

- Skip the expensive suite on documentation-only PRs
  ([`f38761e`](https://github.com/OpenAdaptAI/openadapt-flow/commit/f38761e31b62bf4c0d416c93eb0c794510cf8779))

### Documentation

- Clarify governed and multi-app workflow paths
  ([#428](https://github.com/OpenAdaptAI/openadapt-flow/pull/428),
  [`b14e9d6`](https://github.com/OpenAdaptAI/openadapt-flow/commit/b14e9d6eb89159343176e9ab5384eb357ad8757e))

- Commit screenshots of the HTML program visualizer
  ([#434](https://github.com/OpenAdaptAI/openadapt-flow/pull/434),
  [`a43e005`](https://github.com/OpenAdaptAI/openadapt-flow/commit/a43e005c5e7f2a19e4910b908f80bad7a0cb84d4))

- Drop 29-app failure-discovery from README
  ([`01459e5`](https://github.com/OpenAdaptAI/openadapt-flow/commit/01459e530e734c556060f5f2f0c40198a1ba4cd6))

- Match four published claims to their artifacts
  ([#425](https://github.com/OpenAdaptAI/openadapt-flow/pull/425),
  [`2e498fa`](https://github.com/OpenAdaptAI/openadapt-flow/commit/2e498fa711183e5d348a060a49678071f1b15b15))

- Publish the Contributor License Agreement CONTRIBUTING has promised
  ([#420](https://github.com/OpenAdaptAI/openadapt-flow/pull/420),
  [`d5e5615`](https://github.com/OpenAdaptAI/openadapt-flow/commit/d5e5615db77e38f07f13e02d82adf720836aed3b))

- Restore the exact openIMIS licensing wording the AGPL guard pins
  ([#423](https://github.com/OpenAdaptAI/openadapt-flow/pull/423),
  [`96cbd46`](https://github.com/OpenAdaptAI/openadapt-flow/commit/96cbd46c3d96211f03b94a5a4177debff1d7b712))

- Rewrite the README around what the tool does
  ([#423](https://github.com/OpenAdaptAI/openadapt-flow/pull/423),
  [`96cbd46`](https://github.com/OpenAdaptAI/openadapt-flow/commit/96cbd46c3d96211f03b94a5a4177debff1d7b712))

- Show the mermaid visualize already emits
  ([#434](https://github.com/OpenAdaptAI/openadapt-flow/pull/434),
  [`a43e005`](https://github.com/OpenAdaptAI/openadapt-flow/commit/a43e005c5e7f2a19e4910b908f80bad7a0cb84d4))

- Show Windows and Citrix instead of naming six substrates
  ([#423](https://github.com/OpenAdaptAI/openadapt-flow/pull/423),
  [`96cbd46`](https://github.com/OpenAdaptAI/openadapt-flow/commit/96cbd46c3d96211f03b94a5a4177debff1d7b712))

- **paper**: Correct the tree map, and name the profile in each main.tex
  ([#424](https://github.com/OpenAdaptAI/openadapt-flow/pull/424),
  [`8c1b9dd`](https://github.com/OpenAdaptAI/openadapt-flow/commit/8c1b9dd01ba304cf4bd04c5dd82117971a4005e1))

- **paper**: Mark the workshop condensation superseded, and fix the citation claim
  ([#424](https://github.com/OpenAdaptAI/openadapt-flow/pull/424),
  [`8c1b9dd`](https://github.com/OpenAdaptAI/openadapt-flow/commit/8c1b9dd01ba304cf4bd04c5dd82117971a4005e1))

### Features

- Add admit status that reports not-admitted
  ([#443](https://github.com/OpenAdaptAI/openadapt-flow/pull/443),
  [`a2181f9`](https://github.com/OpenAdaptAI/openadapt-flow/commit/a2181f913d5c5f7560d9dc065289e6b12b4d352c))

- Extend ProcessContract with mixed capabilities
  ([#444](https://github.com/OpenAdaptAI/openadapt-flow/pull/444),
  [`98588e8`](https://github.com/OpenAdaptAI/openadapt-flow/commit/98588e8ac82355ed33c2984473d2708c8e4c3b30))

- Lint same-channel effect oracles on consequential writes
  ([`6fe637a`](https://github.com/OpenAdaptAI/openadapt-flow/commit/6fe637a3251c8ecd9973c65b964885a0427423bb))

- MIT reference Execute server with self-signed local receipts
  ([#442](https://github.com/OpenAdaptAI/openadapt-flow/pull/442),
  [`60f455d`](https://github.com/OpenAdaptAI/openadapt-flow/commit/60f455d5a2948e64636454905007ff37d6d48fbf))

- Refuse VERIFIED without an independent system-of-record read
  ([#435](https://github.com/OpenAdaptAI/openadapt-flow/pull/435),
  [`0584c24`](https://github.com/OpenAdaptAI/openadapt-flow/commit/0584c24210a73386168371a0a785d5000cdcf5c6))

- Version hosted terminal outcome contracts
  ([#412](https://github.com/OpenAdaptAI/openadapt-flow/pull/412),
  [`275b9e0`](https://github.com/OpenAdaptAI/openadapt-flow/commit/275b9e044a3fd5b32942b9acc8cf3510d9e26673))

- **cli**: Default record without --backend to this OS
  ([`b9d00e3`](https://github.com/OpenAdaptAI/openadapt-flow/commit/b9d00e3f96b1c236339c81ac5d50a24b0c71ab37))

- **cli**: Wire serve-reward and relock openadapt-types 0.17
  ([#452](https://github.com/OpenAdaptAI/openadapt-flow/pull/452),
  [`f90f723`](https://github.com/OpenAdaptAI/openadapt-flow/commit/f90f723bb3cae7b453a902e518dc5b2724c16983))

- **cli**: Write a deploy.yaml draft bound to a bundle digest
  ([`f93b755`](https://github.com/OpenAdaptAI/openadapt-flow/commit/f93b755b9f7551fdc6a0fab5acd069155c5ff5b6))

- **compiler**: Admit REST bindings only after held-out Effect confirmation
  ([#432](https://github.com/OpenAdaptAI/openadapt-flow/pull/432),
  [`e47ee6e`](https://github.com/OpenAdaptAI/openadapt-flow/commit/e47ee6e38a2f2e384724f840a55aaebc4f4c6b82))

- **compose**: Sequence separately recorded Flow bundles
  ([#430](https://github.com/OpenAdaptAI/openadapt-flow/pull/430),
  [`751a888`](https://github.com/OpenAdaptAI/openadapt-flow/commit/751a888f9b40d91fe02d7031b399ebd749b016c1))

- **flow**: Authoring session wraps Recorder; compile wrapper returns needs_human_admit
  ([`98d754e`](https://github.com/OpenAdaptAI/openadapt-flow/commit/98d754e264249b04e14a722050694f89e870647b))

- **hosted**: Sign complete terminal outcomes
  ([#412](https://github.com/OpenAdaptAI/openadapt-flow/pull/412),
  [`275b9e0`](https://github.com/OpenAdaptAI/openadapt-flow/commit/275b9e044a3fd5b32942b9acc8cf3510d9e26673))

- **hosted**: Version and sign terminal outcomes
  ([#412](https://github.com/OpenAdaptAI/openadapt-flow/pull/412),
  [`275b9e0`](https://github.com/OpenAdaptAI/openadapt-flow/commit/275b9e044a3fd5b32942b9acc8cf3510d9e26673))

- **induce**: Emit a record-next worklist of missing demonstrations
  ([`1e441db`](https://github.com/OpenAdaptAI/openadapt-flow/commit/1e441db59a62483c5fa1803fdd376cf822c0c26f))

- **process**: Sequence independently admitted capabilities
  ([#434](https://github.com/OpenAdaptAI/openadapt-flow/pull/434),
  [`a43e005`](https://github.com/OpenAdaptAI/openadapt-flow/commit/a43e005c5e7f2a19e4910b908f80bad7a0cb84d4))

- **qualify**: Execute --break-it oracle gate before accept
  ([`5a2ebc8`](https://github.com/OpenAdaptAI/openadapt-flow/commit/5a2ebc8b8d7530ae33f69f0e94212d37125643f0))

- **qualify**: Propose production-shaped pins from a demo
  ([`a6f8d25`](https://github.com/OpenAdaptAI/openadapt-flow/commit/a6f8d254000ea6710029ed5eb2a8f391ad9dca37))

- **reward**: MIT reference reward worker with self-signed reward receipts
  ([#452](https://github.com/OpenAdaptAI/openadapt-flow/pull/452),
  [`f90f723`](https://github.com/OpenAdaptAI/openadapt-flow/commit/f90f723bb3cae7b453a902e518dc5b2724c16983))

- **visualize**: Render composed parents without merging ProgramGraphs
  ([#433](https://github.com/OpenAdaptAI/openadapt-flow/pull/433),
  [`924cae9`](https://github.com/OpenAdaptAI/openadapt-flow/commit/924cae97b083b4bc594c78fef3d7190d215ad73b))

### Testing

- Cover compile_recording REST binding admission
  ([#432](https://github.com/OpenAdaptAI/openadapt-flow/pull/432),
  [`e47ee6e`](https://github.com/OpenAdaptAI/openadapt-flow/commit/e47ee6e38a2f2e384724f840a55aaebc4f4c6b82))

- Publish consumer-safe terminal vectors
  ([#412](https://github.com/OpenAdaptAI/openadapt-flow/pull/412),
  [`275b9e0`](https://github.com/OpenAdaptAI/openadapt-flow/commit/275b9e044a3fd5b32942b9acc8cf3510d9e26673))

- **hosted**: Refuse unsigned v2 runs without a signer
  ([#412](https://github.com/OpenAdaptAI/openadapt-flow/pull/412),
  [`275b9e0`](https://github.com/OpenAdaptAI/openadapt-flow/commit/275b9e044a3fd5b32942b9acc8cf3510d9e26673))


## v1.34.0 (2026-08-28)

### Bug Fixes

- Type hosted runner release bindings
  ([`035e88e`](https://github.com/OpenAdaptAI/openadapt-flow/commit/035e88eada0147673d9836a557cf3b758dca296a))

- **cli**: Preserve model tiers on resume
  ([#404](https://github.com/OpenAdaptAI/openadapt-flow/pull/404),
  [`ba127f3`](https://github.com/OpenAdaptAI/openadapt-flow/commit/ba127f3a3637a794355a2fef2b381383e5d0be40))

- **macos**: Match known Chrome AX title suffixes
  ([#416](https://github.com/OpenAdaptAI/openadapt-flow/pull/416),
  [`4505bda`](https://github.com/OpenAdaptAI/openadapt-flow/commit/4505bdaefb773da150bf28f32b49b713b934d978))

- **release**: Restore changelog generation and backfill the v1.x gap
  ([`e5552d5`](https://github.com/OpenAdaptAI/openadapt-flow/commit/e5552d5e6b56c94295dab5b676f1abe570bc53aa))

- **runtime**: Persist resume egress posture before execution
  ([#408](https://github.com/OpenAdaptAI/openadapt-flow/pull/408),
  [`ca07eca`](https://github.com/OpenAdaptAI/openadapt-flow/commit/ca07ecaa05e1707372fecb55b4d002e886198cc2))

- **runtime**: Read each frame's viewport from the frame, not live from the backend
  ([#406](https://github.com/OpenAdaptAI/openadapt-flow/pull/406),
  [`b5d1d49`](https://github.com/OpenAdaptAI/openadapt-flow/commit/b5d1d49c126aeff3e67609d019da74417521dcf4))

- **runtime**: Retain resume egress boundaries
  ([#404](https://github.com/OpenAdaptAI/openadapt-flow/pull/404),
  [`ba127f3`](https://github.com/OpenAdaptAI/openadapt-flow/commit/ba127f3a3637a794355a2fef2b381383e5d0be40))

- **validate-hosted**: Name the derivative whose approval is missing
  ([#417](https://github.com/OpenAdaptAI/openadapt-flow/pull/417),
  [`4ab4155`](https://github.com/OpenAdaptAI/openadapt-flow/commit/4ab4155fba931bd91d2955c1812c31d7b5a62147))

- **visualize**: Fail closed on an out-of-vocabulary effect fact
  ([#415](https://github.com/OpenAdaptAI/openadapt-flow/pull/415),
  [`1185f87`](https://github.com/OpenAdaptAI/openadapt-flow/commit/1185f87f4b53a2cbc380b9363886ce809945202f))

- **visualize**: Leave the retained packs sealed; project the live export path
  ([#415](https://github.com/OpenAdaptAI/openadapt-flow/pull/415),
  [`1185f87`](https://github.com/OpenAdaptAI/openadapt-flow/commit/1185f87f4b53a2cbc380b9363886ce809945202f))

- **visualize**: Make the non-local projection a closed allow-list
  ([#415](https://github.com/OpenAdaptAI/openadapt-flow/pull/415),
  [`1185f87`](https://github.com/OpenAdaptAI/openadapt-flow/commit/1185f87f4b53a2cbc380b9363886ce809945202f))

- **visualize**: Project the public demo graphs before publishing them
  ([#415](https://github.com/OpenAdaptAI/openadapt-flow/pull/415),
  [`1185f87`](https://github.com/OpenAdaptAI/openadapt-flow/commit/1185f87f4b53a2cbc380b9363886ce809945202f))

### Chores

- Ignore every .env variant, not one at a time
  ([#407](https://github.com/OpenAdaptAI/openadapt-flow/pull/407),
  [`e094dbc`](https://github.com/OpenAdaptAI/openadapt-flow/commit/e094dbc094fb783c48a22eb8a931bd8531e63457))

- **policy**: Sync generated source boundary
  ([#414](https://github.com/OpenAdaptAI/openadapt-flow/pull/414),
  [`5ee829d`](https://github.com/OpenAdaptAI/openadapt-flow/commit/5ee829db8bf38d2e579ffeb1f2343cff41f1d0e9))

### Code Style

- Blank line before the frame viewport helper (ruff format)
  ([#406](https://github.com/OpenAdaptAI/openadapt-flow/pull/406),
  [`b5d1d49`](https://github.com/OpenAdaptAI/openadapt-flow/commit/b5d1d49c126aeff3e67609d019da74417521dcf4))

- Format release publication verifier
  ([`9f1b13d`](https://github.com/OpenAdaptAI/openadapt-flow/commit/9f1b13db9f924cba44786efb0320beb5ce8bb16b))

### Continuous Integration

- Refresh release contract artifacts
  ([`1cba16a`](https://github.com/OpenAdaptAI/openadapt-flow/commit/1cba16ad0608e7582110f37df62bcb0bdf57fc98))

- Require release App tag actor
  ([`6047f8d`](https://github.com/OpenAdaptAI/openadapt-flow/commit/6047f8d5b69db6a4a8b4e94556cc194b68e97075))

- Require release App tag publication
  ([`9e9c33b`](https://github.com/OpenAdaptAI/openadapt-flow/commit/9e9c33b994309bf70d40ad18385660d66036b6d4))

### Documentation

- Align Flow claims and limits with current contracts
  ([#409](https://github.com/OpenAdaptAI/openadapt-flow/pull/409),
  [`7e18308`](https://github.com/OpenAdaptAI/openadapt-flow/commit/7e183081c86c5da51f42f677ca40414c45c78383))

- Clarify package state terminology ([#401](https://github.com/OpenAdaptAI/openadapt-flow/pull/401),
  [`fa49a5d`](https://github.com/OpenAdaptAI/openadapt-flow/commit/fa49a5de54a749ebf00621b0e4d38a2b439bb058))

- Derive Flow product state from admissions
  ([#401](https://github.com/OpenAdaptAI/openadapt-flow/pull/401),
  [`fa49a5d`](https://github.com/OpenAdaptAI/openadapt-flow/commit/fa49a5de54a749ebf00621b0e4d38a2b439bb058))

- **paper**: Add the three-tree paper map so the lineage is unambiguous
  ([#399](https://github.com/OpenAdaptAI/openadapt-flow/pull/399),
  [`e17e2aa`](https://github.com/OpenAdaptAI/openadapt-flow/commit/e17e2aa9ab367c11b2a64b2022f1cf45e62a729d))

- **readme**: Launch-funnel pass — evidence table, comparison, FAQ, quick links
  ([#410](https://github.com/OpenAdaptAI/openadapt-flow/pull/410),
  [`977dd4c`](https://github.com/OpenAdaptAI/openadapt-flow/commit/977dd4cb60e457236d1f31235978a40cb1139479))

- **readme**: Restructure for scan-ability, relocate depth into docs/
  ([#411](https://github.com/OpenAdaptAI/openadapt-flow/pull/411),
  [`1043f48`](https://github.com/OpenAdaptAI/openadapt-flow/commit/1043f48f2c21747be4553104e8f6fbe27aab908a))

### Features

- Add governed program visualization profiles
  ([#415](https://github.com/OpenAdaptAI/openadapt-flow/pull/415),
  [`1185f87`](https://github.com/OpenAdaptAI/openadapt-flow/commit/1185f87f4b53a2cbc380b9363886ce809945202f))

- Complete hosted runner target state
  ([`035e88e`](https://github.com/OpenAdaptAI/openadapt-flow/commit/035e88eada0147673d9836a557cf3b758dca296a))

- **release-health**: Detect published versions that have no changelog entry
  ([`0eb81cb`](https://github.com/OpenAdaptAI/openadapt-flow/commit/0eb81cb0b87687bf3de6d21f8a30b12837d67551))

- **sanitize**: Materialize an approved derivative from its archive
  ([#418](https://github.com/OpenAdaptAI/openadapt-flow/pull/418),
  [`80b33d0`](https://github.com/OpenAdaptAI/openadapt-flow/commit/80b33d0759a5b2dcc65fdc3ebe7039d096551da6))

### Testing

- Exercise the product release admission gate instead of stubbing it
  ([`035e88e`](https://github.com/OpenAdaptAI/openadapt-flow/commit/035e88eada0147673d9836a557cf3b758dca296a))

- Fail a run that leaves tracked repository files dirty
  ([#400](https://github.com/OpenAdaptAI/openadapt-flow/pull/400),
  [`defb0e1`](https://github.com/OpenAdaptAI/openadapt-flow/commit/defb0e19167ffc8331c20035cafe71291691f83a))

- Preserve boolean parameter digest
  ([`035e88e`](https://github.com/OpenAdaptAI/openadapt-flow/commit/035e88eada0147673d9836a557cf3b758dca296a))

- **sanitize**: Pin the approval an approved archive cannot carry
  ([#418](https://github.com/OpenAdaptAI/openadapt-flow/pull/418),
  [`80b33d0`](https://github.com/OpenAdaptAI/openadapt-flow/commit/80b33d0759a5b2dcc65fdc3ebe7039d096551da6))

## v1.33.0 (2026-08-25)


### Bug Fixes

- Bind the campaign permit only when the authority is supplied
  ([#385](https://github.com/OpenAdaptAI/openadapt-flow/pull/385),
  [`7dd34db`](https://github.com/OpenAdaptAI/openadapt-flow/commit/7dd34db7a0ea2ac6264fcadd192d7b9701a0a7df))

- Keep the dispatch binding digest grammar stable
  ([#385](https://github.com/OpenAdaptAI/openadapt-flow/pull/385),
  [`7dd34db`](https://github.com/OpenAdaptAI/openadapt-flow/commit/7dd34db7a0ea2ac6264fcadd192d7b9701a0a7df))

- Keep the qualification-case CLI contract working without a permit file
  ([#385](https://github.com/OpenAdaptAI/openadapt-flow/pull/385),
  [`7dd34db`](https://github.com/OpenAdaptAI/openadapt-flow/commit/7dd34db7a0ea2ac6264fcadd192d7b9701a0a7df))

- Let case authorizations stay valid before a permit is bound
  ([#385](https://github.com/OpenAdaptAI/openadapt-flow/pull/385),
  [`7dd34db`](https://github.com/OpenAdaptAI/openadapt-flow/commit/7dd34db7a0ea2ac6264fcadd192d7b9701a0a7df))

- Migrate an empty legacy pending-delivery table without data loss
  ([#384](https://github.com/OpenAdaptAI/openadapt-flow/pull/384),
  [`d73e629`](https://github.com/OpenAdaptAI/openadapt-flow/commit/d73e62962bca61b9bd5fe87ab3750ddc6d02277d))

- Scope the runtime authority gate to governed deliveries
  ([#385](https://github.com/OpenAdaptAI/openadapt-flow/pull/385),
  [`7dd34db`](https://github.com/OpenAdaptAI/openadapt-flow/commit/7dd34db7a0ea2ac6264fcadd192d7b9701a0a7df))

- **record**: Compose iframe events into page space and emit frame_path
  ([#390](https://github.com/OpenAdaptAI/openadapt-flow/pull/390),
  [`e37b427`](https://github.com/OpenAdaptAI/openadapt-flow/commit/e37b427c68eb390f2602fe7c0e95faa2b9926c94))

- **record**: Double-click as one step, refuse native select, document coverage
  ([#391](https://github.com/OpenAdaptAI/openadapt-flow/pull/391),
  [`9b1c8b5`](https://github.com/OpenAdaptAI/openadapt-flow/commit/9b1c8b5af962dc16cddaa15242d6355636f302c6))

- **types**: Annotate the phash image as Image.Image
  ([`b00b724`](https://github.com/OpenAdaptAI/openadapt-flow/commit/b00b724c27b09118c94e2890be07f75bd9c7554e))

### Chores

- Refresh public artifact inventory ([#380](https://github.com/OpenAdaptAI/openadapt-flow/pull/380),
  [`9806814`](https://github.com/OpenAdaptAI/openadapt-flow/commit/980681490bcfc459f9c0a2069d9da80214b5de7c))

- Register the qualification gate campaign public artifacts
  ([#386](https://github.com/OpenAdaptAI/openadapt-flow/pull/386),
  [`583041f`](https://github.com/OpenAdaptAI/openadapt-flow/commit/583041f3cc09d7b3a53640d359373c3556ee0ed8))

- **types**: Drop pixel_identity_aligned from the mypy debt list
  ([`920bde9`](https://github.com/OpenAdaptAI/openadapt-flow/commit/920bde98ebe09be9d892aeb78cede0b9766c9c49))

### Code Style

- Ruff-format the campaign permit fixture test
  ([#385](https://github.com/OpenAdaptAI/openadapt-flow/pull/385),
  [`7dd34db`](https://github.com/OpenAdaptAI/openadapt-flow/commit/7dd34db7a0ea2ac6264fcadd192d7b9701a0a7df))

- Ruff-format the legacy pending-table migration
  ([#384](https://github.com/OpenAdaptAI/openadapt-flow/pull/384),
  [`d73e629`](https://github.com/OpenAdaptAI/openadapt-flow/commit/d73e62962bca61b9bd5fe87ab3750ddc6d02277d))

- Wrap the campaign authority refusal matcher
  ([#385](https://github.com/OpenAdaptAI/openadapt-flow/pull/385),
  [`7dd34db`](https://github.com/OpenAdaptAI/openadapt-flow/commit/7dd34db7a0ea2ac6264fcadd192d7b9701a0a7df))

### Continuous Integration

- Correct the stale version comment on the pypi-publish pin
  ([#377](https://github.com/OpenAdaptAI/openadapt-flow/pull/377),
  [`92ad832`](https://github.com/OpenAdaptAI/openadapt-flow/commit/92ad83233b23cfbd56083665efa7cf16c45afb5d))

- Deselect the platform-neutral qualification campaign on macOS
  ([#397](https://github.com/OpenAdaptAI/openadapt-flow/pull/397),
  [`d4b64ac`](https://github.com/OpenAdaptAI/openadapt-flow/commit/d4b64aca51f1279bfc92ccaddbca438f878c0c1e))

- Prevent stale-head releases ([#380](https://github.com/OpenAdaptAI/openadapt-flow/pull/380),
  [`9806814`](https://github.com/OpenAdaptAI/openadapt-flow/commit/980681490bcfc459f9c0a2069d9da80214b5de7c))

- Regenerate the public artifact inventory for the edited workflow
  ([#377](https://github.com/OpenAdaptAI/openadapt-flow/pull/377),
  [`92ad832`](https://github.com/OpenAdaptAI/openadapt-flow/commit/92ad83233b23cfbd56083665efa7cf16c45afb5d))

### Documentation

- Reconcile the OpenEMR 19/20 correction with older 20/20 summaries
  ([#389](https://github.com/OpenAdaptAI/openadapt-flow/pull/389),
  [`94e7036`](https://github.com/OpenAdaptAI/openadapt-flow/commit/94e70364aa04632e9e8031434ad801a74344ac15))

- **paper**: State EffectBench's public scope, gated split, and gaming surface
  ([#378](https://github.com/OpenAdaptAI/openadapt-flow/pull/378),
  [`51bd075`](https://github.com/OpenAdaptAI/openadapt-flow/commit/51bd07597097d40af720c2e0c627d5d16d1935aa))

### Features

- Add scaffold-verifier, explain, outcome epilogues, and receipt share cards
  ([#388](https://github.com/OpenAdaptAI/openadapt-flow/pull/388),
  [`ecfcd8d`](https://github.com/OpenAdaptAI/openadapt-flow/commit/ecfcd8d0cec04d890ac2c2a5630f66fbeaebdc07))

- Add the gate-standard local qualification campaign
  ([#386](https://github.com/OpenAdaptAI/openadapt-flow/pull/386),
  [`583041f`](https://github.com/OpenAdaptAI/openadapt-flow/commit/583041f3cc09d7b3a53640d359373c3556ee0ed8))

- Bind production delivery to signed permits and receipts
  ([#383](https://github.com/OpenAdaptAI/openadapt-flow/pull/383),
  [`63f5eff`](https://github.com/OpenAdaptAI/openadapt-flow/commit/63f5efffca770ef1cb2960f8a9745e8950f45ad9))

- Make installer script the primary quickstart path
  ([#387](https://github.com/OpenAdaptAI/openadapt-flow/pull/387),
  [`abac2bb`](https://github.com/OpenAdaptAI/openadapt-flow/commit/abac2bb3bde8e3bac85c975f3ecf6ff1938499da))

- Require signed runtime authority for Production and campaign actuation
  ([#385](https://github.com/OpenAdaptAI/openadapt-flow/pull/385),
  [`7dd34db`](https://github.com/OpenAdaptAI/openadapt-flow/commit/7dd34db7a0ea2ac6264fcadd192d7b9701a0a7df))

- **record**: Declare the coordinate space on the capture path too
  ([#396](https://github.com/OpenAdaptAI/openadapt-flow/pull/396),
  [`faf2bd1`](https://github.com/OpenAdaptAI/openadapt-flow/commit/faf2bd16125e1dac7559d5a3c3f190b6edd8267a))

### Testing

- Fix a Python 3.10 permit parse and a Chromium CDP readiness race
  ([#395](https://github.com/OpenAdaptAI/openadapt-flow/pull/395),
  [`a3351fe`](https://github.com/OpenAdaptAI/openadapt-flow/commit/a3351fe18b954ffc8c418cd4d7a388bbfec4d8fe))

## v1.32.0 (2026-08-20)


### Bug Fixes

- Bind structured push to retained server version
  ([#369](https://github.com/OpenAdaptAI/openadapt-flow/pull/369),
  [`663550f`](https://github.com/OpenAdaptAI/openadapt-flow/commit/663550fc11c58d262a79766b184b510af527b242))

- Close paused push schema state ([#369](https://github.com/OpenAdaptAI/openadapt-flow/pull/369),
  [`663550f`](https://github.com/OpenAdaptAI/openadapt-flow/commit/663550fc11c58d262a79766b184b510af527b242))

- Forbid server bindings on unsuccessful push
  ([#369](https://github.com/OpenAdaptAI/openadapt-flow/pull/369),
  [`663550f`](https://github.com/OpenAdaptAI/openadapt-flow/commit/663550fc11c58d262a79766b184b510af527b242))

- Keep admission identity independent
  ([#376](https://github.com/OpenAdaptAI/openadapt-flow/pull/376),
  [`c2c2993`](https://github.com/OpenAdaptAI/openadapt-flow/commit/c2c2993574aeae763f517f068b5407ca2f366270))

- Preserve resized capture coordinate spaces
  ([#366](https://github.com/OpenAdaptAI/openadapt-flow/pull/366),
  [`02e68bf`](https://github.com/OpenAdaptAI/openadapt-flow/commit/02e68bf9e658d4d2147243fc33e69b4f2dd70b21))

- Prove an exact accepted ingest before push reports success
  ([#369](https://github.com/OpenAdaptAI/openadapt-flow/pull/369),
  [`663550f`](https://github.com/OpenAdaptAI/openadapt-flow/commit/663550fc11c58d262a79766b184b510af527b242))

- Reject contradictory push result states
  ([#369](https://github.com/OpenAdaptAI/openadapt-flow/pull/369),
  [`663550f`](https://github.com/OpenAdaptAI/openadapt-flow/commit/663550fc11c58d262a79766b184b510af527b242))

- Require Flow production release evidence
  ([#366](https://github.com/OpenAdaptAI/openadapt-flow/pull/366),
  [`02e68bf`](https://github.com/OpenAdaptAI/openadapt-flow/commit/02e68bf9e658d4d2147243fc33e69b4f2dd70b21))

- Require passing evidence for supported claims
  ([`e1a236c`](https://github.com/OpenAdaptAI/openadapt-flow/commit/e1a236c8c307e6613b1a310667a49bb7c2e82a53))

- **browser**: Apply the same proof to a bare URL fragment
  ([#364](https://github.com/OpenAdaptAI/openadapt-flow/pull/364),
  [`a5a0bbb`](https://github.com/OpenAdaptAI/openadapt-flow/commit/a5a0bbb3a788d92f61484956ea6b941095bdb991))

- **browser**: Bind same-task secret mutations
  ([#364](https://github.com/OpenAdaptAI/openadapt-flow/pull/364),
  [`a5a0bbb`](https://github.com/OpenAdaptAI/openadapt-flow/commit/a5a0bbb3a788d92f61484956ea6b941095bdb991))

- **browser**: Bind secret redaction to the element, never to keystroke prefixes
  ([#364](https://github.com/OpenAdaptAI/openadapt-flow/pull/364),
  [`a5a0bbb`](https://github.com/OpenAdaptAI/openadapt-flow/commit/a5a0bbb3a788d92f61484956ea6b941095bdb991))

- **browser**: Close attach evidence gaps
  ([#364](https://github.com/OpenAdaptAI/openadapt-flow/pull/364),
  [`a5a0bbb`](https://github.com/OpenAdaptAI/openadapt-flow/commit/a5a0bbb3a788d92f61484956ea6b941095bdb991))

- **browser**: Close attached finalization gaps
  ([#364](https://github.com/OpenAdaptAI/openadapt-flow/pull/364),
  [`a5a0bbb`](https://github.com/OpenAdaptAI/openadapt-flow/commit/a5a0bbb3a788d92f61484956ea6b941095bdb991))

- **browser**: Close three round-4 blockers and redact URLs by structure
  ([#364](https://github.com/OpenAdaptAI/openadapt-flow/pull/364),
  [`a5a0bbb`](https://github.com/OpenAdaptAI/openadapt-flow/commit/a5a0bbb3a788d92f61484956ea6b941095bdb991))

- **browser**: Harden attached recording boundaries
  ([#364](https://github.com/OpenAdaptAI/openadapt-flow/pull/364),
  [`a5a0bbb`](https://github.com/OpenAdaptAI/openadapt-flow/commit/a5a0bbb3a788d92f61484956ea6b941095bdb991))

- **browser**: Harden attached recording finalization
  ([#364](https://github.com/OpenAdaptAI/openadapt-flow/pull/364),
  [`a5a0bbb`](https://github.com/OpenAdaptAI/openadapt-flow/commit/a5a0bbb3a788d92f61484956ea6b941095bdb991))

- **browser**: Latch attached recording races
  ([#364](https://github.com/OpenAdaptAI/openadapt-flow/pull/364),
  [`a5a0bbb`](https://github.com/OpenAdaptAI/openadapt-flow/commit/a5a0bbb3a788d92f61484956ea6b941095bdb991))

- **browser**: Make secret classification sticky across DOM replacement
  ([#364](https://github.com/OpenAdaptAI/openadapt-flow/pull/364),
  [`a5a0bbb`](https://github.com/OpenAdaptAI/openadapt-flow/commit/a5a0bbb3a788d92f61484956ea6b941095bdb991))

- **browser**: Never drop a value the field holds or a commit point recorded
  ([#364](https://github.com/OpenAdaptAI/openadapt-flow/pull/364),
  [`a5a0bbb`](https://github.com/OpenAdaptAI/openadapt-flow/commit/a5a0bbb3a788d92f61484956ea6b941095bdb991))

- **browser**: Prefer the definite withhold reason over the ambiguous one
  ([#364](https://github.com/OpenAdaptAI/openadapt-flow/pull/364),
  [`a5a0bbb`](https://github.com/OpenAdaptAI/openadapt-flow/commit/a5a0bbb3a788d92f61484956ea6b941095bdb991))

- **browser**: Recognise a keystroke prefix per declared field, not globally
  ([#364](https://github.com/OpenAdaptAI/openadapt-flow/pull/364),
  [`a5a0bbb`](https://github.com/OpenAdaptAI/openadapt-flow/commit/a5a0bbb3a788d92f61484956ea6b941095bdb991))

- **browser**: Replace secret value retention with capture-time withholding
  ([#364](https://github.com/OpenAdaptAI/openadapt-flow/pull/364),
  [`a5a0bbb`](https://github.com/OpenAdaptAI/openadapt-flow/commit/a5a0bbb3a788d92f61484956ea6b941095bdb991))

- **browser**: Retain masks through final capture
  ([#364](https://github.com/OpenAdaptAI/openadapt-flow/pull/364),
  [`a5a0bbb`](https://github.com/OpenAdaptAI/openadapt-flow/commit/a5a0bbb3a788d92f61484956ea6b941095bdb991))

- **browser**: Treat a page that consumes its own field as having held a value
  ([#364](https://github.com/OpenAdaptAI/openadapt-flow/pull/364),
  [`a5a0bbb`](https://github.com/OpenAdaptAI/openadapt-flow/commit/a5a0bbb3a788d92f61484956ea6b941095bdb991))

- **browser**: Withhold a later document's URL; the path is not structural
  ([#364](https://github.com/OpenAdaptAI/openadapt-flow/pull/364),
  [`a5a0bbb`](https://github.com/OpenAdaptAI/openadapt-flow/commit/a5a0bbb3a788d92f61484956ea6b941095bdb991))

- **profiles**: Require independent evidence for the Regulated profile
  ([#355](https://github.com/OpenAdaptAI/openadapt-flow/pull/355),
  [`45c8d38`](https://github.com/OpenAdaptAI/openadapt-flow/commit/45c8d383345e3624f9f4a191ec883eeca8da5fc3))

- **sealing**: Keep pre-field sealed v2 digests valid for the new effect fields
  ([#362](https://github.com/OpenAdaptAI/openadapt-flow/pull/362),
  [`d7f58d9`](https://github.com/OpenAdaptAI/openadapt-flow/commit/d7f58d9f35c8369f16a9b378f23952d425334ad7))

### Build System

- Bump ruff from 0.15.22 to 0.16.3 in the python-minor group across 1 directory
  ([#353](https://github.com/OpenAdaptAI/openadapt-flow/pull/353),
  [`833022b`](https://github.com/OpenAdaptAI/openadapt-flow/commit/833022bfc9bf2d5599f135dac666f5e0fe039c88))

- Bump ruff in the python-minor group across 1 directory
  ([#353](https://github.com/OpenAdaptAI/openadapt-flow/pull/353),
  [`833022b`](https://github.com/OpenAdaptAI/openadapt-flow/commit/833022bfc9bf2d5599f135dac666f5e0fe039c88))

- Refresh the lock after the rebase ([#366](https://github.com/OpenAdaptAI/openadapt-flow/pull/366),
  [`02e68bf`](https://github.com/OpenAdaptAI/openadapt-flow/commit/02e68bf9e658d4d2147243fc33e69b4f2dd70b21))

### Chores

- Apply Ruff 0.16.3 formatting ([#353](https://github.com/OpenAdaptAI/openadapt-flow/pull/353),
  [`833022b`](https://github.com/OpenAdaptAI/openadapt-flow/commit/833022bfc9bf2d5599f135dac666f5e0fe039c88))

- Regenerate the artifact inventory after the rebase
  ([#366](https://github.com/OpenAdaptAI/openadapt-flow/pull/366),
  [`02e68bf`](https://github.com/OpenAdaptAI/openadapt-flow/commit/02e68bf9e658d4d2147243fc33e69b4f2dd70b21))

- **browser**: Re-pin the public artifact inventory after the claims update
  ([#364](https://github.com/OpenAdaptAI/openadapt-flow/pull/364),
  [`a5a0bbb`](https://github.com/OpenAdaptAI/openadapt-flow/commit/a5a0bbb3a788d92f61484956ea6b941095bdb991))

- **claims**: Regenerate verification report and artifact inventory after rebase
  ([#364](https://github.com/OpenAdaptAI/openadapt-flow/pull/364),
  [`a5a0bbb`](https://github.com/OpenAdaptAI/openadapt-flow/commit/a5a0bbb3a788d92f61484956ea6b941095bdb991))

- **deps**: Bump aiohttp to 3.14.3 and cryptography to 50.0.0 for security fixes
  ([#361](https://github.com/OpenAdaptAI/openadapt-flow/pull/361),
  [`687090e`](https://github.com/OpenAdaptAI/openadapt-flow/commit/687090e840451e91196e099640f981b87aff297c))

### Continuous Integration

- Bound installer process cleanup ([#370](https://github.com/OpenAdaptAI/openadapt-flow/pull/370),
  [`973e21c`](https://github.com/OpenAdaptAI/openadapt-flow/commit/973e21c458de1bccce67ba23b24ce58ce2cbf7ab))

- Bound qualification and browser setup
  ([#370](https://github.com/OpenAdaptAI/openadapt-flow/pull/370),
  [`973e21c`](https://github.com/OpenAdaptAI/openadapt-flow/commit/973e21c458de1bccce67ba23b24ce58ce2cbf7ab))

- Bound the paper build and stop false private-source alarms
  ([#373](https://github.com/OpenAdaptAI/openadapt-flow/pull/373),
  [`7860278`](https://github.com/OpenAdaptAI/openadapt-flow/commit/7860278858ba391d794c506660858e859d12a630))

- Bump the actions group across 1 directory with 2 updates
  ([#363](https://github.com/OpenAdaptAI/openadapt-flow/pull/363),
  [`16c9177`](https://github.com/OpenAdaptAI/openadapt-flow/commit/16c9177a1dab5046a8c8fd12dee12e3342632b27))

- Enforce immutable dependency lock ([#353](https://github.com/OpenAdaptAI/openadapt-flow/pull/353),
  [`833022b`](https://github.com/OpenAdaptAI/openadapt-flow/commit/833022bfc9bf2d5599f135dac666f5e0fe039c88))

- Make installer cleanup fail closed
  ([#370](https://github.com/OpenAdaptAI/openadapt-flow/pull/370),
  [`973e21c`](https://github.com/OpenAdaptAI/openadapt-flow/commit/973e21c458de1bccce67ba23b24ce58ce2cbf7ab))

- Prefer the canonical Ubuntu archive for the apt installs
  ([#375](https://github.com/OpenAdaptAI/openadapt-flow/pull/375),
  [`7633d45`](https://github.com/OpenAdaptAI/openadapt-flow/commit/7633d45cb1eb0091aa8e068a0c2a4cfd421e0115))

- Prefer the canonical Ubuntu archive for the paper TeX install
  ([#374](https://github.com/OpenAdaptAI/openadapt-flow/pull/374),
  [`068b777`](https://github.com/OpenAdaptAI/openadapt-flow/commit/068b777c08db5ce0bb3645b29178e7facc1ddd02))

- Refresh public artifact inventory ([#363](https://github.com/OpenAdaptAI/openadapt-flow/pull/363),
  [`16c9177`](https://github.com/OpenAdaptAI/openadapt-flow/commit/16c9177a1dab5046a8c8fd12dee12e3342632b27))

- Reserve RDP evidence cleanup budget
  ([#370](https://github.com/OpenAdaptAI/openadapt-flow/pull/370),
  [`973e21c`](https://github.com/OpenAdaptAI/openadapt-flow/commit/973e21c458de1bccce67ba23b24ce58ce2cbf7ab))

- Retain installer trees until verified cleanup
  ([#370](https://github.com/OpenAdaptAI/openadapt-flow/pull/370),
  [`973e21c`](https://github.com/OpenAdaptAI/openadapt-flow/commit/973e21c458de1bccce67ba23b24ce58ce2cbf7ab))

- Terminate privileged installer descendants
  ([#370](https://github.com/OpenAdaptAI/openadapt-flow/pull/370),
  [`973e21c`](https://github.com/OpenAdaptAI/openadapt-flow/commit/973e21c458de1bccce67ba23b24ce58ce2cbf7ab))

- Terminate Windows installer process trees
  ([#370](https://github.com/OpenAdaptAI/openadapt-flow/pull/370),
  [`973e21c`](https://github.com/OpenAdaptAI/openadapt-flow/commit/973e21c458de1bccce67ba23b24ce58ce2cbf7ab))

### Documentation

- Align generated verification timestamp
  ([#364](https://github.com/OpenAdaptAI/openadapt-flow/pull/364),
  [`a5a0bbb`](https://github.com/OpenAdaptAI/openadapt-flow/commit/a5a0bbb3a788d92f61484956ea6b941095bdb991))

- Keep generated verification report in sync
  ([#364](https://github.com/OpenAdaptAI/openadapt-flow/pull/364),
  [`a5a0bbb`](https://github.com/OpenAdaptAI/openadapt-flow/commit/a5a0bbb3a788d92f61484956ea6b941095bdb991))

- **browser**: Correct comments that still described the removed scrubber
  ([#364](https://github.com/OpenAdaptAI/openadapt-flow/pull/364),
  [`a5a0bbb`](https://github.com/OpenAdaptAI/openadapt-flow/commit/a5a0bbb3a788d92f61484956ea6b941095bdb991))

- **browser**: Correct the commit-point comment
  ([#364](https://github.com/OpenAdaptAI/openadapt-flow/pull/364),
  [`a5a0bbb`](https://github.com/OpenAdaptAI/openadapt-flow/commit/a5a0bbb3a788d92f61484956ea6b941095bdb991))

- **browser**: State the retention rule and every withheld identity
  ([#364](https://github.com/OpenAdaptAI/openadapt-flow/pull/364),
  [`a5a0bbb`](https://github.com/OpenAdaptAI/openadapt-flow/commit/a5a0bbb3a788d92f61484956ea6b941095bdb991))

- **browser**: State the structured URL rule and what it still costs
  ([#364](https://github.com/OpenAdaptAI/openadapt-flow/pull/364),
  [`a5a0bbb`](https://github.com/OpenAdaptAI/openadapt-flow/commit/a5a0bbb3a788d92f61484956ea6b941095bdb991))

- **paper**: Record the four B1 decisions -- authorship+ORCID, CC BY 4.0, COI statement, workshop
  target ([#358](https://github.com/OpenAdaptAI/openadapt-flow/pull/358),
  [`3e740f8`](https://github.com/OpenAdaptAI/openadapt-flow/commit/3e740f8d5ba303ab067fc63fbd6921fbcc7a3123))

### Features

- Add structured push result contract
  ([#369](https://github.com/OpenAdaptAI/openadapt-flow/pull/369),
  [`663550f`](https://github.com/OpenAdaptAI/openadapt-flow/commit/663550fc11c58d262a79766b184b510af527b242))

- Add the v2 qualification authority contract
  ([#376](https://github.com/OpenAdaptAI/openadapt-flow/pull/376),
  [`c2c2993`](https://github.com/OpenAdaptAI/openadapt-flow/commit/c2c2993574aeae763f517f068b5407ca2f366270))

- Enforce signed qualification admission before actuation
  ([#376](https://github.com/OpenAdaptAI/openadapt-flow/pull/376),
  [`c2c2993`](https://github.com/OpenAdaptAI/openadapt-flow/commit/c2c2993574aeae763f517f068b5407ca2f366270))

- Enforce signed workflow qualification admission
  ([#376](https://github.com/OpenAdaptAI/openadapt-flow/pull/376),
  [`c2c2993`](https://github.com/OpenAdaptAI/openadapt-flow/commit/c2c2993574aeae763f517f068b5407ca2f366270))

- Negotiate qualification authority v2
  ([#376](https://github.com/OpenAdaptAI/openadapt-flow/pull/376),
  [`c2c2993`](https://github.com/OpenAdaptAI/openadapt-flow/commit/c2c2993574aeae763f517f068b5407ca2f366270))

- **attest**: Opt-in post-run bridge to the openadapt-attest proof sidecar
  ([#357](https://github.com/OpenAdaptAI/openadapt-flow/pull/357),
  [`a2ceac3`](https://github.com/OpenAdaptAI/openadapt-flow/commit/a2ceac3ca0ab14951f2b5124ff1cb74a059e3521))

- **browser**: Attach recorder to existing sessions
  ([#364](https://github.com/OpenAdaptAI/openadapt-flow/pull/364),
  [`a5a0bbb`](https://github.com/OpenAdaptAI/openadapt-flow/commit/a5a0bbb3a788d92f61484956ea6b941095bdb991))

- **effects**: Exact-new-set guard closes the over-write false-pass gap
  ([#362](https://github.com/OpenAdaptAI/openadapt-flow/pull/362),
  [`d7f58d9`](https://github.com/OpenAdaptAI/openadapt-flow/commit/d7f58d9f35c8369f16a9b378f23952d425334ad7))

- **tutorial**: Print next steps after a verified run
  ([#354](https://github.com/OpenAdaptAI/openadapt-flow/pull/354),
  [`f792da6`](https://github.com/OpenAdaptAI/openadapt-flow/commit/f792da63291540fb6a10fef9965fddcc96ca2e36))

### Testing

- **browser**: Assert the refused DOM identity states its reason
  ([#364](https://github.com/OpenAdaptAI/openadapt-flow/pull/364),
  [`a5a0bbb`](https://github.com/OpenAdaptAI/openadapt-flow/commit/a5a0bbb3a788d92f61484956ea6b941095bdb991))

## v1.31.0 (2026-08-09)


### Bug Fixes

- Authenticate decision renewal history
  ([#339](https://github.com/OpenAdaptAI/openadapt-flow/pull/339),
  [`3b6d33e`](https://github.com/OpenAdaptAI/openadapt-flow/commit/3b6d33eb11f9ef26d6698828ee5ebd82b291b57b))

- Bind decision task v2 to pause authority
  ([#308](https://github.com/OpenAdaptAI/openadapt-flow/pull/308),
  [`e010c9f`](https://github.com/OpenAdaptAI/openadapt-flow/commit/e010c9f46cd5744a9c87f1ab818227f14cdf921c))

- Bind identity-armed templates to landmark state
  ([#327](https://github.com/OpenAdaptAI/openadapt-flow/pull/327),
  [`ff1a80c`](https://github.com/OpenAdaptAI/openadapt-flow/commit/ff1a80ca7f820b54f265f50bcd21d3a1428edfb5))

- Bind portable decision receipts to exact answers
  ([#341](https://github.com/OpenAdaptAI/openadapt-flow/pull/341),
  [`079bbc8`](https://github.com/OpenAdaptAI/openadapt-flow/commit/079bbc863dee82ddae6ccf6c293630f3f6cb78b7))

- Bind RDP acceptance campaign to remote surface
  ([#327](https://github.com/OpenAdaptAI/openadapt-flow/pull/327),
  [`ff1a80c`](https://github.com/OpenAdaptAI/openadapt-flow/commit/ff1a80ca7f820b54f265f50bcd21d3a1428edfb5))

- Bind RDP application label ([#327](https://github.com/OpenAdaptAI/openadapt-flow/pull/327),
  [`ff1a80c`](https://github.com/OpenAdaptAI/openadapt-flow/commit/ff1a80ca7f820b54f265f50bcd21d3a1428edfb5))

- Bind RDP campaign qualification authority
  ([#327](https://github.com/OpenAdaptAI/openadapt-flow/pull/327),
  [`ff1a80c`](https://github.com/OpenAdaptAI/openadapt-flow/commit/ff1a80ca7f820b54f265f50bcd21d3a1428edfb5))

- Bind RDP campaign verifier tiers ([#327](https://github.com/OpenAdaptAI/openadapt-flow/pull/327),
  [`ff1a80c`](https://github.com/OpenAdaptAI/openadapt-flow/commit/ff1a80ca7f820b54f265f50bcd21d3a1428edfb5))

- Bind RDP client session identity ([#327](https://github.com/OpenAdaptAI/openadapt-flow/pull/327),
  [`ff1a80c`](https://github.com/OpenAdaptAI/openadapt-flow/commit/ff1a80ca7f820b54f265f50bcd21d3a1428edfb5))

- Bind RDP qualification environment
  ([#327](https://github.com/OpenAdaptAI/openadapt-flow/pull/327),
  [`ff1a80c`](https://github.com/OpenAdaptAI/openadapt-flow/commit/ff1a80ca7f820b54f265f50bcd21d3a1428edfb5))

- Bind RDP qualification target ([#327](https://github.com/OpenAdaptAI/openadapt-flow/pull/327),
  [`ff1a80c`](https://github.com/OpenAdaptAI/openadapt-flow/commit/ff1a80ca7f820b54f265f50bcd21d3a1428edfb5))

- Bind remote mask to fresh protected regions
  ([#322](https://github.com/OpenAdaptAI/openadapt-flow/pull/322),
  [`3b56041`](https://github.com/OpenAdaptAI/openadapt-flow/commit/3b560415144811fe3834a68fb864caffde620209))

- Bind remote masks to resolution evidence
  ([#322](https://github.com/OpenAdaptAI/openadapt-flow/pull/322),
  [`3b56041`](https://github.com/OpenAdaptAI/openadapt-flow/commit/3b560415144811fe3834a68fb864caffde620209))

- Bind remote review to the exact run
  ([#348](https://github.com/OpenAdaptAI/openadapt-flow/pull/348),
  [`cdbe958`](https://github.com/OpenAdaptAI/openadapt-flow/commit/cdbe95803aa795712917d2374abc957d6bfaca36))

- Bound RDP campaign observation and coverage
  ([#327](https://github.com/OpenAdaptAI/openadapt-flow/pull/327),
  [`ff1a80c`](https://github.com/OpenAdaptAI/openadapt-flow/commit/ff1a80ca7f820b54f265f50bcd21d3a1428edfb5))

- Close identity-armed remote actuation evidence
  ([#327](https://github.com/OpenAdaptAI/openadapt-flow/pull/327),
  [`ff1a80c`](https://github.com/OpenAdaptAI/openadapt-flow/commit/ff1a80ca7f820b54f265f50bcd21d3a1428edfb5))

- Continue bounded scroll after OCR ambiguity
  ([#327](https://github.com/OpenAdaptAI/openadapt-flow/pull/327),
  [`ff1a80c`](https://github.com/OpenAdaptAI/openadapt-flow/commit/ff1a80ca7f820b54f265f50bcd21d3a1428edfb5))

- Enforce decision service local trust
  ([#350](https://github.com/OpenAdaptAI/openadapt-flow/pull/350),
  [`8160dc4`](https://github.com/OpenAdaptAI/openadapt-flow/commit/8160dc4761f974f59635369733c27631a7516169))

- Enforce qualified remote frame input gates
  ([#322](https://github.com/OpenAdaptAI/openadapt-flow/pull/322),
  [`3b56041`](https://github.com/OpenAdaptAI/openadapt-flow/commit/3b560415144811fe3834a68fb864caffde620209))

- Expose RDP environment evidence ([#327](https://github.com/OpenAdaptAI/openadapt-flow/pull/327),
  [`ff1a80c`](https://github.com/OpenAdaptAI/openadapt-flow/commit/ff1a80ca7f820b54f265f50bcd21d3a1428edfb5))

- Expose RDP environment markers ([#327](https://github.com/OpenAdaptAI/openadapt-flow/pull/327),
  [`ff1a80c`](https://github.com/OpenAdaptAI/openadapt-flow/commit/ff1a80ca7f820b54f265f50bcd21d3a1428edfb5))

- Harden business decision authority
  ([#339](https://github.com/OpenAdaptAI/openadapt-flow/pull/339),
  [`3b6d33e`](https://github.com/OpenAdaptAI/openadapt-flow/commit/3b6d33eb11f9ef26d6698828ee5ebd82b291b57b))

- Harden Citrix acceptance evidence contract
  ([#338](https://github.com/OpenAdaptAI/openadapt-flow/pull/338),
  [`1c5c925`](https://github.com/OpenAdaptAI/openadapt-flow/commit/1c5c925a0de7b4216559da3f29b3270328c2918e))

- Harden Citrix campaign recovery ([#338](https://github.com/OpenAdaptAI/openadapt-flow/pull/338),
  [`1c5c925`](https://github.com/OpenAdaptAI/openadapt-flow/commit/1c5c925a0de7b4216559da3f29b3270328c2918e))

- Harden RDP environment marker OCR ([#327](https://github.com/OpenAdaptAI/openadapt-flow/pull/327),
  [`ff1a80c`](https://github.com/OpenAdaptAI/openadapt-flow/commit/ff1a80ca7f820b54f265f50bcd21d3a1428edfb5))

- Harden typed decision supervision ([#349](https://github.com/OpenAdaptAI/openadapt-flow/pull/349),
  [`0146429`](https://github.com/OpenAdaptAI/openadapt-flow/commit/0146429c962d34dd03da2f9b51f05690fde9b053))

- Keep custom entity labels local ([#308](https://github.com/OpenAdaptAI/openadapt-flow/pull/308),
  [`e010c9f`](https://github.com/OpenAdaptAI/openadapt-flow/commit/e010c9f46cd5744a9c87f1ab818227f14cdf921c))

- Keep entity labels optional in decision v2
  ([#308](https://github.com/OpenAdaptAI/openadapt-flow/pull/308),
  [`e010c9f`](https://github.com/OpenAdaptAI/openadapt-flow/commit/e010c9f46cd5744a9c87f1ab818227f14cdf921c))

- Keep qualified bundles consistent through sanitization
  ([#351](https://github.com/OpenAdaptAI/openadapt-flow/pull/351),
  [`faf9945`](https://github.com/OpenAdaptAI/openadapt-flow/commit/faf9945537d4011baeb36ce5f063b6e1814903e6))

- Keep qualified workflow rendering current
  ([#351](https://github.com/OpenAdaptAI/openadapt-flow/pull/351),
  [`faf9945`](https://github.com/OpenAdaptAI/openadapt-flow/commit/faf9945537d4011baeb36ce5f063b6e1814903e6))

- Keep RDP fixture text delivery atomic
  ([#327](https://github.com/OpenAdaptAI/openadapt-flow/pull/327),
  [`ff1a80c`](https://github.com/OpenAdaptAI/openadapt-flow/commit/ff1a80ca7f820b54f265f50bcd21d3a1428edfb5))

- Locate RDP fixture application marker
  ([#327](https://github.com/OpenAdaptAI/openadapt-flow/pull/327),
  [`ff1a80c`](https://github.com/OpenAdaptAI/openadapt-flow/commit/ff1a80ca7f820b54f265f50bcd21d3a1428edfb5))

- Make Citrix acceptance fail-safe after dispatch
  ([#338](https://github.com/OpenAdaptAI/openadapt-flow/pull/338),
  [`1c5c925`](https://github.com/OpenAdaptAI/openadapt-flow/commit/1c5c925a0de7b4216559da3f29b3270328c2918e))

- Match RDP environment marker tokens
  ([#327](https://github.com/OpenAdaptAI/openadapt-flow/pull/327),
  [`ff1a80c`](https://github.com/OpenAdaptAI/openadapt-flow/commit/ff1a80ca7f820b54f265f50bcd21d3a1428edfb5))

- Preserve active RDP keyboard focus
  ([#327](https://github.com/OpenAdaptAI/openadapt-flow/pull/327),
  [`ff1a80c`](https://github.com/OpenAdaptAI/openadapt-flow/commit/ff1a80ca7f820b54f265f50bcd21d3a1428edfb5))

- Preserve ambiguous identity landmark refusal
  ([#327](https://github.com/OpenAdaptAI/openadapt-flow/pull/327),
  [`ff1a80c`](https://github.com/OpenAdaptAI/openadapt-flow/commit/ff1a80ca7f820b54f265f50bcd21d3a1428edfb5))

- Preserve drag destination authority
  ([#339](https://github.com/OpenAdaptAI/openadapt-flow/pull/339),
  [`3b6d33e`](https://github.com/OpenAdaptAI/openadapt-flow/commit/3b6d33eb11f9ef26d6698828ee5ebd82b291b57b))

- Preserve exact identity target under ambiguous context
  ([#327](https://github.com/OpenAdaptAI/openadapt-flow/pull/327),
  [`ff1a80c`](https://github.com/OpenAdaptAI/openadapt-flow/commit/ff1a80ca7f820b54f265f50bcd21d3a1428edfb5))

- Preserve qualified machine contracts during scrubbing
  ([#351](https://github.com/OpenAdaptAI/openadapt-flow/pull/351),
  [`faf9945`](https://github.com/OpenAdaptAI/openadapt-flow/commit/faf9945537d4011baeb36ce5f063b6e1814903e6))

- Preserve RDP pseudo-step refusals ([#327](https://github.com/OpenAdaptAI/openadapt-flow/pull/327),
  [`ff1a80c`](https://github.com/OpenAdaptAI/openadapt-flow/commit/ff1a80ca7f820b54f265f50bcd21d3a1428edfb5))

- Preserve RDP qualification lease before input
  ([#327](https://github.com/OpenAdaptAI/openadapt-flow/pull/327),
  [`ff1a80c`](https://github.com/OpenAdaptAI/openadapt-flow/commit/ff1a80ca7f820b54f265f50bcd21d3a1428edfb5))

- Preserve signed decision semantics
  ([#339](https://github.com/OpenAdaptAI/openadapt-flow/pull/339),
  [`3b6d33e`](https://github.com/OpenAdaptAI/openadapt-flow/commit/3b6d33eb11f9ef26d6698828ee5ebd82b291b57b))

- Preserve verified judgment evidence on save
  ([#344](https://github.com/OpenAdaptAI/openadapt-flow/pull/344),
  [`ca94cc2`](https://github.com/OpenAdaptAI/openadapt-flow/commit/ca94cc29d3664d9f05ae3b6d9a3f5f3c4741db0a))

- Qualify every RDP visual pointer action
  ([#327](https://github.com/OpenAdaptAI/openadapt-flow/pull/327),
  [`ff1a80c`](https://github.com/OpenAdaptAI/openadapt-flow/commit/ff1a80ca7f820b54f265f50bcd21d3a1428edfb5))

- Qualify RDP campaign under standard profile
  ([#327](https://github.com/OpenAdaptAI/openadapt-flow/pull/327),
  [`ff1a80c`](https://github.com/OpenAdaptAI/openadapt-flow/commit/ff1a80ca7f820b54f265f50bcd21d3a1428edfb5))

- Refresh remote scroll frame lease ([#327](https://github.com/OpenAdaptAI/openadapt-flow/pull/327),
  [`ff1a80c`](https://github.com/OpenAdaptAI/openadapt-flow/commit/ff1a80ca7f820b54f265f50bcd21d3a1428edfb5))

- Require qualification for new decisions
  ([#339](https://github.com/OpenAdaptAI/openadapt-flow/pull/339),
  [`3b6d33e`](https://github.com/OpenAdaptAI/openadapt-flow/commit/3b6d33eb11f9ef26d6698828ee5ebd82b291b57b))

- Resolve moved RDP targets safely ([#327](https://github.com/OpenAdaptAI/openadapt-flow/pull/327),
  [`ff1a80c`](https://github.com/OpenAdaptAI/openadapt-flow/commit/ff1a80ca7f820b54f265f50bcd21d3a1428edfb5))

- Scope identity actuation to remote surfaces
  ([#327](https://github.com/OpenAdaptAI/openadapt-flow/pull/327),
  [`ff1a80c`](https://github.com/OpenAdaptAI/openadapt-flow/commit/ff1a80ca7f820b54f265f50bcd21d3a1428edfb5))

- Use deterministic RDP fixture reset handshake
  ([#327](https://github.com/OpenAdaptAI/openadapt-flow/pull/327),
  [`ff1a80c`](https://github.com/OpenAdaptAI/openadapt-flow/commit/ff1a80ca7f820b54f265f50bcd21d3a1428edfb5))

- Use executable RDP environment identity
  ([#327](https://github.com/OpenAdaptAI/openadapt-flow/pull/327),
  [`ff1a80c`](https://github.com/OpenAdaptAI/openadapt-flow/commit/ff1a80ca7f820b54f265f50bcd21d3a1428edfb5))

- Verify remote text across the live field
  ([#327](https://github.com/OpenAdaptAI/openadapt-flow/pull/327),
  [`ff1a80c`](https://github.com/OpenAdaptAI/openadapt-flow/commit/ff1a80ca7f820b54f265f50bcd21d3a1428edfb5))

- Wait for RDP environment evidence ([#327](https://github.com/OpenAdaptAI/openadapt-flow/pull/327),
  [`ff1a80c`](https://github.com/OpenAdaptAI/openadapt-flow/commit/ff1a80ca7f820b54f265f50bcd21d3a1428edfb5))

- **cli**: Make judgment-case inspection input-free
  ([#344](https://github.com/OpenAdaptAI/openadapt-flow/pull/344),
  [`ca94cc2`](https://github.com/OpenAdaptAI/openadapt-flow/commit/ca94cc29d3664d9f05ae3b6d9a3f5f3c4741db0a))

- **qualification**: Refuse unresolved judgment evidence
  ([#344](https://github.com/OpenAdaptAI/openadapt-flow/pull/344),
  [`ca94cc2`](https://github.com/OpenAdaptAI/openadapt-flow/commit/ca94cc29d3664d9f05ae3b6d9a3f5f3c4741db0a))

- **qualification**: Type local judgment evidence reads
  ([#344](https://github.com/OpenAdaptAI/openadapt-flow/pull/344),
  [`ca94cc2`](https://github.com/OpenAdaptAI/openadapt-flow/commit/ca94cc29d3664d9f05ae3b6d9a3f5f3c4741db0a))

- **qualification**: Verify judgment evidence contracts
  ([#344](https://github.com/OpenAdaptAI/openadapt-flow/pull/344),
  [`ca94cc2`](https://github.com/OpenAdaptAI/openadapt-flow/commit/ca94cc29d3664d9f05ae3b6d9a3f5f3c4741db0a))

### Build System

- Refresh public workflow inventory ([#327](https://github.com/OpenAdaptAI/openadapt-flow/pull/327),
  [`ff1a80c`](https://github.com/OpenAdaptAI/openadapt-flow/commit/ff1a80ca7f820b54f265f50bcd21d3a1428edfb5))

- Require portable decision schemas ([#341](https://github.com/OpenAdaptAI/openadapt-flow/pull/341),
  [`079bbc8`](https://github.com/OpenAdaptAI/openadapt-flow/commit/079bbc863dee82ddae6ccf6c293630f3f6cb78b7))

### Chores

- Diagnose RDP environment marker refusal
  ([#327](https://github.com/OpenAdaptAI/openadapt-flow/pull/327),
  [`ff1a80c`](https://github.com/OpenAdaptAI/openadapt-flow/commit/ff1a80ca7f820b54f265f50bcd21d3a1428edfb5))

- Diagnose RDP pointer delivery ([#327](https://github.com/OpenAdaptAI/openadapt-flow/pull/327),
  [`ff1a80c`](https://github.com/OpenAdaptAI/openadapt-flow/commit/ff1a80ca7f820b54f265f50bcd21d3a1428edfb5))

- Refresh public artifact inventory ([#327](https://github.com/OpenAdaptAI/openadapt-flow/pull/327),
  [`ff1a80c`](https://github.com/OpenAdaptAI/openadapt-flow/commit/ff1a80ca7f820b54f265f50bcd21d3a1428edfb5))

- Register RDP campaign public artifacts
  ([#327](https://github.com/OpenAdaptAI/openadapt-flow/pull/327),
  [`ff1a80c`](https://github.com/OpenAdaptAI/openadapt-flow/commit/ff1a80ca7f820b54f265f50bcd21d3a1428edfb5))

- Report RDP environment preflight ([#327](https://github.com/OpenAdaptAI/openadapt-flow/pull/327),
  [`ff1a80c`](https://github.com/OpenAdaptAI/openadapt-flow/commit/ff1a80ca7f820b54f265f50bcd21d3a1428edfb5))

### Code Style

- Format qualified bundle fixes ([#351](https://github.com/OpenAdaptAI/openadapt-flow/pull/351),
  [`faf9945`](https://github.com/OpenAdaptAI/openadapt-flow/commit/faf9945537d4011baeb36ce5f063b6e1814903e6))

### Continuous Integration

- Allow complete RDP acceptance campaign
  ([#327](https://github.com/OpenAdaptAI/openadapt-flow/pull/327),
  [`ff1a80c`](https://github.com/OpenAdaptAI/openadapt-flow/commit/ff1a80ca7f820b54f265f50bcd21d3a1428edfb5))

- Expose bounded RDP fixture diagnostics
  ([#327](https://github.com/OpenAdaptAI/openadapt-flow/pull/327),
  [`ff1a80c`](https://github.com/OpenAdaptAI/openadapt-flow/commit/ff1a80ca7f820b54f265f50bcd21d3a1428edfb5))

### Documentation

- Align Flow 1.31 release evidence ([#340](https://github.com/OpenAdaptAI/openadapt-flow/pull/340),
  [`c84f6ff`](https://github.com/OpenAdaptAI/openadapt-flow/commit/c84f6ff408a1ab08527eff4c472ed73e3a0380f3))

- Align v2 labels with reviewed vocabulary
  ([#308](https://github.com/OpenAdaptAI/openadapt-flow/pull/308),
  [`e010c9f`](https://github.com/OpenAdaptAI/openadapt-flow/commit/e010c9f46cd5744a9c87f1ab818227f14cdf921c))

- Clarify decision task v2 certification gate
  ([#308](https://github.com/OpenAdaptAI/openadapt-flow/pull/308),
  [`e010c9f`](https://github.com/OpenAdaptAI/openadapt-flow/commit/e010c9f46cd5744a9c87f1ab818227f14cdf921c))

- Define controlled remote entity wording
  ([#308](https://github.com/OpenAdaptAI/openadapt-flow/pull/308),
  [`e010c9f`](https://github.com/OpenAdaptAI/openadapt-flow/commit/e010c9f46cd5744a9c87f1ab818227f14cdf921c))

- Describe v2 qualified decision labels
  ([#308](https://github.com/OpenAdaptAI/openadapt-flow/pull/308),
  [`e010c9f`](https://github.com/OpenAdaptAI/openadapt-flow/commit/e010c9f46cd5744a9c87f1ab818227f14cdf921c))

- Document the current mobile decision path
  ([#342](https://github.com/OpenAdaptAI/openadapt-flow/pull/342),
  [`398f238`](https://github.com/OpenAdaptAI/openadapt-flow/commit/398f238550a577fa5905d9aea93c5e4a015ef1f6))

- Use neutral entity language in shared guidance
  ([#308](https://github.com/OpenAdaptAI/openadapt-flow/pull/308),
  [`e010c9f`](https://github.com/OpenAdaptAI/openadapt-flow/commit/e010c9f46cd5744a9c87f1ab818227f14cdf921c))

### Features

- Add Citrix acceptance preflight runner
  ([#338](https://github.com/OpenAdaptAI/openadapt-flow/pull/338),
  [`1c5c925`](https://github.com/OpenAdaptAI/openadapt-flow/commit/1c5c925a0de7b4216559da3f29b3270328c2918e))

- Add non-actuating business decision service
  ([#350](https://github.com/OpenAdaptAI/openadapt-flow/pull/350),
  [`8160dc4`](https://github.com/OpenAdaptAI/openadapt-flow/commit/8160dc4761f974f59635369733c27631a7516169))

- Add typed decision qualification CLI
  ([#346](https://github.com/OpenAdaptAI/openadapt-flow/pull/346),
  [`a93f535`](https://github.com/OpenAdaptAI/openadapt-flow/commit/a93f5352bca4f2a2b49df1f0aa0fc3e3b66cc31c))

- Add typed durable business decisions
  ([#339](https://github.com/OpenAdaptAI/openadapt-flow/pull/339),
  [`3b6d33e`](https://github.com/OpenAdaptAI/openadapt-flow/commit/3b6d33eb11f9ef26d6698828ee5ebd82b291b57b))

- Attest typed decision relay envelopes
  ([#345](https://github.com/OpenAdaptAI/openadapt-flow/pull/345),
  [`ba8ab0e`](https://github.com/OpenAdaptAI/openadapt-flow/commit/ba8ab0e36ad30c54025564d05be3664d79ccc12f))

- Bind mobile business decisions during qualification
  ([#348](https://github.com/OpenAdaptAI/openadapt-flow/pull/348),
  [`cdbe958`](https://github.com/OpenAdaptAI/openadapt-flow/commit/cdbe95803aa795712917d2374abc957d6bfaca36))

- Bind remote volatility comparison contract
  ([#322](https://github.com/OpenAdaptAI/openadapt-flow/pull/322),
  [`3b56041`](https://github.com/OpenAdaptAI/openadapt-flow/commit/3b560415144811fe3834a68fb864caffde620209))

- Bridge typed business decisions to mobile
  ([#341](https://github.com/OpenAdaptAI/openadapt-flow/pull/341),
  [`079bbc8`](https://github.com/OpenAdaptAI/openadapt-flow/commit/079bbc863dee82ddae6ccf6c293630f3f6cb78b7))

- Capture reviewed judgment cases during qualification
  ([#344](https://github.com/OpenAdaptAI/openadapt-flow/pull/344),
  [`ca94cc2`](https://github.com/OpenAdaptAI/openadapt-flow/commit/ca94cc29d3664d9f05ae3b6d9a3f5f3c4741db0a))

- Connect typed decisions to Cloud relay
  ([#347](https://github.com/OpenAdaptAI/openadapt-flow/pull/347),
  [`3babe8d`](https://github.com/OpenAdaptAI/openadapt-flow/commit/3babe8d7a0d7553ed22fbf3888dbe773122a3b03))

- Emit qualified entity decision tasks v2
  ([#308](https://github.com/OpenAdaptAI/openadapt-flow/pull/308),
  [`e010c9f`](https://github.com/OpenAdaptAI/openadapt-flow/commit/e010c9f46cd5744a9c87f1ab818227f14cdf921c))

- Enforce reviewed decision presentation egress
  ([#341](https://github.com/OpenAdaptAI/openadapt-flow/pull/341),
  [`079bbc8`](https://github.com/OpenAdaptAI/openadapt-flow/commit/079bbc863dee82ddae6ccf6c293630f3f6cb78b7))

- Expose canonical entity label options
  ([#308](https://github.com/OpenAdaptAI/openadapt-flow/pull/308),
  [`e010c9f`](https://github.com/OpenAdaptAI/openadapt-flow/commit/e010c9f46cd5744a9c87f1ab818227f14cdf921c))

- Guide label changes through recertification
  ([#308](https://github.com/OpenAdaptAI/openadapt-flow/pull/308),
  [`e010c9f`](https://github.com/OpenAdaptAI/openadapt-flow/commit/e010c9f46cd5744a9c87f1ab818227f14cdf921c))

- Qualify mobile business decision delivery
  ([#348](https://github.com/OpenAdaptAI/openadapt-flow/pull/348),
  [`cdbe958`](https://github.com/OpenAdaptAI/openadapt-flow/commit/cdbe95803aa795712917d2374abc957d6bfaca36))

- Require current qualification for decision task v2
  ([#308](https://github.com/OpenAdaptAI/openadapt-flow/pull/308),
  [`e010c9f`](https://github.com/OpenAdaptAI/openadapt-flow/commit/e010c9f46cd5744a9c87f1ab818227f14cdf921c))

- Restrict decision entities to reviewed classes
  ([#308](https://github.com/OpenAdaptAI/openadapt-flow/pull/308),
  [`e010c9f`](https://github.com/OpenAdaptAI/openadapt-flow/commit/e010c9f46cd5744a9c87f1ab818227f14cdf921c))

- Route typed decisions across customer runs
  ([#349](https://github.com/OpenAdaptAI/openadapt-flow/pull/349),
  [`0146429`](https://github.com/OpenAdaptAI/openadapt-flow/commit/0146429c962d34dd03da2f9b51f05690fde9b053))

- Select the strongest qualified effect verifier
  ([`ce35ebd`](https://github.com/OpenAdaptAI/openadapt-flow/commit/ce35ebd91eec536f2c52fb6b14591f0ab4dfe37e))

- **qualification**: Add reviewed judgment cases
  ([#344](https://github.com/OpenAdaptAI/openadapt-flow/pull/344),
  [`ca94cc2`](https://github.com/OpenAdaptAI/openadapt-flow/commit/ca94cc29d3664d9f05ae3b6d9a3f5f3c4741db0a))

- **qualification**: Author typed business decisions
  ([#343](https://github.com/OpenAdaptAI/openadapt-flow/pull/343),
  [`fe91441`](https://github.com/OpenAdaptAI/openadapt-flow/commit/fe9144173af025d80d8608499d7c0a71a6c17b49))

- **tutorial**: Add guided human recording and paced replay
  ([#315](https://github.com/OpenAdaptAI/openadapt-flow/pull/315),
  [`d1b1ced`](https://github.com/OpenAdaptAI/openadapt-flow/commit/d1b1ceddda9914ba32f0daee3b364574fbae73b9))

### Testing

- Add RDP visual fault campaign cells
  ([#327](https://github.com/OpenAdaptAI/openadapt-flow/pull/327),
  [`ff1a80c`](https://github.com/OpenAdaptAI/openadapt-flow/commit/ff1a80ca7f820b54f265f50bcd21d3a1428edfb5))

- Add real-RDP multi-window vision campaign
  ([#327](https://github.com/OpenAdaptAI/openadapt-flow/pull/327),
  [`ff1a80c`](https://github.com/OpenAdaptAI/openadapt-flow/commit/ff1a80ca7f820b54f265f50bcd21d3a1428edfb5))

- Bound and checkpoint the RDP campaign
  ([#327](https://github.com/OpenAdaptAI/openadapt-flow/pull/327),
  [`ff1a80c`](https://github.com/OpenAdaptAI/openadapt-flow/commit/ff1a80ca7f820b54f265f50bcd21d3a1428edfb5))

- Complete RDP multiapp display drift campaign
  ([#327](https://github.com/OpenAdaptAI/openadapt-flow/pull/327),
  [`ff1a80c`](https://github.com/OpenAdaptAI/openadapt-flow/commit/ff1a80ca7f820b54f265f50bcd21d3a1428edfb5))

- Enforce remote scroll preflight order
  ([#327](https://github.com/OpenAdaptAI/openadapt-flow/pull/327),
  [`ff1a80c`](https://github.com/OpenAdaptAI/openadapt-flow/commit/ff1a80ca7f820b54f265f50bcd21d3a1428edfb5))

- Exercise uncertain RDP save delivery
  ([#327](https://github.com/OpenAdaptAI/openadapt-flow/pull/327),
  [`ff1a80c`](https://github.com/OpenAdaptAI/openadapt-flow/commit/ff1a80ca7f820b54f265f50bcd21d3a1428edfb5))

- Export bounded RDP failure diagnostics
  ([#327](https://github.com/OpenAdaptAI/openadapt-flow/pull/327),
  [`ff1a80c`](https://github.com/OpenAdaptAI/openadapt-flow/commit/ff1a80ca7f820b54f265f50bcd21d3a1428edfb5))

- Keep tutorial checks behavioral ([#315](https://github.com/OpenAdaptAI/openadapt-flow/pull/315),
  [`d1b1ced`](https://github.com/OpenAdaptAI/openadapt-flow/commit/d1b1ceddda9914ba32f0daee3b364574fbae73b9))

- Make uncertain RDP delivery evidence exact
  ([#327](https://github.com/OpenAdaptAI/openadapt-flow/pull/327),
  [`ff1a80c`](https://github.com/OpenAdaptAI/openadapt-flow/commit/ff1a80ca7f820b54f265f50bcd21d3a1428edfb5))

- Retain bounded RDP failure frames ([#327](https://github.com/OpenAdaptAI/openadapt-flow/pull/327),
  [`ff1a80c`](https://github.com/OpenAdaptAI/openadapt-flow/commit/ff1a80ca7f820b54f265f50bcd21d3a1428edfb5))

- Retain bounded RDP harness failure location
  ([#327](https://github.com/OpenAdaptAI/openadapt-flow/pull/327),
  [`ff1a80c`](https://github.com/OpenAdaptAI/openadapt-flow/commit/ff1a80ca7f820b54f265f50bcd21d3a1428edfb5))

- Strengthen RDP vision acceptance oracles
  ([#327](https://github.com/OpenAdaptAI/openadapt-flow/pull/327),
  [`ff1a80c`](https://github.com/OpenAdaptAI/openadapt-flow/commit/ff1a80ca7f820b54f265f50bcd21d3a1428edfb5))

- **tutorial**: Preserve break-it CLI coverage
  ([#315](https://github.com/OpenAdaptAI/openadapt-flow/pull/315),
  [`d1b1ced`](https://github.com/OpenAdaptAI/openadapt-flow/commit/d1b1ceddda9914ba32f0daee3b364574fbae73b9))

## v1.30.0 (2026-08-05)


### Bug Fixes

- **ci**: Bound the identity-ladder harness so the fast lane cannot time out
  ([#333](https://github.com/OpenAdaptAI/openadapt-flow/pull/333),
  [`ddbbd64`](https://github.com/OpenAdaptAI/openadapt-flow/commit/ddbbd643e87f213141bfea4604d89b0604b0f7ba))

### Chores

- Regenerate reviewed public artifact inventory for the ci.yml lane change
  ([#333](https://github.com/OpenAdaptAI/openadapt-flow/pull/333),
  [`ddbbd64`](https://github.com/OpenAdaptAI/openadapt-flow/commit/ddbbd643e87f213141bfea4604d89b0604b0f7ba))

### Documentation

- Measured decomposition plan for runtime/replayer.py
  ([#330](https://github.com/OpenAdaptAI/openadapt-flow/pull/330),
  [`caa08b0`](https://github.com/OpenAdaptAI/openadapt-flow/commit/caa08b00b7688b0a17130588eed1300e67b5a1b1))

### Features

- Emit synthetic managed delivery marker
  ([#337](https://github.com/OpenAdaptAI/openadapt-flow/pull/337),
  [`c150ad5`](https://github.com/OpenAdaptAI/openadapt-flow/commit/c150ad597214eaca27350f8d21f611caa71aade5))

### Testing

- Cover synthetic delivery marker observer boundaries
  ([#337](https://github.com/OpenAdaptAI/openadapt-flow/pull/337),
  [`c150ad5`](https://github.com/OpenAdaptAI/openadapt-flow/commit/c150ad597214eaca27350f8d21f611caa71aade5))

## v1.29.0 (2026-08-02)


### Documentation

- Align entry commands and substrate maturity across repo READMEs
  ([#329](https://github.com/OpenAdaptAI/openadapt-flow/pull/329),
  [`f9f866b`](https://github.com/OpenAdaptAI/openadapt-flow/commit/f9f866b9f323ca032a104fe300c94aeebf8db355))

### Features

- **tutorial**: Add --break-it, the caught-fault demonstration
  ([#331](https://github.com/OpenAdaptAI/openadapt-flow/pull/331),
  [`902b987`](https://github.com/OpenAdaptAI/openadapt-flow/commit/902b9871ae0964744d6a9e0ae8a87973d654f838))

## v1.28.0 (2026-08-02)


### Bug Fixes

- Deliver consequential remote clicks through the frame lease
  ([`c9618cc`](https://github.com/OpenAdaptAI/openadapt-flow/commit/c9618ccb22753395d2fbe2c1a2dfb2424610730a))

- Simplify the RDP buyer presentation
  ([`ccdd155`](https://github.com/OpenAdaptAI/openadapt-flow/commit/ccdd155ab2de571e4da32a17238a2f359fd067ad))

### Features

- Carry managed Execute authority through BYOC
  ([`e054caa`](https://github.com/OpenAdaptAI/openadapt-flow/commit/e054caa4c888e90c0732fccfeb9151e36051661f))

## v1.27.1 (2026-07-31)


### Bug Fixes

- Clarify the RDP proof presentation
  ([#324](https://github.com/OpenAdaptAI/openadapt-flow/pull/324),
  [`ca1b9c4`](https://github.com/OpenAdaptAI/openadapt-flow/commit/ca1b9c4884f3fc70af282274a9571fb9cad88d04))

- **qualification**: Bind public evidence to case authority
  ([`bd4c81e`](https://github.com/OpenAdaptAI/openadapt-flow/commit/bd4c81e7e33353d275d6f4320663e6e187f73198))

- **qualification**: Run sealed local cases through governed authority
  ([`fafdf0e`](https://github.com/OpenAdaptAI/openadapt-flow/commit/fafdf0e8bef766851563eb092015f4a266bb7f32))

### Chores

- Improve RDP demo layout and pacing
  ([`c535bab`](https://github.com/OpenAdaptAI/openadapt-flow/commit/c535bab13e544b0d7e24c8bb0d2d6c0dfded7e16))

- **rdp**: Export evidence-bound hybrid timeline
  ([`6f87e87`](https://github.com/OpenAdaptAI/openadapt-flow/commit/6f87e877faf2749e732c01b31ac4e8673c3cddde))

- **rdp**: Export proof-linked buyer demo
  ([`6673dd9`](https://github.com/OpenAdaptAI/openadapt-flow/commit/6673dd97de169d8c500031c4ac4c64a95bb71656))

- **rdp**: Publish exact appointment qualification proof
  ([`6846b69`](https://github.com/OpenAdaptAI/openadapt-flow/commit/6846b699027f2696c1604461d87cb0895d87a52e))

### Code Style

- Format qualification authority changes
  ([`bd4c81e`](https://github.com/OpenAdaptAI/openadapt-flow/commit/bd4c81e7e33353d275d6f4320663e6e187f73198))

## v1.27.0 (2026-07-29)


### Features

- Add attended reconciliation action
  ([#307](https://github.com/OpenAdaptAI/openadapt-flow/pull/307),
  [`e8fb96a`](https://github.com/OpenAdaptAI/openadapt-flow/commit/e8fb96a646338d44e0e643ca28127b0083e89cd4))

## v1.26.0 (2026-07-29)


### Bug Fixes

- Correct OpenEMR saved-row benchmark claims
  ([#302](https://github.com/OpenAdaptAI/openadapt-flow/pull/302),
  [`aee0941`](https://github.com/OpenAdaptAI/openadapt-flow/commit/aee094193b232f472f991be6fa9b33c3c4b3f9be))

- Preserve visual field verification scope
  ([#291](https://github.com/OpenAdaptAI/openadapt-flow/pull/291),
  [`45f2700`](https://github.com/OpenAdaptAI/openadapt-flow/commit/45f2700991e8a3885d86dce11e85a534083bda11))

- Require saved-row context in OpenEMR oracle
  ([#302](https://github.com/OpenAdaptAI/openadapt-flow/pull/302),
  [`aee0941`](https://github.com/OpenAdaptAI/openadapt-flow/commit/aee094193b232f472f991be6fa9b33c3c4b3f9be))

- **benchmark**: Check the MockMed encounter type as its own field
  ([#302](https://github.com/OpenAdaptAI/openadapt-flow/pull/302),
  [`aee0941`](https://github.com/OpenAdaptAI/openadapt-flow/commit/aee094193b232f472f991be6fa9b33c3c4b3f9be))

- **benchmark**: Refuse ambiguous bare note rows
  ([#302](https://github.com/OpenAdaptAI/openadapt-flow/pull/302),
  [`aee0941`](https://github.com/OpenAdaptAI/openadapt-flow/commit/aee094193b232f472f991be6fa9b33c3c4b3f9be))

- **ci**: Keep unrelated release failures inside grace
  ([#288](https://github.com/OpenAdaptAI/openadapt-flow/pull/288),
  [`f133bbd`](https://github.com/OpenAdaptAI/openadapt-flow/commit/f133bbdbe19fab6b8f4c7a8aef315736de1ca04b))

- **ci**: Make release-health alerts fail observable
  ([#288](https://github.com/OpenAdaptAI/openadapt-flow/pull/288),
  [`f133bbd`](https://github.com/OpenAdaptAI/openadapt-flow/commit/f133bbdbe19fab6b8f4c7a8aef315736de1ca04b))

- **compiler**: Prove selection anchor to type checker
  ([#291](https://github.com/OpenAdaptAI/openadapt-flow/pull/291),
  [`45f2700`](https://github.com/OpenAdaptAI/openadapt-flow/commit/45f2700991e8a3885d86dce11e85a534083bda11))

- **compiler**: Retain exact opaque field labels
  ([`67ebec2`](https://github.com/OpenAdaptAI/openadapt-flow/commit/67ebec2afa8253df738a34a75e6deb1228c94a78))

- **console**: Adopt the shared receipt contract and close a free-text field
  ([#290](https://github.com/OpenAdaptAI/openadapt-flow/pull/290),
  [`156562a`](https://github.com/OpenAdaptAI/openadapt-flow/commit/156562a032dd2fe3c5e775d0cb1d2553c81691d6))

- **console**: Stop a rejected run from lingering as an answerable pause
  ([#295](https://github.com/OpenAdaptAI/openadapt-flow/pull/295),
  [`736ff8f`](https://github.com/OpenAdaptAI/openadapt-flow/commit/736ff8f3be74d3715d854b3addf4e6680ae61a28))

- **decisions**: Bind the delivery ceiling to the run's actual profile
  ([#296](https://github.com/OpenAdaptAI/openadapt-flow/pull/296),
  [`42114ca`](https://github.com/OpenAdaptAI/openadapt-flow/commit/42114ca6eac02fbb48efb773b08e6b1de82c4d63))

- **decisions**: Do not report a memoized publish as an observation
  ([#297](https://github.com/OpenAdaptAI/openadapt-flow/pull/297),
  [`3aa71b6`](https://github.com/OpenAdaptAI/openadapt-flow/commit/3aa71b63d39c575cdbb90620235e1e4ee1237a83))

- **decisions**: Let the relay carry every answer, not only Continue
  ([#296](https://github.com/OpenAdaptAI/openadapt-flow/pull/296),
  [`42114ca`](https://github.com/OpenAdaptAI/openadapt-flow/commit/42114ca6eac02fbb48efb773b08e6b1de82c4d63))

- **decisions**: One refused pause must not silence the rest of the queue
  ([#297](https://github.com/OpenAdaptAI/openadapt-flow/pull/297),
  [`3aa71b6`](https://github.com/OpenAdaptAI/openadapt-flow/commit/3aa71b63d39c575cdbb90620235e1e4ee1237a83))

- **decisions**: Preserve completed outcome across lost ack
  ([#297](https://github.com/OpenAdaptAI/openadapt-flow/pull/297),
  [`3aa71b6`](https://github.com/OpenAdaptAI/openadapt-flow/commit/3aa71b63d39c575cdbb90620235e1e4ee1237a83))

- **decisions**: Recover lost acknowledgements after restart
  ([#297](https://github.com/OpenAdaptAI/openadapt-flow/pull/297),
  [`3aa71b6`](https://github.com/OpenAdaptAI/openadapt-flow/commit/3aa71b63d39c575cdbb90620235e1e4ee1237a83))

- **decisions**: Stop the loop spinning on a decision it always refuses
  ([#297](https://github.com/OpenAdaptAI/openadapt-flow/pull/297),
  [`3aa71b6`](https://github.com/OpenAdaptAI/openadapt-flow/commit/3aa71b63d39c575cdbb90620235e1e4ee1237a83))

- **decisions**: Type the halt-context schema constant as its literal
  ([#296](https://github.com/OpenAdaptAI/openadapt-flow/pull/296),
  [`42114ca`](https://github.com/OpenAdaptAI/openadapt-flow/commit/42114ca6eac02fbb48efb773b08e6b1de82c4d63))

- **docs**: Correct a false halt claim; pin HOW the free path reaches VERIFIED
  ([#298](https://github.com/OpenAdaptAI/openadapt-flow/pull/298),
  [`6755c61`](https://github.com/OpenAdaptAI/openadapt-flow/commit/6755c6153385253df758064766609480efa198b3))

- **docs**: Correct the halt outcome the tutorial's fault probe actually reports
  ([#298](https://github.com/OpenAdaptAI/openadapt-flow/pull/298),
  [`6755c61`](https://github.com/OpenAdaptAI/openadapt-flow/commit/6755c6153385253df758064766609480efa198b3))

- **paper**: Bind the corrected OpenEMR count
  ([#302](https://github.com/OpenAdaptAI/openadapt-flow/pull/302),
  [`aee0941`](https://github.com/OpenAdaptAI/openadapt-flow/commit/aee094193b232f472f991be6fa9b33c3c4b3f9be))

- **rdp**: Bind identity checks to actuation frame
  ([#291](https://github.com/OpenAdaptAI/openadapt-flow/pull/291),
  [`45f2700`](https://github.com/OpenAdaptAI/openadapt-flow/commit/45f2700991e8a3885d86dce11e85a534083bda11))

- **rdp**: Preserve native typeahead delivery
  ([#291](https://github.com/OpenAdaptAI/openadapt-flow/pull/291),
  [`45f2700`](https://github.com/OpenAdaptAI/openadapt-flow/commit/45f2700991e8a3885d86dce11e85a534083bda11))

- **rdp**: Revalidate focused selects from context
  ([#299](https://github.com/OpenAdaptAI/openadapt-flow/pull/299),
  [`3481b42`](https://github.com/OpenAdaptAI/openadapt-flow/commit/3481b42d060369ea45d75478ec13f0fc1e77e251))

- **remote**: Require direct target evidence for scroll readiness
  ([#291](https://github.com/OpenAdaptAI/openadapt-flow/pull/291),
  [`45f2700`](https://github.com/OpenAdaptAI/openadapt-flow/commit/45f2700991e8a3885d86dce11e85a534083bda11))

### Chores

- Gitignore .private/ ([#292](https://github.com/OpenAdaptAI/openadapt-flow/pull/292),
  [`02a1b5d`](https://github.com/OpenAdaptAI/openadapt-flow/commit/02a1b5d512a337ca6d6f373b0ff04a0c9073a60a))

- Refresh public artifact inventory ([#302](https://github.com/OpenAdaptAI/openadapt-flow/pull/302),
  [`aee0941`](https://github.com/OpenAdaptAI/openadapt-flow/commit/aee094193b232f472f991be6fa9b33c3c4b3f9be))

- Regenerate the reviewed public artifact inventory for console.js
  ([#290](https://github.com/OpenAdaptAI/openadapt-flow/pull/290),
  [`156562a`](https://github.com/OpenAdaptAI/openadapt-flow/commit/156562a032dd2fe3c5e775d0cb1d2553c81691d6))

- **release**: Derive the private-artifact rules from the policy manifest
  ([#300](https://github.com/OpenAdaptAI/openadapt-flow/pull/300),
  [`83b56d4`](https://github.com/OpenAdaptAI/openadapt-flow/commit/83b56d4ec5a68bf649007b54efeba1300561be5f))

- **release**: Re-review the public artifact inventory for console.js
  ([#295](https://github.com/OpenAdaptAI/openadapt-flow/pull/295),
  [`736ff8f`](https://github.com/OpenAdaptAI/openadapt-flow/commit/736ff8f3be74d3715d854b3addf4e6680ae61a28))

### Code Style

- Format benchmark oracle changes ([#302](https://github.com/OpenAdaptAI/openadapt-flow/pull/302),
  [`aee0941`](https://github.com/OpenAdaptAI/openadapt-flow/commit/aee094193b232f472f991be6fa9b33c3c4b3f9be))

### Continuous Integration

- Bump the actions group with 2 updates
  ([#286](https://github.com/OpenAdaptAI/openadapt-flow/pull/286),
  [`cee096e`](https://github.com/OpenAdaptAI/openadapt-flow/commit/cee096e0c00f93fe0d9f65c8998902039d17c69b))

- Re-sign the public artifact inventory for release-health.yml
  ([#288](https://github.com/OpenAdaptAI/openadapt-flow/pull/288),
  [`f133bbd`](https://github.com/OpenAdaptAI/openadapt-flow/commit/f133bbdbe19fab6b8f4c7a8aef315736de1ca04b))

### Documentation

- **decisions**: Say why an unresolvable decision is the safe failure
  ([#297](https://github.com/OpenAdaptAI/openadapt-flow/pull/297),
  [`3aa71b6`](https://github.com/OpenAdaptAI/openadapt-flow/commit/3aa71b63d39c575cdbb90620235e1e4ee1237a83))

- **effect-kit**: Note that a refuted STEP is not a refuted RUN
  ([#298](https://github.com/OpenAdaptAI/openadapt-flow/pull/298),
  [`6755c61`](https://github.com/OpenAdaptAI/openadapt-flow/commit/6755c6153385253df758064766609480efa198b3))

- **exception-inbox**: Record the new delivery tier and the outbound relay
  ([#296](https://github.com/OpenAdaptAI/openadapt-flow/pull/296),
  [`42114ca`](https://github.com/OpenAdaptAI/openadapt-flow/commit/42114ca6eac02fbb48efb773b08e6b1de82c4d63))

### Features

- Bind governed authorization templates to hosted attestations
  ([`263683a`](https://github.com/OpenAdaptAI/openadapt-flow/commit/263683ac19ae60f3ced076980688291e048f8cfa))

- Complete governed qualification and managed delivery authority
  ([`bc4a49c`](https://github.com/OpenAdaptAI/openadapt-flow/commit/bc4a49c3632fedc6ec0dc41ad8bd9eeb5611564a))

- **attended**: Let an operator reject a halt and end the run
  ([#295](https://github.com/OpenAdaptAI/openadapt-flow/pull/295),
  [`736ff8f`](https://github.com/OpenAdaptAI/openadapt-flow/commit/736ff8f3be74d3715d854b3addf4e6680ae61a28))

- **console**: Close the mobile attended-decision loop with an e2e acceptance test
  ([#290](https://github.com/OpenAdaptAI/openadapt-flow/pull/290),
  [`156562a`](https://github.com/OpenAdaptAI/openadapt-flow/commit/156562a032dd2fe3c5e775d0cb1d2553c81691d6))

- **console**: Tell the operator what broke, in the local projection only
  ([#290](https://github.com/OpenAdaptAI/openadapt-flow/pull/290),
  [`156562a`](https://github.com/OpenAdaptAI/openadapt-flow/commit/156562a032dd2fe3c5e775d0cb1d2553c81691d6))

- **decisions**: Deliver halts to a phone without customer TLS ingress
  ([#296](https://github.com/OpenAdaptAI/openadapt-flow/pull/296),
  [`42114ca`](https://github.com/OpenAdaptAI/openadapt-flow/commit/42114ca6eac02fbb48efb773b08e6b1de82c4d63))

- **decisions**: Run the outbound decision lane, so a halt reaches a phone
  ([#297](https://github.com/OpenAdaptAI/openadapt-flow/pull/297),
  [`3aa71b6`](https://github.com/OpenAdaptAI/openadapt-flow/commit/3aa71b63d39c575cdbb90620235e1e4ee1237a83))

- **rdp**: Atomically verify native option selections
  ([#291](https://github.com/OpenAdaptAI/openadapt-flow/pull/291),
  [`45f2700`](https://github.com/OpenAdaptAI/openadapt-flow/commit/45f2700991e8a3885d86dce11e85a534083bda11))

- **rdp**: Compile atomic native option selections
  ([#291](https://github.com/OpenAdaptAI/openadapt-flow/pull/291),
  [`45f2700`](https://github.com/OpenAdaptAI/openadapt-flow/commit/45f2700991e8a3885d86dce11e85a534083bda11))

### Refactoring

- **attended**: Read the run report without depending on the console
  ([#295](https://github.com/OpenAdaptAI/openadapt-flow/pull/295),
  [`736ff8f`](https://github.com/OpenAdaptAI/openadapt-flow/commit/736ff8f3be74d3715d854b3addf4e6680ae61a28))

### Testing

- **attended**: Pin that a rejection survives an unreadable run report
  ([#295](https://github.com/OpenAdaptAI/openadapt-flow/pull/295),
  [`736ff8f`](https://github.com/OpenAdaptAI/openadapt-flow/commit/736ff8f3be74d3715d854b3addf4e6680ae61a28))

- **attended**: Pin that rejecting a run that already wrote cannot claim absence
  ([#295](https://github.com/OpenAdaptAI/openadapt-flow/pull/295),
  [`736ff8f`](https://github.com/OpenAdaptAI/openadapt-flow/commit/736ff8f3be74d3715d854b3addf4e6680ae61a28))

- **benchmark**: Isolate encounter type parser
  ([#302](https://github.com/OpenAdaptAI/openadapt-flow/pull/302),
  [`aee0941`](https://github.com/OpenAdaptAI/openadapt-flow/commit/aee094193b232f472f991be6fa9b33c3c4b3f9be))

- **console**: Pin the exact receipt contract a portal shell must render
  ([#290](https://github.com/OpenAdaptAI/openadapt-flow/pull/290),
  [`156562a`](https://github.com/OpenAdaptAI/openadapt-flow/commit/156562a032dd2fe3c5e775d0cb1d2553c81691d6))

- **decisions**: Pin that a phone's Reject ends the run it names
  ([#297](https://github.com/OpenAdaptAI/openadapt-flow/pull/297),
  [`3aa71b6`](https://github.com/OpenAdaptAI/openadapt-flow/commit/3aa71b63d39c575cdbb90620235e1e4ee1237a83))

- **e2e**: Pin HOW the free path reaches VERIFIED, not only that it does
  ([#298](https://github.com/OpenAdaptAI/openadapt-flow/pull/298),
  [`6755c61`](https://github.com/OpenAdaptAI/openadapt-flow/commit/6755c6153385253df758064766609480efa198b3))

- **halt-detail**: Stop a digest collision reading as a PHI leak
  ([#296](https://github.com/OpenAdaptAI/openadapt-flow/pull/296),
  [`42114ca`](https://github.com/OpenAdaptAI/openadapt-flow/commit/42114ca6eac02fbb48efb773b08e6b1de82c4d63))

## v1.25.1 (2026-07-27)


### Bug Fixes

- **compiler**: Stop a parameter's demonstrated value becoming a pixel invariant
  ([#285](https://github.com/OpenAdaptAI/openadapt-flow/pull/285),
  [`c068554`](https://github.com/OpenAdaptAI/openadapt-flow/commit/c068554edd280b4b7defe1233c564bfab6a312f5))

- **receipt**: Close remaining verified-evidence gaps
  ([#289](https://github.com/OpenAdaptAI/openadapt-flow/pull/289),
  [`2be3327`](https://github.com/OpenAdaptAI/openadapt-flow/commit/2be33278f41a5dc232133da515ae3bebbe8656bb))

- **receipt**: Require complete evidence for VERIFIED receipts
  ([#289](https://github.com/OpenAdaptAI/openadapt-flow/pull/289),
  [`2be3327`](https://github.com/OpenAdaptAI/openadapt-flow/commit/2be33278f41a5dc232133da515ae3bebbe8656bb))

- **receipt**: Require complete verified evidence
  ([#289](https://github.com/OpenAdaptAI/openadapt-flow/pull/289),
  [`2be3327`](https://github.com/OpenAdaptAI/openadapt-flow/commit/2be33278f41a5dc232133da515ae3bebbe8656bb))

- **receipt**: Revalidate complete verified evidence
  ([#289](https://github.com/OpenAdaptAI/openadapt-flow/pull/289),
  [`2be3327`](https://github.com/OpenAdaptAI/openadapt-flow/commit/2be33278f41a5dc232133da515ae3bebbe8656bb))

### Documentation

- Label every headline benchmark number with the engine it was measured on
  ([#284](https://github.com/OpenAdaptAI/openadapt-flow/pull/284),
  [`40b7960`](https://github.com/OpenAdaptAI/openadapt-flow/commit/40b7960b676c73f1c79212e77b5437453b51439b))

- Re-pin the reviewed public artifact inventory hashes
  ([#284](https://github.com/OpenAdaptAI/openadapt-flow/pull/284),
  [`40b7960`](https://github.com/OpenAdaptAI/openadapt-flow/commit/40b7960b676c73f1c79212e77b5437453b51439b))

- Separate benchmark measurement from artifact provenance
  ([#284](https://github.com/OpenAdaptAI/openadapt-flow/pull/284),
  [`40b7960`](https://github.com/OpenAdaptAI/openadapt-flow/commit/40b7960b676c73f1c79212e77b5437453b51439b))

## v1.25.0 (2026-07-27)


### Bug Fixes

- Apply qualified risk overrides to runtime
  ([#269](https://github.com/OpenAdaptAI/openadapt-flow/pull/269),
  [`6874a08`](https://github.com/OpenAdaptAI/openadapt-flow/commit/6874a08002fc63ece964734d74c84ac277cc55d2))

- Bind attended authority to qualified safety paths
  ([#275](https://github.com/OpenAdaptAI/openadapt-flow/pull/275),
  [`1ee0200`](https://github.com/OpenAdaptAI/openadapt-flow/commit/1ee02004e5040d994638ac3585388e98d2b16e70))

- Bind qualification to policy and effect paths
  ([#269](https://github.com/OpenAdaptAI/openadapt-flow/pull/269),
  [`6874a08`](https://github.com/OpenAdaptAI/openadapt-flow/commit/6874a08002fc63ece964734d74c84ac277cc55d2))

- Bind qualified policy authority end to end
  ([#269](https://github.com/OpenAdaptAI/openadapt-flow/pull/269),
  [`6874a08`](https://github.com/OpenAdaptAI/openadapt-flow/commit/6874a08002fc63ece964734d74c84ac277cc55d2))

- Bind qualified risk and effect policy end to end
  ([#269](https://github.com/OpenAdaptAI/openadapt-flow/pull/269),
  [`6874a08`](https://github.com/OpenAdaptAI/openadapt-flow/commit/6874a08002fc63ece964734d74c84ac277cc55d2))

- Bind qualified risk authority ([#269](https://github.com/OpenAdaptAI/openadapt-flow/pull/269),
  [`6874a08`](https://github.com/OpenAdaptAI/openadapt-flow/commit/6874a08002fc63ece964734d74c84ac277cc55d2))

- Bind typed risk in qualification lane
  ([#269](https://github.com/OpenAdaptAI/openadapt-flow/pull/269),
  [`6874a08`](https://github.com/OpenAdaptAI/openadapt-flow/commit/6874a08002fc63ece964734d74c84ac277cc55d2))

- Infer click risk from accessible labels
  ([#278](https://github.com/OpenAdaptAI/openadapt-flow/pull/278),
  [`befae86`](https://github.com/OpenAdaptAI/openadapt-flow/commit/befae865a9cda8fdfe18059e40e6cc034ed8331a))

- Keep API effects out of GUI approvals
  ([#269](https://github.com/OpenAdaptAI/openadapt-flow/pull/269),
  [`6874a08`](https://github.com/OpenAdaptAI/openadapt-flow/commit/6874a08002fc63ece964734d74c84ac277cc55d2))

- **paper**: Align artifact constants with the corrected absence semantics
  ([#280](https://github.com/OpenAdaptAI/openadapt-flow/pull/280),
  [`11c115c`](https://github.com/OpenAdaptAI/openadapt-flow/commit/11c115ca4641cd2fca5c6e1501ff129a8860fca3))

- **risk**: Stop flagging text-field focus clicks as ambiguous
  ([#278](https://github.com/OpenAdaptAI/openadapt-flow/pull/278),
  [`befae86`](https://github.com/OpenAdaptAI/openadapt-flow/commit/befae865a9cda8fdfe18059e40e6cc034ed8331a))

- **transaction**: Close reconciliation evidence gaps
  ([#280](https://github.com/OpenAdaptAI/openadapt-flow/pull/280),
  [`11c115c`](https://github.com/OpenAdaptAI/openadapt-flow/commit/11c115ca4641cd2fca5c6e1501ff129a8860fca3))

- **transaction**: Prove absence at delivery boundary
  ([#280](https://github.com/OpenAdaptAI/openadapt-flow/pull/280),
  [`11c115c`](https://github.com/OpenAdaptAI/openadapt-flow/commit/11c115ca4641cd2fca5c6e1501ff129a8860fca3))

- **transaction**: Require positive evidence of absence for HALTED_BEFORE_EFFECT
  ([#280](https://github.com/OpenAdaptAI/openadapt-flow/pull/280),
  [`11c115c`](https://github.com/OpenAdaptAI/openadapt-flow/commit/11c115ca4641cd2fca5c6e1501ff129a8860fca3))

### Chores

- Refresh public artifact inventory ([#274](https://github.com/OpenAdaptAI/openadapt-flow/pull/274),
  [`3011558`](https://github.com/OpenAdaptAI/openadapt-flow/commit/30115589a984345fa472af2264778bd43725cb1e))

### Code Style

- Format browser setup contract ([#273](https://github.com/OpenAdaptAI/openadapt-flow/pull/273),
  [`d5ce14f`](https://github.com/OpenAdaptAI/openadapt-flow/commit/d5ce14fff52f4bf5a0897280c0141b6526d469ae))

- Format identity signal tests ([#279](https://github.com/OpenAdaptAI/openadapt-flow/pull/279),
  [`3f34200`](https://github.com/OpenAdaptAI/openadapt-flow/commit/3f34200a438728480d3d1bcbcba4cc39160143ed))

- Format qualification override ([#269](https://github.com/OpenAdaptAI/openadapt-flow/pull/269),
  [`6874a08`](https://github.com/OpenAdaptAI/openadapt-flow/commit/6874a08002fc63ece964734d74c84ac277cc55d2))

### Continuous Integration

- Detect unreleased work and silently skipped publishes
  ([#283](https://github.com/OpenAdaptAI/openadapt-flow/pull/283),
  [`cc835fc`](https://github.com/OpenAdaptAI/openadapt-flow/commit/cc835fc31f6b505d77774525b3a282bc1f20e354))

- Move recurring qualification matrices to weekly gates
  ([#274](https://github.com/OpenAdaptAI/openadapt-flow/pull/274),
  [`3011558`](https://github.com/OpenAdaptAI/openadapt-flow/commit/30115589a984345fa472af2264778bd43725cb1e))

- Register the release-health artifacts in the reviewed public inventory
  ([#283](https://github.com/OpenAdaptAI/openadapt-flow/pull/283),
  [`cc835fc`](https://github.com/OpenAdaptAI/openadapt-flow/commit/cc835fc31f6b505d77774525b3a282bc1f20e354))

- Reserve recurring qualification matrices for weekly gates
  ([#274](https://github.com/OpenAdaptAI/openadapt-flow/pull/274),
  [`3011558`](https://github.com/OpenAdaptAI/openadapt-flow/commit/30115589a984345fa472af2264778bd43725cb1e))

### Documentation

- Bind the per-scenario coverage matrix to its artifact slip sets
  ([#277](https://github.com/OpenAdaptAI/openadapt-flow/pull/277),
  [`153bc3f`](https://github.com/OpenAdaptAI/openadapt-flow/commit/153bc3f15b179fa5186d921e48bbba1233eeaf3a))

- Make the paper submission-ready and discharge both adversarial reviews
  ([#277](https://github.com/OpenAdaptAI/openadapt-flow/pull/277),
  [`153bc3f`](https://github.com/OpenAdaptAI/openadapt-flow/commit/153bc3f15b179fa5186d921e48bbba1233eeaf3a))

- Remove a duplicated scoping sentence from related work
  ([#277](https://github.com/OpenAdaptAI/openadapt-flow/pull/277),
  [`153bc3f`](https://github.com/OpenAdaptAI/openadapt-flow/commit/153bc3f15b179fa5186d921e48bbba1233eeaf3a))

- **effectbench**: Frame reference counts as pinned fixture values, not a result
  ([#276](https://github.com/OpenAdaptAI/openadapt-flow/pull/276),
  [`ab64734`](https://github.com/OpenAdaptAI/openadapt-flow/commit/ab64734a85d93c552ff956390f11403654f61799))

### Features

- Add signed attended decision portal
  ([#272](https://github.com/OpenAdaptAI/openadapt-flow/pull/272),
  [`90fc48f`](https://github.com/OpenAdaptAI/openadapt-flow/commit/90fc48ff964882c2adaf63532b8fad60b34d4faa))

- Bind PHI-free qualified identity signals
  ([#279](https://github.com/OpenAdaptAI/openadapt-flow/pull/279),
  [`3f34200`](https://github.com/OpenAdaptAI/openadapt-flow/commit/3f34200a438728480d3d1bcbcba4cc39160143ed))

- Bind remote attended decisions to AAL2
  ([#275](https://github.com/OpenAdaptAI/openadapt-flow/pull/275),
  [`1ee0200`](https://github.com/OpenAdaptAI/openadapt-flow/commit/1ee02004e5040d994638ac3585388e98d2b16e70))

- Make browser runtime an opt-in capability
  ([#273](https://github.com/OpenAdaptAI/openadapt-flow/pull/273),
  [`d5ce14f`](https://github.com/OpenAdaptAI/openadapt-flow/commit/d5ce14fff52f4bf5a0897280c0141b6526d469ae))

- **tutorial**: Make the free path reach VERIFIED and emit a local receipt
  ([#281](https://github.com/OpenAdaptAI/openadapt-flow/pull/281),
  [`2752813`](https://github.com/OpenAdaptAI/openadapt-flow/commit/2752813ccb12910732d8b4c174e717c4b120e04e))

### Testing

- Reconcile partially verified tutorial fault
  ([#280](https://github.com/OpenAdaptAI/openadapt-flow/pull/280),
  [`11c115c`](https://github.com/OpenAdaptAI/openadapt-flow/commit/11c115ca4641cd2fca5c6e1501ff129a8860fca3))

## v1.24.0 (2026-07-27)


### Bug Fixes

- Harden rich action admission ([#268](https://github.com/OpenAdaptAI/openadapt-flow/pull/268),
  [`9dcda2d`](https://github.com/OpenAdaptAI/openadapt-flow/commit/9dcda2d9407d1deba0099de2c93e55c39c37dd89))

- Invalidate certification when sealing bundles
  ([`a1edd0a`](https://github.com/OpenAdaptAI/openadapt-flow/commit/a1edd0a5249536cc6ec4daed4ecbf6d44d0adf30))

- Keep generated benchmark runs out of distributions
  ([`39070de`](https://github.com/OpenAdaptAI/openadapt-flow/commit/39070de25832680a3745cfa6e1650f44a7e648b2))

- Make browser actuation guards semantic
  ([`130b9be`](https://github.com/OpenAdaptAI/openadapt-flow/commit/130b9becf58c1fb9ad3b269a2010506162d78ad7))

- Narrow failure signal categories for mypy
  ([#270](https://github.com/OpenAdaptAI/openadapt-flow/pull/270),
  [`35be7ff`](https://github.com/OpenAdaptAI/openadapt-flow/commit/35be7ffea34df750df598fe3caddaa08ed4c4ee6))

- Preserve full-frame identity OCR scope
  ([#267](https://github.com/OpenAdaptAI/openadapt-flow/pull/267),
  [`4090af4`](https://github.com/OpenAdaptAI/openadapt-flow/commit/4090af417d37e242876dc11b843acda20c428f9e))

- Preserve sealed v2 bundle digests ([#268](https://github.com/OpenAdaptAI/openadapt-flow/pull/268),
  [`9dcda2d`](https://github.com/OpenAdaptAI/openadapt-flow/commit/9dcda2d9407d1deba0099de2c93e55c39c37dd89))

- Preserve standard external encryption boundary
  ([`c25d45c`](https://github.com/OpenAdaptAI/openadapt-flow/commit/c25d45c60382234ddd9f4cb78c19994d05b7e2d4))

- Require review for ambiguous rich actions
  ([#268](https://github.com/OpenAdaptAI/openadapt-flow/pull/268),
  [`9dcda2d`](https://github.com/OpenAdaptAI/openadapt-flow/commit/9dcda2d9407d1deba0099de2c93e55c39c37dd89))

- Restore scheduled qualification gates
  ([#267](https://github.com/OpenAdaptAI/openadapt-flow/pull/267),
  [`4090af4`](https://github.com/OpenAdaptAI/openadapt-flow/commit/4090af417d37e242876dc11b843acda20c428f9e))

- Retain compiled demo bundle evidence
  ([`0779c45`](https://github.com/OpenAdaptAI/openadapt-flow/commit/0779c45b8ab55ccb26553e943563d2d4dddaeb88))

- **bench**: Register citrix_ica_hdx artifacts in inventory + ruff format
  ([`22fbb3a`](https://github.com/OpenAdaptAI/openadapt-flow/commit/22fbb3a0afe44abf0ac2c71244c1e2a2663992b6))

- **bundle**: Preserve sealed v2 frame-path compatibility
  ([#254](https://github.com/OpenAdaptAI/openadapt-flow/pull/254),
  [`f9091aa`](https://github.com/OpenAdaptAI/openadapt-flow/commit/f9091aab0f22b4a65401252b94d648a939da0575))

- **bundle**: Version sealed canonicalization rules
  ([#254](https://github.com/OpenAdaptAI/openadapt-flow/pull/254),
  [`f9091aa`](https://github.com/OpenAdaptAI/openadapt-flow/commit/f9091aab0f22b4a65401252b94d648a939da0575))

- **demo**: Bind claims to exact presentation sources
  ([`80c3d0c`](https://github.com/OpenAdaptAI/openadapt-flow/commit/80c3d0ca3930ec67e6239b0e28fedc2ad1f72a8d))

- **demo**: Keep validator independent of interop extra
  ([`f8d0fa5`](https://github.com/OpenAdaptAI/openadapt-flow/commit/f8d0fa5c7674ea9ceec1930871f2543a7bc0b857))

- **effects**: Harden verifier evidence boundaries
  ([#264](https://github.com/OpenAdaptAI/openadapt-flow/pull/264),
  [`a36660d`](https://github.com/OpenAdaptAI/openadapt-flow/commit/a36660d6ad7bce3229c0fc5f80121e285e8e67c2))

- **effects**: Ship only working verifier surfaces
  ([#264](https://github.com/OpenAdaptAI/openadapt-flow/pull/264),
  [`a36660d`](https://github.com/OpenAdaptAI/openadapt-flow/commit/a36660d6ad7bce3229c0fc5f80121e285e8e67c2))

- **playwright**: Bind frame-scoped identity and input
  ([#252](https://github.com/OpenAdaptAI/openadapt-flow/pull/252),
  [`4c4fea6`](https://github.com/OpenAdaptAI/openadapt-flow/commit/4c4fea69565d7662cedba49b6477763044179644))

- **playwright**: Follow hit-tested frame chain
  ([#249](https://github.com/OpenAdaptAI/openadapt-flow/pull/249),
  [`e4dd9ea`](https://github.com/OpenAdaptAI/openadapt-flow/commit/e4dd9ea96aadd34d4a05374f48564945289d7fe2))

- **playwright**: Reprove ancestor frame chain
  ([#249](https://github.com/OpenAdaptAI/openadapt-flow/pull/249),
  [`e4dd9ea`](https://github.com/OpenAdaptAI/openadapt-flow/commit/e4dd9ea96aadd34d4a05374f48564945289d7fe2))

- **recorder**: Never leak a nested control's typed value into field_label
  ([#262](https://github.com/OpenAdaptAI/openadapt-flow/pull/262),
  [`4f5120b`](https://github.com/OpenAdaptAI/openadapt-flow/commit/4f5120bbefce89ff1c8442446ff781ff3df7e327))

- **release**: Inventory public evidence videos
  ([`7cc518e`](https://github.com/OpenAdaptAI/openadapt-flow/commit/7cc518ee0b83dd571c0902423134a5525635e6b2))

- **release**: Require public artifacts tracked
  ([`1001d03`](https://github.com/OpenAdaptAI/openadapt-flow/commit/1001d03142189a3c09f4ba012297ebc68081558c))

- **runtime**: Bind overlay targets to event instances
  ([#251](https://github.com/OpenAdaptAI/openadapt-flow/pull/251),
  [`b8734c6`](https://github.com/OpenAdaptAI/openadapt-flow/commit/b8734c6da63ed0ec0300b44b980e4f47f9bff87e))

- **runtime**: Verify uncertain delivery without retry
  ([#250](https://github.com/OpenAdaptAI/openadapt-flow/pull/250),
  [`b1fbd3e`](https://github.com/OpenAdaptAI/openadapt-flow/commit/b1fbd3e1120ba559197ca22a05acb07e5282b755))

### Chores

- Refresh regulated deployment artifact inventory
  ([`ad9969f`](https://github.com/OpenAdaptAI/openadapt-flow/commit/ad9969fe25eab3df7d7e5679424ea3fa4cc7d9be))

- **demo**: Regenerate evidence on exact main
  ([`cab2fb8`](https://github.com/OpenAdaptAI/openadapt-flow/commit/cab2fb8eca4f488f5bb71810b012005ec0ea36ae))

- **release**: Refresh public artifact inventory
  ([#251](https://github.com/OpenAdaptAI/openadapt-flow/pull/251),
  [`b8734c6`](https://github.com/OpenAdaptAI/openadapt-flow/commit/b8734c6da63ed0ec0300b44b980e4f47f9bff87e))

- **release**: Refresh public artifact inventory hashes (ci.yml, deployment.example.yaml)
  ([#264](https://github.com/OpenAdaptAI/openadapt-flow/pull/264),
  [`a36660d`](https://github.com/OpenAdaptAI/openadapt-flow/commit/a36660d6ad7bce3229c0fc5f80121e285e8e67c2))

- **release**: Refresh verifier artifact inventory
  ([#264](https://github.com/OpenAdaptAI/openadapt-flow/pull/264),
  [`a36660d`](https://github.com/OpenAdaptAI/openadapt-flow/commit/a36660d6ad7bce3229c0fc5f80121e285e8e67c2))

### Code Style

- Format failure signal path ([#270](https://github.com/OpenAdaptAI/openadapt-flow/pull/270),
  [`35be7ff`](https://github.com/OpenAdaptAI/openadapt-flow/commit/35be7ffea34df750df598fe3caddaa08ed4c4ee6))

- Ruff-format transaction module (restores lint gate)
  ([#260](https://github.com/OpenAdaptAI/openadapt-flow/pull/260),
  [`75e2050`](https://github.com/OpenAdaptAI/openadapt-flow/commit/75e20505283fa92bdbd348efb148cefcd7f57b9e))

### Continuous Integration

- Refresh wheel artifact inventory ([#264](https://github.com/OpenAdaptAI/openadapt-flow/pull/264),
  [`a36660d`](https://github.com/OpenAdaptAI/openadapt-flow/commit/a36660d6ad7bce3229c0fc5f80121e285e8e67c2))

- Remove retired verifier stub path ([#264](https://github.com/OpenAdaptAI/openadapt-flow/pull/264),
  [`a36660d`](https://github.com/OpenAdaptAI/openadapt-flow/commit/a36660d6ad7bce3229c0fc5f80121e285e8e67c2))

- Validate exact overlay emitter head
  ([#251](https://github.com/OpenAdaptAI/openadapt-flow/pull/251),
  [`b8734c6`](https://github.com/OpenAdaptAI/openadapt-flow/commit/b8734c6da63ed0ec0300b44b980e4f47f9bff87e))

### Documentation

- Drop em dashes from the new parameter-identification section
  ([#262](https://github.com/OpenAdaptAI/openadapt-flow/pull/262),
  [`4f5120b`](https://github.com/OpenAdaptAI/openadapt-flow/commit/4f5120bbefce89ff1c8442446ff781ff3df7e327))

- Make public demo exporter invocation runnable
  ([`022aa30`](https://github.com/OpenAdaptAI/openadapt-flow/commit/022aa3074eee66351dcacb10651919bd4ada3120))

### Features

- Add atomic bundle sealing command
  ([`11e7410`](https://github.com/OpenAdaptAI/openadapt-flow/commit/11e7410d6eb1841c8a7d2b615a5ed7b57269ba0e))

- Emit privacy-safe failures from customer runners
  ([#270](https://github.com/OpenAdaptAI/openadapt-flow/pull/270),
  [`35be7ff`](https://github.com/OpenAdaptAI/openadapt-flow/commit/35be7ffea34df750df598fe3caddaa08ed4c4ee6))

- Emit private failure signals from BYOC
  ([#270](https://github.com/OpenAdaptAI/openadapt-flow/pull/270),
  [`35be7ff`](https://github.com/OpenAdaptAI/openadapt-flow/commit/35be7ffea34df750df598fe3caddaa08ed4c4ee6))

- Govern rich input actions across substrates
  ([#268](https://github.com/OpenAdaptAI/openadapt-flow/pull/268),
  [`9dcda2d`](https://github.com/OpenAdaptAI/openadapt-flow/commit/9dcda2d9407d1deba0099de2c93e55c39c37dd89))

- Govern right-click, drag, and shortcuts across substrates
  ([#268](https://github.com/OpenAdaptAI/openadapt-flow/pull/268),
  [`9dcda2d`](https://github.com/OpenAdaptAI/openadapt-flow/commit/9dcda2d9407d1deba0099de2c93e55c39c37dd89))

- Seal durable state for encrypted production runs
  ([`1cc42d6`](https://github.com/OpenAdaptAI/openadapt-flow/commit/1cc42d6ba2eba317a93b617a010c26e74cafdbfc))

- **cli**: Explicit surface selection for production profiles + surface-bound workflows (Section 5)
  ([#263](https://github.com/OpenAdaptAI/openadapt-flow/pull/263),
  [`f7eca97`](https://github.com/OpenAdaptAI/openadapt-flow/commit/f7eca972cd5bc04126ed344a34b1c2c149f8bc34))

- **compiler**: Field-label parameter inference with one-shot operator confirm
  ([#262](https://github.com/OpenAdaptAI/openadapt-flow/pull/262),
  [`4f5120b`](https://github.com/OpenAdaptAI/openadapt-flow/commit/4f5120bbefce89ff1c8442446ff781ff3df7e327))

- **demo**: Bind presentation clips to exact runtime frames
  ([`018516d`](https://github.com/OpenAdaptAI/openadapt-flow/commit/018516d6bd9f48c00134fbb57df6c0b9ed9ea8f6))

- **demo**: Publish audited exact-bound MockMed v3 evidence
  ([`810e5bb`](https://github.com/OpenAdaptAI/openadapt-flow/commit/810e5bb29322321f36008e2869d51a96c07d0363))

- **effects**: Verifier adapter platform (Section 4)
  ([#264](https://github.com/OpenAdaptAI/openadapt-flow/pull/264),
  [`a36660d`](https://github.com/OpenAdaptAI/openadapt-flow/commit/a36660d6ad7bce3229c0fc5f80121e285e8e67c2))

- **playwright**: Guard structural actions across frames
  ([#249](https://github.com/OpenAdaptAI/openadapt-flow/pull/249),
  [`e4dd9ea`](https://github.com/OpenAdaptAI/openadapt-flow/commit/e4dd9ea96aadd34d4a05374f48564945289d7fe2))

- **repair**: Governed promotion lifecycle with campaigns, canary, and rollback (Section 9)
  ([#265](https://github.com/OpenAdaptAI/openadapt-flow/pull/265),
  [`e6fbeb7`](https://github.com/OpenAdaptAI/openadapt-flow/commit/e6fbeb791d8c9aa0ee3520905fceab7d8221f028))

- **runtime**: Bind browser overlay targets to exact observations
  ([#251](https://github.com/OpenAdaptAI/openadapt-flow/pull/251),
  [`b8734c6`](https://github.com/OpenAdaptAI/openadapt-flow/commit/b8734c6da63ed0ec0300b44b980e4f47f9bff87e))

- **runtime**: Emit canonical control overlay events
  ([#251](https://github.com/OpenAdaptAI/openadapt-flow/pull/251),
  [`b8734c6`](https://github.com/OpenAdaptAI/openadapt-flow/commit/b8734c6da63ed0ec0300b44b980e4f47f9bff87e))

- **runtime**: Emit exact-bound browser overlay events
  ([#251](https://github.com/OpenAdaptAI/openadapt-flow/pull/251),
  [`b8734c6`](https://github.com/OpenAdaptAI/openadapt-flow/commit/b8734c6da63ed0ec0300b44b980e4f47f9bff87e))

- **runtime**: Explicit transaction outcome taxonomy + effect journal + idempotency (Section 3)
  ([#259](https://github.com/OpenAdaptAI/openadapt-flow/pull/259),
  [`1410cdd`](https://github.com/OpenAdaptAI/openadapt-flow/commit/1410cddd703fb0cfc8313f356a3b6124bd6b6fcb))

### Testing

- Align rich action safety expectations
  ([#268](https://github.com/OpenAdaptAI/openadapt-flow/pull/268),
  [`9dcda2d`](https://github.com/OpenAdaptAI/openadapt-flow/commit/9dcda2d9407d1deba0099de2c93e55c39c37dd89))

- Preserve unverified connector outcome semantics
  ([#270](https://github.com/OpenAdaptAI/openadapt-flow/pull/270),
  [`35be7ff`](https://github.com/OpenAdaptAI/openadapt-flow/commit/35be7ffea34df750df598fe3caddaa08ed4c4ee6))

## v1.23.0 (2026-07-25)


### Bug Fixes

- Arm browser target before identity read
  ([`a31d4c1`](https://github.com/OpenAdaptAI/openadapt-flow/commit/a31d4c1ae3fa353b141cfa07a8f1945dca8aaea3))

- Bind browser identity to actuation
  ([`a087dd4`](https://github.com/OpenAdaptAI/openadapt-flow/commit/a087dd46237720fcb376d11803850dfae336f78e))

- Bind browser keyboard input to verified focus
  ([`1397e05`](https://github.com/OpenAdaptAI/openadapt-flow/commit/1397e05f66670b96cb46dcc9b1538b8c741f3afd))

- Close qualified identity enforcement gaps
  ([`b509510`](https://github.com/OpenAdaptAI/openadapt-flow/commit/b50951093608564e9998cdb5599e6229f826b4c2))

- Enforce execution profile outcome contracts
  ([`fe7afe7`](https://github.com/OpenAdaptAI/openadapt-flow/commit/fe7afe7209f256f21f3d99fc1e451330616a6212))

- Enforce precise outcomes across consumers
  ([`b154814`](https://github.com/OpenAdaptAI/openadapt-flow/commit/b15481484fdee868f1bee93003045a2e29d79368))

- Expose identity extraction in qualification CLI
  ([`e63d81c`](https://github.com/OpenAdaptAI/openadapt-flow/commit/e63d81ce1a6ba438911bc1e7d016af93eca0ec5f))

- Inventory nested evidence manifests
  ([`6b64e28`](https://github.com/OpenAdaptAI/openadapt-flow/commit/6b64e2816d776673f6b7fd630a807fc9de6a3cae))

- Keep unverified outcomes off break rail
  ([`51cf035`](https://github.com/OpenAdaptAI/openadapt-flow/commit/51cf0353f76a261584689cc5ae92a25fc357a26e))

- Lease visual type focus before input
  ([`decc7b2`](https://github.com/OpenAdaptAI/openadapt-flow/commit/decc7b2ca1577dd480b20d4ea511c5243cd2e46f))

- Preserve legacy resume report compatibility
  ([`2418e30`](https://github.com/OpenAdaptAI/openadapt-flow/commit/2418e30e8909105238226fbb76980a703b29f867))

- Strip live authority from public demo evidence
  ([`c764a52`](https://github.com/OpenAdaptAI/openadapt-flow/commit/c764a52064062d6260e58ee80b933472ad558555))

- **ci**: Prime fresh AT-SPI qualification sessions
  ([`9b295d5`](https://github.com/OpenAdaptAI/openadapt-flow/commit/9b295d508d7276d5e877364e542828ab61ac548c))

- **identity**: Align API target paths exactly
  ([`99640d1`](https://github.com/OpenAdaptAI/openadapt-flow/commit/99640d1f7cf3a96e271206af413cf30125a91466))

- **identity**: Bind quorum evidence to exact targets
  ([`538ee28`](https://github.com/OpenAdaptAI/openadapt-flow/commit/538ee2888fecc24a851d53c5b0fd9b3fbc9fbe3e))

### Chores

- Add verified MockMed public demo evidence
  ([`eed6539`](https://github.com/OpenAdaptAI/openadapt-flow/commit/eed65397c3ee644364fb59aa0fcff60ba2fbefbc))

- Refresh public artifact inventory
  ([`0fde2a9`](https://github.com/OpenAdaptAI/openadapt-flow/commit/0fde2a9d08215df83634db9363c22630134a6c0c))

- Regenerate exhaustive public demo evidence
  ([`8c95a46`](https://github.com/OpenAdaptAI/openadapt-flow/commit/8c95a46aebe90e00080740c0a27b33c3164cb5e1))

### Features

- Add named execution profiles
  ([`1ac8c46`](https://github.com/OpenAdaptAI/openadapt-flow/commit/1ac8c46c0567649db6b2fe7c8c360b90ce8deef1))

- Enforce qualified identity signal quorums
  ([`4a39feb`](https://github.com/OpenAdaptAI/openadapt-flow/commit/4a39feb5cbc6aaefd351f7ba20e85059ad0388e4))

- Export real public demo evidence pack
  ([`c416b7d`](https://github.com/OpenAdaptAI/openadapt-flow/commit/c416b7d404ab6480351ec1d1809bcc26bdee1b4a))

- Transport precise execution outcomes
  ([`5d4ee71`](https://github.com/OpenAdaptAI/openadapt-flow/commit/5d4ee7181977f90d85639081e1badbd13ad8cd79))

### Testing

- Model guarded healing actuation
  ([`fec4c18`](https://github.com/OpenAdaptAI/openadapt-flow/commit/fec4c18592fd48c0752eb0342668e08fa9f2f196))

- **identity**: Model fresh loop actuation read
  ([`6ef3fdb`](https://github.com/OpenAdaptAI/openadapt-flow/commit/6ef3fdb0f567ff394df88a853b2182b9f4ee45f6))

## v1.22.0 (2026-07-25)


### Bug Fixes

- Preserve native Windows UIA boundaries
  ([`320c1d4`](https://github.com/OpenAdaptAI/openadapt-flow/commit/320c1d424e14b1994b681cb071797660dcfe2704))

- Visualize durable target evidence
  ([`e37019a`](https://github.com/OpenAdaptAI/openadapt-flow/commit/e37019a945e8c95fb419d51ee8009d656c26d83c))

### Chores

- Require capture 1.1 UIA contract
  ([`ac35f56`](https://github.com/OpenAdaptAI/openadapt-flow/commit/ac35f56565e754c5058e1f96c88d8bd249465193))

### Documentation

- Explain visual target evidence
  ([`4dd592f`](https://github.com/OpenAdaptAI/openadapt-flow/commit/4dd592f3a1ae1d32f3c81545b92927e3863542ee))

### Features

- Add minimum effect tier setter
  ([`d05771e`](https://github.com/OpenAdaptAI/openadapt-flow/commit/d05771e5b88a565dd09c82f69d11bd8610eac52a))

- Compile captured Windows UIA evidence
  ([`6aa8651`](https://github.com/OpenAdaptAI/openadapt-flow/commit/6aa865159156c97697a8a2c199795114f9c5900e))

## v1.21.0 (2026-07-25)


### Bug Fixes

- Bind qualification evidence to exact contract
  ([#236](https://github.com/OpenAdaptAI/openadapt-flow/pull/236),
  [`9f3ac04`](https://github.com/OpenAdaptAI/openadapt-flow/commit/9f3ac0495f649f416a5814b23a97a81e781c601e))

### Documentation

- Specify canonical remote frame continuity
  ([`915b103`](https://github.com/OpenAdaptAI/openadapt-flow/commit/915b103414d080269ad96381086739ea1eca42f4))

### Features

- Add two-phase remote actuation leases
  ([`3fc1d84`](https://github.com/OpenAdaptAI/openadapt-flow/commit/3fc1d84e6686ec96a4158d195f192535f8146a12))

- Add versioned qualification project contract
  ([#236](https://github.com/OpenAdaptAI/openadapt-flow/pull/236),
  [`9f3ac04`](https://github.com/OpenAdaptAI/openadapt-flow/commit/9f3ac0495f649f416a5814b23a97a81e781c601e))

## v1.20.2 (2026-07-25)


### Bug Fixes

- Contain BYOC report writes ([#235](https://github.com/OpenAdaptAI/openadapt-flow/pull/235),
  [`8e7796f`](https://github.com/OpenAdaptAI/openadapt-flow/commit/8e7796fcf0b9ac38f1beb2545a43462928803350))

- Fail closed on malformed connector reports
  ([#235](https://github.com/OpenAdaptAI/openadapt-flow/pull/235),
  [`8e7796f`](https://github.com/OpenAdaptAI/openadapt-flow/commit/8e7796fcf0b9ac38f1beb2545a43462928803350))

- Isolate capture adapter codec fixture
  ([#234](https://github.com/OpenAdaptAI/openadapt-flow/pull/234),
  [`3de5fc6`](https://github.com/OpenAdaptAI/openadapt-flow/commit/3de5fc67acf3024a621f812c5a6ed9be07fac335))

### Documentation

- Align connector enrollment with Cloud
  ([#235](https://github.com/OpenAdaptAI/openadapt-flow/pull/235),
  [`8e7796f`](https://github.com/OpenAdaptAI/openadapt-flow/commit/8e7796fcf0b9ac38f1beb2545a43462928803350))

- Reconcile desktop and remote substrate evidence
  ([#235](https://github.com/OpenAdaptAI/openadapt-flow/pull/235),
  [`8e7796f`](https://github.com/OpenAdaptAI/openadapt-flow/commit/8e7796fcf0b9ac38f1beb2545a43462928803350))

## v1.20.1 (2026-07-24)


### Bug Fixes

- Remove SciPy from core runtime ([#233](https://github.com/OpenAdaptAI/openadapt-flow/pull/233),
  [`09a9004`](https://github.com/OpenAdaptAI/openadapt-flow/commit/09a9004f75b111d49dd56d5cd44efd4c4504a50d))

### Continuous Integration

- Move full matrix to qualification lane
  ([#230](https://github.com/OpenAdaptAI/openadapt-flow/pull/230),
  [`5857498`](https://github.com/OpenAdaptAI/openadapt-flow/commit/5857498fa4714dfb09413eebb5ff518a8ff1ac34))

- Parallelize profiled identity regressions
  ([#231](https://github.com/OpenAdaptAI/openadapt-flow/pull/231),
  [`0b17323`](https://github.com/OpenAdaptAI/openadapt-flow/commit/0b173230d6ee2a1a8ec5c98f5bbfc2f7bc88e4eb))

- Refresh public artifact inventory ([#231](https://github.com/OpenAdaptAI/openadapt-flow/pull/231),
  [`0b17323`](https://github.com/OpenAdaptAI/openadapt-flow/commit/0b173230d6ee2a1a8ec5c98f5bbfc2f7bc88e4eb))

- Revert same-runner identity parallelization
  ([#232](https://github.com/OpenAdaptAI/openadapt-flow/pull/232),
  [`5c131aa`](https://github.com/OpenAdaptAI/openadapt-flow/commit/5c131aa1c79f4d810273711a3b987ab18afb1c3c))

### Testing

- Pin exact parallel CI exclusions ([#231](https://github.com/OpenAdaptAI/openadapt-flow/pull/231),
  [`0b17323`](https://github.com/OpenAdaptAI/openadapt-flow/commit/0b173230d6ee2a1a8ec5c98f5bbfc2f7bc88e4eb))

## v1.20.0 (2026-07-23)


### Bug Fixes

- Close Citrix target binding and readiness gaps
  ([#183](https://github.com/OpenAdaptAI/openadapt-flow/pull/183),
  [`f6faac5`](https://github.com/OpenAdaptAI/openadapt-flow/commit/f6faac5b900b78cbda5980de0e983a9f987285ac))

- Enforce Citrix readiness across run and resume
  ([#183](https://github.com/OpenAdaptAI/openadapt-flow/pull/183),
  [`f6faac5`](https://github.com/OpenAdaptAI/openadapt-flow/commit/f6faac5b900b78cbda5980de0e983a9f987285ac))

- Enforce public source artifact boundary
  ([#225](https://github.com/OpenAdaptAI/openadapt-flow/pull/225),
  [`7472d19`](https://github.com/OpenAdaptAI/openadapt-flow/commit/7472d19d3ddba76932b756cfdd0001874ccd4d46))

- Enforce the public source artifact boundary
  ([#225](https://github.com/OpenAdaptAI/openadapt-flow/pull/225),
  [`7472d19`](https://github.com/OpenAdaptAI/openadapt-flow/commit/7472d19d3ddba76932b756cfdd0001874ccd4d46))

- Honor landmark contradiction for LABELED anchors in template_global rung
  ([#166](https://github.com/OpenAdaptAI/openadapt-flow/pull/166),
  [`9c5f34e`](https://github.com/OpenAdaptAI/openadapt-flow/commit/9c5f34e4f1c3de1ab89829baca30a7051b904a55))

- Keep interop types green in developer mode
  ([#228](https://github.com/OpenAdaptAI/openadapt-flow/pull/228),
  [`95d66d9`](https://github.com/OpenAdaptAI/openadapt-flow/commit/95d66d9b25db83766866173a3b9ebf1addf43212))

- Locality+uniqueness gate for pixel template resolution (ambiguity -> halt)
  ([#165](https://github.com/OpenAdaptAI/openadapt-flow/pull/165),
  [`e72cc05`](https://github.com/OpenAdaptAI/openadapt-flow/commit/e72cc0524c8fad3f87e041e7a5d865d6dcb0be5a))

- Locality+uniqueness gate for pixel template resolution (ambiguity -> safe halt)
  ([#165](https://github.com/OpenAdaptAI/openadapt-flow/pull/165),
  [`e72cc05`](https://github.com/OpenAdaptAI/openadapt-flow/commit/e72cc0524c8fad3f87e041e7a5d865d6dcb0be5a))

- Redact bundle errors with safe recovery guidance
  ([#183](https://github.com/OpenAdaptAI/openadapt-flow/pull/183),
  [`f6faac5`](https://github.com/OpenAdaptAI/openadapt-flow/commit/f6faac5b900b78cbda5980de0e983a9f987285ac))

- **ci**: Derive protocol members via get_protocol_members, not __protocol_attrs__
  ([#204](https://github.com/OpenAdaptAI/openadapt-flow/pull/204),
  [`1c21866`](https://github.com/OpenAdaptAI/openadapt-flow/commit/1c2186631e2490fbdb1010fe7a3759f2eae03308))

- **ci**: Run EffectBench outside source checkout
  ([#221](https://github.com/OpenAdaptAI/openadapt-flow/pull/221),
  [`3b224b6`](https://github.com/OpenAdaptAI/openadapt-flow/commit/3b224b6b6067849a6dd2819210fc32230667f739))

- **ci**: Tomli fallback for Python 3.10 benchmark tests
  ([#220](https://github.com/OpenAdaptAI/openadapt-flow/pull/220),
  [`0212128`](https://github.com/OpenAdaptAI/openadapt-flow/commit/02121282ffe3f2c5ae0b33de6d0621c8a704e374))

- **ci**: Unblock main — de-symlink workshop bib (sdist) + deterministic pixel font test
  ([#189](https://github.com/OpenAdaptAI/openadapt-flow/pull/189),
  [`ce94d97`](https://github.com/OpenAdaptAI/openadapt-flow/commit/ce94d97c84b2542d5a4fb2b8db9a35070a76fbd7))

- **citrix**: Refuse unsupported default Linux client
  ([#183](https://github.com/OpenAdaptAI/openadapt-flow/pull/183),
  [`f6faac5`](https://github.com/OpenAdaptAI/openadapt-flow/commit/f6faac5b900b78cbda5980de0e983a9f987285ac))

- **citrix**: Wire CLI and refuse RDP transport mismatch
  ([#183](https://github.com/OpenAdaptAI/openadapt-flow/pull/183),
  [`f6faac5`](https://github.com/OpenAdaptAI/openadapt-flow/commit/f6faac5b900b78cbda5980de0e983a9f987285ac))

- **demo_media**: Stop burning the status badge into run_openemr footage
  ([#195](https://github.com/OpenAdaptAI/openadapt-flow/pull/195),
  [`7eeaf83`](https://github.com/OpenAdaptAI/openadapt-flow/commit/7eeaf83eb6295e0d14c1ac49c18e6f742756eac5))

- **effect-e2e**: Quote discovered SQLite tables
  ([#214](https://github.com/OpenAdaptAI/openadapt-flow/pull/214),
  [`1c088fd`](https://github.com/OpenAdaptAI/openadapt-flow/commit/1c088fdce3ab9e96d24617bff204fd2252f4d689))

- **effect_e2e**: Open-world ground truth + independent delta primitive + closed-world disclosure
  ([#214](https://github.com/OpenAdaptAI/openadapt-flow/pull/214),
  [`1c088fd`](https://github.com/OpenAdaptAI/openadapt-flow/commit/1c088fdce3ab9e96d24617bff204fd2252f4d689))

- **effect_e2e**: Open-world ground truth + independent delta primitive + closed-world disclosure
  (review #2 finding #3) ([#214](https://github.com/OpenAdaptAI/openadapt-flow/pull/214),
  [`1c088fd`](https://github.com/OpenAdaptAI/openadapt-flow/commit/1c088fdce3ab9e96d24617bff204fd2252f4d689))

- **effectbench**: Bind provider provenance
  ([#213](https://github.com/OpenAdaptAI/openadapt-flow/pull/213),
  [`6a51113`](https://github.com/OpenAdaptAI/openadapt-flow/commit/6a511136a68cc6e97d3d573b4975ac91015aa251))

- **effectbench**: Keep divergence schemas in parity
  ([#219](https://github.com/OpenAdaptAI/openadapt-flow/pull/219),
  [`bd2a27c`](https://github.com/OpenAdaptAI/openadapt-flow/commit/bd2a27caf300e58d2b19dcee3b1493c5ab6fa813))

- **eligibility**: Bind answers to verified subjects
  ([#147](https://github.com/OpenAdaptAI/openadapt-flow/pull/147),
  [`abe9f36`](https://github.com/OpenAdaptAI/openadapt-flow/commit/abe9f36b889d2939e389a07348bf4a68e5d54996))

- **eligibility**: Bind evidence to execution mode
  ([#147](https://github.com/OpenAdaptAI/openadapt-flow/pull/147),
  [`abe9f36`](https://github.com/OpenAdaptAI/openadapt-flow/commit/abe9f36b889d2939e389a07348bf4a68e5d54996))

- **eligibility**: Bind verified results to requests
  ([#147](https://github.com/OpenAdaptAI/openadapt-flow/pull/147),
  [`abe9f36`](https://github.com/OpenAdaptAI/openadapt-flow/commit/abe9f36b889d2939e389a07348bf4a68e5d54996))

- **eligibility**: Close activation safety contracts
  ([#147](https://github.com/OpenAdaptAI/openadapt-flow/pull/147),
  [`abe9f36`](https://github.com/OpenAdaptAI/openadapt-flow/commit/abe9f36b889d2939e389a07348bf4a68e5d54996))

- **eligibility**: Harden governed payer waterfall
  ([#147](https://github.com/OpenAdaptAI/openadapt-flow/pull/147),
  [`abe9f36`](https://github.com/OpenAdaptAI/openadapt-flow/commit/abe9f36b889d2939e389a07348bf4a68e5d54996))

- **eligibility**: Verify response provider
  ([#147](https://github.com/OpenAdaptAI/openadapt-flow/pull/147),
  [`abe9f36`](https://github.com/OpenAdaptAI/openadapt-flow/commit/abe9f36b889d2939e389a07348bf4a68e5d54996))

- **hardening**: Stabilize adversarial search seed
  ([#222](https://github.com/OpenAdaptAI/openadapt-flow/pull/222),
  [`69189dc`](https://github.com/OpenAdaptAI/openadapt-flow/commit/69189dc0203c245bf8d92cb25cfb891a1e2739a2))

- **lending**: Audit canonical table contents
  ([#219](https://github.com/OpenAdaptAI/openadapt-flow/pull/219),
  [`bd2a27c`](https://github.com/OpenAdaptAI/openadapt-flow/commit/bd2a27caf300e58d2b19dcee3b1493c5ab6fa813))

- **lending**: Make benchmark ground truth independent
  ([#219](https://github.com/OpenAdaptAI/openadapt-flow/pull/219),
  [`bd2a27c`](https://github.com/OpenAdaptAI/openadapt-flow/commit/bd2a27caf300e58d2b19dcee3b1493c5ab6fa813))

- **lending**: Publish bounded evidence aggregate
  ([#219](https://github.com/OpenAdaptAI/openadapt-flow/pull/219),
  [`bd2a27c`](https://github.com/OpenAdaptAI/openadapt-flow/commit/bd2a27caf300e58d2b19dcee3b1493c5ab6fa813))

- **mockmed**: Revert focus/caret CSS that broke deterministic e2e halts
  ([#210](https://github.com/OpenAdaptAI/openadapt-flow/pull/210),
  [`25e0f7c`](https://github.com/OpenAdaptAI/openadapt-flow/commit/25e0f7ca2123de79efc4ed1aba1fb2edf8683c41))

- **mockmed**: Stabilize textarea render metrics
  ([#227](https://github.com/OpenAdaptAI/openadapt-flow/pull/227),
  [`d756d8a`](https://github.com/OpenAdaptAI/openadapt-flow/commit/d756d8a5b1857f9e6658d56feb8d379a0d8969c9))

- **openimis**: Bind eligibility evidence fail closed
  ([#145](https://github.com/OpenAdaptAI/openadapt-flow/pull/145),
  [`d952c36`](https://github.com/OpenAdaptAI/openadapt-flow/commit/d952c363d1910f1699c1a4690002879b1990d743))

- **openimis**: Make eligibility evidence fail closed
  ([#145](https://github.com/OpenAdaptAI/openadapt-flow/pull/145),
  [`d952c36`](https://github.com/OpenAdaptAI/openadapt-flow/commit/d952c363d1910f1699c1a4690002879b1990d743))

- **openimis**: Refresh adapted compose provenance hash
  ([#145](https://github.com/OpenAdaptAI/openadapt-flow/pull/145),
  [`d952c36`](https://github.com/OpenAdaptAI/openadapt-flow/commit/d952c363d1910f1699c1a4690002879b1990d743))

- **openimis**: Revoke inherited table grants
  ([#145](https://github.com/OpenAdaptAI/openadapt-flow/pull/145),
  [`d952c36`](https://github.com/OpenAdaptAI/openadapt-flow/commit/d952c363d1910f1699c1a4690002879b1990d743))

- **openimis**: Verify exact read-only eligibility outcome
  ([#145](https://github.com/OpenAdaptAI/openadapt-flow/pull/145),
  [`d952c36`](https://github.com/OpenAdaptAI/openadapt-flow/commit/d952c363d1910f1699c1a4690002879b1990d743))

- **paper**: De-symlink workshop references.bib so the sdist packages
  ([#189](https://github.com/OpenAdaptAI/openadapt-flow/pull/189),
  [`ce94d97`](https://github.com/OpenAdaptAI/openadapt-flow/commit/ce94d97c84b2542d5a4fb2b8db9a35070a76fbd7))

- **paper**: Give gh release the repo context (GH_REPO) in the publish job
  ([#193](https://github.com/OpenAdaptAI/openadapt-flow/pull/193),
  [`5aeb126`](https://github.com/OpenAdaptAI/openadapt-flow/commit/5aeb126527df54c6d63aa12617cd6c2176ed6331))

- **paper**: Use lmodern+fontenc so microtype expansion builds on CI TeX
  ([#170](https://github.com/OpenAdaptAI/openadapt-flow/pull/170),
  [`3b99960`](https://github.com/OpenAdaptAI/openadapt-flow/commit/3b99960bc095c7cd8b7d252e931cff61f1ac250b))

- **rdp**: Anchor the note field distinctly
  ([`affedc5`](https://github.com/OpenAdaptAI/openadapt-flow/commit/affedc5f1f0de533a0744deaa8e30a203c91c6b3))

- **rdp**: Bind document idempotency to output path
  ([`affedc5`](https://github.com/OpenAdaptAI/openadapt-flow/commit/affedc5f1f0de533a0744deaa8e30a203c91c6b3))

- **rdp**: Encode punctuation as X11 keysyms
  ([`affedc5`](https://github.com/OpenAdaptAI/openadapt-flow/commit/affedc5f1f0de533a0744deaa8e30a203c91c6b3))

- **rdp**: Govern vision-ladder qualification evidence
  ([`affedc5`](https://github.com/OpenAdaptAI/openadapt-flow/commit/affedc5f1f0de533a0744deaa8e30a203c91c6b3))

- **rdp**: Make real-wire qualification deterministic
  ([`affedc5`](https://github.com/OpenAdaptAI/openadapt-flow/commit/affedc5f1f0de533a0744deaa8e30a203c91c6b3))

- **rdp**: Wait for selected record transition
  ([`affedc5`](https://github.com/OpenAdaptAI/openadapt-flow/commit/affedc5f1f0de533a0744deaa8e30a203c91c6b3))

- **release**: Carve corpus data from artifacts
  ([#225](https://github.com/OpenAdaptAI/openadapt-flow/pull/225),
  [`7472d19`](https://github.com/OpenAdaptAI/openadapt-flow/commit/7472d19d3ddba76932b756cfdd0001874ccd4d46))

- **release**: Remove private corpus data from public source
  ([#225](https://github.com/OpenAdaptAI/openadapt-flow/pull/225),
  [`7472d19`](https://github.com/OpenAdaptAI/openadapt-flow/commit/7472d19d3ddba76932b756cfdd0001874ccd4d46))

- **resolver**: Disambiguate OCR with retained evidence
  ([#226](https://github.com/OpenAdaptAI/openadapt-flow/pull/226),
  [`af143f2`](https://github.com/OpenAdaptAI/openadapt-flow/commit/af143f224e72efdd89e3e1068c0f0d278a3d785f))

- **resolver**: Preserve typed OCR refusals
  ([#226](https://github.com/OpenAdaptAI/openadapt-flow/pull/226),
  [`af143f2`](https://github.com/OpenAdaptAI/openadapt-flow/commit/af143f224e72efdd89e3e1068c0f0d278a3d785f))

- **resolver**: Refuse ambiguous OCR targets
  ([#226](https://github.com/OpenAdaptAI/openadapt-flow/pull/226),
  [`af143f2`](https://github.com/OpenAdaptAI/openadapt-flow/commit/af143f224e72efdd89e3e1068c0f0d278a3d785f))

- **runtime**: Bind interstitial actions safely
  ([#218](https://github.com/OpenAdaptAI/openadapt-flow/pull/218),
  [`d2f3347`](https://github.com/OpenAdaptAI/openadapt-flow/commit/d2f3347347201ffe235500677fe7d94904ec2fa8))

- **runtime**: Close interstitial admission gaps
  ([#218](https://github.com/OpenAdaptAI/openadapt-flow/pull/218),
  [`d2f3347`](https://github.com/OpenAdaptAI/openadapt-flow/commit/d2f3347347201ffe235500677fe7d94904ec2fa8))

- **runtime**: Close typed-input masked false success
  ([#223](https://github.com/OpenAdaptAI/openadapt-flow/pull/223),
  [`558c07d`](https://github.com/OpenAdaptAI/openadapt-flow/commit/558c07d14d2c4b182212eb0bdc078a3500d88bd9))

- **runtime**: Govern interstitial dismissals
  ([#218](https://github.com/OpenAdaptAI/openadapt-flow/pull/218),
  [`d2f3347`](https://github.com/OpenAdaptAI/openadapt-flow/commit/d2f3347347201ffe235500677fe7d94904ec2fa8))

- **runtime**: Keep readiness halts fail closed
  ([#218](https://github.com/OpenAdaptAI/openadapt-flow/pull/218),
  [`d2f3347`](https://github.com/OpenAdaptAI/openadapt-flow/commit/d2f3347347201ffe235500677fe7d94904ec2fa8))

- **runtime**: Make state readiness fail closed
  ([#218](https://github.com/OpenAdaptAI/openadapt-flow/pull/218),
  [`d2f3347`](https://github.com/OpenAdaptAI/openadapt-flow/commit/d2f3347347201ffe235500677fe7d94904ec2fa8))

- **runtime**: Require sealed dismissal anchors
  ([#218](https://github.com/OpenAdaptAI/openadapt-flow/pull/218),
  [`d2f3347`](https://github.com/OpenAdaptAI/openadapt-flow/commit/d2f3347347201ffe235500677fe7d94904ec2fa8))

- **runtime**: Sample postconditions at action boundary
  ([#218](https://github.com/OpenAdaptAI/openadapt-flow/pull/218),
  [`d2f3347`](https://github.com/OpenAdaptAI/openadapt-flow/commit/d2f3347347201ffe235500677fe7d94904ec2fa8))

- **runtime**: Verify typed values without masked false success
  ([#223](https://github.com/OpenAdaptAI/openadapt-flow/pull/223),
  [`558c07d`](https://github.com/OpenAdaptAI/openadapt-flow/commit/558c07d14d2c4b182212eb0bdc078a3500d88bd9))

- **test**: Make pixel scale-invariance test deterministic across fonts
  ([#189](https://github.com/OpenAdaptAI/openadapt-flow/pull/189),
  [`ce94d97`](https://github.com/OpenAdaptAI/openadapt-flow/commit/ce94d97c84b2542d5a4fb2b8db9a35070a76fbd7))

- **vision**: Compare peak center to expected center in locality gate
  ([#165](https://github.com/OpenAdaptAI/openadapt-flow/pull/165),
  [`e72cc05`](https://github.com/OpenAdaptAI/openadapt-flow/commit/e72cc0524c8fad3f87e041e7a5d865d6dcb0be5a))

### Build System

- Bump ruff from 0.15.21 to 0.15.22 in the python-minor group
  ([#175](https://github.com/OpenAdaptAI/openadapt-flow/pull/175),
  [`8d383a0`](https://github.com/OpenAdaptAI/openadapt-flow/commit/8d383a04d821b02ccf3d5c7efcf1187facc7596d))

- Update transformers requirement from <5.13,>=5.5 to >=5.5,<5.15
  ([#176](https://github.com/OpenAdaptAI/openadapt-flow/pull/176),
  [`743681b`](https://github.com/OpenAdaptAI/openadapt-flow/commit/743681be66153436d86ee26c4b38ff37bab2d90a))

- **openimis**: Exclude eligibility demo/test + showcase-openimis from sdist
  ([#145](https://github.com/OpenAdaptAI/openadapt-flow/pull/145),
  [`d952c36`](https://github.com/OpenAdaptAI/openadapt-flow/commit/d952c363d1910f1699c1a4690002879b1990d743))

- **openimis**: Preserve release policy union
  ([#145](https://github.com/OpenAdaptAI/openadapt-flow/pull/145),
  [`d952c36`](https://github.com/OpenAdaptAI/openadapt-flow/commit/d952c363d1910f1699c1a4690002879b1990d743))

### Chores

- **ci**: Validate Citrix branch against main
  ([#183](https://github.com/OpenAdaptAI/openadapt-flow/pull/183),
  [`f6faac5`](https://github.com/OpenAdaptAI/openadapt-flow/commit/f6faac5b900b78cbda5980de0e983a9f987285ac))

- **showcase**: Reseal loop showcase bundle for additive interstitials field
  ([#218](https://github.com/OpenAdaptAI/openadapt-flow/pull/218),
  [`d2f3347`](https://github.com/OpenAdaptAI/openadapt-flow/commit/d2f3347347201ffe235500677fe7d94904ec2fa8))

### Code Style

- Ruff format the canvas-ladder e2e wrapper
  ([`194b65a`](https://github.com/OpenAdaptAI/openadapt-flow/commit/194b65a19ea9e2a991337baa720cc38d8da16655))

- Ruff format the Citrix backend + tests
  ([#183](https://github.com/OpenAdaptAI/openadapt-flow/pull/183),
  [`f6faac5`](https://github.com/OpenAdaptAI/openadapt-flow/commit/f6faac5b900b78cbda5980de0e983a9f987285ac))

- Ruff-format compliance and hyphens in new runtime strings
  ([#218](https://github.com/OpenAdaptAI/openadapt-flow/pull/218),
  [`d2f3347`](https://github.com/OpenAdaptAI/openadapt-flow/commit/d2f3347347201ffe235500677fe7d94904ec2fa8))

- **effect-e2e**: Format identifier coverage test
  ([#214](https://github.com/OpenAdaptAI/openadapt-flow/pull/214),
  [`1c088fd`](https://github.com/OpenAdaptAI/openadapt-flow/commit/1c088fdce3ab9e96d24617bff204fd2252f4d689))

- **lending**: Apply current Ruff formatting
  ([#219](https://github.com/OpenAdaptAI/openadapt-flow/pull/219),
  [`bd2a27c`](https://github.com/OpenAdaptAI/openadapt-flow/commit/bd2a27caf300e58d2b19dcee3b1493c5ab6fa813))

### Continuous Integration

- Bump the actions group across 1 directory with 4 updates
  ([#174](https://github.com/OpenAdaptAI/openadapt-flow/pull/174),
  [`1d199ec`](https://github.com/OpenAdaptAI/openadapt-flow/commit/1d199ecf4213ba47870599096cbc9c683acb1ee0))

- Make Flow releases explicitly dispatched
  ([#224](https://github.com/OpenAdaptAI/openadapt-flow/pull/224),
  [`a1a924e`](https://github.com/OpenAdaptAI/openadapt-flow/commit/a1a924ef3184a1f4f48320a2e207769b9def0baf))

- **paper**: Publish built PDF to stable paper-latest release + notify web
  ([#192](https://github.com/OpenAdaptAI/openadapt-flow/pull/192),
  [`5f6bca0`](https://github.com/OpenAdaptAI/openadapt-flow/commit/5f6bca034cf2a3b030737c4066b4bf8ba922a1cc))

- **paper**: Reword comment so docs-consistency gate passes
  ([#192](https://github.com/OpenAdaptAI/openadapt-flow/pull/192),
  [`5f6bca0`](https://github.com/OpenAdaptAI/openadapt-flow/commit/5f6bca034cf2a3b030737c4066b4bf8ba922a1cc))

- **rdp**: Pin qualification dependencies
  ([`affedc5`](https://github.com/OpenAdaptAI/openadapt-flow/commit/affedc5f1f0de533a0744deaa8e30a203c91c6b3))

- **rdp**: Qualify pull request changes
  ([`affedc5`](https://github.com/OpenAdaptAI/openadapt-flow/commit/affedc5f1f0de533a0744deaa8e30a203c91c6b3))

### Documentation

- Rewrite workflow-program IR as an implemented spec + add publication options
  ([#203](https://github.com/OpenAdaptAI/openadapt-flow/pull/203),
  [`152e2de`](https://github.com/OpenAdaptAI/openadapt-flow/commit/152e2de9e5b712d51693ec9e44a64791d9f007fb))

- **effectbench**: Normalize em dashes to hyphens in new provider prose
  ([#213](https://github.com/OpenAdaptAI/openadapt-flow/pull/213),
  [`6a51113`](https://github.com/OpenAdaptAI/openadapt-flow/commit/6a511136a68cc6e97d3d573b4975ac91015aa251))

- **lending**: Clean nested-bold in SWER.md ladder bullets
  ([#219](https://github.com/OpenAdaptAI/openadapt-flow/pull/219),
  [`bd2a27c`](https://github.com/OpenAdaptAI/openadapt-flow/commit/bd2a27caf300e58d2b19dcee3b1493c5ab6fa813))

- **paper**: Add ~8-page workshop condensation, gate-checked and built with the report
  ([#170](https://github.com/OpenAdaptAI/openadapt-flow/pull/170),
  [`3b99960`](https://github.com/OpenAdaptAI/openadapt-flow/commit/3b99960bc095c7cd8b7d252e931cff61f1ac250b))

- **paper**: Adversarial peer review + clearly-correct honesty fixes
  ([#200](https://github.com/OpenAdaptAI/openadapt-flow/pull/200),
  [`76c8a86`](https://github.com/OpenAdaptAI/openadapt-flow/commit/76c8a869a8be55be626de07dae364140c22a2eb2))

- **paper**: Align evidence release boundary
  ([#217](https://github.com/OpenAdaptAI/openadapt-flow/pull/217),
  [`2e94d71`](https://github.com/OpenAdaptAI/openadapt-flow/commit/2e94d7125bf2db5bfa9d10cd8d421d7f3ec77ff0))

- **paper**: Bound evidence and oracle independence
  ([#217](https://github.com/OpenAdaptAI/openadapt-flow/pull/217),
  [`2e94d71`](https://github.com/OpenAdaptAI/openadapt-flow/commit/2e94d7125bf2db5bfa9d10cd8d421d7f3ec77ff0))

- **paper**: Cite the real end-to-end silent-wrong-effect numbers as the headline
  ([#211](https://github.com/OpenAdaptAI/openadapt-flow/pull/211),
  [`47ff049`](https://github.com/OpenAdaptAI/openadapt-flow/commit/47ff049ba6f3d400afed2e563788e1f31b0ce9df))

- **paper**: Correct stale README (byline set; workshop bib is a copy not a symlink)
  ([#199](https://github.com/OpenAdaptAI/openadapt-flow/pull/199),
  [`d634825`](https://github.com/OpenAdaptAI/openadapt-flow/commit/d6348250d2fb84f7f2500329be705e55ba7df8a6))

- **paper**: Honest disclosures for adversarial review #2 (closed-world, statistics, positioning,
  ethics) ([#217](https://github.com/OpenAdaptAI/openadapt-flow/pull/217),
  [`2e94d71`](https://github.com/OpenAdaptAI/openadapt-flow/commit/2e94d7125bf2db5bfa9d10cd8d421d7f3ec77ff0))

- **paper**: Present the lending three-arm ladder + rebind check_artifacts
  ([#219](https://github.com/OpenAdaptAI/openadapt-flow/pull/219),
  [`bd2a27c`](https://github.com/OpenAdaptAI/openadapt-flow/commit/bd2a27caf300e58d2b19dcee3b1493c5ab6fa813))

- **paper**: Second adversarial review — independent-harness, stats, second-domain, benchmark lens
  ([#209](https://github.com/OpenAdaptAI/openadapt-flow/pull/209),
  [`9a14fa2`](https://github.com/OpenAdaptAI/openadapt-flow/commit/9a14fa2658380a37e1e5560039c56f07e7f0c61a))

- **paper**: Set author line (Richard Abrich, OpenAdapt / MLDSAI Inc.)
  ([#180](https://github.com/OpenAdaptAI/openadapt-flow/pull/180),
  [`a14960d`](https://github.com/OpenAdaptAI/openadapt-flow/commit/a14960d532e95111f6c075df71127e8bfb0fa5ea))

- **paper**: Set author to Richard Abrich, OpenAdapt (MLDSAI Inc.)
  ([#180](https://github.com/OpenAdaptAI/openadapt-flow/pull/180),
  [`a14960d`](https://github.com/OpenAdaptAI/openadapt-flow/commit/a14960d532e95111f6c075df71127e8bfb0fa5ea))

- **paper**: Sharpen thesis, add figures, foreground the safety instrument
  ([#170](https://github.com/OpenAdaptAI/openadapt-flow/pull/170),
  [`3b99960`](https://github.com/OpenAdaptAI/openadapt-flow/commit/3b99960bc095c7cd8b7d252e931cff61f1ac250b))

- **paper**: State released data boundary precisely
  ([#217](https://github.com/OpenAdaptAI/openadapt-flow/pull/217),
  [`2e94d71`](https://github.com/OpenAdaptAI/openadapt-flow/commit/2e94d7125bf2db5bfa9d10cd8d421d7f3ec77ff0))

- **paper**: Temper EffectBench scope claim (review #2 #3.6/#3.8)
  ([#217](https://github.com/OpenAdaptAI/openadapt-flow/pull/217),
  [`2e94d71`](https://github.com/OpenAdaptAI/openadapt-flow/commit/2e94d7125bf2db5bfa9d10cd8d421d7f3ec77ff0))

- **paper**: Use richard@openadapt.ai as contact email
  ([#180](https://github.com/OpenAdaptAI/openadapt-flow/pull/180),
  [`a14960d`](https://github.com/OpenAdaptAI/openadapt-flow/commit/a14960d532e95111f6c075df71127e8bfb0fa5ea))

- **readme**: Document the for-each data-driven loop and visualize commands
  ([#190](https://github.com/OpenAdaptAI/openadapt-flow/pull/190),
  [`f059f6c`](https://github.com/OpenAdaptAI/openadapt-flow/commit/f059f6c930ce62f0bd99b3a2e4024e8c3065db96))

- **readme**: Embed real visualize Mermaid + record->compile->replay loop
  ([#201](https://github.com/OpenAdaptAI/openadapt-flow/pull/201),
  [`3f7424f`](https://github.com/OpenAdaptAI/openadapt-flow/commit/3f7424f465406c4b75b33348fe60dcb5794eb114))

- **readme**: Substrate-aware refresh of the engine README
  ([#201](https://github.com/OpenAdaptAI/openadapt-flow/pull/201),
  [`3f7424f`](https://github.com/OpenAdaptAI/openadapt-flow/commit/3f7424f465406c4b75b33348fe60dcb5794eb114))

### Features

- Compiled-program visualizer (shared graph spec + CLI render)
  ([#184](https://github.com/OpenAdaptAI/openadapt-flow/pull/184),
  [`144dd82`](https://github.com/OpenAdaptAI/openadapt-flow/commit/144dd82e0a70ea7590e83683516ade8837390748))

- MacOS AX IdentityBackend + StructuralActionBackend (identity parity)
  ([#171](https://github.com/OpenAdaptAI/openadapt-flow/pull/171),
  [`7db7d14`](https://github.com/OpenAdaptAI/openadapt-flow/commit/7db7d14bd7fc7633f31b9f99f3e11c318b295251))

- Resolve pixel-verify identity gate from deployment runtime config
  ([#179](https://github.com/OpenAdaptAI/openadapt-flow/pull/179),
  [`f8bcf48`](https://github.com/OpenAdaptAI/openadapt-flow/commit/f8bcf48fb2d500025521ddd7434522e7fd241a3a))

- **backends**: Citrix Workspace pixel backend and no-DOM stand-in proof
  ([#183](https://github.com/OpenAdaptAI/openadapt-flow/pull/183),
  [`f6faac5`](https://github.com/OpenAdaptAI/openadapt-flow/commit/f6faac5b900b78cbda5980de0e983a9f987285ac))

- **backends**: Citrix-Workspace-window pixel backend + no-DOM stand-in proof
  ([#183](https://github.com/OpenAdaptAI/openadapt-flow/pull/183),
  [`f6faac5`](https://github.com/OpenAdaptAI/openadapt-flow/commit/f6faac5b900b78cbda5980de0e983a9f987285ac))

- **benchmark**: Add second (non-healthcare) domain: MockLoan lending effect-verification study
  ([#208](https://github.com/OpenAdaptAI/openadapt-flow/pull/208),
  [`78b27aa`](https://github.com/OpenAdaptAI/openadapt-flow/commit/78b27aad3ba430a7b9558a846e416219203a7e70))

- **benchmark**: EffectBench foundation — episode schema + substrate-agnostic effect-oracle harness
  ([#178](https://github.com/OpenAdaptAI/openadapt-flow/pull/178),
  [`3e44571`](https://github.com/OpenAdaptAI/openadapt-flow/commit/3e445717c3b9aab85dcb4f11dcbf30e8e29e0e31))

- **benchmark**: EffectBench multi-baseline runner adapter — one arm interface, identical
  task+oracle ([#186](https://github.com/OpenAdaptAI/openadapt-flow/pull/186),
  [`7688922`](https://github.com/OpenAdaptAI/openadapt-flow/commit/7688922c29b74616587858a836a055ea6acceb1f))

- **benchmark**: Independent end-to-end silent-wrong-effect harness (real SWER)
  ([#206](https://github.com/OpenAdaptAI/openadapt-flow/pull/206),
  [`e9a6583`](https://github.com/OpenAdaptAI/openadapt-flow/commit/e9a65833a9c91c536bfad2fa1f9fa201e104e324))

- **benchmark**: Index pinned system-of-record environments for the effect benchmark
  ([#173](https://github.com/OpenAdaptAI/openadapt-flow/pull/173),
  [`972ed38`](https://github.com/OpenAdaptAI/openadapt-flow/commit/972ed38f9213aee89ad0ad5244a5de5a35847ef1))

- **benchmark**: Package EffectBench/SWER as a standalone, versioned benchmark
  ([#205](https://github.com/OpenAdaptAI/openadapt-flow/pull/205),
  [`282ddb6`](https://github.com/OpenAdaptAI/openadapt-flow/commit/282ddb65ac6880c9ddd61ae1fc6698960056e7e8))

- **benchmark**: Publish bounded paid-agent aggregates
  ([#216](https://github.com/OpenAdaptAI/openadapt-flow/pull/216),
  [`e740a7e`](https://github.com/OpenAdaptAI/openadapt-flow/commit/e740a7e4b68ed5c6461bdcd6fbd086e8ac75c8be))

- **citrix**: Add CVAD 30-day trial-mode Azure lab provisioning helpers
  ([#182](https://github.com/OpenAdaptAI/openadapt-flow/pull/182),
  [`60a48cc`](https://github.com/OpenAdaptAI/openadapt-flow/commit/60a48cc66b54bc5c24e952a82e9a679a516a2121))

- **citrix_daas**: Staged, guarded PREP kit for the DaaS-Standard-for-Azure 7-day trial (clock NOT
  started) ([#187](https://github.com/OpenAdaptAI/openadapt-flow/pull/187),
  [`a3737a5`](https://github.com/OpenAdaptAI/openadapt-flow/commit/a3737a56d35ba97a5e4d96439b2cf12d4b825af4))

- **compiler**: Author data-driven LOOP from a single demonstration
  ([#188](https://github.com/OpenAdaptAI/openadapt-flow/pull/188),
  [`f9f8bac`](https://github.com/OpenAdaptAI/openadapt-flow/commit/f9f8bac278276e4fa7adaaf83335a2b9aef8df82))

- **connector**: Engine-side BYOC outbound-pull daemon (openadapt-flow connector)
  ([#212](https://github.com/OpenAdaptAI/openadapt-flow/pull/212),
  [`3d9635b`](https://github.com/OpenAdaptAI/openadapt-flow/commit/3d9635b787bdcb69846c28535d8f2a91677fcc40))

- **effectbench**: Author the first task pack (~40 tasks, all 7 divergence categories)
  ([#185](https://github.com/OpenAdaptAI/openadapt-flow/pull/185),
  [`6f15983`](https://github.com/OpenAdaptAI/openadapt-flow/commit/6f15983464c501de2233dafd7c99f3a5aedeefb5))

- **effectbench**: Pluggable external system-of-record + oracle interface (reference oracle marked
  reference-only) ([#213](https://github.com/OpenAdaptAI/openadapt-flow/pull/213),
  [`6a51113`](https://github.com/OpenAdaptAI/openadapt-flow/commit/6a511136a68cc6e97d3d573b4975ac91015aa251))

- **effects**: Auto-derived, different-path on-screen read-back oracle (no-connector default)
  ([#191](https://github.com/OpenAdaptAI/openadapt-flow/pull/191),
  [`d20ead8`](https://github.com/OpenAdaptAI/openadapt-flow/commit/d20ead89448556df27250fdd1bbcb271aefc3fc0))

- **eligibility**: API-first 270/271 eligibility waterfall (Stedi client, payer route map,
  document-hash verified artifacts) ([#147](https://github.com/OpenAdaptAI/openadapt-flow/pull/147),
  [`abe9f36`](https://github.com/OpenAdaptAI/openadapt-flow/commit/abe9f36b889d2939e389a07348bf4a68e5d54996))

- **hardening**: Harder cases (latency/reflow/dense) + close 36 new silent-wrongs
  ([#215](https://github.com/OpenAdaptAI/openadapt-flow/pull/215),
  [`71e3532`](https://github.com/OpenAdaptAI/openadapt-flow/commit/71e35324c1a16ff62b3fef18dabb70880ed7cec6))

- **identity**: Jitter-robust pixel identity VERIFY (config-gated, default off)
  ([#172](https://github.com/OpenAdaptAI/openadapt-flow/pull/172),
  [`625a8be`](https://github.com/OpenAdaptAI/openadapt-flow/commit/625a8be564d9d2d4f714903e7014d5839eb3fc95))

- **lending**: Add collateral-write fault class + single-surface arm for honest cross-domain
  comparability ([#219](https://github.com/OpenAdaptAI/openadapt-flow/pull/219),
  [`bd2a27c`](https://github.com/OpenAdaptAI/openadapt-flow/commit/bd2a27caf300e58d2b19dcee3b1493c5ab6fa813))

- **openimis**: Effect-verified eligibility-check reference workflow + showcase
  ([#145](https://github.com/OpenAdaptAI/openadapt-flow/pull/145),
  [`d952c36`](https://github.com/OpenAdaptAI/openadapt-flow/commit/d952c363d1910f1699c1a4690002879b1990d743))

- **openimis**: Effect-verified insurance eligibility-check reference workflow + showcase
  ([#145](https://github.com/OpenAdaptAI/openadapt-flow/pull/145),
  [`d952c36`](https://github.com/OpenAdaptAI/openadapt-flow/commit/d952c363d1910f1699c1a4690002879b1990d743))

- **report**: Add per-step before/after evidence
  ([#202](https://github.com/OpenAdaptAI/openadapt-flow/pull/202),
  [`32b9624`](https://github.com/OpenAdaptAI/openadapt-flow/commit/32b9624ee0b36d6014b1df5b47c5fc632ca2c677))

- **runtime**: State-dependency robustness (settle readiness + interstitials)
  ([#218](https://github.com/OpenAdaptAI/openadapt-flow/pull/218),
  [`d2f3347`](https://github.com/OpenAdaptAI/openadapt-flow/commit/d2f3347347201ffe235500677fe7d94904ec2fa8))

- **runtime**: User-configurable ("bring your own") grounding model with fail-closed PHI allowlist
  ([#196](https://github.com/OpenAdaptAI/openadapt-flow/pull/196),
  [`ec5a52a`](https://github.com/OpenAdaptAI/openadapt-flow/commit/ec5a52abc9b8281571a866dd4dc429f6545d0d62))

- **validation**: Configurable hardening corpus + private-corpus release-boundary guard
  ([#197](https://github.com/OpenAdaptAI/openadapt-flow/pull/197),
  [`848f8d6`](https://github.com/OpenAdaptAI/openadapt-flow/commit/848f8d6d110f681f194664dcfc13de42f14e55be))

- **validation**: Vision hardening flywheel — adversarial perturbation sweep + silent-wrong ratchet
  ([#194](https://github.com/OpenAdaptAI/openadapt-flow/pull/194),
  [`3074699`](https://github.com/OpenAdaptAI/openadapt-flow/commit/30746999c92e1f440096f29a2d0b73ae446b550c))

- **visualize**: Expand loop bodies in the program graph + compact loop showcase
  ([#207](https://github.com/OpenAdaptAI/openadapt-flow/pull/207),
  [`861477d`](https://github.com/OpenAdaptAI/openadapt-flow/commit/861477d547e033899baea6810279a61e38c4a019))

### Testing

- Assert zero-model-call reference bar on Windows/RDP replays
  ([#168](https://github.com/OpenAdaptAI/openadapt-flow/pull/168),
  [`98c9a0c`](https://github.com/OpenAdaptAI/openadapt-flow/commit/98c9a0c84a8f039b954ee77d55b21204a3811894))

- Keep missing Citrix targets out of reports
  ([#183](https://github.com/OpenAdaptAI/openadapt-flow/pull/183),
  [`f6faac5`](https://github.com/OpenAdaptAI/openadapt-flow/commit/f6faac5b900b78cbda5980de0e983a9f987285ac))

- Pin backend optional-capability matrix to the maturity map
  ([#169](https://github.com/OpenAdaptAI/openadapt-flow/pull/169),
  [`ce9ddbb`](https://github.com/OpenAdaptAI/openadapt-flow/commit/ce9ddbbc704e2bcdd1b2f25b286971bce51578bf))

- **citrix**: Bind evidence to clean reviewed source
  ([#183](https://github.com/OpenAdaptAI/openadapt-flow/pull/183),
  [`f6faac5`](https://github.com/OpenAdaptAI/openadapt-flow/commit/f6faac5b900b78cbda5980de0e983a9f987285ac))

- **citrix**: Inspect protocol surface without actuation
  ([#229](https://github.com/OpenAdaptAI/openadapt-flow/pull/229),
  [`b7c863b`](https://github.com/OpenAdaptAI/openadapt-flow/commit/b7c863be37d99693184f5855800d079a4adea115))

- **citrix**: Record three-by-three code-readiness evidence
  ([#183](https://github.com/OpenAdaptAI/openadapt-flow/pull/183),
  [`f6faac5`](https://github.com/OpenAdaptAI/openadapt-flow/commit/f6faac5b900b78cbda5980de0e983a9f987285ac))

- **citrix**: Refresh final provenance evidence
  ([#183](https://github.com/OpenAdaptAI/openadapt-flow/pull/183),
  [`f6faac5`](https://github.com/OpenAdaptAI/openadapt-flow/commit/f6faac5b900b78cbda5980de0e983a9f987285ac))

- **citrix**: Refresh provenance-bound evidence
  ([#183](https://github.com/OpenAdaptAI/openadapt-flow/pull/183),
  [`f6faac5`](https://github.com/OpenAdaptAI/openadapt-flow/commit/f6faac5b900b78cbda5980de0e983a9f987285ac))

- **citrix**: Refresh synthetic qualification evidence
  ([#183](https://github.com/OpenAdaptAI/openadapt-flow/pull/183),
  [`f6faac5`](https://github.com/OpenAdaptAI/openadapt-flow/commit/f6faac5b900b78cbda5980de0e983a9f987285ac))

- **citrix**: Require reproducible 3x3 evidence
  ([#183](https://github.com/OpenAdaptAI/openadapt-flow/pull/183),
  [`f6faac5`](https://github.com/OpenAdaptAI/openadapt-flow/commit/f6faac5b900b78cbda5980de0e983a9f987285ac))

- **hardening**: Ratchet corpus down after locality+uniqueness gate
  ([#165](https://github.com/OpenAdaptAI/openadapt-flow/pull/165),
  [`e72cc05`](https://github.com/OpenAdaptAI/openadapt-flow/commit/e72cc0524c8fad3f87e041e7a5d865d6dcb0be5a))

- **openemr**: Expose exact fixture text readback
  ([#223](https://github.com/OpenAdaptAI/openadapt-flow/pull/223),
  [`558c07d`](https://github.com/OpenAdaptAI/openadapt-flow/commit/558c07d14d2c4b182212eb0bdc078a3500d88bd9))

- **openimis**: Stub the optional psycopg driver in all verifier-construction tests
  ([#145](https://github.com/OpenAdaptAI/openadapt-flow/pull/145),
  [`d952c36`](https://github.com/OpenAdaptAI/openadapt-flow/commit/d952c363d1910f1699c1a4690002879b1990d743))

- **rdp**: Prove compiled crop verifies healthy identity
  ([`affedc5`](https://github.com/OpenAdaptAI/openadapt-flow/commit/affedc5f1f0de533a0744deaa8e30a203c91c6b3))

- **rdp**: Retain per-step qualification diagnostics
  ([`affedc5`](https://github.com/OpenAdaptAI/openadapt-flow/commit/affedc5f1f0de533a0744deaa8e30a203c91c6b3))

## v1.19.0 (2026-07-19)


### Code Style

- Ruff format record-window files ([#164](https://github.com/OpenAdaptAI/openadapt-flow/pull/164),
  [`d85b371`](https://github.com/OpenAdaptAI/openadapt-flow/commit/d85b371308e03ada05ef06942047287476ebb36e))

### Features

- **record**: Expose window-scoped capture on `record --window`
  ([#164](https://github.com/OpenAdaptAI/openadapt-flow/pull/164),
  [`d85b371`](https://github.com/OpenAdaptAI/openadapt-flow/commit/d85b371308e03ada05ef06942047287476ebb36e))

### Testing

- Force supported platform in window-forwarding test
  ([#164](https://github.com/OpenAdaptAI/openadapt-flow/pull/164),
  [`d85b371`](https://github.com/OpenAdaptAI/openadapt-flow/commit/d85b371308e03ada05ef06942047287476ebb36e))

## v1.18.1 (2026-07-19)


### Bug Fixes

- **hosted**: Close run id path races
  ([#163](https://github.com/OpenAdaptAI/openadapt-flow/pull/163),
  [`40c4326`](https://github.com/OpenAdaptAI/openadapt-flow/commit/40c4326d8768d820287eb96c3bba3ede001294d4))

- **hosted**: Harden versioned local run reporting
  ([#160](https://github.com/OpenAdaptAI/openadapt-flow/pull/160),
  [`9ccfd91`](https://github.com/OpenAdaptAI/openadapt-flow/commit/9ccfd91cd0050b632f3e8a13e0916c626215b476))

- **hosted**: Preserve binary run id bytes on Windows
  ([#163](https://github.com/OpenAdaptAI/openadapt-flow/pull/163),
  [`40c4326`](https://github.com/OpenAdaptAI/openadapt-flow/commit/40c4326d8768d820287eb96c3bba3ede001294d4))

- **hosted**: Wait for exclusive run id writer
  ([#163](https://github.com/OpenAdaptAI/openadapt-flow/pull/163),
  [`40c4326`](https://github.com/OpenAdaptAI/openadapt-flow/commit/40c4326d8768d820287eb96c3bba3ede001294d4))

### Documentation

- Add explicit Beta lifecycle label to README
  ([#161](https://github.com/OpenAdaptAI/openadapt-flow/pull/161),
  [`6e2c8b3`](https://github.com/OpenAdaptAI/openadapt-flow/commit/6e2c8b3de7e80ecb31bcef3cf7004a35abaed6b3))

- Point lifecycle copy to qualification matrix
  ([#162](https://github.com/OpenAdaptAI/openadapt-flow/pull/162),
  [`5033b84`](https://github.com/OpenAdaptAI/openadapt-flow/commit/5033b844d29479c44af05044b6d65fd1553c23e8))

## v1.18.0 (2026-07-19)


### Bug Fixes

- **attended**: Rebind pause before checkpoint
  ([#152](https://github.com/OpenAdaptAI/openadapt-flow/pull/152),
  [`a93fdb5`](https://github.com/OpenAdaptAI/openadapt-flow/commit/a93fdb5f6a91fa3fe2b58223294729352c84ef54))

- **attended**: Rebind program pause before commit
  ([#152](https://github.com/OpenAdaptAI/openadapt-flow/pull/152),
  [`a93fdb5`](https://github.com/OpenAdaptAI/openadapt-flow/commit/a93fdb5f6a91fa3fe2b58223294729352c84ef54))

- **backends**: Harden Win32 window input ABI
  ([#159](https://github.com/OpenAdaptAI/openadapt-flow/pull/159),
  [`79f4dda`](https://github.com/OpenAdaptAI/openadapt-flow/commit/79f4ddad31bbf94b2936c9166644af7ea03453b6))

- **console**: Own attended backends on one thread
  ([#155](https://github.com/OpenAdaptAI/openadapt-flow/pull/155),
  [`3257673`](https://github.com/OpenAdaptAI/openadapt-flow/commit/32576737ca97d331fd8ac16dffcdd03deb64876e))

### Code Style

- Format Win32 scan-code refusal ([#159](https://github.com/OpenAdaptAI/openadapt-flow/pull/159),
  [`79f4dda`](https://github.com/OpenAdaptAI/openadapt-flow/commit/79f4ddad31bbf94b2936c9166644af7ea03453b6))

### Documentation

- **runner**: Design note — verified cloud contract, refusal matrix, required contract revisions
  ([#157](https://github.com/OpenAdaptAI/openadapt-flow/pull/157),
  [`8916ac9`](https://github.com/OpenAdaptAI/openadapt-flow/commit/8916ac98f160add56d53284fd27411873a8dea19))

### Features

- Add exact attended program receipts
  ([#152](https://github.com/OpenAdaptAI/openadapt-flow/pull/152),
  [`a93fdb5`](https://github.com/OpenAdaptAI/openadapt-flow/commit/a93fdb5f6a91fa3fe2b58223294729352c84ef54))

- Report-run — PHI-free SUCCESS summary rail to /api/runs/ingest-report
  ([#156](https://github.com/OpenAdaptAI/openadapt-flow/pull/156),
  [`1db6420`](https://github.com/OpenAdaptAI/openadapt-flow/commit/1db64205aaf0a0fe18c2753bae5cf106f4cf6943))

- **attended**: Execute governed halt actions
  ([#152](https://github.com/OpenAdaptAI/openadapt-flow/pull/152),
  [`a93fdb5`](https://github.com/OpenAdaptAI/openadapt-flow/commit/a93fdb5f6a91fa3fe2b58223294729352c84ef54))

- **backends**: Win32 WindowClient for remote-display window replay
  ([#159](https://github.com/OpenAdaptAI/openadapt-flow/pull/159),
  [`79f4dda`](https://github.com/OpenAdaptAI/openadapt-flow/commit/79f4ddad31bbf94b2936c9166644af7ea03453b6))

- **compiler**: Emit identifier_crop — arm identity-on-pixels for remote-display workflows
  ([#158](https://github.com/OpenAdaptAI/openadapt-flow/pull/158),
  [`b3e54ac`](https://github.com/OpenAdaptAI/openadapt-flow/commit/b3e54ac148bc2c1531a258c6ac10d7a9eb6b16d8))

- **runner**: Governed-dispatch verification + lease-logic client library (Experimental)
  ([#157](https://github.com/OpenAdaptAI/openadapt-flow/pull/157),
  [`8916ac9`](https://github.com/OpenAdaptAI/openadapt-flow/commit/8916ac98f160add56d53284fd27411873a8dea19))

- **runner**: Governed-dispatch verification + lease-logic client library (Experimental,
  library-only) ([#157](https://github.com/OpenAdaptAI/openadapt-flow/pull/157),
  [`8916ac9`](https://github.com/OpenAdaptAI/openadapt-flow/commit/8916ac98f160add56d53284fd27411873a8dea19))

### Testing

- **backends**: Pin Win32 Unicode and ABI contracts
  ([#159](https://github.com/OpenAdaptAI/openadapt-flow/pull/159),
  [`79f4dda`](https://github.com/OpenAdaptAI/openadapt-flow/commit/79f4ddad31bbf94b2936c9166644af7ea03453b6))

- **runner**: Fixture-driven suite for the runner client library
  ([#157](https://github.com/OpenAdaptAI/openadapt-flow/pull/157),
  [`8916ac9`](https://github.com/OpenAdaptAI/openadapt-flow/commit/8916ac98f160add56d53284fd27411873a8dea19))

## v1.17.2 (2026-07-19)


### Bug Fixes

- **capture**: Align malformed window marker error
  ([#154](https://github.com/OpenAdaptAI/openadapt-flow/pull/154),
  [`e626ce4`](https://github.com/OpenAdaptAI/openadapt-flow/commit/e626ce4501af529710ad200735ba022bff33de61))

## v1.17.1 (2026-07-19)


### Bug Fixes

- Preserve region stability across theme drift
  ([#153](https://github.com/OpenAdaptAI/openadapt-flow/pull/153),
  [`30512ec`](https://github.com/OpenAdaptAI/openadapt-flow/commit/30512ec1039790f29db7b2a1e3831f72fd91ed43))

### Testing

- Cover all observed theme over-halts
  ([#153](https://github.com/OpenAdaptAI/openadapt-flow/pull/153),
  [`30512ec`](https://github.com/OpenAdaptAI/openadapt-flow/commit/30512ec1039790f29db7b2a1e3831f72fd91ed43))

## v1.17.0 (2026-07-19)


### Features

- **hosted**: Claim secure browser pairings
  ([#151](https://github.com/OpenAdaptAI/openadapt-flow/pull/151),
  [`0625898`](https://github.com/OpenAdaptAI/openadapt-flow/commit/062589882596800d6681bfd9e29e5091af518c11))

## v1.16.1 (2026-07-19)


### Bug Fixes

- Preserve unchanged identity evidence in healing gate
  ([#150](https://github.com/OpenAdaptAI/openadapt-flow/pull/150),
  [`b179e1c`](https://github.com/OpenAdaptAI/openadapt-flow/commit/b179e1cacf6acbb5dccae6b9d246c906571314ce))

- Refuse unreviewed healing identity additions
  ([#150](https://github.com/OpenAdaptAI/openadapt-flow/pull/150),
  [`b179e1c`](https://github.com/OpenAdaptAI/openadapt-flow/commit/b179e1cacf6acbb5dccae6b9d246c906571314ce))

- **healing**: Preserve identity evidence across locator repair
  ([#150](https://github.com/OpenAdaptAI/openadapt-flow/pull/150),
  [`b179e1c`](https://github.com/OpenAdaptAI/openadapt-flow/commit/b179e1cacf6acbb5dccae6b9d246c906571314ce))

## v1.16.0 (2026-07-19)


### Bug Fixes

- **linux**: Align AT-SPI fixture app identity
  ([#148](https://github.com/OpenAdaptAI/openadapt-flow/pull/148),
  [`891f390`](https://github.com/OpenAdaptAI/openadapt-flow/commit/891f390b2e4565458549bb763548831afb3b3b91))

- **linux**: Name qualification accessibles explicitly
  ([#148](https://github.com/OpenAdaptAI/openadapt-flow/pull/148),
  [`891f390`](https://github.com/OpenAdaptAI/openadapt-flow/commit/891f390b2e4565458549bb763548831afb3b3b91))

### Chores

- **linux**: Lock native AT-SPI extra
  ([#148](https://github.com/OpenAdaptAI/openadapt-flow/pull/148),
  [`891f390`](https://github.com/OpenAdaptAI/openadapt-flow/commit/891f390b2e4565458549bb763548831afb3b3b91))

- **linux**: Normalize qualification checks after hotfix rebase
  ([#148](https://github.com/OpenAdaptAI/openadapt-flow/pull/148),
  [`891f390`](https://github.com/OpenAdaptAI/openadapt-flow/commit/891f390b2e4565458549bb763548831afb3b3b91))

### Documentation

- Regenerate Linux verification report deterministically
  ([#148](https://github.com/OpenAdaptAI/openadapt-flow/pull/148),
  [`891f390`](https://github.com/OpenAdaptAI/openadapt-flow/commit/891f390b2e4565458549bb763548831afb3b3b91))

### Features

- **linux**: Add fail-closed AT-SPI backend
  ([#148](https://github.com/OpenAdaptAI/openadapt-flow/pull/148),
  [`891f390`](https://github.com/OpenAdaptAI/openadapt-flow/commit/891f390b2e4565458549bb763548831afb3b3b91))

- **linux**: Add fail-closed native AT-SPI backend
  ([#148](https://github.com/OpenAdaptAI/openadapt-flow/pull/148),
  [`891f390`](https://github.com/OpenAdaptAI/openadapt-flow/commit/891f390b2e4565458549bb763548831afb3b3b91))

### Testing

- Qualify native Linux AT-SPI in isolated CI
  ([#148](https://github.com/OpenAdaptAI/openadapt-flow/pull/148),
  [`891f390`](https://github.com/OpenAdaptAI/openadapt-flow/commit/891f390b2e4565458549bb763548831afb3b3b91))

## v1.15.0 (2026-07-18)


### Bug Fixes

- **adapters**: Fail closed on window capture drift
  ([#146](https://github.com/OpenAdaptAI/openadapt-flow/pull/146),
  [`1bd3f55`](https://github.com/OpenAdaptAI/openadapt-flow/commit/1bd3f55b52abae96042ccf4bbcb9c55130f0b123))

### Code Style

- **console**: Satisfy pinned formatter
  ([#149](https://github.com/OpenAdaptAI/openadapt-flow/pull/149),
  [`540433b`](https://github.com/OpenAdaptAI/openadapt-flow/commit/540433b663b9c7dcb90a1353515d8f586e5f0e85))

### Features

- **adapters**: Convert window-scoped capture sessions in their own pixel space
  ([#146](https://github.com/OpenAdaptAI/openadapt-flow/pull/146),
  [`1bd3f55`](https://github.com/OpenAdaptAI/openadapt-flow/commit/1bd3f55b52abae96042ccf4bbcb9c55130f0b123))

- **console**: Add read-only attended exception queue
  ([#149](https://github.com/OpenAdaptAI/openadapt-flow/pull/149),
  [`540433b`](https://github.com/OpenAdaptAI/openadapt-flow/commit/540433b663b9c7dcb90a1353515d8f586e5f0e85))

## v1.14.1 (2026-07-18)


### Bug Fixes

- Restore MIT-only releases and harden operator console
  ([#144](https://github.com/OpenAdaptAI/openadapt-flow/pull/144),
  [`1a02182`](https://github.com/OpenAdaptAI/openadapt-flow/commit/1a02182b620cdb30542d31fe1d833bd01c6b5bf6))

## v1.14.0 (2026-07-18)

> **Yanked on PyPI.** This version shipped AGPL-licensed openIMIS
> benchmark files in the package artifact and must not be installed.


### Bug Fixes

- **build**: Drop duplicate force-include of the console static UI
  ([#133](https://github.com/OpenAdaptAI/openadapt-flow/pull/133),
  [`5b21c2f`](https://github.com/OpenAdaptAI/openadapt-flow/commit/5b21c2f5bbdbfad99e18043c7ecb4ae92a32dd24))

### Documentation

- **console**: Operator-console screenshots from a live fixture session
  ([#133](https://github.com/OpenAdaptAI/openadapt-flow/pull/133),
  [`5b21c2f`](https://github.com/OpenAdaptAI/openadapt-flow/commit/5b21c2f5bbdbfad99e18043c7ecb4ae92a32dd24))

### Features

- **console**: Localhost operator console over bundles, runs, and skill lineage
  ([#133](https://github.com/OpenAdaptAI/openadapt-flow/pull/133),
  [`5b21c2f`](https://github.com/OpenAdaptAI/openadapt-flow/commit/5b21c2f5bbdbfad99e18043c7ecb4ae92a32dd24))

## v1.13.0 (2026-07-18)

> **Yanked on PyPI.** This version shipped AGPL-licensed openIMIS
> benchmark files in the package artifact and must not be installed.


### Documentation

- Align product copy and report with scoped substrate evidence
  ([#143](https://github.com/OpenAdaptAI/openadapt-flow/pull/143),
  [`ede792f`](https://github.com/OpenAdaptAI/openadapt-flow/commit/ede792f5adf6d63c53afa46bb2d25e9caa725fae))

- Align product copy with accepted substrate evidence
  ([#143](https://github.com/OpenAdaptAI/openadapt-flow/pull/143),
  [`ede792f`](https://github.com/OpenAdaptAI/openadapt-flow/commit/ede792f5adf6d63c53afa46bb2d25e9caa725fae))

- Align technical report with scoped substrate evidence
  ([#143](https://github.com/OpenAdaptAI/openadapt-flow/pull/143),
  [`ede792f`](https://github.com/OpenAdaptAI/openadapt-flow/commit/ede792f5adf6d63c53afa46bb2d25e9caa725fae))

- Stabilize generated verification timestamp
  ([#143](https://github.com/OpenAdaptAI/openadapt-flow/pull/143),
  [`ede792f`](https://github.com/OpenAdaptAI/openadapt-flow/commit/ede792f5adf6d63c53afa46bb2d25e9caa725fae))

### Features

- **benchmark**: OpenIMIS claims-intake reference environment (insurance vertical)
  ([#141](https://github.com/OpenAdaptAI/openadapt-flow/pull/141),
  [`06824f2`](https://github.com/OpenAdaptAI/openadapt-flow/commit/06824f2bb39c2f4900d567a2f73bd27b154b583b))

## v1.12.2 (2026-07-18)


### Bug Fixes

- Qualify real RDP session readiness
  ([`b97af63`](https://github.com/OpenAdaptAI/openadapt-flow/commit/b97af63dfb69059914c3e1cd7104997bb082e8a8))

- Send RDP chords as physical scancodes
  ([`82a658a`](https://github.com/OpenAdaptAI/openadapt-flow/commit/82a658a6926ddac74b010b613535c023d0b5f079))

### Code Style

- Format RDP qualification files
  ([`8f2290c`](https://github.com/OpenAdaptAI/openadapt-flow/commit/8f2290cdd13b9bc0fedbcb417b77091296c6f9cd))

### Documentation

- Distinguish RDP evidence hashes
  ([`a5fe047`](https://github.com/OpenAdaptAI/openadapt-flow/commit/a5fe047e521b86b6ca0e39966dfe6ac2d747d21a))

### Testing

- Bind counted RDP readiness timeout
  ([`e2c7acf`](https://github.com/OpenAdaptAI/openadapt-flow/commit/e2c7acf238d42ccf802461457a9f6503328c96e3))

- Harden RDP desktop readiness proof
  ([`309fa24`](https://github.com/OpenAdaptAI/openadapt-flow/commit/309fa24152ee8ef22ecf3020614fdff46be53ebe))

- Record rejected real RDP qualification
  ([`57249dc`](https://github.com/OpenAdaptAI/openadapt-flow/commit/57249dc2e4d2ea9beed4246ae6dcb1edab85c579))

## v1.12.1 (2026-07-17)


### Bug Fixes

- Bound README claims and preserve plaintext PHI warnings
  ([#140](https://github.com/OpenAdaptAI/openadapt-flow/pull/140),
  [`5a8b3e6`](https://github.com/OpenAdaptAI/openadapt-flow/commit/5a8b3e6d3b4191eee3d8fd0e6b316663c83044fe))

- Keep plaintext PHI warnings fail closed
  ([#140](https://github.com/OpenAdaptAI/openadapt-flow/pull/140),
  [`5a8b3e6`](https://github.com/OpenAdaptAI/openadapt-flow/commit/5a8b3e6d3b4191eee3d8fd0e6b316663c83044fe))

### Documentation

- Bound README substrate claims ([#140](https://github.com/OpenAdaptAI/openadapt-flow/pull/140),
  [`5a8b3e6`](https://github.com/OpenAdaptAI/openadapt-flow/commit/5a8b3e6d3b4191eee3d8fd0e6b316663c83044fe))

## v1.12.0 (2026-07-17)


### Bug Fixes

- **macos**: Bind native input to exact AX focus
  ([#135](https://github.com/OpenAdaptAI/openadapt-flow/pull/135),
  [`c52a6b3`](https://github.com/OpenAdaptAI/openadapt-flow/commit/c52a6b3d458857e194eac48f9c3da3ec5ab4b0aa))

### Chores

- Adopt openadapt-capture 0.5.4 and run the adapter tests in CI
  ([#139](https://github.com/OpenAdaptAI/openadapt-flow/pull/139),
  [`4e424dd`](https://github.com/OpenAdaptAI/openadapt-flow/commit/4e424dddc20a33ff12d0cffddbcd098c20259ca8))

- Regenerate verification report for updated capture-bridge evidence
  ([#139](https://github.com/OpenAdaptAI/openadapt-flow/pull/139),
  [`4e424dd`](https://github.com/OpenAdaptAI/openadapt-flow/commit/4e424dddc20a33ff12d0cffddbcd098c20259ca8))

### Documentation

- Frame machine-checked claims and Frappe matrix result as strengths
  ([#137](https://github.com/OpenAdaptAI/openadapt-flow/pull/137),
  [`a4ad4e8`](https://github.com/OpenAdaptAI/openadapt-flow/commit/a4ad4e81dc94736f07fac3918677e025e43ca86b))

- Surface machine-checked claims + fix community funnel (--version, question routing)
  ([#137](https://github.com/OpenAdaptAI/openadapt-flow/pull/137),
  [`a4ad4e8`](https://github.com/OpenAdaptAI/openadapt-flow/commit/a4ad4e81dc94736f07fac3918677e025e43ca86b))

- Vision-forward README opening — any repeated GUI task, once
  ([#138](https://github.com/OpenAdaptAI/openadapt-flow/pull/138),
  [`5d2c711`](https://github.com/OpenAdaptAI/openadapt-flow/commit/5d2c711de1fe03aece605282bf70261f6cbed23f))

### Features

- **macos**: Add fail-closed native backend
  ([#135](https://github.com/OpenAdaptAI/openadapt-flow/pull/135),
  [`c52a6b3`](https://github.com/OpenAdaptAI/openadapt-flow/commit/c52a6b3d458857e194eac48f9c3da3ec5ab4b0aa))

### Testing

- **macos**: Preserve counted evidence and fix cleanup audit
  ([#135](https://github.com/OpenAdaptAI/openadapt-flow/pull/135),
  [`c52a6b3`](https://github.com/OpenAdaptAI/openadapt-flow/commit/c52a6b3d458857e194eac48f9c3da3ec5ab4b0aa))

## v1.11.0 (2026-07-17)


### Bug Fixes

- **effects**: Harden kit after adversarial review — PHI-free tasks, resumable params, honest SQL
  claims ([#134](https://github.com/OpenAdaptAI/openadapt-flow/pull/134),
  [`e373fb9`](https://github.com/OpenAdaptAI/openadapt-flow/commit/e373fb900d14878bba2f1924054baf521cb6a6cf))

### Features

- **effects**: Effect-verifier kit — declarative verifiers, coverage gates, reconciliation tasks
  ([#134](https://github.com/OpenAdaptAI/openadapt-flow/pull/134),
  [`e373fb9`](https://github.com/OpenAdaptAI/openadapt-flow/commit/e373fb900d14878bba2f1924054baf521cb6a6cf))

## v1.10.1 (2026-07-17)


### Bug Fixes

- **ci**: Repair cross-platform launch gates
  ([#136](https://github.com/OpenAdaptAI/openadapt-flow/pull/136),
  [`8e1fcdd`](https://github.com/OpenAdaptAI/openadapt-flow/commit/8e1fcdd01253ff22c5796a0717b561cf858a4fe2))

### Continuous Integration

- Allow exact-branch matrix validation
  ([#136](https://github.com/OpenAdaptAI/openadapt-flow/pull/136),
  [`8e1fcdd`](https://github.com/OpenAdaptAI/openadapt-flow/commit/8e1fcdd01253ff22c5796a0717b561cf858a4fe2))

## v1.10.0 (2026-07-17)


### Chores

- **windows**: Record UIA qualification evidence
  ([#132](https://github.com/OpenAdaptAI/openadapt-flow/pull/132),
  [`defafba`](https://github.com/OpenAdaptAI/openadapt-flow/commit/defafbae758a75c8e149d9693f2cffe1f2264b8c))

### Features

- **windows**: Qualify governed typed UIA replay
  ([#132](https://github.com/OpenAdaptAI/openadapt-flow/pull/132),
  [`defafba`](https://github.com/OpenAdaptAI/openadapt-flow/commit/defafbae758a75c8e149d9693f2cffe1f2264b8c))

## v1.9.1 (2026-07-17)


### Bug Fixes

- **runtime**: Preserve parameterized identity and scroll readiness
  ([#131](https://github.com/OpenAdaptAI/openadapt-flow/pull/131),
  [`077bae0`](https://github.com/OpenAdaptAI/openadapt-flow/commit/077bae0e54529799eecf345504d39e3b1f56396c))

### Code Style

- Apply canonical Ruff formatting ([#131](https://github.com/OpenAdaptAI/openadapt-flow/pull/131),
  [`077bae0`](https://github.com/OpenAdaptAI/openadapt-flow/commit/077bae0e54529799eecf345504d39e3b1f56396c))

## v1.9.0 (2026-07-16)


### Bug Fixes

- **on-prem**: Harden signed atomic release lifecycle
  ([#122](https://github.com/OpenAdaptAI/openadapt-flow/pull/122),
  [`07abc2c`](https://github.com/OpenAdaptAI/openadapt-flow/commit/07abc2c11e9527ec576016e478832179d7a345fa))

### Chores

- Bound openadapt-types pin + add interop CI job
  ([#121](https://github.com/OpenAdaptAI/openadapt-flow/pull/121),
  [`36bd900`](https://github.com/OpenAdaptAI/openadapt-flow/commit/36bd9004ba796d7c27a0f9715c2f9713baab19d0))

### Continuous Integration

- Bound and validate openadapt-types interop
  ([#121](https://github.com/OpenAdaptAI/openadapt-flow/pull/121),
  [`36bd900`](https://github.com/OpenAdaptAI/openadapt-flow/commit/36bd9004ba796d7c27a0f9715c2f9713baab19d0))

- Consolidate interop types validation
  ([#121](https://github.com/OpenAdaptAI/openadapt-flow/pull/121),
  [`36bd900`](https://github.com/OpenAdaptAI/openadapt-flow/commit/36bd9004ba796d7c27a0f9715c2f9713baab19d0))

### Documentation

- **on-prem**: Use current release in update examples
  ([#122](https://github.com/OpenAdaptAI/openadapt-flow/pull/122),
  [`07abc2c`](https://github.com/OpenAdaptAI/openadapt-flow/commit/07abc2c11e9527ec576016e478832179d7a345fa))

### Features

- **on-prem**: Real atomic, rollback-able offline update path
  ([#122](https://github.com/OpenAdaptAI/openadapt-flow/pull/122),
  [`07abc2c`](https://github.com/OpenAdaptAI/openadapt-flow/commit/07abc2c11e9527ec576016e478832179d7a345fa))

## v1.8.1 (2026-07-16)


### Bug Fixes

- Expose governed run params file ([#130](https://github.com/OpenAdaptAI/openadapt-flow/pull/130),
  [`ccc20c5`](https://github.com/OpenAdaptAI/openadapt-flow/commit/ccc20c5ddba48abb15ee7d04a10f423b5e28eebf))

### Documentation

- Align limits with governed authorization
  ([#128](https://github.com/OpenAdaptAI/openadapt-flow/pull/128),
  [`abf0233`](https://github.com/OpenAdaptAI/openadapt-flow/commit/abf02336503356822cfd54890944546c01658104))

- Bound structured identity uniqueness
  ([#128](https://github.com/OpenAdaptAI/openadapt-flow/pull/128),
  [`abf0233`](https://github.com/OpenAdaptAI/openadapt-flow/commit/abf02336503356822cfd54890944546c01658104))

- Clarify effect approval over-halt ([#128](https://github.com/OpenAdaptAI/openadapt-flow/pull/128),
  [`abf0233`](https://github.com/OpenAdaptAI/openadapt-flow/commit/abf02336503356822cfd54890944546c01658104))

- Make LIMITS a durable buyer trust boundary
  ([#128](https://github.com/OpenAdaptAI/openadapt-flow/pull/128),
  [`abf0233`](https://github.com/OpenAdaptAI/openadapt-flow/commit/abf02336503356822cfd54890944546c01658104))

- Make limits a durable trust boundary
  ([#128](https://github.com/OpenAdaptAI/openadapt-flow/pull/128),
  [`abf0233`](https://github.com/OpenAdaptAI/openadapt-flow/commit/abf02336503356822cfd54890944546c01658104))

## v1.8.0 (2026-07-16)


### Bug Fixes

- Close governed authorization bypasses
  ([#129](https://github.com/OpenAdaptAI/openadapt-flow/pull/129),
  [`9b6693c`](https://github.com/OpenAdaptAI/openadapt-flow/commit/9b6693cb4f2917ea8946bab9aa9dc2916789c07e))

- Fail predicate-only asset mutations
  ([#129](https://github.com/OpenAdaptAI/openadapt-flow/pull/129),
  [`9b6693c`](https://github.com/OpenAdaptAI/openadapt-flow/commit/9b6693cb4f2917ea8946bab9aa9dc2916789c07e))

- Preserve effects across transition halts
  ([#129](https://github.com/OpenAdaptAI/openadapt-flow/pull/129),
  [`9b6693c`](https://github.com/OpenAdaptAI/openadapt-flow/commit/9b6693cb4f2917ea8946bab9aa9dc2916789c07e))

- Snapshot governed bundle assets ([#129](https://github.com/OpenAdaptAI/openadapt-flow/pull/129),
  [`9b6693c`](https://github.com/OpenAdaptAI/openadapt-flow/commit/9b6693cb4f2917ea8946bab9aa9dc2916789c07e))

### Documentation

- Normalize authorization design ([#129](https://github.com/OpenAdaptAI/openadapt-flow/pull/129),
  [`9b6693c`](https://github.com/OpenAdaptAI/openadapt-flow/commit/9b6693cb4f2917ea8946bab9aa9dc2916789c07e))

### Features

- Bind governed run authorization ([#129](https://github.com/OpenAdaptAI/openadapt-flow/pull/129),
  [`9b6693c`](https://github.com/OpenAdaptAI/openadapt-flow/commit/9b6693cb4f2917ea8946bab9aa9dc2916789c07e))

## v1.7.3 (2026-07-16)


### Bug Fixes

- Preserve sanitized workflow integrity
  ([`d11e3d2`](https://github.com/OpenAdaptAI/openadapt-flow/commit/d11e3d20da2b9316690f0edc013c03dcc7484044))

## v1.7.2 (2026-07-16)


### Bug Fixes

- Patch optional MLX transformer dependencies
  ([`3e15cc1`](https://github.com/OpenAdaptAI/openadapt-flow/commit/3e15cc1cffc6c497ad12b55637d9bcd66d159344))

## v1.7.1 (2026-07-16)


### Bug Fixes

- Build releases without unsupported lock resolution
  ([`c096d71`](https://github.com/OpenAdaptAI/openadapt-flow/commit/c096d71d70e9d7579c9d9e53813dbb3100aed73a))

- Restore supported Python release matrix
  ([`2949267`](https://github.com/OpenAdaptAI/openadapt-flow/commit/29492670fb1b4d5fa4f2a54c05435d12401ec3c7))

## v1.7.0 (2026-07-15)


### Bug Fixes

- Fail-closed bundle push — verify compiled bundle before PHI-gate bypass
  ([`6d7e09f`](https://github.com/OpenAdaptAI/openadapt-flow/commit/6d7e09fac2588ac8194e9ecfe6b5078d77584751))

- Fail-closed PHI boundary on recording upload + break-report free text
  ([`d7baa16`](https://github.com/OpenAdaptAI/openadapt-flow/commit/d7baa160107023981313ee29bcbd85cf73421d53))

- Type sanitization approval envelope
  ([`4aba2dc`](https://github.com/OpenAdaptAI/openadapt-flow/commit/4aba2dc097e71b83ec373aa66f83ee71ca434c37))

### Code Style

- Satisfy launch formatting gate
  ([`5f224f9`](https://github.com/OpenAdaptAI/openadapt-flow/commit/5f224f909d7cba4852216b35a5224ce5b49ebede))

### Features

- Add hosted login/push/break-emit CLI (cloud connectivity)
  ([`260be45`](https://github.com/OpenAdaptAI/openadapt-flow/commit/260be453e237e649fa3782ec46b90c6893e11348))

- Govern hosted artifact activation
  ([`899d16c`](https://github.com/OpenAdaptAI/openadapt-flow/commit/899d16c5ca44b5a5fbf3165ba3126d0e77fb080a))

## v1.6.0 (2026-07-14)


### Bug Fixes

- Sync claims report + update obsolete desktop-record refusal test
  ([#118](https://github.com/OpenAdaptAI/openadapt-flow/pull/118),
  [`c6ffddf`](https://github.com/OpenAdaptAI/openadapt-flow/commit/c6ffddfeb94093bd07ffb88268bddb21de4ee6a3))

### Features

- Desktop recording via record --backend windows|rdp (record->compile->replay on desktop)
  ([#118](https://github.com/OpenAdaptAI/openadapt-flow/pull/118),
  [`c6ffddf`](https://github.com/OpenAdaptAI/openadapt-flow/commit/c6ffddfeb94093bd07ffb88268bddb21de4ee6a3))

## v1.5.1 (2026-07-14)


### Bug Fixes

- Update _resolve_step direct callers for the new workflow arg
  ([#116](https://github.com/OpenAdaptAI/openadapt-flow/pull/116),
  [`caeb823`](https://github.com/OpenAdaptAI/openadapt-flow/commit/caeb8234d3c3fc556e9b2c0a96464f204a976deb))

### Chores

- Wire sealed-templates+resume through the new seams; fix pre-existing OCR benchmark test
  ([#116](https://github.com/OpenAdaptAI/openadapt-flow/pull/116),
  [`caeb823`](https://github.com/OpenAdaptAI/openadapt-flow/commit/caeb8234d3c3fc556e9b2c0a96464f204a976deb))

## v1.5.0 (2026-07-14)


### Features

- Auto-provision win_agent TLS cert on launch + fix pre-existing factory token test
  ([#117](https://github.com/OpenAdaptAI/openadapt-flow/pull/117),
  [`ab2115b`](https://github.com/OpenAdaptAI/openadapt-flow/commit/ab2115b0db4346d0c659c2bb6a785e73a9a5da35))

## v1.4.0 (2026-07-14)


### Features

- CLI backend selector (--backend web|windows|rdp) — unblock the desktop/Citrix path
  ([#115](https://github.com/OpenAdaptAI/openadapt-flow/pull/115),
  [`780f8c1`](https://github.com/OpenAdaptAI/openadapt-flow/commit/780f8c11a2a909e5671beffc26888c4de4ef6f01))

## v1.3.0 (2026-07-14)


### Bug Fixes

- Unbreak non-PHI desktop callers under require_tls; drop pyautogui dep in TLS test
  ([#112](https://github.com/OpenAdaptAI/openadapt-flow/pull/112),
  [`3ffaddd`](https://github.com/OpenAdaptAI/openadapt-flow/commit/3ffaddd841bab73e17fadb028e96c1dac3d0f660))

### Chores

- Pin ruff==0.15.21 (stop CI-vs-local formatter drift)
  ([#114](https://github.com/OpenAdaptAI/openadapt-flow/pull/114),
  [`b1afd58`](https://github.com/OpenAdaptAI/openadapt-flow/commit/b1afd58fe188101d31961e1e56eda3fb4683d53e))

### Features

- TLS + cert-pinning on the win_agent channel (PHI-in-transit encryption)
  ([#112](https://github.com/OpenAdaptAI/openadapt-flow/pull/112),
  [`3ffaddd`](https://github.com/OpenAdaptAI/openadapt-flow/commit/3ffaddd841bab73e17fadb028e96c1dac3d0f660))

## v1.2.0 (2026-07-14)


### Code Style

- Format run_gate with ruff >=0.6 (match CI formatter)
  ([#109](https://github.com/OpenAdaptAI/openadapt-flow/pull/109),
  [`74e4d9f`](https://github.com/OpenAdaptAI/openadapt-flow/commit/74e4d9f47408b7fcda785aaa8e9e757a715591c2))

- Ruff format run_gate + tests (fix lint)
  ([#109](https://github.com/OpenAdaptAI/openadapt-flow/pull/109),
  [`74e4d9f`](https://github.com/OpenAdaptAI/openadapt-flow/commit/74e4d9f47408b7fcda785aaa8e9e757a715591c2))

### Continuous Integration

- Make E2E/wheel/CLI-smoke/docs/coverage merge-blocking + mypy-strict on safety path + CODEOWNERS
  ([#111](https://github.com/OpenAdaptAI/openadapt-flow/pull/111),
  [`96decd1`](https://github.com/OpenAdaptAI/openadapt-flow/commit/96decd18239a462d8fad53d503427251e98bb736))

### Documentation

- Remove agent-partition build notes, honest backend status, claims-consistency with LIMITS
  ([#108](https://github.com/OpenAdaptAI/openadapt-flow/pull/108),
  [`4e073cb`](https://github.com/OpenAdaptAI/openadapt-flow/commit/4e073cb388b043bedcc9597fb200cad9896b3182))

### Features

- Claim->evidence validation harness (maturity claims backed by tests + reproducible report)
  ([#110](https://github.com/OpenAdaptAI/openadapt-flow/pull/110),
  [`4eb12ff`](https://github.com/OpenAdaptAI/openadapt-flow/commit/4eb12ffcede76b2a2522cd63f6a381d4579ec5f0))

- Fail-closed 'openadapt-flow run' for regulated execution (cert+identity+effect+crypto gates)
  ([#109](https://github.com/OpenAdaptAI/openadapt-flow/pull/109),
  [`74e4d9f`](https://github.com/OpenAdaptAI/openadapt-flow/commit/74e4d9f47408b7fcda785aaa8e9e757a715591c2))

- Seal template screenshot crops in the AEAD bundle (close at-rest image-PHI gap)
  ([#113](https://github.com/OpenAdaptAI/openadapt-flow/pull/113),
  [`fe606b8`](https://github.com/OpenAdaptAI/openadapt-flow/commit/fe606b8a9ddc75067888dbe04a8828091b5b3580))

## v1.1.0 (2026-07-14)


### Documentation

- **on-prem**: Reconcile at-rest note now that per-bundle AEAD shipped (#103)
  ([#107](https://github.com/OpenAdaptAI/openadapt-flow/pull/107),
  [`696a68f`](https://github.com/OpenAdaptAI/openadapt-flow/commit/696a68f20d6125c1ab27ba264b13590504ce7c26))

### Features

- Citrix/remote-display pixel-only e2e proof (UIA-off, on-screen verify, identity-gate + halt)
  ([#106](https://github.com/OpenAdaptAI/openadapt-flow/pull/106),
  [`b4b13ad`](https://github.com/OpenAdaptAI/openadapt-flow/commit/b4b13adb5dd3a304e5c1bcd2a67d41da9a904d44))

## v1.0.0 (2026-07-14)


### Continuous Integration

- Bump the actions group across 1 directory with 7 updates
  ([#87](https://github.com/OpenAdaptAI/openadapt-flow/pull/87),
  [`247af2c`](https://github.com/OpenAdaptAI/openadapt-flow/commit/247af2c2ef3f5d9bd2a7f790cbdcf4c192ac3e3d))

### Features

- On-prem (air-gapped) clinic deployment package + docs
  ([#105](https://github.com/OpenAdaptAI/openadapt-flow/pull/105),
  [`aa47db6`](https://github.com/OpenAdaptAI/openadapt-flow/commit/aa47db642bc306b71cefa5816fa91d3b67470c01))

## v0.26.0 (2026-07-14)


### Features

- Integrated OpenEMR end-to-end harness (compiled arm, cost-capped agent arm gated off)
  ([#104](https://github.com/OpenAdaptAI/openadapt-flow/pull/104),
  [`25e05ea`](https://github.com/OpenAdaptAI/openadapt-flow/commit/25e05eaf213af84302a0c52f4105e67b614da94f))

Add benchmark/openemr_e2e: one entry point that ties the whole compiled runtime pipeline together
  against the OpenEMR add-patient-note flagship task, reproducibly and for $0:

compile -> replay -> effect-verify (system of record) -> catch a silent wrong write -> inject drift
  -> HALT -> teach the fix (governed learn/promote) -> re-run clean

Unlike the existing live-demo OpenEMR benchmark (openemr_benchmark), this orchestrates the runtime
  components end to end on a deterministic, offline fixture (in-process MockMed fault_server as the
  system of record) so CI runs the whole loop on every push. Each phase is a real runtime call; all
  six pass at $0 with zero model calls.

Cost guardrail: the compiled arm is model-free by construction (no API client is ever constructed).
  The paid computer-use agent arm is wired ONLY as a gate -- requires an explicit --agent-arm opt-in
  AND a hard --max-cost-usd cap, and even then this harness refuses to invoke it (AgentArmRefused),
  pointing at the audited paid path `scripts/openemr_demo.py benchmark`. The compiled-vs-agent ratio
  is reported from previously-recorded numbers, never spent now.

Live vs. fixture is always labelled and never a silent skip: the loop runs on the fixture
  (CI-reproducible); when OPENEMR_FHIR_BASE_URL is set the harness additionally probes the real FHIR
  system of record for reachability and records it honestly; --require-live makes an unreachable
  live SoR a hard error.

14 targeted tests drive the fixture path end to end and assert the wiring, $0, and the gated-off
  agent arm. ruff + mypy clean on changed files.

Claude-Session: https://claude.ai/code/session_01CKrVJJy5jWVCkXAqgUqtqZ

Co-authored-by: Claude Opus 4.8 <noreply@anthropic.com>

- Opt-in encryption-at-rest for bundles + checkpoints (AEAD)
  ([#103](https://github.com/OpenAdaptAI/openadapt-flow/pull/103),
  [`88623d3`](https://github.com/OpenAdaptAI/openadapt-flow/commit/88623d3a97b7ad3681914d07afef892c74f20564))

Add an opt-in AES-256-GCM encryption layer for compiled bundles and durable checkpoints, the at-rest
  cryptographic control the PHI story needs (docs/phi_at_rest.md). OFF by default -- the unencrypted
  path is byte-for-byte unchanged.

- openadapt_flow/crypto.py: audited-library AEAD (cryptography.AESGCM) with a scrypt-KDF'd
  passphrase (explicit key or OPENADAPT_BUNDLE_KEY), a self-describing JSON container, and
  domain-separated associated data (bundle vs checkpoint). Wrong/missing key and tampered ciphertext
  both fail loud (MissingKeyError / DecryptionError) with no partial load. -
  Workflow.save(encrypt=True, key=…) seals workflow.json as workflow.json.enc; Workflow.load(key=…)
  decrypts in memory. The schema-v2 integrity manifest is sealed over the plaintext BEFORE
  encryption, so a decrypted load still verifies content digest + asset hashes + provenance.
  manifest.json sidecar stays plaintext (encrypted:true) for an opaque compliance inventory. -
  CheckpointStore(key=…) seals every RunCheckpoint / PendingEscalation / RunManifest /
  ProgramCheckpoint / approval as …​.json.enc; threaded through DurableRun,
  Replayer(checkpoint_key=…), resume(…, key=…) and the resume/ approve CLI. bundle_version tolerates
  an encrypted bundle. - tests/test_encryption_at_rest.py: round-trip, wrong/missing-key failure,
  tamper detection, domain separation, unencrypted-default-unchanged, and checkpoint +
  program-checkpoint encryption (22 tests).

Claude-Session: https://claude.ai/code/session_01CKrVJJy5jWVCkXAqgUqtqZ

Co-authored-by: Claude Opus 4.8 <noreply@anthropic.com>

## v0.25.0 (2026-07-14)


### Bug Fixes

- Desktop e2e targets a reliable app (repeatable structural-rung proof, not flaky Calculator)
  ([#102](https://github.com/OpenAdaptAI/openadapt-flow/pull/102),
  [`c19fda1`](https://github.com/OpenAdaptAI/openadapt-flow/commit/c19fda1b31d7cf6026cb9b21fab4c9ab31d8ea43))

The opt-in Parallels desktop e2e drove the built-in UWP Calculator and asserted per-key
  AutomationIds (num7Button, ...). On Windows 11 ARM the modern Calculator is a packaged UWP app
  hosted under ApplicationFrameHost whose keypad does not surface as a findable top-level window
  through the UIA path WindowsBackend.locate_structural walks, so locate_structural returns None and
  the test fails even though the desktop stack works -- a flaky Calculator test, not a repeatable
  structural-rung proof.

Retarget the e2e at the in-tree Patient Notes -- Benchmark Harness WinForms app
  (scripts/desktop/patient_notes.ps1), the same target the desktop benchmark uses. Its controls are
  classic System.Windows.Forms TextBox (EditControl) / Button (ButtonControl) controls with explicit
  .Name / .AccessibleName, so WinForms exposes each with a stable AutomationId in the top-level
  window's UIA tree -- verified live on the Win11-ARM VM
  (locate_structural(automation_id='searchBox') -> StructuralHandle, conf 1.0). The demo clicks only
  searchBox/searchButton/noteBox/saveButton (all stable AutomationIds) and deliberately avoids the
  DataGridView rows, whose WinForms UIA tree is only partially populated -- so every recorded click
  is structurally armable and armed_coverage == 1.0 / the structural-rung assertion are meaningful
  and repeatable.

Deploy + seed + launch the app in session 1 via the existing ParallelsVM / session1_launch.py
  plumbing (no source modules changed). Stays snapshot-safe and OPT-IN (OAFLOW_PARALLELS_E2E=1):
  collected-but-skipped without the env var; the maintainer runs the live proof. Also corrects the
  compile -> Workflow.load flow (compile_recording returns a Workflow; the bundle dir is what
  Workflow.load takes).

Claude-Session: https://claude.ai/code/session_01CKrVJJy5jWVCkXAqgUqtqZ

Co-authored-by: Claude Opus 4.8 <noreply@anthropic.com>

### Chores

- Type-check the safety-critical modules (compile, identity, replayer) under mypy
  ([#100](https://github.com/OpenAdaptAI/openadapt-flow/pull/100),
  [`1d3bd34`](https://github.com/OpenAdaptAI/openadapt-flow/commit/1d3bd348ef6e21c6fbbb29b4c1fe77dfa795a7ed))

External reviews flagged that mypy's ignore_errors debt list excluded the most safety-critical
  modules. Bring the compiler, the pre-click identity gate, and the replayer under the type checker.

Removed from the [tool.mypy] ignore list and fixed the real errors: - compiler.compile: annotate the
  landmark `relation` local as its Literal; cast known-valid cv2.imdecode results (Optional stub) to
  np.ndarray; narrow the validated risk-override value to Step.risk's Literal via cast. -
  runtime.identity: already clean once un-ignored (no code change needed). - runtime.replayer: type
  `self.vision` as Any (always defaulted to the vision module in __init__); drop a redundant per-run
  attribute re-annotation; handle scrub_text's Optional[str] return at two log sites; add caller-
  guaranteed None asserts for api_actuator / state_verifier (mirroring the existing `assert binding
  is not None`); use the already-narrowed local `anchor` in the identity attempt closure.

Also tightened classify_step_risk's return type to the reversible/irreversible Literal. No runtime
  behavior change — typing only.

Documented the remaining, genuinely lower-stakes debt in pyproject.toml and noted that the
  safety-critical compile/replay path is now fully checked.

Claude-Session: https://claude.ai/code/session_01CKrVJJy5jWVCkXAqgUqtqZ

Co-authored-by: Claude Opus 4.8 <noreply@anthropic.com>

### Features

- Self-serve halt->learn via 'openadapt flow teach' (governed, refuses bad fixes)
  ([#101](https://github.com/OpenAdaptAI/openadapt-flow/pull/101),
  [`8e17641`](https://github.com/OpenAdaptAI/openadapt-flow/commit/8e17641c3c9bbd2e610f8d3e082b3bf24503ecbd))

Wire the existing halt->learn library capability into a one-command, operator-facing flow.
  `openadapt-flow teach <run_dir> --fix <recording_or_spec> --bundle <base> --out <updated_bundle>`
  loads a halted RunReport, turns a fix demonstration into the operator-correction ExecutionTrace,
  and drives the UNCHANGED learn_from_halt loop (induce -> RegressionGate -> held-out canary).

An updated, versioned bundle is written ONLY when the correction promotes; an underdetermined or
  unsafe fix is REFUSED (nonzero exit, base bundle unchanged, still halting) with the reason
  printed. The fix source is flexible: a recording directory of the resolution (reuses
  compile_recording) or a scripted correction spec (deterministic, CI-friendly).

New orchestration: openadapt_flow/learning/teach.py (no runtime files touched; reuses halt_loop /
  loop / gate / library / synth_stream). New CLI verb in __main__.py. New tests drive the modal-once
  scenario THROUGH the CLI: valid fix (spec + recording) -> updated bundle -> re-run resolves
  without halting; blind halt -> refusal, nonzero exit, bundle unchanged, re-run still halts; plus
  --help and input-guard cases.

Claude-Session: https://claude.ai/code/session_01CKrVJJy5jWVCkXAqgUqtqZ

Co-authored-by: Claude Opus 4.8 <noreply@anthropic.com>

## v0.24.0 (2026-07-14)


### Features

- Durable checkpoint/resume for ProgramGraph + authenticated approval on resume (P0)
  ([#99](https://github.com/OpenAdaptAI/openadapt-flow/pull/99),
  [`2bc2807`](https://github.com/OpenAdaptAI/openadapt-flow/commit/2bc2807ffba3d0548d4efc386a3534a641685053))

* feat: durable checkpoint/resume for ProgramGraph + authenticated approval on resume (P0)

P0-4 — durable ProgramGraph checkpoint/resume: the Phase-2 state-machine interpreter now checkpoints
  its whole INTERPRETER STATE after each verified action state (frame/subflow/loop stack, loop
  cursors, bound params, completed effect keys, expected on-screen text, transition-history hash,
  bundle version) via a new ProgramCheckpoint (in runtime/durable/, not ir.py). On a halt it durably
  PAUSES; resume RESTORES the interpreter from that state — re-entering each subflow/loop graph at
  the paused state, finishing the in-progress loop row and running the remaining rows — never
  restarting from the graph entry / a step index, and never re-performing an already-confirmed
  write. Linear-mode durability is unchanged.

P0-5 — resume as an authenticated approval workflow: resume() now REQUIRES an ApprovalRecord
  (approver identity + timestamp + chosen resolution + bundle version hash) before continuing a
  paused run; revalidates the live app is still in the checkpoint's expected state and that
  already-confirmed effects still hold; and refuses a stale (expired) pause. A caller with no valid
  approval cannot resume. The CLI approve/resume commands record and enforce it.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>

Claude-Session: https://claude.ai/code/session_01CKrVJJy5jWVCkXAqgUqtqZ

* style: ruff format durable files; merge main (schema-v2)

---------

Co-authored-by: Claude Opus 4.8 <noreply@anthropic.com>

## v0.23.0 (2026-07-14)


### Bug Fixes

- Learning gate compares program semantics, not step IDs (no silent safety regression)
  ([#97](https://github.com/OpenAdaptAI/openadapt-flow/pull/97),
  [`f399bbc`](https://github.com/OpenAdaptAI/openadapt-flow/commit/f399bbc0bb48d32acae92975ef075738df4499a8))

* fix: learning gate compares program semantics, not step IDs (Wave 2)

Gate now traverses both programs + subflows and quarantines a candidate that weakens any safety
  invariant: dropped identity-armed guard, dropped system-of-record effect, new consequential step
  without effects, risk downgrade, lost approval requirement, or a write made reachable under
  broader conditions. Matches steps by structural role, not step.id.

* style: ruff format learning-gate files

### Features

- Bundle schema v2 (manifest, digest, provenance) + load-time structural validation
  ([#98](https://github.com/OpenAdaptAI/openadapt-flow/pull/98),
  [`88b1fa8`](https://github.com/OpenAdaptAI/openadapt-flow/commit/88b1fa87b64a5bcf97104f36913e7d23d007f170))

Bump Workflow.schema_version 1 -> 2 (SCHEMA_VERSION constant) now that the IR carries ~10x its v1
  semantics, with a clean v1 -> v2 migration on read so every existing bundle still loads and
  replays byte-for-byte.

Schema v2 additions (ir.py): - BundleProvenance: compiler version + certification block (policy
  name, certified flag, status, timestamp, optional expiry). - BundleManifest: per-asset SHA-256
  file_hashes, a whole-bundle content_digest, provenance, and the encrypted flag (mirrors
  Workflow.encrypted; at-rest crypto still deferred). - Workflow.manifest field, sealed on save()
  (also written to a manifest.json sidecar), migrated/verified on load(), plus
  Workflow.stamp_certification().

New openadapt_flow/bundle_validation.py: - migrate_bundle_dict (v1 -> v2, additive), build_manifest,
  compute_file_hashes, compute_content_digest, verify_integrity (rejects a tampered workflow.json or
  sealed template; ignores post-seal template additions). - validate_workflow: structural rules
  (entry exists, transition/handler targets resolve, kind<->payload match, referenced subflows
  exist, unique state/step ids, terminals reachable, no unsafe unconditional cycle) plus the safety
  rule (every consequential/irreversible action carries effect verification). Load raises on
  structural malformation; the safety finding is surfaced to lint/certify so existing
  uncertified-but-well-formed bundles still load.

Tests: tests/test_bundle_schema_v2.py (29 cases) covers migration, stable-digest

round-trip, provenance/certification, integrity tampering, every validation rule on a crafted-bad
  graph, and a good program/linear bundle passing. test_annotate byte-identical assertion now
  excludes the (per-save-varying) manifest metadata.

Claude-Session: https://claude.ai/code/session_01CKrVJJy5jWVCkXAqgUqtqZ

Co-authored-by: Claude Opus 4.8 <noreply@anthropic.com>

## v0.22.0 (2026-07-14)


### Features

- Windows desktop parity — interactive-session VM agent, backend hardening, snapshot-safe Parallels
  e2e ([#95](https://github.com/OpenAdaptAI/openadapt-flow/pull/95),
  [`781f32c`](https://github.com/OpenAdaptAI/openadapt-flow/commit/781f32c5413274520b92b078cac65a8d12ccc859))

* feat: Windows desktop parity — interactive-session VM agent, backend hardening, snapshot-safe
  Parallels e2e

Bring the desktop (Windows/Parallels) path to parity with the web path: record -> compile -> replay
  over the structural (UIA) + vision ladder, with identity and effect verification unchanged and
  backend-agnostic.

- backends/win_agent: new self-contained, stdlib-only in-guest agent server that runs in the
  interactive session (session 1) — solves the session-0 screenshot/input problem. Loopback bind by
  default; optional bearer token (closes the PHI-audit unauthenticated-shim finding). Endpoints
  match the WindowsBackend contract (/screenshot, /execute_windows, /health). Ships a logon .bat +
  scheduled-task recipe (README). - windows_backend: send bearer auth when configured; the ACTION
  path now fails loudly (RuntimeError) on an unreachable/non-2xx agent so a dropped click/keystroke
  can never be a silent wrong action; read paths still return None (fall through the visual ladder).
  Confirmed it implements the StructuralActionBackend protocol so the resolver drives it unchanged.
  - adapters/desktop_recorder: live-record helper that arms a UIA structural locator per click (web
  parity), plus structural_armed_coverage metric. The offline capture-convert structural gap is
  documented precisely as a follow-up (no live UIA tree at conversion time). -
  parallels_vm.launch_agent: deploy + launch the hardened agent in session 1 with optional token;
  poll /health. - benchmark/desktop_benchmark: DesktopHarness threads an auth token to the backend.
  - tests/e2e: snapshot-safe, OPT-IN (OAFLOW_PARALLELS_E2E=1) Parallels proof driving the built-in
  Calculator through record->compile->replay, asserting the UIA structural rung fires;
  snapshot-first, revert-after, never deletes the VM or its snapshots. Collected-but-skipped
  everywhere else. - docs/desktop_windows_runbook.md: one-pass operator runbook for the live proof.

Mock-tested end to end on macOS (agent HTTP roundtrip incl. auth, backend error paths, launch_agent,
  recorder arming). e2e is skipped without the env var. Ruff clean; ruff check openadapt_flow green.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>

Claude-Session: https://claude.ai/code/session_01CKrVJJy5jWVCkXAqgUqtqZ

* fix: only send bearer header when a token is set; format e2e

Reconcile CI failure in tests/test_dom_identity.py. The hardening added an unconditional
  `headers=self._headers()` (None when unauthenticated) to the WindowsBackend request calls; that
  `headers=` kwarg was swallowed by the pre-existing `_FakeSession.post(url, json=, timeout=)` mock
  (no `headers` param) -> TypeError -> the tolerant read path returned None, so structured_text_at /
  structural_locator_at regressed to None.

Fix preserves the safety property and restores backward compat: build request kwargs with `timeout`
  always and `headers` ONLY when a token is set, so the unauthenticated call shape is byte-for-byte
  the legacy one and predates-auth mocks/callers are never handed an unexpected kwarg. The bearer
  header still goes out whenever auth_token is configured (win_agent auth roundtrip test and the
  header-assertion test both cover it).

Also apply ruff format to the opt-in e2e (CI format gate).

* fix: type-check ctypes.windll access on non-Windows (mypy)

`ctypes.windll` exists only on Windows, so mypy on the Linux CI lint job flagged
  `_active_console_session` with attr-defined. Access it dynamically via getattr and return -1 when
  absent — keeps the module importable and type-clean on macOS/Linux while behaving identically
  in-guest on Windows.

---------

Co-authored-by: Claude Opus 4.8 <noreply@anthropic.com>

## v0.21.2 (2026-07-14)


### Bug Fixes

- Reconcile induce --held-out test with honest 'STRUCTURAL coverage' header
  ([#96](https://github.com/OpenAdaptAI/openadapt-flow/pull/96),
  [`8d99247`](https://github.com/OpenAdaptAI/openadapt-flow/commit/8d992472674f7333bf133c73994bdcf39769bdd4))

Cross-PR regression on main: #91 (CLI) asserted the induce --held-out output says 'Held-out
  validation', but #93 (induction hardening) renamed it to 'Held-out STRUCTURAL coverage' (the
  honest naming -- it is structural trace-shape coverage, not behavioral validation). Each PR passed
  alone; together they broke main. Update the test to the honest header.

## v0.21.1 (2026-07-14)


### Bug Fixes

- Bind runtime params into effect contracts + idempotency keys (P0)
  ([#94](https://github.com/OpenAdaptAI/openadapt-flow/pull/94),
  [`35c4530`](https://github.com/OpenAdaptAI/openadapt-flow/commit/35c45309286de2a4cc32b911715f0ae3167aa66a))

* fix: bind runtime params into effect contracts + idempotency keys (P0)

A parameterized workflow verified its system-of-record effects against the values baked in at
  DEMONSTRATION time: Effect.match/value/idempotency_key were plain static strings and the replayer
  passed the effect to the verifier unchanged. So a run could write patient "Susan" via the GUI yet
  verify the recorded demo patient "Phil", check the demonstrated note instead of the run's, reuse
  ONE frozen idempotency key across unrelated runs, confirm an unrelated pre-existing record, or
  false-halt every non-demo run.

Fix: - ir/effects: add ValueExpr (literal | param) and make Effect.match values and Effect.value /
  idempotency_key ValueExpr. Back-compat is exact: a before validator coerces the v1 bare-string
  JSON form to ValueExpr(literal=...), and ValueExpr's __eq__/__hash__/__str__/__repr__ make a
  literal transparently string-compatible, so every existing reader (learning-gate signatures,
  codegen review comments) and matcher behaves byte-for-byte identically. validate_assignment keeps
  the compiler's raw-string assignment consistent. - replayer: resolve each effect's contract
  against the run's params (plus a reserved __run_id__ stable-per-run identity) BEFORE
  capture_pre_state and verify, on both the GUI and API-actuator paths, mirroring how ApiBinding
  {param} templates are filled. Pass the RESOLVED effect to the verifier. - idempotency key is now
  per-run: bind it to a run param (or __run_id__) so it no longer collides across unrelated runs; a
  literal (v1) key is unchanged. - persist a non-secret SHA-256 digest of each resolved contract in
  StepResult.effect_contract_hashes for auditability. - dedupe the double self.use_structural
  assignment in Replayer.__init__.

Tests: new test_value_expr.py (type contract + coercion) and

test_replayer_effect_param_binding.py (resolves to the run's patient/value not the demo's; v1
  plain-string effect loads + verifies identically; idempotency key differs across runs;
  resolved-contract hash recorded; end-to-end CONFIRM vs. frozen-demo-literal REFUTE against the
  real MockMed system of record).

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>

Claude-Session: https://claude.ai/code/session_01CKrVJJy5jWVCkXAqgUqtqZ

* fix: effect_mining emits ValueExpr (mypy) after Effect param-binding

Effect.match/value/idempotency_key are now ValueExpr; the runtime validator coerces bare strings at
  runtime (tests pass) but mypy flagged effect_mining passing raw str. Wrap mined literals in
  ValueExpr(literal=...) at the seven construction sites so the compiler is type-clean too.

* fix: silent_wrong_action emits ValueExpr (mypy) after Effect param-binding

---------

Co-authored-by: Claude Opus 4.8 <noreply@anthropic.com>

## v0.21.0 (2026-07-14)


### Bug Fixes

- Induction refuses to over-certify (uncertainty on flagged proposals, entity params, honest
  coverage naming) ([#93](https://github.com/OpenAdaptAI/openadapt-flow/pull/93),
  [`5557c53`](https://github.com/OpenAdaptAI/openadapt-flow/commit/5557c53751d9d3c9e0d8abdc9ceadb50e3a12e6e))

* fix: induction refuses to over-certify (uncertainty on flagged proposals, entity params, honest
  coverage naming)

Multi-trace induction is a useful PROTOTYPE whose output could over-claim. Both external reviews
  flagged this. Hardens the safety posture so certification matches what was actually verified. All
  changes are within induction.py logic (no ir.py / runtime changes); compiler/__init__.py
  re-exports the new name.

1. A flagged Proposal no longer auto-certifies. When an inferred branch or an OPTIONAL step over a
  CONSEQUENTIAL action (irreversible or effect-bearing) is proposed, induction ALSO emits an
  Uncertainty requiring operator confirmation, so certified=False until resolved. "Absent in some
  traces" is no longer a silent optional/skip for a consequential step -- it is a question routed to
  the disambiguation flow.

2. reproduction_score() renamed to structural_trace_coverage() (deprecated, warning-emitting alias
  kept). It is a structural trace-SHAPE score -- gives params full credit, treats loop tokens as
  reproduced, executes no app and checks no effect/identity -- so its docstring now states exactly
  what it does and does NOT verify, and nothing treats it alone as behavioral validation /
  certification (HeldOutValidation reworded to match).

3. Entity/selection generalization: a CLICK/selection whose target VARIES across traces is no longer
  frozen as a literal that silently re-selects the demo entity (the runtime clicks the resolved
  anchor, not a param). Click field-keys are now value-free so varying selections align; a varying
  selection becomes an ambiguous_selection Uncertainty with an advisory entity_ref proposal -- never
  a hardcoded demo entity.

4. Loop honesty: documented (module + _reduce_trace docstrings, in-file LIMITS) that only
  consecutive-repeated-subsequence loops are detected -- NOT search->process->return, pagination, or
  per-row conditional bodies. A repeated CONSEQUENTIAL body yields an ambiguous_loop Uncertainty
  instead of a possibly-wrong loop over an irreversible action.

Adds tests/test_induction_hardening.py (13 tests). Existing test_induction.py (17) unchanged and
  green.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>

Claude-Session: https://claude.ai/code/session_01CKrVJJy5jWVCkXAqgUqtqZ

* style: ruff format induction-hardening files

---------

Co-authored-by: Claude Opus 4.8 <noreply@anthropic.com>

### Features

- Expose induce / worklist / effects / resume / deployment-config via the CLI
  ([#91](https://github.com/OpenAdaptAI/openadapt-flow/pull/91),
  [`428ed49`](https://github.com/OpenAdaptAI/openadapt-flow/commit/428ed496452c3b0356adc71c01bbc8638f0e690c))

* feat: expose induce / worklist / effects / resume / deployment-config via the CLI

The library gained program IR, multi-trace induction, effect verification, API actuation, a durable
  runtime, and a skill library, but the installable CLI could still only do the old linear
  record->compile->replay. Surface the new capabilities so they are usable (and auditable) product,
  not test fixture.

New / extended subcommands (thin wrappers over existing library APIs; no library behavior changed):

- induce: multi-trace induction over MULTIPLE recording (or bundle) dirs into a program bundle via
  compiler.induction.induce_program; prints the audit trail, honestly refuses (nonzero exit, no
  bundle written) when intent is underdetermined, optional --held-out leave-one-out validation. -
  replay --worklist [RELATION=]FILE: load a CSV/JSON worklist of param rows and drive a program's
  loop over a relation (wired into Replayer.run worklists=). - replay/run effect + actuator wiring:
  --config / --effects-* / --api-* build and inject an EffectVerifier (rest/fhir/document-hash) and
  an ApiActuator, plus --durable for the Tier-3 durable runtime. All default off, so an unconfigured
  replay is byte-for-byte unchanged. - run: deployment-config-driven execution (the replay path
  wired for a real deployment instead of the MockMed demo). - resume <run_dir> / approve <run_dir>:
  surface the durable pause/resume + approval path via the current durable public API
  (CheckpointStore + resume). - deployment.py + docs/deployment.example.yaml: one canonical
  deployment config (backend / actuation / effects / runtime / policy) read by record / compile /
  certify / replay / run / resume.

Tests: tests/test_cli_{deployment,induce,new_commands}.py cover config load +

verifier/actuator construction, induce end-to-end (certified + refuse + held-out), worklist
  loaders/binding, approve/resume paths, and a fake-browser replay proving the deployment objects
  reach the Replayer. 95 relevant tests green (incl. existing
  induction/durable/effects/actuator/emit suites).

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>

Claude-Session: https://claude.ai/code/session_01CKrVJJy5jWVCkXAqgUqtqZ

* style: ruff format CLI + deployment files

---------

Co-authored-by: Claude Opus 4.8 <noreply@anthropic.com>

## v0.20.1 (2026-07-14)


### Bug Fixes

- Policy/lint traverse program graphs + require system-of-record effects (P0)
  ([#92](https://github.com/OpenAdaptAI/openadapt-flow/pull/92),
  [`726eff8`](https://github.com/OpenAdaptAI/openadapt-flow/commit/726eff843314216fda34dc7d1860f73ae27f9257))

Two P0 safety holes let clinical-write certify an unsafe bundle.

P0-1 — cert/lint now traverse the program graph + subflows, not just Workflow.steps. A program-mode
  bundle keeps its actions in program.states and subflows[*].states (kind==ACTION -> state.step),
  often with an EMPTY Workflow.steps, so evaluate_policy()/lint_workflow() saw "zero steps" and
  inspected nothing. New canonical generator openadapt_flow/traversal.py (iter_workflow_steps) is
  now the single source both checks iterate.

P0-2 — "effect verification" now means the system of record, not the screen.
  require_effect_verification_for only checked step.expect (visual/structural postconditions), so a
  clinical write certified merely because it had a TEXT_PRESENT assertion — the weak oracle the
  effect layer replaced. New rules: require_screen_postconditions_for (step.expect),
  require_system_effects_for (non-empty step.effects), require_idempotency_key_for (effect carries
  an idempotency key), prohibit_unconfirmed_effect_bindings (no placeholder /
  needs_operator_confirmation effect). clinical-write.yaml now requires real system-of-record
  effects + an idempotency key on writes, keeping screen postconditions as an ADDITIONAL
  requirement. require_effect_verification_for kept as a deprecated alias of
  require_screen_postconditions_for.

Claude-Session: https://claude.ai/code/session_01CKrVJJy5jWVCkXAqgUqtqZ

Co-authored-by: Claude Opus 4.8 <noreply@anthropic.com>

### Documentation

- Rewrite README to the current architecture + add a claims-consistency CI gate
  ([#90](https://github.com/OpenAdaptAI/openadapt-flow/pull/90),
  [`a3c3e06`](https://github.com/OpenAdaptAI/openadapt-flow/commit/a3c3e06bdd23fb674aafaf03507f71436ae4a837))

The README materially misrepresented the product: it called the runtime "vision-only", claimed "864
  tests", and said desktop/RDP backends were "adapters to come". Rewrite it to the current
  architecture (vision-FIRST with a structural DOM/UIA rung; existing WindowsBackend + FreeRDP
  adapters, mock-tested in CI) and add the marquee capabilities that were absent: the Phase-2
  workflow-program IR, multi-trace induction with refuse-if-underdetermined, effect verification
  against the system of record (REST/FHIR/doc-hash), the API actuator tier, policy lint/certify,
  governed healing, durable checkpoint/resume, and PHI-free identity templates. Fix DESIGN.md's
  stale "Frozen contracts" section to reflect ir.py's grown types
  (ParamSpec/Predicate/Guard/ProgramGraph/State/
  Transition/LoopSpec/Relation/ApiBinding/StructuralLocator + identity templates + effects).

Add scripts/check_consistency.py (run by tests/test_consistency.py and a fast step in ci.yml's
  required `test` gate) so the claims can't silently drift again. It fails on: a version mismatch
  between openadapt_flow.__version__ and pyproject; a broken file path in README/DESIGN/LIMITS or a
  workflow comment; a banned stale phrase in the README; or a hardcoded README test count that
  disagrees with `pytest --collect-only`. The README deliberately carries no hard test number, so
  that check is drift-proof by construction while staying enforceable if one is ever reintroduced.

Claude-Session: https://claude.ai/code/session_01CKrVJJy5jWVCkXAqgUqtqZ

Co-authored-by: Claude Opus 4.8 <noreply@anthropic.com>

## v0.20.0 (2026-07-14)


### Features

- Close the halt→learn→resolve loop (governed, one scenario) — modal-once
  ([#89](https://github.com/OpenAdaptAI/openadapt-flow/pull/89),
  [`aad55ee`](https://github.com/OpenAdaptAI/openadapt-flow/commit/aad55ee23cc56cd3c0b86903d74fd8fba6354635))

Today a HALT is a dead stop: the Replayer refuses rather than guessing on an unhandled state, but
  nothing learns from the operator's post-halt correction. The continuous-learning scaffold
  (learning/loop.py, gate.py, library.py, the Phase-2 interpreter) and the reference inducer existed
  but no real run fed them. This wires them for ONE real scenario — the MockMed modal-once drift —
  end to end, correctness-first, with no ungoverned learning.

- ir.py: add HaltObservation + RunReport.halt (additive/back-compatible). The structured record a
  halt emits: halt point, observed unexpected on-screen text (PHI-scrubbed), and the completed
  pre-context — shaped exactly like an ExecutionTrace so the loop consumes it with no reshaping. -
  runtime/replayer.py: Replayer.run now EMITS report.halt on any unsuccessful run (linear + program
  paths), probing the frame via the same OCR the runtime uses. Never raises — emission cannot turn a
  halt into a crash. - learning/halt_loop.py (new): the thin bridge. execution_trace_from_halt lifts
  a halt into the trace corpus; resolution_demonstration models the operator correction as a
  demonstration; learn_from_halt runs the UNCHANGED learn_from_traces (induce → RegressionGate →
  held-out canary → promote/refuse); promoted_workflow materializes the learned ProgramGraph as
  Workflow.program. - tests/test_halt_learn_loop.py: before(halt+emit) → learn(promote a guarded
  dismiss branch) → after(no halt) on the SAME scenario, plus a clean run and a DIFFERENT modal
  still behaving as before; the loop REFUSES an underdetermined correction and the RegressionGate
  BLOCKS an identity-weakening one.

Deliberately minimal: one scenario, no UI/CLI surface, no multi-scenario generalization. Old
  behavior intact — a bundle without a learned branch replays exactly as before.

Claude-Session: https://claude.ai/code/session_01CKrVJJy5jWVCkXAqgUqtqZ

Co-authored-by: Claude Opus 4.8 <noreply@anthropic.com>

## v0.19.1 (2026-07-14)


### Bug Fixes

- Remove plaintext PHI from compiled bundles + scrub/governance/egress guards
  ([#88](https://github.com/OpenAdaptAI/openadapt-flow/pull/88),
  [`8b44649`](https://github.com/OpenAdaptAI/openadapt-flow/commit/8b44649828cfb2a87fd7c2cee4c2bdc9f8861e36))

* fix: store identity band as PHI-free salted-hash template (audit REM-2)

The compiled bundle stored patient identifiers verbatim: anchor.context_text (the identity band:
  name/DOB/MRN), anchor.structured_identity (DOM/a11y text), and workflow.py reprinted the band as a
  comment. That makes workflow.json an unencrypted PHI-at-rest record (PHI audit GAP-1a / REM-2).

Replace the plaintext with a salted-hash, shape-preserving IdentityTemplate: per-token HMAC hashes
  (canonical + raw) plus non-identifying shape flags. The wrong-patient guard re-runs the SAME
  token-level match against the template at replay (runtime/identity_template.py, a faithful
  key-space port of identity.band_match) — right row verifies, wrong row refuses, with no readable
  identifier in the artifact. The near-miss ratio/containment contradiction the plaintext matcher
  does (needs the recorded string) is replaced by the stricter shape-based rules, so template mode
  is only ever as strict or STRICTER than the plaintext matcher — never a false-accept. Verified by
  a parity/safety corpus in tests/test_identity_template.py.

Also drop identifier-bearing TEXT_PRESENT postconditions on the compile path via an OPTIONAL
  openadapt-privacy (Presidio) pass (audit GAP-1b / GAP-3): a scrub that changes a candidate means
  it carries PII, so the candidate is dropped rather than mining a name into expect[].text. Graceful
  fallback when the privacy extra is absent (no crash; the governance guard blocks any residual
  plaintext).

Backward compatible: bundles compiled before this carry the plaintext fields and replay unchanged.
  New compiles emit context_text=None + identity_template. The heal-governance, policy,
  learning-gate, and replayer armed/coverage predicates all recognize the template so a
  template-armed step is never treated as unarmed.

Threat model: a salted hash of a low-entropy identifier is brute-forceable by a holder of the bundle
  + salt — this removes PLAINTEXT PHI, it is not encryption. Set OPENADAPT_FLOW_IDENTITY_SALT to
  keep the salt out of the bundle. At-rest encryption is the deferred next step
  (docs/phi_at_rest.md).

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>

Claude-Session: https://claude.ai/code/session_01CKrVJJy5jWVCkXAqgUqtqZ

* fix: fail-closed egress guard on the replay path (audit REM-3)

The default compiled replay is genuinely local, but nothing PREVENTED a caller (or the CLI, when
  OPENADAPT_FLOW_VLM_URL was set) from silently wiring an off-box grounder / identity-VLM /
  state-verifier that base64-POSTs a live patient screen to a paid API or on-prem appliance.

Add a fail-closed guard: the Replayer refuses to wire any egress-capable model component unless the
  operator explicitly opts in (allow_model_grounding / CLI --allow-model-grounding), and the run
  report now carries screenshots_may_leave_box, surfaced in REPORT.md ("Data egress: none" vs a
  warning). Egress capability is a MAY_EGRESS marker on the component classes (AnthropicGrounder,
  RemoteGrounder/RemoteIdentityVLM/RemoteStateVerifier = True; NullGrounder/OCRAnchorGrounder =
  local/False; a FallbackGrounder is egress iff any member is). The CLI no longer wires the
  appliance without the flag and prints that it is replaying fully local.

test_egress_guard.py is the regression guard on the "stays local" claim: a default
  Replayer(backend).run(...) with every HTTP transport stubbed to raise completes with ZERO outbound
  calls, and wiring an egress component without the opt-in raises EgressNotPermitted.

* fix: PHI governance + at-rest classification for bundles (audit REM-1)

A compiled bundle is a HIPAA-designated record, but nothing stopped one from reaching git (the
  docs/showcase-openemr/bundle precedent) or told a compliance team it is PHI.

- Manifest: add contains_phi / phi_scrubbed / encrypted to Workflow, set by the compiler, so a
  bundle can be classified and the format is ready for the deferred at-rest encryption
  (encrypted=false today). - Guard: scripts/check_bundle_phi.py blocks committing any workflow.json
  whose steps carry a plaintext identity band (structural, no deps) and — with the privacy extra —
  identifier-bearing postconditions/labels. Wired as a .pre-commit hook and a CI phi-guard job. -
  .gitignore excludes bundle output dirs (the committed showcase bundle is an explicit
  synthetic-data exception). - Regenerate the committed OpenEMR showcase bundle through the PHI-free
  compile path: context_text/structured_identity are now null (salted-hash templates),

workflow.py no longer reprints the band, contains_phi=false, and it passes the guard. Residual UI
  text is the FAKE OpenEMR public-demo patient (see the bundle README); no git history rewrite
  (forward-fix only). - docs/phi_at_rest.md: a bundle is a HIPAA record to classify/encrypt; the
  identity template removes plaintext but is NOT crypto; the template PNGs stay image-PHI protected
  by the guards + operator disk encryption; and the encrypted-sealed-bundle design is specified as
  the next step (deferred: encryption needs deployment-time key management that does not exist yet —
  half-shipped crypto is worse than none).

* style: ruff format + tighten band_verdict return type for the PHI remediation

Formatting-only reflow of the REM-1/REM-2/REM-3 files plus two typing fixes so the lint/type gate
  stays green: band_verdict now returns the 3-value Literal it always produced (so
  IdentityCheck.status assignments type-check in the new template verifier), and the heal-governance
  template branch narrows the Optional identity template before use. No behavior change.

* fix: scrub identifier landmarks + widen the PHI guard (audit REM-2)

A geometry landmark (anchor.landmarks[].ocr_text) is nearby ROW text used to re-locate the target;
  on a patient list that is frequently the name itself, so it was a residual plaintext-PHI vector
  alongside the identity band. Drop a landmark whose text the optional Presidio scrub flags as an
  identifier (geometry is a fallback rung and the identity gate still disposes, so dropping it is
  safe), and extend scripts/check_bundle_phi.py to flag landmark text too.

With the scrub active a fresh compile is now fully identifier-free (name/DOB/MRN absent from
  workflow.json); unconditionally, the identity band is hashed regardless of the scrub.
  docs/phi_at_rest.md records the remaining load-bearing residuals (target label ocr_text, typed
  literal) and the fix (parameterize the typed identifier as entity_ref).

---------

Co-authored-by: Claude Opus 4.8 <noreply@anthropic.com>

### Chores

- Engineering hygiene — version-sync, lint/type gate, reformat, supply-chain (final, on settled
  main) ([#86](https://github.com/OpenAdaptAI/openadapt-flow/pull/86),
  [`d38dc78`](https://github.com/OpenAdaptAI/openadapt-flow/commit/d38dc788bbcc2f062294021ff01168a599d4ee10))

* chore: engineering hygiene — version-sync, lint/type gate, supply-chain

Regenerated on current (settled) main; supersedes the stale #77 whose reformat was generated on an
  old main and no longer applies.

- Version-sync (the real bug): openadapt_flow.__version__ was 0.1.0 while the released pyproject
  version is 0.19.0. Sync __version__ and add version_variables to [tool.semantic_release] so
  releases keep them in lockstep (the wheel job already asserts they match). - Lint/type gate: add
  ruff, mypy, pytest-cov to [dev]; add [tool.ruff] (lint+format), [tool.mypy] (core package,
  ignore_missing_imports, a documented ignore_errors debt list re-derived against current main), and
  [tool.coverage]. Ship py.typed + wheel force-include (PEP 561). - CI: add a `lint` job (ruff check
  + ruff format --check + mypy) as its own job; the required `test` gate and the #76 structure are
  untouched. - Supply chain: SHA-pin the release.yml actions; add .github/dependabot.yml. -
  Scaffolding (absent on main): CONTRIBUTING, SECURITY, CODE_OF_CONDUCT, CODEOWNERS, issue + PR
  templates. - Import hygiene: ruff --fix import sorting + 3 dead-import removals so the lint gate
  is green.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>

Claude-Session: https://claude.ai/code/session_01CKrVJJy5jWVCkXAqgUqtqZ

* style: reformat openadapt_flow and tests with ruff format

Mechanical `ruff format openadapt_flow tests` reflow only — no behavior change. Isolated in its own
  commit so it is trivially revertable and keeps the chore commit's config/infra diff legible.
  Regenerated fresh on current main (the stale #77 reformat was generated on an old main).

---------

Co-authored-by: Claude Opus 4.8 <noreply@anthropic.com>

## v0.19.0 (2026-07-13)


### Features

- Opt-in session-video capture + how-it-works media generator
  ([#85](https://github.com/OpenAdaptAI/openadapt-flow/pull/85),
  [`513cfb4`](https://github.com/OpenAdaptAI/openadapt-flow/commit/513cfb4044acb5d154a11516ed1a46da6600d8f8))

* feat: opt-in Playwright session-video capture + how-it-works media generator

Add an OFF-by-default WebM session-video capture to the recorder and replayer, and a
  scripts/demo_media.py orchestrator that drives the real pipeline (record -> compile -> replay ->
  heal -> audit) against MockMed and a live OpenEMR to render the website's five "How it works"
  clips.

- PlaywrightBackend.launch(record_video_dir=...): opt-in; when set the page lives in a context that
  records a WebM finalized on close(). None keeps the old direct-page path with zero effect on
  normal runs. - demo_driver.record_triage_demo(record_video_dir=...) threads it through. - CLI:
  `demo-record --record-video DIR` and `replay --record-video DIR`. - scripts/demo_media.py: renders
  webm(VP9)+mp4(H.264 faststart)+gif+jpg poster per step (~880px, palette-optimized), writes
  MANIFEST.json, and honestly labels real footage vs the crafted Compile annotation. Presentation
  overlays are post-processing only and never touch the app under test. -
  tests/e2e/test_video_capture.py pins both halves of the opt-in contract.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>

Claude-Session: https://claude.ai/code/session_01CKrVJJy5jWVCkXAqgUqtqZ

* fix: wrap the Compile code-panel excerpt so it fits the panel width

The workflow.py excerpt lines overflowed the 548px code panel at 880px output width; reflow onto
  short lines (still the real step_010 fields: click_point, ocr_text, the irreversible risk, the
  text_present assert, and the note param).

---------

Co-authored-by: Claude Opus 4.8 <noreply@anthropic.com>

## v0.18.0 (2026-07-13)


### Features

- Auto-provision Chromium on first browser launch (pip install just works)
  ([#84](https://github.com/OpenAdaptAI/openadapt-flow/pull/84),
  [`04ec900`](https://github.com/OpenAdaptAI/openadapt-flow/commit/04ec9003d2fa7e30c955742de4f7b5c5c6e0a3bc))

`pip install openadapt-flow` pulls the Playwright Python package but not the Chromium browser
  binary, which previously required a separate manual `playwright install chromium` step.
  Post-install hooks are unreliable for wheels, so provision the browser lazily on first real use
  instead.

New `openadapt_flow/_browser_setup.ensure_chromium_installed()` probes for the browser binary (via
  Playwright's reported executable path) and, when missing, runs `python -m playwright install
  chromium` once with a friendly one-time notice. It is guarded by a module-level flag so it runs at
  most once per process, is a no-op when the browser is present, idempotent across runs, and has no
  import-time side effects. `OPENADAPT_FLOW_NO_AUTO_INSTALL=1` opts out for air-gapped /
  pre-provisioned environments, letting Playwright's own clear "browser not installed" error
  surface.

Hooked at every real browser-launch chokepoint: PlaywrightBackend.launch (covers demo-record,
  benchmark, dom-arm, hybrid, structural-action), InteractiveRecorder.start (record), and the CLI
  replay/bench direct launches in __main__. Updates the README: `pip install openadapt-flow` now
  suffices, with uvx / uv tool install noted as the fast path and the opt-out documented.

Claude-Session: https://claude.ai/code/session_01CKrVJJy5jWVCkXAqgUqtqZ

Co-authored-by: Claude Opus 4.8 <noreply@anthropic.com>

## v0.17.0 (2026-07-13)


### Features

- Continuous skill learning — versioned skill library + governed learn/promote loop (reuses #70
  promotion gate) ([#83](https://github.com/OpenAdaptAI/openadapt-flow/pull/83),
  [`642cc75`](https://github.com/OpenAdaptAI/openadapt-flow/commit/642cc7514f5b9cfb1df4c814318779629acdfcec))

* feat: workflow-program IR Phase 2 — loops, branches, subflows, exception paths (the state machine)

Evolve the compiled artifact from a linear action list into a parameterized STATE MACHINE (RFC
  docs/design/WORKFLOW_PROGRAM_IR.md §2), closing the review's "a workflow is not a list of actions"
  gap. Phase 1 (typed params, guards, wait_until) added the pieces; Phase 2 adds the control flow a
  trajectory cannot carry: LOOPS over a worklist, guarded BRANCHES, reusable SUBFLOWS, and

EXCEPTION paths — the program the PBD literature (Rousillon, WebRobot, Skill-DisCo, PROLEX) says a
  demonstration compiler must express.

IR (openadapt_flow/ir.py), additive and backward-compatible: - State (action | branch | loop |
  subflow_call | terminal) + Transition (guarded edge) form a ProgramGraph; an action state's
  payload IS a Phase-1 Step (the unchanged hardened leaf), a transition's guard IS a Phase-1
  Predicate. - Relation (worklist) + LoopSpec (bounded per-row body subflow); Workflow gains
  optional program / subflows / data_sources. When program is None the linear steps list runs
  exactly as today. - lift_to_program: mechanical degenerate lift (RFC §2.6) — a linear bundle is
  the single-path graph.

Interpreter (runtime/replayer.py): a deterministic graph interpreter ($0, zero model calls) that
  REUSES the linear per-action pipeline unchanged — every action state runs through _run_step, so
  identity / effect / risk / heal gates fire identically inside loop bodies and branches. Adds
  guarded transition selection (first match wins, no-match HALTs fail-safe), bounded worklist loops,
  subflow dispatch, and on_exception routing (graph try/except); unhandled failures and
  halt/escalate terminals stop the run. Bounded against non-terminating graphs (step budget +
  nesting depth). Linear path is byte-for-byte unchanged (program=None branch).

Tests (tests/test_program_ir_phase2.py, 18): loop runs body 3x / 0x / run-time worklist / bound
  enforced; branch takes each arm (param + screen predicate) and dead-ends HALT; subflow reused as
  loop body AND direct call; on_exception catches a failed action and continues; identity- and
  effect-gates fire inside a loop body; the lifted linear graph replays byte-identically to the
  linear replayer; program round-trips through save/load. Full non-e2e suite green in isolation (859
  passed; the concurrent-agent FileNotFoundError errors are environmental).

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>

Claude-Session: https://claude.ai/code/session_01CKrVJJy5jWVCkXAqgUqtqZ

* feat: continuous skill learning — versioned skill library + governed learn/promote loop (reuses
  #70 promotion gate)

Add openadapt_flow/learning/: orchestration for the review's item 7 — cluster successful/failed
  traces, revise the inferred Phase-2 state machine, validate a candidate on held-out executions,
  and promote only verified versions.

- SkillLibrary: skills as versioned ProgramGraphs (id -> ordered versions, each with provenance +
  validation score + status active/candidate/rolled_back/ superseded), persistent as JSON on disk;
  promotion retires the prior active, never deletes it (auditable lineage). - learn_from_traces:
  cluster -> coverage check -> revise (via a thin Inducer Protocol; multi-trace induction is a
  sibling PR, stubbed here) -> validate -> promote/quarantine. Reuses PR #70's RegressionGate per
  surviving step (identity/effect/risk may not regress), lifted from one heal to a whole program,
  then a held-out coverage canary — a candidate is promoted only if it passes BOTH; else the active
  version is retained and the candidate is quarantined with the reason (never a silent adoption). -
  Symbolic Phase-2 coverage interpreter: walks a ProgramGraph over a trace's observed facts with the
  SAME control-flow rules as runtime.replayer (guarded transitions, skip-guards, bounded loops) —
  deterministic, $0, no backend. - Synthetic drift-stream harness (synth_stream): a MockMed
  add-patient-note skill drifting over time (a new consent dialog appears mid-stream, a field is
  renamed) plus a deterministic reference inducer, so the loop is exercised end-to-end with no live
  app and no model calls.

Tests (tests/test_continuous_learning.py): a new dialog mid-stream is detected, induced, validated,
  and PROMOTED; noise/failures alone do NOT promote (stability); a rigged inducer that regresses
  identity/effect/risk is REJECTED by the gate with the active version retained; an uncoverable
  drift is refused; version history + provenance are correct. No model calls at runtime.

---------

Co-authored-by: Claude Opus 4.8 <noreply@anthropic.com>

## v0.16.0 (2026-07-13)


### Features

- Durable tiered runtime — checkpoint + pause/approve/resume from last verified state
  ([#80](https://github.com/OpenAdaptAI/openadapt-flow/pull/80),
  [`729d9b6`](https://github.com/OpenAdaptAI/openadapt-flow/commit/729d9b650648591fdd8cef4ba2119162ea1fd4fe))

Implement the escalation tier of the Workflow-Program IR runtime (RFC
  docs/design/WORKFLOW_PROGRAM_IR.md §5, Tier 3). Today the escalation tier just HALTs and a re-run
  starts from step 0 — unsafe in production because a workflow that already performed consequential
  writes would re-perform them.

New package openadapt_flow/runtime/durable/ (import-light: pydantic + json + pathlib, zero
  backend/vision/model):

- checkpoint.py: RunCheckpoint (written to run_dir/checkpoints/ after each VERIFIED step — identity
  ok + effects CONFIRMED + postconditions ok), PendingEscalation (written to
  run_dir/pending_escalation.json on a halt, capturing WHY it paused, the proposed operator options,
  and the checkpoint to resume from), RunManifest, and CheckpointStore. - controller.py: DurableRun
  (the replayer's per-run hook: verified -> checkpoint, halt -> pending escalation), classify_halt
  (halt reason -> category + operator options: effect_refuted / effect_indeterminate /
  effect_escalated / placeholder_effect / effect_unverifiable / unmet_guard / disambiguation /
  identity / postcondition / resolution), resumed_step_results. - resume.py: resume(run_dir,
  replayer) — reload the last verified checkpoint and continue from there (paused step onward),
  NEVER from step 0 and NEVER by handing the remaining workflow to a free-form agent (RFC §5
  non-goal). Idempotent w.r.t. already-verified steps; the paused step's re-execution is safe by the
  effect layer's idempotency_key posture.

Minimal, localized replayer touch-points (for Phase-2 state-machine reconciliation — all additive,
  +60 lines): - Replayer.__init__: new durable: bool = False. - Replayer.run: new resume_from:
  Optional[int]; construct one DurableRun; skip the already-verified prefix on resume and pre-load
  its results; call DurableRun.record after each step result is appended.

Tests (tests/test_durable_runtime.py, faked backend/vision + scripted in-memory EffectVerifier — no
  network, no model): clean run checkpoints each step and completes; a REFUTED effect mid-run writes
  a PendingEscalation + prior checkpoints; resume continues from the last checkpoint and does NOT
  re-run confirmed steps; resume does not re-verify confirmed steps (no double write);
  halt-on-first-step resumes from zero; placeholder-effect pause is classified; durability-off
  writes no artifacts. Full suite green (1098 passed, 16 skipped).

Claude-Session: https://claude.ai/code/session_01CKrVJJy5jWVCkXAqgUqtqZ

Co-authored-by: Claude Opus 4.8 <noreply@anthropic.com>

- Multi-trace induction — infer a parameterized program (params/loops/branches) from multiple demos,
  reject-if-underdetermined ([#81](https://github.com/OpenAdaptAI/openadapt-flow/pull/81),
  [`76ee70c`](https://github.com/OpenAdaptAI/openadapt-flow/commit/76ee70c5eb95a0163c0468bb0fd4c9ec0f7d9c85))

* feat: workflow-program IR Phase 2 — loops, branches, subflows, exception paths (the state machine)

Evolve the compiled artifact from a linear action list into a parameterized STATE MACHINE (RFC
  docs/design/WORKFLOW_PROGRAM_IR.md §2), closing the review's "a workflow is not a list of actions"
  gap. Phase 1 (typed params, guards, wait_until) added the pieces; Phase 2 adds the control flow a
  trajectory cannot carry: LOOPS over a worklist, guarded BRANCHES, reusable SUBFLOWS, and

EXCEPTION paths — the program the PBD literature (Rousillon, WebRobot, Skill-DisCo, PROLEX) says a
  demonstration compiler must express.

IR (openadapt_flow/ir.py), additive and backward-compatible: - State (action | branch | loop |
  subflow_call | terminal) + Transition (guarded edge) form a ProgramGraph; an action state's
  payload IS a Phase-1 Step (the unchanged hardened leaf), a transition's guard IS a Phase-1
  Predicate. - Relation (worklist) + LoopSpec (bounded per-row body subflow); Workflow gains
  optional program / subflows / data_sources. When program is None the linear steps list runs
  exactly as today. - lift_to_program: mechanical degenerate lift (RFC §2.6) — a linear bundle is
  the single-path graph.

Interpreter (runtime/replayer.py): a deterministic graph interpreter ($0, zero model calls) that
  REUSES the linear per-action pipeline unchanged — every action state runs through _run_step, so
  identity / effect / risk / heal gates fire identically inside loop bodies and branches. Adds
  guarded transition selection (first match wins, no-match HALTs fail-safe), bounded worklist loops,
  subflow dispatch, and on_exception routing (graph try/except); unhandled failures and
  halt/escalate terminals stop the run. Bounded against non-terminating graphs (step budget +
  nesting depth). Linear path is byte-for-byte unchanged (program=None branch).

Tests (tests/test_program_ir_phase2.py, 18): loop runs body 3x / 0x / run-time worklist / bound
  enforced; branch takes each arm (param + screen predicate) and dead-ends HALT; subflow reused as
  loop body AND direct call; on_exception catches a failed action and continues; identity- and
  effect-gates fire inside a loop body; the lifted linear graph replays byte-identically to the
  linear replayer; program round-trips through save/load. Full non-e2e suite green in isolation (859
  passed; the concurrent-agent FileNotFoundError errors are environmental).

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>

Claude-Session: https://claude.ai/code/session_01CKrVJJy5jWVCkXAqgUqtqZ

* feat: multi-trace induction — infer a parameterized program (params/loops/branches) from multiple
  demos, reject-if-underdetermined

Implements RFC docs/design/WORKFLOW_PROGRAM_IR.md §3 steps [4]+[5]: the induction loop the PBD
  lineage (Rousillon, WebRobot, Skill-DisCo, PROLEX) says a demonstration compiler must have. "One
  demonstration is evidence, not specification."

openadapt_flow/compiler/induction.py: - induce_program(traces) aligns multiple demos structurally
  and infers a Phase-2 ProgramGraph: PARAMS (values that VARY across traces at an aligned position;
  constant => literal), LOOPS (a repeated body whose count DIFFERS => LoopSpec over an inferred
  Relation), BRANCHES (a divergent step under a detectable condition => guarded branch, guard
  proposed/flagged), and OPTIONAL steps (present in some, absent in others, no condition => guarded
  skip). All deterministic, ZERO model calls. - validate_held_out / reproduction_score:
  leave-one-out held-out validation (infer from N-1, check reproduction of the held trace). -
  Reject-rather-than-guess: contradictory / underdetermined traces are QUARANTINED (no program
  emitted, certified=False) and routed to the disambiguation flow (#74), mirroring the identity
  gate's posture. - The optional compile-time Proposer (the #78 StepAnnotator fits behind it) only
  PROPOSES interpretations — flagged, never silently trusted, never flips an underdetermined point
  to certified.

Touch-points kept minimal: reuses the Phase-2 IR + Phase-1 ParamSpec/Guard/ Predicate verbatim (no
  new IR fields), reuses disambiguation's question model, and the emitted program replays through
  the EXISTING interpreter unchanged (compile.py untouched; compiler/__init__ re-exports the new
  API).

Tests (tests/test_induction.py, 17 tests): a synthetic MockMed corpus of trace variants covers (a)
  param, (b) loop, (c) branch/optional, (d) contradiction=> reject; held-out scores a good induction
  high and an over-specialized one low; underdetermined is flagged not guessed; the induced program
  round-trips through the real Phase-2 interpreter (faked backend/vision, zero model calls).

---------

Co-authored-by: Claude Opus 4.8 <noreply@anthropic.com>

## v0.15.0 (2026-07-13)


### Features

- Opt-in compile-time model annotation (label/risk/param proposals, confirm-don't-trust; runtime
  stays $0) ([#78](https://github.com/OpenAdaptAI/openadapt-flow/pull/78),
  [`75120bb`](https://github.com/OpenAdaptAI/openadapt-flow/commit/75120bba1a3a2bf8952875af314b572a07418db2))

The reviews' 'use the model at compile time, not just repair time' cheap win. A StepAnnotator
  Protocol proposes step labels, richer risk classifications, and parameter inferences from a
  demonstration; the model runs ONCE at compile, OFF by default, behind an interface (fake for
  tests, lazy Anthropic impl). A proposed risk UPGRADE applies (safe direction); a downgrade or
  consequential param is FLAGGED needs_operator_confirmation, never silently trusted. The
  runtime/replayer is untouched — zero model calls at replay.

Claude-Session: https://claude.ai/code/session_01CKrVJJy5jWVCkXAqgUqtqZ

Co-authored-by: Claude Opus 4.8 <noreply@anthropic.com>

- Workflow-program IR Phase 2 — loops, branches, subflows, exception paths (the state machine)
  ([#79](https://github.com/OpenAdaptAI/openadapt-flow/pull/79),
  [`ffe2242`](https://github.com/OpenAdaptAI/openadapt-flow/commit/ffe2242a5a36f9fa3c04111deb94402fcaa3af6b))

Evolve the compiled artifact from a linear action list into a parameterized STATE MACHINE (RFC
  docs/design/WORKFLOW_PROGRAM_IR.md §2), closing the review's "a workflow is not a list of actions"
  gap. Phase 1 (typed params, guards, wait_until) added the pieces; Phase 2 adds the control flow a
  trajectory cannot carry: LOOPS over a worklist, guarded BRANCHES, reusable SUBFLOWS, and

EXCEPTION paths — the program the PBD literature (Rousillon, WebRobot, Skill-DisCo, PROLEX) says a
  demonstration compiler must express.

IR (openadapt_flow/ir.py), additive and backward-compatible: - State (action | branch | loop |
  subflow_call | terminal) + Transition (guarded edge) form a ProgramGraph; an action state's
  payload IS a Phase-1 Step (the unchanged hardened leaf), a transition's guard IS a Phase-1
  Predicate. - Relation (worklist) + LoopSpec (bounded per-row body subflow); Workflow gains
  optional program / subflows / data_sources. When program is None the linear steps list runs
  exactly as today. - lift_to_program: mechanical degenerate lift (RFC §2.6) — a linear bundle is
  the single-path graph.

Interpreter (runtime/replayer.py): a deterministic graph interpreter ($0, zero model calls) that
  REUSES the linear per-action pipeline unchanged — every action state runs through _run_step, so
  identity / effect / risk / heal gates fire identically inside loop bodies and branches. Adds
  guarded transition selection (first match wins, no-match HALTs fail-safe), bounded worklist loops,
  subflow dispatch, and on_exception routing (graph try/except); unhandled failures and
  halt/escalate terminals stop the run. Bounded against non-terminating graphs (step budget +
  nesting depth). Linear path is byte-for-byte unchanged (program=None branch).

Tests (tests/test_program_ir_phase2.py, 18): loop runs body 3x / 0x / run-time worklist / bound
  enforced; branch takes each arm (param + screen predicate) and dead-ends HALT; subflow reused as
  loop body AND direct call; on_exception catches a failed action and continues; identity- and
  effect-gates fire inside a loop body; the lifted linear graph replays byte-identically to the
  linear replayer; program round-trips through save/load. Full non-e2e suite green in isolation (859
  passed; the concurrent-agent FileNotFoundError errors are environmental).

Claude-Session: https://claude.ai/code/session_01CKrVJJy5jWVCkXAqgUqtqZ

Co-authored-by: Claude Opus 4.8 <noreply@anthropic.com>

## v0.14.0 (2026-07-13)


### Features

- Api/tool actuator tier — perform writes via API when available, GUI fallback
  ([#72](https://github.com/OpenAdaptAI/openadapt-flow/pull/72),
  [`9c55239`](https://github.com/OpenAdaptAI/openadapt-flow/commit/9c552397202facc471cad561531c42ce250f53e6))

* feat: structural (DOM/UIA) action rung — vision-first, not vision-only

Make structural (DOM/accessibility) evidence a first-class ACTION rung — the deterministic top of
  the resolution ladder — not just an identity signal. On structure-bearing backends the runtime
  re-finds the recorded target as a DOM/UIA element and acts on its center deterministically,
  falling back to the visual ladder (template/ocr/geometry/grounder) only where structure is absent
  (pixel-only substrates: RDP/Citrix/canvas). Two external reviews + the desktop benchmark converge
  here: UIA execution 21/21 vs compiled visual replay 6/21.

Ladder: API → tool/MCP → [structural DOM/UIA] → template → template_global →

ocr → geometry → grounder(VLM) → human. `structural` is rung 0, above `ocr`, so an irreversible step
  may act on it (strongest evidence). The visual rungs are unchanged — the fallback floor for
  pixel-only substrates.

- ir: StructuralLocator (selector / role+name / UIA AutomationId) on Anchor.structural;
  StructuralHandle; "structural" added to Rung. - backend: optional StructuralActionBackend protocol
  (structural_locator_at + locate_structural). - resolver: structural rung first; falls through
  unchanged on miss/pixel-only. - playwright/windows backends: DOM (#id / role+name, with an
  occlusion hit-test) and UIA (AutomationId / role+name) locate. - recorder/compiler: capture the
  locator at record time; keep the visual anchor. - replayer: structural resolution flows through
  the SAME click path, so the identity gate + risk gate still fire; exempt from healing
  (deterministic locate ≠ stale template). New use_structural flag (default True) lets the
  visual-floor characterization suites exercise the pixel-only path.

Availability measured in benchmark/structural_action (21/21 vs 6/21). Identity gate proven to still
  abort a sibling on a structurally-resolved point. Occlusion safe-halt preserved. New coverage in
  tests/test_structural_rung.py and tests/e2e/test_structural_action.py.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>

Claude-Session: https://claude.ai/code/session_01CKrVJJy5jWVCkXAqgUqtqZ

* feat: API/tool actuator tier — perform writes via API when available, GUI as fallback

The EXECUTE half of the capability ladder (the reviews' 'where a real API exists, GUI-driving it is
  the wrong tool'). When a step carries a reachable ApiBinding, actuate the write via the API
  deterministically, confirm it with the EffectVerifier (non-CONFIRMED -> HALT), and skip GUI
  actuation; otherwise fall through to the structural->visual ladder unchanged. Fail-safe: an
  attempted-but-unknown API outcome HALTS rather than risk a double-write. Additive (no binding ->
  replays as today). $0 / zero model calls.

---------

Co-authored-by: Claude Opus 4.8 <noreply@anthropic.com>

## v0.13.0 (2026-07-13)


### Continuous Integration

- Fast required gate (e2e post-merge) to unclog the merge queue
  ([#76](https://github.com/OpenAdaptAI/openadapt-flow/pull/76),
  [`7e7605e`](https://github.com/OpenAdaptAI/openadapt-flow/commit/7e7605effb8a84f474faf527b45a132f4fa3520c))

* ci: fast required gate (e2e-excluded unit test), PR-only trigger, concurrency-cancel, caching

Extracted from the engineering-hygiene branch so the merge queue benefits now. Required 'test'
  context stays a single ubuntu/py3.12 job (fast unit suite, e2e excluded); full matrix + e2e run
  post-merge/nightly. Halves runner load (drops the push+pull_request double-trigger) and cancels
  superseded runs.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>

Claude-Session: https://claude.ai/code/session_01CKrVJJy5jWVCkXAqgUqtqZ

* ci: decouple test gate from lint (main not yet ruff-formatted; #62 restores lint)

* ci: drop --cov (pytest-cov not on main until #62); keep fast e2e-excluded gate

* ci: drop lint job (ruff/mypy + reformat land with #62); keep fast test gate only

---------

Co-authored-by: Claude Opus 4.8 <noreply@anthropic.com>

### Features

- Compiler effect-mining — auto-derive record_written/field_equals from a demo (honest placeholders
  where customer-specific) ([#75](https://github.com/OpenAdaptAI/openadapt-flow/pull/75),
  [`1f80080`](https://github.com/OpenAdaptAI/openadapt-flow/commit/1f8008075ddf3c6cccfe918c4e6622e3db04f37a))

Both external reviews flagged the same gap: the compiler emits vision/structural Postconditions but
  the typed system-of-record Effect contracts (record_written / field_equals) were hand-authored per
  workflow. This adds a heuristic, zero-model, zero-network miner that derives those contracts from
  what a demonstration actually observed — honoring the RFC §7 boundary between what is derivable
  now and what is "irreducibly app-specific".

New openadapt_flow/compiler/effect_mining.py: - Observed /api/db-style SoR delta
  (sor_before/sor_after on the event) with one new record -> a REAL record_written (identity
  selector = observed fields minus the surrogate id and the typed payload) plus a field_equals
  read-back per typed field, plus an idempotency key ONLY when the record actually carries one
  (never invented). - Structured DOM field map (dom_fields_*) whose field took the typed value -> a
  form-level field_equals, flagged needs_operator_confirmation (not a record write). - Consequential
  (irreversible) step with no captured SoR delta -> a flagged PLACEHOLDER record_written with a
  SENTINEL selector + needs_operator_ confirmation (§7: which API/record/idempotency-key is
  app-specific) — no fabricated endpoint. - Otherwise -> NO effect and an honest "no verifiable
  effect derivable" log.

Wiring (small, additive, opt-in): - compile_recording(mine_effects=False) — default off => bundle
  byte-identical; runs LAST (after risk_overrides) and attaches to Step.effects. +27 lines. -
  Effect.needs_operator_confirmation flag; replayer._verify_effects fails safe (HALT, never verify a
  fabricated binding) on a placeholder. - recorder + backend.SystemOfRecordBackend: a demo CAN now
  capture the SoR snapshot per event (sor_before/sor_after), exactly like url_before/after. -
  codegen renders mined effects (and loudly flags placeholders) in workflow.py.

Tests (tests/test_effect_mining.py, 13): mined effects CONFIRM on the live MockMed SoR and REFUTE a
  duplicate; no-delta -> honest gap; placeholder is marked and HALTs the run (not silently trusted);
  compile wiring + back-compat. Full suite: 1078 passed, 16 skipped.

Claude-Session: https://claude.ai/code/session_01CKrVJJy5jWVCkXAqgUqtqZ

Co-authored-by: Claude Opus 4.8 <noreply@anthropic.com>

- Interactive disambiguation — Socrates-style compile-time questions → guards/params (ask, don't
  guess) ([#74](https://github.com/OpenAdaptAI/openadapt-flow/pull/74),
  [`8c7ec41`](https://github.com/OpenAdaptAI/openadapt-flow/commit/8c7ec41dfb5c41e70f71dd835549bfd9d4e7dbf8))

* feat: workflow-program IR Phase 1 — typed params, guards, wait_until (additive, back-compatible)

Implements the RFC's Phase 1 (docs/design/WORKFLOW_PROGRAM_IR.md): the first additive,
  backward-compatible step from a linear macro IR toward a parameterized program. Typed parameters
  on Workflow (substituted at replay), an optional per-step guard (deterministic precondition;
  fail-safe), and wait_until (bounded readiness predicate that subsumes the SCROLL closed-loop). A
  bundle with none of these replays byte-identically to today. $0 / zero model calls.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>

Claude-Session: https://claude.ai/code/session_01CKrVJJy5jWVCkXAqgUqtqZ

* feat: interactive disambiguation — Socrates-style compile-time questions → guards/params (ask,
  don't guess)

Implements the RFC (docs/design/WORKFLOW_PROGRAM_IR.md §3 [3]) induction stage: where a single
  demonstration under-specifies intent, surface CONCRETE multiple-choice questions and apply the
  answer deterministically as a Phase-1 guard/param — instead of silently freezing an accidental
  interpretation.

New module openadapt_flow/compiler/disambiguation.py detects three ambiguity kinds structurally
  (ZERO model calls): - parameter candidate — an untagged typed value → ParamSpec + param binding -
  absent-result handling — an identity-armed entity selection after a search with no 0/>1-match
  branch → Guard(ANCHOR_RESOLVES, on_unmet="halt") - optional dialog — a once-handled popup →
  Guard(TEXT_PRESENT, on_unmet="skip")

Answers map to #71's Guard/Predicate/ParamSpec types verbatim (no new IR fields).
  Refuse-rather-than-guess (mirrors runtime.identity): an UNANSWERED consequential ambiguity (one
  gating an irreversible write) is flagged and the resolved skill is marked NOT certified until
  answered — never silently defaulted. Non-consequential unanswered ambiguities fall back to a safe
  no-op default.

Core is a pure, testable API — detect_ambiguities(workflow) and apply_answers(workflow, answers) —
  plus a thin `disambiguate` CLI subcommand (interactive prompt or --answers JSON). compile.py is
  UNCHANGED; disambiguation is an opt-in pass over a compiled bundle.

Stacks on #71 (feat/workflow-program-ir-phase1); retarget base to main after #71 merges.

---------

Co-authored-by: Claude Opus 4.8 <noreply@anthropic.com>

## v0.12.0 (2026-07-13)


### Features

- Structural (DOM/UIA) action rung — vision-first, not vision-only
  ([#69](https://github.com/OpenAdaptAI/openadapt-flow/pull/69),
  [`d9e5e6f`](https://github.com/OpenAdaptAI/openadapt-flow/commit/d9e5e6f47b76602e9f314051f9e0fd76a11cced5))

Make structural (DOM/accessibility) evidence a first-class ACTION rung — the deterministic top of
  the resolution ladder — not just an identity signal. On structure-bearing backends the runtime
  re-finds the recorded target as a DOM/UIA element and acts on its center deterministically,
  falling back to the visual ladder (template/ocr/geometry/grounder) only where structure is absent
  (pixel-only substrates: RDP/Citrix/canvas). Two external reviews + the desktop benchmark converge
  here: UIA execution 21/21 vs compiled visual replay 6/21.

Ladder: API → tool/MCP → [structural DOM/UIA] → template → template_global →

ocr → geometry → grounder(VLM) → human. `structural` is rung 0, above `ocr`, so an irreversible step
  may act on it (strongest evidence). The visual rungs are unchanged — the fallback floor for
  pixel-only substrates.

- ir: StructuralLocator (selector / role+name / UIA AutomationId) on Anchor.structural;
  StructuralHandle; "structural" added to Rung. - backend: optional StructuralActionBackend protocol
  (structural_locator_at + locate_structural). - resolver: structural rung first; falls through
  unchanged on miss/pixel-only. - playwright/windows backends: DOM (#id / role+name, with an
  occlusion hit-test) and UIA (AutomationId / role+name) locate. - recorder/compiler: capture the
  locator at record time; keep the visual anchor. - replayer: structural resolution flows through
  the SAME click path, so the identity gate + risk gate still fire; exempt from healing
  (deterministic locate ≠ stale template). New use_structural flag (default True) lets the
  visual-floor characterization suites exercise the pixel-only path.

Availability measured in benchmark/structural_action (21/21 vs 6/21). Identity gate proven to still
  abort a sibling on a structurally-resolved point. Occlusion safe-halt preserved. New coverage in
  tests/test_structural_rung.py and tests/e2e/test_structural_action.py.

Claude-Session: https://claude.ai/code/session_01CKrVJJy5jWVCkXAqgUqtqZ

Co-authored-by: Claude Opus 4.8 <noreply@anthropic.com>

## v0.11.0 (2026-07-13)


### Features

- Competitor-drift instrument harness (pluggable external-agent silent-wrong-action-rate runner,
  cost-capped) ([#73](https://github.com/OpenAdaptAI/openadapt-flow/pull/73),
  [`fea38e9`](https://github.com/OpenAdaptAI/openadapt-flow/commit/fea38e9075ddbd4ecd6a2ecca2d10918fc5e59ee))

Extend the self-directed silent-wrong-action benchmark (#67) from "our own runtime" to ANY external
  computer-use agent. A new `openadapt_flow.instrument` package points the #63 EffectVerifier at an
  arbitrary external agent's runs against the MockMed transactional-fault suite
  (`mockmed.fault_server`) and measures its silent-wrong-action rate (wrong effect landed while the
  agent reported success), anonymized by architecture class.

This PR is the HARNESS ONLY: no concrete competitor adapter, no paid API / model call, no vendor
  name. The real (cost-capped) run against a real competitor is a separate, user-gated step this
  makes one command away.

- `ExternalAgentAdapter` Protocol: the pluggable seam (run_task + a pre-flight estimate_cost_usd +
  an anonymized architecture_class). A real adapter wraps a vendor's own entry points behind it, out
  of this repo (docstring example). - `run_instrument`: drives an adapter through the fault suite,
  reads the system of record with RestRecordVerifier, and computes the rate — output anonymized by
  architecture class (Tool A/B/C), structurally enforced; never a vendor. - `CostGuard`: hard
  max_cost_usd / max_steps / max_runs kill-switch that aborts the WHOLE run the instant a cap would
  be crossed (pre- and post-flight), plus a dry-run mode that projects cost BEFORE spending. No run
  can silently exceed. - `StubExternalAgentAdapter`: deterministic, offline, $0 stub (screen-blind
  and honest modes) proving the harness measures nonzero silent-wrong on the fault classes and zero
  on clean ones end to end. - 25 tests; reuses the #67 ground-truth judge and #63 effect contract as
  the single source of truth. No existing files modified.

Claude-Session: https://claude.ai/code/session_01CKrVJJy5jWVCkXAqgUqtqZ

Co-authored-by: Claude Opus 4.8 <noreply@anthropic.com>

## v0.10.0 (2026-07-13)


### Features

- Workflow-program IR Phase 1 — typed params, guards, wait_until (additive, back-compatible)
  ([#71](https://github.com/OpenAdaptAI/openadapt-flow/pull/71),
  [`8bfcffe`](https://github.com/OpenAdaptAI/openadapt-flow/commit/8bfcffe8d95572dbdf2d96899a29be87ae92d101))

Implements the RFC's Phase 1 (docs/design/WORKFLOW_PROGRAM_IR.md): the first additive,
  backward-compatible step from a linear macro IR toward a parameterized program. Typed parameters
  on Workflow (substituted at replay), an optional per-step guard (deterministic precondition;
  fail-safe), and wait_until (bounded readiness predicate that subsumes the SCROLL closed-loop). A
  bundle with none of these replays byte-identically to today. $0 / zero model calls.

Claude-Session: https://claude.ai/code/session_01CKrVJJy5jWVCkXAqgUqtqZ

Co-authored-by: Claude Opus 4.8 <noreply@anthropic.com>

## v0.9.0 (2026-07-13)


### Features

- Interactive `record --url` + secret-typed parameters (never persisted)
  ([#64](https://github.com/OpenAdaptAI/openadapt-flow/pull/64),
  [`7f145f1`](https://github.com/OpenAdaptAI/openadapt-flow/commit/7f145f11845db2ecf3f47aa6949dd6fdaf49636e))

Closes the #1 adoption gap: the README promised "record a GUI workflow once" but the only recorder
  (`demo-record`) ran the hard-coded MockMed script. There was no way to record your OWN app.

record --url: - New `openadapt-flow record --url <app>` opens a headed browser on the user's own app
  and watches real clicks/typing/keys/scrolls via in-page capture-phase DOM listeners (installed
  with add_init_script so they survive navigations), writing the EXACT recording format `compile`
  already consumes. Stop with Ctrl-C or by closing the window. record -> compile -> replay now
  closes the self-serve loop for any app, not just the bundled demo. - Architecture: the
  expose_binding callback only appends raw events to a Python list (calling any page method inside a
  sync-API binding callback deadlocks the driver); the main loop drains it and does all
  screenshotting. Each step's before-frame is the previous step's settled frame (no post-navigation
  race); type/scroll runs capture their after-frame+structural state at the moment they happen so a
  following navigating click can't corrupt them. Structured DOM identity is captured in-page at
  click time, arming the identity ladder on interactively-recorded bundles. Reuses the existing
  Recorder via a new `record_observed` seam — the recording format is not forked.

Secret-typed parameters: - input[type=password] is auto-detected as secret; any field can be marked
  with `--secret <name>`. A secret's literal value is NEVER read into Python, never written to
  meta.json / events.jsonl / the compiled bundle, and its field region is redacted (solid black)
  from the persisted before/after frames. - At replay the value is injected from
  OPENADAPT_FLOW_SECRET_<PARAM>; a missing secret fails fast with an actionable message naming the
  env var. - Schema: ir.Step.secret + Workflow.secret_params; compiler carries the secret through
  with text=None; replayer resolves it from the environment.

Tests: tests/test_secret_params.py (fast unit: recorder redaction/non-persist,

compiler carry-through, replayer env injection + missing-secret error) and
  tests/test_interactive_recorder.py (headless scripted record -> compile -> replay proving the
  loop, no secret leak in any artifact, frame redaction, and env injection). Full suite: 962 passed,
  9 skipped.

Claude-Session: https://claude.ai/code/session_01CKrVJJy5jWVCkXAqgUqtqZ

Co-authored-by: Claude Opus 4.8 <noreply@anthropic.com>

## v0.8.0 (2026-07-13)


### Features

- Governed healing — reviewable patches, regression/perturbation gate, identity-never-weakened
  invariant (fixes heal context-drop) ([#70](https://github.com/OpenAdaptAI/openadapt-flow/pull/70),
  [`422ccf6`](https://github.com/OpenAdaptAI/openadapt-flow/commit/422ccf6566e955392a6ed218c0bccf60983ee7ae))

A heal was a LOCAL locator repair that silently swapped the anchor bundle, and (two external
  reviews) it could refresh a step's identity context to None — flipping an ARMED step to UNARMED
  and disabling the pre-click identity gate for that step while still reporting green. This makes
  healing a governed patch pipeline whose invariant is: a repair may change HOW an operation is
  performed, but never silently weaken WHAT it means or how its effects are verified.

New module openadapt_flow/runtime/healing/: - patch.py: HealEvent -> reviewable, diffable HealPatch
  (identity vs locator changes called out; identity_before/after snapshots). - governance.py:
  identity_preserved() (the invariant), effect/risk regression checks, RegressionGate.
  Deterministic, $0, no model calls; identity reuses the same OCR band matcher the pre-click gate
  uses. - pipeline.py: candidate -> gate -> canary -> promote/rollback; govern_heal() entrypoint. A
  refused patch is QUARANTINED (persisted for review) and the run HALTS — never auto-applies an
  unverified repair. - perturbation.py: deterministic synthetic UI-drift harness (shift/scale/
  retheme/reflow) + replay_patch regression report; reusable for held-out validation and future
  patch induction.

replayer.py: near-zero change — the heal hook now governs the built event and only applies a
  PROMOTED patch; a quarantined heal fails the step so the run halts. The identity-weakening is
  fixed in the heal code path, not by restructuring the replayer.

Tests (tests/test_governed_healing.py): the old ARMED->UNARMED weakening is reproduced end-to-end
  and blocked (quarantine + halt, anchor unchanged); a benign locator drift heals + passes the gate
  + promotes; dropped identity/ effect coverage and risk downgrades are rejected; the perturbation
  harness is deterministic. Full suite green (1007 passed, 10 skipped).

Claude-Session: https://claude.ai/code/session_01CKrVJJy5jWVCkXAqgUqtqZ

Co-authored-by: Claude Opus 4.8 <noreply@anthropic.com>

## v0.7.0 (2026-07-13)


### Features

- Policy engine + `lint`/`certify` + auto risk-classification (enforcement, not just disclosure)
  ([#65](https://github.com/OpenAdaptAI/openadapt-flow/pull/65),
  [`fe8876d`](https://github.com/OpenAdaptAI/openadapt-flow/commit/fe8876dbdaa438b9f9369980639b201a3679749a))

Turn the bundle's safety posture from DISCLOSURE into ENFORCEMENT: the compiler already reported
  weak coverage (unarmed clicks, vacuous postconditions, risk defaulting to reversible) but never
  refused an uncertifiable workflow before running it. "Compiled successfully" is too weak. This
  adds a compile-/pre-deploy layer on top of the unchanged replayer/identity/heal logic.

- Auto risk-classification (openadapt_flow/risk.py): the compiler now infers risk="irreversible" for
  CLICK/DOUBLE_CLICK steps whose intent/label is write-shaped
  (create/update/delete/submit/save/confirm/add ...), word- boundary matched so `address` != `add`.
  Biased toward irreversible on write-shaped steps (a false irreversible costs availability; a false
  reversible costs safety). risk_overrides still wins either way. This arms the existing below-OCR /
  unreadable-identity refusals by default for consequential writes.

- Policy schema + certifier (openadapt_flow/policy.py): a Policy (loadable from YAML, extra=forbid
  so a typo'd rule fails loudly) with rules prohibit_unarmed_clicks,
  prohibit_vacuous_postconditions, require_identity_for, require_effect_verification_for,
  max_unverified_steps, require_human_approval_below_confidence. evaluate_policy() -> a structured
  pass/fail report naming each violating step + reason.

- CLI: `openadapt-flow lint <bundle>` reports coverage gaps by severity (exit code by max severity);
  `openadapt-flow certify <bundle> --policy <name|path>` enforces a policy and exits nonzero on
  failure — making "runnable" distinct from "certified safe". Two example policies ship: permissive
  (default) and clinical-write (strict).

- Tests: auto-risk flags a save/submit step irreversible and leaves benign navigation reversible;
  certify FAILS a gappy bundle and PASSES a clean one under the strict policy; lint reports the
  known gaps; example policies parse. Two e2e healing tests recompile with write steps forced
  reversible (via a new bundle_writes_reversible fixture) so they isolate the heal mechanism from
  the now-default risk gate; the gate itself stays covered by TestIrreversibleRiskGate. Docs
  (LIMITS.md, README) updated.

Claude-Session: https://claude.ai/code/session_01CKrVJJy5jWVCkXAqgUqtqZ

Co-authored-by: Claude Opus 4.8 <noreply@anthropic.com>

### Testing

- Live OpenEMR end-to-end for the FHIR EffectVerifier (real GUI/API write → FHIR read-back)
  ([#68](https://github.com/OpenAdaptAI/openadapt-flow/pull/68),
  [`15962c5`](https://github.com/OpenAdaptAI/openadapt-flow/commit/15962c59ce622e2f57e81bc36c1ae8c52992ffa5))

* feat: EffectVerifier — independent effect verification against system-of-record (OpenEMR FHIR +
  second substrate)

Screen/vision postconditions silently mishandle 5 of 7 transactional fault classes (fault-model
  study, docs/LIMITS.md). This adds the concrete runtime for the RFC's typed Effect
  (docs/design/WORKFLOW_PROGRAM_IR.md, PR #61): verify REAL business effects against a system of
  record, not the screen.

- EffectVerifier protocol (capture_pre_state / verify) with typed Effect (record_written /
  field_equals) and a three-valued, fail-safe verdict: CONFIRMED / REFUTED / INDETERMINATE→HALT
  (mirrors the identity gate's refuse-rather-than-guess posture; an unreachable SoR never reads as
  success). - Three structurally-different verifier substrates, proving substrate- agnosticism:
  FhirEffectVerifier (OpenEMR FHIR R4, primary — real documented

contract; CI runs a byte-faithful FHIR Bundle fake, live path gated behind OPENEMR_FHIR_BASE_URL),
  RestRecordVerifier (MockMed fault_server /api/db, live in CI), DocumentHashVerifier (filesystem,
  SHA-256, non-HTTP). - Idempotency / at-most-once: an idempotency key plumbed through
  record_written verifies exactly one record per key. - Compensation: reconcile_or_escalate +
  RestCompensator — a detected duplicate on an irreversible effect is compensated (delete extras)
  and re-verified, or durably escalated; missing/partial/collateral/indeterminate always escalate. -
  THE PROOF (tests/test_effect_fault_matrix.py): at the real persistence boundary, screen-verify
  PASSES but effect-verify CATCHES each of the 5 silent classes — duplicate, optimistic-UI-reject,
  partial save, stale overwrite, double-click. - Additive DELETE /api/encounter/<id> on fault_server
  for compensation (never used by any ?fault= path; study behavior unchanged).

No Anthropic/model calls on any path (runtime hot path stays $0).

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>

Claude-Session: https://claude.ai/code/session_01CKrVJJy5jWVCkXAqgUqtqZ

* test: live OpenEMR end-to-end for the FHIR EffectVerifier (real API write → FHIR read-back)

Close PR #63's one honest caveat ("OpenEMR did NOT run live; the FHIR verifier is contract-gated
  against a fake"). Stand up a REAL local OpenEMR and wire the verifier's live path to it.

- benchmark/openemr_live/: docker-compose (OpenEMR 7.0.3 + MariaDB) with the REST + FHIR R4 APIs and
  OAuth2 enabled, a setup.sh that waits for install, enables the APIs + password grant, registers +
  enables an OAuth2 client, mints a bearer token, and prints OPENEMR_FHIR_BASE_URL/TOKEN/VERIFY_TLS,
  and a README with the one-command bring-up. - tests/test_effect_fhir_live_openemr.py: env-gated
  live test (skips in CI, runs when the instance is up). Writes a real Patient via OpenEMR's FHIR
  API, then has the #63 FhirEffectVerifier independently read it back: CONFIRMED (record_written +
  field_equals), REFUTED (wrong field value; absent record), INDETERMINATE→HALT (401 bad token is
  never "record absent").

Honest scope: the live write is a FHIR Patient POST (an API write, not GUI-driven) — OpenEMR's FHIR
  API exposes Observation read-only, so the note-as-Observation write the fake models cannot be
  created over FHIR on a stock OpenEMR. The property proven is the one the fake could not: the
  verifier's verdicts are correct against a REAL FHIR server. Verified end-to-end against
  openemr/openemr:7.0.3 (6/6 live tests).

Stacks on feat/effect-verifier (#63); retarget to main after #63 merges.

---------

Co-authored-by: Claude Opus 4.8 <noreply@anthropic.com>

## v0.6.0 (2026-07-13)


### Features

- Silent-wrong-action-rate benchmark (screen-verify vs effect-verify on MockMed faults)
  ([#67](https://github.com/OpenAdaptAI/openadapt-flow/pull/67),
  [`81f757d`](https://github.com/OpenAdaptAI/openadapt-flow/commit/81f757d048deee24d3b21ccff0fb2814b16c1310))

* feat: EffectVerifier — independent effect verification against system-of-record (OpenEMR FHIR +
  second substrate)

Screen/vision postconditions silently mishandle 5 of 7 transactional fault classes (fault-model
  study, docs/LIMITS.md). This adds the concrete runtime for the RFC's typed Effect
  (docs/design/WORKFLOW_PROGRAM_IR.md, PR #61): verify REAL business effects against a system of
  record, not the screen.

- EffectVerifier protocol (capture_pre_state / verify) with typed Effect (record_written /
  field_equals) and a three-valued, fail-safe verdict: CONFIRMED / REFUTED / INDETERMINATE→HALT
  (mirrors the identity gate's refuse-rather-than-guess posture; an unreachable SoR never reads as
  success). - Three structurally-different verifier substrates, proving substrate- agnosticism:
  FhirEffectVerifier (OpenEMR FHIR R4, primary — real documented

contract; CI runs a byte-faithful FHIR Bundle fake, live path gated behind OPENEMR_FHIR_BASE_URL),
  RestRecordVerifier (MockMed fault_server /api/db, live in CI), DocumentHashVerifier (filesystem,
  SHA-256, non-HTTP). - Idempotency / at-most-once: an idempotency key plumbed through
  record_written verifies exactly one record per key. - Compensation: reconcile_or_escalate +
  RestCompensator — a detected duplicate on an irreversible effect is compensated (delete extras)
  and re-verified, or durably escalated; missing/partial/collateral/indeterminate always escalate. -
  THE PROOF (tests/test_effect_fault_matrix.py): at the real persistence boundary, screen-verify
  PASSES but effect-verify CATCHES each of the 5 silent classes — duplicate, optimistic-UI-reject,
  partial save, stale overwrite, double-click. - Additive DELETE /api/encounter/<id> on fault_server
  for compensation (never used by any ?fault= path; study behavior unchanged).

No Anthropic/model calls on any path (runtime hot path stays $0).

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>

Claude-Session: https://claude.ai/code/session_01CKrVJJy5jWVCkXAqgUqtqZ

* feat: silent-wrong-action-rate benchmark (screen-verify vs effect-verify on MockMed faults)

Turn the #63 transactional fault-class matrix (tests/test_effect_fault_matrix.py) into a measured,
  publishable metric: the silent-wrong-action rate instrument
  (docs/validation/SILENT_WRONG_ACTION_RATE.md) pointed at our OWN runtime. No competitor runs, no
  paid API, no model calls, localhost only.

For each MockMed fault scenario (mockmed.fault_server) it records three independent judgments per
  run: ground truth off the system-of-record store (before vs after), the SCREEN oracle (app.js
  saved-banner rule applied to the real server response), and the EFFECT oracle (#63
  RestRecordVerifier's consequential-save contract against GET /api/db). Numbers are REAL — every
  run drives the fault server and reads back the store.

Measured (n=10/scenario, 90 runs): screen-verify silent-wrong-action rate 55.6% (undetected-wrong
  83.3%), effect-verify 0.0% (0.0%); false-abort screen 33.3% vs effect 0.0% (effect also rescues
  the timeout false-abort).

- openadapt_flow/benchmark/silent_wrong_action.py: benchmark + CLI (python -m
  openadapt_flow.benchmark.silent_wrong_action), results.json, SILENT_WRONG_ACTION.md, chart via
  chart_fonts (repo convention). - tests/test_silent_wrong_action_benchmark.py: CI guard for the
  qualitative claim (screen silent rate > 0; effect drives it to 0). -
  benchmark/silent_wrong_action/: committed real artifacts.

---------

Co-authored-by: Claude Opus 4.8 <noreply@anthropic.com>

- Wire EffectVerifier into the live replay path (Step.effects + halt/compensate on non-CONFIRMED)
  ([#66](https://github.com/OpenAdaptAI/openadapt-flow/pull/66),
  [`e975ace`](https://github.com/OpenAdaptAI/openadapt-flow/commit/e975ace853de42f5afb44254cbcbdc6c96adc928))

* feat: EffectVerifier — independent effect verification against system-of-record (OpenEMR FHIR +
  second substrate)

Screen/vision postconditions silently mishandle 5 of 7 transactional fault classes (fault-model
  study, docs/LIMITS.md). This adds the concrete runtime for the RFC's typed Effect
  (docs/design/WORKFLOW_PROGRAM_IR.md, PR #61): verify REAL business effects against a system of
  record, not the screen.

- EffectVerifier protocol (capture_pre_state / verify) with typed Effect (record_written /
  field_equals) and a three-valued, fail-safe verdict: CONFIRMED / REFUTED / INDETERMINATE→HALT
  (mirrors the identity gate's refuse-rather-than-guess posture; an unreachable SoR never reads as
  success). - Three structurally-different verifier substrates, proving substrate- agnosticism:
  FhirEffectVerifier (OpenEMR FHIR R4, primary — real documented

contract; CI runs a byte-faithful FHIR Bundle fake, live path gated behind OPENEMR_FHIR_BASE_URL),
  RestRecordVerifier (MockMed fault_server /api/db, live in CI), DocumentHashVerifier (filesystem,
  SHA-256, non-HTTP). - Idempotency / at-most-once: an idempotency key plumbed through
  record_written verifies exactly one record per key. - Compensation: reconcile_or_escalate +
  RestCompensator — a detected duplicate on an irreversible effect is compensated (delete extras)
  and re-verified, or durably escalated; missing/partial/collateral/indeterminate always escalate. -
  THE PROOF (tests/test_effect_fault_matrix.py): at the real persistence boundary, screen-verify
  PASSES but effect-verify CATCHES each of the 5 silent classes — duplicate, optimistic-UI-reject,
  partial save, stale overwrite, double-click. - Additive DELETE /api/encounter/<id> on fault_server
  for compensation (never used by any ?fault= path; study behavior unchanged).

No Anthropic/model calls on any path (runtime hot path stays $0).

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>

Claude-Session: https://claude.ai/code/session_01CKrVJJy5jWVCkXAqgUqtqZ

* feat: wire EffectVerifier into the live replay path (Step.effects + halt/compensate on
  non-CONFIRMED)

Real runs are now protected by independent system-of-record verification, not just the screen
  oracle. Closes the wiring gap between the merged EffectVerifier library (PR #63) and the Replayer.

- ir.Step gains `effects: list[Effect]` (default empty; RFC WORKFLOW_PROGRAM_IR.md 2.2). Threaded
  through bundle save/load round-trip; additive and back-compatible (bundles with no effects replay
  unchanged). The Effect type is imported at the BOTTOM of ir.py to avoid a circular import through
  runtime's package init; Step/Workflow are model_rebuilt. - Replayer gains `effect_verifier` /
  `effect_compensator` (OFF by default, mirroring state_verifier/grounder/identity_vlm). It
  snapshots the real system of record BEFORE a step's action and, after the screen postconditions
  pass, verifies each declared Effect against the record. A non-CONFIRMED verdict (REFUTED /
  INDETERMINATE) HALTS; an irreversible effect first runs reconcile_or_escalate (RECONCILED
  continues, ESCALATE halts). Zero model calls -- est_model_cost_usd untouched, the $0 guarantee. -
  Fail-safe: a step that declares effects with NO verifier configured is a deployment error and
  HALTS before acting -- an unverifiable consequential write is never silently accepted. -
  StepResult carries effect_verified / effect_results for the audit trail. - docs/LIMITS.md "5 of 7
  silent" updated: the gap is now closable in the live path, with the honest caveat that protection
  requires effects declared on the step AND a verifier configured. - tests/test_replayer_effects.py
  drives the REAL Replayer against the live MockMed fault_server via RestRecordVerifier: REFUTED
  halts despite a green screen; CONFIRMED proceeds; duplicate irreversible reconciles (and halts
  without a compensator); effects-without-verifier halts; a no-effects bundle replays unchanged.

---------

Co-authored-by: Claude Opus 4.8 <noreply@anthropic.com>

## v0.5.0 (2026-07-13)


### Continuous Integration

- Fix release double-build; add workflow_dispatch manual publish
  ([#59](https://github.com/OpenAdaptAI/openadapt-flow/pull/59),
  [`0f377e1`](https://github.com/OpenAdaptAI/openadapt-flow/commit/0f377e165ccb1374592d6ad98bec62fd9df8fd0e))

The v0.4.0 auto-release tagged and bumped the version but FAILED to publish: the workflow ran a
  separate 'uv build' step AND pyproject's semantic_release build_command runs 'uv build' too, so
  the second build hit PermissionError overwriting dist/openadapt_flow-0.4.0.tar.gz.

Fix: the auto-release job no longer has a separate build step — Semantic

Release's build_command is the single source of dist/, which the publish step consumes (this matches
  how the other repos avoid the collision). Added a workflow_dispatch 'manual-publish' job that
  checks out a given ref/tag, builds, and publishes to PyPI (OIDC) — used to publish the
  already-tagged v0.4.0 without deleting the tag, and a permanent manual/recovery path.

Claude-Session: https://claude.ai/code/session_01CKrVJJy5jWVCkXAqgUqtqZ

Co-authored-by: Claude Opus 4.8 <noreply@anthropic.com>

### Documentation

- Rfc — workflow-program IR (control flow, induction, capability-adaptive compilation)
  ([#61](https://github.com/OpenAdaptAI/openadapt-flow/pull/61),
  [`16a3621`](https://github.com/OpenAdaptAI/openadapt-flow/commit/16a36218b441d8a5936a37f3516d5abd97a42d00))

Design-only RFC for evolving the compiled artifact from a linear action list (ir.Workflow =
  list[Step]) into a parameterized workflow program: a state machine with typed params, guarded
  transitions, loops over worklists, branches, subflows, wait-until predicates, exception/approval
  nodes, and per-state risk + compensation. Today's linear workflow is the degenerate case (backward
  compatible).

Grounds every claim in the current code (ir.py, compiler/compile.py, backend.py, emit/*, DESIGN.md,
  docs/LIMITS.md) and the PBD lineage (Ringer straight-line replay -> Rousillon/Helena generalizer,
  WebRobot loopy-program synthesis, PROLEX single-demo recovery, Skill-DisCo parameterized FSM
  subgraphs, AWM/ASI skill induction, Socrates-style disambiguation). Covers: motivation (demo =
  evidence not spec), the target IR/DSL with a worked add-patient-note example, the induction loop
  (bootstrap -> candidates -> interactive disambiguation -> multi-trace -> validate/quarantine),
  capability-adaptive compilation (one contract, many backend impls), a tiered runtime
  (deterministic -> bounded one-transition model recovery -> durable checkpoint/resume, never
  free-run the remainder), a phased reversible migration, and an honest scope split (buildable now
  vs. needs a real customer workflow).

Claude-Session: https://claude.ai/code/session_01CKrVJJy5jWVCkXAqgUqtqZ

Co-authored-by: Claude Opus 4.8 <noreply@anthropic.com>

- Silent-wrong-action-rate instrument (anonymized, launch-ready)
  ([#60](https://github.com/OpenAdaptAI/openadapt-flow/pull/60),
  [`874ece3`](https://github.com/OpenAdaptAI/openadapt-flow/commit/874ece377d3374f4c617160832f64885d034bf21))

Add docs/validation/SILENT_WRONG_ACTION_RATE.md: an anonymized category measurement of the
  silent-wrong-action rate under UI drift for the self-healing / deterministic-replay automation
  class. Same methodology, ground truth, and "our own engine first / glass house / instrument not
  indictment / pre-committed interpretation" framing as our internal study, with our own honest
  pre/post-fix numbers, but with all other tools reduced to architecture classes (Tool A/B/C) — no
  product, vendor, version, or model names, and no raw tool-identifying evidence.

Claude-Session: https://claude.ai/code/session_01CKrVJJy5jWVCkXAqgUqtqZ

Co-authored-by: Claude Opus 4.8 <noreply@anthropic.com>

### Features

- Effectverifier — independent effect verification against system-of-record (OpenEMR FHIR + second
  substrate) ([#63](https://github.com/OpenAdaptAI/openadapt-flow/pull/63),
  [`2d85f1b`](https://github.com/OpenAdaptAI/openadapt-flow/commit/2d85f1b06a02e366e9f3bfb2af626c4d9e75de5d))

Screen/vision postconditions silently mishandle 5 of 7 transactional fault classes (fault-model
  study, docs/LIMITS.md). This adds the concrete runtime for the RFC's typed Effect
  (docs/design/WORKFLOW_PROGRAM_IR.md, PR #61): verify REAL business effects against a system of
  record, not the screen.

- EffectVerifier protocol (capture_pre_state / verify) with typed Effect (record_written /
  field_equals) and a three-valued, fail-safe verdict: CONFIRMED / REFUTED / INDETERMINATE→HALT
  (mirrors the identity gate's refuse-rather-than-guess posture; an unreachable SoR never reads as
  success). - Three structurally-different verifier substrates, proving substrate- agnosticism:
  FhirEffectVerifier (OpenEMR FHIR R4, primary — real documented

contract; CI runs a byte-faithful FHIR Bundle fake, live path gated behind OPENEMR_FHIR_BASE_URL),
  RestRecordVerifier (MockMed fault_server /api/db, live in CI), DocumentHashVerifier (filesystem,
  SHA-256, non-HTTP). - Idempotency / at-most-once: an idempotency key plumbed through
  record_written verifies exactly one record per key. - Compensation: reconcile_or_escalate +
  RestCompensator — a detected duplicate on an irreversible effect is compensated (delete extras)
  and re-verified, or durably escalated; missing/partial/collateral/indeterminate always escalate. -
  THE PROOF (tests/test_effect_fault_matrix.py): at the real persistence boundary, screen-verify
  PASSES but effect-verify CATCHES each of the 5 silent classes — duplicate, optimistic-UI-reject,
  partial save, stale overwrite, double-click. - Additive DELETE /api/encounter/<id> on fault_server
  for compensation (never used by any ?fault= path; study behavior unchanged).

No Anthropic/model calls on any path (runtime hot path stays $0).

Claude-Session: https://claude.ai/code/session_01CKrVJJy5jWVCkXAqgUqtqZ

Co-authored-by: Claude Opus 4.8 <noreply@anthropic.com>

## v0.4.0 (2026-07-13)


### Bug Fixes

- Downscale frames below the VLM image ceiling; record measured caveats
  ([#39](https://github.com/OpenAdaptAI/openadapt-flow/pull/39),
  [`d9a4a96`](https://github.com/OpenAdaptAI/openadapt-flow/commit/d9a4a9667b7cc816d14b349b1c7fd92d0854aea4))

Direct follow-up to the real-model validation (benchmark/appliance_validation), which found the
  served 4-bit VLM emits empty/degenerate output on native-Retina (~1800px+) screenshots — so the
  grounder and state-verifier silently went inert (every call -> null/uncertain -> safe-halt, never
  useful) because the clients sent frames un-downscaled.

- _downscale_for_model(): downscale a PNG so its longest side is <= 1024, returning the scale so
  callers can map coordinates back. Fail-open on size only (malformed/oversize -> original bytes ->
  model may abstain -> safe-halt). - RemoteGrounder.locate(): send a downscaled frame and map the
  proposed point BACK to original pixel space before anything acts on it. -
  RemoteStateVerifier.verify(): downscale before sending (no coordinates to map). - Identity crops
  are already small and are untouched.

docs/LIMITS.md: replace the drift-oracle hand-wave with the MEASURED numbers (false-rescue 1/8
  ~12.5% on in-progress 'Saving…' ambiguity; true-rescue 6/6) and record two more measured caveats:
  the native-Retina image ceiling (now fixed here) and that the grounder resolves the column but not
  the row on dense lists (0/6, ~470px median error) — fails safe, but not yet dependable for
  list-dense UIs; a stronger grounding model is the open item.

9 new tests pin the scaling maths and the grounder point round-trip.

Claude-Session: https://claude.ai/code/session_01CKrVJJy5jWVCkXAqgUqtqZ

Co-authored-by: Claude Opus 4.8 <noreply@anthropic.com>

- Honest-docs corrections + privacy-default hardening (weak-spot review)
  ([#47](https://github.com/OpenAdaptAI/openadapt-flow/pull/47),
  [`ad360a4`](https://github.com/OpenAdaptAI/openadapt-flow/commit/ad360a440df8138ef4b949a2e481329251aa1787))

Closes the remaining MED/LOW review findings.

Privacy defaults (real clinical foot-guns in the merged code): - vlm_service --host now defaults to
  127.0.0.1 (was 0.0.0.0); an empty VLM_SERVICE_TOKEN and/or a non-loopback bind now warn LOUDLY at
  startup (unauthenticated PHI endpoint on the network) instead of silently. -
  OPENADAPT_FLOW_SCRUB=on now IMPLIES image redaction of persisted frames, so 'on' no longer
  text-scrubs REPORT.md while leaking full PHI screenshots (the two-flag false-sense gap);
  SCRUB_IMAGES=1 remains the explicit opt-in for other modes. - REPORT.md written with plaintext
  identity text under default 'auto' (extra absent) now emits a one-time plaintext-PHI warning.

Honest-docs corrections: - LIMITS.md + VALIDATION.md closing 'no false success without a wrong
  action' scoped to the UI-drift matrix + cross-ref the fault-model transactional exception it
  contradicted. - ON_PREM_VLM.md drift-oracle 'robust to drift' qualified (conditional;
  downscaled-only; ~12.5% false-rescue). - grounding_eval/REPORT.md: the ~3px is a renderer artifact
  (text-center vs DOM-button-center), the VLM baseline is a quoted constant not head-to-head, and
  method C is handed ground-truth identity. - README test count refreshed.

32 tests pass (privacy + vlm_service).

Claude-Session: https://claude.ai/code/session_01CKrVJJy5jWVCkXAqgUqtqZ

Co-authored-by: Claude Opus 4.8 <noreply@anthropic.com>

- P0 wrong-patient — separator-formatted collapsible MRNs bypassed the glyph gate
  ([#45](https://github.com/OpenAdaptAI/openadapt-flow/pull/45),
  [`fce2df0`](https://github.com/OpenAdaptAI/openadapt-flow/commit/fce2df050e93c297ad245b8cae3e1537019f5db4))

An adversarial review found a wrong-patient false-accept (10th reopening). `_is_identifier_shaped`
  ended with `token.isalnum()`, which is False for any separator-bearing token — so a dash-formatted
  MRN (`MG-4408`) was never flagged as glyph-confusable. A same-NAME/same-DOB homonym differing only
  by an O/0 glyph in a DASHED MRN (`MG-4408` vs `MG-44O8` -> OCR-collapse to a byte-identical band)
  returned VERIFIED instead of abstaining, on pure-pixel/OCR substrates. Confirmed via the public
  `verify_target_identity` entry point. It also contradicted LIMITS.md's "ANY identifier-position
  token" claim.

Fix: strip intra-identifier separators before the run test, excluding only

date-shaped tokens FIRST (new `_is_date_like`, range-validated on the homoglyph-canonical form) so a
  DOB never becomes a gated identifier and over-halts every band. Separator MRNs are now gated;
  dates are not.

- Safety invariant intact: test_zero_false_accepts_* still pass (0 false-accept). - Cost is the SAFE
  direction: v1 over-halt 48.15%->60.42%, v2 ->47.33% (the corpus carries dashed collapsible MRNs);
  budgets widened + documented. - New tests/test_identity_separator_glyph_10th.py pins the class
  (was untested). - LIMITS.md 'ANY identifier-position token' now covers separator MRNs.

Claude-Session: https://claude.ai/code/session_01CKrVJJy5jWVCkXAqgUqtqZ

Co-authored-by: Claude Opus 4.8 <noreply@anthropic.com>

- Rewrite capture adapter onto real openadapt-capture 0.5.1 API (was dead code)
  ([#55](https://github.com/OpenAdaptAI/openadapt-flow/pull/55),
  [`b362f5b`](https://github.com/OpenAdaptAI/openadapt-flow/commit/b362f5b454f1e68599f2e16872f10d3e80f8d8a7))

The adapter targeted a capture.db/events flat schema that no longer exists, so convert_capture()
  FileNotFerror'd on any real recording and the tests passed only against a hand-rolled LEGACY db.
  Rewrite onto the real public API: CaptureSession.load(dir).actions(include_moves=False) over the
  real recording.db (SQLAlchemy recording/action_event tables), frames via get_frame_at, mapping to
  flow's meta.json + events.jsonl compile input. openadapt-capture added as an optional 'capture'
  extra; imported lazily with a clear error when absent.

Test now builds a REAL recording.db via capture's own SQLAlchemy models and exercises the real
  load/actions path (event mapping, coordinate scaling, meta contract, frame selection, compile
  round-trip, and the reject cases).

HONEST LIMITATION: openadapt-capture screenshots the display AT IMPORT time, so the whole real-path
  test module SKIPS on headless CI / no display (it runs and asserts fully where a display is
  present). Fixing capture's import-time screenshot (a separate change in that repo) would let this
  run in CI.

Claude-Session: https://claude.ai/code/session_01CKrVJJy5jWVCkXAqgUqtqZ

Co-authored-by: Claude Opus 4.8 <noreply@anthropic.com>

- Robust benchmark chart fonts (bundled DejaVu; cosmetic chart never fails the suite)
  ([#57](https://github.com/OpenAdaptAI/openadapt-flow/pull/57),
  [`fc04ffb`](https://github.com/OpenAdaptAI/openadapt-flow/commit/fc04ffb2bc8c8f9672e5caa7371a0194b8d689f4))

Multiple runs hit ValueError: Failed to find font DejaVu Sans from matplotlib findfont in the
  chart-rendering benchmark tests — a font-cache fragility (fresh venvs / concurrent runs corrupting
  the shared matplotlib cache). A cosmetic chart must never fail the benchmark suite.

- New chart_fonts.configure_bundled_font(): register matplotlib's OWN wheel-bundled DejaVuSans.ttf
  (get_data_path()/fonts/ttf) and set it as the sans-serif family, so findfont resolves against the
  registered font and cannot miss the fragile on-disk cache. Primary fix — charts still render. -
  chart_fonts.safe_render(): wrap the chart-render step so any matplotlib/font failure is caught +
  logged and the chart is skipped, WITHOUT failing the benchmark (results.json is the product; the
  PNG is nice-to-have). - Wired into all chart-rendering modules
  (desktop/openemr/hybrid/run/dom_arm); tests assert numeric results are intact and a simulated
  findfont failure no longer reds the suite.

No benchmark measurement logic, thresholds, or numbers changed. chart_fonts tests 4/4; benchmark
  tests 63 pass.

Claude-Session: https://claude.ai/code/session_01CKrVJJy5jWVCkXAqgUqtqZ

Co-authored-by: Claude Opus 4.8 <noreply@anthropic.com>

### Continuous Integration

- Auto-release via Python Semantic Release (match the other openadapt repos)
  ([#58](https://github.com/OpenAdaptAI/openadapt-flow/pull/58),
  [`d0ad0f2`](https://github.com/OpenAdaptAI/openadapt-flow/commit/d0ad0f2c2c818f61b8f7ecbf96685a8260a1f919))

Replace the manual tag-triggered publish with Conventional-Commit-driven auto-release on merge to
  main, matching openadapt-capture/ml/privacy. Semantic Release reads commit subjects since the last
  tag (feat -> minor, fix/perf -> patch, BREAKING -> major), bumps pyproject, tags, and publishes to
  PyPI (OIDC, environment 'pypi') + GitHub Releases only when it actually cuts a release.

Prerequisites (repo settings): secrets.ADMIN_TOKEN (push the release commit/tag past branch
  protection, same as the other repos) and the existing PyPI Trusted Publishing config. Skips its
  own 'chore: release' commit to avoid a loop.

NOTE: the first auto-release will compute the version from the 11 feat + 4 fix

commits since v0.3.0 -> v0.4.0 (feat bumps minor); it is NOT a v0.3.1 patch.

Claude-Session: https://claude.ai/code/session_01CKrVJJy5jWVCkXAqgUqtqZ

Co-authored-by: Claude Opus 4.8 <noreply@anthropic.com>

### Documentation

- Openadapt ecosystem integration roadmap (types/capture/verifier + sequencing)
  ([#40](https://github.com/OpenAdaptAI/openadapt-flow/pull/40),
  [`10db215`](https://github.com/OpenAdaptAI/openadapt-flow/commit/10db215bf18be1ae8ef25bce2b63985f74488d3d))

Decision-grade architecture memo analyzing how openadapt-flow should adopt the rest of the
  openadapt-* ecosystem, focusing on the packages no other workstream is covering: openadapt-types,
  openadapt-capture, openadapt-verifier.

Recommendations: types = interop shim (keep ir.py as source of truth),

capture = adopt via public API + fix the stale adapter, verifier = leave standalone (it is a
  clinical RWE validator, not GUI verification).

Claude-Session: https://claude.ai/code/session_01CKrVJJy5jWVCkXAqgUqtqZ

Co-authored-by: Claude Opus 4.8 <noreply@anthropic.com>

- Rewrite safety gallery copy for clarity (plain-language explainer + labels)
  ([#51](https://github.com/OpenAdaptAI/openadapt-flow/pull/51),
  [`8df9ba8`](https://github.com/OpenAdaptAI/openadapt-flow/commit/8df9ba82dd883a8ed240feaf211c960742e016a1))

A viewer opened the wrong-patient safety gallery and could not tell what they were looking at: the
  framing assumed prior knowledge of the wrong-patient problem and leaned on internal jargon
  (ABSTAIN/MISMATCH/ VERIFIED, "coverage", "byte-identically", "RECORDED target / LIVE row", "Nth
  reopening"). This rewrites the WORDS only — no case, verdict, or datum changes.

- Add a plain-language explainer above the cards: the stakes (writing to the wrong patient's chart),
  why it's hard (OCR reads pixels, and O/0 or l/1 look-alikes collapse two different patients to the
  same text), the defense (halt instead of guess), and a "How to read each card" legend. - Relabel
  columns per case kind ("The patient you recorded" vs "A DIFFERENT patient — same-looking row" /
  "The same patient at replay" / "A different patient"). - Add a per-card difference callout naming
  (and visually marking) the one look-alike character that separates the two patients. - Translate
  the verdicts: ABSTAIN -> "HALTED — refuses to click", MISMATCH -> "STOPPED — caught the mismatch",
  VERIFIED -> "PROCEEDS — safe to act", keeping the technical term small in parentheses; move
  "coverage" out of the headline into a tooltip. - Rename the page to "Wrong-Patient Safety" with a
  plain subtitle, retitle the honest-limits panel "What this does NOT protect against", and plain-
  language the OCR/collapse labels.

Regenerated gallery.html + results.json: headline unchanged at 5/5 dangerous look-alikes refused and
  2/2 controls correct; the only results.json delta is the glyph_class display labels.

Claude-Session: https://claude.ai/code/session_01CKrVJJy5jWVCkXAqgUqtqZ

Co-authored-by: Claude Opus 4.8 <noreply@anthropic.com>

### Features

- Compiled-vs-agent comparison artifact (generated from real benchmark results)
  ([#50](https://github.com/OpenAdaptAI/openadapt-flow/pull/50),
  [`32f25a9`](https://github.com/OpenAdaptAI/openadapt-flow/commit/32f25a9ff04c7c727ef8c17d4c2c2d4673b354a4))

Add benchmark/comparison_artifact: a deterministic generator that reads the two real head-to-head
  results.json files and emits a self-contained, theme-aware comparison.html packaging the core
  wedge — compiled replay is model-free, ~$0/run, and faster, at parity success on real EMR tasks.

- Leads with the real third-party result (OpenEMR public demo, 20 compiled vs 10 claude-sonnet-5
  agent, both 100%; $0 vs $0.5522/run; 39.2s vs 70.4s p50), then the CI-reproducible MockMed anchor
  (100 vs 20, both 100%). - Charts are inline SVG (axis, gridlines, emphasized zero endpoint,
  tabular-nums) — no screenshots, no external assets. - Shares the wrong-patient safety gallery's
  design vocabulary (CSS token palette, dual light/dark theming, card + honest-limits patterns). -
  Honest, up-front caveats: small N, field-not-CI result on a shared public demo, list-price cost
  with hard caps, one conservative OCR check on both arms, and that this is a cost/latency result at
  parity success — not a general capability claim. - Every figure comes from results.json; the only
  prose-sourced figure (one-time demonstration cost) is labelled with its source. Zero model calls,
  zero network. Reproduce: python -m benchmark.comparison_artifact.generate

Also emits comparison.json (extracted figures + provenance), a README, and a browser-free test
  asserting the loaded figures equal the source files and the emitted HTML carries the real numbers.

Claude-Session: https://claude.ai/code/session_01CKrVJJy5jWVCkXAqgUqtqZ

Co-authored-by: Claude Opus 4.8 <noreply@anthropic.com>

- Drift-oracle postcondition rescue via on-prem VLM (opt-in, veto-safe)
  ([#37](https://github.com/OpenAdaptAI/openadapt-flow/pull/37),
  [`de44c52`](https://github.com/OpenAdaptAI/openadapt-flow/commit/de44c52acebc0f8c2e29cd74e56dea3cee0d9419))

Wires the third remote-VLM client (RemoteStateVerifier) into the replayer, completing the appliance
  integration. Opt-in via OPENADAPT_FLOW_VLM_URL; unset (default) => postconditions behave exactly
  as before, zero model calls.

When a deterministic postcondition FALSE-FAILS under render drift, the VLM state-verifier gets one
  confirmation pass -- the same heal-under-drift the resolution ladder already does for click
  targets. Veto-safe by construction: - only text_present / region_stable are eligible (never
  structural or text_absent, where a failure is real, not drift); - only a confident "yes" rescues;
  "no" / "uncertain" / any outage keep the halt; a verifier exception is a fail-safe halt; - every
  call and rescue is recorded on StepResult (postcondition_drift_rescues, drift_oracle_calls) and
  counted in report.model_calls -- a rescued run is not a zero-model run, and the rescue is
  auditable, never silent.

docs/LIMITS.md gains two honest entries: the 2026-07-12 fault-model finding (postconditions read the
  screen, not the system of record -> 5/7 transactional write faults are silent; needs
  effect-verification + at-most-once, neither generic in vision-only replay) and the drift-oracle's
  own residual-risk caveat (a screen-reading VLM can rescue a genuine failure that ambiguously reads
  as success -- a little safety traded for availability, which is why it is opt-in and audited).

7 new tests pin the veto-safe behavior; 96 pass across replayer + remote-vlm.

Claude-Session: https://claude.ai/code/session_01CKrVJJy5jWVCkXAqgUqtqZ

Co-authored-by: Claude Opus 4.8 <noreply@anthropic.com>

- Freerdp backend adapter (L1 over RDP; transport-abstracted, mock-tested + gated live smoke)
  ([#44](https://github.com/OpenAdaptAI/openadapt-flow/pull/44),
  [`af7a78a`](https://github.com/OpenAdaptAI/openadapt-flow/commit/af7a78ae8e41483205a68c1a506b9ec56a1b7e96))

* feat: FreeRDP backend adapter (L1 over RDP; transport-abstracted, mock-tested + gated live smoke)

The L1/Retinology wedge reaches a legacy ophthalmology EMR over RDP, read pixel-only (no
  accessibility tree) — exactly the vision-only substrate the runtime was built for, so RDP is an
  adapter, not a rewrite.

- RDPTransport: minimal, honest protocol (connect/disconnect/framebuffer/ pointer/key/wheel) so the
  adapter is CI-testable without a live RDP server and the RDP library stays swappable. -
  FreeRDPBackend: implements the flow Backend protocol on top of an RDPTransport (screenshot->PNG,
  click down/up + double, per-char type_text, chord-decomposed press, wheel scroll). Pixel-only, so
  it deliberately omits the optional IdentityBackend/StructuralBackend capabilities; identity falls
  back to the OCR name+DOB tier. - AardwolfTransport: real transport over the pure-Python async
  aardwolf RDP client, bridged to sync via a dedicated event-loop thread; lazily imported behind a
  new optional `rdp` extra (importing the module never imports aardwolf). - FakeRDPTransport +
  tests/test_rdp_backend.py: mirrors the windows_backend mock pattern; 31 mock tests incl. a full
  record->compile->replay conformance run (zero compiler/replayer changes) + a gated live smoke
  test. - docs: L1_INTEGRATION gap #1 -> spike landed; docs/backends/RDP.md.

Spike boundary: adapter shape proven; real-clinic-EMR validation over RDP (OCR/grounding quality
  under RDP compression, where the VLM fallback matters) is pending a screen recording.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>

Claude-Session: https://claude.ai/code/session_01CKrVJJy5jWVCkXAqgUqtqZ

* fix: RDP backend robustness — stuck-modifier, connect-leak, scroll (weak-spot review) (#46)

Adversarial review of the FreeRDP backend (the FakeRDPTransport never raises, so these
  real-connection failure paths had zero coverage; the live smoke has since proven a real frame
  decodes, so these are real):

- HIGH stuck modifier: press()/type_text() released keys only on the success path, so a transport
  exception mid-chord left e.g. Ctrl held -> every later input became Ctrl+click/type (a
  wrong-action generator). Keys are now released in a finally (each queued for release before its
  down is sent); best-effort _release_keys so one failing release never strands the rest. -
  connect-failure teardown: a half-open session / event-loop thread no longer leaks when connect
  raises after the session opens. - horizontal scroll + wheel position reconciled between the real
  transport and the fake so a test can't pass on a capability the real path lacks. - racy
  live-smoke: polls for the first non-blank frame (first RDP frame is blank before the desktop
  paints — confirmed on the live Parallels run).

New RaisingRDPTransport fixture + tests cover the stuck-modifier and half-open teardown paths. 37
  passed, 5 gated/skipped.

Co-authored-by: Claude Opus 4.8 <noreply@anthropic.com>

---------

- Integrate openadapt-privacy (PHI scrubbing on persist/log paths; VLM-crop boundary policy)
  ([#42](https://github.com/OpenAdaptAI/openadapt-flow/pull/42),
  [`b5b55d2`](https://github.com/OpenAdaptAI/openadapt-flow/commit/b5b55d22d918ec39d311bd650d873153547b0376))

Wire the optional Presidio-backed openadapt-privacy dependency into every place openadapt-flow
  persists or logs PHI, and document the one path it cannot scrub (the on-prem VLM identity crop).

- openadapt_flow/privacy.py: single choke point over openadapt-privacy. OPENADAPT_FLOW_SCRUB=auto
  (default; scrub when installed, else plaintext) / on (fail closed) / off. Opt-in image redaction
  via OPENADAPT_FLOW_SCRUB_IMAGES. Lazy singleton so importing never pulls in Presidio/spaCy;
  test-injectable. - report.py: scrub every free-text field rendered into the shareable REPORT.md
  (workflow name, params, intents, errors, unarmed reasons). - replayer.py: scrub the drift-oracle
  console log; route persisted step frames through opt-in image redaction. - heal.py: route
  persisted heal crop/frame through opt-in image redaction. - vlm_service/backends.py: MLX
  no-retention fix -- private 0700 scratch dir, files chmod 0600, deleted in a finally (pre-fix
  leaked PHI crops on any inference error). Production VLLM backend sends base64 inline (no disk). -
  pyproject.toml: add optional `privacy` extra. - docs/PRIVACY.md (PHI touchpoint map + what is
  scrubbed vs boundary/gap), ON_PREM_VLM.md (PHI data-flow boundary: on-prem-only + no-retention),
  LIMITS.md, README.md. - tests: text scrubbing on REPORT.md, extra is optional, on fails closed,
  off no-op, opt-in image redaction, MLX no-retention (incl. on inference error).

Claude-Session: https://claude.ai/code/session_01CKrVJJy5jWVCkXAqgUqtqZ

Co-authored-by: Claude Opus 4.8 <noreply@anthropic.com>

- Interactive run player (scrub a real compiled run: replay, heal, halt)
  ([#54](https://github.com/OpenAdaptAI/openadapt-flow/pull/54),
  [`430d1b5`](https://github.com/OpenAdaptAI/openadapt-flow/commit/430d1b56ec14c7a87aa001aff4729dba39d408f7))

* feat: interactive run player (scrub a real compiled run: replay, heal, halt)

A self-contained HTML player generated from THREE real compiled runs (model-free, model_calls=0):
  baseline replay (all template rung), a theme-drift run that HEALS (8 anchors re-resolve via
  geometry/OCR, each heal shown as a diff), and a run that HALTS loudly on a blocking modal. The
  viewer scrubs/plays the real captured frames with a per-step overlay: which resolution rung fired,
  the identity verdict, whether it healed, and the postcondition result. Plain-language 'what you're
  watching' framing; stop-on-halt is a first-class moment. Reuses the safety gallery's design
  vocabulary. Reproducible via python -m benchmark.run_player.generate.

7 tests. player_data.json carries the step metadata without base64.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>

Claude-Session: https://claude.ai/code/session_01CKrVJJy5jWVCkXAqgUqtqZ

* feat: include the real modal-HALT run artifacts for the run player

Adds the committed model-free HALT run (report.json + 22 before/after frames) that the interactive
  run player reuses and the test asserts on. Without these, `python -m
  benchmark.run_player.generate` re-runs the replay and the player test skips in CI; committing them
  makes the run reproducible and the test exercised (10/11 steps ok, halts at step_010,
  model_calls=0).

---------

Co-authored-by: Claude Opus 4.8 <noreply@anthropic.com>

- Ocr text-anchor grounding rung (adopt openadapt-grounding; VLM grounder -> fallback)
  ([#52](https://github.com/OpenAdaptAI/openadapt-flow/pull/52),
  [`9ad96e4`](https://github.com/OpenAdaptAI/openadapt-flow/commit/9ad96e463b06b36b81403f657ef77e8c6726241a))

Benchmark #41 measured openadapt-grounding's OCR text-anchoring at 88-100% vs the bespoke remote-VLM
  grounder's 0/6 on dense lists, but the runtime still used the weak one. Adopt the validated one as
  the PRIMARY grounding rung; the remote-VLM grounder demotes to a fallback for text-less surfaces.

- OCRAnchorGrounder (runtime/grounder.py) implements the Grounder protocol via openadapt-grounding's
  ElementLocator; lazily imported behind a new optional 'grounding' extra; returns None (abstain, no
  proposal) if unavailable or nothing located -> SAFE (ladder halts, never mis-clicks). - Wired as
  preferred grounder at the construction site (__main__). - SAFETY INVARIANT UNCHANGED: the grounder
  only PROPOSES; the deterministic identity band still disposes before every click. identity.py /
  resolver / replayer core untouched.

14 tests: protocol conformance, dense-list resolution through the wired path, safe-abstain when
  unavailable/not-found, and that a grounder-proposed point still faces verify_target_identity.

Claude-Session: https://claude.ai/code/session_01CKrVJJy5jWVCkXAqgUqtqZ

Co-authored-by: Claude Opus 4.8 <noreply@anthropic.com>

- Openadapt-types interop shim (adopt canonical action vocabulary at the boundary)
  ([#43](https://github.com/OpenAdaptAI/openadapt-flow/pull/43),
  [`cf127f9`](https://github.com/OpenAdaptAI/openadapt-flow/commit/cf127f991544ae9cad9bd1803a7cb1503ed27300))

* feat: openadapt-types interop shim (adopt canonical action vocabulary at the boundary)

Add an optional, additive boundary layer (openadapt_flow.interop.types) that translates flow's
  compiler IR to/from the ecosystem's canonical openadapt-types schema, without touching ir.py (the
  internal source of truth, FROZEN in DESIGN.md). This is roadmap integration #1 from
  docs/ECOSYSTEM_INTEGRATION.md: "adopt the words, keep the core."

- step_to_action / result_to_action_result: flow Step/StepResult -> canonical Action/ActionResult
  (shared vocabulary only; compiler-only Anchor, Postcondition, Resolution, IdentityCheck, risk are
  dropped, never smuggled into Action.raw). - action_to_step: partial reverse hydrate for
  ingest/round-trip; refuses out-of-vocabulary ActionTypes (right_click, drag, ...) rather than
  dropping. - ACTION_KIND_TO_ACTION_TYPE: the trivial, exhaustive enum map (flow's 6 ActionKinds are
  a byte-identical subset of the 21-member ActionType). - openadapt-types imported lazily inside
  functions; new optional extra openadapt-flow[interop] keeps the dependency off the core/replay hot
  path. - tests/test_interop_types.py: exhaustive enum map, Step round-trip, result mapping, reverse
  hydrate + rejection, and an import-light subprocess assert.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>

Claude-Session: https://claude.ai/code/session_01CKrVJJy5jWVCkXAqgUqtqZ

* fix: interop shim round-trip corruption (param placeholder + timeout error_type)

Adversarial review findings on the boundary shim: - action_to_step turned a parameterized-TYPE
  Action.text of '{name}' into LITERAL text with param lost, so an evals->flow round-trip would type
  the characters '{name}' verbatim instead of substituting. Reverse now restores '{name}' ->
  Step.param (TYPE only; a genuine literal on a non-TYPE action is left alone). - error_type never
  emitted 'timeout' (mapped to execution_error) though the canonical vocabulary has it; now detected
  from the error string. Documented that identity-mismatch and postcondition-miss both coarsely map
  to state_mismatch (consumers must read 'error' to separate them).

3 tests added; 22 pass.

---------

Co-authored-by: Claude Opus 4.8 <noreply@anthropic.com>

- Remote on-prem VLM inference service + fail-safe clients
  ([#34](https://github.com/OpenAdaptAI/openadapt-flow/pull/34),
  [`c70e2cc`](https://github.com/OpenAdaptAI/openadapt-flow/commit/c70e2ccd5c2e70e2f201503af9bed4c7a84323c6))

Add a shared GPU-box VLM appliance that GPU-less runners call over the LAN, so the runtime stays
  GPU-free and patient data never leaves the building.

Service (openadapt_flow/services/vlm_service/): - FastAPI app: POST /v1/identity/compare (veto-only
  same/different, reusing the validated PR #28 prompt+parser), /v1/ground, /v1/verify_state, GET
  /health, /ready. Shared bearer-token auth; unauthenticated /v1/* -> 401. - Micro-batching: async
  queue drains a short window (default 15ms, max batch 8) so concurrent runners share one GPU;
  documented tunables. - Pluggable backends: StubBackend (CI/safe default), MLXBackend
  (Apple-Silicon dev, Qwen3-VL-4B-4bit), VLLMBackend (prod OpenAI-compatible vLLM/SGLang). - serve
  CLI: openadapt-flow-vlm-service / python -m ....

Clients (openadapt_flow/runtime/remote_vlm.py): - RemoteGrounder implements the Grounder protocol
  (drop-in for NullGrounder). - RemoteIdentityVLM (verify/mismatch/abstain) for the identity
  ladder's VLM tier. - RemoteStateVerifier (yes/no/uncertain). - FAIL-SAFE:
  unreachable/timeout/auth/5xx/malformed -> SAFE outcome (identity ABSTAIN, grounder None, state
  uncertain) so the runner halts, never wrong-acts.

Docs: docs/deployment/ON_PREM_VLM.md (topology, contract, sizing, fail-safe,

auth, latency budget, post-#33 integration). Tests: service contract/auth/batching + client
  fail-safe (all 6 failure modes)

+ optional live MLX test (skipped without model). 599 passed, 1 skipped.

Claude-Session: https://claude.ai/code/session_01CKrVJJy5jWVCkXAqgUqtqZ

Co-authored-by: Claude Opus 4.8 <noreply@anthropic.com>

- Transactional fault-model study (idempotency / partial-save / duplicate-write for consequential
  writes) ([#35](https://github.com/OpenAdaptAI/openadapt-flow/pull/35),
  [`c3c1e79`](https://github.com/OpenAdaptAI/openadapt-flow/commit/c3c1e79b077ab69dfeae3c841a271884cbb0ac59))

Prior rigor studies (cosmetic_drift, dense_surface, reliability) stress UI drift. This one stresses
  the persistence boundary — the failure classes that matter for consequential writes, which UI
  drift never touches.

MockMed is a client-side SPA (the UI is the source of truth), so there is nothing to verify a write
  against. This adds a real persistence boundary behind a flag-gated hook, mirroring the existing
  ?drift= mechanism:

- openadapt_flow/mockmed/fault_server.py: serves the same static app plus a small JSON API with an
  in-process DB (independent ground truth via GET /api/db) and seven transactional fault modes. -
  mockmed/static/app.js: a ?fault=<mode> hook routes the Save write through the backend. Inert with
  no ?fault query — the normal benchmark is byte-for-byte unaffected (pinned by
  test_off_state_pinned). - benchmark/fault_model/{faults.py,run.py}: the fault registry, the
  ground-truth outcome taxonomy (SUCCESS / SAFE-HALT / WRONG-ACTION / FALSE-ABORT /
  UNDETECTED-FAILURE), and the runner. Zero model calls. - benchmark/fault_model/FAULT_MODEL.md +
  results.json: 90 replays, deterministic. - tests/e2e/test_fault_model.py: taxonomy unit tests +
  e2e per-fault outcomes.

Finding: the vision postcondition system (text_present / region_stable /

url_changed) reads the screen, not the record system. It silently mishandles 5 of 7 transactional
  fault classes — reporting a clean success while ground truth is wrong: partial save (note
  dropped), optimistic-UI reject (phantom success over an empty DB), duplicate submission and
  double-click (two rows written), and stale overwrite (a concurrent change lost). Session expiry is
  safe-halt (it also breaks the screen); timeout-after-write is a conservative false-abort whose
  natural retry double-writes. The idempotency-key control neutralizes the duplicate/double-click
  hazard, motivating at-most-once writes and effect-verification postconditions as first-class
  write-step requirements.

Claude-Session: https://claude.ai/code/session_01CKrVJJy5jWVCkXAqgUqtqZ

Co-authored-by: Claude Opus 4.8 <noreply@anthropic.com>

- Wire remote-VLM appliance into the replay ladder (opt-in, fail-safe)
  ([#36](https://github.com/OpenAdaptAI/openadapt-flow/pull/36),
  [`de69c42`](https://github.com/OpenAdaptAI/openadapt-flow/commit/de69c427658f1dd6e95fcfd5b605e4ebb5cb9d88))

Brings #34's on-prem VLM clients online in the production path. Opt-in via env; unset (default) =>
  the run stays fully local and model-free, unchanged.

- RemoteIdentityVLM.same_or_different(): adapt IdentityVerdict onto the VLM tier's veto-only
  interface. VERIFY -> "same" (fail-to-veto); MISMATCH and ABSTAIN (the default on any
  uncertainty/timeout/outage) -> "different" (halt). The tier can only veto; an appliance outage
  means more halts, never a wrong click. - appliance_from_env(): build the runner-side handles from
  OPENADAPT_FLOW_VLM_URL / _TOKEN / _TIMEOUT, or None when unconfigured. - replay CLI: pass
  appliance.grounder + appliance.identity_vlm into the Replayer (both already-injectable slots,
  default None). RemoteGrounder only proposes; the deterministic identity band still disposes before
  any click. - 15 tests: the safety-critical verdict->veto mapping, the wired tier through the real
  verify_vlm_identity (outage+different -> halt, same -> abstain, non-confusable -> gated off),
  grounder fail-safe, and the env factory.

Drift-oracle (RemoteStateVerifier) left as a follow-up: it needs a postcondition-failure hook in the
  replayer.

Claude-Session: https://claude.ai/code/session_01CKrVJJy5jWVCkXAqgUqtqZ

Co-authored-by: Claude Opus 4.8 <noreply@anthropic.com>

- Wrong-patient safety gallery (generated visual proof of the identity defense + honest limits)
  ([#49](https://github.com/OpenAdaptAI/openadapt-flow/pull/49),
  [`cfd547b`](https://github.com/OpenAdaptAI/openadapt-flow/commit/cfd547b335a9b1eda99e7d841901bdb67d66b06e))

* fix: P0 wrong-patient — separator-formatted collapsible MRNs bypassed the glyph gate

An adversarial review found a wrong-patient false-accept (10th reopening). `_is_identifier_shaped`
  ended with `token.isalnum()`, which is False for any separator-bearing token — so a dash-formatted
  MRN (`MG-4408`) was never flagged as glyph-confusable. A same-NAME/same-DOB homonym differing only
  by an O/0 glyph in a DASHED MRN (`MG-4408` vs `MG-44O8` -> OCR-collapse to a byte-identical band)
  returned VERIFIED instead of abstaining, on pure-pixel/OCR substrates. Confirmed via the public
  `verify_target_identity` entry point. It also contradicted LIMITS.md's "ANY identifier-position
  token" claim.

Fix: strip intra-identifier separators before the run test, excluding only

date-shaped tokens FIRST (new `_is_date_like`, range-validated on the homoglyph-canonical form) so a
  DOB never becomes a gated identifier and over-halts every band. Separator MRNs are now gated;
  dates are not.

- Safety invariant intact: test_zero_false_accepts_* still pass (0 false-accept). - Cost is the SAFE
  direction: v1 over-halt 48.15%->60.42%, v2 ->47.33% (the corpus carries dashed collapsible MRNs);
  budgets widened + documented. - New tests/test_identity_separator_glyph_10th.py pins the class
  (was untested). - LIMITS.md 'ANY identifier-position token' now covers separator MRNs.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>

Claude-Session: https://claude.ai/code/session_01CKrVJJy5jWVCkXAqgUqtqZ

* feat: wrong-patient safety gallery (generated proof of the identity defense + honest limits)

A self-contained HTML gallery generated from REAL renders + the REAL production identity check
  (verify_target_identity, zero model calls). For each adversarial class it shows the two patient
  rows as rendered, the OCR output (proving a true collapse reads BYTE-IDENTICALLY), and the system
  verdict.

Headline: 5/5 dangerous cases correctly refused (O/0, l/1, purely-numeric,

separator-formatted, same-name sibling -> abstain/mismatch), 2/2 controls correct (clean MRN
  verifies -> no over-halt; different patient mismatches). Includes an honest 'what still slips'
  panel (unarmed steps, transactional phantom-success, pure-pixel over-halt) from docs/LIMITS.md.
  Reproducible via python -m benchmark.safety_gallery.generate; results.json is machine-checkable; a
  test guards that every dangerous case stays SAFE.

---------

Co-authored-by: Claude Opus 4.8 <noreply@anthropic.com>

### Testing

- Property-based fuzzing of resolver, postcondition, and healing invariants
  ([#53](https://github.com/OpenAdaptAI/openadapt-flow/pull/53),
  [`3486959`](https://github.com/OpenAdaptAI/openadapt-flow/commit/3486959d2fa8108f5465962a6d9d4233336092e8))

Extends the identity fuzzer (#48) to the other safety/correctness-bearing paths that had no property
  coverage. Encodes invariants a reviewer would agree MUST hold and searches the space with
  Hypothesis:

- resolver: a resolved point is always within frame bounds; an irreversible step never accepts a
  below-OCR/grounder low-confidence match (risk gate holds under fuzzed confidences/rungs);
  all-abstain -> None (halt), never a fabricated point. - postconditions: text_absent never passes
  when text is present and vice versa; evaluation is deterministic; region_stable vacuous only as
  documented. - healing: a heal produces a valid bundle diff (healed bundle still loads/re-resolves)
  and is idempotent (no further heal on re-run).

Result: NO counterexample across the searches -- the invariants hold (unlike

identity, where the fuzzer found the separator P0). Pure test additions; no runtime change.

Claude-Session: https://claude.ai/code/session_01CKrVJJy5jWVCkXAqgUqtqZ

Co-authored-by: Claude Opus 4.8 <noreply@anthropic.com>

- Property-based fuzzing of the identity gate (never-false-accept invariant)
  ([#48](https://github.com/OpenAdaptAI/openadapt-flow/pull/48),
  [`af56e0d`](https://github.com/OpenAdaptAI/openadapt-flow/commit/af56e0d6b6a6869f2a446b3361bf21e28cbeb863))

The FROZEN adversary corpora enumerate hand-picked shapes, which is why the separator-formatted
  collapsible-MRN P0 (10th reopening, #45) slipped: no dashed/slashed MRN was in the corpus. This
  adds Hypothesis strategies that SEARCH the space of collapsible-identifier homonyms instead of
  enumerating cases, and assert the invariant that must always hold: a same-NAME/same-DOB pair
  differing only by an OCR-collapsible identifier glyph must NEVER return `verified`.

Properties (tests/test_identity_fuzz.py): - byte-identical collapse (O/0, l/1/I): recorded == live
  band must ABSTAIN; - raw-different sibling (o0, l1i, s5, z2, b8, g9): confusion-only identifier
  match must MISMATCH via the suspect mechanism; - no-over-halt: a clean identifier + matching
  name/DOB must VERIFY; - date exclusion: a fuzzed DOB (a separator-bearing token) must not gate.

Inputs are constructed to be collapsible BY CONSTRUCTION and are NOT gated on the code's own
  `_is_glyph_vulnerable_identifier` predicate — the separator P0 was exactly a shape that predicate
  wrongly excluded, so a misclassified shape surfaces as a shrunk counterexample. ~1400 examples
  across the safety properties; deadline off, timeout-marked heavy. hypothesis added to the [dev]
  extra.

Validation: run against pre-#45 identity code the byte-identical property

falsifies and shrinks to `O-000` (a dashed O/0-collapsible MRN that false-accepts); on the fixed
  code no counterexample is found.

Claude-Session: https://claude.ai/code/session_01CKrVJJy5jWVCkXAqgUqtqZ

Co-authored-by: Claude Opus 4.8 <noreply@anthropic.com>

## v0.3.0 (2026-07-12)


### Bug Fixes

- Ocr verify-path conservative on any collapsible-glyph identifier incl. numeric MRNs (9th
  reopening) ([#33](https://github.com/OpenAdaptAI/openadapt-flow/pull/33),
  [`a6cc373`](https://github.com/OpenAdaptAI/openadapt-flow/commit/a6cc373334f2cfef9daacb8b23226b27a86b1ee8))

* feat: dense sibling-surface false-abort/false-accept study

Measures the identity band matcher on a DENSE, sibling-heavy record list -- the surface where a
  wrong-patient write does damage and which the headline ROC (synthetic corpora + clean OpenEMR
  banners; FA 0.000% / FAbort 26.17%) never covered.

The harness renders a dense clinical record table (HTML), screenshots it, runs the repo's own OCR
  (RapidOCR), and extracts the identity band EXACTLY as the compiler records it (context_from_lines,
  clicked-cell crop excluded) and the replayer verifies it (band_region + lines_near_point +
  2x-upscale retry), then runs verify_target_identity. No Anthropic calls. Seeded collision classes
  (near-name, Nguyen variants, Jr/Sr, same-surname, same-name-diff-DOB, MRN transposition, l/1 and
  O/0 identifier confusions) sit one row from their target; siblings are realistic distinct
  patients.

Findings (5 seeds, 360 armed clicks/direction): - per-click false abort 6.11% -- LOWER than
  synthetic 26.17% (the rendered surface OCRs cleanly); all 22 are the O/0 identifier-glyph
  instability. - per-click false accept 7.22% (26/360) -- NOT zero. Every one is an OCR
  glyph-collapse: OCR reads the target's C0X3834 (digit 0) and the sibling's COX3834 (letter O) as
  the SAME string, so the bands are raw-identical and the string-level identifier-suspect rule
  (which fires only on a confusion-VISIBLE mismatch) never triggers. This falsifies the ROC/LIMITS
  "0.000% false accept" claim for the v3 identifier-collision class ON THE REAL SURFACE: the
  synthetic corpus injects the confusion as a text edit that keeps both variants distinct, the exact
  condition the suspect rule was built for. - adjacent-row bleed present in 40.8% of raw 64px bands
  but the lines_near_point row filter absorbs ALL of it (0 survived, region-aware); removing the
  filter would flip 77/360 true-row verdicts.

Deliverables: openadapt_flow/validation/dense_surface.py (fixture +

faithful record/replay harness), benchmark/dense_surface/DENSE_SURFACE.md (+ dense_surface.json and
  audit screenshots), tests/test_dense_surface.py. Full suite green (591 passed).

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>

Claude-Session: https://claude.ai/code/session_01CKrVJJy5jWVCkXAqgUqtqZ

* fix: halt on OCR glyph-collapse of ambiguous MRN identifiers (6th wrong-patient reopening)

The dense-surface study (PR #24) found a wrong-patient false-accept BELOW the matcher, at the OCR
  layer: two same-name patients whose MRNs differ by one letter/digit near-homoglyph (target C0X3834
  digit-ZERO vs sibling COX3834 letter-O) are read by RapidOCR as the SAME string before band_match
  sees them. The recorded and observed bands are RAW-IDENTICAL, the match is a clean raw match, and
  the string-level identifier-suspect rule (which needs two DIFFERENT strings) never fires. Measured
  7.22% false accept (26/360) on the dense surface, 60% on the O/0 class.

Fix (identity.py): a RAW match to an IDENTIFIER-LIKE recorded token (mixes letters and digits)
  carrying an O/0 or l/1/I near-homoglyph is not evidence of same-identity, so it is charged to a
  zero budget (GLYPH_AMBIGUOUS_ID_CHARS_CAP) and identity HALTS. Only the O/0 and l/1/I
  near-homoglyph classes qualify (the only ASCII letter/digit pairs that render as near-identical
  glyphs), so a numeric MRN with a stable alpha prefix (MG483726) and identity carried by a clean
  name+DOB still verify. Option A: no corroboration escape (name+DOB are shared between same-name
  siblings, so only the MRN discriminates). Flag is set only for a single recorded identifier token,
  not for OCR-joins of a name and an adjacent numeric field.

Re-measured with the study's own harness (same seeds, same operating point): false accept 7.22%
  (26/360) -> 0.00% (0/360); false abort 6.11% -> 18.89%, entirely confined to the two
  ambiguous-identifier classes (id_confusion_l1 0->100%, id_confusion_O0 55->70%; all seven other
  classes unchanged at 0%). Post-fix false abort stays below the synthetic 26.17%.

Docs corrected honestly: DENSE_SURFACE.md gains a before/after section; IDENTITY_ROC.md and
  LIMITS.md now scope the 0.000% false-accept claim to the synthetic corpora + probes, disclose the
  real-OCR finding and its number, and state the halt-based fix + its false-abort cost and the
  disclosed letter-side residual (with a glyph-disambiguating OCR pass noted as the complete future
  alternative). Pinned probe test added (TestBlocker6GlyphCollapse). Full suite green (596 passed).

* fix: rest identity on name+DOB, not a confusable identifier (7th wrong-patient reopening)

The 6th-reopening fix (#26) halted on a raw match to an identifier carrying a homoglyph LETTER
  (O/l/I). An adversarial review broke it on the DIGIT side: a real MRN is <alpha prefix><numeric
  body>, and when the confusable

glyph is digit-flanked RapidOCR reads the DIGIT form on BOTH a patient (AC50061) and a DIFFERENT
  same-name/DOB patient (AC5OO61, letter O) — both collapse to 'AC50061', NO homoglyph letter
  survives, the letter-only flag misses it, and the sibling verifies. Measured ~87% false accept on
  the digit-flanked shape through the real render->OCR->match pipeline. No string-layer flag can
  recover a distinction OCR destroyed at the pixel level, and flagging the digit side (any 0/1 in an
  MRN) would halt ~3 of 4 real MRNs.

The fix changes WHAT identity trusts. Identity is verified on the OCR-reliable, redundant NAME +
  DOB; a confusable-glyph identifier is CORROBORATION only (identity.py):

- band_match now tracks whether a DISCRIMINATIVE name carries the identity (a name-like >= 4-char
  token that is not a generic column word, matched raw or by name-confusion). A glyph-vulnerable
  identifier is detected on BOTH sides (O/0 and l/1/I). - A DIGIT-body glyph-vulnerable identifier
  is charged to the zero halt budget ONLY when no discriminative name carries the identity (the
  clicked NAME cell excluded, leaving DOB + MRN + generic columns) — closing the digit-flanked
  collapse where identity rests solely on the identifier. - A homoglyph LETTER stays a HARD halt
  (affirmative OCR ambiguity), so the 6th-reopening closure is preserved with NO regression.

Measured with the REAL dense harness (seeds 1-5). Original collision corpus: false accept 0/360
  (unchanged), per-click false abort 18.89% ->

45.00% — the rise is entirely the digit-side sole-discriminator halt in click_name (name excluded):
  click_action (name carries) stays 18.89%. The digit-flanked attack drops ~87% -> 43.8% (click_name
  half closes to 0).

DISCLOSED RESIDUAL (fundamental): a same-name/DOB DIFFERENT patient whose digit-body MRN collapses
  to the target's, WITH the name displayed and matching (click_action), is band-identical to a
  legitimate same-patient re-read and verifies. The two rows reach the matcher as the same bytes; no
  band-level rule can separate them. Closing it needs glyph-disambiguating / high-resolution OCR on
  identifier regions (roadmapped). This also means the over-halt is NOT reduced vs #26: softening
  the letter halt to recover availability would re-verify the same-name/DOB letter siblings the 6th
  reopening closed — an FA-vs-availability tension this surface cannot escape at the string layer.

Tests reconciled + pinned (tests/test_identity_out_of_corpus.py):
  test_plain_numeric_mrn_target_still_verifies (which enshrined the vulnerable no-name digit-MRN
  shape) reconciled to name+DOB-primary; new TestBlocker7NameDobPrimary pins digit-flanked
  different-name verify, sole-discriminator halt, clean name+DOB verify, letter-side hard halt, and
  the disclosed residual. Docs corrected honestly (LIMITS.md, IDENTITY_ROC.md, DENSE_SURFACE.md):
  the guarantee is name+DOB-discriminated identity; identity resting on a look-alike-character
  identifier ALONE halts; the complete upstream fix is roadmapped. Full suite green (591 +
  identity).

* feat: structured-text identity tier (DOM + UIA/AX) — the ladder foundation

Verifies a click target's identity against STRUCTURED TEXT where the backend exposes it — the DOM
  (PlaywrightBackend) or the native accessibility tree (WindowsBackend UIA) — instead of OCR'd
  characters, which collapse look-alike glyphs (O/0, l/1) and cannot distinguish two patients whose
  MRNs differ only by such a glyph (the 6th/7th wrong-patient reopenings, proven unclosable at the
  OCR string layer).

Adds an optional Backend capability `structured_text_at(point) -> str | None` (real characters from
  DOM/a11y, or None on pure-pixel substrates), captures the target's structured identity into the
  bundle at record time alongside the OCR band, and restructures the replay-time identity check as
  an EXTENSIBLE LADDER: tier 1 structured text (unambiguous, browser + most native desktop) → final
  tier the name+DOB-primary OCR fallback (#27) for pixel-only substrates. Clean seam left for the
  validated pixel-compare and VLM-veto tiers (PRs #29/#28). Docs (LIMITS/ROC/DENSE_SURFACE) state
  the honest substrate-complete picture.

* feat: integrated substrate-complete identity ladder (structured-text → pixel-compare → VLM-veto →
  OCR fallback)

Promote the two experimentally-validated identity probes into real ladder tiers on the
  structured-text foundation, so pre-click identity verification is substrate-complete and
  fail-safe:

- tier 2 PIXEL-COMPARE (verify_pixel_identity): localized max abs-diff of the recorded vs live
  identifier crop. On a stable render it separates the O/0 collapse pairs at AUC 1.0 (threshold
  ~0.049); it breaks under render drift, so it VERIFIES only when the render matches (a distance no
  different identifier can produce — structurally cannot false-accept), MISMATCHES only on a
  localized glyph change, and ABSTAINS under whole-crop drift. Free, no model. Validated in
  benchmark/pixel_identity (PR #29). - tier 3 LOCAL-VLM VETO (verify_vlm_identity +
  runtime.identity_vlm): a local open VLM (Qwen3-VL-4B via MLX, zero cloud calls), veto-only, gated
  on a glyph-confusable identifier and the cheaper tiers abstaining. 0% false-accept + 100%
  detection on the collapse surface. OPTIONAL and OFF by default (injected via
  Replayer(identity_vlm=...), like the grounder) — the default install needs no model. Validated in
  benchmark/vlm_identity (PR #28).

Ladder: structured text → pixel-compare → optional VLM veto → OCR name+DOB →

HALT. Every tier is fail-safe (unsure → abstain; nothing verifies → halt); a higher tier's verdict
  is final. Anchor gains identifier_crop/identifier_region for the pixel/VLM tiers;
  IdentityCheck.mode gains pixel/vlm.

Measured integrated on the dense O/0-collapse surface (openadapt_flow.validation.identity_ladder,
  artifacts in benchmark/identity_ladder): 0 false-accept across ALL substrate configs (the safety
  invariant), clean structured-text/name+DOB targets still verify. Per config over-halt: structured
  0%, pixel-stable 0%, pixel-drift+VLM ~47% (pixel abstains, VLM vetoes/over-halts on zoom/font),
  pixel-drift+VLM-off 100% (the disclosed OCR residual). True floor — a font rendering O/0 or l/1
  pixel-identical — not found among 14 common fonts.

Docs updated (LIMITS.md, IDENTITY_ROC.md, DENSE_SURFACE.md) to the final substrate-complete,
  fail-safe story with the VLM optional/on-prem.

* fix: OCR tier halts on collapsible-MRN homonyms; harness drives real replayer stack (8th
  reopening)

An adversarial review of PR #31 proved a LIVE, production-reachable wrong-patient VERIFY. Two
  DIFFERENT patients sharing NAME and DOB, differing only by a glyph-confusable MRN (recorded
  AC50061 vs live AC5OO61, letter O) OCR to a BYTE-IDENTICAL band. #27's name+DOB-primary rule let
  the matched name "carry" identity and suppressed the digit-side glyph budget -> status verified ->
  the wrong patient's chart was clicked (reproduced at coverage 1.0 through the real
  Replayer._verify_identity).

BLOCKER 1 (safety) — the honest correctness rule: - band_match no longer suppresses the
  glyph-confusable-identifier budget when a name/DOB matches; ANY raw-matched glyph-vulnerable
  identifier (O/0 or l/1/I, either side) charges it. - verify_target_identity/band_verdict is now
  three-way: a band whose name+DOB match but rests on a glyph-confusable identifier ABSTAINS (new
  IdentityCheck status) — OCR can neither certify SAME nor assert DIFFERENT — instead of a false
  verify or a dishonest mismatch. The ladder then HALTs (abstain + irreversible), recovered on real
  substrates by the structured-text tier. A different-NAME sibling still MISMATCHES; a clean
  name+DOB with a NON-confusable identifier still VERIFIES. - repro test
  (tests/test_identity_homonym_8th.py) renders both rows, real RapidOCR, drives the real replayer
  OCR tier; fails pre-fix (verified), passes post-fix (abstain).

MEASUREMENT FLAW — the harness now tests what ships: - validation/identity_ladder.py drives the REAL
  Replayer._verify_identity for every config (never a [pixel]-only subset that omitted the
  always-appended OCR tier). Adds the ocr_only_confusable config. On the pre-fix code the real stack
  surfaces 20 false-accepts (2 pixel-dilution + 18 OCR-homonym) the old harness reported as 0;
  post-fix 0 false-accept across ALL configs. - identity_roc.py _decide now includes the
  glyph-ambiguous-identifier budget (it omitted it, measuring a non-production matcher): v1
  false-abort 28.2% -> 48.2%, v2 -> 43.6%, false-accept still 0.000%.

BLOCKER 2 (pixel crop-scale sensitivity): - the absolute whole-crop threshold false-accepted a
  diluted one-glyph MRN on realistic wide cells. Added a scale-invariant localized-spike distance (a
  one-glyph change MISMATCHES at any crop width) and HARD-GATED the VERIFY path
  (PIXEL_VERIFY_ENABLED=False) — cross-render jitter defeats any safe same/different threshold —
  until fixed-size crop capture + a jitter-robust distance land. Not production-reachable today (no
  crop capture).

VLM veto-only: - verify_vlm_identity no longer returns verified on "same"; a "same" answer ABSTAINS
  (never grants a pass), "different" vetoes. Docs + tests updated.

Docs regenerated with TRUE numbers from the production stack: LIMITS.md, IDENTITY_ROC.md,
  DENSE_SURFACE.md (0 false accept, 71.11% false abort on the dense OCR surface),
  benchmark/identity_ladder. Full suite green (649).

* fix: OCR verify-path conservative on any collapsible-glyph identifier incl. numeric MRNs (9th
  reopening)

The 8th fix (#32) made the OCR identity tier ABSTAIN on a glyph-confusable identifier, but its
  predicate required a letter+digit MIX, so it only covered ALPHANUMERIC MRNs. A real MRN can be
  purely numeric, and a numeric MRN is just as glyph-collapsible: a recorded `100512` and a
  DIFFERENT same-name/same-DOB patient's `1OO512` (letter O's) OCR to the byte-identical `100512`,
  so the mix predicate never flagged `100512` and the homonym VERIFIED the wrong patient on the real
  `Replayer._verify_identity` stack (also `400761`/`4OO761`, `417063`/`4l7063`).

Make the rule structural and conservative by default:

- `_is_glyph_vulnerable_identifier` now flags ANY identifier-shaped token (new
  `_is_identifier_shaped`: a bare alphanumeric run >= 3 chars carrying a digit -- numeric,
  alphanumeric, or lowercase; a separator-bearing date and a digit-free name are excluded) that
  bears a confusable glyph {0,1,O,l,I}, on either side. The `letter AND digit` requirement is
  dropped. When uncertain whether a token is an identifier it is treated AS one (-> abstain), the
  safe over-halting direction. - Split identifiers are covered: the glyph flag is now a property of
  the recorded token charged on ANY match path (single/split/join) via a unified post-pass keyed on
  raw-match, so a confusable glyph in a numeric FRAGMENT of an OCR-split MRN still abstains. -
  Name/DOB never suppresses this: the OCR tier verifies same-identity only when NO
  identifier-position token bears a collapsible glyph.

Corpus + docs (measurement can't miss this again):

- Added purely-numeric and split-numeric homonyms to the dense_surface and identity_ladder collapse
  corpora (they were all alpha-prefixed, which hid the numeric hole). Re-measured every config on
  the REAL replayer stack: identity_ladder 0 false-accept across ALL configs (14 pairs incl. 5
  numeric); dense_surface 0 false-accept across all 12 classes (480 trials), over-halt 78.33% on the
  OCR path (honest higher cost), structured-text path 0 FA / 0 over-halt. Regenerated
  IDENTITY_ROC.md (FA 0.000% all corpora; false-abort 47.36% -> 48.31% as frozen-corpus numeric
  identifiers now abstain). - Removed the now-false "alphanumeric MRN/account token" scoping in
  LIMITS.md and IDENTITY_ROC.md; the rule is now ANY identifier token with a confusable glyph. Added
  9th-reopening notes.

Pinned real-stack tests (tests/test_identity_ocr_conservative_9th.py): numeric O/0 and l/1 homonyms
  across Arial/Times/Courier/Georgia/Verdana at 10-15px, the alphanumeric 8th-fix case (no
  regression), split identifiers, lowercase -- all HALT on the real render + RapidOCR +
  Replayer._verify_identity stack; a clean non-confusable MRN (RC79284) still VERIFIES; a
  different-name sibling still MISMATCHES. Full suite green.

* chore: bump to 0.3.0 — substrate-complete identity ladder + safety fixes

* fix: make identity CI env-independent (skimage dep, cross-platform font, browser reuse)

Three CI-only failures a clean/slower runner exposed but a dirty/fast local env masked:

- test_pixel_identity_probe: pixel_identity_probe.m_ssim/m_charcell import scikit-image, which was
  never declared. Add scikit-image to [dev] (the pixel tier is dev-only validation, hard-gated off
  in the shipped runtime). - test_blocker2_wide_cell_different_mrn: _mrn_cell hardcoded a macOS-only
  Arial.ttf path; on Linux CI it fell back to a degenerate bitmap font, so the ladder abstained
  (None) instead of MISMATCH. Add a cross-platform font resolver that uses matplotlib's bundled
  DejaVuSans (a dev dep, always present in CI). Verified DejaVu still yields MISMATCH on both the
  digit and O/0-homonym cases. - test_harness_zero_false_accept_all_configs: identity_ladder.run
  relaunched Chromium on every _render (~45 cold starts) and timed out >600s. Launch one shared
  browser and open a cheap page per render: 344s local. Add a 900s timeout margin on this one heavy
  browser+OCR integration test.

---------

Co-authored-by: Claude Opus 4.8 <noreply@anthropic.com>

### Features

- Compile-reliability study across diverse public web apps
  ([`efab8ef`](https://github.com/OpenAdaptAI/openadapt-flow/commit/efab8ef680581649cc3b023c7cbab3bc2bd9c289))

Broaden compile+replay testing from N=1-2 hand-controlled apps (MockMed, OpenEMR) to a corpus of 29
  diverse PUBLIC web apps (login forms, e-commerce browse/cart, multi-widget forms, todo/CRUD, dense
  tables, a Swagger console, native/date-picker/canvas widgets, and known-hard anti-bot/consent
  sites) across React, Vue, jQuery, Bootstrap, server-rendered, and static stacks.

Harness (openadapt_flow/benchmark/reliability.py) reuses the demo_driver record path: scripted
  Playwright flow -> Recorder -> compile_recording -> Replayer on the unchanged UI, once per app,
  with grounder=None (compiled replay + OCR only, ZERO Anthropic API calls). Each replay is scored
  against an arm-independent DOM/URL ground truth into success / safe_halt / wrong_action /
  false_halt / crash, plus a WHY taxonomy.

Result: compile 29/29 (100%, no per-app tuning); replay 17/29 verified

success, 10 safe_halt, 2 wrong_action, 0 crash. Dominant non-success is the pre-click identity gate
  halting SAFELY on text-dense web chrome (tuned for dense EMR tables, over-conservative for general
  web). Both wrong_actions are vacuous successes under an environment blocker (DDG html returns no
  results to headless; petstore EU consent overlay), not harmful wrong-writes. Every resolved step
  used the template rung (unchanged UI needs no lower rung).

Central limitation stated plainly: public no-auth apps only; the real enterprise/desktop/Citrix
  targets are behind auth walls and unrepresented.

Deliverables: harness + committed corpus manifest (corpus.json), results.json,

benchmark/reliability/RELIABILITY.md (full distribution, taxonomy, per-app table, verdict), and 30
  network-free harness tests. Full suite green (610).

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>

Claude-Session: https://claude.ai/code/session_01CKrVJJy5jWVCkXAqgUqtqZ

- Cosmetic-drift operating-envelope study
  ([`5fe69b9`](https://github.com/OpenAdaptAI/openadapt-flow/commit/5fe69b94081547bea47ed32767afc5c039cfd670))

Map the precise points at which zoom / DPI / font drift breaks compiled replay of the MockMed triage
  bundle, turning the unqualified "0% at 125% zoom" into a bounded, defensible spec.

Sweep one recorded+compiled bundle under cosmetic-ONLY perturbations (browser zoom 80-200%, DPI
  1-3x, font-size, font-family, and realistic pairs); the target is always present and semantically
  identical, so a correct run always saves to patient p1 and any other save is a wrong-action.

Findings (benchmark/cosmetic_drift/COSMETIC_DRIFT.md; 21 points): - Fails SAFE across the ENTIRE
  sweep: 0 wrong-actions, 0 crashes. - Scale drift (zoom != 100%, DPI > 1x, font-size != recorded)
  halts safe at step_000 on its region_stable postcondition - a hair-trigger at the first deviation,
  enforced by the postcondition gate, not the resolver. - Font-FAMILY substitution to a proportional
  face (Georgia, Times) is fully absorbed by the heal ladder (OCR + healing); monospace safe-halts
  at the pre-click identity gate. - Operating spec: deterministic replay holds only at the recorded
  render (100% zoom, 1x DPI, recorded font size); outside that it halts safely and never acts on the
  wrong target.

- benchmark/cosmetic_drift/sweep.py: the sweep harness (no model calls). -
  benchmark/cosmetic_drift/{results.json,results.md}: committed matrix. -
  tests/e2e/test_cosmetic_drift.py + validation_utils.replay_cosmetic: pin the envelope and the
  no-wrong-action safety property. - Cross-references from docs/validation/VALIDATION.md (P2
  cosmetic-drift).

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>

Claude-Session: https://claude.ai/code/session_01CKrVJJy5jWVCkXAqgUqtqZ

## v0.2.0 (2026-07-11)


### Bug Fixes

- Crashed agent runs' spend reaches the row and the cost ceiling
  ([`cbec44c`](https://github.com/OpenAdaptAI/openadapt-flow/commit/cbec44c2c2f355d5cc04a72ea9267e2d6ea68ac6))

A mid-run exception (API 429/500/529 after N paid calls, or a screenshot failure) previously
  propagated past the local cost accumulators, so the recorded row carried cost_usd 0.0 and the $8
  agent-arm ceiling never saw the real spend. run_agent now records usage into a UsageLedger the
  moment each response arrives; _agent_run passes its own ledger and builds error rows from it, so
  partial spend always reaches the row, the aggregates, and the ceiling check. Unit-tested with a
  scripted client that raises mid-run, both directly and end-to-end through the orchestrator
  ceiling.

Also from review: - state the bounded overshoot of the per-run/total caps (checked after each API
  call, so at most one call's marginal cost past the bound) in code docs and the generated
  BENCHMARK.md - preflight: one retry on transient-looking errors before declaring the key dead
  (auth/billing failures still fail fast); billing fingerprint moved to agent_baseline and shared -
  note_for: assert the index is inside the note list instead of silently wrapping (which would break
  pairwise distinctness); orchestrator validates n_compiled/n_agent up front - exact-decimal cache
  pricing constants (no binary-float noise in results.json), trailing newline on results.json -
  'tokens (in/out)' table label renamed 'uncached in / out' - rows.jsonl documented as append-only
  across invocations - committed benchmark artifacts regenerated from the existing rows (no re-run;
  generated_at kept)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>

Claude-Session: https://claude.ai/code/session_01CKrVJJy5jWVCkXAqgUqtqZ

- Extend the suspect budget to identifiers — close the 5th wrong-patient reopening
  ([`da713c5`](https://github.com/OpenAdaptAI/openadapt-flow/commit/da713c5b5295b3d8c26e2808dd9fdd6099e4b849))

The round-3 suspect budget guarded NAME tokens only: _name_plausible is False for any token
  containing a digit, so the rule was OFF for MRNs/account numbers while the confusion
  canonicalization (l/1, O/0, S/5, Z/2, B/8, g/9) still applied to them. A DIFFERENT patient's
  alphanumeric identifier one letter/digit-confusable char apart ('A01234' vs 'AO1234') silently
  VERIFIED, defeating MRN-based disambiguation of same-name patients (verified in param mode too).

Fix: _suspicious_pair now also returns True when the RECORDED token

contains a digit (an identifier matched only across a confusion). A confusion-only match on such a
  token is charged to the zero suspect budget -> abort. Chosen design is option A of the review (no
  corroboration escape): a confusion-differing identifier aborts even when name and DOB raw-match,
  so two same-name patients distinguished ONLY by an OCR-confusable identifier char never verify.
  Option B (allow if name+DOB corroborate) was rejected because two real patients can share a name
  and DOB, so the MRN is the sole unique key and B would re-admit exactly the Doe John wrong-patient
  case.

Scoping on the RECORDED token is what keeps name-with-digit-noise verifying while
  identifier-with-digit aborting: the recording carries the ground truth of the token's type.
  'Belford' -> 'Be1ford' is clean (recorded all-alpha = name); 'A01234' -> 'AO1234' aborts (recorded
  has a digit = identifier). All-DIGIT differences (748291 vs 748292) are not confusion-equivalent
  and mismatch via coverage/contradiction as before.

Measured (frozen v1+v2+v3, regression-netted): 0 false accepts across all three including v3's 300
  id_letter_digit_collision pairs and the 18 out-of-corpus probes. Availability cost, honest:
  true-row identifier OCR noise now aborts (indistinguishable at band level) — v2
  digit_confusion_true_row 0% -> 48.7%, v1 overall 21.2% -> 28.2% (budgets updated). Residual
  verify: short 1-2 char all-alpha codes confused with a digit (recorded token has no digit; under
  the 3-char name floor). Full suite green including e2e (43/43).

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>

Claude-Session: https://claude.ai/code/session_01CKrVJJy5jWVCkXAqgUqtqZ

- Harden identity matching and typed-input verification (review blockers)
  ([`31b2223`](https://github.com/OpenAdaptAI/openadapt-flow/commit/31b222312c16366bfcdbec9dd756c4d6867b4104))

Adversarial review of the wrong-actions fix ran four probes that all falsified the initial identity
  matcher; each is reproduced and closed:

B1: char-coverage let shared row text buy a wrong entity a pass

('Ann Wu <same procedure>' verified at 0.89 against a 'Jane Li' band) and generic bands armed false
  confidence ('Active High 7' at 0.91). The matcher is now token-wise and order-insensitive
  (verbatim / contiguous-containment / 0.7-similarity tiers) and requires BOTH >= 0.8 coverage AND
  no contiguous uncovered run over 4 squashed chars — a wrong name is a contiguous mismatch, so both
  probes now fail while true rows, OCR-jittered rows, and token-permuted bands (the live OpenEMR
  modal-band false abort at 0.66 under order-sensitive scoring) verify. MIN_CONTEXT_CHARS 8 -> 12:
  too-generic bands are no longer recorded and yield 'unreadable', never 'verified', at runtime.

B2/P1a: an embedded param demo value (MIN_PARAM_CHARS was 3, 'High') switched to a mode that ignored
  the band, verifying a wrong patient at 1.0, and any row containing the run's value passed. Param
  mode now substitutes the run's value into the recorded band and verifies the WHOLE substituted
  band; MIN_PARAM_CHARS raised to 4. Disclosed cost: entity rows whose non-param text varies (search
  results carry the surname) now halt on the correct row (LIMITS.md).

P1b: the 64px band spanned 2-3 dense-table rows; compile, heal, and

verification now restrict band lines to the click/resolved point's OWN text row (lines_near_point),
  so a one-row-off resolution cannot verify on text bleed from the true row.

P1c: risk was inert (never assigned). compile_recording gains

risk_overrides plumbing (opt-in, validated, e2e-tested through the refusal branch); docs state
  plainly that risk is never auto-assigned.

P2a/P2b: typed-input verification accepted ANY >=4px change, so a dialog over the field
  false-verified while keystrokes fell elsewhere, and the retry could destroy pre-existing content.
  OCR-able values must now be READ back (diff-only acceptance reserved for the masked no-new-text
  shape); the refocus/select-all retry fires only when nothing changed, otherwise the run halts
  without retyping. Characterization flip: the native-date garbage replay now safe-halts
  (value-transforming widgets false-abort, disclosed) instead of faithfully rewriting garbage.

P2d: the identity gate also covers anchored TYPE focusing clicks.

tests/test_identity.py pins the four reviewer probes verbatim plus the modal-band permutation,
  boundary, clamping, and substitution edges; new replayer tests pin one-row-off, param residue,
  dialog-over-field (unit + chaos e2e), masked acceptance, and the anchored-TYPE gate. Full suite:
  293 passed.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>

Claude-Session: https://claude.ai/code/session_01CKrVJJy5jWVCkXAqgUqtqZ

- Harden volatility classifier against reviewer-verified evasions
  ([`9cd009c`](https://github.com/OpenAdaptAI/openadapt-flow/commit/9cd009c5aceaea400825b547c616f77fc8de8b51))

Review of the postcondition-mining fix verified that the numeric-only DATE_RE/CLOCK_RE let whole
  classes of volatile text classify as stable. All evasions fixed and pinned as unit tests in both
  directions:

- Month-name dates ('Jul 8, 2026', '08 Jul 2026', 'July 2026', 'Updated Jul 8', 'Wednesday July 8')
  feed the same near/far split as numeric dates; a month-day with no year recurs annually and is
  always volatile; a month-name DOB ('Jan 1, 1980') is kept as identity data. Concrete risk fixed:
  OpenEMR's post-login calendar header ('July 2026') would false-halt every replay the next month. -
  Relative-time phrases ('3 min ago', '2 hours ago', 'just now') and standalone day-words
  ('Yesterday'); embedded day-words in stable chrome ("Today's Appointments") are kept. - Counts and
  pagination ('56 total entries', '1 to 1 of 1', 'Page 2 of 9' — reclassified from stable:
  pagination position is navigation state, not identity), whitespace-optional for OCR-squashed forms
  ('Showing1to1of1entries(filteredfrom56totalentries)'). - Parenthesized badge counters ('Inbox
  (2)') via strip-and-test: if removing the number leaves the classification unchanged the counter
  is volatile decoration; the label alone stays minable. - European dot-clocks ('Last updated
  18.38') — unambiguous forms only, so 'v2.0', 'v2.10 changelog' and 'Version 2.10 release notes'
  are pinned stable. ':01'-class guarantees unchanged.

heal._recontext now passes reference_date=date.today() so a healed anchor's refreshed band keeps
  DOB-class far-date lines instead of dropping every date-bearing line (unit-tested).

Docs (LIMITS.md, VALIDATION.md): scope the parameter-leakage lint claim exactly (text postconditions
  + landmark OCR text only; REGION_STABLE templates can embed rendered parameter pixels — false-halt
  direction); disclose the fuzzy-match weakness on digit-differing lines ('0 to 0 of 0 entries'
  scores 0.95 against the recorded entries banner — fixed upstream by rejecting the banner as
  'count' at compile time, matcher not redesigned); add known-remaining: long-line OCR-segmentation
  fragility, structural-check transient-None passes, NEW_TAB_OPENED false-halt on named-window
  reuse, no persistence coverage on the recording's final step.

Suite: 330 green (288 unit + 42 e2e), zero model calls.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>

Claude-Session: https://claude.ai/code/session_01CKrVJJy5jWVCkXAqgUqtqZ

- Hybrid fallback spend accounting, subsample-mix disclosure, review nits
  ([`1dbea8c`](https://github.com/OpenAdaptAI/openadapt-flow/commit/1dbea8c27f7841f3e91f7a4dc4d0326024d64c4c))

From adversarial review of the hybrid benchmark (PR #14), applied after merging the updated base
  (which brings the UsageLedger F1 fix):

- Exception-path spend undercount (same class as the base's F1): a fallback agent that crashed
  mid-run recorded $0 to the SpendLedger and a zero-cost row. _hybrid_run now passes a UsageLedger
  to run_agent, so pre-crash paid calls land on the row (cost/api_calls/token fields) and count
  against the shared ceiling. Unit test with a scripted mid-run crash asserts both. - BENCHMARK.md
  discloses the B/C subsample's drift mix: 3/8 = 37.5% drift vs the 20-slot schedule's 30% — a small
  cost bias in the hybrid's favor (B mean $0.23770 measured vs ~$0.23530 reweighted to the schedule
  mix; cost-per-run ratio 8.2x vs 8.1x); conclusions unchanged. Computed by the generator from the
  rows, and the committed artifacts regenerated from the existing results.json (no re-run;
  exact-decimal cache pricing and trailing newline picked up in the same regeneration). -
  test_arms_see_identical_conditions_per_slot now asserts on the URLs each fake arm actually
  received (captured in the run helpers), not only the orchestrator-stamped condition labels
  (circular). - _failed_result_index returns None when a failed report has no failing step result;
  the caller no longer blames the last COMPLETED step or undercounts completed_steps by one in the
  handoff prompt. Unit-tested (helper edges + caller behavior).

README test count corrected to this branch's actual collected count (248). Full suite: 248 passed.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>

Claude-Session: https://claude.ai/code/session_01CKrVJJy5jWVCkXAqgUqtqZ

- Identity matcher redesign — close the four out-of-corpus review blockers
  ([`82b21da`](https://github.com/OpenAdaptAI/openadapt-flow/commit/82b21da5d26bc65292b7e1abe1949c641fb29d83))

All 13 reviewer probes (committed first in tests/test_identity_out_of_corpus.py, failing) now pass;
  corpus v2 was frozen in the preceding commit, before this change was evaluated on it.

Four new decision budgets, all zero-tolerance at the operating point:

- SUSPECT chars (Blocker 1): a name-plausible token matched ONLY by a letter-letter confusion
  equivalence (Neil/Nell i-l, Clay/Day cl-d, Marnie/Mamie rn-m) is indistinguishable from a real
  sibling — the honest outcome is an abort for BOTH readings, corroborating identical MRN/DOB
  notwithstanding (the probes pin exactly that). Digit/symbol confusions ('Phi1', '5ample') stay
  clean: names contain no digits, so no collision with a different name is possible. This ports the
  spirit of param mode's raw longest_run check (which already rejected Neil->Nell) into context
  mode. - Short-token replacement (Blocker 2), COUNT-based: a replaced 1-2 char alphabetic token
  (middle initial J->K, SEX column M->F, 2-char names Al->Bo) is contradiction. Multiset accounting,
  because a replaced initial can duplicate the sex letter and look 'explained' per-pair. -
  Unexplained observed name-shaped tokens (Blocker 3): context mode gains the observed-side budget
  param mode always had — appended middle names, two-row OCR merges, and message/cc rows that merely
  MENTION the recorded patient all refuse; lowercase adjacent-row bleed stays exempt (the legitimate
  spurious class, 0% false aborts on v2). - Absent name-like token (Major 4): a fully absent 4+ char
  alphabetic token refuses even inside the generic run cap — identity must not verify with its
  identity token never read. Trailing-numerics dropout keeps the old tolerance (class-weighted, not
  blanket). The old pin test_pure_absence_boundary_at_run_cap is FLIPPED accordingly.

The replayer now extracts the LIVE band exactly as the compiler extracted the recorded band
  (target's own crop excluded at the resolved point, volatile lines dropped against the replay
  date): the previous asymmetry meant the label and live clock cells appeared as observed-side
  extras, which is what made an observed-superset budget impossible.

Measured (frozen corpora, regression-netted in tests/test_identity_corpus_rates.py): 0 false accepts
  across v1 (2200 wrong-entity pairs), v2 (1590 wrong-entity + 200 indistinguishable), and the 13
  probes; v1 false aborts 21.2% (up from 10.7% — the availability bill of closing the blockers,
  per-class breakdown in the regenerated IDENTITY_ROC.md), v2 legitimate-noise classes 0.0%;
  indistinguishable class 200/200 abort. Letter-letter jitter on the true row now aborts (pinned;
  disclosed) — the flipped twin of the Neil/Nell fix. Full suite green including e2e (43/43 live
  record-compile-replay).

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>

Claude-Session: https://claude.ai/code/session_01CKrVJJy5jWVCkXAqgUqtqZ

- Identity-verified click targets and verified typed input
  ([`cbfcfef`](https://github.com/OpenAdaptAI/openadapt-flow/commit/cbfcfefff5a40fc0eb833fb1d0369eac8353a969))

Close the six wrong-action modes (five silent) found by the adversarial validation suite. Two root
  causes, two mechanisms:

1. Pre-click identity check (runtime/identity.py). The compiler records each click target's context
  band — full-width OCR text on the target's row, excluding the target's own crop (labels stay
  mutable/healable) and timestamp lines (volatile) — as Anchor.context_text. Before every click the
  replayer re-reads the band around the RESOLVED point and requires lenient squashed-text coverage
  >= 0.8 (contiguous runs >= 3 chars); measured: true row ~1.0, look-alike row sharing all non-name
  columns ~0.70. When a parameter's demo value is embedded in the recorded band (parameterized
  target, e.g. the patient row) the check re-anchors on the RUN's value instead. Mismatch: safe-halt
  before the click, with expected/observed band text in the error. Unreadable band (2x-upscale OCR
  retry first): reversible steps proceed flagged in the report; irreversible steps refuse. Heals
  refresh the context from the live frame. The 8421d51 rule is untouched: parameterized values are
  still never baked into compiled postconditions — this is a pre-action check against runtime
  values.

2. Typed-input verification (Replayer._verify_typed_input). After every TYPE action, screenshot-diff
  of the field region (around the focusing click; full frame for keyboard-moved focus) plus lenient
  OCR for the typed value (2x retry; masked fields rely on the diff). On failure: one
  refocus-and-retype retry (re-click, select-all so a false-negative is replaced rather than
  duplicated, retype), then safe-halt.

Before -> after across the suite (characterization tests flipped to pin the fixed behavior): -
  drift=lookalike: silent save to wrong patient -> safe-halt before click - drift=missing: silent
  save to neighbour -> safe-halt before click - drift=grow: silent save to imposter row -> safe-halt
  (or verified save to the CORRECT patient where the global rung wins) - chaos delete-row: silent
  save to slid-in row -> safe-halt before click - chaos steal-focus: silent EMPTY-note save ->
  recovered; correct note - sort-reorder: wrong click then halt -> halt with NO click

Wrong-actions after fix: 0. False-abort cost: none measured — 30/30 clean + 3/3 theme-drift local
  benchmark compiled-arm runs; full e2e matrix (baseline/params/viewports/heal showcases/CLI) green;
  OpenEMR live regression (1 record + 3 paced replays, fake patients, $0): 18/18 identity
  evaluations verified at 0.95-1.00 on real dense EMR rows, 9/9 typed inputs verified — the replays
  later halted on the pre-existing, documented ':01' postcondition-mining defect (out of scope,
  disclosed in VALIDATION.md).

Suite: 237 passed (14 new unit tests for the identity gate and typed-input

verification). VALIDATION.md carries before/after columns; LIMITS.md moves the fixed modes to
  safe-halts and adds a known-remaining section.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>

Claude-Session: https://claude.ai/code/session_01CKrVJJy5jWVCkXAqgUqtqZ

- Masked typed-input acceptance must survive dots OCRing as glyph noise
  ([`e8df60c`](https://github.com/OpenAdaptAI/openadapt-flow/commit/e8df60c2a9523ef9815c5e8129ea324789c02cc8))

CI (Linux) regression from the typed-input hardening: on the GitHub runner the password field's dots
  OCR not as nothing (as on macOS) but as punctuation runs / glyph noise, so the raw squashed-text
  length comparison read the masked rendering as 'new readable text' and false-halted every login
  TYPE step (all chaos e2e tests and the CLI smoke failed at step_003).

The masked-acceptance metric is now the count of confidently readable ALPHANUMERIC characters (lines
  >= 0.6 OCR confidence): dot glyphs and low-confidence noise are excluded whatever the platform
  renderer produces, and alnum counts are invariant to OCR re-segmentation between frames. A dialog
  over the field still adds confident words and still halts (pinned). New unit test pins the
  dots-as-punctuation shape.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>

Claude-Session: https://claude.ai/code/session_01CKrVJJy5jWVCkXAqgUqtqZ

- Masked-dot misreads can be CONFIDENT homogeneous digit runs
  ([`a93b64d`](https://github.com/OpenAdaptAI/openadapt-flow/commit/a93b64d16f07dce0662c71cc2b74f7c47519f170))

The previous hotfix excluded punctuation and low-confidence noise, but the actual Linux-renderer
  artifact (recovered from the CI run's uploaded step frames and reproduced locally on those exact
  PNGs) is a CONFIDENT alphanumeric misread: 17 password bullets OCR as '0000000000006' at 0.81
  confidence when the field region is cropped. The readable-text metric now also excludes
  homogeneous glyph runs (>= 4 alnum chars dominated >= 66% by one repeated character) — no real
  dialog sentence is homogeneous, so the dialog-over-field probes still halt (pinned). Verified
  against the actual Linux CI frames: readable 31 -> 31, masked-accept.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>

Claude-Session: https://claude.ai/code/session_01CKrVJJy5jWVCkXAqgUqtqZ

- Never assert a parameterized value's pixel rendering
  ([`8421d51`](https://github.com/OpenAdaptAI/openadapt-flow/commit/8421d51e26f2c354a12b28ac6c007b00ce429ac0))

A parameterized TYPE step's largest-changed-region is the typed value itself, so the diff-based
  REGION_STABLE postcondition baked the recorded example's glyphs into the bundle — replaying with a
  different value failed as semantic drift (observed on the OpenEMR spike, run 5: the only run whose
  note text differed enough in length to push the region phash past tolerance). Parameterized TYPE
  steps now get no REGION_STABLE at all, completing the existing rule that parameterized values are
  never asserted in any form.

Also adds scripts/openemr_demo.py, the record/compile/replay driver for the OpenEMR public-demo
  showcase (fake demo patients only; not shipped in the package).

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>

- Ocr-segmentation-tolerant TEXT_PRESENT postconditions
  ([`10296cd`](https://github.com/OpenAdaptAI/openadapt-flow/commit/10296cd4f5927fc253cc7119143015e915d2a000))

Root cause of the TestMoveDrift CI failure: the step_010 save landed and the 'Encounter saved —
  <note>' banner was plainly on screen (verified in the uploaded CI run artifacts), but rapidocr
  returned the banner as ONE box (prefix merged with the note) instead of two, and find_text's
  whole-line similarity against the short stable prefix scores ~0.46 < 0.8 — a deterministic false
  postcondition failure on a correct screen. Whether the engine merges or splits that line is
  pixel-noise dependent, hence the flake.

Presence must not depend on that segmentation coin flip: new vision.text_present passes when either
  a whole OCR line fuzzy-matches (find_text's criterion) or a contiguous run of >= min_ratio of the
  squashed target appears in the squashed concatenation of all lines, with a 2x-resolution retry
  when the raw frame misses (the known rapidocr dense-line dropout, same mitigation
  verify_note_saved uses). Scattered character coincidences still fail (the run must be contiguous),
  so the modal-drift screen — which shares the words Encounter/Save — still fails honestly, pinned
  by test. The replayer's text_present/text_absent postconditions now use it; verified against the
  actual failing and passing CI frames.

verify.py's private _upscale_png moved to vision.ocr.upscale_png and is shared. README test count
  192 -> 204 (actual collected count).

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>

Claude-Session: https://claude.ai/code/session_01CKrVJJy5jWVCkXAqgUqtqZ

- Re-run desktop Phase-2 identity cells on post-#16 matcher, correct stale-code finding
  ([`4178b0c`](https://github.com/OpenAdaptAI/openadapt-flow/commit/4178b0c450f5fedb4e1b199fb26d5fb12e0089b7))

The branch was originally cut from a stale local main that predated the identity-matcher fixes
  (#16/#17/#19), so the compiled arm ran against the pre-#16 matcher and recorded 3 sibling
  wrong-actions — a stale-code artifact. Rebased onto current main and re-ran the identity-sensitive
  cells: the compiled arm now safe-halts both the near-lexical sibling (Sorenson/Sorensen) and the
  decoy, 3/3 each — 0 identity wrong-actions. The browser identity fixes transfer to
  desktop-rendered OCR text. Narrative corrected in BENCHMARK.md; non-identity findings (UIA-tree
  gap, DPI/theme defeat vision but halt-not-miswrite, full prlctl automation) unchanged.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>

Claude-Session: https://claude.ai/code/session_01CKrVJJy5jWVCkXAqgUqtqZ

- Rebuild identity band matcher — near-name siblings mismatched (3rd P0 reopening)
  ([`266d764`](https://github.com/OpenAdaptAI/openadapt-flow/commit/266d764e624fa15b6ba71f2eccb687c91bb4f7e9))

Confirmed vulnerability: band_match returned (coverage=1.0, residue=0) — VERIFIED — for sibling
  rows: 'Belford, Phil' vs 'Belford, Philip' (containment tier), the reverse, 'Smith, John' vs
  'Smith, Joan' (similarity tier, 0.75 >= 0.7), and 'Belford, Phil' vs 'Belford, Phillipa'. On the
  frozen adversarial corpus the legacy matcher's false-accept rate was 53.9% overall (DOB off-by-one
  99.1%, Jr/Sr 99.1%, single-letter edits 98.2%, transpositions 95.5%, prefix extensions 72.3%, MRN
  digit swaps 50.0%).

The rebuild: - token matching accepts ONLY OCR-equivalence — identical after canonicalizing real OCR
  confusion classes (l/1/i, O/0, 5/s, 2/z, 8/b, 9/g, rn/m, cl/d, vv/w) — plus full-consumption token
  splits/joins. The partial-containment and 0.7-similarity tiers are gone: both accepted semantic
  extensions of name tokens. - unmatched tokens split into ABSENCE (uncovered runs — OCR dropout,
  budgeted as before) and CONTRADICTION (near-miss similarity >= 0.62, semantic containment with
  alphabetic residue, replacement by an unexplained observed token, generational-suffix presence on
  one side) with its own zero budget.

Operating point picked from the ROC on the frozen corpus (sweep of contradiction_sim x coverage x
  run_cap x contradiction_cap, before/ after chart + tables committed under docs/validation/):
  coverage 0.8, run cap 4, contradiction_sim 0.62, contradiction cap 0 -> false accept 0.000% (was
  53.9%), false abort 10.69% (was 12.1%), NOT the Pareto-min false-abort corner: at cov 0.7/run 8
  the zero rests entirely on the contradiction rule (FA 60.8% if it is evaded) whereas at 0.8/4 the
  older budgets independently catch 79.5% — defense in depth over 2.7pp of availability concentrated
  in unreadable-name occlusion shapes.

All four sibling probes pinned as permanent mismatches; operating point pinned by boundary tests;
  corpus-wide zero-false-accept regression test added. Full unit suite green (364), true-row live
  shapes (OpenEMR modal permutation, OCR jitter, split/join) still verified.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>

Claude-Session: https://claude.ai/code/session_01CKrVJJy5jWVCkXAqgUqtqZ

- Stability-selected postcondition mining and parameter hygiene
  ([`2af9bd4`](https://github.com/OpenAdaptAI/openadapt-flow/commit/2af9bd4070657f0909d6f223442fe9c34dca833c))

Postcondition mining selected for novelty (longest new text) and its timestamp filter was
  simultaneously too weak and too strong: a fresh OpenEMR recording mined text_present ':01' (a
  clock-minute OCR fragment) that false-halted every later replay, while DOB-bearing identity
  banners were eaten because a date of birth looks like a date. Mining now selects for STABILITY:

- volatility classifier (openadapt_flow/volatility.py, shared with the identity-context extractor):
  rejects clock times (incl. bare ':NN' fragments), dates NEAR the recording date, digit-dominated
  counters and low-entropy noise; KEEPS dates far from the recording date (DOB-class identity data)
  - empirical stability: TEXT_PRESENT candidates must persist into the next step's before frame;
  self-mutating REGION_STABLE regions are dropped - ranking prefers alphabetic content with a
  proximity tiebreak toward the click target, not raw length - structural fallback postconditions
  (URL_CHANGED / TITLE_CHANGED / NEW_TAB_OPENED) for steps with no visual change, when the recorder
  captured the backend's structural observations (StructuralBackend on Playwright);
  honestly-unverified pass on backends that cannot observe - parameter hygiene: demo parameter
  values never become geometry landmarks, and a compile-time lint fails compilation loudly if a
  demonstrated parameter value leaks into any postcondition or landmark

Validation: 290 tests green (unit + full e2e matrix re-run whole; the

new-tab characterization test flipped to fixed behavior). Live OpenEMR (4 paced demo sessions, fake
  patients, $0): fresh bundle mines only chrome/header text — the ':01' class is gone, 0
  postcondition failures in 3/3 replays. Newly exposed pre-existing identity-band order fragility on
  dialog clicks is documented in docs/validation/VALIDATION.md and docs/LIMITS.md, not attempted
  here.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>

Claude-Session: https://claude.ai/code/session_01CKrVJJy5jWVCkXAqgUqtqZ

- Tolerate OCR line segmentation in the shared success check
  ([`45f5ba8`](https://github.com/OpenAdaptAI/openadapt-flow/commit/45f5ba8a141d361420d903c0700aac7403a37d97))

RapidOCR sometimes splits the saved banner into two lines (prefix + note), so whole-line find_text
  against the full banner string never matched on the light theme. Each check now accepts a small
  set of candidate line forms describing the same on-screen evidence; the banner prefix exists only
  after a save, so the criterion is unchanged.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>

### Documentation

- Add compiled-vs-agent benchmark to README
  ([`0c6ba1e`](https://github.com/OpenAdaptAI/openadapt-flow/commit/0c6ba1e24daa64694b3cbd1fa639caff8542c10c))

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>

- Add OpenEMR public-demo showcase artifacts and findings
  ([`3b45c47`](https://github.com/OpenAdaptAI/openadapt-flow/commit/3b45c47358478978632d52c21a00fa3946e2d7bb))

Record -> compile -> replay against the official OpenEMR demo (fake patients, resets daily): 18-step
  add-a-patient-note workflow, five fresh-browser replays, 4/5 end-to-end with per-run parameter
  substitution, fifth run failed safely at the icon-precision limit and was aborted by
  postconditions. FINDINGS.md covers what worked, the four capability fixes the live app forced,
  per-rung stats, and what is still rough (OCR coverage, open-loop scrolling, shared mutable demo
  state).

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>

- Adversarial validation failure-mode matrix and public LIMITS page
  ([`c4a8725`](https://github.com/OpenAdaptAI/openadapt-flow/commit/c4a872550c8e50ffd3c548dbec0d5ca5bc81fc81))

docs/validation/VALIDATION.md: every experiment across four tracks with outcome, mechanism, and
  evidence pointers — 6 wrong-actions found (5 silent wrong-state writes), 100% safe-halt rate
  elsewhere, zero crashes, zero model calls; failure modes ranked P0-P3. docs/LIMITS.md: the
  disclosed-limitations-first distillation — the dangerous list (silent failure modes), what
  safe-halts, parameterization depth, and what a demonstration cannot express.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>

Claude-Session: https://claude.ai/code/session_01CKrVJJy5jWVCkXAqgUqtqZ

- Hybrid benchmark results — verdict SUPPORTED, 20/20 hybrid at $0.029/success vs $0.238 agent-only
  ([`7526f30`](https://github.com/OpenAdaptAI/openadapt-flow/commit/7526f3089a81fb568387ab98695e4a9724264463))

Run of 2026-07-09 on the frozen 20-slot schedule (30% drift):

- compiled (A): 14/20 — 14/14 clean, 0/6 drift, all six drifted slots safe-halted deterministically
  at the probed steps, $0 - agent (B): 8/8, $0.2377/run - demo-conditioned agent (C): 8/8,
  $0.2489/run — the demo made the from-scratch agent neither cheaper nor more reliable here - hybrid
  (D): 20/20, 30% fallback rate, 6/6 fallbacks succeeded, mean 5.7 fallback actions / $0.0967 per
  fallback, $0.0290 per successful run — ~8x cheaper than agent-only on this mix

Break-even a/f = 2.5 (>1), so on these numbers the hybrid is cheaper at every drift rate; verdict
  scoped to DETECTED-halt drift only (silent wrong-action modes documented in PR #12 bypass the
  fallback — caveat carried prominently). Zero wrong-action events by the final-state identity
  check. Total paid spend $4.47 at list (≈$2.98 billed at the intro rate); no per-run or total cap
  tripped.

Also: computed C-vs-B demo-conditioning note in the renderer, gitignore

for benchmark/hybrid run artifacts (finals/, rows.jsonl).

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>

Claude-Session: https://claude.ai/code/session_01CKrVJJy5jWVCkXAqgUqtqZ

- Openemr closed-loop round 5/5 — update findings, runs, README
  ([`da38b76`](https://github.com/OpenAdaptAI/openadapt-flow/commit/da38b763d723596d01923f7641aea2d6c759947c))

Fresh 5-run round against the live OpenEMR public demo with closed-loop scrolling: 5/5 success,
  18/18 steps per run, zero model calls, on a demo

instance carrying more content growth than broke the open-loop run 5. Wall time rose ~29s -> ~37s
  per run (a ladder probe per scroll gesture); the out-of-band OCR note verification missed 2 runs
  whose notes are plainly visible in the saved final screens (known rapidocr limitation). README
  gains the OpenEMR result and the test count moves to 163.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>

- Openemr head-to-head results — 20/20 compiled, 10/10 agent, $5.52 under an $8 cap
  ([`e989756`](https://github.com/OpenAdaptAI/openadapt-flow/commit/e98975640428fdc8037430cdca81bcb68db54220))

Benchmark run 2026-07-08 against the official OpenEMR public demo (fake patients only) with the cost
  guardrails active:

- compiled replay: 20/20, 39.2s p50 / 41.0s p95, $0 - computer-use agent (claude-sonnet-5): 10/10,
  70.4s p50 / 82.6s p95, $0.5522/run, $5.52 total at list price (est. ~$3.68 billed at the intro
  rate) — no per-run or total cap tripped - cache tokens: 1,317,803 written / 563,928 read (30% of
  prompt tokens served from cache; reads plateau at the stable prefix once screenshot truncation
  begins, as disclosed in the methodology)

README now leads with the OpenEMR result; MockMed stays as the CI-reproducible methodology anchor.
  rows.jsonl and finals/ stay local (gitignored), matching the MockMed artifact convention.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>

Claude-Session: https://claude.ai/code/session_01CKrVJJy5jWVCkXAqgUqtqZ

- Platform-dependence caveat for drift=grow, /a/-vs-/b/ corrections
  ([`73ed47f`](https://github.com/OpenAdaptAI/openadapt-flow/commit/73ed47fe0e1319fbd08496fffd253be3baf165ca))

From adversarial review of the validation suite (PR #12):

- VALIDATION.md: the drift=grow wrong-patient outcome is platform/rendering-dependent (the pinned
  test accepts #patient/g1 OR #patient/p1; the pinned invariant is success-without-identity-
  verification). Headline restated as 4 silent modes pinned on every platform + 1 observed on the
  recording platform. - scripts/openemr_param_depth.py: module and cross_instance docstrings said
  /a/ while ALT_DEMO_URL is the /b/ instance — corrected. - VALIDATION.md: noted the /a/
  credential-rejection probe was ad hoc and is not reproducible from the committed script (which
  targets /b/). - test_perturbation.py: comment on the 4s-render-delay test's thin timing margin
  (flake watch; no behavior change).

Full suite green after merging feat/openemr-benchmark: 234 passed.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>

Claude-Session: https://claude.ai/code/session_01CKrVJJy5jWVCkXAqgUqtqZ

- Readme test count 293 -> 294 (masked-noise regression test added)
  ([`3e23881`](https://github.com/OpenAdaptAI/openadapt-flow/commit/3e23881785e1e56b14820e071dba7aae8b4ff311))

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>

Claude-Session: https://claude.ai/code/session_01CKrVJJy5jWVCkXAqgUqtqZ

- Readme with generated side-by-side demo GIF, badges, PyPI quickstart
  ([`7908a17`](https://github.com/OpenAdaptAI/openadapt-flow/commit/7908a17b00bfb228e6e2f9620cd912322169a981))

The demo GIF is composed from the real showcase run artifacts (baseline vs theme-drift replays of
  the same bundle) by scripts/make_demo_gif.py — no mockups; regenerable whenever the showcase is
  re-recorded.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>

- Scope the 0-wrong-actions claim; disclose hardened-check costs and live re-check
  ([`546156d`](https://github.com/OpenAdaptAI/openadapt-flow/commit/546156dfab9702c3e901984d58d037f6f79fdcd6))

VALIDATION.md and LIMITS.md now describe the 2026-07-09 hardened matcher/verifier exactly:
  order-insensitive token matching with the uncovered-residue cap (measured lookalike coverage
  ~0.67, not the initial matcher's 0.70), param-mode whole-substituted-band verification,
  row-refined bands, and the guarded typed-input retry. The '0 wrong-actions' claim is scoped to the
  pinned cases only, with the dangerous list (zero-postcondition steps, label-only/too-generic
  bands, unreadable bands on default-compiled steps) explicitly excluded.

Plainly stated per review: risk classification is opt-in via compile-time risk_overrides and never
  auto-assigned — in a default-compiled bundle an unreadable band on a chart-open click proceeds
  flagged and the wrong-patient-write tail remains reachable with a green report.

Live false-abort re-check of the tightened thresholds (public demo, fake patients, 4 sessions,
  /bin/zsh, 0 model calls): 6/6 identity evaluations verified at 1.0, 9/9 typed inputs verified,
  zero identity false-aborts; all 3 replays safe-halted at the same pre-existing identity-unrelated
  pencil-anchor/scroll-probe fragility (unarmed step, caught by postconditions, nothing written).

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>

Claude-Session: https://claude.ai/code/session_01CKrVJJy5jWVCkXAqgUqtqZ

- Tighten README
  ([`1b83206`](https://github.com/OpenAdaptAI/openadapt-flow/commit/1b83206cb54703144047bb671975cb789732c00e))

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>

- V1+v2 ROC, occlusion recount, realistic-exposure analysis, honest limits
  ([`defc564`](https://github.com/OpenAdaptAI/openadapt-flow/commit/defc56436dfe85115c66556075b488c66c1329e8))

Re-run the full ROC on corpora v1+v2 with three-label scoring (different_entity / same_entity /
  indistinguishable) and the six-budget decision sweep; re-picked operating point keeps 0.8/4/0 and
  adds suspect=0, unexplained-name=0, absent-name-cap=3:

- FA 0.000% across v1+v2 (3990 wrong-entity/indistinguishable pairs) — every number explicitly
  scoped to 'corpus v1+v2 plus the 13 out-of-corpus probes', with the operating-point-fit limitation
  stated plainly (freezing prevents tuning the corpus toward the matcher, not the thresholds toward
  the corpus; v1's zero was shown partially tautological one review ago and the same criticism
  applies to v2's). - FAbort v1 21.2% / v2 0.0%; indistinguishable class 200/200 abort. - The
  cheaper zero-FA Pareto corner (cov 0.85 / run 8 / absent-name off, FAbort 15.86%) is rejected with
  an empirical counter-example: its Major-4 protection is a band-length artifact (the same absent
  4-char name at coverage 0.915 verifies there; the absent-name cap refuses structurally). -
  Occlusion recount CORRECTS the earlier framing: 102/216 occlusion aborts at the shipped decision
  (107/224 at production) still had BOTH name tokens readable — trailing DOB/MRN loss, an
  availability cost, not the 'correct epistemic refusal' previously claimed. VALIDATION.md's
  original sentences carry strikethrough corrections. - Realistic-exposure analysis: the Blocker-1
  probes used identical MRNs (unrealistic); with differing readable IDs the absence/ contradiction
  budgets catch 180/180 without the suspect rule; the true residual exposure is
  name-as-only-discriminative-token bands, where the suspect rule is the only defense and covers
  only the frozen confusion table. - LIMITS.md restores and EXPANDS the honest disclosure this PR
  had deleted ('names within OCR-jitter distance verify'): the residual verify classes are now
  listed plainly (Ann Marie/Annmarie join, case/whitespace-only differences, 1-2 char letter-letter
  confusions, added short tokens), plus the permanent indistinguishable-class aborts and the ~21%
  compiled-only availability price.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>

Claude-Session: https://claude.ai/code/session_01CKrVJJy5jWVCkXAqgUqtqZ

- V1+v2+v3 ROC re-run, identifier-collision analysis, corrected disclosures
  ([`6db2fc4`](https://github.com/OpenAdaptAI/openadapt-flow/commit/6db2fc44b9af06a89fdc6d65e15babbc1b79bb8b))

Re-run the full ROC on corpora v1+v2+v3 (6900 pairs) after the identifier-suspect fix; operating
  point confirmed (same six/seven caps, FA 0.000% / FAbort 26.17% / indistinguishable-abort 100%
  across all three). New content:

- IDENTITY_ROC.md: a '5th reopening' section (the identifier letter/digit collision, chosen design A
  with the option-B rejection rationale, the RECORDED-token scoping, and the honest true-row
  availability cost); a v3 per-category table (id_letter_digit_collision legacy 100% -> 0.0%); the
  realistic-exposure table gains the v3 row (300/300 verify without the suspect rule, 0 with it) and
  CORRECTS the first review's 'ids differ -> 180/180 without the suspect rule' claim as
  name-collision-only (it did not cover the letter/DIGIT identifier case). Scope re-stated as
  v1+v2+v3 plus the 18-probe set. - LIMITS.md: the contradicted-list 'swapped MRN digits' is
  qualified to 'all-DIGIT' (the letter/digit case is now a suspect, not a contradiction); the
  suspect budget is described as name AND identifier; the residual-verify list gains the short
  all-alpha-code case and the true-row-identifier-noise availability cost; the halt price is updated
  21% -> 28%; every zero-claim re-scoped to v1+v2+v3 + 18 probes. - VALIDATION.md: the
  realistic-exposure bullet gets a second-review caveat, and a new 'SECOND review / 5th reopening'
  subsection records the hole, the fix, corpus v3, and the availability cost.

No claim left standing that the final matcher falsifies.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>

Claude-Session: https://claude.ai/code/session_01CKrVJJy5jWVCkXAqgUqtqZ

### Features

- Add name-filtered DOM arm; reframe verdict on spec underspecification
  ([`b38b011`](https://github.com/OpenAdaptAI/openadapt-flow/commit/b38b011c6be70387aee945656aa2800ab5169f76))

Adversarial review of PR #17: the 8/8 positional-DOM wrong-action finding is an artifact of pairing
  a position-phrased task spec with an identity-keyed judge. This adds the identity-honest steelman
  as a THIRD arm (name-filtered: get_by_role row=Jane Sample -> Open) run across the full schedule
  and every perturbation mode.

Three-arm result (all $0, deterministic): schedule 14/20 tie. On the perturbation matrix the
  name-filtered DOM completes CORRECTLY on lookalike/grow/sort (saved to #patient/p1), fails closed
  on missing, zero wrong actions — where the compiled arm safe-halted 8/8 with 0 heals, so on data
  drift the name-filtered arm finished the work the compiled arm declined. Positional DOM keeps its
  8/8 wrong-patient writes (now scoped to positional selectors, not 'Playwright').

Verdict reframed to the honest finding: (a) spec underspecification is the wrong-action vector; (b)
  positional selectors silently retarget on data drift; (c) name-filtered DOM is safe AND available
  on data drift where a stable DOM exists — the compiled arm's remaining browser-side edges are
  demo-derived identity (no spec authoring), heal-through of label drift (rename), fail-closed
  semantics, plus non-DOM substrates. Retracts the asymmetric variant dismissal (the compiled
  identity band embeds the same patient name). Commits the two disputed theme finals; gitignores the
  rest of benchmark/dom/{finals,rows.jsonl}; Reproduce command now matches --n-per-perturbation 2.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>

Claude-Session: https://claude.ai/code/session_01CKrVJJy5jWVCkXAqgUqtqZ

- Add SCROLL action and resolution retry for real-app replay
  ([`88c9d30`](https://github.com/OpenAdaptAI/openadapt-flow/commit/88c9d3069c3bc3c3618ed637d5d4195043ccf039))

Additive IR change: ActionKind.SCROLL with Step.scroll_dx/scroll_dy wheel deltas. Backend protocol
  gains scroll(dx, dy) — a wheel gesture at the current pointer position, so nested scroll
  containers and iframes scroll exactly as they do for a human. Recorder records scroll events; the
  compiler emits SCROLL steps with no postconditions (a scroll shifts the whole viewport, so frame
  diffs would assert mutable page content — the next anchored step's resolution verifies the scroll
  landed); the replayer dispatches them.

The replayer also now retries resolution-ladder misses with fresh settled frames until
  Step.timeout_s (previously unused in the runtime): remote apps can present a settled-looking but
  still-loading frame where the target only appears moments later. Structural errors and the risk
  gate do not retry.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>

- Armed-coverage metric in the hybrid benchmark methodology
  ([`0f20ec4`](https://github.com/OpenAdaptAI/openadapt-flow/commit/0f20ec49c544502fb6fb621be789cd59fdb49a4e))

The hybrid generator (PR #14, merged after this branch forked) reuses _compiled_run and
  _arm_aggregate, so its compiled-arm rows and aggregates already carry the identity-coverage
  fields; this renders them in the BENCHMARK.md methodology section. The committed
  benchmark/hybrid/results.json predates the metric, so the regenerated BENCHMARK.md carries the
  explicit not-captured note (verbatim regeneration verified — one-line diff).

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>

Claude-Session: https://claude.ai/code/session_01CKrVJJy5jWVCkXAqgUqtqZ

- Automated Parallels desktop benchmark pipeline (Phase 2)
  ([`9c91537`](https://github.com/OpenAdaptAI/openadapt-flow/commit/9c915377b118837ddbbb9f88a6863ec88a8a27dc))

Fully programmatic, $0 desktop benchmark on a local Parallels Windows 11 ARM VM (Apple Silicon has
  no nested virt, so the WAA/QEMU stack can't run here). No manual/GUI steps; no cloud; no model
  calls (ANTHROPIC_API_KEY unset).

Control plane - openadapt_flow/backends/parallels_vm.py: ParallelsVM wraps prlctl for
  lifecycle/snapshot/revert/exec/capture, guest/host IP discovery, ephemeral- port file push (prlctl
  exec hangs on long args), and shim launch. - scripts/desktop/session1_launch.py: launches the shim
  in the interactive console session (session 1) via WTSQueryUserToken + CreateProcessAsUser --
  prlctl exec lands in SYSTEM/session 0, where mss BitBlt and pyautogui input can't reach the
  desktop. This is the foundational blocker, solved. - scripts/desktop/waa_shim.py: in-guest
  WAA-contract HTTP shim (GET /screenshot PNG, POST /execute_windows exec, GET /uia tree dump),
  reusing the Phase-1 WindowsBackend contract unchanged.

Target app + ground truth - scripts/desktop/patient_notes.ps1: real WinForms list-select->edit->save
  app (drift knobs via pn_env.json). Substitute for OpenDental, whose trial is a 149MB interactive
  bootstrapper gated by SmartScreen + a UAC secure-desktop prompt -- not no-touch installable
  (documented honestly in PHASE2.md/LIMITS). - scripts/desktop/pn_db.py: SQLite ground-truth CLI;
  the judge reads DB state, never OCR -- wrong-action detection is exact. -
  scripts/desktop/uia_arm.py: pywinauto UIA incumbent, identity + positional.

Benchmark - openadapt_flow/benchmark/desktop_benchmark.py: 3 arms x 7 conditions (clean,
  render_125/150 as DPI proxy, theme_dark, data_reorder/decoy/siblings), record->compile->replay via
  WindowsBackend, DB judge, results.json + BENCHMARK.md + chart. Per-run reset by DB reseed +
  relaunch; harness-ready VM snapshot for warm boot.

Findings (n=3/cell, DB ground truth): the record->compile->replay mechanism works on a real desktop
  with identity bands on desktop-rendered text; vision replay is defeated by render-scale/theme
  drift (0% -> safe-halt, never mis-writes); the positional UIA incumbent silently mis-writes on any
  name collision; identity catches a distinct decoy (safe-halt) but FALSE-VERIFIES a near-lexical
  sibling (Sorenson~Sorensen) -- the desktop analogue of the open browser wrong-action findings;
  UIA-tree quality 5/6, the identity-critical patient row has no AutomationId. Caveats (ARM+x64
  emulation, render-scale-as-DPI proxy, WinForms substitute) in docs/desktop/LIMITS.md.

Includes the Phase-1 WindowsBackend + capture adapter (rebased onto current main; previously only on
  feat/desktop-backend). Tests mock the VM/HTTP; full suite green.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>

Claude-Session: https://claude.ai/code/session_01CKrVJJy5jWVCkXAqgUqtqZ

- Benchmark results — compiled replay vs computer-use agent
  ([`b2eec0b`](https://github.com/OpenAdaptAI/openadapt-flow/commit/b2eec0be7ddee1930bd45d447cee3afced8d1fd8))

Full run on MockMed triage (2026-07-08, claude-sonnet-5 + computer_20251124):

- compiled: 100/100 success, p50 4.9s, p95 5.1s, $0/run - agent: 20/20 success, p50 37.5s, p95
  43.4s, $0.27/run ($5.43 total, list price) - drift=theme: compiled healed (8 heals, 9.7s); agent
  succeeded in 87.4s at $0.63 (failed on budget in an earlier smoke run — n=1 either way)

Both arms judged by the same OCR check of the final screenshot.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>

- Closed-loop scroll — SCROLL steps scroll until the next anchor resolves
  ([`c20d329`](https://github.com/OpenAdaptAI/openadapt-flow/commit/c20d3290b993d7217bcac095f844e291c583befd))

A compiled SCROLL step now executes as a closed loop: probe the NEXT anchored step's anchor on the
  current settled frame (no-op if already in view), then repeat scroll-by-recorded-delta -> settle
  -> probe until the anchor resolves, bounded by ~2.5x the step's recorded scroll distance.
  Consecutive SCROLL steps hand the loop to each other (combined ~2.5x budget); exhausting the
  budget with no following SCROLL step fails the run loudly, naming the anchor that never came into
  view. Falls back to the fixed recorded delta when no later step has an anchor. Probes never call
  the grounder, keeping replays model-free.

This removes the open-loop failure mode from the OpenEMR spike (run 5: grown dashboard content
  displaced the post-scroll viewport ~12px and a geometry resolution missed an 18px icon).

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>

- Compiled-replay vs computer-use-agent benchmark harness
  ([`27cd30b`](https://github.com/OpenAdaptAI/openadapt-flow/commit/27cd30b1728ba40093284a7b4dd5a932b551faee))

Adds openadapt_flow/benchmark:

- agent_baseline: minimal Claude computer-use agent (claude-sonnet-5, computer_20251124 tool)
  driving the same vision-only PlaywrightBackend the replayer uses; 25-action budget, history
  bounded to the last 3 screenshots, per-run token/cost accounting at list pricing. - verify:
  arm-independent success criterion (OCR of the final screenshot must show the encounter-saved
  banner and the Triage encounter row). - run_benchmark: orchestrator (record+compile once, N
  compiled replays, N agent runs, one drift=theme run per arm) emitting results.json, BENCHMARK.md,
  and latency_cost.png. - CLI: openadapt-flow benchmark --n-compiled N --n-agent N --out DIR. - 22
  unit tests, no network (fake Anthropic client + fake backend).

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>

- Disclose compiled runs that self-flagged postcondition drift in BENCHMARK.md
  ([`5db5ab5`](https://github.com/OpenAdaptAI/openadapt-flow/commit/5db5ab56929a0d2a7c6d80bd943e00c13baab68d))

One of the 20 compiled runs (run 20) self-flagged expected-screen drift at step_017 after the save
  click; the arm-independent OCR check both arms share verified the note saved, so it counts as a
  success — and is now disclosed as such. The renderer computes the disclosure from results.json
  (success=true, replayer_success=false) so regeneration preserves it; the 20/20 headline is
  unchanged and unit-tested.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>

Claude-Session: https://claude.ai/code/session_01CKrVJJy5jWVCkXAqgUqtqZ

- Dom-selector benchmark arm with drift hooks ported from hybrid
  ([`4926387`](https://github.com/OpenAdaptAI/openadapt-flow/commit/492638785bbd547dfb29d7e1ed89a550425baeaa))

Adds openadapt_flow/benchmark/dom_arm.py — a steelman Playwright selector script run head-to-head
  against the compiled vision replay on the hybrid benchmark's frozen 20-slot schedule and the
  validation suite's perturbation modes, judged by the same OCR final-state identity check. Ports
  the hybrid branch's flag-gated MockMed drift hooks (notice/reqfield/modal-once/typelabel) and adds
  a new 'sort' mode.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>

Claude-Session: https://claude.ai/code/session_01CKrVJJy5jWVCkXAqgUqtqZ

- Frozen adversarial corpus for the identity band matcher
  ([`4961831`](https://github.com/OpenAdaptAI/openadapt-flow/commit/4961831a763dd5f45bbe4a43e3ad157812f06660))

Deterministic, seeded generator (seed 20260710) of 4360 labeled (recorded_band, observed_band)
  pairs: 2200 different_entity (the false-accept side: prefix-extension names, single-letter sibling
  edits, transpositions, Jr/Sr suffixes, shared clinical text, DOB off-by-one-field, MRN digit
  swaps, adjacent-row mixtures) and 2160 same_entity (the false-abort side: OCR confusions,
  splits/joins, dropped short tokens, case/whitespace jitter, segment reordering, occlusion,
  spurious tokens, compound noise).

Frozen BEFORE evaluating or touching the matcher: the sha256 manifest is committed
  (docs/validation/adversary_corpus_manifest.json) and pinned by tests, so any post-hoc tuning of
  the corpus toward the matcher is detectable in git history.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>

Claude-Session: https://claude.ai/code/session_01CKrVJJy5jWVCkXAqgUqtqZ

- Frozen adversarial corpus v2 — the classes v1 excluded by construction
  ([`f77807b`](https://github.com/OpenAdaptAI/openadapt-flow/commit/f77807b9945a7e2c9379cb8db45c1de7d1f41aba))

Versioned extension of the frozen corpus (v1 generator and manifest are untouched, history intact):
  own seed (20260711), own SHA manifest, committed BEFORE the redesigned matcher is evaluated on it
  — the same freeze discipline as v1, so the corpus-v2 commit precedes the matcher-fix commit in git
  history.

2240 pairs across the reviewer-identified excluded classes:

- different_entity (1590): confusion-collided names generated systematically from the letter-letter
  members of the frozen confusion table over the v1 name lists (name-only / realistic distinct-IDs /
  identical-IDs probe shape), middle initial, sex column, 2-char names, observed-superset shapes
  (appended name, merged second row, title/cc row mentioning the recorded patient), and absent
  4-char name tokens. - indistinguishable (200) — NEW third label: the true row misread by a
  letter-letter confusion, textually identical to its different-entity twin. ABORT is the correct
  outcome for both readings; scoring counts abort as justified (not a false abort) and verify as a
  false accept. - same_entity (450): the availability side the new budgets must not kill —
  digit-class-only OCR noise (names contain no digits, so no collision is possible), lowercase
  adjacent-row bleed, hyphenated surname splits.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>

Claude-Session: https://claude.ai/code/session_01CKrVJJy5jWVCkXAqgUqtqZ

- Frozen adversarial corpus v3 — identifier letter/digit collisions
  ([`4b3f72b`](https://github.com/OpenAdaptAI/openadapt-flow/commit/4b3f72b435d173d19af58f4ace8174fc826d5982))

Versioned extension (v1/v2 untouched, history intact): own seed (20260712), own SHA manifest,
  committed BEFORE the identifier-suspect matcher change is evaluated on it — same freeze
  discipline, so the corpus-v3 commit precedes the matcher-fix commit.

300 different_entity pairs, one class id_letter_digit_collision: two entities identical in every
  token EXCEPT an alphanumeric identifier (MRN/account/chart ref) differing by exactly one
  letter/digit-confusable position (l/1, i/1, o/0, s/5, z/2, b/8, g/9), generated systematically
  from the confusion pairs. This is the class v1's mrn_digit_swap could not surface: v1 only
  swapped/changed DIGITS (748291 vs 748292), which are never in one confusion class. A VERIFY here
  is a wrong-patient action — the identifier is the sole discriminator and is exactly what MRN-based
  disambiguation relies on.

The generator renders one row template per pair and formats it with each identifier, so the
  identifier is provably the only differing token (pinned by
  test_v3_pairs_are_confusion_equivalent_and_id_only_differ). No same-entity identifier-noise class
  is added: under the chosen safety-first design all confusion-differing recorded identifiers abort,
  so such a label would be unwinnable by construction; the availability cost is measured directly on
  v2's digit_confusion_true_row class.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>

Claude-Session: https://claude.ai/code/session_01CKrVJJy5jWVCkXAqgUqtqZ

- Hard cost guardrails + prompt caching for the agent benchmark arm
  ([`099eac0`](https://github.com/OpenAdaptAI/openadapt-flow/commit/099eac0759440a86eceef38530797e8d5b765a15))

A previous benchmark run had no caps and burned real money mid-flight. This makes the ceiling
  structural:

- Prompt caching in run_agent: cache_control breakpoints on the computer-use tool definition and the
  newest user message each turn (stale markers stripped, 2 of 4 allowed breakpoints). Screenshot
  truncation intentionally mutates the prefix ~3 turns back; matching falls back to the longest
  still-valid earlier prefix so the growing stable prefix stays cached. Per-call usage (input /
  cache write / cache read / output) is logged for hit-rate visibility. - compute_cost prices all
  four usage buckets at claude-sonnet-5 list price: $3 input, $15 output, 1.25x input cache writes,
  0.1x reads.

- Per-run cap: run_agent(max_cost_usd=1.50) stops with stopped="cost_cap" and returns normally
  (capped run = data point). - Total cap: run_openemr_benchmark(max_total_cost_usd=8.00) truncates
  the agent arm before any run that could exceed the ceiling, with honest disclosure in results.json
  and BENCHMARK.md. - Preflight: one max_tokens=1 API call before any run; a dead key skips the
  agent arm and still runs the free compiled arm. - Billing-error abort: two consecutive
  auth/billing/credit failures abort the agent arm. - Incremental persistence: every finished run
  appends one JSON line to out_dir/rows.jsonl, so a stop/crash never loses completed-run data.

15 new tests; full suite 192 green.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>

Claude-Session: https://claude.ai/code/session_01CKrVJJy5jWVCkXAqgUqtqZ

- Harden postconditions and global template matching for live apps
  ([`b6d17ca`](https://github.com/OpenAdaptAI/openadapt-flow/commit/b6d17ca89e44fcea27e43b4f5a56a6472f83d961))

Two failure modes surfaced replaying against a live third-party app (OpenEMR public demo), both
  fixed at the root:

1. REGION_STABLE postconditions hashed a fixed region, but real apps re-layout by a few pixels
  between runs (OpenEMR's calendar day view scrolls itself relative to the current time, shifting
  the recorded region ~12px and pushing the phash distance to 34 with tolerance 16). The compiler
  now also stores a crop of the expected region content (templates/<step_id>_expect.png) and the
  replayer first searches for that content near the recorded region, falling back to the exact-
  position phash.

2. The global template rung clicked the wrong one of a dozen identical pencil icons (one per OpenEMR
  dashboard card) after mutable content near the true target changed and the local search missed.
  For unlabeled anchors a global match is now rejected when every locatable landmark places the
  target more than 40px away; the ladder then falls through to ocr/geometry. Labeled anchors are
  exempt (their templates carry the label; rename/move drift relies on global acceptance).

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>

- Hybrid compiled+agent-fallback benchmark harness
  ([`fec7902`](https://github.com/OpenAdaptAI/openadapt-flow/commit/fec79027c67b9929378c67d2768380f8f3499d21))

Four-arm MockMed benchmark (compiled / agent / demo-conditioned agent /
  compiled-with-fallback-on-halt) over one frozen 20-slot schedule with 30% drift. New MockMed drift
  hooks behind flags — notice (post-login interstitial), reqfield (required Acuity field),
  modal-once (one-shot survey modal) — each probed free to safe-halt the compiled bundle
  deterministically (3/3) while staying completable at intent level. Absorbed conditions
  (theme/rename/move/typelabel) rejected and reported.

Shared SpendLedger enforces the per-run cap and an $8 total ceiling across ALL paid runs (agent arms
  + hybrid fallbacks), with preflight, consecutive-billing-error abort, and rows.jsonl persistence.
  verify_hybrid_final checks final-state identity (right patient, right type) per the validation
  suite's silent wrong-action findings (PR #12).

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>

Claude-Session: https://claude.ai/code/session_01CKrVJJy5jWVCkXAqgUqtqZ

- Identity-protection coverage as a first-class, auditable metric
  ([`cd86ceb`](https://github.com/OpenAdaptAI/openadapt-flow/commit/cd86ceb5433c9a9d0eefe9f1198f5c3e5e06acb7))

Identity verification covers ONLY armed steps, and real bundles arm a minority (live OpenEMR checks
  armed 4/12; a fresh MockMed demo bundle arms 1/8). That fact was previously a buried sentence in a
  live-check note; an unarmed click proceeds with NO identity check at all. Now:

- workflow.json: per-step identity_armed / identity_unarmed_reason written by the compiler (with the
  concrete reason: no readable band text / only the target's own label / too generic after volatile
  filtering) so an operator can audit protection BEFORE running. - REPORT.md: every run report
  states 'N of M click steps identity-armed' and lists the unarmed steps by id, intent and reason
  (computed over the whole bundle at run start, not just executed steps; pre-metric bundles get an
  honest fallback reason). - Benchmark generators (MockMed + OpenEMR): compiled-arm rows and arm
  aggregates carry the coverage; BENCHMARK.md methodology sections render it. The committed
  BENCHMARK.md files' results.json predate the metric, so they carry an explicit 'not captured in
  this results.json' note instead of fabricated numbers. - docs/LIMITS.md: the dangerous list now
  LEADS with the coverage gap, and the wrong-entity section is updated for the 2026-07-10 matcher
  rebuild (near-name siblings, corpus rates, occlusion-abort rationale). -
  docs/validation/VALIDATION.md: 2026-07-10 fix update — the third wrong-patient reopening said
  plainly with the four probe strings, frozen-corpus methodology, before/after rates per category,
  ROC operating point with the stated cost weighting, and the coverage metric surfaces.

Verified end-to-end: CLI demo-record -> compile -> replay produces a REPORT.md with '1 of 8 click
  steps identity-armed' and per-step reasons; e2e CLI smoke test now asserts the section and the
  bundle fields.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>

Claude-Session: https://claude.ai/code/session_01CKrVJJy5jWVCkXAqgUqtqZ

- Mockmed adversarial drift modes and widgets lab page
  ([`e2618ca`](https://github.com/OpenAdaptAI/openadapt-flow/commit/e2618ca86d9bbc22ab31300a2a43ad9360aa5521))

New query-string drift modes for the demo app, all additive: font (19px type, reflows layout), zoom
  (CSS 125%), slow (delayed navigation renders, ?slowms= override), grow (4 referrals arrive above
  the target), lookalike (a pixel-identical row lands at the recorded position), missing (the
  target's row is gone), empty (no referrals). Plus widgets.html/widgets.js: one interaction
  primitive per ?panel= (select, checks, date, modal, typeahead, paginated+sortable table, keyboard
  flow, new-tab link, upload) for the primitive-taxonomy suite.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>

Claude-Session: https://claude.ai/code/session_01CKrVJJy5jWVCkXAqgUqtqZ

- Openemr benchmark orchestrator — compiled replay vs agent on the public demo
  ([`b7867f6`](https://github.com/OpenAdaptAI/openadapt-flow/commit/b7867f625d8cc570ce185f32f843db0c6aaaedfa))

Adds the external-target counterpart of the MockMed benchmark: 20 compiled replays vs 10
  computer-use-agent runs of the 18-step add-patient-note workflow against demo.openemr.io, with a
  distinct parameterized note per run in both arms, a shared OCR success check (verify_note_saved),
  pacing as public-demo courtesy, and per-run failure rows instead of retries. Also: agent scroll
  action support, timestamped-text exclusion in the

compiler's TEXT_PRESENT postconditions, and shared verify helpers.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>

Claude-Session: https://claude.ai/code/session_01CKrVJJy5jWVCkXAqgUqtqZ

- Openemr parameterization-depth and cross-instance driver
  ([`8b0c8b0`](https://github.com/OpenAdaptAI/openadapt-flow/commit/8b0c8b0eda2f1e776e76d2a03360da13dedcb44e))

scripts/openemr_param_depth.py records the add-patient-note workflow with the PATIENT search text
  parameterized, then replays one bundle with the demonstrated patient (control), a different
  patient (content-changing parameter), and against the /b/ alternate instance (cross-instance state
  drift). Compiled-replay only — zero model calls, no API key read; fresh browser per run, >=30s
  pacing, fake demo patients only. Artifacts land in gitignored runs/validation/track-d/.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>

Claude-Session: https://claude.ai/code/session_01CKrVJJy5jWVCkXAqgUqtqZ

- Run DOM vs compiled head-to-head; report disputes honestly
  ([`ba95893`](https://github.com/OpenAdaptAI/openadapt-flow/commit/ba95893d4971c0d21dc8ec89d7c68f803b2a32c1))

Results (all $0, deterministic): 14/20 tie on the frozen schedule (both arms stopped by
  notice/reqfield/modal-once); on the perturbation modes the DOM script wrote to the WRONG PATIENT
  on 4 of 8 modes (lookalike/missing/grow/sort, 8/8 runs) while the compiled arm's identity check
  safe-halted every one; DOM absorbed move/typelabel and is ~38x faster per clean run; compiled
  healed rename that broke the DOM script. Adds verification-dispute reporting: one theme run per
  arm completed but failed the shared OCR judge (dark-palette false negative, disclosed, counted as
  failure).

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>

Claude-Session: https://claude.ai/code/session_01CKrVJJy5jWVCkXAqgUqtqZ

- Windowsbackend + desktop recording-adapter contract (desktop spike phase 1)
  ([`368c898`](https://github.com/OpenAdaptAI/openadapt-flow/commit/368c898f4da00ef45640c21dd4ddbd69244f0b6b))

Phase 1 of the desktop spike: de-risk the desktop integration without a live VM or any model calls.

- WindowsBackend (openadapt_flow/backends/windows_backend.py): the 4-method vision-only Backend
  protocol over the WAA HTTP API (WAADirect pattern: GET /screenshot raw PNG, POST /execute_windows
  with bare-Python commands). Playwright-style key chords normalized to pyautogui; typed text
  embedded via repr(); non-ASCII text routed through the clipboard (pyautogui.write silently drops
  it — a silent wrong-write mode); pixel scroll converted to wheel notches. No structural
  observations — native desktop steps stay honestly unverified. New 'windows' extra carries the
  requests dependency. - Recording adapter (openadapt_flow/adapters/capture.py): the capture->flow
  contract converting an openadapt-capture session (capture.db + video.mp4) into the recording
  format (meta.json + events.jsonl + frames/), with logical-point -> frame-pixel scaling,
  video-based before/after frame selection, param marking, and loud rejection of anything that would
  silently drop a user action (drags, shortcuts, non-left clicks, unmapped keys, raw-only sessions).
  - Conformance proven with zero compiler/replayer changes: the unmodified Recorder ->
  compile_recording -> Replayer loop succeeds over WindowsBackend against a stateful mock WAA server
  (coordinate-checked state machine, real OCR), and the adapter's output compiles with the
  unmodified compiler from a synthetic capture session in openadapt-capture's exact on-disk schema.
  - docs/desktop/PHASE1.md: infra reality check (no Azure VMs exist -> contract-mock path), the
  reverse-engineered capture schema, the /execute_windows bare-Python correction to project memory,
  and the Phase-2 readiness checklist.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>

Claude-Session: https://claude.ai/code/session_01CKrVJJy5jWVCkXAqgUqtqZ

### Refactoring

- Reuse hybrid benchmark's frozen schedule and final-state check
  ([`99f5fdd`](https://github.com/OpenAdaptAI/openadapt-flow/commit/99f5fdd97a26f09d012f9e5b828d0d7fa8c79c5f))

dom_arm now imports SCHEDULE / DRIFT_TYPES / note_for_slot / condition_url / verify_hybrid_final
  from hybrid_benchmark (merged to main in PR #14) instead of carrying duplicates; adds MockMed
  tests for the new sort drift mode.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>

Claude-Session: https://claude.ai/code/session_01CKrVJJy5jWVCkXAqgUqtqZ

### Testing

- Adversarial validation suites — perturbation, chaos, primitives
  ([`c3f13aa`](https://github.com/OpenAdaptAI/openadapt-flow/commit/c3f13aa415377384c19831afd54c962f17fb37fc))

30 characterization tests pinning the failure-mode matrix in docs/validation/VALIDATION.md. Track A
  (test_perturbation.py): viewport / scale / font / data drift / timing envelopes — including the
  three silent wrong-patient saves under row drift, asserted AS the current behavior so any change
  is caught loudly. Track B (test_chaos.py): mid-run fault injection via ChaosBackend — entity
  deletion, opaque and invisible overlays, control swaps, focus theft (silent empty-note save),
  navigation hijack, mid-run rename. Track C (test_primitives.py): record→compile→ replay per
  interaction primitive on the widgets lab, including the vacuous successes (zero-postcondition
  steps) and the wrong-row click under reorder.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>

Claude-Session: https://claude.ai/code/session_01CKrVJJy5jWVCkXAqgUqtqZ

- Native-date characterization is platform-shaped; pin the invariant
  ([`9f49a8f`](https://github.com/OpenAdaptAI/openadapt-flow/commit/9f49a8f96eff4c1ee8b6509a9a766efb3aef114d))

The Linux renderer ignores digits typed into a native date input entirely (widget stays empty,
  status 'Ready and waiting.'), while macOS transforms them into the 70820-02-06 garbage — so
  pinning the garbage string was itself platform-dependent and failed on CI. The pinned invariant is
  the one that matters: a native-date TYPE step never false-verifies — both platform shapes
  safe-halt at the type step with the typed-input verification error.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>

Claude-Session: https://claude.ai/code/session_01CKrVJJy5jWVCkXAqgUqtqZ

- Pin the 13 out-of-corpus reviewer probes as acceptance criteria
  ([`c2a9072`](https://github.com/OpenAdaptAI/openadapt-flow/commit/c2a90720c4b493dc7f82a1e28cb24dda0fd929e4))

All 13 probes VERIFY against the shipped matcher at the shipped operating point (reproduced locally,
  wrong-patient direction); they are committed FIRST, asserting mismatch, so the acceptance criteria
  for the matcher redesign are on record before the redesign or corpus v2:

- Blocker 1: confusion-collided distinct names (Neil/Nell, Clay/Day, Marnie/Mamie, Gail/Gall) — the
  v1 corpus excluded this class by construction, so its 0.000% headline was partially tautological.
  - Blocker 2: sub-MIN_BLOCK tokens invisible to contradiction (middle initial, SEX column, 2-char
  names). - Blocker 3: observed-side superset always verifies (appended tokens, two-row merge, wrong
  row mentioning the recorded patient). - Major 4: fully absent 4-char name at the run cap verifies
  with the identity token never read.

Safe-direction pins (hyphenated split, Bob/Robert, Alison/Allison, MRN/DOB edits, digit-class
  homoglyphs, param-mode raw-run rejection of Neil->Nell) pass today and must keep passing. The Ann
  Marie/Annmarie join edge is pinned as a disclosed residual.

The 13 probe tests FAIL at this commit by design; they pass after the matcher redesign lands.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>

Claude-Session: https://claude.ai/code/session_01CKrVJJy5jWVCkXAqgUqtqZ

- Pin the 5th-reopening identifier letter/digit collision probes
  ([`5f348cf`](https://github.com/OpenAdaptAI/openadapt-flow/commit/5f348cf1bc294fa6e5673626e7324a82ae674eb2))

Second adversarial review of PR #16 found a 5th wrong-patient P0 reopening: the round-3 suspect
  budget guards NAME tokens only

(_name_plausible is False for any token containing a digit), so the rule was OFF for MRNs/account
  numbers while confusion canonicalization (l/1, O/0, S/5, Z/2, B/8, g/9) still applied to them. A
  different patient's alphanumeric identifier differing only by one letter/digit-confusable char
  silently VERIFIED, defeating MRN-based disambiguation of same-name patients.

Committed FIRST, FAILING, as acceptance criteria (reproduced locally): - probes 14-16: MRN/Acct l/1,
  O/0, S/5 confusions verify (must abort) - probe 17: two same-name patients, MRN the sole
  discriminator, one confusable char apart -> verify (the canonical clinical case; must abort
  regardless of name raw-match) - probe 18: same hole fires in param mode (MRN as parameter) -
  availability-cost boundary: true-row MRN OCR noise (A01234->AO1234) must abort under the chosen
  safety-first design (documented cost)

Controls that must keep passing: all-digit MRN diff (748291 vs 748292) mismatches via coverage not
  suspect; raw-equal MRN with name-side digit noise ('Belford'->'Be1ford') still verifies (the fix
  is scoped to RECORDED identifier tokens, not any observed digit).

The 6 new failing tests pass after the identifier-suspect fix; corpus v3 is frozen before that fix.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>

Claude-Session: https://claude.ai/code/session_01CKrVJJy5jWVCkXAqgUqtqZ

- Pin the third (ignored-input) native-date platform shape
  ([`b937b97`](https://github.com/OpenAdaptAI/openadapt-flow/commit/b937b97372c9df4fe4c168445dca03c414ff2bcf))

On the Linux renderer the native date input swallows typed digits entirely: the recording is itself
  a no-op, and the replay reproduces the

no-op — the refocus retry's focus-ring change with no readable text is exactly the masked acceptance
  shape, so the step verifies vacuously. The pinned invariant across all shapes is that no wrong
  date value is ever written at replay: the transformed-value shape (macOS) safe-halts on read-back,
  the ignored-input shape (Linux) no-ops. Both residues (false abort on transforming widgets;
  vacuous verify via the masked acceptance) were already disclosed in docs/LIMITS.md;
  VALIDATION.md's Track C row now states the platform split.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>

Claude-Session: https://claude.ai/code/session_01CKrVJJy5jWVCkXAqgUqtqZ

## v0.1.0 (2026-07-08)


### Bug Fixes

- **ci**: Create runs/ before pytest --basetemp on fresh checkout
  ([`c8054da`](https://github.com/OpenAdaptAI/openadapt-flow/commit/c8054daab9d34e3221e8f09b31246ae2523f2717))

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>

- **resolver**: Require 0.9 OCR label ratio so near-miss labels fall through to geometry
  ([`21ce00c`](https://github.com/OpenAdaptAI/openadapt-flow/commit/21ce00cbdd719e7f50b91e5d2aa57dd859b0c44d))

A 0.8 fuzzy ratio let the ocr rung match a different-but-similar label ('New Encounter' for 'Save
  Encounter', ratio ~0.81) and click the wrong element; Linux OCR rendering crossed the threshold
  that macOS stayed under, failing the rename-drift E2E in CI. Postconditions caught the wrong click
  as designed, but the rung should never accept it.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>

### Chores

- Genericize layered-platform references ahead of public release
  ([`61a3338`](https://github.com/OpenAdaptAI/openadapt-flow/commit/61a3338c359713b658b843b4a1e6a059c105c32d))

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>

### Documentation

- Readme with replay/heal screenshots and plainer prose
  ([`d050575`](https://github.com/OpenAdaptAI/openadapt-flow/commit/d05057591d0ebd1d2a31f2298e9dd417bf4b0ab2))

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>

- Showcase run artifacts — baseline and theme-drift replay reports
  ([`bdbadb6`](https://github.com/OpenAdaptAI/openadapt-flow/commit/bdbadb638c2a3e383aa5455855108464c563a8d0))

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>

### Features

- E2e drift matrix and integration fixes (heal-frame source, landmark offsets, param postcondition
  exclusion)
  ([`ff35626`](https://github.com/OpenAdaptAI/openadapt-flow/commit/ff356261032b8b2deb0c336cf2cb0aa8feb5e4d3))

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>

- Mockmed mock EMR app, Playwright backend, demonstration recorder
  ([`109a6da`](https://github.com/OpenAdaptAI/openadapt-flow/commit/109a6da18406fab7ca985057a816624b0cb2a6ee))

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>

- Pypi release workflow (trusted publishing); playwright as core dependency
  ([`73f595f`](https://github.com/OpenAdaptAI/openadapt-flow/commit/73f595f0e83eba6d073abca6170c7e8f92f7440c))

playwright moves from the dev extra to core dependencies: the CLI's demo-record and replay
  self-serve paths import it, so a plain 'pip install openadapt-flow' quickstart broke without it.
  Verified by installing the built wheel into a clean venv and running the full
  record->compile->replay loop from it.

Release: tag-triggered (v*) workflow — tag/version consistency check,

build, PyPI via OIDC trusted publishing (environment: pypi), GitHub release with artifacts.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>

- Replay runtime — resolution ladder, risk gate, postconditions, healing
  ([`6b03643`](https://github.com/OpenAdaptAI/openadapt-flow/commit/6b03643d1b2542c97908954aead58a461aecc2a6))

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>

- Run reports, bench harness, Skill/MCP emission, CLI, CI
  ([`4fb6a9b`](https://github.com/OpenAdaptAI/openadapt-flow/commit/4fb6a9b504d83565da5cb2e6486a2acc92914199))

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>

- Scaffold openadapt-flow — IR, backend protocol, design contracts
  ([`3fb2882`](https://github.com/OpenAdaptAI/openadapt-flow/commit/3fb28829cfce944dab136e22bc15cda6aa869d19))

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>

- Vision utilities and demonstration compiler
  ([`7a8ee36`](https://github.com/OpenAdaptAI/openadapt-flow/commit/7a8ee36f6b59319e23a0ae88fe09dedc79393d84))

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>

- **cli**: Replay self-serves MockMed when --url is omitted; add --drift
  ([`00e43a3`](https://github.com/OpenAdaptAI/openadapt-flow/commit/00e43a3fdb0fd659979d95968e981acc0d1ce443))

After demo-record and compile, the natural third command is replay — but it demanded a --url to an
  app the user isn't running, and the README worked around it with 'bench --n 1', which obscures the
  product's core loop. The flagship heal demo also had no CLI path short of hand-building a ?drift=
  URL.

replay now serves the bundled MockMed app when no --url is given and accepts --drift (rejected
  loudly when combined with --url), so the quickstart is the real story: record -> compile -> replay
  -> drift-and-heal, four commands. CLI smoke test extended to pin the self-serve contract, the heal
  outcome, and the --url/--drift rejection.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>

- **emit**: L1 acquisition-artifact emitter and layered-platform integration doc
  ([`2975bab`](https://github.com/OpenAdaptAI/openadapt-flow/commit/2975babbe01fb8381cea1d3bde831e02883024a9))

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
