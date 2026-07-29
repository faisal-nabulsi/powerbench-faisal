"""Scratch lab for POWERBENCH grader v2 render-based metric design. /tmp only."""
from __future__ import annotations
import glob, json, math, os, subprocess, shutil, time
import numpy as np
import cv2
from PIL import Image

# ------------------------------------------------------------------ rendering
def render_deck(pptx, workdir, target_w=1280):
    """pptx -> list of PNG paths (ALL slides), via soffice pdf + pdftoppm."""
    os.makedirs(workdir, exist_ok=True)
    base = os.path.splitext(os.path.basename(pptx))[0]
    pdf = os.path.join(workdir, base + ".pdf")
    if not os.path.isfile(pdf):
        # CONCURRENCY: soffice shares one user profile (~/.config/libreoffice) across
        # processes. Two renders at once race on its lock; one silently produces no PDF.
        # That made the REWARD NONDETERMINISTIC -- re-scoring 12 decks, 3 changed
        # (0.5019->0.7114, 0.4437->0.1890) because the failed render fell back to a
        # neutral score. A per-render profile makes concurrent rendering safe.
        prof = os.path.join(workdir, "_loprofile")
        os.makedirs(prof, exist_ok=True)
        for _attempt in range(2):
            subprocess.run(["soffice", "-env:UserInstallation=file://" + prof,
                            "--headless", "--convert-to", "pdf",
                            "--outdir", workdir, pptx], capture_output=True, timeout=600)
            if os.path.isfile(pdf):
                break
    if not os.path.isfile(pdf):
        return []
    pref = os.path.join(workdir, base)
    got = sorted(glob.glob(pref + "-*.png"))
    if not got:
        subprocess.run(["pdftoppm", "-png", "-scale-to-x", str(target_w),
                        "-scale-to-y", "-1", pdf, pref], capture_output=True, timeout=600)
        got = sorted(glob.glob(pref + "-*.png"))
    return got

def load_rgb(path):
    with Image.open(path) as im:
        im.load()
        if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
            rgba = im.convert("RGBA")
            bg = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
            im = Image.alpha_composite(bg, rgba)
        arr = np.asarray(im.convert("RGB"), dtype=np.float32) / 255.0
    return arr

# ------------------------------------------------------------------- masks
def local_std(gray, win):
    k = (win, win)
    m = cv2.boxFilter(gray, -1, k, normalize=True, borderType=cv2.BORDER_REFLECT)
    m2 = cv2.boxFilter(gray * gray, -1, k, normalize=True, borderType=cv2.BORDER_REFLECT)
    return np.sqrt(np.maximum(m2 - m * m, 0.0))

