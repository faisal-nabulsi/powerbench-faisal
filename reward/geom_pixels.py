"""geom_pixels.py -- deterministic whitespace / content-density scoring from
rendered slide images (PNG), using only numpy + PIL (no scipy on this box).

Public API
----------
    score_pixels(png_paths: list) -> {'whitespace': float}

Score convention (shared by every metric in this project):
    float in [0.0, 1.0], 1.0 = GOOD, 0.0 = BAD.

Method (AeSlides-style local-variance content mask)
---------------------------------------------------
1. Load each PNG, flatten alpha onto white, convert to grayscale in [0, 1].
2. Compute the LOCAL STANDARD DEVIATION sigma over a square neighbourhood.
   Implemented with summed-area tables (integral images) so it is O(N) and
   needs nothing beyond numpy.  The window side scales with the image so the
   result is resolution-invariant (a slide rendered at 2x gives the same
   answer).
3. Normalize:  F = min(sigma, T_CLIP) / T_CLIP,  then threshold at TAU=0.05
   to get a binary "this pixel is near real content" mask.
4. content_ratio = mask.mean()  -- the fraction of the slide carrying content.

5. Map content_ratio -> score so that BOTH extremes are bad:
       nearly empty slide (ratio ~ 0)  -> score ~ 0
       crammed edge-to-edge (ratio ~ 1) -> score ~ 0
       well composed slide             -> score 1.0
   The good band is BAND_LO..BAND_HI (0.15 .. 0.40).  Outside the band the
   score falls off with a smoothstep, NOT a cliff, so the reward surface has a
   usable gradient for RL everywhere on [0, 1].

ANTI-PADDING PROPERTY
---------------------
Dumping extra text onto a slide raises content_ratio.  Once it passes
BAND_HI the score strictly decreases, all the way to ~0 for a wall of text.
So "add more words" is never a free win.
"""

from __future__ import annotations

import numpy as np
from PIL import Image

__all__ = ["score_pixels"]

# ---------------------------------------------------------------- parameters
T_CLIP = 0.25   # sigma clip, in normalized [0,1] grey units
TAU = 0.05      # threshold on F = min(sigma,T_CLIP)/T_CLIP
                # -> effective sigma threshold = 0.0125 ~= 3.2 grey levels

BAND_LO = 0.15  # content_ratio at/above which a slide is "not too empty"
BAND_HI = 0.40  # content_ratio at/below which a slide is "not too crammed"
EMPTY_AT = 0.00 # content_ratio giving score 0 on the empty side
FULL_AT = 1.00  # content_ratio giving score 0 on the crammed side

WIN_DIVISOR = 64    # window side ~= min(H, W) / WIN_DIVISOR  (odd, >= 3)
MAX_SIDE = 1600     # downscale huge renders before analysis (speed only)


# ------------------------------------------------------------------ helpers
def _smoothstep(edge0: float, edge1: float, x: float) -> float:
    """Classic 3t^2 - 2t^3 ramp: 0 at edge0, 1 at edge1, C1-continuous."""
    if edge1 == edge0:
        return 1.0 if x >= edge1 else 0.0
    t = (float(x) - edge0) / (edge1 - edge0)
    t = min(1.0, max(0.0, t))
    return t * t * (3.0 - 2.0 * t)


def _load_gray(path) -> "np.ndarray | None":
    """PNG -> float64 grayscale array in [0,1], alpha flattened onto white.

    Returns None if the file cannot be read/decoded.
    """
    try:
        with Image.open(path) as im:
            im.load()
            if im.mode in ("RGBA", "LA") or (
                im.mode == "P" and "transparency" in im.info
            ):
                rgba = im.convert("RGBA")
                bg = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
                im = Image.alpha_composite(bg, rgba).convert("L")
            else:
                im = im.convert("L")

            w, h = im.size
            if w < 2 or h < 2:
                return None
            longest = max(w, h)
            if longest > MAX_SIDE:
                s = MAX_SIDE / float(longest)
                im = im.resize(
                    (max(2, int(round(w * s))), max(2, int(round(h * s)))),
                    Image.LANCZOS,
                )
            arr = np.asarray(im, dtype=np.float64) / 255.0
    except Exception:
        return None

    if arr.ndim != 2 or arr.size == 0 or not np.isfinite(arr).all():
        return None
    return arr


