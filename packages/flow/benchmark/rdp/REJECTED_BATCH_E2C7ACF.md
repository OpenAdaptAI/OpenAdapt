# Rejected real-RDP batch: `e2c7acf`

Candidate `e2c7acf238d42ccf802461457a9f6503328c96e3` ran one fresh,
fixed batch of exactly three trials from retained clean qualification base
`{8f43c385-0566-44d3-9e50-f646777d315b}`. There were no retries.

The result was 0/3. All three trials proved exact active RDP session `3`,
Explorer in session `3`, and the candidate's desktop-readiness gate. The
keyboard calls returned without an error receipt, but the independent
`prlctl` file oracle observed no file in any trial. Each row was therefore
classified `over_halt_or_timeout`, not success.

| Metric | Result |
| --- | ---: |
| Successes | 0/3 |
| Silent incorrect successes | 0 |
| Over-halts | 3 |
| Model calls | 0 |
| Cleanup | Passed |

The immutable JSON is `results_e2c7acf_20260718.json`, committed in
`b68bd8caf9d66e8308c7c8f23354613309c500f8`, with SHA-256
`70b88e3f55af4cb1c9192bad838f5944ca8123e9ac81db3a15fa58f9fa2e608e`.
Cleanup deleted only the batch-owned snapshot, restored the exact eight-ID
inventory, and left the retained qualification base suspended and current.
The VM was subsequently switched, without resume, to the unchanged original
base `{35dba943-a22d-473c-b1b0-44fa6326e626}` while retaining the clean base
and both footage branches.

This batch is rejected evidence. It establishes neither general RDP input
reliability nor any Citrix ICA/HDX capability.
