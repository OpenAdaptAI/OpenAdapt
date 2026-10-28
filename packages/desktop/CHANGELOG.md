# CHANGELOG


## v0.25.0 (2026-10-28)

### Features

- **desktop**: an administrator can pin the approver root for this computer with `openadapt-desktop approver-pin ROOT` (with sudo, or from an elevated prompt on Windows). The pin lives in a file only an administrator can write: `/Library/Application Support/OpenAdapt` (macOS), `/etc/openadapt` (Linux) or `ProgramData\OpenAdapt` (Windows). Desktop trusts it only when no one else can change it or the folders that lead to it: it checks owners, permissions, access lists and links. On Windows and Linux it is the only pin.

### Bug Fixes

- **desktop**: a Mac reads its Keychain approver root with Keychain prompts turned off, so an item another program planted or rewrote counts as no root instead of a prompt someone might allow. A Keychain root that disagrees with the administrator's pin is refused.
- **desktop**: the stop-review screen says when no approver root is pinned, since answers can't count until one is.



## v0.24.1 (2026-10-25)

### Bug Fixes

- **desktop**: governed runs and approvals work again. Desktop bundles openadapt-flow 2.4.0, which accepts every option Desktop sends: the pinned run configuration, the approver root and roster head, and the approver signature from the app or from a key file. 0.23.0 and 0.24.0 bundled a Flow without them, so governed runs couldn't start and a person's answers couldn't be recorded. Every command line Desktop builds is now checked against the bundled Flow, both in CI and against the installer's own binary.



## v0.24.0 (2026-10-24)

### Features

- **desktop**: set up the approver key in the app, behind Touch ID
- **desktop**: review stops as one-tap cards with the evidence side by side,
  on the computer (keys 1-9, batches of similar stops) or a paired phone; every
  answer is signed with the approver key
- **desktop**: re-arm "only ask about problems" from a stop card with one
  Touch ID

### Bug Fixes

- **desktop**: a locked Mac isn't a Mac without a Secure Enclave
- **desktop**: the Touch ID setup signs the approver roster's root behind its one prompt and pins that root for Desktop's runs; a key rotation is signed by the old key, and a roster this Mac didn't start needs an approver to add the key
- **desktop**: re-register a stored approver key only after it opens behind Touch ID
- **desktop**: Flow checks each run's config against the digest Desktop computed (--config-sha256); Run it answers carry the Keychain's head; staged-config folders that are symlinks are refused; only the Keychain names Desktop's approver root (not OPENADAPT_APPROVER_ROOT, not a user-writable file); with no Keychain pin, no person's answer counts
- **desktop**: each run's deployment config also carries the Keychain's roster head and is staged outside the run directory; Run it answers name this Mac's pin with --approver-root
- **desktop**: the Keychain's approver pin wins over OPENADAPT_APPROVER_ROOT (a different value is refused); each run's deployment config carries the pin; the roster head is kept in the Keychain and an older roster put back is refused
- **desktop**: a Mac that can keep the key behind Touch ID never falls back to a terminal-made key
- **desktop**: a stop card that changes during the Touch ID prompt changes nothing
- **desktop**: a stop where nothing was written also offers "I did it by hand"



## v0.23.0 (2026-10-24)

### Bug Fixes