def _window_side(h: int, w: int) -> int:
    """Odd window side that scales with the image (resolution invariance)."""
    win = int(round(min(h, w) / float(WIN_DIVISOR)))
    if win < 3:
        win = 3
    if win % 2 == 0:
        win += 1
    win = min(win, min(h, w))
    if win % 2 == 0:
        win -= 1
    return max(3, win)


def _local_std(img: np.ndarray, win: int) -> np.ndarray:
    """Local standard deviation over a win x win box, via integral images.

    Border pixels use the clipped (partial) window, with the true pixel count,
    so no padding artefacts are introduced.
    """
    h, w = img.shape
    r = win // 2

    # Centring kills catastrophic cancellation in E[x^2] - E[x]^2.
    x = img - img.mean()

    ii = np.zeros((h + 1, w + 1), dtype=np.float64)
    ii2 = np.zeros((h + 1, w + 1), dtype=np.float64)
    np.cumsum(np.cumsum(x, axis=0), axis=1, out=ii[1:, 1:])
    np.cumsum(np.cumsum(x * x, axis=0), axis=1, out=ii2[1:, 1:])

    ys0 = np.clip(np.arange(h) - r, 0, h)
    ys1 = np.clip(np.arange(h) + r + 1, 0, h)
    xs0 = np.clip(np.arange(w) - r, 0, w)
    xs1 = np.clip(np.arange(w) + r + 1, 0, w)

    def _box(table):
        return (
            table[np.ix_(ys1, xs1)]
            - table[np.ix_(ys0, xs1)]
            - table[np.ix_(ys1, xs0)]
            + table[np.ix_(ys0, xs0)]
        )

    cnt = ((ys1 - ys0)[:, None] * (xs1 - xs0)[None, :]).astype(np.float64)
    mean = _box(ii) / cnt
    meansq = _box(ii2) / cnt
    var = np.maximum(meansq - mean * mean, 0.0)
    return np.sqrt(var)


def content_ratio(img: np.ndarray) -> float:
    """Fraction of the image covered by the local-variance content mask."""
    win = _window_side(*img.shape)
    sigma = _local_std(img, win)
    f = np.minimum(sigma, T_CLIP) / T_CLIP
    return float((f >= TAU).mean())


def density_score(ratio: float) -> float:
    """content_ratio -> [0,1] score; 1.0 inside the band, smooth falloff out.

    Both tails are smooth all the way to their endpoints (no flat dead zone
    other than at the exact extremes), so RL always sees a gradient.
    """
    rise = _smoothstep(EMPTY_AT, BAND_LO, ratio)          # punishes emptiness
    fall = 1.0 - _smoothstep(BAND_HI, FULL_AT, ratio)     # punishes cramming
    return float(min(1.0, max(0.0, rise * fall)))


# --------------------------------------------------------------- public API
def score_pixels(png_paths: list) -> dict:
    """Returns {'whitespace': float} in [0,1], 1.0 = good (well-balanced
    content density).

    png_paths: list of paths to rendered slide PNGs.
    Empty list -> 0.0.  Unreadable images are skipped; if none are readable
    the score is 0.0.
    """
    if not png_paths:
        return {"whitespace": 0.0}

    scores = []
    for p in png_paths:
        img = _load_gray(p)
        if img is None:
            continue
        scores.append(density_score(content_ratio(img)))

    if not scores:
        return {"whitespace": 0.0}
    return {"whitespace": float(np.clip(np.mean(scores), 0.0, 1.0))}


def score_pixels_detail(png_paths: list) -> dict:
    """Same computation, but also reports the per-slide content ratios.
    Diagnostics only -- the reward should use score_pixels().
    """
    ratios, per_slide = [], []
    for p in png_paths or []:
        img = _load_gray(p)
        if img is None:
            continue
        r = content_ratio(img)
        ratios.append(r)
        per_slide.append(density_score(r))
    return {
        "whitespace": score_pixels(png_paths)["whitespace"],
        "content_ratios": ratios,
        "per_slide": per_slide,
    }


