# Lending (MockLoan) resolution-ladder probe

The same compiled disbursement bundle replayed under cosmetic UI drift with a clean write (`?fault=ok`), zero model calls. `theme` breaks template matching; `rename` relabels the Authorize/Open buttons.

Generated: 2026-07-21 01:38:10  
Platform: Darwin arm64 py3.12.7  

| config | drift | replay | rows | correct disbursement | wrong write |
|---|---|---|---|---|---|
| full_ladder | none | SUCCESS | 1 | yes | no |
| full_ladder | theme | SUCCESS | 1 | yes | no |
| full_ladder | rename | SUCCESS | 1 | yes | no |
| template_only | none | SUCCESS | 1 | yes | no |
| template_only | theme | HALT | 0 | no | no |
| template_only | rename | HALT | 0 | no | no |

## Reading

- **Full ladder** (structural + template + OCR + geometry, the default): recovers cosmetic drift model-free and books the correct disbursement in 3/3 cells (all).
- **Template-only rung** (fault-isolation config): when a drift breaks the template and no grounder rung is installed it HALTS before the consequential write - 3/3 cells took no wrong action (all).
- In neither config is the money-movement step taken on a low-confidence resolve.

## Reproduce

```
python -m benchmark.lending_fault_model.resolution_ladder
```
