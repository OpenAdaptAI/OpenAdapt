# RDP input-delivery diagnosis

After the rejected `e2c7acf` batch, one uncounted diagnostic isolated the
failure without changing the runtime candidate. The repository head was
`b68bd8caf9d66e8308c7c8f23354613309c500f8`, whose only change after the
candidate was the rejected evidence JSON.

The diagnostic used a simplified command with an unquoted, no-space path,
waited 1.5 seconds after `Meta+r`, typed all 111 characters individually with
40 ms pacing, captured after every phase, and queried the final file only
through the independent guest-tools oracle. Every input call returned without
an error receipt, but the oracle remained empty.

| Frame | SHA-256 | Change from desktop |
| --- | --- | ---: |
| Desktop ready | `3d4086464827417be53c522cc70f4c4fd8babc8893e1b24c0bb8a185da3e60fd` | `0.000000` |
| After `Meta+r` | `3d4086464827417be53c522cc70f4c4fd8babc8893e1b24c0bb8a185da3e60fd` | `0.000000` |
| After typing | `3e1bd54a6aca35e8a25ecc1f5ce7c2aa1d5a2afce99d46d81ded559cfe9bb942` | `0.000211` |
| After Enter | `247666fc71a344b9ecebc79fd7729f6f7ad13e864c37967a69622c863ad181ff` | `0.000355` |

The post-`Meta+r` framebuffer was byte-identical to the ready desktop: the Run
dialog never opened. Inspection then found the exact protocol mismatch:
`press("Meta+r")` sent Meta as a physical virtual key but sent `r` through
Aardwolf's Unicode character path. A Unicode text event cannot be the physical
second member of a Windows-key shortcut. The later typing and Enter phases
therefore had no Run-dialog target.

The raw local diagnostic JSON SHA-256 is
`727430aea8ed62777982a92a107a738a2ea0fa7617b503ce3e990b12750b19b7`.
Raw screenshots remain local because the qualification desktop contains
unrelated operator artifacts. Cleanup deleted only the diagnostic-owned
snapshot, restored the exact eight-ID inventory, and the VM was returned to
the unchanged suspended original base afterward.

The successor keeps Unicode for `type_text` but uses a separate, layout-bound
physical-scancode path for `press` chords, with matched reverse releases and
refusal for unsupported or implicit-modifier chord characters.

## Successor proof

One uncounted, snapshot-isolated proof then exercised exact candidate
`82a658a6926ddac74b010b613535c023d0b5f079`. The post-`Meta+r` framebuffer
changed by `0.080957` and visibly contained the Windows Run dialog. All 113
input receipts returned without error, and the independent guest-tools oracle
observed the exact expected value `oaflow-input-c02a4f5a0513fe0d`.

| Frame | SHA-256 | Change from desktop |
| --- | --- | ---: |
| Desktop ready | `c30efb8a20685adc2c0f3baa2332159e426119b9e02f147ff3cad957077753c0` | `0.000000` |
| After `Meta+r` | `e144dc9f2820b55ad66972c6db87dc03d8cfad899750e8fa8869afef182c08c5` | `0.080957` |
| After typing | `b47b8a8c829a31acef488a60e9440c56bbaae95f2bcfb7583da7480b3f923ef0` | `0.081000` |
| After Enter | `a49442596f765f50752456551cd255864df4cc2a6171359aa536ba6b5934a44e` | `0.000355` |

The local proof report SHA-256 is
`55d5379ca3ee50225aadad433353b5909931e447c11db3faafecbd29aed8b412`.
Its screenshots remain local because the qualification desktop contains
unrelated operator artifacts. Cleanup deleted only the proof-owned snapshot,
restored the exact eight-ID inventory, and left the clean base suspended and
current before the counted batch began.