# ------------------------------------------------------------------ selftest
if __name__ == "__main__":
    import os
    import tempfile

    from PIL import ImageDraw, ImageFont

    W, H = 1280, 720

    def _font(size):
        try:
            return ImageFont.load_default(size=size)
        except Exception:
            return ImageFont.load_default()

    def _blank(path):
        """(a) nearly blank: just one small title word."""
        im = Image.new("RGB", (W, H), "white")
        d = ImageDraw.Draw(im)
        d.text((80, 60), "Title", fill="black", font=_font(34))
        im.save(path)

    def _moderate(path):
        """(b) moderate: a normally composed slide -- title, bullets, chart."""
        im = Image.new("RGB", (W, H), "white")
        d = ImageDraw.Draw(im)
        d.text((70, 45), "Quarterly Results", fill="black", font=_font(52))
        d.line((70, 115, 1210, 115), fill="black", width=3)
        body = _font(26)
        lines = [
            "Revenue grew 18 percent year over year",
            "Churn fell to 2.1 percent in enterprise",
            "Two new regions launched early",
            "Gross margin held steady at 71 percent",
            "Headcount plan unchanged for H2",
            "Pipeline coverage now at 3.4x",
        ]
        y = 165
        for ln in lines:
            d.text((80, y), "- " + ln, fill="black", font=body)
            y += 52
        # a chart-like block on the right (thin bars + axis + labels)
        x0, ybase = 700, 560
        d.line((x0 - 20, ybase, 1200, ybase), fill="black", width=3)
        d.line((x0 - 20, 200, x0 - 20, ybase), fill="black", width=3)
        small = _font(18)
        for k, hgt in enumerate((120, 190, 165, 260, 300)):
            bx = x0 + k * 95
            d.rectangle((bx, ybase - hgt, bx + 55, ybase), outline="black", width=3)
            d.text((bx + 8, ybase + 10), "Q%d" % (k + 1), fill="black", font=small)
        d.text((700, 620), "Bookings by quarter (USD m)", fill="black", font=small)
        im.save(path)

    def _dense(path):
        """(c) crammed: wall of small text, edge to edge."""
        im = Image.new("RGB", (W, H), "white")
        d = ImageDraw.Draw(im)
        body = _font(15)
        words = (
            "revenue churn margin pipeline retention expansion cohort "
            "runway burn bookings backlog attach renewal upsell segment "
        ).split()
        y = 2
        i = 0
        while y < H - 6:
            line, wdt = [], 0
            while wdt < W - 12:
                w_ = words[i % len(words)]
                i += 1
                line.append(w_)
                wdt += 8 * (len(w_) + 1)
            d.text((4, y), " ".join(line), fill="black", font=body)
            y += 17
        im.save(path)

    _PAD_WORDS = (
        "revenue grew strongly across every enterprise segment while churn "
        "declined and the pipeline expanded faster than the plan assumed "
        "margins held steady as bookings accelerated into the second half "
        "retention improved and expansion offset a softer new logo quarter "
    ).split()

    def _padded(path, n_words):
        """Same slide, progressively padded with more body copy.

        Body font and line spacing are FIXED; the extra words simply wrap and
        push further down the slide. This is what "the agent dumped more text
        on the slide" actually looks like, and it is monotone in ink.
        """
        im = Image.new("RGB", (W, H), "white")
        d = ImageDraw.Draw(im)
        d.text((25, 20), "Quarterly Results", fill="black", font=_font(40))
        body = _font(15)
        margin, top, spacing = 20, 90, 17
        maxw = W - 2 * margin

        y, i, drawn = top, 0, 0
        while drawn < n_words and y < H - spacing:
            line, used = [], 0.0
            while drawn + len(line) < n_words:
                cand = _PAD_WORDS[(i + len(line)) % len(_PAD_WORDS)]
                adv = d.textlength((" " if line else "") + cand, font=body)
                if used + adv > maxw:
                    break
                line.append(cand)
                used += adv
            if not line:
                break
            d.text((margin, y), " ".join(line), fill="black", font=body)
            drawn += len(line)
            i += len(line)
            y += spacing
        im.save(path)

    with tempfile.TemporaryDirectory(prefix="geom_pixels_selftest_") as tmp:
        pa = os.path.join(tmp, "a_blank.png")
        pb = os.path.join(tmp, "b_moderate.png")
        pc = os.path.join(tmp, "c_dense.png")
        _blank(pa)
        _moderate(pb)
        _dense(pc)

        print("=== three synthetic slides ===")
        print("%-22s %12s %8s" % ("image", "content_ratio", "score"))
        for name, p in (("(a) nearly blank", pa), ("(b) moderate", pb),
                        ("(c) crammed dense", pc)):
            det = score_pixels_detail([p])
            print("%-22s %12.4f %8.4f"
                  % (name, det["content_ratios"][0], det["whitespace"]))

        sa = score_pixels([pa])["whitespace"]
        sb = score_pixels([pb])["whitespace"]
        sc = score_pixels([pc])["whitespace"]
        print()
        print("assert b > a  ->", sb > sa, " (%.4f > %.4f)" % (sb, sa))
        print("assert b > c  ->", sb > sc, " (%.4f > %.4f)" % (sb, sc))
        assert sb > sa and sb > sc, "moderate slide must win"

        print()
        print("=== ANTI-PADDING sweep: same slide, more and more text ===")
        print("%-10s %14s %8s" % ("n_words", "content_ratio", "score"))
        prev_r = -1.0
        rows = []
        for n in (0, 25, 55, 95, 145, 205, 280, 370, 470, 590, 720, 860, 1000):
            p = os.path.join(tmp, "pad_%03d.png" % n)
            _padded(p, n)
            det = score_pixels_detail([p])
            r, s = det["content_ratios"][0], det["whitespace"]
            rows.append((n, r, s))
            print("%-10d %14.4f %8.4f" % (n, r, s))
            assert r >= prev_r - 1e-9, "content_ratio must rise as text is added"
            prev_r = r

        peak_i = max(range(len(rows)), key=lambda i: rows[i][2])
        n_peak, r_peak, s_peak = rows[peak_i]
        n_last, r_last, s_last = rows[-1]
        print()
        print("peak at n=%d (ratio %.4f, score %.4f); "
              "over-padded n=%d -> ratio %.4f, score %.4f"
              % (n_peak, r_peak, s_peak, n_last, r_last, s_last))
        assert rows[-1][1] > rows[0][1] + 0.5, "padding must raise content_ratio"
        assert s_peak > 0.99, "a well-composed slide must reach ~1.0"
        assert s_last < 0.35, "over-padding must drive the score near zero"
        print("ANTI-PADDING HOLDS: adding text past the band strictly lowers the score")

        print()
        print("=== multi-slide average ===")
        print("all three:", score_pixels([pa, pb, pc]))
        print("b twice  :", score_pixels([pb, pb]))

        print()
        print("=== edge cases ===")
        print("empty list      :", score_pixels([]))
        print("missing file    :", score_pixels([os.path.join(tmp, "nope.png")]))
        bad = os.path.join(tmp, "bad.png")
        with open(bad, "wb") as fh:
            fh.write(b"this is not a png")
        print("corrupt file    :", score_pixels([bad]))
        print("corrupt + good b:", score_pixels([bad, pb]))

        print()
        print("=== resolution invariance (same slide, 2x render) ===")
        big = os.path.join(tmp, "b_2x.png")
        Image.open(pb).resize((W * 2, H * 2), Image.LANCZOS).save(big)
        print("1x:", score_pixels_detail([pb])["content_ratios"][0],
              "2x:", score_pixels_detail([big])["content_ratios"][0])

        print()
        print("=== score curve (density_score vs content_ratio) ===")
        for r in (0.0, 0.02, 0.05, 0.10, 0.15, 0.25, 0.40, 0.50,
                  0.60, 0.70, 0.85, 0.95, 1.0):
            print("  ratio %.2f -> %.4f" % (r, density_score(r)))

    print()
    print("SELFTEST OK")
