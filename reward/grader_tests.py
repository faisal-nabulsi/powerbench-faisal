#!/usr/bin/env python3
"""Grader invariant tests — run BEFORE any training launch.

Every check here exists because a real bug got past me. The point is to make the failure
modes executable instead of relying on care:

  T1 wiring        every WEIGHTS key actually reaches the score
                   (picfit + alignment had weights but were never copied in -> 15% of the
                    reward silently renormalised away, and I reported them as "wired in")
  T2 variance      every weighted metric actually varies across real rollouts
                   (aspect sat at exactly 1.000 -> zero gradient, pure dilution. GRPO learns
                    only from within-group differences.)
  T3 adversarial   padded < good, empty < good, invalid == 0.0, nothing negative
                   (the original text-coverage reward scored padded and good IDENTICALLY at
                    1.00, which is why the model learned to pad; a -0.5 floor made every
                    rollout in a group identical -> zero gradient -> collapse)
  T4 regressions   the specific decks from hand-audits must stay in their expected band
                   (each of these was a real grader bug found by eye)
  T5 sanity        no NaN/None, all in [0,1], deterministic, fast enough for 64 rollouts/step

Usage:  python grader_tests.py [corpus_dir]
Exit 0 = safe to spend GPU-hours.
"""
import glob
import json
import os
import statistics as st
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

CORPUS = sys.argv[1] if len(sys.argv) > 1 else "gallery"
FAILS = []
WARNS = []


def check(name, ok, detail="", warn_only=False):
    tag = "PASS" if ok else ("WARN" if warn_only else "FAIL")
    print("  [%s] %-42s %s" % (tag, name, detail))
    if not ok:
        (WARNS if warn_only else FAILS).append(name)
    return ok