def bg_color(rgb):
    """Modal quantized colour of the slide = the background."""
    q = np.clip((rgb * 255).astype(np.int32) // 16, 0, 15)
    key = (q[..., 0] * 256 + q[..., 1] * 16 + q[..., 2]).ravel()
    counts = np.bincount(key, minlength=4096)
    k = int(counts.argmax())
    sel = key == k
    flat = rgb.reshape(-1, 3)
    return flat[sel].mean(axis=0), float(counts[k]) / key.size

SIGMA_TAU = 0.020     # local-std threshold -> "structured ink" (text/edges/texture)
DEV_TAU   = 0.12      # colour distance from background -> "solid fill ink"

def masks(rgb):
    h, w = rgb.shape[:2]
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    win = max(3, int(round(min(h, w) / 64.0)) | 1)
    sig = local_std(gray, win)
    D = sig >= SIGMA_TAU                      # detail / structured ink
    bg, bgfrac = bg_color(rgb)
    dev = np.linalg.norm(rgb - bg[None, None, :], axis=2)
    V = dev >= DEV_TAU                        # deviation from background
    INK = D | V
    return {"gray": gray, "sigma": sig, "D": D, "V": V, "INK": INK,
            "bg": bg, "bgfrac": bgfrac, "win": win}

def cc(mask, close=0):
    m = mask.astype(np.uint8)
    if close:
        m = cv2.morphologyEx(m, cv2.MORPH_CLOSE,
                             cv2.getStructuringElement(cv2.MORPH_RECT, (close, close)))
    n, lab, stats, cent = cv2.connectedComponentsWithStats(m, connectivity=8)
    return n, lab, stats, cent

# --------------------------------------------------------------- candidates
AREA_EXEMPT = 0.20    # component covering >=20% of canvas = background/photo, may bleed
EDGE_RUN_EXEMPT = 0.55  # component spanning >=55% of an edge = designed full-width band
FILL_EXEMPT = 0.70    # dense bbox fill = solid block / photo, not a cut text line
MIN_CLIP_PX = 12      # ignore specks
# A clipped TEXT LINE has a minimum plausible size. At the fixed 1280px render width an
# 18pt glyph on a 13.33in canvas is ~24px tall, so anything much smaller cannot be cut-off
# text -- it is decoration. Without this, the small dotted squares in a geometric design
# motif that touch the top edge registered as "clipped text" on human decks
# (HighlightImportantWords_GroundTruth slide 1: two 14x8px specks, render-confirmed as
# decoration, drove clip_frac to 0.014 on a slide with nothing clipped).
MIN_TEXT_PX_H = 10    # component bbox height below this = decoration, not a text line
MIN_TEXT_AREA = 0.00035  # ...and it must occupy at least this fraction of the canvas
SPAN_EXEMPT = 0.98    # bbox spanning a whole canvas dimension = designed band/rule
GRAPHIC_AREA = 0.030  # INK component >= this frac of canvas ...
GRAPHIC_FILL = 0.45   # ... AND this dense in its bbox = a picture / solid graphic
GRAPHIC_OVERLAP = 0.50  # text candidate this covered by a graphic = part of the graphic
TEXT_MAX_H = 0.20     # a text LINE is short relative to the canvas
TEXT_MAX_W = 0.80

def graphic_regions(m, shape):
    """Mask of picture / solid-graphic regions, from the UNION ink mask.

    A photo, a full-bleed background, or a decorative blob is a LARGE and DENSE
    ink component. Those are allowed to run off the canvas -- that is design,
    not damage. Text ink is sparse inside its bbox and never qualifies.
    """
    h, w = shape
    n, lab, stats, _ = cc(m["INK"], close=5)
    g = np.zeros((h, w), dtype=bool)
    canvas = float(h * w)
    for i in range(1, n):
        x, y, bw, bh, area = stats[i]
        if area / canvas >= GRAPHIC_AREA and area / float(max(bw * bh, 1)) >= GRAPHIC_FILL:
            g |= (lab == i)
    return g


def clipping(rgb, m, band=2):
    """Detect TEXT ink truncated by the canvas boundary.

    Pipeline:
      1. graphic_regions() marks pictures / solid decorative shapes (union mask,
         large AND dense). These legitimately bleed off the edge.
      2. Candidates are DETAIL-mask components (text strokes) that are NOT inside
         a graphic region.
      3. A candidate that reaches the outer `band` pixels is CLIPPED unless it is
         exempt: covers a lot of canvas (area), spans a whole canvas dimension
         (span, e.g. a rule or shadow under a full-width band), contacts most of
         one edge (edgerun), is dense in its bbox (solid), or is simply too large
         to be a text line (size).

    Off-canvas PICTURES are deliberately NOT this metric's job: geometric
    `overflow` already reads the shape box and catches them (measured:
    ctrlF_pic_offcanvas overflow=0.744). This detector covers only what geometry
    provably cannot see -- ink outside a box that is legally inside the canvas.
    """
    h, w = rgb.shape[:2]
    G = graphic_regions(m, (h, w))
    D = m["D"] & ~G
    Dc = cv2.morphologyEx(D.astype(np.uint8), cv2.MORPH_CLOSE,
                          cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5)))
    n, lab, stats, _ = cv2.connectedComponentsWithStats(Dc, connectivity=8)
    canvas = float(h * w)
    edge = np.zeros((h, w), dtype=bool)
    edge[:band, :] = True; edge[-band:, :] = True
    edge[:, :band] = True; edge[:, -band:] = True
    hits, clipped_px, detail = [], 0, []
    for i in range(1, n):
        x, y, bw, bh, area = stats[i]
        comp = lab == i
        tp = int((comp & edge).sum())
        if tp < MIN_CLIP_PX:
            continue
        area_frac = area / canvas
        bbox_fill = area / float(max(bw * bh, 1))
        span = max(bw / float(w), bh / float(h))
        run = 0.0
        if comp[:band, :].any(): run = max(run, comp[:band, :].any(axis=0).sum() / float(w))
        if comp[-band:, :].any(): run = max(run, comp[-band:, :].any(axis=0).sum() / float(w))
        if comp[:, :band].any(): run = max(run, comp[:, :band].any(axis=1).sum() / float(h))
        if comp[:, -band:].any(): run = max(run, comp[:, -band:].any(axis=1).sum() / float(h))
        exempt = None
        if bh < MIN_TEXT_PX_H or area_frac < MIN_TEXT_AREA: exempt = "speck"
        elif float((comp & G).sum()) / area >= GRAPHIC_OVERLAP: exempt = "graphic"
        elif area_frac >= AREA_EXEMPT: exempt = "area"
        elif span >= SPAN_EXEMPT: exempt = "span"
        elif run >= EDGE_RUN_EXEMPT: exempt = "edgerun"
        elif bh / float(h) > TEXT_MAX_H or bw / float(w) > TEXT_MAX_W: exempt = "size"
        elif bbox_fill >= FILL_EXEMPT and (bw * bh) / canvas >= 0.05: exempt = "solid"
        detail.append(dict(i=int(i), area_frac=round(area_frac, 4), touch=tp,
                           bbox_fill=round(bbox_fill, 3), run=round(run, 3),
                           span=round(span, 3), exempt=exempt,
                           bbox=[int(x), int(y), int(bw), int(bh)]))
        if exempt is None:
            hits.append(i); clipped_px += tp
    return {"n_clipped": len(hits), "clipped_px": clipped_px,
            "clip_frac": clipped_px / float(2 * (h + w)), "detail": detail}


