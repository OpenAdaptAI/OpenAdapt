# Desktop Benchmark (Phase 2) — compiled vision replay vs UIA incumbent

_Generated 2026-07-10T22:28:31.896919+00:00_

**Task.** Patient Notes (WinForms) search -> select -> note -> save; DB-ground-truth judge; $0 (no model calls)

**Substrate.** Parallels Windows 11 ARM VM on Apple M2 Max; WindowsBackend over in-guest WAA HTTP shim (session 1)

> WinForms substitute for OpenDental (trial not no-touch installable; see PHASE2.md).

## Headline

| Arm | n | success | wrong-action | safe-halt | false-abort | success rate | wrong-action rate |
|---|--:|--:|--:|--:|--:|--:|--:|
| `compiled` | 21 | 6 | 0 | 15 | 9 | 29% | 0% |
| `uia_identity` | 21 | 21 | 0 | 0 | 0 | 100% | 0% |
| `uia_positional` | 21 | 15 | 6 | 0 | 0 | 71% | 29% |

## Identity transfer to desktop-rendered text

- Compiled-arm **armed coverage**: 2/4 click steps carry an identity band (50%).
- UIA-tree quality: 5/6 workflow targets expose a usable AutomationId (83%); the identity-critical patient row does **not** (`identity_target_has_id=False`) — the measured 'vision is necessary' evidence.

## Outcome matrix (per arm × condition)

| Arm | clean | render_125 | render_150 | theme_dark | data_reorder | data_decoy | data_siblings |
|---|---|---|---|---|---|---|---|
| `compiled` | 3/3✓ | 0/3✓ 3⚠abort | 0/3✓ 3⚠abort | 0/3✓ 3⚠abort | 3/3✓ | 0/3✓ | 0/3✓ |
| `uia_identity` | 3/3✓ | 3/3✓ | 3/3✓ | 3/3✓ | 3/3✓ | 3/3✓ | 3/3✓ |
| `uia_positional` | 3/3✓ | 3/3✓ | 3/3✓ | 3/3✓ | 3/3✓ | 0/3✓ 3✗wrong | 0/3✓ 3✗wrong |

## Reading

- **Success** = the right patient got the right note and no one else did (DB ground truth). **Wrong-action** = a note landed on a different patient (silent mis-write). **Safe-halt** = the arm stopped without writing. **False-abort** = a safe-halt on a purely cosmetic condition (render-scale/theme) where the target was still present.
- Caveats (ARM+x64 emulation rendering, render-scale-as-DPI proxy, WinForms substitute for OpenDental) are in `docs/desktop/LIMITS.md`.

## Verdict (honest, both ways)

1. **The mechanism exists on desktop.** Record → compile → replay of a real WinForms workflow runs deterministically over the vision-only `WindowsBackend`, judged by DB ground truth — on a pixel substrate with no browser DOM. Identity bands are extracted and verified on **desktop-rendered** text.
2. **Vision replay is defeated by render-scale and theme drift** (render_125/150 and theme_dark → 0% success, all safe-halts / false-aborts). This is the pre-committed 'DPI is ugly' result and the roadmap justification for multi-scale / appearance-invariant matching. It **never mis-wrote** under cosmetic drift — it halted.
3. **The positional UIA incumbent silently mis-writes** under *any* name-collision drift (decoy and siblings) — the exact wrong-action the identity work targets, measured on the incumbent.
4. **Identity verification transfers to desktop-rendered text.** On the current identity matcher (ROC operating point of #16/#19: coverage + contradicted-char / suspect / unexplained-name / absent-name budgets, all judged together) the compiled arm **safe-halts on both** the discriminable decoy (distinct surname/DOB → 3/3 halted) **and** the near-lexical sibling (Sorenson≈Sorensen, adjacent DOB → 3/3 halted) — **0 identity wrong-actions**. The same budgets that close the browser wrong-patient reopenings fire on OCR'd desktop text: a 1-char surname / multi-digit DOB difference registers as *contradicted characters* (affirmative evidence of a different entity), not OCR jitter, so the band is judged a MISMATCH and no note is written. The browser identity fixes **do transfer** to the pixel substrate. UIA-identity distinguishes the same sibling only by exact cell-text equality — a lever that vanishes on a broken-a11y or pixel-only substrate, where the vision matcher is the only one available. (An earlier draft of this benchmark ran the compiled arm against a *pre-#16* matcher and recorded 3 sibling wrong-actions; that was a stale-code artifact and is corrected here.)
