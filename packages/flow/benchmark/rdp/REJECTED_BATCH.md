# Rejected real-RDP qualification batch

Candidate `a1b152640e36ccd8722e2df33252403078a87653` was evaluated in exactly
three real Aardwolf/Parallels RDP sessions on 2026-07-17. The immutable result
is `results.json` (SHA-256
`733fe8948a423dcbf2d413a10bca924cba918d196693d30cfb30f0a425ca5d15`).

The batch was rejected: all three trials failed closed before input with
`connect_or_frame_failure`. There were zero silent incorrect successes, zero
oracle writes, zero model calls, and no input returned without error.

Each failure took 92.6-96.2 seconds and raised `AssertionError`. The only
90-second assertion in that pre-input sequence is `_wait_user_shell`, which
polls `tasklist /V` for the temporary account name after Aardwolf has already
connected and delivered its first framebuffer. Aardwolf stderr then logged
`cannot unpack non-iterable NoneType object` and `Connection reset by peer`
when the rejected session was closed. This localizes the failure to the
session/shell-readiness oracle; it does not establish an input or business
outcome failure because input was never attempted.

The harness cleanup passed independently: it restored the exact suspended
base, deleted only its owned snapshot
`{042ac8d3-d09f-42c0-adc2-35d75a96e0c2}`, and proved the complete pre/post
seven-snapshot inventory identical. This candidate must not be rerun as a
counted batch. Any successor requires diagnostic evidence and a new commit.
