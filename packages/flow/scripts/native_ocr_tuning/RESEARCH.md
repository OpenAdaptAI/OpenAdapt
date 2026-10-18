# Apple Vision text recognition: published accuracy advice, and what we measured

Sources and what they recommend, followed by what each did on the identity
evidence. `harness.py --config NAME --out DIR` measures one configuration;
`table.py DIR` collects the runs into one table.

| advice | source | measured here |
|---|---|---|
| Use `.accurate` for small or hard text; `.fast` for live capture | WWDC19 session 234 (https://developer.apple.com/videos/play/wwdc2019/234/), slides (https://devstreaming-cdn.apple.com/videos/wwdc/2019/234rye7511ohnx5dmvl/234/234_text_recognition_in_vision_framework.pdf), Hacking with Swift (https://www.hackingwithswift.com/example-code/vision/how-to-use-vnrecognizetextrequests-optical-character-recognition-to-detect-text-in-an-image) | already `.accurate`; `fast` measured as a control |
| Turn language correction off for codes and identifiers | WWDC19 234; Apple sample "Extracting phone numbers from text in images" (https://apple-docs.everest.mt/docs/vision/extracting-phone-numbers-from-text-in-images/); uitag OCR experiments (https://github.com/swaylenhayes/uitag/blob/main/docs/research/ocr-rescan-experiments.md); Cosenza (https://danielcosenza.com/posts/mac-vision-text-recognition-pipeline/) | already off; `lc_on` measured |
| `customWords` for domain vocabulary | WWDC19 234; Hacking with Swift | not applicable: identifiers are unknown at record time, and uitag found no effect with correction off (customWords works through the language model) |
| Set `recognitionLanguages` when known | WWDC19 234; Ben Dodson (https://bendodson.com/weblog/2019/06/11/detecting-text-with-vnrecognizetextrequest-in-ios-13/) | already `en-US` |
| `automaticallyDetectsLanguage` (revision 3) | WWDC22 "What's new in Vision" (https://developer.apple.com/videos/play/wwdc2022/10024/) | not used: the language is fixed |
| `minimumTextHeight` (fraction of image height; larger = faster, drops small text) | WWDC19 234; Apple docs (https://developer.apple.com/documentation/vision/vnrecognizetextrequest/minimumtextheight); SO "minimumTextHeight has no effect" (https://stackoverflow.com/questions/74746653) | the request already defaults to 0.0 on this macOS (revision 3); setting 0 or 0.005 changed nothing |
| `regionOfInterest` / crop to the subject | WWDC19 234 | measured as tiling (tall frames) and per-row crops (identity bands) |
| Upscale small text (2x, 3x; crop-then-upscale for tiny details) | macos-vision-ocr skill notes (https://skillsmp.com/creators/qianjinguo/wiki/skills-devops-macos-vision-ocr) | measured at 1.5x/2x/3x, Lanczos and bicubic, whole image and short crops only |
| Pad crops; crop boundaries are chaotically sensitive; multi-crop ensemble | uitag OCR experiments | measured: white padding, and union/vote across paddings |
| Grayscale/contrast/binarize/sharpen preprocessing | uitag (no sweet spot), SBC study (https://sol.sbc.org.br/index.php/sibgrapi_estendido/article/download/38292/38066/), r/iOSProgramming (https://www.reddit.com/r/iOSProgramming/comments/1n5xd01/improving_apples_ocr_accuracy/) all report it does not help or hurts | grayscale measured |
| Use `topCandidates(n)` and choose by domain constraints | WWDC19 234; Apple docs (https://developer.apple.com/documentation/vision/vnrecognizedtextobservation/topcandidates(_:)); plate OCR (https://nikoskatsikanis.com/blog/ios-plate-ocr/) | with language correction OFF Vision returns exactly one candidate per line (0 of 304 and 0 of 312 lines had a second one on two record frames); with it ON, 271/305 lines had alternatives, mostly truncations ("MRN" -> "MR", "MN", "RN"). Measured only as an agreement check (correction on vs off must agree) |
| Word-level boxes via `boundingBox(for:)` | Apple docs (https://apple-docs.everest.mt/docs/vision/vnrecognizedtext/boundingbox(for:)/), SO 78155832 | not needed: the dense table already comes back one cell per observation |
| Confidence is not a calibrated probability | Cosenza | observed: Vision returns 0.3/0.5/1.0 only; single letters ("M", "F") usually 0.3 |

## What we found that the advice does not cover

1. **Tall frames lose whole columns.** On the 2240x3702 record frame Vision
   returned the Status column for 5 of 41 rows, and no "Status" header. A
   128 px strip of the same frame read every cell. Reading tall images in
   overlapping horizontal strips fixed the record side.
2. **Revision 3 reads cut-off band crops upside down.** A 64 px replay band
   at 1x slices the rows above and below; on Georgia text revision 3 read the
   whole crop rotated 180 degrees ("MG367459" -> "69tL980W"). The
   observation's quad shows it (`topLeft.x > topRight.x`). Revision 2 has
   no orientation step and reads the same crop upright. Screens are upright,
   so an upside-down read is always wrong.
