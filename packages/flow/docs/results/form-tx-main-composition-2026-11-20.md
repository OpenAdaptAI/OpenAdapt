# FormTx prototype: main composition

PR #2308 source `03e9cae33c539288a30bde7efa4442c7621cbacf` was normally merged with public main `9e78e27da15e27a927033bf5c9392d01db7ad742` on an isolated helper branch. The sole merge conflict was independent appends to `.gitleaksignore`. Both existing exact historical fingerprint sets were preserved; no new fingerprint exception, scanner rule or exclusion was added. Current main's compiled/runtime and choice contracts were retained.

The package consistency check then found six missing artifact paths: the existing synthetic benchmark result JSON, the side panel's CSS/HTML/JS, and the two new declarative profiles. The existing `check_release_consistency.py --write-public-artifact-inventory` generator added exactly those six paths. The read-only check passed afterward. No release version, pin, admission or artifact publication changed.

## Targeted validation

All tests and lint/type checks ran through the shared CPU gate. Python was the existing supported Anaconda 3.12.7 interpreter, with Flow and Types imports explicitly bound to this checkout. This is an existing local test environment, not a newly qualified locked dependency closure. Browser tests were headless and used synthetic fixtures and temporary profiles.

- Nine changed FormTx/copilot test modules: **161 passed**, no failures or skips, in133.42s.
- Seven existing extension messaging/content/frame/network/native-host regression modules: **165 passed**, no failures or skips, in42.68s. These are distinct tests; total326.
- Repository-pinned cached Ruff0.17.0: affected engine lint passed; all16 source files already formatted.
- Repository-configured mypy: no issues in16 affected source files.
- Package consistency: initially failed with exactly six unregistered paths; passed after the canonical inventory update.
- `git diff --check`: passed.

The old head's CI failures were only the three draft-PR sentinel steps. They did not constitute a source CI run. Current-source CI and one genuine independent review remain required after the coordinator routes the prepared source to the original branch.

## Scope

This is browser FormTx prototype source composition. The original nine-completion benchmark checked only partial summary coverage. Full coverage first caused all nine to refuse; the synthetic summaries were then expanded. Those source-fixture results establish neither real hospital/carrier performance nor an independently held-out or production outcome. `docs/FORM_TX.md` contains the canonical scope. Local receipt signatures attest the runner's statement under its own key.

No application-specific engine branch was added. One inventory model registers every new asset/profile sibling. The existing provenance, read-back and operator-decision models remain. Evidence here is synthetic source/browser evidence; no private qualification data, real application/DB/provider operation, release or production admission was introduced.
