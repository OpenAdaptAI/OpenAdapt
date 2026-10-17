"""Experiment harness: Apple Vision accuracy tweaks vs. the identity evidence.

Runs one engine configuration (``--config NAME``) through the identity and
OCR evidence sets with the REAL record/replay code paths, swapping only the
process-wide OCR engine:

* dense sibling surface (``validation.dense_surface.run_trials``, 480 cases)
* integrated identity ladder (``validation.identity_ladder.run``, 308 cases)
* look-alike pair OCR-collapse probe (``pixel_identity_probe`` OCR baseline)
* numeric homonyms (``tests/test_identity_ocr_conservative_9th._verdict``),
  wrong row (must not verify) and correct row (should verify)

and records per-call OCR latency. Writes ``<out>/<config>.json``.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

os.environ["OPENADAPT_FLOW_OCR"] = "native"

from PIL import Image, ImageOps  # noqa: E402

import importlib  # noqa: E402

ocr_mod = importlib.import_module("openadapt_flow.vision.ocr")
from openadapt_flow.vision.ocr_backends import AppleVisionEngine, RawLine  # noqa: E402

_RESAMPLE = {"lanczos": Image.LANCZOS, "bicubic": Image.BICUBIC}


def _png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "PNG", compress_level=1)
    return buf.getvalue()


def _overlap(a: RawLine, b: RawLine) -> float:
    ax, ay, aw, ah = a[:4]
    bx, by, bw, bh = b[:4]
    ix = max(0, min(ax + aw, bx + bw) - max(ax, bx))
    iy = max(0, min(ay + ah, by + bh) - max(ay, by))
    small = min(aw * ah, bw * bh)
    return (ix * iy) / small if small > 0 else 0.0


def _sq(text: str) -> str:
    return "".join(text.split()).lower()


class TunedVision(AppleVisionEngine):
    """AppleVisionEngine with optional preprocessing passes and a merge rule.

    pass keys: scale (float), resample, gray (bool), pad (px, white border),
    dx/dy (sub-image offset crop, px), tiles (horizontal strips).
    merge: "first" (single pass), "union" (pass 0, plus lines later passes
    found where pass 0 found nothing), "vote" (keep a line only where a
    majority of passes read the same squashed text; disagreements dropped).
    """

    def __init__(
        self,
        passes: list[dict[str, Any]],
        merge: str = "first",
        language_correction: bool = False,
        level: int = 0,
        min_text_height: float | None = None,
    ) -> None:
        super().__init__()
        self.passes = passes
        self.merge = merge
        self.lc = language_correction
        self.level = level
        self.mth = min_text_height
        self.calls: list[tuple[int, int, float]] = []

    def _request(self) -> Any:
        request = super()._request()
        request.setRecognitionLevel_(self.level)
        request.setUsesLanguageCorrection_(self.lc)
        if self.mth is not None:
            request.setMinimumTextHeight_(self.mth)
        return request

    def _raw(self, png: bytes) -> list[RawLine]:
        return AppleVisionEngine.recognize_png(self, png)

    def _pass(self, img: Image.Image, p: dict[str, Any]) -> list[RawLine]:
        W, H = img.size
        tiles = int(p.get("tiles", 1))
        if tiles > 1 and H > 400:
            ov = 96
            step = H // tiles
            out: list[RawLine] = []
            for k in range(tiles):
                y0 = max(0, k * step - ov)
                y1 = min(H, (k + 1) * step + ov)
                sub = img.crop((0, y0, W, y1))
                for ln in self._pass(sub, {**p, "tiles": 1}):
                    x, y, w, h = ln[:4]
                    if (y <= 2 and y0 > 0) or (y + h >= (y1 - y0) - 2 and y1 < H):
                        continue  # cut by an internal tile edge
                    cand = (x, y + y0, w, h, ln[4], ln[5])
                    if not any(_overlap(cand, o) > 0.5 for o in out):
                        out.append(cand)
            return out
        dx, dy = int(p.get("dx", 0)), int(p.get("dy", 0))
        work = img.crop((dx, dy, W, H)) if (dx or dy) else img
        if p.get("gray"):
            work = ImageOps.grayscale(work).convert("RGB")
        pad = int(p.get("pad", 0))
        if pad:
            work = ImageOps.expand(work, border=pad, fill=(255, 255, 255))
        s = float(p.get("scale", 1.0))
        if s != 1.0:
            work = work.resize(
                (max(1, round(work.size[0] * s)), max(1, round(work.size[1] * s))),
                _RESAMPLE[p.get("resample", "lanczos")],
            )
        lines = self._raw(_png(work)) if (work is not img or s != 1.0) else None
        if lines is None:
            lines = self._raw(_png(work))
        out = []
        for x, y, w, h, t, c in lines:
            x0 = x / s - pad + dx
            y0 = y / s - pad + dy
            out.append(
                (
                    int(max(0, x0)),
                    int(max(0, y0)),
                    int(round(w / s)),
                    int(round(h / s)),
                    t,
                    c,
                )
            )
        return out

    def recognize_png(self, png: bytes) -> list[RawLine]:
        t0 = time.perf_counter()
        img = Image.open(io.BytesIO(png)).convert("RGB")
        per_pass = [self._pass(img, p) for p in self.passes]
        if self.merge == "first" or len(per_pass) == 1:
            lines = per_pass[0]
        elif self.merge == "union":
            lines = list(per_pass[0])
            for extra in per_pass[1:]:
                for ln in extra:
                    if not any(_overlap(ln, o) > 0.3 for o in lines):
                        lines.append(ln)
        elif self.merge == "vote":
            need = len(per_pass) // 2 + 1
            lines = []
            used = [set() for _ in per_pass]
            for i, src in enumerate(per_pass):
                for j, ln in enumerate(src):
                    if j in used[i]:
                        continue
                    agree, group = 1, [(i, j)]
                    for k in range(i + 1, len(per_pass)):
                        for m, o in enumerate(per_pass[k]):
                            if m in used[k] or _overlap(ln, o) <= 0.5:
                                continue
                            if _sq(o[4]) == _sq(ln[4]):
                                agree += 1
                                group.append((k, m))
                                break
                    for k, m in group:
                        used[k].add(m)
                    if agree >= need:
                        lines.append(ln)
        else:
            raise ValueError(self.merge)
        img_w, img_h = img.size
        self.calls.append((img_w, img_h, time.perf_counter() - t0))
        return lines


L, B = "lanczos", "bicubic"
CONFIGS: dict[str, dict[str, Any]] = {
    "baseline": {"passes": [{}]},
    "lc_on": {"passes": [{}], "language_correction": True},
    "fast": {"passes": [{}], "level": 1},
    "gray": {"passes": [{"gray": True}]},
    "s1.5_lanczos": {"passes": [{"scale": 1.5, "resample": L}]},
    "s1.5_bicubic": {"passes": [{"scale": 1.5, "resample": B}]},
    "s2_lanczos": {"passes": [{"scale": 2.0, "resample": L}]},
    "s2_bicubic": {"passes": [{"scale": 2.0, "resample": B}]},
    "s3_lanczos": {"passes": [{"scale": 3.0, "resample": L}]},
    "s0.5_lanczos": {"passes": [{"scale": 0.5, "resample": L}]},
    "pad16": {"passes": [{"pad": 16}]},
    "tiles4": {"passes": [{"tiles": 4}]},
    "gray_s1.5": {"passes": [{"gray": True, "scale": 1.5, "resample": L}]},
    "union_orig_gray": {"passes": [{}, {"gray": True}], "merge": "union"},
    "union_orig_s1.5": {"passes": [{}, {"scale": 1.5, "resample": L}], "merge": "union"},
    "vote_orig_gray_s1.5": {
        "passes": [{}, {"gray": True}, {"scale": 1.5, "resample": L}],
        "merge": "vote",
    },
    "vote_gray_s1.5_s2": {
        "passes": [
            {"gray": True},
            {"scale": 1.5, "resample": L},
            {"scale": 2.0, "resample": B},
        ],
        "merge": "vote",
    },
    "vote_offsets_gray": {
        "passes": [{"gray": True}, {"gray": True, "dx": 1, "dy": 1}, {"gray": True, "dx": 2, "dy": 0}],
        "merge": "vote",
    },
}


def install(name: str) -> TunedVision:
    cfg = dict(CONFIGS[name])
    eng = TunedVision(**cfg)
    ocr_mod._engine = eng
    ocr_mod.clear_ocr_memo()
    return eng


def run_dense(seeds: list[int]) -> dict[str, Any]:
    from openadapt_flow.validation import dense_surface as D

    res = D.run_trials(seeds, n_rows=40)
    armed = [t for t in res["trials"] if t["armed"]]
    fa = [t for t in armed if t["is_false_abort"]]
    by: dict[str, dict[str, int]] = {}
    for t in armed:
        d = by.setdefault(t["replay_condition"], {"n": 0, "fa": 0, "facc": 0})
        d["n"] += 1
        d["fa"] += int(t["is_false_abort"])
        d["facc"] += int(t["is_false_accept"])
    return {
        "n": len(armed),
        "false_abort": len(fa),
        "false_accept": sum(t["is_false_accept"] for t in armed),
        "fa_mismatch": sum(t["fa_status"] == "mismatch" for t in fa),
        "fa_abstain": sum(t["fa_status"] == "abstain" for t in fa),
        "fa_unreadable": sum(t["fa_status"] == "unreadable" for t in fa),
        "unarmed": len(res["trials"]) - len(armed),
        "by_condition": by,
        "trials": [
            {k: t[k] for k in (
                "seed", "collision_class", "click_config", "replay_condition",
                "context_text", "fa_status", "fa_observed", "acc_status",
                "acc_observed", "is_false_abort", "is_false_accept")}
            for t in res["trials"]
        ],
    }


def run_ladder(out: Path) -> dict[str, Any]:
    from openadapt_flow.validation import identity_ladder as IL

    s = IL.run(out)
    cfgs = s["configs"]
    return {
        "n": sum(c["n_correct"] + c["n_wrong"] for c in cfgs.values()),
        "false_accept": sum(c["false_accept"] for c in cfgs.values()),
        "over_halt": sum(c["over_halt"] for c in cfgs.values()),
        "n_correct": sum(c["n_correct"] for c in cfgs.values()),
        "configs": cfgs,
    }


def run_pairs() -> dict[str, Any]:
    from openadapt_flow.validation import pixel_identity_probe as P

    sep = P.run_separation(P.COLLAPSE_PAIRS, with_ocr=True)
    base = sep.get("ocr_baseline", [])
    return {
        "n_pairs": len(P.COLLAPSE_PAIRS),
        "ocr_collapsed": sum(1 for r in base if r.get("ocr_collapsed")),
        "ocr_baseline": base,
    }


def run_homonyms() -> dict[str, Any]:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tests"))
    import test_identity_ocr_conservative_9th as T
    from openadapt_flow.validation.dense_surface import RenderCondition

    cases = list(T._NUMERIC_HOMONYMS)
    wrong_verified = 0
    correct_verified = 0
    rows = []
    for r, s, f, p in cases:
        cond = RenderCondition(f"{f}_{p}", f, p, 2, 6)
        _, _, _, wrong = T._verdict(r, s, cond)
        _, _, _, right = T._verdict(r, r, cond)
        wrong_verified += wrong.status == "verified"
        correct_verified += right.status == "verified"
        rows.append((r, s, f, p, wrong.status, right.status))
    return {
        "n": len(cases),
        "false_accept": wrong_verified,
        "correct_verified": correct_verified,
        "rows": rows,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True, choices=sorted(CONFIGS))
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--sets", nargs="+", default=["dense", "ladder", "pairs", "homonyms"])
    ap.add_argument("--seeds", type=int, nargs="+", default=[1, 2, 3, 4, 5])
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    eng = install(args.config)
    result: dict[str, Any] = {"config": args.config, "spec": CONFIGS[args.config],
                              "engine": eng.contract(),
                              "started": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    t0 = time.time()
    for name in args.sets:
        ts = time.time()
        if name == "dense":
            result["dense"] = run_dense(args.seeds)
        elif name == "ladder":
            result["ladder"] = run_ladder(args.out / f"{args.config}_ladder")
        elif name == "pairs":
            result["pairs"] = run_pairs()
        elif name == "homonyms":
            result["homonyms"] = run_homonyms()
        result.setdefault("set_seconds", {})[name] = round(time.time() - ts, 1)
    big = [c[2] for c in eng.calls if c[0] * c[1] >= 1_000_000]
    small = [c[2] for c in eng.calls if c[0] * c[1] < 1_000_000]

    def stats(xs: list[float]) -> dict[str, Any]:
        if not xs:
            return {"n": 0}
        xs = sorted(xs)
        return {"n": len(xs), "mean_ms": round(1000 * sum(xs) / len(xs), 1),
                "p50_ms": round(1000 * xs[len(xs) // 2], 1),
                "p95_ms": round(1000 * xs[int(len(xs) * 0.95)], 1)}

    result["latency"] = {"full_frame": stats(big), "crop": stats(small)}
    result["wall_s"] = round(time.time() - t0, 1)
    result["finished"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    result["load"] = os.getloadavg()
    (args.out / f"{args.config}.json").write_text(json.dumps(result, indent=1, default=str))
    d = result.get("dense", {})
    print(f"RESULT {args.config} dense FA={d.get('false_abort')}/{d.get('n')} "
          f"FACC={d.get('false_accept')} mism={d.get('fa_mismatch')} "
          f"ladder FACC={result.get('ladder', {}).get('false_accept')} "
          f"halt={result.get('ladder', {}).get('over_halt')} "
          f"hom FACC={result.get('homonyms', {}).get('false_accept')} "
          f"ok={result.get('homonyms', {}).get('correct_verified')} "
          f"lat={result['latency']} wall={result['wall_s']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
