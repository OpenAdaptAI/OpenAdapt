# Accepted real-RDP batch: `82a658a`

Candidate `82a658a6926ddac74b010b613535c023d0b5f079` ran one fresh,
fixed batch of exactly three trials from retained clean qualification base
`{8f43c385-0566-44d3-9e50-f646777d315b}`. There were no retries.

The task was to open the Windows Run dialog through RDP, type a command that
creates one trial-unique file, and verify that file through an independent
`prlctl exec type <path>` guest-tools oracle. All three expected values matched
the independently observed values exactly.

## Environment and readiness

- Substrate: real Aardwolf RDP 0.2.14 into a Parallels Windows 11 guest
  (`Microsoft Windows [Version 10.0.22631.6199]`), 1280x800 framebuffer.
- Base source commit: `db87e3ffe802a94046f0f131da6094dac9a0fbd7`.
- Qualification account: one exact active target-account RDP session; all
  trials qualified session `3`, with Explorer in that same exact session.
- Desktop gate: the fixed-VM Windows 11 light taskbar occupied the bottom 8%,
  with at least 50% of pixels at luma 161 or greater; framebuffer transition
  from the login baseline was at least 0.10 unless the baseline was already
  desktop-ready; three consecutive ready frames changed by no more than 0.02.
- Counted desktop-readiness timeout: 75 seconds per trial.

## Counted rows

| Trial | Expected | Independently observed | Exact | Input receipt | Latency |
| ---: | --- | --- | :---: | --- | ---: |
| 1 | `oaflow-rdp-trial-1-a7afe4a26c8135af` | `oaflow-rdp-trial-1-a7afe4a26c8135af` | Yes | Returned without error | 51.845 s |
| 2 | `oaflow-rdp-trial-2-fae5492e869de211` | `oaflow-rdp-trial-2-fae5492e869de211` | Yes | Returned without error | 10.467 s |
| 3 | `oaflow-rdp-trial-3-367fd897497feee3` | `oaflow-rdp-trial-3-367fd897497feee3` | Yes | Returned without error | 7.477 s |

| Metric | Result |
| --- | ---: |
| Successes | 3/3 |
| Failures | 0 |
| Silent incorrect successes | 0 |
| Over-halts | 0 |
| Model calls | 0 |

The declared failure taxonomy was `connect_or_frame_failure`,
`input_delivery_failure`, `independent_oracle_mismatch`,
`over_halt_or_timeout`, and `environment_restore_failure`. All three counted
rows were classified `none`.

## Evidence and cleanup

The exact raw counted report remains local with SHA-256
`7c31e220bfba34057b83a8910181ce54ab82bd67f5bbc527fb319ad2091b6b9b`.
The committed `results_82a658a_20260718.sanitized.json` is a deterministic
derivative that retains the evidence-relevant environment, rows, counters,
readiness, and cleanup booleans while replacing local machine identifiers and
snapshot-ID arrays. The committed sanitized file SHA-256 is
`c6be771a567dfef8e924655ac165ea78c1eb32893bcf441908f4c6bc8c4c4246`.
It embeds the raw source-report hash, an explicit redaction manifest, and
verifiable self-excluding derivative-payload SHA-256
`f1c5c4b9c83f99f89a0a5039c0fcbb7dca9ed458cccd49e37bc961e645565ae2`.

Final cleanup passed. It deleted only the batch-owned snapshot, restored the
exact eight-ID snapshot inventory, and left the VM suspended. The current
snapshot pointer was then switched without resume to the unchanged original
immutable base `{35dba943-a22d-473c-b1b0-44fa6326e626}`. The retained clean
qualification base and both footage branches remain present.

This evidence qualifies the tested RDP transport and input path for this exact
task and environment. It does **not** qualify Citrix ICA/HDX, general desktop
workflow reliability, or any untested Windows application.
