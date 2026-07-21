# Third-party native-runtime notices

These records cover third-party files embedded in the self-contained Desktop
engine. The build copies the applicable installed ONNX Runtime license and
third-party notice verbatim and bundles this repository's pinned RapidOCR
license and attribution alongside the OCR models. CI inventories the actual
PyInstaller archive and refuses an artifact missing any of these files.

## ONNX Runtime

- Source: `https://pypi.org/project/onnxruntime/`
- Upstream repository: `https://github.com/microsoft/onnxruntime`
- Locked versions: `1.20.1` on macOS Intel; `1.27.0` elsewhere
- Package paths copied without modification: `onnxruntime/LICENSE` and
  `onnxruntime/ThirdPartyNotices.txt`
- License: MIT plus the dependency notices in `ThirdPartyNotices.txt`
- `1.20.1` CPython 3.12 macOS universal2 wheel SHA-256:
  `22b0655e2bf4f2161d52706e31f517a0e54939dc393e92577df51808a7edc8c9`
- `1.20.1` license SHA-256:
  `2f07c72751aed99790b8a4869cf2311df85a860b22ded05fa22803587a48922c`
- `1.20.1` third-party notices SHA-256:
  `cf7342f7ba482ef715ae58f5f497a8d3564fa255164175aea324cd293c5701a0`
- `1.27.0` license SHA-256:
  `2f07c72751aed99790b8a4869cf2311df85a860b22ded05fa22803587a48922c`
- `1.27.0` third-party notices SHA-256:
  `0e07b95f3a8d6230037707c5c4a2b554d12c4cb67369669ac255635528ffcee2`

Wheel hashes for every supported platform are locked in `uv.lock`; the build
does not download mutable notice text separately.

## RapidOCR models

- Distribution: `rapidocr-onnxruntime==1.4.4`
- Distribution wheel SHA-256:
  `971d7d5f223a7a808662229df1ef69893809d8457d834e6373d3854bc1782cbf`
- Upstream repository: `https://github.com/RapidAI/RapidOCR`
- Upstream tag and commit: `v1.4.4`,
  `86ae3f5079df3422c1829cd84baf19bc8a7a9453`
- License source path: `LICENSE`
- Pinned license SHA-256:
  `3e0af25fdd06aa9586ae97adb00ea927ebe5a3805ac77d2d3a81ce5f55693333`
- License: Apache-2.0
- Modification status: the three embedded model files are unmodified
- Model SHA-256 values:
  - `ch_PP-OCRv4_det_infer.onnx`:
    `d2a7720d45a54257208b1e13e36a8479894cb74155a5efe29462512d42f49da9`
  - `ch_PP-OCRv4_rec_infer.onnx`:
    `48fc40f24f6d2a207a2b1091d3437eb3cc3eb6b676dc3ef9c37384005483683b`
  - `ch_ppocr_mobile_v2.0_cls_infer.onnx`:
    `e47acedf663230f8863ff1ab0e64dd2d82b838fceb5957146dab185a89d6215c`

The copied `rapidocr/LICENSE` is byte-for-byte identical to the pinned upstream
license. `rapidocr/NOTICE` records the upstream model attribution; it is an
OpenAdapt-authored notice and does not modify the upstream license.