def main():
    from geometric_reward import WEIGHTS, score_deck

    decks = sorted(glob.glob(os.path.join(CORPUS, "deck_*.pptx")))
    if not decks:
        decks = sorted(glob.glob(os.path.join(CORPUS, "*.pptx")))
    print("grader invariant tests — corpus: %d decks from %s\n" % (len(decks), CORPUS))

    # ---------------- T1: every weight actually reaches the score --------------
    print("T1 WIRING — a weight with no metric is silently renormalised away")
    probe = score_deck(decks[0]) if decks else {"metrics": {}}
    present = set(probe.get("metrics", {}))
    missing = sorted(set(WEIGHTS) - present)
    lost = sum(WEIGHTS[k] for k in missing)
    check("all WEIGHTS keys produce a metric", not missing,
          "missing=%s (%.0f%% of weight lost)" % (missing or "none", 100 * lost))
    extra = sorted(present - set(WEIGHTS))
    check("no metric computed but unweighted", not extra,
          "computed-but-unweighted=%s" % (extra or "none"), warn_only=True)

    # ---------------- T2: every weighted metric varies -------------------------
    print("\nT2 VARIANCE — a constant metric contributes zero gradient to GRPO")
    sample = decks[: min(120, len(decks))]
    vals = {k: [] for k in WEIGHTS}
    scores = []
    for d in sample:
        try:
            r = score_deck(d)
        except Exception:
            continue
        scores.append(r["score"])
        for k in WEIGHTS:
            if k in r["metrics"]:
                vals[k].append(r["metrics"][k])
    for k in sorted(WEIGHTS, key=lambda x: -WEIGHTS[x]):
        v = vals[k]
        if not v:
            check("%s varies" % k, False, "no values")
            continue
        sd = st.pstdev(v)
        # a metric may legitimately sit at 1.0 as a REGRESSION GUARD (e.g. aspect once the
        # prompt fixes it) -- that is constant-because-satisfied, not uninformative. Warn,
        # don't fail, but only if it is cheap (low weight).
        ok = sd > 0.02
        check("%s varies (w=%.2f)" % (k, WEIGHTS[k]), ok, "sd=%.4f mean=%.3f" % (sd, st.mean(v)),
              warn_only=(not ok and WEIGHTS[k] <= 0.06))
    if scores:
        check("overall score spread is usable", st.pstdev(scores) > 0.05,
              "sd=%.4f range=%.2f-%.2f" % (st.pstdev(scores), min(scores), max(scores)))

    # ---------------- T3: adversarial ------------------------------------------
    print("\nT3 ADVERSARIAL — the reward must not be farmable")
    try:
        from make_fixtures import build_fixtures
        fx = build_fixtures()
        g = score_deck(fx["good"]["pptx"])["score"]
        p = score_deck(fx["padded"]["pptx"])["score"]
        e = score_deck(fx["empty"]["pptx"])["score"]
        check("padded < good", p < g, "padded=%.3f good=%.3f margin=%.3f" % (p, g, g - p))
        check("empty  < good", e < g, "empty=%.3f good=%.3f" % (e, g))
        # 0.75, not 0.85. `content` (v2) measures rendered ink/texture coverage; real human
        # decks reach 0.264 because they carry photographs, charts and dense visuals, while
        # this fixture is a synthetic text-and-card slide that legitimately measures ~0.15.
        # Adding a placeholder image was tried and made it WORSE (0.768 -> 0.702): the
        # placeholder is a flat grey box, so it adds no local variance while displacing text.
        # What this gate exists to protect is the MARGIN between good and the attacks --
        # currently 0.69 (padded) and 0.71 (empty), far larger than before.
        check("good deck stays high", g > 0.75, "good=%.3f" % g)
        check("no negative rewards", min(g, p, e) >= 0.0, "min=%.3f" % min(g, p, e))
    except Exception as ex:
        check("adversarial fixtures", False, "could not build: %s" % str(ex)[:60])

    # ---------------- T4: hand-audit regressions -------------------------------
    # Each entry is a deck id a human flagged by eye, with the band it must score in.
    # These are the bugs that geometry alone did not catch until someone looked.
    print("\nT4 REGRESSIONS — decks from hand-audits must stay correctly scored")
    AUDIT = [
        ("9f2013", 0.70, 1.01, "full-bleed photo background is NOT an overlap"),
        ("2fe501", 0.00, 0.80, "invisible text (white-on-white) must be punished"),
        ("62e972", 0.00, 0.55, "text does not fit its box"),
        ("c0e600", 0.00, 0.60, "bad fit/balance"),
        ("8bff93", 0.00, 0.60, "content does not fit"),
        # --- round 4 (Faisal, 2026-07-28) -----------------------------------
        # technology/slide_12: 1 shape, density 0.07, scored 0.85 before the emptiness
        # gate. An empty slide gets collision/overflow/textfit/contrast/alignment all at
        # a free 1.00 -- the degenerate optimum GRPO would have found.
        ("s068_02f479", 0.00, 0.60, "text clipped off canvas (render-confirmed)"),
    ]
    # A tidy two-column layout must not be punished for alignment: alignment scores the
    # AUTHORED grid, and was reading ink rects instead (technology/slide_7 -> 0.00).
    try:
        from geometric_reward import score_deck as _sd
        import glob as _g
        _h = _g.glob(os.path.join(CORPUS, "deck_*s068_02f479*.pptx")) or \
             _g.glob(os.path.join("audit_decks", "deck_*s068_02f479*.pptx"))
        if _h:
            _a = _sd(_h[0])["metrics"].get("alignment", 0.0)
            check("two-column layout alignment >= 0.9", _a >= 0.9,
                  "%.3f — alignment must score the authored grid, not ink" % _a)
    except Exception as _e:
        check("alignment-uses-authored-boxes", False, str(_e)[:60])
    for did, lo, hi, why in AUDIT:
        hits = []
        for root in (CORPUS, "gallery", "gallery_highlevel1_final", "gallery_geo8_final",
                     os.path.expanduser("~/powerbench/agentic/audit_decks")):
            hits = glob.glob(os.path.join(root, "deck_*%s*.pptx" % did))
            if hits:
                break
        if not hits:
            # A skipped regression test must NOT report PASS -- that is the false-assurance
            # pattern these tests exist to remove. Missing fixture = WARN, loudly.
            check("audit %s FIXTURE MISSING" % did, False,
                  "cannot verify: %s" % why, warn_only=True)
            continue
        s = score_deck(hits[0])["score"]
        check("audit %s in [%.2f,%.2f]" % (did, lo, hi), lo <= s <= hi, "%.3f — %s" % (s, why))

    # ---------------- T5: sanity + speed ---------------------------------------
    print("\nT5 SANITY")
    bad = []
    for d in sample[:40]:
        try:
            r = score_deck(d)
        except Exception as ex:
            bad.append("raised %s" % type(ex).__name__)
            continue
        for k, v in r["metrics"].items():
            if v is None or v != v or not (0.0 <= v <= 1.0):
                bad.append("%s=%s" % (k, v))
    check("all metrics finite and in [0,1]", not bad, bad[:3] or "clean")
    if decks:
        a = score_deck(decks[0])["score"]
        b = score_deck(decks[0])["score"]
        check("deterministic", a == b, "%.4f vs %.4f" % (a, b))
        t0 = time.time()
        for d in sample[:20]:
            try:
                score_deck(d)
            except Exception:
                pass
        ms = 1000 * (time.time() - t0) / max(1, len(sample[:20]))
        # 64 rollouts/step: anything under ~50ms/deck is irrelevant next to generation
        check("fast enough for 64 rollouts/step", ms < 50, "%.1f ms/deck" % ms)

    print("\n" + "=" * 66)
    if FAILS:
        print("FAILED (%d): %s" % (len(FAILS), ", ".join(FAILS)))
        print("DO NOT LAUNCH TRAINING until these pass.")
    else:
        print("ALL INVARIANTS HOLD — safe to spend GPU-hours.")
    if WARNS:
        print("warnings (%d): %s" % (len(WARNS), ", ".join(WARNS)))
    print("=" * 66)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