def fg_mask(m, rgb):
    """INK minus huge background components (full-bleed photo / bg fill)."""
    h, w = rgb.shape[:2]
    n, lab, stats, _ = cc(m["INK"], close=3)
    keep = np.zeros((h, w), dtype=bool)
    canvas = float(h * w)
    for i in range(1, n):
        if stats[i][4] / canvas < AREA_EXEMPT:
            keep |= (lab == i)
    return keep

def largest_empty_rect(mask, grid=120):
    """Max-area all-background axis-aligned rectangle, as a fraction of canvas."""
    h, w = mask.shape
    s = grid / float(min(h, w))
    gw, gh = max(8, int(w * s)), max(8, int(h * s))
    small = cv2.resize(mask.astype(np.uint8), (gw, gh), interpolation=cv2.INTER_AREA)
    free = (small == 0).astype(np.int32)
    best = 0
    heights = np.zeros(gw, dtype=np.int32)
    for r in range(gh):
        heights = np.where(free[r] > 0, heights + 1, 0)
        stack = []
        for c in range(gw + 1):
            cur = heights[c] if c < gw else 0
            start = c
            while stack and stack[-1][1] >= cur:
                si, sh = stack.pop()
                best = max(best, sh * (c - si))
                start = si
            stack.append((start, cur))
    return best / float(gw * gh)

def text_contrast(rgb, m):
    """Legibility: ink luminance vs the luminance of the background immediately
    around it, per text-like block. The old erode/dilate range saturated at 1.0
    for any dark-on-light text and could not discriminate."""
    gray = m["gray"]; h, w = gray.shape
    Dc = cv2.morphologyEx(m["D"].astype(np.uint8), cv2.MORPH_CLOSE,
                          cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5)))
    n, lab, stats, _ = cv2.connectedComponentsWithStats(Dc, connectivity=8)
    canvas = float(h * w)
    vals, wts = [], []
    for i in range(1, n):
        x, y, bw, bh, area = stats[i]
        if area < 0.00005 * canvas or area / canvas > AREA_EXEMPT:
            continue
        comp = (lab == i)
        pad = max(3, int(0.01 * min(h, w)))
        y0, y1 = max(0, y - pad), min(h, y + bh + pad)
        x0, x1 = max(0, x - pad), min(w, x + bw + pad)
        sub = comp[y0:y1, x0:x1]; g = gray[y0:y1, x0:x1]
        ring = (~sub) & (cv2.dilate(sub.astype(np.uint8),
                np.ones((pad * 2 + 1, pad * 2 + 1), np.uint8)).astype(bool))
        if sub.sum() < 20 or ring.sum() < 20:
            continue
        vals.append(abs(float(g[sub].mean()) - float(g[ring].mean())))
        wts.append(float(area))
    if not vals:
        return 0.0
    vals = np.asarray(vals); wts = np.asarray(wts)
    return float((vals * wts).sum() / wts.sum())


