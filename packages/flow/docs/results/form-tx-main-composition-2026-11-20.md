# FormTx prototype: main composition

PR #2308 source `03e9cae33c539288a30bde7efa4442c7621cbacf` was normally merged with public main `9e78e27da15e27a927033bf5c9392d01db7ad742` on an isolated helper branch. The sole merge conflict was independent appends to `.gitleaksignore`. Both existing exact historical fingerprint sets were preserved; no new fingerprint exception, scanner rule or exclusion was added. Current main's compiled/runtime and choice contracts were retained.

The package consistency check then found six missing artifact paths: the existing synthetic benchmark result JSON, the side panel's CSS/HTML/JS, and the two new declarative profiles. The existing `check_release_consistency.py --write-public-artifact-inventory` generator added exactly those six paths. The read-only check passed afterward. No release version, pin, admission or artifact publication changed.

## Targeted validation

All tests and lint/type checks ran through the shared CPU gate. Python was the existing supported Anaconda 3.12.7 interpreter, with Flow and Types imports explicitly bound to this checkout. This is an existing local test environment, not a newly qualified locked dependency closure. Browser tests were headless and used synthetic fixtures and temporary profiles.

- Nine changed FormTx/copilot test modules: **161 passed**, no failures or skips, in 133.42s.
- Seven existing extension messaging/content/frame/network/native-host regression modules: **165 passed**, no failures or skips, in 42.68s. These are distinct tests; total 326.
- Repository-pinned cached Ruff 0.17.0: affected engine lint passed; all 16 source files already formatted.
- Repository-configured mypy: no issues in 16 affected source files.
- Package consistency: initially failed with exactly six unregistered paths; passed after the canonical inventory update.
- `git diff --check`: passed.

The old head's CI failures were only the three draft-PR sentinel steps. They did not constitute a source CI run. Current-source CI and one genuine independent review remain required after the coordinator routes the prepared source to the original branch.

## Scope

This is browser FormTx prototype source composition. The original nine-completion benchmark checked only partial summary coverage. Full coverage first caused all nine to refuse; the synthetic summaries were then expanded. Those source-fixture results establish neither real hospital/carrier performance nor an independently held-out or production outcome. `docs/FORM_TX.md` contains the canonical scope. Local receipt signatures attest the runner's statement under its own key.

No application-specific engine branch was added. One inventory model registers every new asset/profile sibling. The existing provenance, read-back and operator-decision models remain. Evidence here is synthetic source/browser evidence; no private qualification data, real application/DB/provider operation, release or production admission was introduced.


## Current-source CI boundary correction

Normal CI at `f0e509854a73b3daa590ed04ec54922839414cb5` exposed three new-entrypoint gaps in existing package contracts. Flow run 44778134521 failed the raw-descriptor, explicit CLI spelling and CDP acquisition inventories in its three unit shards. Windows replay 44778132132 failed the same raw-descriptor source guard; its governed step was then skipped and its expected output was absent. This is a concrete source failure, not a resource or flaky-native diagnosis. The cited launcher run 44778132760 was cancelled; its failed steps were the earlier draft sentinels.

The correction reuses `private_file.open_binary` for the copilot JSONL byte owner, explicitly disables abbreviations on all five new FormTx/benchmark parent/child parser constructors, and calls `require_rendering_ready` on the exact selected attached page before constructing a form surface, session or runtime. The new checked caller joins the existing CDP inventory; no existing guard is removed or relaxed. Already-correct native-host and copilot CLI parsers retain their policies. Empty claim descriptors perform no byte I/O; temporary state descriptors already use binary `mkstemp`.

Local reproduction of the three exact CI assertions failed 3/3 before correction. A new physical-byte check exposed CRLF expansion using the existing owned CRT model; logical JSON round-trip alone would miss it. The first readiness test attempt had an incomplete mock context (`pages` missing); after that setup correction, all three readiness cases exposed entry into form construction without the shared check. Those failures are not passing-control claims.

After correction, **16 selected boundary/regression cases passed**, 0 failures/skips, in 18.98s through the CPU gate. They cover existing descriptor properties and exact physical copilot bytes/deduplication, the package-wide CLI constructor/prefix model including all FormTx children, benchmark prefix refusal before work, the CDP inventory and owned selected-page success/refusal/cancellation before form startup. These are targeted source controls, not a repeat of the 326 campaign. Ruff 0.17 lint/format passes for the seven changed source/test files; configured mypy passes for the three changed engine modules; diff check passes.

No local Windows, real CDP/browser attachment, native/private fixture, provider or application qualification was run for this correction. The mocked rendering callback checks create no browser or form write. An attached browser/context remains caller-owned. Normal new-head CI and the same reviewer's source delta remain required; the genuine `f0e` approval stays historical.

Durable item: independent record bytes and explicit entrypoint intent survive platform and acquisition differences. No per-app code. Existing package-wide opener, parser and rendering models cover each known new sibling. Evidence is owned synthetic/source evidence; no held-out real-system outcome is inferred.
