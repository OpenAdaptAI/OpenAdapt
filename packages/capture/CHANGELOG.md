# CHANGELOG

<!-- version list -->

## Unreleased

### Features

- Add `window_tree` structural observations and a fail-closed authoring
  projector. The raw tree may persist on disk for compile, except password
  and secure-field values. Native AX, UIA, AT-SPI, and ARIA roles map onto
  the types ElementRole enum. The vendor-wire payload is
  `openadapt.authoring.observe/v1` without values, titles, or screenshots.
  The observe fixture is pinned to openadapt-types PR 35. OS-injected input
  still does not persist.
- Add attended authentication handoffs. Capture suppresses sensitive source
  data, seals a bounded method marker, and retains a fresh exact frame before
  native input resumes. The same retry-safe operations are available through
  the authenticated local control channel.

## v1.3.0 (2026-08-28)

_This release is published under the MIT License._

### Features

- Capture an exact macOS window through a desktop-independent
  ScreenCaptureKit filter. Exact-window Quartz and system-utility providers
  remain as compatibility paths.
- **capture**: Seal native action geometry at capture time
  ([#94](https://github.com/OpenAdaptAI/openadapt-capture/pull/94),
  [`08727ab`](https://github.com/OpenAdaptAI/openadapt-capture/commit/08727ab0e471d4a6de3c460099b1a8a97758cf6f))
- Add authenticated recorder control channel, which provides the documented
  `capture status` and `capture stop` commands. Both were documented but
  absent from the 1.2.2 wheel, and they ship here for the first time
  ([#79](https://github.com/OpenAdaptAI/openadapt-capture/pull/79),
  [`6109678`](https://github.com/OpenAdaptAI/openadapt-capture/commit/61096788a623a4a302a69a363215dad81fb9008b))
- Bind each action to its exact retained screen frame
  ([#84](https://github.com/OpenAdaptAI/openadapt-capture/pull/84),
  [`fb4c36b`](https://github.com/OpenAdaptAI/openadapt-capture/commit/fb4c36bdcaaea7ab001e2fd8472c23bc00c92c0e))
- Capture native structural observations
  ([#96](https://github.com/OpenAdaptAI/openadapt-capture/pull/96),
  [`7a96690`](https://github.com/OpenAdaptAI/openadapt-capture/commit/7a96690bf8684be3f61fb7c14d783813003e221e))
- Install FFmpeg with one command, `capture install-ffmpeg`. The wheel and
  the source archive carry no FFmpeg bytes and nothing downloads unless the
  operator runs that command, which fetches one digest-pinned LGPL-2.1-or-later
  build
  ([#118](https://github.com/OpenAdaptAI/openadapt-capture/pull/118),
  [`9701237`](https://github.com/OpenAdaptAI/openadapt-capture/commit/9701237a89c9b939f6978dc6c88c4ccdba09b02e))
- Qualify resilient native capture
  ([#78](https://github.com/OpenAdaptAI/openadapt-capture/pull/78),
  [`b04e829`](https://github.com/OpenAdaptAI/openadapt-capture/commit/b04e8292aad9b2df36bcd9c63fe9ea0c04d5b760))
- Ship the two demo captures as loadable fixtures
  ([#101](https://github.com/OpenAdaptAI/openadapt-capture/pull/101),
  [`ad1aaf1`](https://github.com/OpenAdaptAI/openadapt-capture/commit/ad1aaf1467b5e17a0bb1cf404976a3f1c0fb0e2a))

### Bug Fixes

- Refuse ambiguous macOS window selectors instead of choosing a different
  matching window.
- Keep FFmpeg outside the recorder's interrupt process group so Ctrl-C can
  finish the video trailer.
- Ignore unattributable macOS modifier-flag events without terminating the
  input observer.
- Report failed and finalizing sessions with `ready: false`, plus a stable
  failure stage and error code.
- **ci**: Allowlist the published extension key by exact value
  ([#112](https://github.com/OpenAdaptAI/openadapt-capture/pull/112),
  [`69a123f`](https://github.com/OpenAdaptAI/openadapt-capture/commit/69a123fccfa6dcbf848e486301832cc826ab679a))
- **db**: Retry transient SQLite writer locks
  ([#106](https://github.com/OpenAdaptAI/openadapt-capture/pull/106),
  [`d560e45`](https://github.com/OpenAdaptAI/openadapt-capture/commit/d560e45b1ef7e26be0044536891999f411bbfd18))
- **fixtures**: Bind synthetic provenance to the fixture byte determinants
  ([#107](https://github.com/OpenAdaptAI/openadapt-capture/pull/107),
  [`d00afdd`](https://github.com/OpenAdaptAI/openadapt-capture/commit/d00afdd1eb275d9a73c9baeafa8b492da5bfd67d))
- **recorder**: Preserve the capture clock epoch
  ([`97d7ca4`](https://github.com/OpenAdaptAI/openadapt-capture/commit/97d7ca404a8f1ef06d71c80283225569c1856178))
- **recorder**: Survive SQLite writer contention in the video writer
  ([#116](https://github.com/OpenAdaptAI/openadapt-capture/pull/116),
  [`1695be7`](https://github.com/OpenAdaptAI/openadapt-capture/commit/1695be789988c36370aba752685b8ccf61330120))
- **db**: Bound the SQLite write-lock wait by time rather than by a count of
  attempts, and give a live capture a write-ahead log so the lock is free far
  more often. A recorder writer used to starve for about twenty-two seconds and
  then exhaust its three retries, which killed the writer process or cost it
  the startup readiness deadline
  ([#122](https://github.com/OpenAdaptAI/openadapt-capture/pull/122))
- **db**: Close each recorder database session before finalization. The setup
  session kept one idle write-log connection open until cyclic garbage
  collection ran, so macOS could fail the final journal change with
  `database is locked` after an otherwise complete capture.
- **release**: Let the changelog document the pending release candidate
  ([#110](https://github.com/OpenAdaptAI/openadapt-capture/pull/110),
  [`854f015`](https://github.com/OpenAdaptAI/openadapt-capture/commit/854f015fd994b0aa1992948791c18c2e6c571029))
- **test**: Do not assert a POSIX execute bit on Windows
  ([#119](https://github.com/OpenAdaptAI/openadapt-capture/pull/119),
  [`9f8672c`](https://github.com/OpenAdaptAI/openadapt-capture/commit/9f8672c86b61ac22e3c98a87980e0aa07ce14408))
- Refuse capture before recorder readiness
  ([#62](https://github.com/OpenAdaptAI/openadapt-capture/pull/62),
  [`3e6b933`](https://github.com/OpenAdaptAI/openadapt-capture/commit/3e6b933f2ccdff72d27bf79aea0c56eb8662f341))
- Replace public recordings with synthetic fixtures
  ([#103](https://github.com/OpenAdaptAI/openadapt-capture/pull/103),
  [`cca59d9`](https://github.com/OpenAdaptAI/openadapt-capture/commit/cca59d9a66ab452be800a3b10ecf6f51316c3900))
- Retain an initial frame before recorder readiness
  ([#100](https://github.com/OpenAdaptAI/openadapt-capture/pull/100),
  [`4cce32b`](https://github.com/OpenAdaptAI/openadapt-capture/commit/4cce32b6ba5f432ad2f7d3cbcf77f3a9dc990215))

### Documentation

- Align entry commands and substrate maturity across repo READMEs
  ([#66](https://github.com/OpenAdaptAI/openadapt-capture/pull/66),
  [`431fd37`](https://github.com/OpenAdaptAI/openadapt-capture/commit/431fd376f1d4968fbee0bfa1b044c32f71f71e8d))
- Correct stale recorder facts and mark the extension a prototype
  ([#76](https://github.com/OpenAdaptAI/openadapt-capture/pull/76),
  [`0799088`](https://github.com/OpenAdaptAI/openadapt-capture/commit/07990886b1e206bcc807ba95f822ef859e9b7325))
- Derive Capture product state from admissions
  ([`e20e65b`](https://github.com/OpenAdaptAI/openadapt-capture/commit/e20e65be0e8affffc4646900d77c70ebf26da9f3))
- Rewrite the README and move the deep contracts into docs/
  ([#111](https://github.com/OpenAdaptAI/openadapt-capture/pull/111),
  [`e5c1261`](https://github.com/OpenAdaptAI/openadapt-capture/commit/e5c1261dc23f6d8b40646f78ac9064489f99bf06))

### Testing

- **fixtures**: Run the byte check on every matching builder
  ([#109](https://github.com/OpenAdaptAI/openadapt-capture/pull/109),
  [`86e6419`](https://github.com/OpenAdaptAI/openadapt-capture/commit/86e64194edc0134a2c4be412c985b5c5b4774e31))
- **recorder**: Isolate observer failure from ffmpeg
  ([#98](https://github.com/OpenAdaptAI/openadapt-capture/pull/98),
  [`7346e20`](https://github.com/OpenAdaptAI/openadapt-capture/commit/7346e201d6d00db1c1a410f9d5e88cfa9d7c7252))
- **terminal**: Accept platform-specific safe rejection
  ([#97](https://github.com/OpenAdaptAI/openadapt-capture/pull/97),
  [`a376ebe`](https://github.com/OpenAdaptAI/openadapt-capture/commit/a376ebecd5d31ec0cba62ebd7483bcb760cea72d))
- Pin the writer shutdown-drain contract
  ([#85](https://github.com/OpenAdaptAI/openadapt-capture/pull/85),
  [`f114669`](https://github.com/OpenAdaptAI/openadapt-capture/commit/f11466908d49c7ca274a8dd4358c54030caad539))

### Continuous Integration

- **windows**: Fail when ffmpeg install is absent
  ([#99](https://github.com/OpenAdaptAI/openadapt-capture/pull/99),
  [`682f77d`](https://github.com/OpenAdaptAI/openadapt-capture/commit/682f77d168bdff60d55285a1682a7c0ebf726847))
- Bound the apt install and prefer the canonical Ubuntu archive
  ([#81](https://github.com/OpenAdaptAI/openadapt-capture/pull/81),
  [`90557ef`](https://github.com/OpenAdaptAI/openadapt-capture/commit/90557efa9fe8e1e4a8091b81c9621f1ee929b25b))
- Bound the trial step and drop the flaking Windows lane from the gate
  ([#115](https://github.com/OpenAdaptAI/openadapt-capture/pull/115),
  [`1c5e732`](https://github.com/OpenAdaptAI/openadapt-capture/commit/1c5e7322f15c482b3c83d9fa96807b257bc06162))
- Qualify releases on hosted runners, schedule the live lanes
  ([#114](https://github.com/OpenAdaptAI/openadapt-capture/pull/114),
  [`ecdd9c0`](https://github.com/OpenAdaptAI/openadapt-capture/commit/ecdd9c026e21eaaf0babcbefa766a40010abc15d))
- Require release App tag publication
  ([#92](https://github.com/OpenAdaptAI/openadapt-capture/pull/92),
  [`681683e`](https://github.com/OpenAdaptAI/openadapt-capture/commit/681683e73be13ce75ee3e8c720cd8ce7fbd7c76a))
- Require three counted live qualification trials per OS
  ([#86](https://github.com/OpenAdaptAI/openadapt-capture/pull/86),
  [`d1f6615`](https://github.com/OpenAdaptAI/openadapt-capture/commit/d1f66151b72fe95967b6f8d2696ab93cf5b49721))
- Run pull-request checks on every base branch
  ([#80](https://github.com/OpenAdaptAI/openadapt-capture/pull/80),
  [`7c83ba2`](https://github.com/OpenAdaptAI/openadapt-capture/commit/7c83ba2802fc93725d6079a69e06e0e0f986f322))

### Chores

- **ci**: Update CodeQL actions atomically to 4.37.8
  ([#104](https://github.com/OpenAdaptAI/openadapt-capture/pull/104),
  [`9f76fc1`](https://github.com/OpenAdaptAI/openadapt-capture/commit/9f76fc134cc5a804ab8790762ec2a40156ef35e6))
- **deps**: Bump actions/download-artifact from 4.3.0 to 8.0.1
  ([#89](https://github.com/OpenAdaptAI/openadapt-capture/pull/89),
  [`25aa082`](https://github.com/OpenAdaptAI/openadapt-capture/commit/25aa082839d030a7da5d16f20776cc8fe5480b7a))
- **deps**: Bump actions/upload-artifact from 4.6.2 to 7.0.1
  ([#87](https://github.com/OpenAdaptAI/openadapt-capture/pull/87),
  [`f294824`](https://github.com/OpenAdaptAI/openadapt-capture/commit/f2948249403b285bc3d247dd51525b135fc7d8d1))
- **deps**: Bump pypa/gh-action-pypi-publish from 1.14.1 to 1.14.2
  ([#67](https://github.com/OpenAdaptAI/openadapt-capture/pull/67),
  [`23e210a`](https://github.com/OpenAdaptAI/openadapt-capture/commit/23e210add80aed456f871c4e74a5a2c121653bce))
- **deps**: Update CodeQL actions together
  ([#75](https://github.com/OpenAdaptAI/openadapt-capture/pull/75),
  [`bcf1294`](https://github.com/OpenAdaptAI/openadapt-capture/commit/bcf12942d61d66b64d94e645e9124273a5cc5963))
- **policy**: Sync generated source boundary
  ([#105](https://github.com/OpenAdaptAI/openadapt-capture/pull/105),
  [`33ed381`](https://github.com/OpenAdaptAI/openadapt-capture/commit/33ed3816b16a963c4603f7b61c1e4dbdd6e4ab8d))
- **release**: Enforce source policy on archives
  ([#63](https://github.com/OpenAdaptAI/openadapt-capture/pull/63),
  [`843e03e`](https://github.com/OpenAdaptAI/openadapt-capture/commit/843e03e72ad9f01fbe20cd886eca0210edf384ff))
- **release**: Prepare 1.2.3
  ([#113](https://github.com/OpenAdaptAI/openadapt-capture/pull/113),
  [`0a15199`](https://github.com/OpenAdaptAI/openadapt-capture/commit/0a1519951de7ebb1719919b099c37509f44385cd))
- **release**: Prepare 1.3.0
  ([#117](https://github.com/OpenAdaptAI/openadapt-capture/pull/117),
  [`2a3ef96`](https://github.com/OpenAdaptAI/openadapt-capture/commit/2a3ef969b28f68278ed0f9a753a34e6248ce4bbd))
- Ignore every .env variant, not one at a time
  ([#93](https://github.com/OpenAdaptAI/openadapt-capture/pull/93),
  [`5ed28e6`](https://github.com/OpenAdaptAI/openadapt-capture/commit/5ed28e6c63e70cdedf32c760f0272841adb1063b))
- Refuse invented capture input and geometry
  ([`a9d0877`](https://github.com/OpenAdaptAI/openadapt-capture/commit/a9d087728e6f789426a8fcc63ef6db2ae66e8216))

**Detailed Changes**: [v1.2.2...v1.3.0](https://github.com/OpenAdaptAI/openadapt-capture/compare/v1.2.2...v1.3.0)

## v1.2.2 (2026-07-28)

_This release is published under the MIT License._

### Bug Fixes

- Stop fabricating display metrics, window data, and UIA evidence
  ([#61](https://github.com/OpenAdaptAI/openadapt-capture/pull/61),
  [`e9e2d82`](https://github.com/OpenAdaptAI/openadapt-capture/commit/e9e2d8247e4cd56c7fc9aa250046852f0233912a))

### Build System

- Keep repository clutter out of source archive
  ([#59](https://github.com/OpenAdaptAI/openadapt-capture/pull/59),
  [`0137ecb`](https://github.com/OpenAdaptAI/openadapt-capture/commit/0137ecb1572c20b95ac379a423bc200120271a26))

### Chores

- Gitignore `.private/`
  ([#60](https://github.com/OpenAdaptAI/openadapt-capture/pull/60),
  [`c0ee3e8`](https://github.com/OpenAdaptAI/openadapt-capture/commit/c0ee3e86be97f47e164c323d2017c9688aa51d19))

### Continuous Integration

- Detect unreleased work and silently skipped publishes
  ([#57](https://github.com/OpenAdaptAI/openadapt-capture/pull/57),
  [`fc549ed`](https://github.com/OpenAdaptAI/openadapt-capture/commit/fc549ed47b3185bc804e725fa2972cd7b2673134))
- Simplify and harden release health
  ([#58](https://github.com/OpenAdaptAI/openadapt-capture/pull/58),
  [`191a352`](https://github.com/OpenAdaptAI/openadapt-capture/commit/191a3523d2a4bff5564377aaf5de72d25b2d84e8))

**Detailed Changes**: [v1.2.1...v1.2.2](https://github.com/OpenAdaptAI/openadapt-capture/compare/v1.2.1...v1.2.2)


## v1.2.1 (2026-07-27)

_This release is published under the MIT License._

### Bug Fixes

- **audio**: Make narration capture on-device only and fail closed
  ([#51](https://github.com/OpenAdaptAI/openadapt-capture/pull/51),
  [`27ccd26`](https://github.com/OpenAdaptAI/openadapt-capture/commit/27ccd2625ddb6d76f81d4d53c52793366aafdddb))

### Chores

- **deps**: Update release and CodeQL actions
  ([#56](https://github.com/OpenAdaptAI/openadapt-capture/pull/56),
  [`22ca9cf`](https://github.com/OpenAdaptAI/openadapt-capture/commit/22ca9cfb36174e28dd18a5b62500a51ad4e34be6))

### Continuous Integration

- Run capture platform integration on exact `main`
  ([#50](https://github.com/OpenAdaptAI/openadapt-capture/pull/50),
  [`f54346c`](https://github.com/OpenAdaptAI/openadapt-capture/commit/f54346c4f40b018e302d1edef8082829251d6b7f))

### Documentation

- Note the PyAV/GPL packaging boundary for the `transcribe-fast` extra
  ([#51](https://github.com/OpenAdaptAI/openadapt-capture/pull/51),
  [`27ccd26`](https://github.com/OpenAdaptAI/openadapt-capture/commit/27ccd2625ddb6d76f81d4d53c52793366aafdddb))

**Detailed Changes**: [v1.2.0...v1.2.1](https://github.com/OpenAdaptAI/openadapt-capture/compare/v1.2.0...v1.2.1)


## v1.2.0 (2026-07-26)

_This release is published under the MIT License._

### Bug Fixes

- Bound native structural evidence
  ([#48](https://github.com/OpenAdaptAI/openadapt-capture/pull/48),
  [`1547f83`](https://github.com/OpenAdaptAI/openadapt-capture/commit/1547f83b01d2e087dcc0f41462e5be8d5b521b82))

### Features

- Preserve keyboard shortcuts in Capture events
  ([#49](https://github.com/OpenAdaptAI/openadapt-capture/pull/49),
  [`3720cea`](https://github.com/OpenAdaptAI/openadapt-capture/commit/3720cea5dad937100c98211213330302cc367664))

**Detailed Changes**: [v1.1.1...v1.2.0](https://github.com/OpenAdaptAI/openadapt-capture/compare/v1.1.1...v1.2.0)


## v1.1.1 (2026-07-25)

_This release is published under the MIT License._

### Bug Fixes

- Preserve observer setup failure at readiness timeout
  ([#47](https://github.com/OpenAdaptAI/openadapt-capture/pull/47),
  [`1b2cbd3`](https://github.com/OpenAdaptAI/openadapt-capture/commit/1b2cbd38227ea67fd2ab9e618123e091039d7f89))
- Preserve readiness timeout on startup cancellation
  ([#47](https://github.com/OpenAdaptAI/openadapt-capture/pull/47),
  [`1b2cbd3`](https://github.com/OpenAdaptAI/openadapt-capture/commit/1b2cbd38227ea67fd2ab9e618123e091039d7f89))
- Preserve setup failure at observer timeout
  ([#47](https://github.com/OpenAdaptAI/openadapt-capture/pull/47),
  [`1b2cbd3`](https://github.com/OpenAdaptAI/openadapt-capture/commit/1b2cbd38227ea67fd2ab9e618123e091039d7f89))

**Detailed Changes**: [v1.1.0...v1.1.1](https://github.com/OpenAdaptAI/openadapt-capture/compare/v1.1.0...v1.1.1)


## v1.1.0 (2026-07-25)

_This release is published under the MIT License._

### Bug Fixes

- Enumerate UIA candidates with supported filters
  ([#45](https://github.com/OpenAdaptAI/openadapt-capture/pull/45),
  [`ba08f0b`](https://github.com/OpenAdaptAI/openadapt-capture/commit/ba08f0b6da322378c165b699129487d41d91334f))
- Make Windows UIA capture use real APIs
  ([#45](https://github.com/OpenAdaptAI/openadapt-capture/pull/45),
  [`ba08f0b`](https://github.com/OpenAdaptAI/openadapt-capture/commit/ba08f0b6da322378c165b699129487d41d91334f))

### Chores

- Remove the generated viewer artifact
  ([#44](https://github.com/OpenAdaptAI/openadapt-capture/pull/44),
  [`031b378`](https://github.com/OpenAdaptAI/openadapt-capture/commit/031b37880f3f09ce91578130cc1a8c4f9dc03075))

### Features

- Capture Windows UIA structural evidence
  ([#45](https://github.com/OpenAdaptAI/openadapt-capture/pull/45),
  [`ba08f0b`](https://github.com/OpenAdaptAI/openadapt-capture/commit/ba08f0b6da322378c165b699129487d41d91334f))

### Testing

- Exercise real Windows UIA observation
  ([#45](https://github.com/OpenAdaptAI/openadapt-capture/pull/45),
  [`ba08f0b`](https://github.com/OpenAdaptAI/openadapt-capture/commit/ba08f0b6da322378c165b699129487d41d91334f))

**Detailed Changes**: [v1.0.4...v1.1.0](https://github.com/OpenAdaptAI/openadapt-capture/compare/v1.0.4...v1.1.0)


## v1.0.4 (2026-07-24)

_This release is published under the MIT License._

### Bug Fixes

- Report FFmpeg input worker startup failure
  ([#43](https://github.com/OpenAdaptAI/openadapt-capture/pull/43),
  [`c11969b`](https://github.com/OpenAdaptAI/openadapt-capture/commit/c11969b7efd2eaaf9981d853fbe62d2bdaddd953))

**Detailed Changes**: [v1.0.3...v1.0.4](https://github.com/OpenAdaptAI/openadapt-capture/compare/v1.0.3...v1.0.4)


## v1.0.3 (2026-07-23)

_This release is published under the MIT License._

### Bug Fixes

- Isolate video codec runtime from capture
  ([#41](https://github.com/OpenAdaptAI/openadapt-capture/pull/41),
  [`dd84934`](https://github.com/OpenAdaptAI/openadapt-capture/commit/dd849344e5b1687b78fc85242912635a08930b4a))
- Propagate video writer contracts and failures
  ([#41](https://github.com/OpenAdaptAI/openadapt-capture/pull/41),
  [`dd84934`](https://github.com/OpenAdaptAI/openadapt-capture/commit/dd849344e5b1687b78fc85242912635a08930b4a))

**Detailed Changes**: [v1.0.2...v1.0.3](https://github.com/OpenAdaptAI/openadapt-capture/compare/v1.0.2...v1.0.3)


## v1.0.2 (2026-07-23)

_This release is published under the MIT License._

### Bug Fixes

- Replace copyleft input dependencies
  ([`f0af5a8`](https://github.com/OpenAdaptAI/openadapt-capture/commit/f0af5a8b13ccc7cdb02e8309f0b0f8ff9fece914))

**Detailed Changes**: [v1.0.1...v1.0.2](https://github.com/OpenAdaptAI/openadapt-capture/compare/v1.0.1...v1.0.2)


## v1.0.1 (2026-07-23)

_This release is published under the MIT License._

### Bug Fixes

- Dispatch docs updates to canonical repository
  ([#38](https://github.com/OpenAdaptAI/openadapt-capture/pull/38),
  [`b65d3ad`](https://github.com/OpenAdaptAI/openadapt-capture/commit/b65d3adc280cc98c9855cdc31f8bf1accde38e17))

### Documentation

- Refresh README to shared OpenAdapt house style
  ([#37](https://github.com/OpenAdaptAI/openadapt-capture/pull/37),
  [`da30acd`](https://github.com/OpenAdaptAI/openadapt-capture/commit/da30acddd7ac52617d45ec09945046e84f0295de))

**Detailed Changes**: [v1.0.0...v1.0.1](https://github.com/OpenAdaptAI/openadapt-capture/compare/v1.0.0...v1.0.1)


## v1.0.0 (2026-07-21)

_This release is published under the MIT License._

### Chores

- Add security CI for CodeQL, Gitleaks, dependency review, and Dependabot
  ([#31](https://github.com/OpenAdaptAI/openadapt-capture/pull/31),
  [`ea786c4`](https://github.com/OpenAdaptAI/openadapt-capture/commit/ea786c45e550ff29bb2ce5caaf67811120506fdb))
- **deps**: Bump `actions/checkout` from 4 to 7
  ([#32](https://github.com/OpenAdaptAI/openadapt-capture/pull/32),
  [`3b5d694`](https://github.com/OpenAdaptAI/openadapt-capture/commit/3b5d694567fffd883094423e40a3be3e92d27b00))
- **deps**: Bump `actions/dependency-review-action` from 4.9.0 to 5.0.0
  ([#33](https://github.com/OpenAdaptAI/openadapt-capture/pull/33),
  [`100f8cf`](https://github.com/OpenAdaptAI/openadapt-capture/commit/100f8cf13701317a1070714ad8198b09027223cd))
- **deps**: Bump `astral-sh/setup-uv` from 4 to 7
  ([#35](https://github.com/OpenAdaptAI/openadapt-capture/pull/35),
  [`2829be5`](https://github.com/OpenAdaptAI/openadapt-capture/commit/2829be5ccc99fb8060f297e04c24e6d7ada09eba))
- **deps**: Bump `peter-evans/repository-dispatch` from 3 to 4
  ([#36](https://github.com/OpenAdaptAI/openadapt-capture/pull/36),
  [`52c6a4f`](https://github.com/OpenAdaptAI/openadapt-capture/commit/52c6a4f62448598e549827086a9461b6bec86fba))
- **deps**: Bump `python-semantic-release/python-semantic-release`
  ([#34](https://github.com/OpenAdaptAI/openadapt-capture/pull/34),
  [`4b1f42b`](https://github.com/OpenAdaptAI/openadapt-capture/commit/4b1f42b9eb879cc045244c738d04613adc4878e3))

**Detailed Changes**: [v0.6.0...v1.0.0](https://github.com/OpenAdaptAI/openadapt-capture/compare/v0.6.0...v1.0.0)


## v0.6.0 (2026-07-18)

### Chores

- Add MIT LICENSE file ([#26](https://github.com/OpenAdaptAI/openadapt-capture/pull/26),
  [`287aec2`](https://github.com/OpenAdaptAI/openadapt-capture/commit/287aec2591022d93e7d40a6cc0e9fc9b297bf770))

pyproject.toml declares license = "MIT" and the README carries an MIT badge, but the repository had
  no LICENSE file, so GitHub license detection reported none. Adds the standard MIT license text
  (copyright OpenAdapt.AI, 2025-2026 per first-commit year).

Co-authored-by: Claude Fable 5 <noreply@anthropic.com>

- Modernize Recorder framing, CI coverage, and dependency floors
  ([#28](https://github.com/OpenAdaptAI/openadapt-capture/pull/28),
  [`6382974`](https://github.com/OpenAdaptAI/openadapt-capture/commit/63829745e0c192578b363dcd6deb438bb0a69a11))

* chore: modernize Recorder framing, CI coverage, and dependency floors

- Reframe the Recorder docstring: it is OpenAdapt's original, hand-built, battle-tested recorder
  (the engine behind openadapt-flow's desktop record backends), not "legacy" code. Documents the
  multiprocessing listener/writer architecture and the headless-import invariant. - CI: add a
  windows-latest job that runs the unit suite plus the live Recorder integration tests
  (tests/test_performance.py, marked slow) -- the live recording path previously ran in NO CI
  anywhere because those tests skip off-Windows and CI was ubuntu-only. Add a macos-latest job
  running the unit suite for platform coverage. - Dependency floors: pynput>=1.7.6, av>=12.0.0,
  Pillow>=10.1.0, numpy>=1.26.0 (previous floors predate cp312 wheels, so they were unsatisfiable on
  the newest supported Python). Full suite verified green against current stable resolutions (av 18,
  Pillow 12, numpy 2.5, SQLAlchemy 2.0.51).

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>

* fix: release SQLite file handles on close (Windows sharing violation)

The new windows-latest CI leg surfaced a real bug invisible on POSIX: CaptureSession.close() closed
  the SQLAlchemy Session but never disposed the engine, so the pool kept the recording.db file
  handle open. On Windows that leaves the capture directory undeletable (WinError 32) -- teardown of
  every test using a TemporaryDirectory failed, and any consumer deleting a capture dir after
  close() would hit the same lock.

- CaptureSession.close() now disposes the session's bind engine. - tests/test_highlevel.py: dispose
  the helper-created engines in the temp_capture_dir fixture teardown (before the tmpdir is
  removed), and dispose the engine leaked in test_pixel_ratio_round_trips_through_model.

* fix: release SQLite handles on load() error paths and refresh-held sessions

Second round of Windows CI findings (all invisible on POSIX, all WinError 32 file locks on
  recording.db):

- CaptureSession.load() error paths (corrupt query, no recording row) closed the session but never
  disposed its engine -- the pool kept the file handle open. Both paths now dispose the bind as
  well. - get_session_for_path() leaked its engine when migrate_missing_columns raised on a
  corrupt/unreadable db (the sqlite connection to the bad file stayed pooled). Now disposed before
  re-raising. - tests/test_highlevel.py: crud.insert_recording ends with session.refresh(), which
  leaves the helper session's connection checked out until close -- engine.dispose() alone cannot
  reclaim it. The temp_capture_dir fixture now closes helper sessions before disposing helper
  engines, and test_session_leak_on_no_recording no longer discards the setup engine it creates.

* fix: recorder shutdown deadlock on empty event queue

Third Windows CI finding: the live roundtrip test hung for the full 300s pytest timeout inside
  Recorder.__exit__. process_events() used a bare blocking event_q.get(): when terminate_processing
  is set while the queue is empty and the reader threads have already exited, no event ever arrives,
  the loop condition is never re-checked, and join_tasks() waits on the event_processor thread
  forever. The get is now bounded (timeout=1) so the loop re-checks terminate and drains cleanly.

Also make the integration tests wait on the recorder's own readiness signal (rec.wait_for_ready())
  instead of a fixed 0.5-1s sleep: on a cold CI runner startup takes ~3.5s, so the synthetic input
  was being injected before the listeners were up.

* test: gate listener-dependent live tests behind input-injection flag

Hosted Windows runners execute jobs in a non-interactive session, so SendInput-injected events never
  reach the low-level hooks pynput uses: the three event-capture assertions (roundtrip, reuse,
  throughput) can never hold there -- a runner-environment limitation, not a recorder bug. The CI
  step now sets OPENADAPT_CI_NO_INPUT_INJECTION=1 and those three tests skip with a precise reason,
  while the live pipeline tests that do not depend on captured input (recorder startup + clean
  bounded shutdown, per-capture db creation, bounded memory) keep running the real recorder on
  windows-latest. Run the full set on an interactive Windows desktop.

---------

Co-authored-by: Claude Fable 5 <noreply@anthropic.com>

### Documentation

- Add lifecycle status banner pointing to openadapt-flow
  ([#25](https://github.com/OpenAdaptAI/openadapt-capture/pull/25),
  [`82c7151`](https://github.com/OpenAdaptAI/openadapt-capture/commit/82c715145f50b3f8e7a07dd2d5c53d5a1649a3db))

Aligns this repository's front door with the org-wide product narrative: the demonstration compiler
  (openadapt-flow) installed via the OpenAdapt launcher. Status label matches the public repository
  lifecycle registry in OpenAdaptAI/.github.

Co-authored-by: Claude Fable 5 <noreply@anthropic.com>

- Banner — note the openadapt-flow desktop record on-ramp role
  ([#27](https://github.com/OpenAdaptAI/openadapt-capture/pull/27),
  [`399cd28`](https://github.com/OpenAdaptAI/openadapt-capture/commit/399cd28987f81b229f58ac53893e5274daee5aaf))

The status banner said this package is 'not the product' and 'the compiler does not require this
  package', which is accurate for the compiler core and the web path, but omitted the one documented
  product role capture DOES have: openadapt-flow's `record --backend windows|rdp` uses
  openadapt_capture.Recorder (via the optional `capture` extra) as its desktop demonstration
  recorder, and openadapt_flow.adapters.capture.convert_capture converts the session into the
  compile-ready recording format (openadapt-flow docs/desktop/RECORDING.md).

Add one paragraph stating that role precisely so readers do not conclude the package is unused by
  the product.

Co-authored-by: Claude Fable 5 <noreply@anthropic.com>

- Focus capture on its current product role
  ([#29](https://github.com/OpenAdaptAI/openadapt-capture/pull/29),
  [`19de075`](https://github.com/OpenAdaptAI/openadapt-capture/commit/19de075a001c50bff7228527c100b664f32a661a))

Rewrite the README around the current optional desktop-recorder role, the Flow-owned web recording
  path, the experimental extension boundary, concise usage, sensitive-data handling, and current
  limitations.

### Features

- Window-scoped recording (capture one window in its own pixel space)
  ([#30](https://github.com/OpenAdaptAI/openadapt-capture/pull/30),
  [`26a8836`](https://github.com/OpenAdaptAI/openadapt-capture/commit/26a88367dc22f30f9b1c7ad72fabbc0ee77ea373))

The missing recording half of the Citrix/remote-display wedge: the recorder captured the FULL SCREEN
  while openadapt-flow's RemoteDisplayBackend (rdp_window) replays in the client WINDOW's pixel
  space, forcing workarounds (record inside the session or full-screen the client). This adds a
  window capture mode that removes the mismatch at the source:

- Recorder(window={"owner": "Parallels", "title": None}): resolve the target window by
  case-insensitive owner/title substrings (macOS: CGWindowList, same selection semantics as flow's
  MacWindowClient; Windows: Win32 EnumWindows + DWM extended frame, no new deps), also

configurable via RECORD_WINDOW_OWNER/RECORD_WINDOW_TITLE and `capture record --window-owner ...`. -
  Frames are the window's own pixels (macOS: CGWindowListCreateImage with
  kCGWindowImageBoundsIgnoreFraming — the identical call flow's replay capture uses; Windows: mss
  region grab of the resolved bounds), re-resolved every frame so a moved window stays scoped. -
  Global input coordinates are translated at capture time into the captured frame's pixel space
  (pixel = (global - origin) * scale, the exact inverse of flow's _to_screen replay mapping);
  out-of-window input records out-of-range coordinates instead of being clamped. - Persistence for
  exact conversion: the window scoping (target, resolved window, initial bounds, scale, viewport,
  coordinate_space="window_pixels") is stored in the recording's config JSON (exposed as
  CaptureSession.window_capture), and a bounds timeline is recorded as window events whenever the
  resolved bounds/title change, linked to actions by the existing post-processing. - Fail-loud:
  recording refuses to start if the window cannot be resolved AND captured; input before the first
  frame is discarded with a warning rather than recorded in the wrong space; video frames whose size
  no longer matches the stream (mid-recording resize) are skipped loudly while screenshots and the
  bounds timeline stay exact.

Tests: 38 new tests — coordinate translation (including a round-trip

against flow's replay formula), bounds tracking/change detection via injected fakes (no display),
  config plumbing, recorder action-path translation, and persistence round-trip; plus a live smoke
  test gated like the input-injection tests (slow marker, platform gate,
  OPENADAPT_CI_NO_INPUT_INJECTION skip with precise reasons, OPENADAPT_WINDOW_SMOKE_OWNER override
  for the Parallels rig).

Live-validated end to end on macOS against a real window (Finder, 2x Retina): 8s window-mode
  Recorder run captured 49 actions with window-pixel coordinates, a bounds-timeline window event
  linked to all actions, window-sized video frames (1512x1888), and the persisted capture_window
  config.

Co-authored-by: Claude Fable 5 <noreply@anthropic.com>


## v0.5.4 (2026-07-12)

### Bug Fixes

- Importable headless (no screenshot at import) + persist pixel_ratio on the recording model
  ([#24](https://github.com/OpenAdaptAI/openadapt-capture/pull/24),
  [`a98322a`](https://github.com/OpenAdaptAI/openadapt-capture/commit/a98322a4a422c38f85e16737495500c1f05dcf97))

* fix: tolerate headless screenshot failure on import

The recorder no longer takes a screenshot at module load (PR #23), but the package import guard only
  caught ImportError. Broaden it so a display/screenshot failure (mss ScreenShotError) during
  recorder import also degrades to `Recorder = None` instead of crashing the whole package on a
  headless host (CI, containers, sandboxes).

Add a runtime headless-import test that imports the package fresh in a subprocess where mss.grab
  raises ScreenShotError, complementing the existing static (AST) guard.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>

Claude-Session: https://claude.ai/code/session_01CKrVJJy5jWVCkXAqgUqtqZ

* fix: persist pixel_ratio on the SQLAlchemy recording model

CaptureSession.pixel_ratio read `pixel_ratio` off the SQLAlchemy Recording model, but that model had
  no such column (only the legacy raw-sqlite schema did), so a HiDPI capture whose config JSON
  lacked it silently scaled at 1.0, under-scaling downstream coordinate mapping.

Add a nullable `pixel_ratio` column to the Recording model and write the captured display's ratio
  (platform.get_display_pixel_ratio()) at record time in create_recording. Add an additive ALTER
  TABLE ADD COLUMN migration (migrate_missing_columns) run when opening any per-capture DB, so older
  recording.db files that predate the column still load: the column is added as NULL, and
  CaptureSession.pixel_ratio falls back to the config JSON, then 1.0 when genuinely unknown.

---------

Co-authored-by: Claude Opus 4.8 <noreply@anthropic.com>


## v0.5.3 (2026-06-13)

### Bug Fixes

- Don't take a screenshot at import time
  ([#23](https://github.com/OpenAdaptAI/openadapt-capture/pull/23),
  [`d4e2016`](https://github.com/OpenAdaptAI/openadapt-capture/commit/d4e2016680b053d257b9b1934d07ac76589ee970))

recorder.py computed monitor dimensions via utils.take_screenshot() at module scope, so `import
  openadapt_capture` crashed in any headless environment whose display reported a zero-size region.
  This took down `openadapt version` and `openadapt doctor` (found via the new CLI smoke tests in
  OpenAdaptAI/OpenAdapt). Move the computation into the video-setup function that is its only
  consumer.

Adds tests/test_headless_import.py: a deterministic AST guard that no package module calls a display
  API (take_screenshot/get_monitor_dims/ grab) at import scope. A subprocess import test is
  unreliable (only reproduces on a genuinely headless display); this fails regardless of
  environment.

Co-authored-by: Claude Fable 5 <noreply@anthropic.com>

### Testing

- Add import-integrity guards and release-failure alerting
  ([#22](https://github.com/OpenAdaptAI/openadapt-capture/pull/22),
  [`cd7ed78`](https://github.com/OpenAdaptAI/openadapt-capture/commit/cd7ed786b3cb3b5512769787cde3ab4f62ca78c5))

Ecosystem rollout of the OpenAdaptAI/OpenAdapt#999 guards (see openadapt-ml#64, OpenAdapt#1002,
  openadapt-evals#262):

- tests/test_import_integrity.py: AST-based phantom-import and phantom-kwarg detection, including
  imports inside function bodies (40 modules scanned; this package is clean - zero findings) -
  release.yml: file/append a GitHub issue when the release workflow fails, so PyPI cannot silently
  go stale

Co-authored-by: Claude Fable 5 <noreply@anthropic.com>


## v0.5.2 (2026-03-17)

### Bug Fixes

- Add oa-atomacos dependency for macOS window capture
  ([#16](https://github.com/OpenAdaptAI/openadapt-capture/pull/16),
  [`50230cf`](https://github.com/OpenAdaptAI/openadapt-capture/commit/50230cf7dc195d21432e74399208c6a2be07036c))

oa-atomacos is imported in window/_macos.py but was missing from pyproject.toml, causing ImportError
  on macOS. The package (OpenAdapt's fork of atomacos) fixes pickle serialization of namedtuples
  needed for window state capture.

Co-authored-by: Claude Opus 4.6 <noreply@anthropic.com>


## v0.5.1 (2026-03-17)

### Bug Fixes

- Browser capture end-to-end pipeline
  ([#15](https://github.com/OpenAdaptAI/openadapt-capture/pull/15),
  [`505eb05`](https://github.com/OpenAdaptAI/openadapt-capture/commit/505eb05a2153a462e86ed2347ed2f41f3bc00562))

* fix: browser capture end-to-end pipeline

Three bugs prevented browser events from being captured and parsed:

1. background.js only relayed DOM_EVENT messages but the content script sends USER_EVENT — events
  were silently dropped.

2. background.js handleSetMode only read message.payload?.mode but the recorder sends flat {mode:
  "record"} — mode was never set to "record" so the content script never attached record listeners.

3. The BrowserEventType enum used "browser.click" prefix format but the content script sends raw DOM
  event names ("click", "keydown", etc.). This was an artificial convention introduced during the
  port from legacy OpenAdapt that was never tested end-to-end. Legacy used raw names throughout.

Changes: - background.js: add USER_EVENT relay, fix SET_MODE format handling - browser_events.py:
  change enum values to raw DOM names matching the content script and legacy OpenAdapt, add
  BrowserMouseMoveEvent - capture.py: add _parse_element_ref() and rewrite _convert_browser_event()
  to handle actual content-script message format including the recorder's {"message": <raw>}
  wrapper, add browser_events() and browser_event_count to CaptureSession - cli.py: add
  --browser-events flag to record, show browser event breakdown in info command - tests: add 15 e2e
  tests covering both DB roundtrip and raw content-script format parsing

Verified with live recording: 84/84 events captured and parsed from Chrome extension on Hacker News.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>

* fix: clean up stale docstring and unused import

* fix: address review feedback

- Replace bare except with debug logging in _convert_browser_event - Move lazy imports to module
  level (BoundingBox, ElementState, etc.) - Remove unused imports (pytest, Recording) from test file
  - Update test class names to reflect structure tested, not removed format - Fix stale docstring in
  _parse_element_ref

* fix: remove unused BrowserEventType import from tests

---------

Co-authored-by: Claude Opus 4.6 <noreply@anthropic.com>


## v0.5.0 (2026-03-04)

### Features

- Conditional window capture + recording profiling with auto-wormhole
  ([#12](https://github.com/OpenAdaptAI/openadapt-capture/pull/12),
  [`c58e880`](https://github.com/OpenAdaptAI/openadapt-capture/commit/c58e8800ce4096316bd1af6b5b6db7ce9e9fedc9))

* feat: disable window capture by default, add recording profiling with auto-wormhole

- Make window reader/writer conditional on RECORD_WINDOW_DATA (defaults to False), eliminating
  unnecessary thread + process + expensive platform API calls - Add throttle to read_window_events
  (0.1s) and memory_writer (1s) loops - Add profiling summary at end of record() with duration,
  event counts/rates, config flags, main thread check, and thread count - Auto-send profiling.json
  via Magic Wormhole after recording stops

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>

* fix: skip window requirement when RECORD_WINDOW_DATA=False, set log level to WARNING

- When window capture is disabled, skip the window timestamp requirement in process_events instead
  of discarding all action events - Set loguru log level to WARNING by default (was DEBUG) to reduce
  noise during recording

* fix: set log level to INFO not WARNING

* fix: guard window event save when capture disabled, fix PyAV pict_type compat

- Second reference to prev_window_event in process_events was unguarded, causing AttributeError when
  RECORD_WINDOW_DATA=False - PyAV pict_type="I" raises TypeError on newer versions; fall back to
  integer constant

* fix: use PictureType.I enum for PyAV pict_type, add video tests

- Use av.video.frame.PictureType.I instead of string "I" which is unsupported in current PyAV
  versions - Add test_video.py with tests for frame writing, key frames, and PictureType enum
  compatibility

* fix: use Agg backend for matplotlib, improve wormhole-not-found message

- Set matplotlib to non-interactive Agg backend so plotting works from background threads (fixes
  RuntimeError when Recorder runs record() in a non-main thread) - Improve wormhole-not-found
  message with install instructions

* feat: add per-screenshot timing to profiling, fix stop sequence IndexError

- Track screenshot duration (avg/max/min ms) and total iteration duration per screen reader loop
  iteration in profiling.json - Reset stop sequence index after match to prevent IndexError on extra
  keypresses

* feat: make send_profile opt-in CLI flag, add magic-wormhole as regular dep

Profiling data is no longer auto-sent via wormhole after every recording. Use --send_profile flag to
  opt in. Also promotes magic-wormhole from optional [share] extra to a regular dependency since
  sharing is core functionality.

* fix: address PR #12 review feedback (5 issues)

- Move magic-wormhole back to optional [share] extra (was accidentally made a required dependency;
  recorder.py already handles ImportError) - Remove module-level logger.remove() that destroyed
  global loguru config for all library consumers; configure inside record() instead - Replace
  duplicate wormhole-finding logic with _find_wormhole() from share.py to eliminate code duplication
  - Add 60s timeout to _send_profiling_via_wormhole to prevent blocking indefinitely waiting for a
  receiver - Replace unbounded _screen_timing list with _ScreenTimingStats class that computes
  running stats (count/sum/min/max) in constant memory

---------

Co-authored-by: Claude Opus 4.6 <noreply@anthropic.com>


## v0.4.0 (2026-03-03)

### Features

- Add docs sync trigger ([#14](https://github.com/OpenAdaptAI/openadapt-capture/pull/14),
  [`250ce56`](https://github.com/OpenAdaptAI/openadapt-capture/commit/250ce565ed2a2b2d60da02ab378faf8ea15a4c17))


## v0.3.0 (2026-02-18)

### Bug Fixes

- Resolve all ruff lint errors ([#7](https://github.com/OpenAdaptAI/openadapt-capture/pull/7),
  [`de00dab`](https://github.com/OpenAdaptAI/openadapt-capture/commit/de00dab66724eb786dafb80ddfc860c51be13877))

* fix: resolve all ruff lint errors

- Remove unused variable assignments (share.py, browser_bridge.py, windows.py, test_highlevel.py) -
  Add noqa comment for Quartz import needed by ApplicationServices (darwin.py) - Remove unused
  TYPE_CHECKING import (storage/__init__.py) - Add proper TYPE_CHECKING import for CaptureStats
  annotation (generate_real_capture_plot.py) - Auto-fix import sorting across multiple files

Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>

* docs: update README with share command and ecosystem links

- Uncomment PyPI badges (package now published as 0.3.0) - Add "Sharing Recordings" section with
  Magic Wormhole usage - Update openadapt-privacy from "Coming soon" to GitHub link - Add share
  extra to optional extras table - Add openadapt-privacy and openadapt-evals to Related Projects

* docs: remove redundant openadapt-ml training section

The detailed training workflow belongs in openadapt-ml's README. This keeps openadapt-capture
  focused on its core functionality. Users can find training info via the Related Projects link.

---------

Co-authored-by: Claude Opus 4.5 <noreply@anthropic.com>

- Throttle screen capture to reduce system lag during recording
  ([#11](https://github.com/OpenAdaptAI/openadapt-capture/pull/11),
  [`d7134a8`](https://github.com/OpenAdaptAI/openadapt-capture/commit/d7134a83921a551cd8e91ae127f63f0759a146fe))

The screen reader thread was capturing screenshots in a tight loop with no frame rate limit, causing
  high CPU and memory pressure. With action-gated video, only the most recent screenshot matters
  when an action occurs, so capturing at 100+ fps was pure waste.

Add SCREEN_CAPTURE_FPS config (default: 10 fps). The throttle sleeps for the remainder of the frame
  interval after each capture. Set to 0 for unlimited (legacy behavior). Also available as
  screen_capture_fps param on Recorder constructor and RecordingConfig.

Co-authored-by: Claude Opus 4.6 <noreply@anthropic.com>

- **ci**: Fix release automation — use ADMIN_TOKEN for protected branches
  ([#8](https://github.com/OpenAdaptAI/openadapt-capture/pull/8),
  [`4205cd5`](https://github.com/OpenAdaptAI/openadapt-capture/commit/4205cd5d2c6fd86eff4fd1e3bcd93a59aae03416))

* fix: resolve all ruff lint errors

- Remove unused variable assignments (share.py, browser_bridge.py, windows.py, test_highlevel.py) -
  Add noqa comment for Quartz import needed by ApplicationServices (darwin.py) - Remove unused
  TYPE_CHECKING import (storage/__init__.py) - Add proper TYPE_CHECKING import for CaptureStats
  annotation (generate_real_capture_plot.py) - Auto-fix import sorting across multiple files

Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>

* docs: update README with share command and ecosystem links

- Uncomment PyPI badges (package now published as 0.3.0) - Add "Sharing Recordings" section with
  Magic Wormhole usage - Update openadapt-privacy from "Coming soon" to GitHub link - Add share
  extra to optional extras table - Add openadapt-privacy and openadapt-evals to Related Projects

* docs: remove redundant openadapt-ml training section

The detailed training workflow belongs in openadapt-ml's README. This keeps openadapt-capture
  focused on its core functionality. Users can find training info via the Related Projects link.

* fix(ci): fix release automation — use ADMIN_TOKEN to push to protected branches

Root cause: GITHUB_TOKEN cannot push commits to protected branches. Semantic-release created the
  v0.3.0 tag (tags bypass protection) but the "chore: release 0.3.0" commit that bumps
  pyproject.toml was orphaned.

- Use ADMIN_TOKEN for checkout and semantic-release (can push to main) - Add skip-check to prevent
  infinite loops on release commits - Sync pyproject.toml version to 0.3.0 (matches latest tag)

Prerequisite: Add ADMIN_TOKEN secret (GitHub PAT with repo scope) to

repository settings.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>

---------

Co-authored-by: Claude Opus 4.5 <noreply@anthropic.com>

- **ci**: Use v9 branch config for python-semantic-release
  ([#13](https://github.com/OpenAdaptAI/openadapt-capture/pull/13),
  [`11eafca`](https://github.com/OpenAdaptAI/openadapt-capture/commit/11eafcaac1cab88dde4e861e93553fdef4b4ac1b))

* feat: disable window capture by default, add recording profiling with auto-wormhole

- Make window reader/writer conditional on RECORD_WINDOW_DATA (defaults to False), eliminating
  unnecessary thread + process + expensive platform API calls - Add throttle to read_window_events
  (0.1s) and memory_writer (1s) loops - Add profiling summary at end of record() with duration,
  event counts/rates, config flags, main thread check, and thread count - Auto-send profiling.json
  via Magic Wormhole after recording stops

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>

* fix: skip window requirement when RECORD_WINDOW_DATA=False, set log level to WARNING

- When window capture is disabled, skip the window timestamp requirement in process_events instead
  of discarding all action events - Set loguru log level to WARNING by default (was DEBUG) to reduce
  noise during recording

* fix: set log level to INFO not WARNING

* fix: handle missing video frames on early Ctrl+C

video_post_callback crashes with KeyError 'last_frame' when recording stops before any action
  triggers a video frame write. Guard against missing state keys and close the container gracefully.

* fix: guard window event save when capture disabled, fix PyAV pict_type compat

- Second reference to prev_window_event in process_events was unguarded, causing AttributeError when
  RECORD_WINDOW_DATA=False - PyAV pict_type="I" raises TypeError on newer versions; fall back to
  integer constant

* fix: use PictureType.I enum for PyAV pict_type, add video tests

- Use av.video.frame.PictureType.I instead of string "I" which is unsupported in current PyAV
  versions - Add test_video.py with tests for frame writing, key frames, and PictureType enum
  compatibility

* fix: use Agg backend for matplotlib, improve wormhole-not-found message

- Set matplotlib to non-interactive Agg backend so plotting works from background threads (fixes
  RuntimeError when Recorder runs record() in a non-main thread) - Improve wormhole-not-found
  message with install instructions

* fix: reset stop sequence index after match to prevent IndexError

When the stop sequence was fully matched, the index wasn't reset. Extra keypresses after the match
  would index past the end of the sequence list, causing IndexError.

* feat: add per-screenshot timing to profiling, fix stop sequence IndexError

- Track screenshot duration (avg/max/min ms) and total iteration duration per screen reader loop
  iteration in profiling.json - Reset stop sequence index after match to prevent IndexError on extra
  keypresses

* feat: make send_profile opt-in CLI flag, add magic-wormhole as regular dep

Profiling data is no longer auto-sent via wormhole after every recording. Use --send_profile flag to
  opt in. Also promotes magic-wormhole from optional [share] extra to a regular dependency since
  sharing is core functionality.

* fix: add pixel_ratio and audio_start_time to CaptureSession

HTML visualizer referenced these attributes which didn't exist on CaptureSession. Added properties
  with safe fallbacks and updated html.py to use getattr with defaults.

* fix(ci): use v9 branch config for python-semantic-release

The `branch = "main"` key is from v7/v8 and is silently ignored by v9, causing "No release will be
  made, 0.3.0 has already been released!" on every push. Use the v9 `[branches.main]` table.

---------

Co-authored-by: Claude Opus 4.6 <noreply@anthropic.com>

### Features

- Copy legacy OpenAdapt recording system into openadapt-capture
  ([#9](https://github.com/OpenAdaptAI/openadapt-capture/pull/9),
  [`d359011`](https://github.com/OpenAdaptAI/openadapt-capture/commit/d3590118edad60171c8c2ca47cee181f05d590e2))

* fix: match legacy OpenAdapt recording architecture

- Action-gated video capture: only encode frames when actions occur (~1-5 fps) instead of every
  screenshot (24fps). This is the core reason legacy OpenAdapt was smooth — not just separate
  processes. Matches legacy RECORD_FULL_VIDEO=False default behavior. - Video encoding in separate
  multiprocessing.Process (avoids GIL) - Screenshots via mss (2-4x faster than PIL.ImageGrab on
  Windows) - SIGINT ignored in worker process (main handles Ctrl+C) - Non-daemon process ensures
  video finalization on shutdown - First frame forced as key frame for seekability - Fix wormhole
  FileNotFoundError on Windows (searches Scripts/ dir)

Legacy patterns matched: - prev_screen_event buffering → _prev_screen_frame -
  prev_saved_screen_timestamp dedup → _prev_saved_screen_timestamp - RECORD_FULL_VIDEO option →
  record_full_video parameter - SIG_IGN in worker processes - mss with CAPTUREBLT=0 on Windows

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>

* feat: copy legacy OpenAdapt recording system into openadapt-capture

Replace vibe-coded recording internals with proven legacy OpenAdapt code, adapted only for
  per-capture databases and import paths.

New modules (copied from legacy): - db/models.py: SQLAlchemy models (Recording, ActionEvent,
  Screenshot, WindowEvent, PerformanceStat, MemoryStat) - db/crud.py: batch insert functions,
  post_process_events - extensions/synchronized_queue.py: multiprocessing queue wrapper - utils.py:
  timestamps, screenshots, monitor dims - window/: platform-specific active window capture -
  plotting.py: performance stat visualization

Updated modules: - recorder.py: full legacy record() with multi-process writers, action-gated video,
  stop sequences, SIGINT handling - capture.py: reads from SQLAlchemy DB, fixes session leak,
  mouse_pressed=None handling, disabled event filtering, adds dx/dy/button properties to Action -
  config.py: all legacy recording config values - video.py: legacy functional API wrappers - cli.py:
  wired to new recorder - pyproject.toml: added sqlalchemy, loguru, psutil, tqdm deps

Bug fixes: - Reset stop_sequence_detected on re-entry (Recorder reuse) - Close session on error in
  CaptureSession.load() - Skip click events with mouse_pressed=None - Filter disabled events in
  raw_events()

Tests: 118 passed + 6 performance tests (Windows-only)

Docs: updated README.md and CLAUDE.md to match new architecture

* fix: make pynput import conditional for headless CI

- Wrap Recorder import in try/except in __init__.py and test files - Skip Recorder tests when pynput
  unavailable (no display server) - Fix all ruff I001 import sorting violations - Remove unused
  imports and variables

* fix(ci): exclude browser bridge tests and add timeout

Browser bridge tests hang indefinitely on headless CI due to async websocket fixtures. Add
  pytest-timeout and a 10-minute job timeout.

---------

Co-authored-by: Claude Opus 4.6 <noreply@anthropic.com>

- Unified Recorder API with config overrides and runtime properties
  ([#10](https://github.com/OpenAdaptAI/openadapt-capture/pull/10),
  [`d840e81`](https://github.com/OpenAdaptAI/openadapt-capture/commit/d840e8107d7d45347201f04958c53293f08902e6))

Restore the clean Python API from the pre-legacy design on top of the proven legacy multi-process
  recording internals.

Recorder constructor now accepts capture_video, capture_audio, and other recording options as
  keyword params that override config defaults. Adds event_count, is_recording, stats,
  wait_for_ready(), and capture properties for runtime introspection.

Changes: - config.py: Add RecordingConfig dataclass + config_override() context manager for
  temporary config patching - recorder.py: Add shared counter params to record(), fix module-level
  config reads (STOP_SEQUENCES, PLOT_PERFORMANCE), rewrite Recorder class with full API (~120 lines
  replacing ~36) - cli.py: Forward --video/--audio/--images flags to Recorder - __init__.py: Export
  RecordingConfig - tests: 11 new tests (Recorder API + config_override)

Fixes compatibility with record_waa_demos.py which passes capture_video/capture_audio and reads
  recorder.event_count.

Co-authored-by: Claude Opus 4.6 <noreply@anthropic.com>

- **share**: Add Magic Wormhole sharing for recordings
  ([`e7cfb1e`](https://github.com/OpenAdaptAI/openadapt-capture/commit/e7cfb1ec84999184a96682ebf74c08929485fe63))

- Add share.py module with send/receive via wormhole - Add 'capture share send/receive' CLI commands
  - Add magic-wormhole as optional [share] dependency - Auto-installs wormhole if missing

Usage: capture share send ./my_recording capture share receive 7-guitarist-revenge

Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>


## v0.2.0 (2026-01-29)

### Bug Fixes

- Comment out PyPI badges until package is published
  ([#3](https://github.com/OpenAdaptAI/openadapt-capture/pull/3),
  [`5aedd99`](https://github.com/OpenAdaptAI/openadapt-capture/commit/5aedd99f6329368514cfb6340741241c3f71813a))

The PyPI version and downloads badges show "package not found" since openadapt-capture is not yet
  published to PyPI. Commenting them out until the package is released.

Co-authored-by: Claude Sonnet 4.5 <noreply@anthropic.com>

- Move openai-whisper to optional [transcribe] extra
  ([`9dca9e5`](https://github.com/OpenAdaptAI/openadapt-capture/commit/9dca9e5e394b015664d982a3581c11801217d50b))

The openai-whisper package requires numba → llvmlite which only supports Python 3.6-3.9, causing
  installation failures on Python 3.12+.

Moving whisper to an optional dependency allows the meta-package (openadapt) to install successfully
  while users who need transcription can explicitly opt-in with `pip install
  openadapt-capture[transcribe]`.

Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>

- Update author email to richard@openadapt.ai
  ([`1987bee`](https://github.com/OpenAdaptAI/openadapt-capture/commit/1987beeb22eed52d98b67516217d8c486ab7c37d))

Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>

- Use filename-based GitHub Actions badge URL
  ([#4](https://github.com/OpenAdaptAI/openadapt-capture/pull/4),
  [`957ca48`](https://github.com/OpenAdaptAI/openadapt-capture/commit/957ca480baf06b1b328b9f9cf65b1a483d948ea2))

The workflow-name-based badge URL was showing "no status" because GitHub requires workflow runs on
  the specified branch. Using the filename-based URL format (actions/workflows/test.yml/badge.svg)
  is more reliable and works regardless of when the workflow last ran.

Co-authored-by: Claude Sonnet 4.5 <noreply@anthropic.com>

- **ci**: Remove build_command from semantic-release config
  ([`93cdbb8`](https://github.com/OpenAdaptAI/openadapt-capture/commit/93cdbb8ff6f87a6ad96dc74d8092bbad58a34d51))

The python-semantic-release action runs in a Docker container where uv is not available. Let the
  workflow handle building instead.

Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>

### Chores

- Gitignore turn-off-nightshift test capture
  ([`62b25be`](https://github.com/OpenAdaptAI/openadapt-capture/commit/62b25be430ca2e1e5f69803c3c4db9568fbcf72f))

Test capture data (video, screenshots, database) should not be committed.

Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>

### Continuous Integration

- Add auto-release workflow
  ([`c3b3eb8`](https://github.com/OpenAdaptAI/openadapt-capture/commit/c3b3eb806ac060f3d8a98d8b5e048a0f2acfa2b2))

Automatically bumps version and creates tags on PR merge: - feat: minor version bump - fix/perf:
  patch version bump - docs/style/refactor/test/chore/ci/build: patch version bump

Triggers publish.yml which deploys to PyPI.

Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>

- Switch to python-semantic-release for automated versioning
  ([`b9246a6`](https://github.com/OpenAdaptAI/openadapt-capture/commit/b9246a60de8f83fa7d5ff32749fd0df9d0e22163))

Replaces manual commit parsing with python-semantic-release: - Automatic version bumping based on
  conventional commits - feat: -> minor, fix:/perf: -> patch - Creates GitHub releases automatically
  - Publishes to PyPI on release

Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>

### Documentation

- Add CLAUDE.md with development guidelines
  ([#1](https://github.com/OpenAdaptAI/openadapt-capture/pull/1),
  [`5f8fafc`](https://github.com/OpenAdaptAI/openadapt-capture/commit/5f8fafcd77c613b3c74f46546e605f75a7b1c675))

- Add overview of package purpose - Add quick commands for installation, testing, and usage - Add
  architecture overview and key components - Add links to related projects

- Add viewer screenshot to README
  ([`a22c789`](https://github.com/OpenAdaptAI/openadapt-capture/commit/a22c78952cc0e12f2a6cd742a2218a93a55a146d))

Add screenshot of the Capture Viewer HTML interface to improve documentation and show users what the
  viewer looks like.

### Features

- Add browser event capture via Chrome extension
  ([`553bb0a`](https://github.com/OpenAdaptAI/openadapt-capture/commit/553bb0ac783e7e0535b772c3840aeccf74815b20))

- Add BrowserBridge WebSocket server for Chrome extension communication - Add browser_events.py with
  Pydantic models for click, key, scroll events - Add Chrome extension with manifest v3 for DOM
  event capture - Export browser bridge API from __init__.py - Add step navigation controls to HTML
  visualizer - Comprehensive test suite (800+ lines)

Also includes: - docs/whisper-integration-plan.md: Whisper strategy analysis - README improvements
  with ecosystem documentation

Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>

- Add faster-whisper backend for 4x faster transcription
  ([`6a8e30e`](https://github.com/OpenAdaptAI/openadapt-capture/commit/6a8e30ec71ed4513bb643bfb58558911d2fe9584))

Add support for faster-whisper as an alternative transcription backend: - New transcribe-fast
  optional dependency in pyproject.toml - Backend auto-detection (tries faster-whisper first, falls
  back to openai-whisper) - New --backend CLI option: auto, faster-whisper, openai-whisper, api -
  Maintain backward compatibility with existing --api flag

Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>


## v0.1.0 (2025-12-12)

### Bug Fixes

- Add contents read permission for publish workflow
  ([`eda29a4`](https://github.com/OpenAdaptAI/openadapt-capture/commit/eda29a49124d6db70369db518ae734ff0b994cec))

### Features

- Complete GUI capture with transcription, visualization, processing, and CI/CD
  ([`365dff8`](https://github.com/OpenAdaptAI/openadapt-capture/commit/365dff8689378afce4013a50997b0fe02e650730))

- Initial repo with design doc
  ([`9e34077`](https://github.com/OpenAdaptAI/openadapt-capture/commit/9e34077504e76e7449d185142caa4de2744059b9))