def slide_measures(png):
    rgb = load_rgb(png)
    h, w = rgb.shape[:2]
    m = masks(rgb)
    fg = fg_mask(m, rgb)
    out = {}
    out["ink_cov"] = float(m["INK"].mean())
    out["detail_cov"] = float(m["D"].mean())
    out["fg_cov"] = float(fg.mean())
    out["glyph_cov"] = float((m["V"] & fg).mean())
    # bbox / margins on foreground ink
    ys, xs = np.nonzero(fg)
    if len(ys):
        top = ys.min() / h; bot = 1 - (ys.max() + 1) / h
        left = xs.min() / w; right = 1 - (xs.max() + 1) / w
        out["min_margin"] = float(min(top, bot, left, right))
        out["bbox_frac"] = float(((ys.max()-ys.min()+1)*(xs.max()-xs.min()+1))/(h*w))
        cy = ys.mean() / h; cx = xs.mean() / w
        out["centroid_off"] = float(math.hypot(cx - 0.5, cy - 0.5) / math.hypot(0.5, 0.5))
    else:
        out["min_margin"] = 0.5; out["bbox_frac"] = 0.0; out["centroid_off"] = 0.0
    c = clipping(rgb, m)
    out["n_clipped"] = c["n_clipped"]; out["clip_frac"] = c["clip_frac"]
    out["_clipdetail"] = c["detail"]
    out["dead_rect"] = largest_empty_rect(m["INK"] | fg)
    # component census on foreground, closed into blocks
    n, lab, stats, _ = cc(fg, close=max(3, int(min(h, w) * 0.012) | 1))
    areas = sorted((int(stats[i][4]) for i in range(1, n)), reverse=True)
    areas = [a for a in areas if a >= 0.0002 * h * w]
    out["n_cc"] = len(areas)
    out["cc_top_frac"] = float(areas[0] / (h * w)) if areas else 0.0
    out["cc_gini"] = float(_gini(areas)) if len(areas) > 1 else 0.0
    out["contrast_p50"] = text_contrast(rgb, m)
    out["bgfrac"] = m["bgfrac"]
    return out

def _gini(x):
    x = np.sort(np.asarray(x, dtype=np.float64)); n = len(x)
    if n == 0 or x.sum() == 0: return 0.0
    idx = np.arange(1, n + 1)
    return float((2 * (idx * x).sum()) / (n * x.sum()) - (n + 1) / n)

def deck_measures(pptx, workdir):
    pngs = render_deck(pptx, workdir)
    if not pngs:
        return None
    per = [slide_measures(p) for p in pngs]
    agg = {}
    keys = [k for k in per[0] if not k.startswith("_")]
    for k in keys:
        v = [p[k] for p in per]
        agg[k] = float(np.mean(v))
    agg["n_slides"] = len(per)
    agg["max_clip_frac"] = float(max(p["clip_frac"] for p in per))
    agg["any_clip"] = float(max(p["n_clipped"] for p in per) > 0)
    agg["frac_slides_clipped"] = float(np.mean([p["n_clipped"] > 0 for p in per]))
    agg["_per"] = per
    agg["_pngs"] = pngs
    return agg

# =========================================================================== #
# PRODUCTION API  (appended to the validated measurement lab above)
# =========================================================================== #
# Everything above is the measurement code validated in the v2 design pass. This
# section turns it into a reward-safe API:
#   * fixed render width, so the score cannot drift with resolution
#   * cache by (path, mtime, size), so score_deck can be called repeatedly
#   * NEVER raises: a reward exception kills a training run, so every failure
#     path returns None and the caller falls back to geometry
#
# Which measures became reward terms, and why (from the design sweep over the 45
# blind-graded audit decks; rho = Spearman vs the designer's grade):
#
#   content   <- detail_cov   rho +0.544, AUC 0.826, Cohen d +1.22   KEEP
#               The single strongest signal found. The old grader scored
#               rho -0.139, i.e. worse than chance, so this one term is what
#               flips the sign.
#   clipping  <- max_clip_frac                                        KEEP
#               Catches ink outside a box that is legally inside the canvas --
#               precisely what render-free geometry provably cannot see.
#
# Deliberately NOT reward terms (measured, rejected):
#   dead_rect     rho -0.553 (highest!) but MONOTONE USE REWARDS PADDING: the
#                 padded fixture scores 0.052 (best possible) because a wall of
#                 text leaves no empty rectangle. A 64-setting sweep found every
#                 band with rho > 0.46 fails the adversarial gate. Diagnostic only.
#   centroid_off  pool-level and within-pool signals point OPPOSITE ways
#                 (AUC 0.726 but rho -0.264). Real design uses deliberate
#                 asymmetry; the best-rated deck in the pool is the most off-centre.
#   contrast_p50  wrong direction (ours scores HIGHER than human decks).
#   ink_cov       separates the pools but does not track the grade (rho +0.226).
#   n_cc          Cohen d +0.03 -- no separation at all.
# They are still returned under "diag" so they can be tracked without being paid for.