- a person's approval needs the signature of a key on the signed approver roster, whose root the run pins from the deployment config's `approvers.root_public_key` (a governed run takes it from nowhere else, so an environment variable can't pin it). Set the key up from a terminal (`jit keygen`, then `jit approver init`); every later approver is added with an approver's signature (`jit approver add`). An approval from a key not on the pinned roster is refused.
- security updates to locked dependencies (pillow 13.0.0, certifi, setuptools, rustls-webpki, webpki-root-certs)



## v0.22.0 (2026-10-19)

0.21.1 was never published: its release stopped before PyPI, because the release check accepted only Python packages and refused the Desktop installers. This release ships the 0.21.1 and 0.21.0 changes below together with these.

### Features

- **desktop**: Check it asks three questions; the rest comes from the recording

### Bug Fixes

- **desktop**: check and approve the one sealed version that runs



## v0.21.1 (2026-10-19)

0.21.0 was never published, because main's macOS, Windows and Linux builds of it failed. This release ships the 0.21.0 changes below together with these fixes.

### Bug Fixes

- **desktop**: go back to tauri-plugin-deep-link 2.6.1 so the Linux shell compiles
- **desktop**: supply the reviewed PyObjC license for the 12.2.2 wheels
- **desktop**: prepare a release on a CRLF checkout



## v0.21.0 (2026-10-18)

### Features

- **approval**: show who decided each save

### Bug Fixes

- **approvers**: the phone route is the decision portal, not a Cloud relay
- **desktop**: release only on a commit whose desktop jobs passed
- **desktop**: price an S3 endpoint as R2 by its host; scope JS/Rust CodeQL



## v0.20.0 (2026-10-18)

### Documentation

- **portal**: The swipe card, its rules, and new reference captures ([`dcc189b`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/dcc189bdcb6eb90ba42321c7f7673fc32f289d1d))

### Features

- Opt-in, local shadow mode with a notice, status indicators, and draft offers ([#205](https://github.com/OpenAdaptAI/openadapt-desktop/pull/205), [`6902a5d`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/6902a5d51febde51e14c03c3e941782d303e0f45))

- **portal**: One swipe card per decision for paused runs and saves ([`6dd8292`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/6dd82926d7300bd47ff78cc97d4d24036d617af2))

- **portal**: Relay Flow's save-approval tasks to the paired phone ([`3c66e27`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/3c66e27a1ee504ee88bc2d9398cf8328fdf9d983))

### Testing

- **shadow**: Drop an unused import ([`36e3572`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/36e35721cc3ca6a12f80b2925dd4930bcb51a8d0))

- **shadow**: Make the shadow-mode tests pass on Windows ([`745f28e`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/745f28eeeb1d69beaa45fba434c0a012741f0947))


## v0.19.0 (2026-10-17)

### Features

- **teach**: Offer a branch once one step's halt is resolved twice ([#194](https://github.com/OpenAdaptAI/openadapt-desktop/pull/194), [`e43b9d4`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/e43b9d4f11c0b57ce94313872b0acca7e1a41c26))


## v0.18.0 (2026-10-17)

### Build System

- Bump Tauri JS and Rust dependencies together ([#197](https://github.com/OpenAdaptAI/openadapt-desktop/pull/197), [`9016888`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/9016888de5088af25b151d9bbce1101ec6b5eaef))

- Bump the cargo-minor group across 1 directory with 9 updates ([#197](https://github.com/OpenAdaptAI/openadapt-desktop/pull/197), [`9016888`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/9016888de5088af25b151d9bbce1101ec6b5eaef))

- Bump the npm-minor group across 1 directory with 8 updates ([#197](https://github.com/OpenAdaptAI/openadapt-desktop/pull/197), [`9016888`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/9016888de5088af25b151d9bbce1101ec6b5eaef))

- Pin openadapt-flow 2.1.0 ([#200](https://github.com/OpenAdaptAI/openadapt-desktop/pull/200), [`4b03ee1`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/4b03ee1627c9dd484f96a9030844dfae8013d4ab))

- Upgrade to React 19 (react, react-dom, and types together) ([#198](https://github.com/OpenAdaptAI/openadapt-desktop/pull/198), [`0c8c804`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/0c8c8044537f7a54c8babb29c38f0aa61722f823))

### Continuous Integration

- Bump the actions group across 1 directory with 10 updates ([#183](https://github.com/OpenAdaptAI/openadapt-desktop/pull/183), [`bf48617`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/bf486174d50ed30cb00b4d6f4725a4d24ccca068))

### Documentation

- **readme**: Say an agent can drive recording in native Windows apps and over RDP/Citrix ([#199](https://github.com/OpenAdaptAI/openadapt-desktop/pull/199), [`c402442`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/c4024427f2781d0ac335beab9c091b19e455c1b3))

### Features

- **needs-you**: Show the field diff on a Check the record stop ([#203](https://github.com/OpenAdaptAI/openadapt-desktop/pull/203), [`f10d0b5`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/f10d0b5ef814825250d98d1a4429abd586dbedb6))

### Testing

- Pin reviewed Tauri CLI 2.12.2 / API 2.12.3 ([#197](https://github.com/OpenAdaptAI/openadapt-desktop/pull/197), [`9016888`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/9016888de5088af25b151d9bbce1101ec6b5eaef))

- Update pinned-action contracts for the actions bump ([#183](https://github.com/OpenAdaptAI/openadapt-desktop/pull/183), [`bf48617`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/bf486174d50ed30cb00b4d6f4725a4d24ccca068))


## v0.17.1 (2026-10-17)

### Bug Fixes

- **deps**: Drop tauri-plugin-updater zip feature to remove zip 4.x ([#195](https://github.com/OpenAdaptAI/openadapt-desktop/pull/195), [`171a740`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/171a7405d6624d58a8367e597f8c0e56252792c2))

- **deps**: Upgrade zip to 9.0 for RUSTSEC-2026-0367/0368/0369 ([#195](https://github.com/OpenAdaptAI/openadapt-desktop/pull/195), [`171a740`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/171a7405d6624d58a8367e597f8c0e56252792c2))


## v0.17.0 (2026-10-16)

### Bug Fixes

- Doctor names the openadapt-flow it found instead of "development PATH" ([#188](https://github.com/OpenAdaptAI/openadapt-desktop/pull/188), [`d45d11a`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/d45d11acf2d5b35771bea2f342ebb74cb1524dcd))

- Format oracle vocabulary test ([#159](https://github.com/OpenAdaptAI/openadapt-desktop/pull/159), [`9a1708e`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/9a1708e64346cade44982ef58615e8a30f3ef867))

- Keep an uncertain first run locked when another task starts ([#179](https://github.com/OpenAdaptAI/openadapt-desktop/pull/179), [`be08876`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/be088764b53c78349be0a1d5ebd76e8ca9d7168f))

- Keep Deploy disabled until the version can deploy, and drop a duplicate button ([#180](https://github.com/OpenAdaptAI/openadapt-desktop/pull/180), [`2396e14`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/2396e148aae15d37072934b649747a630c5ef5c2))

- Keep local data private; package imports as openadapt_desktop ([#190](https://github.com/OpenAdaptAI/openadapt-desktop/pull/190), [`62ffe9e`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/62ffe9e5611f8ce91158c24ee5f13bb523551cee))

- Make the source quick start work, and rewrite the README for developers ([#188](https://github.com/OpenAdaptAI/openadapt-desktop/pull/188), [`d45d11a`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/d45d11acf2d5b35771bea2f342ebb74cb1524dcd))

- Never call a saving task read-only; run Check it signed in ([#185](https://github.com/OpenAdaptAI/openadapt-desktop/pull/185), [`b9294b2`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/b9294b23855351e6ffcbd4e595a73ca179c45aa0))

- Patch lock advisories, gate Deploy, and fix admission CI ([#180](https://github.com/OpenAdaptAI/openadapt-desktop/pull/180), [`2396e14`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/2396e148aae15d37072934b649747a630c5ef5c2))

- Report the RDP driver the ready path actually uses ([#180](https://github.com/OpenAdaptAI/openadapt-desktop/pull/180), [`2396e14`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/2396e148aae15d37072934b649747a630c5ef5c2))

- Run the app from a source checkout, and stop doctor flagging optional items ([#188](https://github.com/OpenAdaptAI/openadapt-desktop/pull/188), [`d45d11a`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/d45d11acf2d5b35771bea2f342ebb74cb1524dcd))

- Update GitPython past current advisories ([#159](https://github.com/OpenAdaptAI/openadapt-desktop/pull/159), [`9a1708e`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/9a1708e64346cade44982ef58615e8a30f3ef867))

- Use canonical Oracle tiers in operator copy ([#159](https://github.com/OpenAdaptAI/openadapt-desktop/pull/159), [`9a1708e`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/9a1708e64346cade44982ef58615e8a30f3ef867))

- **check-it**: Never call a task read-only unless every step is proven read-only ([#185](https://github.com/OpenAdaptAI/openadapt-desktop/pull/185), [`b9294b2`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/b9294b23855351e6ffcbd4e595a73ca179c45aa0))

- **check-it**: Never call a task read-only while Flow counts steps it can't see ([#185](https://github.com/OpenAdaptAI/openadapt-desktop/pull/185), [`b9294b2`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/b9294b23855351e6ffcbd4e595a73ca179c45aa0))

- **check-it**: Say drills come with the practice item; don't deny a known record check ([#184](https://github.com/OpenAdaptAI/openadapt-desktop/pull/184), [`0c03bb6`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/0c03bb6329daf8da9d20ae5e5efe6ba7db7a7ae3))

- **check-it**: Say when OpenAdapt doesn't know what a save writes ([#185](https://github.com/OpenAdaptAI/openadapt-desktop/pull/185), [`b9294b2`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/b9294b23855351e6ffcbd4e595a73ca179c45aa0))

- **check-it**: Say where and why a practice run or drill didn't pass ([#184](https://github.com/OpenAdaptAI/openadapt-desktop/pull/184), [`0c03bb6`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/0c03bb6329daf8da9d20ae5e5efe6ba7db7a7ae3))

- **check-it**: Show Flow's own words for the step a case run stopped at ([#184](https://github.com/OpenAdaptAI/openadapt-desktop/pull/184), [`0c03bb6`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/0c03bb6329daf8da9d20ae5e5efe6ba7db7a7ae3))

- **deps**: Accept the same openadapt-types range as the bundled Flow ([#180](https://github.com/OpenAdaptAI/openadapt-desktop/pull/180), [`2396e14`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/2396e148aae15d37072934b649747a630c5ef5c2))

- **deps**: Move locked packages past known security advisories ([#179](https://github.com/OpenAdaptAI/openadapt-desktop/pull/179), [`be08876`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/be088764b53c78349be0a1d5ebd76e8ca9d7168f))

- **deps**: Move locked packages past known security advisories ([#180](https://github.com/OpenAdaptAI/openadapt-desktop/pull/180), [`2396e14`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/2396e148aae15d37072934b649747a630c5ef5c2))

- **deps**: Update rustls to 0.23.46 for RUSTSEC-2026-0346 to 0349 ([#190](https://github.com/OpenAdaptAI/openadapt-desktop/pull/190), [`62ffe9e`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/62ffe9e5611f8ce91158c24ee5f13bb523551cee))

- **deps**: Update rustls to 0.23.46 for RUSTSEC-2026-0346..0349 ([#189](https://github.com/OpenAdaptAI/openadapt-desktop/pull/189), [`13706a4`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/13706a49cfeaf39f3217fc5c78c9e2b9404a2285))

- **engine**: An item interrupted mid-run becomes a record to check ([#179](https://github.com/OpenAdaptAI/openadapt-desktop/pull/179), [`be08876`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/be088764b53c78349be0a1d5ebd76e8ca9d7168f))

- **engine**: Require a record check before closing a possible save ([#179](https://github.com/OpenAdaptAI/openadapt-desktop/pull/179), [`be08876`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/be088764b53c78349be0a1d5ebd76e8ca9d7168f))

- **engine**: Run the sample with optional product signals off ([#179](https://github.com/OpenAdaptAI/openadapt-desktop/pull/179), [`be08876`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/be088764b53c78349be0a1d5ebd76e8ca9d7168f))

- **engine**: Send an item to a person unless every record agrees ([#184](https://github.com/OpenAdaptAI/openadapt-desktop/pull/184), [`0c03bb6`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/0c03bb6329daf8da9d20ae5e5efe6ba7db7a7ae3))

- **shell**: Give item, sample, and readiness-test runs the workflow timeout ([#179](https://github.com/OpenAdaptAI/openadapt-desktop/pull/179), [`be08876`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/be088764b53c78349be0a1d5ebd76e8ca9d7168f))

- **targets**: Refuse a signed-in browser from any config on a Flow that ignores it ([#185](https://github.com/OpenAdaptAI/openadapt-desktop/pull/185), [`b9294b2`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/b9294b23855351e6ffcbd4e595a73ca179c45aa0))

- **tests**: Keep bundle and qualification keys off the real keychain ([#189](https://github.com/OpenAdaptAI/openadapt-desktop/pull/189), [`13706a4`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/13706a49cfeaf39f3217fc5c78c9e2b9404a2285))

- **ui**: Finish the Clinical Calm pass on the operator screens ([#179](https://github.com/OpenAdaptAI/openadapt-desktop/pull/179), [`be08876`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/be088764b53c78349be0a1d5ebd76e8ca9d7168f))

- **ui**: Fold away a passed practice run and finished drills in Check it ([#179](https://github.com/OpenAdaptAI/openadapt-desktop/pull/179), [`be08876`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/be088764b53c78349be0a1d5ebd76e8ca9d7168f))

- **ui**: Let a read-only task start Check it, and hide a no-op confirm ([#179](https://github.com/OpenAdaptAI/openadapt-desktop/pull/179), [`be08876`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/be088764b53c78349be0a1d5ebd76e8ca9d7168f))

- **ui**: Never cut, drop or default a value on the way to a task ([#184](https://github.com/OpenAdaptAI/openadapt-desktop/pull/184), [`0c03bb6`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/0c03bb6329daf8da9d20ae5e5efe6ba7db7a7ae3))

- **ui**: Plain words on Settings and the runner; fix the phone page layout ([#179](https://github.com/OpenAdaptAI/openadapt-desktop/pull/179), [`be08876`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/be088764b53c78349be0a1d5ebd76e8ca9d7168f))

- **ui**: Say only what the drills, a finished list, and a repeat refusal prove ([#179](https://github.com/OpenAdaptAI/openadapt-desktop/pull/179), [`be08876`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/be088764b53c78349be0a1d5ebd76e8ca9d7168f))

- **ui**: Show a closed Needs-you item in ink, not the verdict green ([#179](https://github.com/OpenAdaptAI/openadapt-desktop/pull/179), [`be08876`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/be088764b53c78349be0a1d5ebd76e8ca9d7168f))

- **ui**: Start a new list after Run it finishes instead of running it again ([#179](https://github.com/OpenAdaptAI/openadapt-desktop/pull/179), [`be08876`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/be088764b53c78349be0a1d5ebd76e8ca9d7168f))

- **ui**: Zoom the sample screens to the record area ([#179](https://github.com/OpenAdaptAI/openadapt-desktop/pull/179), [`be08876`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/be088764b53c78349be0a1d5ebd76e8ca9d7168f))

### Build System

- Set the package author to MLDSAI Inc. ([#192](https://github.com/OpenAdaptAI/openadapt-desktop/pull/192), [`12714c9`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/12714c997feb1f71d87e84c0148948aebd2cf63c))

- Ship the font licences with the built app ([#179](https://github.com/OpenAdaptAI/openadapt-desktop/pull/179), [`be08876`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/be088764b53c78349be0a1d5ebd76e8ca9d7168f))

### Chores

- Align published runtime and native dependency contracts ([`e4ec785`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/e4ec7858f81bb3703c1a1d1deae7569e070653b3))

- Correct native FFmpeg extraction assertions ([#162](https://github.com/OpenAdaptAI/openadapt-desktop/pull/162), [`cf6a98d`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/cf6a98df9d5a4bf0a168b4637ed04743901081f2))

- Sync the published source policy ([#187](https://github.com/OpenAdaptAI/openadapt-desktop/pull/187), [`b89b394`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/b89b394402b819da871df11a39218e1e10bc0f7c))

- Update CI dependencies and fix rustls advisory ([`a6faa18`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/a6faa188085a18a0c05af551303902f649542793))

- Update locked Rust networking dependencies ([`dcceab0`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/dcceab0a1681c5d3533d6c07abad305d06261b6d))

- Update release actions and use published design tokens ([`27b960b`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/27b960bcbbc5499656eba4621205e8fe5ab09834))

### Code Style

- Wrap a long line in the engine test fixture ([#190](https://github.com/OpenAdaptAI/openadapt-desktop/pull/190), [`62ffe9e`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/62ffe9e5611f8ce91158c24ee5f13bb523551cee))

- Wrap long lines in the keyring isolation test ([#190](https://github.com/OpenAdaptAI/openadapt-desktop/pull/190), [`62ffe9e`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/62ffe9e5611f8ce91158c24ee5f13bb523551cee))

### Continuous Integration

- Install the central validator's dependencies before Production admission checks ([#180](https://github.com/OpenAdaptAI/openadapt-desktop/pull/180), [`2396e14`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/2396e148aae15d37072934b649747a630c5ef5c2))

- **release**: Publish engine releases automatically on green main ([#193](https://github.com/OpenAdaptAI/openadapt-desktop/pull/193), [`cefcd80`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/cefcd80e51675a7b423b43f626ec30ff1f666b71))

### Documentation

- Describe per-save approval and the local drill campaign ([#184](https://github.com/OpenAdaptAI/openadapt-desktop/pull/184), [`0c03bb6`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/0c03bb6329daf8da9d20ae5e5efe6ba7db7a7ae3))

- Describe the problems-only switch and shoot its design-review set ([#184](https://github.com/OpenAdaptAI/openadapt-desktop/pull/184), [`0c03bb6`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/0c03bb6329daf8da9d20ae5e5efe6ba7db7a7ae3))

- Lead the README with what Desktop does for a team, and document the operator screens ([#179](https://github.com/OpenAdaptAI/openadapt-desktop/pull/179), [`be08876`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/be088764b53c78349be0a1d5ebd76e8ca9d7168f))

- List the checks Desktop adds before an item closes without a person ([#184](https://github.com/OpenAdaptAI/openadapt-desktop/pull/184), [`0c03bb6`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/0c03bb6329daf8da9d20ae5e5efe6ba7db7a7ae3))

- List the Needs a fix status and the Show it step ([#179](https://github.com/OpenAdaptAI/openadapt-desktop/pull/179), [`be08876`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/be088764b53c78349be0a1d5ebd76e8ca9d7168f))

- Match the README to the code, and stop release builds packaging the dev launcher ([#188](https://github.com/OpenAdaptAI/openadapt-desktop/pull/188), [`d45d11a`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/d45d11acf2d5b35771bea2f342ebb74cb1524dcd))

- Name the bundled Flow 1.35.1 where the docs state the pin ([#180](https://github.com/OpenAdaptAI/openadapt-desktop/pull/180), [`2396e14`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/2396e148aae15d37072934b649747a630c5ef5c2))

- Point the README at flow tutorial, not quickstart ([#157](https://github.com/OpenAdaptAI/openadapt-desktop/pull/157), [`095082c`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/095082c87ca51fa5d2d2979b99955adf5abd610a))

- Re-shoot the README operator screens with the final styles ([#179](https://github.com/OpenAdaptAI/openadapt-desktop/pull/179), [`be08876`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/be088764b53c78349be0a1d5ebd76e8ca9d7168f))

- Rewrite the README for a developer who finds it on GitHub or PyPI ([#188](https://github.com/OpenAdaptAI/openadapt-desktop/pull/188), [`d45d11a`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/d45d11acf2d5b35771bea2f342ebb74cb1524dcd))

- Say what happens when an app's screen changes, accurately ([#179](https://github.com/OpenAdaptAI/openadapt-desktop/pull/179), [`be08876`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/be088764b53c78349be0a1d5ebd76e8ca9d7168f))

- State the current native release boundary ([#158](https://github.com/OpenAdaptAI/openadapt-desktop/pull/158), [`5845c12`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/5845c12d8e9073cd13039c0d2dcf2f3082f070f5))

- **evidence**: Record the first end-to-end run of the sample command ([#179](https://github.com/OpenAdaptAI/openadapt-desktop/pull/179), [`be08876`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/be088764b53c78349be0a1d5ebd76e8ca9d7168f))

- **readme**: A person or an agent shows the task; drop 'once' ([#191](https://github.com/OpenAdaptAI/openadapt-desktop/pull/191), [`ef36fc6`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/ef36fc6bbc3a1b634a6f5f980f463dbc3da8327e))

### Features

- A person approves each save; Check it runs the local drill campaign; problems-only switch ([#184](https://github.com/OpenAdaptAI/openadapt-desktop/pull/184), [`0c03bb6`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/0c03bb6329daf8da9d20ae5e5efe6ba7db7a7ae3))

- Tasks, Needs you, and Results with Show it, Check it, Run it ([#179](https://github.com/OpenAdaptAI/openadapt-desktop/pull/179), [`be08876`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/be088764b53c78349be0a1d5ebd76e8ca9d7168f))

- **engine**: A person approves each save; drills run from the local campaign ([#184](https://github.com/OpenAdaptAI/openadapt-desktop/pull/184), [`0c03bb6`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/0c03bb6329daf8da9d20ae5e5efe6ba7db7a7ae3))

- **engine**: Read the problems-only switch and Flow's per-item decision ([#184](https://github.com/OpenAdaptAI/openadapt-desktop/pull/184), [`0c03bb6`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/0c03bb6329daf8da9d20ae5e5efe6ba7db7a7ae3))

- **engine**: Remember task targets, expose readiness, add sample run ([#179](https://github.com/OpenAdaptAI/openadapt-desktop/pull/179), [`be08876`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/be088764b53c78349be0a1d5ebd76e8ca9d7168f))

- **targets**: Run Check it in the signed-in browser the task was recorded in ([#185](https://github.com/OpenAdaptAI/openadapt-desktop/pull/185), [`b9294b2`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/b9294b23855351e6ffcbd4e595a73ca179c45aa0))

- **ui**: Approve each save next to the item's own values ([#184](https://github.com/OpenAdaptAI/openadapt-desktop/pull/184), [`0c03bb6`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/0c03bb6329daf8da9d20ae5e5efe6ba7db7a7ae3))

- **ui**: Ask in plain words how to confirm a save the recording can't show ([#179](https://github.com/OpenAdaptAI/openadapt-desktop/pull/179), [`be08876`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/be088764b53c78349be0a1d5ebd76e8ca9d7168f))

- **ui**: Plain outcome mapping, item parsing, and wire types ([#179](https://github.com/OpenAdaptAI/openadapt-desktop/pull/179), [`be08876`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/be088764b53c78349be0a1d5ebd76e8ca9d7168f))

- **ui**: Show it again when a person chose to teach a fix ([#179](https://github.com/OpenAdaptAI/openadapt-desktop/pull/179), [`be08876`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/be088764b53c78349be0a1d5ebd76e8ca9d7168f))

- **ui**: Show the problems-only switch, its evidence, and what it saved ([#184](https://github.com/OpenAdaptAI/openadapt-desktop/pull/184), [`0c03bb6`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/0c03bb6329daf8da9d20ae5e5efe6ba7db7a7ae3))

- **ui**: Tasks, Needs you, and Results with Check it, Run it, and a sample ([#179](https://github.com/OpenAdaptAI/openadapt-desktop/pull/179), [`be08876`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/be088764b53c78349be0a1d5ebd76e8ca9d7168f))

### Testing

- Allow the item-approval commands next to the switch commands ([#184](https://github.com/OpenAdaptAI/openadapt-desktop/pull/184), [`0c03bb6`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/0c03bb6329daf8da9d20ae5e5efe6ba7db7a7ae3))

- **ui**: Pin the operator screens' safety and wording rules ([#179](https://github.com/OpenAdaptAI/openadapt-desktop/pull/179), [`be08876`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/be088764b53c78349be0a1d5ebd76e8ca9d7168f))


## v0.16.0 (2026-09-02)

### Bug Fixes

- Bind guided qualification fault targets ([#102](https://github.com/OpenAdaptAI/openadapt-desktop/pull/102), [`06e6266`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/06e6266143333f3a990bb173b2e0739c22eec2b3))

- Bind Teach corrections to halted runs ([`1f8674f`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/1f8674f78e0d424cc058bf30beddf122c58bff4d))

- Close Desktop release and Tray protocol gaps ([#143](https://github.com/OpenAdaptAI/openadapt-desktop/pull/143), [`aaa0c59`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/aaa0c591c354aba8ba86f27533bc868821c6ec1b))

- Close desktop release contract gaps ([#143](https://github.com/OpenAdaptAI/openadapt-desktop/pull/143), [`aaa0c59`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/aaa0c591c354aba8ba86f27533bc868821c6ec1b))

- Do not assert POSIX journal modes on Windows ([#151](https://github.com/OpenAdaptAI/openadapt-desktop/pull/151), [`5987957`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/5987957677a1d64bcef9061589390109de3c21fd))

- Enforce governed Desktop egress boundaries ([#116](https://github.com/OpenAdaptAI/openadapt-desktop/pull/116), [`e6c42df`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/e6c42df417a2674fcdd0149617ea214368f50a6a))

- Fail closed on untrusted native releases ([#115](https://github.com/OpenAdaptAI/openadapt-desktop/pull/115), [`6f43a97`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/6f43a97ef99c6251dfcd9dd4052c408298718195))

- Require Teach correction artifacts ([`d92a3f8`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/d92a3f8d0b88e4c0f5c453714fa09ce48274f075))

- Retain dual-path qualification targets ([#102](https://github.com/OpenAdaptAI/openadapt-desktop/pull/102), [`06e6266`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/06e6266143333f3a990bb173b2e0739c22eec2b3))

- Rustfmt overlay coords and skip POSIX 0600 on Windows ([`3345f7f`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/3345f7ffc140a3e7e0fbd181dae34ca8e0aab618))

- Secure Intel macOS cryptography build ([#103](https://github.com/OpenAdaptAI/openadapt-desktop/pull/103), [`38490d0`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/38490d0c6be75236aabbc664341aa7377b961db0))

- Ship the OpenAdapt robot as the macOS app icon and DMG ([`bacfc6c`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/bacfc6cf36f1b9b29e8b829ac40fe2ad15c74240))

- Update audited pip lock ([#126](https://github.com/OpenAdaptAI/openadapt-desktop/pull/126), [`a73c3fa`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/a73c3faf90f5d0b766ea7156a057bd6240d1c2bb))

- Update vulnerable Python dependencies ([#103](https://github.com/OpenAdaptAI/openadapt-desktop/pull/103), [`38490d0`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/38490d0c6be75236aabbc664341aa7377b961db0))

- Validate release verification contract ([#101](https://github.com/OpenAdaptAI/openadapt-desktop/pull/101), [`9405a78`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/9405a78de16709d2c73b6917e8b26589be24372b))

- Verify desktop release manifest bytes ([#101](https://github.com/OpenAdaptAI/openadapt-desktop/pull/101), [`9405a78`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/9405a78de16709d2c73b6917e8b26589be24372b))

- Verify release metadata checksums ([#101](https://github.com/OpenAdaptAI/openadapt-desktop/pull/101), [`9405a78`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/9405a78de16709d2c73b6917e8b26589be24372b))

- **ci**: Allowlist FFmpeg's public signing-key digest in gitleaks ([#138](https://github.com/OpenAdaptAI/openadapt-desktop/pull/138), [`6415619`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/641561957a4b04a6c12694767c00e11fc76afaf3))

- **ci**: Dispatch docs notification to renamed openadapt-ops repo ([#100](https://github.com/OpenAdaptAI/openadapt-desktop/pull/100), [`6f8a8da`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/6f8a8daea04c05be917e53e897f401b275f85310))

- **ci**: Isolate Desktop secret scan to owned commits ([`b57cee9`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/b57cee9d1b3cf6cf11dd057fdb34128cdf60f5a5))

- **ci**: Name the CI native checksum manifest exactly SHA256SUMS ([#121](https://github.com/OpenAdaptAI/openadapt-desktop/pull/121), [`acfca3f`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/acfca3fa2ff41df140938b9498d99138a5273366))

- **release**: Close the residual native release admission gaps ([#122](https://github.com/OpenAdaptAI/openadapt-desktop/pull/122), [`1511075`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/1511075b7b4db3b8cc9d8f65e92b449c9621e57e))

- **release**: Publish native chain from reviewed main ([`1e71b19`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/1e71b1958e3b9a53e448d58f53d6f289f2d733a6))

- **release**: Write the native version transform as exact UTF-8 LF bytes ([#120](https://github.com/OpenAdaptAI/openadapt-desktop/pull/120), [`f4f1235`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/f4f12353e22b3590f77b6a4232e2c2ca62d5c807))

- **tests**: Read tracked source files as UTF-8, not as the platform locale ([#140](https://github.com/OpenAdaptAI/openadapt-desktop/pull/140), [`0ec925d`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/0ec925d7cfc13b2f3cdbe6d8cd47ce50bcd94cb3))

### Build System

- Bump jsdom from 25.0.1 to 30.0.1 ([#96](https://github.com/OpenAdaptAI/openadapt-desktop/pull/96), [`0e63218`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/0e632180e7c38bd403a531c1bd9685cfd281fad7))

- Bump the npm-minor group with 3 updates ([#145](https://github.com/OpenAdaptAI/openadapt-desktop/pull/145), [`703f27b`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/703f27b56629fa8a6e889c7bba45d0737895f574))

- Lock python floors for httpx, segno, PyInstaller, and semantic-release ([#146](https://github.com/OpenAdaptAI/openadapt-desktop/pull/146), [`5a2aeab`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/5a2aeab617af088a90521a0c18ec682902878a63))

### Chores

- Ignore every .env variant, not one at a time ([#134](https://github.com/OpenAdaptAI/openadapt-desktop/pull/134), [`6ff4146`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/6ff4146b9eb64b0c4d21bb136c9e4ac4ace4c2cd))

- Refresh audited dependency locks ([`7091039`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/7091039fe1e2387f147b2eecad73ee52a35b3fce))

- **deps**: Update GitHub Actions ([#113](https://github.com/OpenAdaptAI/openadapt-desktop/pull/113), [`9947b21`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/9947b214c4e09bb92b3679d46b5dfdadf83f66a8))

- **deps**: Update the rustls lock ([#106](https://github.com/OpenAdaptAI/openadapt-desktop/pull/106), [`40647ad`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/40647ad6eb1c46cc25e8fc2e2134d107c565377b))

- **deps**: Update the Vite toolchain ([#104](https://github.com/OpenAdaptAI/openadapt-desktop/pull/104), [`86b1da2`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/86b1da232d88537e5e4b92ec23571008ed7ff81f))

- **native**: Sync native installer version to 0.15.0 ([`ac01e90`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/ac01e90f04e604f40e030046457f5b5167c88755))

- **policy**: Sync generated source boundary ([#136](https://github.com/OpenAdaptAI/openadapt-desktop/pull/136), [`321f643`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/321f6436cdfc0c0c0a64a43f25f97e2c3e77401f))

### Code Style

- Consume the canonical cool design tokens from openadapt-web ([`b6599e7`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/b6599e728a67efcf79a7f8a1d5cbaa9c5858c6c5))

### Continuous Integration

- Bound the apt installs and prefer the canonical Ubuntu archive ([#117](https://github.com/OpenAdaptAI/openadapt-desktop/pull/117), [`d354180`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/d35418018028d4f8912a41c6286b4f8881dcb268))

- Bump the actions group with 3 updates ([#150](https://github.com/OpenAdaptAI/openadapt-desktop/pull/150), [`5176e6e`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/5176e6ea5559fabc16c1f4892d07c6f1a7f82f18))

- Bump the actions group with 3 updates ([#98](https://github.com/OpenAdaptAI/openadapt-desktop/pull/98), [`38b0502`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/38b0502a73468645f5e0e4d01203571898760662))

- Correct the stale version comment on the pypi-publish pin ([#124](https://github.com/OpenAdaptAI/openadapt-desktop/pull/124), [`bfb2882`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/bfb2882e70cd6e4fbc90d02d9443d8fcf43f2755))

- Deduplicate native release builds ([#95](https://github.com/OpenAdaptAI/openadapt-desktop/pull/95), [`25038a2`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/25038a274fde6db7450581829672f23ff3fe0d55))

- Derive Desktop Production from central admission ([#126](https://github.com/OpenAdaptAI/openadapt-desktop/pull/126), [`a73c3fa`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/a73c3faf90f5d0b766ea7156a057bd6240d1c2bb))

- Gate Desktop Production on central admission ([#126](https://github.com/OpenAdaptAI/openadapt-desktop/pull/126), [`a73c3fa`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/a73c3faf90f5d0b766ea7156a057bd6240d1c2bb))

- Pin Desktop tests to merged Tray commit ([#143](https://github.com/OpenAdaptAI/openadapt-desktop/pull/143), [`aaa0c59`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/aaa0c591c354aba8ba86f27533bc868821c6ec1b))

- Publish verified desktop release manifest ([#101](https://github.com/OpenAdaptAI/openadapt-desktop/pull/101), [`9405a78`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/9405a78de16709d2c73b6917e8b26589be24372b))

### Documentation

- Align entry commands and substrate maturity across repo READMEs ([#99](https://github.com/OpenAdaptAI/openadapt-desktop/pull/99), [`18a3198`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/18a319817c4950993a8837a364491256ccaf1693))

- Correct the signing state, the socket owner, and the left rail ([#142](https://github.com/OpenAdaptAI/openadapt-desktop/pull/142), [`098f3f6`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/098f3f653e8bb5982ebc49b65d0af2a4a59a0b25))

- Keep the role and admission claims the metadata test pins ([#142](https://github.com/OpenAdaptAI/openadapt-desktop/pull/142), [`098f3f6`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/098f3f653e8bb5982ebc49b65d0af2a4a59a0b25))

- Record the shell commit the phone fixtures were captured from ([`d8c0856`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/d8c08567e42ae45e9687211d757ae3536c97e460))

- Require HTTPS decision portal origin ([#109](https://github.com/OpenAdaptAI/openadapt-desktop/pull/109), [`a06336b`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/a06336b3da07ddd961463f33622fdf532c08c1d0))

- Rewrite the README, and say up front that there is no build to download ([#142](https://github.com/OpenAdaptAI/openadapt-desktop/pull/142), [`098f3f6`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/098f3f653e8bb5982ebc49b65d0af2a4a59a0b25))

- Show mobile decision portal ([#109](https://github.com/OpenAdaptAI/openadapt-desktop/pull/109), [`a06336b`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/a06336b3da07ddd961463f33622fdf532c08c1d0))

- Tighten release candidate copy ([#126](https://github.com/OpenAdaptAI/openadapt-desktop/pull/126), [`a73c3fa`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/a73c3faf90f5d0b766ea7156a057bd6240d1c2bb))

### Features

- Add a local demonstration coach HUD on the control overlay ([#153](https://github.com/OpenAdaptAI/openadapt-desktop/pull/153), [`e90d3be`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/e90d3be4c26593707b0de4b627879c415133d21c))

- Add the qualification program workbench ([#137](https://github.com/OpenAdaptAI/openadapt-desktop/pull/137), [`1ad1731`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/1ad1731883aa6a9c4f366e52c5f87f4fcdefe8db))

- Guide qualification fault cases ([#102](https://github.com/OpenAdaptAI/openadapt-desktop/pull/102), [`06e6266`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/06e6266143333f3a990bb173b2e0739c22eec2b3))

- Guide the first real workflow run ([#135](https://github.com/OpenAdaptAI/openadapt-desktop/pull/135), [`b02cb16`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/b02cb165b580e6e50e41a8d114ffb521f70e7b23))

- Qualify this recording by filling Flow pins from the demo ([#152](https://github.com/OpenAdaptAI/openadapt-desktop/pull/152), [`bb6580a`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/bb6580ab9c0d0ba5cda1a2776ad53446c53e5d9b))

- Route hosted runs through Flow's governed adapter ([#130](https://github.com/OpenAdaptAI/openadapt-desktop/pull/130), [`87a08b9`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/87a08b94cebd63086495dba267b0ce08e0662a25))

- **capture**: Put both published workbench shots under the capture contract ([`b77a05a`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/b77a05a9976948dfcb36b266c5a07f1466acb017))

- **desktop**: Authoring runner URI, Allow-per-sub, and mailbox poll ([#154](https://github.com/OpenAdaptAI/openadapt-desktop/pull/154), [`20c9304`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/20c9304b7de13ee1e3dedbba9ff2f8d35b8b6f2b))

- **qualification**: Add business decision authoring ([#110](https://github.com/OpenAdaptAI/openadapt-desktop/pull/110), [`e6a9700`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/e6a9700185775cc4ff6b04ead2d4e4917c30aae9))

- **qualification**: Capture local institutional judgment cases ([#112](https://github.com/OpenAdaptAI/openadapt-desktop/pull/112), [`79a4e36`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/79a4e3644cde4cfe76539b62237ab09a97e9d219))

### Testing

- Guard the POSIX 0600 assertions that cannot hold on Windows ([#118](https://github.com/OpenAdaptAI/openadapt-desktop/pull/118), [`38b17ac`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/38b17ac350d622ff6ab7139fdd2d89a03af8bc5a))

- Pin repaired tray protocol peer ([#143](https://github.com/OpenAdaptAI/openadapt-desktop/pull/143), [`aaa0c59`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/aaa0c591c354aba8ba86f27533bc868821c6ec1b))

- **release**: State the O_NOFOLLOW contract per platform ([#123](https://github.com/OpenAdaptAI/openadapt-desktop/pull/123), [`a735601`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/a73560190939056c2e542e0b4ae8810be8846d8c))

- **runner**: Make the lease-expiry test observe the clock, not race it ([#119](https://github.com/OpenAdaptAI/openadapt-desktop/pull/119), [`8bebe4b`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/8bebe4b931b159a837b151df923f57f1c6e5c708))


## v0.15.0 (2026-07-31)

### Bug Fixes

- Attest observed qualification capabilities ([#74](https://github.com/OpenAdaptAI/openadapt-desktop/pull/74), [`c887890`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/c8878906f58066cb73dbb60912dac9017cf4adde))

- Bind capability coverage to latest typed run ([#74](https://github.com/OpenAdaptAI/openadapt-desktop/pull/74), [`c887890`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/c8878906f58066cb73dbb60912dac9017cf4adde))

- Bind receipts to one report read ([#74](https://github.com/OpenAdaptAI/openadapt-desktop/pull/74), [`c887890`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/c8878906f58066cb73dbb60912dac9017cf4adde))

- Declare qualification signer dependency ([#66](https://github.com/OpenAdaptAI/openadapt-desktop/pull/66), [`7218691`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/7218691f96baee67ee36124e98c90f1a061d4cd0))

- Keep browser provisioning substrate-aware ([#67](https://github.com/OpenAdaptAI/openadapt-desktop/pull/67), [`7ab0d6b`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/7ab0d6bc8c61706b91fa86d7148a1bc7aeefd86d))

- Make the local Desktop cockpit account-free ([#72](https://github.com/OpenAdaptAI/openadapt-desktop/pull/72), [`1685ea8`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/1685ea8a0a477aa332c37bde1d8e85551915cc8e))

- **auth**: Keep immediate rotation verification fresh ([#90](https://github.com/OpenAdaptAI/openadapt-desktop/pull/90), [`1aecd3d`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/1aecd3d2fc5dd1e0488cc84c823796a46be8f2a1))

- **auth**: Stop the unwired browser-PKCE provider from stalling login ([#86](https://github.com/OpenAdaptAI/openadapt-desktop/pull/86), [`5b8e6c4`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/5b8e6c480a485e9ca3c0eb4afa10f2f4dfb1f4fb))

- **auth**: Supersede stale rotation recovery ([#90](https://github.com/OpenAdaptAI/openadapt-desktop/pull/90), [`1aecd3d`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/1aecd3d2fc5dd1e0488cc84c823796a46be8f2a1))

- **auth**: Verify staged rotation replacements ([#90](https://github.com/OpenAdaptAI/openadapt-desktop/pull/90), [`1aecd3d`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/1aecd3d2fc5dd1e0488cc84c823796a46be8f2a1))

- **ci**: Let the capture-floor gate run without the bundled Flow installed ([#81](https://github.com/OpenAdaptAI/openadapt-desktop/pull/81), [`8204c45`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/8204c4554f71e53eaa53f3c47dbebcb8e8ea5a1f))

- **installer**: Ship a capture the bundled Flow will actually accept ([#81](https://github.com/OpenAdaptAI/openadapt-desktop/pull/81), [`8204c45`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/8204c4554f71e53eaa53f3c47dbebcb8e8ea5a1f))

- **packaging**: Freeze the attended console the decision portal needs ([#78](https://github.com/OpenAdaptAI/openadapt-desktop/pull/78), [`ccd608d`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/ccd608d90cbc3a7b6ae865859d7735aa42055ae8))

- **policy**: Bind the resolved safety policy to dispatched runs ([#76](https://github.com/OpenAdaptAI/openadapt-desktop/pull/76), [`0ceae7c`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/0ceae7ceca93db120e188d65db995d5b68fd2f08))

- **portal**: Bind the console to a deployment target and bound its startup ([#78](https://github.com/OpenAdaptAI/openadapt-desktop/pull/78), [`ccd608d`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/ccd608d90cbc3a7b6ae865859d7735aa42055ae8))

- **portal**: Close four defects found in adversarial review ([#75](https://github.com/OpenAdaptAI/openadapt-desktop/pull/75), [`3e51a34`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/3e51a347df2d1fb5628edaafba9963156cc27b9d))

- **portal**: Freeze the attended console and wire the decision portal to a deployment target ([#78](https://github.com/OpenAdaptAI/openadapt-desktop/pull/78), [`ccd608d`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/ccd608d90cbc3a7b6ae865859d7735aa42055ae8))

- **portal**: Kill the console process tree and stop deriving temp dirs from pins ([#80](https://github.com/OpenAdaptAI/openadapt-desktop/pull/80), [`6f35409`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/6f354098157f17ad7b1d756318c57f3db3a530ab))

- **portal**: Stop the phone recommending continue, and put the reading on the path to the answer ([#82](https://github.com/OpenAdaptAI/openadapt-desktop/pull/82), [`33827de`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/33827dea54d4d3926bfd9229766b953112c941a6))

- **portal**: Teach the phone the receipt shape and say what actually broke ([#79](https://github.com/OpenAdaptAI/openadapt-desktop/pull/79), [`3299115`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/329911578a3e23468593ba4e37ea5f465a880f2d))

- **release**: Mirror the attested installer set onto the engine release ([#73](https://github.com/OpenAdaptAI/openadapt-desktop/pull/73), [`92a56db`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/92a56db30d3288eff816d53c23065dc99d04d874))

- **release**: Point "Latest" at the native installers ([#69](https://github.com/OpenAdaptAI/openadapt-desktop/pull/69), [`3cb7adb`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/3cb7adbc7e8a3bb6ec032dcfa5f725ab807a2902))

- **scrubber**: Refuse a scrub that could not run instead of reporting zero redactions ([#87](https://github.com/OpenAdaptAI/openadapt-desktop/pull/87), [`d918b63`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/d918b6315860e16dd8b025f3c885ea50b2913b47))

- **test**: Read portal shell assets as UTF-8 ([#77](https://github.com/OpenAdaptAI/openadapt-desktop/pull/77), [`571b338`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/571b338ccfa5c87e9d844d106439ebe1a4f1607c))

### Build System

- Bump the cargo-minor group in /src-tauri with 3 updates ([#52](https://github.com/OpenAdaptAI/openadapt-desktop/pull/52), [`c541d2c`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/c541d2c6df6f03e63d5f672ad85362c3a69ac8e1))

### Chores

- Gitignore .private/ ([#83](https://github.com/OpenAdaptAI/openadapt-desktop/pull/83), [`590eb28`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/590eb28fa6149483d11203c67d3c0b922109a888))

- **native**: Sync native installer version to 0.14.0 ([`778c838`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/778c8384369c75b31c24c1de92c9597c0e09b469))

- **release**: Enforce source-policy boundary ([`f677d4c`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/f677d4c8b9b22c693d15ba0b7364f3f0f7687aa0))

### Continuous Integration

- Bump the actions group with 9 updates ([#56](https://github.com/OpenAdaptAI/openadapt-desktop/pull/56), [`f47ed27`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/f47ed27f4cd0cd2a253cb492dd7184d2aab2115c))

- Detect unreleased work and silently skipped publishes ([#70](https://github.com/OpenAdaptAI/openadapt-desktop/pull/70), [`df419f1`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/df419f13d8dcfdad9597e371f14a421884b4429a))

- Keep docs dispatch on proven v3 ([#65](https://github.com/OpenAdaptAI/openadapt-desktop/pull/65), [`9a4ad67`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/9a4ad67b79322a2da0f39567c78c97d812e58c3b))

- Move full platform matrices to exact main ([#68](https://github.com/OpenAdaptAI/openadapt-desktop/pull/68), [`ecccd4a`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/ecccd4abc5bff73fe6320ea023da791ed0a6f29d))

- Rebuild artifacts for engine changes ([#89](https://github.com/OpenAdaptAI/openadapt-desktop/pull/89), [`0996480`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/09964803cb0c5e75a329c7cf11eb0238009bfd81))

- Rebuild docs only for public documentation changes ([#68](https://github.com/OpenAdaptAI/openadapt-desktop/pull/68), [`ecccd4a`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/ecccd4abc5bff73fe6320ea023da791ed0a6f29d))

- Reserve full desktop platform matrices for exact main ([#68](https://github.com/OpenAdaptAI/openadapt-desktop/pull/68), [`ecccd4a`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/ecccd4abc5bff73fe6320ea023da791ed0a6f29d))

- Run capability tests with bundled Flow ([#74](https://github.com/OpenAdaptAI/openadapt-desktop/pull/74), [`c887890`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/c8878906f58066cb73dbb60912dac9017cf4adde))

- Skip duplicate desktop artifact matrices ([#89](https://github.com/OpenAdaptAI/openadapt-desktop/pull/89), [`0996480`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/09964803cb0c5e75a329c7cf11eb0238009bfd81))

### Documentation

- **portal**: Record the reject answer and why it is not escalate ([#84](https://github.com/OpenAdaptAI/openadapt-desktop/pull/84), [`89dde2c`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/89dde2c1665d1806b63021cf395701a701f5a50c))

### Features

- Capability-aware surface availability with precise remediation (Section 6) ([#64](https://github.com/OpenAdaptAI/openadapt-desktop/pull/64), [`e424496`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/e4244961db1f61a2426ea51e0ca9dd8c3a5b3f09))

- Compile recordings automatically after stop ([#88](https://github.com/OpenAdaptAI/openadapt-desktop/pull/88), [`e3257f7`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/e3257f781b84354f78a5d5fd9ba5c618ceeec45c))

- Complete qualification and attended phone workflow ([`99401a1`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/99401a1f415cf7cc2e7aa298231ddf109dd67b17))

- Complete qualification cockpit lifecycle ([#66](https://github.com/OpenAdaptAI/openadapt-desktop/pull/66), [`7218691`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/7218691f96baee67ee36124e98c90f1a061d4cd0))

- **auth**: Add loopback pairing and credential rotation ([#90](https://github.com/OpenAdaptAI/openadapt-desktop/pull/90), [`1aecd3d`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/1aecd3d2fc5dd1e0488cc84c823796a46be8f2a1))

- **portal**: Add the runner-local mobile decision portal and QR pairing ([#75](https://github.com/OpenAdaptAI/openadapt-desktop/pull/75), [`3e51a34`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/3e51a347df2d1fb5628edaafba9963156cc27b9d))

- **portal**: Render the reject answer and keep the action bar usable ([#84](https://github.com/OpenAdaptAI/openadapt-desktop/pull/84), [`89dde2c`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/89dde2c1665d1806b63021cf395701a701f5a50c))

### Testing

- Verify automatic compile handoff ([#88](https://github.com/OpenAdaptAI/openadapt-desktop/pull/88), [`e3257f7`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/e3257f781b84354f78a5d5fd9ba5c618ceeec45c))

- **portal**: Classify the attended-session host from Flow's own refusal ([#78](https://github.com/OpenAdaptAI/openadapt-desktop/pull/78), [`ccd608d`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/ccd608d90cbc3a7b6ae865859d7735aa42055ae8))


## v0.14.0 (2026-07-26)

### Bug Fixes

- Keep overlay assurance labels exact ([#63](https://github.com/OpenAdaptAI/openadapt-desktop/pull/63), [`223c39f`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/223c39f2664606408990d60604d62ee15309c6e6))

### Chores

- **native**: Sync native installer version to 0.13.1 ([`1ab92d1`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/1ab92d10d0a0dea237aecdfd25ee5d7ec1179bb9))

### Features

- Post-compose exact overlay presentation videos ([#63](https://github.com/OpenAdaptAI/openadapt-desktop/pull/63), [`223c39f`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/223c39f2664606408990d60604d62ee15309c6e6))


## v0.13.1 (2026-07-25)

### Bug Fixes

- Label Desktop CLI as Beta ([#62](https://github.com/OpenAdaptAI/openadapt-desktop/pull/62), [`fe3e5ee`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/fe3e5eee78ab70d00a03ff81976089ed6503a52a))

### Chores

- **native**: Sync native installer version to 0.13.0 ([`f9d6a3c`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/f9d6a3c45e7753e720a7e5a346018ba652cfe679))


## v0.13.0 (2026-07-25)

### Chores

- **native**: Sync native installer version to 0.12.1 ([`67a51ee`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/67a51ee1aad9bcbe7b630c157b11f3b4e52a5f0b))

### Features

- Add privacy-safe automation control overlay ([`fb19d51`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/fb19d51c0335b600d995042eac400d5d8c76adf3))


## v0.12.1 (2026-07-25)

### Bug Fixes

- Make macOS Intel crypto runtime self-contained ([`394c9e2`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/394c9e2e5dbe3eeca68e403223a5317d75d97cae))

### Chores

- **native**: Sync native installer version to 0.12.0 ([`72f7c76`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/72f7c76f26378b0f857e1fb4d1500a51adef9e30))


## v0.12.0 (2026-07-25)

### Bug Fixes

- Accept verified reconciled Flow outcomes ([`2a0ae1e`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/2a0ae1e341d47c00030fc01d2dfc8072f9f15714))

- Honor precise Flow execution outcomes ([`a99f27f`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/a99f27f2e18071a0d61fa33f89f1e0ea7273aa37))

- Keep in-flight outcomes unverified ([`9db8b3a`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/9db8b3a840e3dba644e09686e7b82fa4e258f8bd))

- Make dependency audits self-contained ([#48](https://github.com/OpenAdaptAI/openadapt-desktop/pull/48), [`3d6f361`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/3d6f36115d063d9b9f0285917ecc0c40198bfb09))

- Reject verified compensated outcomes ([`5091071`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/5091071b95a6e2e9cbaca247a38c56af1a402b33))

- Revalidate retained precise outcomes ([`2ef5953`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/2ef5953237ed995b3bda64cac9f1ec97f63d66c3))

- Validate precise Flow outcome envelopes ([`bc91fdb`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/bc91fdbd0e8da4156223280c91b38137a101e3a0))

### Chores

- **native**: Sync native installer version to 0.11.0 ([`d433ba6`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/d433ba64cecb7dca414b92f0942c35aa81e6d855))

### Features

- Bundle Flow 1.23 precise outcomes ([`968e54c`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/968e54c09d22c66c9f30110936a1a70babfed6ae))

- Complete context identity cockpit ([`a346109`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/a3461095cdff09cf4df3060fab436d8cb4363ae7))

- Publish SBOM and add desktop security gates ([#48](https://github.com/OpenAdaptAI/openadapt-desktop/pull/48), [`3d6f361`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/3d6f36115d063d9b9f0285917ecc0c40198bfb09))


## v0.11.0 (2026-07-25)

### Chores

- **native**: Sync native installer version to 0.10.0 ([`d1af5d2`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/d1af5d229b43e76321f36862fb43c6df27c66241))

### Features

- Complete qualification identity and effect editing ([#47](https://github.com/OpenAdaptAI/openadapt-desktop/pull/47), [`6ea7666`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/6ea76662b628bb8494ac21b977c0f86517a1c021))


## v0.10.0 (2026-07-25)

### Bug Fixes

- Align qualified effect risk with action review ([#45](https://github.com/OpenAdaptAI/openadapt-desktop/pull/45), [`372c6ee`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/372c6ee37a5765c57967416da6a319d2185a3b3d))

- Close desktop target lifecycle gaps ([#43](https://github.com/OpenAdaptAI/openadapt-desktop/pull/43), [`8dae91a`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/8dae91abcb8fd3da140c31cdede4beb211a722df))

- Complete native and remote authoring targets ([#43](https://github.com/OpenAdaptAI/openadapt-desktop/pull/43), [`8dae91a`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/8dae91abcb8fd3da140c31cdede4beb211a722df))

- Fail closed across desktop execution targets ([#43](https://github.com/OpenAdaptAI/openadapt-desktop/pull/43), [`8dae91a`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/8dae91abcb8fd3da140c31cdede4beb211a722df))

- Keep qualification certification fail-closed ([#44](https://github.com/OpenAdaptAI/openadapt-desktop/pull/44), [`66ad7cf`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/66ad7cf5653b092030d9cabb9b6fb03de3095549))

- Name irreversible risk precisely ([#44](https://github.com/OpenAdaptAI/openadapt-desktop/pull/44), [`66ad7cf`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/66ad7cf5653b092030d9cabb9b6fb03de3095549))

- Pin canonical qualification runtime ([#46](https://github.com/OpenAdaptAI/openadapt-desktop/pull/46), [`20ef526`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/20ef526c1e5f77972561896950ea07428c601136))

### Chores

- **native**: Sync native installer version to 0.9.0 ([`7ab2c0e`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/7ab2c0e9b01b61e87bacad02b7c75bd44681b189))

### Features

- Add first writable qualification cockpit slice ([#44](https://github.com/OpenAdaptAI/openadapt-desktop/pull/44), [`66ad7cf`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/66ad7cf5653b092030d9cabb9b6fb03de3095549))

- Add native and remote execution targets ([#43](https://github.com/OpenAdaptAI/openadapt-desktop/pull/43), [`8dae91a`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/8dae91abcb8fd3da140c31cdede4beb211a722df))

- Add writable qualification cockpit slice ([#44](https://github.com/OpenAdaptAI/openadapt-desktop/pull/44), [`66ad7cf`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/66ad7cf5653b092030d9cabb9b6fb03de3095549))

- Author identity and effect qualification contracts ([#45](https://github.com/OpenAdaptAI/openadapt-desktop/pull/45), [`372c6ee`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/372c6ee37a5765c57967416da6a319d2185a3b3d))

- Consume canonical Flow qualification projects ([#46](https://github.com/OpenAdaptAI/openadapt-desktop/pull/46), [`20ef526`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/20ef526c1e5f77972561896950ea07428c601136))

- Consume canonical qualification projects ([#46](https://github.com/OpenAdaptAI/openadapt-desktop/pull/46), [`20ef526`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/20ef526c1e5f77972561896950ea07428c601136))

### Testing

- Decode frontend sources as UTF-8 ([#43](https://github.com/OpenAdaptAI/openadapt-desktop/pull/43), [`8dae91a`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/8dae91abcb8fd3da140c31cdede4beb211a722df))

- Respect Windows private-file semantics ([#43](https://github.com/OpenAdaptAI/openadapt-desktop/pull/43), [`8dae91a`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/8dae91abcb8fd3da140c31cdede4beb211a722df))


## v0.9.0 (2026-07-24)

### Bug Fixes

- Bundle native runtime notices ([`7eb2fa7`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/7eb2fa74489d14f7a48053d91c8f96fbabe2d6fd))

- Collapse represented inventory separators ([`7eb2fa7`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/7eb2fa74489d14f7a48053d91c8f96fbabe2d6fd))

- Complete runtime smoke dependencies ([`e420cd0`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/e420cd0b4d54df0db6d050dfbe81de085740dcac))

- Complete runtime verification ([`ee68ccb`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/ee68ccbd97e0d7361b18a658fbe065ad99f1ee49))

- Enforce frozen dependency license boundary ([`c7bcaa4`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/c7bcaa46ba8c960b461ec53efa0db4b4d2061015))

- Exclude unpinned Linux libgcc runtime ([`c7bcaa4`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/c7bcaa46ba8c960b461ec53efa0db4b4d2061015))

- Gate mac recording on input monitoring ([`c7bcaa4`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/c7bcaa46ba8c960b461ec53efa0db4b4d2061015))

- Isolate PyInstaller bootloader license boundary ([`c7bcaa4`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/c7bcaa46ba8c960b461ec53efa0db4b4d2061015))

- Keep publication guard build-extra free ([`c7bcaa4`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/c7bcaa46ba8c960b461ec53efa0db4b4d2061015))

- Make FFmpeg runtime build portable ([`3adeffe`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/3adeffefa2b9a6e3c8451f2d2d033ba4b33fd408))

- Make recorder startup transactional ([`c7bcaa4`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/c7bcaa46ba8c960b461ec53efa0db4b4d2061015))

- Make recording finalization fail closed ([`c7bcaa4`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/c7bcaa46ba8c960b461ec53efa0db4b4d2061015))

- Mirror capture runtime contract ([`fe194a1`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/fe194a1ab7a5f7e728a608e9cc6f71e7d27af1a0))

- Normalize frozen inventory paths ([`7eb2fa7`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/7eb2fa74489d14f7a48053d91c8f96fbabe2d6fd))

- Preserve macOS Intel OCR support ([`7eb2fa7`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/7eb2fa74489d14f7a48053d91c8f96fbabe2d6fd))

- Refuse false successful native recordings ([`c7bcaa4`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/c7bcaa46ba8c960b461ec53efa0db4b4d2061015))

- Request macOS input monitoring from onboarding ([`c7bcaa4`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/c7bcaa46ba8c960b461ec53efa0db4b4d2061015))

- Secure managed runtime boundaries ([`c7bcaa4`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/c7bcaa46ba8c960b461ec53efa0db4b4d2061015))

- Serialize managed runtime activation ([`c7bcaa4`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/c7bcaa46ba8c960b461ec53efa0db4b4d2061015))

### Chores

- **native**: Sync native installer version to 0.8.0 ([`b37c34d`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/b37c34d5b3d8c66f32552d482636aeee6ce55f1f))

### Continuous Integration

- Auto-run native build/attest, gate only the publish step ([#31](https://github.com/OpenAdaptAI/openadapt-desktop/pull/31), [`0dc7234`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/0dc7234f13cdca0490aa1c10cde3b76aa6e3f469))

- Build managed FFmpeg runtime assets ([#41](https://github.com/OpenAdaptAI/openadapt-desktop/pull/41), [`144cae3`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/144cae3ccc6f5341dbed5642bb2b95d0bc178d11))

- Gate Desktop releases on green main ([#39](https://github.com/OpenAdaptAI/openadapt-desktop/pull/39), [`8e00ae0`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/8e00ae036c2d2836929f2d799793d3b688913abf))

- Harden runtime source build portability ([#41](https://github.com/OpenAdaptAI/openadapt-desktop/pull/41), [`144cae3`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/144cae3ccc6f5341dbed5642bb2b95d0bc178d11))

### Documentation

- Refresh README for the reconciled cockpit and wired engine sidecar ([#37](https://github.com/OpenAdaptAI/openadapt-desktop/pull/37), [`540f0f0`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/540f0f0208b762d5d860530a36ed3150ad245caf))

### Features

- Bundle OpenAdapt Flow 1.20.0 ([`c7bcaa4`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/c7bcaa46ba8c960b461ec53efa0db4b4d2061015))

- Promote native installer lane to beta ([`7eb2fa7`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/7eb2fa74489d14f7a48053d91c8f96fbabe2d6fd))

- Ship Flow 1.20.1 with verified managed runtimes ([`c7bcaa4`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/c7bcaa46ba8c960b461ec53efa0db4b4d2061015))

- Ship verified managed desktop runtimes ([`c7bcaa4`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/c7bcaa46ba8c960b461ec53efa0db4b4d2061015))

### Testing

- Align supersede-job assertion with the no-environment-gate design ([#31](https://github.com/OpenAdaptAI/openadapt-desktop/pull/31), [`0dc7234`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/0dc7234f13cdca0490aa1c10cde3b76aa6e3f469))

- Keep Linux boundary check hermetic ([`c7bcaa4`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/c7bcaa46ba8c960b461ec53efa0db4b4d2061015))

- Make frozen notice checks hermetic ([`c7bcaa4`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/c7bcaa46ba8c960b461ec53efa0db4b4d2061015))


## v0.8.0 (2026-07-21)

### Bug Fixes

- Reconcile engine IPC + flow bridge with the cockpit frontend contract ([#36](https://github.com/OpenAdaptAI/openadapt-desktop/pull/36), [`3d4a568`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/3d4a56826691d103efa5e5fb109e029dc7b9e0ad))

### Chores

- **native**: Sync native installer version to 0.7.0 ([`c4b2201`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/c4b2201a0425bac7d11d228b133aa3377bd07bdc))

### Code Style

- Use a plain hyphen for the empty run-step target placeholder ([#36](https://github.com/OpenAdaptAI/openadapt-desktop/pull/36), [`3d4a568`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/3d4a56826691d103efa5e5fb109e029dc7b9e0ad))

### Features

- Add fail-closed effective-policy fetch + cache + dispatcher commands ([#36](https://github.com/OpenAdaptAI/openadapt-desktop/pull/36), [`3d4a568`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/3d4a56826691d103efa5e5fb109e029dc7b9e0ad))

- Add read-only "Grounding model" section to desktop Settings ([#36](https://github.com/OpenAdaptAI/openadapt-desktop/pull/36), [`3d4a568`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/3d4a56826691d103efa5e5fb109e029dc7b9e0ad))

- Reconcile cockpit frontend, wired engine, and fail-closed policy sync onto main ([#36](https://github.com/OpenAdaptAI/openadapt-desktop/pull/36), [`3d4a568`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/3d4a56826691d103efa5e5fb109e029dc7b9e0ad))


## v0.7.0 (2026-07-20)

### Chores

- Bump native version to 0.6.2 for the launch-panic fix release ([#28](https://github.com/OpenAdaptAI/openadapt-desktop/pull/28), [`2cd2385`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/2cd2385a14f45b8e23026553b40571024e195cc0))

- Sync package-lock.json to 0.6.2 ([#28](https://github.com/OpenAdaptAI/openadapt-desktop/pull/28), [`2cd2385`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/2cd2385a14f45b8e23026553b40571024e195cc0))

### Documentation

- Refresh README release pointer and canonical evidence wording ([#29](https://github.com/OpenAdaptAI/openadapt-desktop/pull/29), [`8936c12`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/8936c12cbda7a65d9783562ede78c07acf7830a9))

### Features

- Add Azure Trusted Signing path and founder code-signing runbook ([#30](https://github.com/OpenAdaptAI/openadapt-desktop/pull/30), [`224f235`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/224f2350b4317f0408e68ef154c0f98079ba07e9))


## v0.6.2 (2026-07-19)

### Bug Fixes

- Gate updater plugin on config presence so packaged apps launch (#26) ([#27](https://github.com/OpenAdaptAI/openadapt-desktop/pull/27), [`47fe489`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/47fe48995feab87b9f04e45b59c425f1a75662cd))

### Chores

- **native**: Sync native installer version to 0.6.1 ([`4912989`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/49129892025cc5cb7b589e5e7c641f377640441b))


## v0.6.1 (2026-07-19)

### Bug Fixes

- Align native package descriptions ([`81bb37f`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/81bb37fb1823b7b60fa5935cb3a9372b4fb63ef0))

### Chores

- **native**: Sync native installer version to 0.6.0 ([`68481cc`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/68481cc92839f7a3f44e6d606f4af936ca80ec37))


## v0.6.0 (2026-07-19)

### Bug Fixes

- Make desktop pairing transactional ([#23](https://github.com/OpenAdaptAI/openadapt-desktop/pull/23), [`61182cd`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/61182cd81e4a3e90b13329f0d87483452da34b33))

### Chores

- **native**: Sync native installer version to 0.5.1 ([`8c9dad9`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/8c9dad96c362bd9a8dd319947bef01c3553baa0e))

### Features

- Handle secure local pairing links ([#23](https://github.com/OpenAdaptAI/openadapt-desktop/pull/23), [`61182cd`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/61182cd81e4a3e90b13329f0d87483452da34b33))

### Testing

- Bind native artifact assertions to current version ([#23](https://github.com/OpenAdaptAI/openadapt-desktop/pull/23), [`61182cd`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/61182cd81e4a3e90b13329f0d87483452da34b33))


## v0.5.1 (2026-07-18)

### Bug Fixes

- Supersede installers only after publish ([#22](https://github.com/OpenAdaptAI/openadapt-desktop/pull/22), [`4cfffae`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/4cfffaead3fdbc7780bb9272613c0c265491ed70))

### Continuous Integration

- Keep native installer prereleases fresh with engine releases ([#22](https://github.com/OpenAdaptAI/openadapt-desktop/pull/22), [`4cfffae`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/4cfffaead3fdbc7780bb9272613c0c265491ed70))

### Documentation

- Document two-lane release policy and installer selection rule ([#22](https://github.com/OpenAdaptAI/openadapt-desktop/pull/22), [`4cfffae`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/4cfffaead3fdbc7780bb9272613c0c265491ed70))


## v0.5.0 (2026-07-17)

### Documentation

- Refresh Current Status and Known Gaps to post-wiring reality ([#20](https://github.com/OpenAdaptAI/openadapt-desktop/pull/20), [`2609716`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/26097167fd1849a0d7c9ea1b2d7a910dc37cb38b))

### Features

- Experimental runner loop for cloud-dispatched governed runs (P0) ([#21](https://github.com/OpenAdaptAI/openadapt-desktop/pull/21), [`84fd7de`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/84fd7de5acc3614b7d41bf2006a73a81d3ad39ca))


## v0.4.0 (2026-07-17)

### Bug Fixes

- Close mkstemp fd in zip_dir so push works on Windows ([#11](https://github.com/OpenAdaptAI/openadapt-desktop/pull/11), [`160ae42`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/160ae4275d104fc465913d043013da3c8704b0e1))

- Keep the updater feed disabled until a signing-key lifecycle exists ([#10](https://github.com/OpenAdaptAI/openadapt-desktop/pull/10), [`65c037f`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/65c037fa4665c7b936fcfae6e501d1f256ec7f3b))

### Continuous Integration

- Build the engine sidecar before Tauri bundling; rustfmt fixes ([#10](https://github.com/OpenAdaptAI/openadapt-desktop/pull/10), [`65c037f`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/65c037fa4665c7b936fcfae6e501d1f256ec7f3b))

- Ship Experimental native installer pipeline ([#16](https://github.com/OpenAdaptAI/openadapt-desktop/pull/16), [`3afadc7`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/3afadc7876b79101267395151985d9893ac09344))

### Features

- Build Tauri shell + Vite/React cockpit on the design system ([#10](https://github.com/OpenAdaptAI/openadapt-desktop/pull/10), [`65c037f`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/65c037fa4665c7b936fcfae6e501d1f256ec7f3b))

- Rewire engine for hosted loop (auth, ingest push, flow bridge) ([#11](https://github.com/OpenAdaptAI/openadapt-desktop/pull/11), [`160ae42`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/160ae4275d104fc465913d043013da3c8704b0e1))

- Tauri shell + Vite/React cockpit on the design system (W2) ([#10](https://github.com/OpenAdaptAI/openadapt-desktop/pull/10), [`65c037f`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/65c037fa4665c7b936fcfae6e501d1f256ec7f3b))

- Wire the local loop (IPC handlers, tray socket server, keychain) ([#11](https://github.com/OpenAdaptAI/openadapt-desktop/pull/11), [`160ae42`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/160ae4275d104fc465913d043013da3c8704b0e1))


## v0.3.2 (2026-07-15)

### Bug Fixes

- Remove stale package version claim ([`da148d7`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/da148d74625121875489ffaae57a6c6bd72bad06))


## v0.3.1 (2026-07-15)

### Bug Fixes

- Keep desktop releases version-consistent ([`30f320e`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/30f320e84dbb4b34e19a2772e9aa80888258cb25))


## v0.3.0 (2026-07-15)

### Features

- Align desktop with the hosted workflow loop
  ([`99fee01`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/99fee010cb3422cb2118589adc4c998f0a8dfbd4))

Publish the Experimental Python authoring surface and unsigned CI shells while keeping signed
  installers, updater signing, and production distribution explicitly unsupported.


## v0.2.0 (2026-03-04)

### Features

- Add `openadapt doctor` command and fix audit/wormhole bugs
  ([#8](https://github.com/OpenAdaptAI/openadapt-desktop/pull/8),
  [`daaf643`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/daaf6437f02ebc43b07ea5da1001ec5be9d666f3))

Add diagnostic command that checks all dependencies and configuration: - Python version, data
  directory writability, database connectivity - Core deps: openadapt-capture, openadapt-privacy,
  psutil - Optional deps: boto3, huggingface_hub, magic-wormhole - Backend credentials (S3 keys, HF
  token) when configured - Shows actionable install instructions for missing dependencies

Bug fixes: - audit.py: create parent directory before writing log entries - wormhole.py: use
  subprocess.run() instead of Popen to await completion and capture exit code properly

Co-authored-by: Claude Opus 4.6 <noreply@anthropic.com>


## v0.1.1 (2026-03-04)

### Bug Fixes

- Replace deprecated macos-13 runner with macos-14
  ([#7](https://github.com/OpenAdaptAI/openadapt-desktop/pull/7),
  [`eeb9052`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/eeb9052db7f05452e774dea2ef26edca131602ed))

* fix: replace deprecated macos-13 runner with macos-14 in build workflow

macos-13 runners have been deprecated by GitHub Actions, causing the Build Python Sidecar job to
  fail on every PR. Both macOS targets now use macos-14 (Apple Silicon), which supports x86_64
  builds via Rosetta.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>

* fix: flaky concurrent reads test and deprecated macos-13 runner

- test_concurrent_reads: use separate IndexDB connections per thread (WAL concurrent reads require
  separate connections, not a shared one) - build.yml: replace macos-13 with macos-14 (deprecated
  runner)

---------

Co-authored-by: Claude Opus 4.6 <noreply@anthropic.com>

### Continuous Integration

- Add automated release workflow with python-semantic-release
  ([#6](https://github.com/OpenAdaptAI/openadapt-desktop/pull/6),
  [`95d52d7`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/95d52d7157dbddd7afbcc5afbe1bd2100e9263c4))

Add release.yml workflow triggered on push to main that: - Runs python-semantic-release v9.15.2 to
  determine version bumps from conventional commit messages (feat=minor, fix/perf=patch) - Builds
  with uv and publishes to PyPI (trusted publishing) - Creates GitHub releases with changelogs -
  Uses ADMIN_TOKEN to push through branch protection - Skips semantic-release's own commits to
  prevent infinite loops

Also adds semantic_release config to pyproject.toml with version_toml + version_variables for dual
  version tracking (pyproject.toml + engine/__init__.py).

Co-authored-by: Claude Opus 4.6 <noreply@anthropic.com>


## v0.1.0 (2026-03-03)

### Bug Fixes

- Add hatch build config, fix lint errors and workflow ordering
  ([#1](https://github.com/OpenAdaptAI/openadapt-desktop/pull/1),
  [`01964f7`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/01964f7570fe118c75e5361474a91e32755b8504))

- Add [tool.hatch.build.targets.wheel] packages = ["engine"] so hatchling can find the Python
  package - Fix ruff import sorting in test_backends.py and test_scrubber.py - Remove unused
  StorageBackend import from test_backends.py - Move Xvfb setup step before test run in CI workflow
  - Add uv.lock for reproducible builds

Co-authored-by: Claude Opus 4.6 <noreply@anthropic.com>

- Remove premature screenshot generation and Playwright UI tests
  ([#3](https://github.com/OpenAdaptAI/openadapt-desktop/pull/3),
  [`0791216`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/07912161401b47a87d03b0867b7d501cfdc80f99))

Remove: - screenshots/ (fake screenshots of non-functional placeholder HTML) -
  scripts/generate_screenshots.py (generates screenshots of stubs) - tests/test_e2e/test_ui.py
  (Playwright tests against placeholder HTML) - tests/test_e2e/test_screenshots.py (tests for the
  screenshot generator) - pytest-playwright dependency - Xvfb CI step

Keep: - tests/test_e2e/test_ipc.py (tests real IPC protocol code) - All engine tests (test real
  business logic) - README (without screenshot section) - CLAUDE.md

Co-authored-by: Claude Opus 4.6 <noreply@anthropic.com>

### Features

- Add docs sync trigger ([#4](https://github.com/OpenAdaptAI/openadapt-desktop/pull/4),
  [`c52ea08`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/c52ea081f0fcc57d552f21fd808ed4d6cc493a3f))

- Add README, screenshots, e2e tests, and automated screenshot generation
  ([#2](https://github.com/OpenAdaptAI/openadapt-desktop/pull/2),
  [`ca0f35c`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/ca0f35cb90b4c21f828d0a27686db2c8bafee8a8))

* docs: add README and CLAUDE.md

- Comprehensive README with architecture, state machine, storage backends, project structure,
  configuration reference, and development guide - CLAUDE.md with project conventions and file map

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>

* feat: add screenshots, e2e tests, and automated screenshot generation

- Add Playwright-based UI tests for all HTML pages (dashboard, review, settings) - Add IPC protocol
  e2e tests (response format, error handling, event format) - Add automated screenshot generation
  script with mock data injection - Generate 4 documentation screenshots (idle, recording, review,
  settings) - Add screenshots to README with raw.githubusercontent.com URLs - Add pytest-playwright
  to dev dependencies - Update CI to install Playwright browsers - Update ruff config to allow long
  lines in scripts/ (inline HTML)

52 tests passing (26 engine + 7 IPC + 5 screenshot + 11 UI + 3 viewport)

---------

Co-authored-by: Claude Opus 4.6 <noreply@anthropic.com>

- Implement end-to-end engine with CLI
  ([#5](https://github.com/OpenAdaptAI/openadapt-desktop/pull/5),
  [`61f575b`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/61f575bb0a18e73b37b221c9aca081555e88df86))

* feat: implement end-to-end engine with CLI

Replace all NotImplementedError stubs with working implementations across the entire Python engine.
  The full pipeline now works: record -> scrub -> review -> upload.

New files: - engine/db.py: SQLite index database (WAL mode, captures + upload_jobs) - engine/cli.py:
  13-command argparse CLI entry point - engine/__main__.py: python -m engine support -
  tests/test_engine/test_db.py: 11 database tests - tests/test_engine/test_upload.py: 7 upload
  manager tests - tests/test_engine/test_monitor.py: 5 health monitor tests -
  tests/test_engine/test_cli.py: 8 CLI tests - tests/test_e2e/test_pipeline.py: 3 end-to-end
  pipeline tests

Implemented modules: - controller.py: recording lifecycle with openadapt-capture, crash recovery -
  scrubber.py: regex PII scrubbing (email/CC/SSN/phone/IP), Presidio fallback - review.py:
  DB-persisted egress gating with audit logging - storage_manager.py: hot/warm/cold tiers, tar.gz
  archival, cleanup - upload_manager.py: persistent queue, egress checks, multi-backend dispatch -
  monitor.py: memory + disk monitoring with daemon threads - backends/s3.py: boto3
  upload/delete/list/verify - backends/huggingface.py: huggingface_hub upload/delete/list/verify -
  backends/wormhole.py: subprocess-based wormhole send

Test results: 106 passed, 0 skipped (up from 33 passed, 17 skipped)

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>

* fix: use uv run prefix in README Quick Start

---------

Co-authored-by: Claude Opus 4.6 <noreply@anthropic.com>

- Initial repo scaffold
  ([`629fa4e`](https://github.com/OpenAdaptAI/openadapt-desktop/commit/629fa4ed5300e0d75505c2f7c5c8a9c67d2eefdd))

Tauri 2.x shell + Python sidecar architecture. See DESIGN.md for comprehensive design document.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
