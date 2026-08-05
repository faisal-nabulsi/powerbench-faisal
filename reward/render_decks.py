#!/usr/bin/env python3
"""Render every deck_*.pptx in a directory to a slide-1 PNG next to it (for the judge panel).
    python render_decks.py <dir>
"""
import sys, os, glob, shutil, tempfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import render_metrics as rm

d = sys.argv[1]
decks = sorted(glob.glob(os.path.join(d, "deck_*.pptx")))
print("rendering %d decks in %s" % (len(decks), d))
ok = 0
for p in decks:
    wd = tempfile.mkdtemp(prefix="rd_")
    try:
        pngs = rm.render_deck(p, wd)
        if pngs:
            out = p[:-5] + ".png"          # deck_<task>.pptx -> deck_<task>.png
            shutil.copy(pngs[0], out)
            ok += 1
    except Exception as e:
        print("  render failed:", os.path.basename(p), str(e)[:60])
    finally:
        shutil.rmtree(wd, ignore_errors=True)
print("rendered %d/%d -> PNG" % (ok, len(decks)))
