# Four synthetic referrals with paired high-density display captures

The corrected run saved and independently read back three synthetic charts. The duplicate case stopped at the duplicate dialog before saving, with no changed or new chart. The engine called no model. These are display-evidence runs, not a timing or accuracy estimate.

| Case | Transaction | Direct SQL new / changed | Paired frames |
|---|---|---|---|
| r3 | VERIFIED | 1 / 0 | 58 / 58 |
| r4 | VERIFIED | 1 / 0 | 58 / 58 |
| r5 | VERIFIED | 1 / 0 | 58 / 58 |
| r6 | HALTED_BEFORE_EFFECT | 0 / 0 | 56 / 56 |

Bundle: `10e4594523362a0c79a8a533742a71b5b73178d32378c7bc934b51a2b006bc1c`, requalified on 19 November with the clock and REST completeness fixes. Engine base: main `43bf78d44`; this PR changes benchmark capture, not runtime checks. The fixture is pinned OpenEMR 8.0.0.3 with synthetic records only.

The engine evidence remains 1280×800. The 2560×1600 companions are separate display captures taken without input between the two captures, then paired by the original frame SHA-256, companion SHA-256, dimensions, and tile comparison. All 230 frames paired; the largest tile difference was 7.480 under the existing limit of 16. These companions are not sealed engine evidence. `screenshots.sha256` pins both sets; original PNGs remain on the execution machine.

## Authorization and signatures

The founder authorized the four synthetic referrals in Advisor fable on 19 November: “yes do the new runs for 2x frames”, followed by “you do it” for signing. The scoped SSH authorization names only r3–r6, this bundle, the standard profile and the existing demo Seal key. It was signed, anchored and timestamped before the corrected runs. Its SHA-256 is `27a7c62df5aadca9dbcf03e2431e4f5c52bfbf1b6b5562225c5000d0bd838016`; Rekor index 4011200742, 2026-11-20 00:25:23 UTC. No private key is published. The public verification key is the existing approved registry at `../2026-10-20/seal-key.json`; both new local registry copies were byte-equivalent public entries and are kept only with the local source runs, avoiding duplicate key registries in the public tree.

All four Seals were checked live against Rekor and the GitHub SSH key list and offline against the pinned Seal key and Sigstore TSA root. The verifier reports `before_run` for each. Seal Rekor indexes: r3 4011311201, r4 4011313265, r5 4011317256, r6 4011321057. Each report's existing redaction check found nothing further to redact. No raw credential, private evidence directory, identity crop or database snapshot is committed.

## Retained first attempt

`initial-capture-attempt/` contains the first four reports, direct SQL reads, pairing records and all screenshot hashes. Its three saves and duplicate stop were correct, but the capture-quality command exited 1: the r3 and r5 `step_016_before.png` had no companion. The benchmark wrapped `screenshot()` but missed the backend's separate `guarded_keyboard_frame()` entry point. The fix wraps both without changing their returned bytes, retries a transient companion-capture failure, and leaves the runtime checks unchanged. Five targeted real-Chromium tests pass, including both frame APIs and an injected capture failure. The complete rerun used a distinct signed authorization. The failed attempt was retained rather than included as a successful capture.

## Reproduce

Take the shared OpenEMR fixture lease. Start a pinned synthetic fixture with the lifecycle helper and point the qualification hooks at its own project/state. Run:

```sh
python -m benchmark.openemr_local.certified_referrals \
  --bundle benchmark/openemr_local/referral_program/program \
  --out <new-directory> --seal-home <private-key-home> \
  --authorized-by <actual-authorization> --hidpi-scale 2
```

The runner refuses an unexpected transaction, wrong chart counts, a duplicate stop at another step, or any missing/mismatched companion. SQL read-back runs before capture pairing so a presentation failure cannot conceal a write. Offline verification from this directory:

```sh
openadapt-flow local-seal verify 1-r3-ines-carvalho/engine 2-r4-theo-lindqvist/engine \
  3-r5-grace-adeyemi/engine 4-r6-rosa-delgado/engine --keys ../2026-10-20/seal-key.json \
  --require-anchor --require-timestamp --authorization run-authorization
```

Generality: no engine code changed; the defect class is multiple framebuffer entry points and transient capture failures. Both screenshot entry points and fault recovery were checked on an independent headless test page. The real-app run is OpenEMR only; this does not establish a second application qualification.