import os as _os
import tempfile as _tempfile
import shutil as _shutil

RENDER_WIDTH = 1280          # FIXED. geom_pixels' resolution-invariance claim was
                             # false (11% drift 1x->2x); a fixed width makes it true.

# content band: rises out of "empty", plateaus over the range real decks occupy,
# then falls so a wall of text cannot farm it. Zero-point 0.80 chosen because the
# padded fixture sits at detail_cov 0.746 and must score near zero.
CONTENT_LO0, CONTENT_LO1 = 0.030, 0.220
CONTENT_HI0, CONTENT_HI1 = 0.450, 0.800

CLIP_TAU = 0.020             # clip_frac at which clipping scores 0. Calibrated below.

_CACHE = {}
_CACHE_MAX = 4096


def _sstep(e0, e1, x):
    if e1 <= e0:
        return 0.0
    t = max(0.0, min(1.0, (x - e0) / (e1 - e0)))
    return t * t * (3.0 - 2.0 * t)


def content_score(detail_cov):
    """Real content coverage. Band-limited on BOTH sides: an empty slide and a
    wall of padding both score near zero."""
    return float(max(0.0, min(1.0,
        _sstep(CONTENT_LO0, CONTENT_LO1, detail_cov)
        * (1.0 - _sstep(CONTENT_HI0, CONTENT_HI1, detail_cov)))))


def clipping_score(max_clip_frac):
    """1.0 = no rendered ink cut off at the canvas edge."""
    return float(max(0.0, min(1.0, 1.0 - (max_clip_frac / CLIP_TAU))))


def _key(pptx_path):
    try:
        st = _os.stat(pptx_path)
        return (_os.path.abspath(pptx_path), int(st.st_mtime), int(st.st_size))
    except OSError:
        return None


def score_render(pptx_path, use_cache=True):
    """Render-based metrics for one deck.

    Returns {"content": float, "clipping": float, "diag": {...}} or None if the
    deck could not be rendered. NEVER raises -- callers fall back to geometry.
    """
    k = _key(pptx_path)
    if use_cache and k is not None and k in _CACHE:
        return _CACHE[k]
    wd = None
    try:
        wd = _tempfile.mkdtemp(prefix="rmx_")
        agg = deck_measures(pptx_path, wd)
        if not agg:
            return None
        out = {
            "content": content_score(agg.get("detail_cov", 0.0)),
            # MEAN across slides, not max. Our task always emits ONE slide, so mean==max
            # in training; but a 16-slide human deck otherwise gets 16 chances to trip the
            # detector and is unfairly punished in the calibration corpus.
            "clipping": clipping_score(agg.get("clip_frac", agg.get("max_clip_frac", 0.0))),
            "diag": {kk: agg[kk] for kk in
                     ("detail_cov", "max_clip_frac", "n_clipped", "dead_rect",
                      "centroid_off", "ink_cov", "min_margin", "n_cc", "n_slides")
                     if kk in agg},
        }
    except Exception:
        return None
    finally:
        if wd:
            _shutil.rmtree(wd, ignore_errors=True)
    if use_cache and k is not None:
        if len(_CACHE) >= _CACHE_MAX:
            _CACHE.clear()
        _CACHE[k] = out
    return out


def score_render_batch(pptx_paths, use_cache=True):
    """Batch entry point. LibreOffice amortises heavily when handed many files at
    once (0.34 s/deck batched vs ~2 s cold), which is what keeps a 64-rollout
    training step under 1% overhead."""
    todo, out = [], {}
    for p in pptx_paths:
        k = _key(p)
        if use_cache and k is not None and k in _CACHE:
            out[p] = _CACHE[k]
        else:
            todo.append(p)
    for p in todo:
        out[p] = score_render(p, use_cache=use_cache)
    return out
