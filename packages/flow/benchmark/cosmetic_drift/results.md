# Cosmetic-drift sweep -- results matrix

Generated: 2026-07-11 16:57:55  
Platform: Darwin arm64 py3.12.9  
Bundle: 11 steps, recorded at 1280x800 dsf=1.  
Template scale ladder: [0.85, 1.0, 1.18]; template threshold 0.985.  

| axis | perturbation | outcome | SAFE? | steps ok | failed step | last rung | heals | rungs used |
|---|---|---|---|---|---|---|---|---|
| baseline | baseline (100%, 1x, default font) | pass | safe | 11/11 | - | template | 0 | template:8 |
| zoom | zoom 80% | safe-halt | safe | 0/1 | step_000 | geometry | 0 | - |
| zoom | zoom 90% | safe-halt | safe | 0/1 | step_000 | geometry | 0 | - |
| zoom | zoom 110% | safe-halt | safe | 0/1 | step_000 | geometry | 0 | - |
| zoom | zoom 125% | safe-halt | safe | 0/1 | step_000 | geometry | 0 | - |
| zoom | zoom 133% | safe-halt | safe | 0/1 | step_000 | geometry | 0 | - |
| zoom | zoom 150% | safe-halt | safe | 0/1 | step_000 | geometry | 0 | - |
| zoom | zoom 175% | safe-halt | safe | 0/1 | step_000 | geometry | 0 | - |
| zoom | zoom 200% | safe-halt | safe | 0/1 | step_000 | geometry | 0 | - |
| dpi | DPI 1.5x | safe-halt | safe | 0/1 | step_000 | geometry | 0 | - |
| dpi | DPI 2.0x | safe-halt | safe | 0/1 | step_000 | geometry | 0 | - |
| dpi | DPI 3.0x | safe-halt | safe | 0/1 | step_000 | geometry | 0 | - |
| fontsize | font-size +10% | safe-halt | safe | 0/1 | step_000 | geometry | 0 | - |
| fontsize | font-size +19% (19px) | safe-halt | safe | 0/1 | step_000 | geometry | 0 | - |
| fontsize | font-size +37% | safe-halt | safe | 0/1 | step_000 | geometry | 0 | - |
| fontfamily | font Georgia (serif) | pass | safe | 11/11 | - | ocr | 8 | geometry:3, ocr:5 |
| fontfamily | font Times New Roman (serif) | pass | safe | 11/11 | - | ocr | 5 | ocr:5, template:3 |
| fontfamily | font Courier New (monospace) | safe-halt | safe | 5/6 | step_005 | ocr | 1 | ocr:1, template:2 |
| combo | zoom 125% + DPI 2x | safe-halt | safe | 0/1 | step_000 | geometry | 0 | - |
| combo | zoom 133% + DPI 1.5x | safe-halt | safe | 0/1 | step_000 | geometry | 0 | - |
| combo | zoom 110% + font +19% + Georgia | safe-halt | safe | 0/1 | step_000 | geometry | 0 | - |

**Wrong-actions / crashes: 0 of 21 points.**
