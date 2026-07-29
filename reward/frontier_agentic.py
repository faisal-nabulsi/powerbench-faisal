#!/usr/bin/env python3
"""Frontier model on our held-out set THROUGH THE AGENTIC HARNESS.

The single-turn baseline (frontier_baseline.py) is the fair comparison to our RL'd 27B,
because our model also gets exactly one shot with no feedback. But it understates a frontier
model, which in the real powerbench harness gets to RENDER its deck, LOOK at it, and fix
what it sees. This script gives it that.

Mirrors pptx_tools.py (the verl tools our agentic training uses) exactly:
    run_python(code)   -> executes in a sandbox, reports stdout/stderr + whether output.pptx exists
    render_slides()    -> LibreOffice -> pdftoppm -> PNG, returned to the model AS IMAGES
    submit()           -> ends the episode

Scoring is the geometric grader (score_deck), NOT pptx_tools.grade_deck -- the latter is the
legacy text-coverage reward and is not comparable to any other number in this project.

Usage:
    export ANTHROPIC_API_KEY=...
    python frontier_agentic.py --model claude-fable-5 --n 45 --max-turns 10

Cost: multi-turn with images is much pricier than the single-turn run. ~45 tasks x up to 10
turns, with a full-page PNG per render. Start with --n 8 to sanity-check spend.
"""
from __future__ import annotations

import argparse
import base64
import concurrent.futures as cf
import io
import json
import os
import re
import shutil
import statistics as st
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

HELD_OUT = "/home/ubuntu/powerbench/data/slidesbench_highlevel/test.parquet"
PYTHON = "/home/ubuntu/powerbench/.venv/bin/python"
DECK_NAME = "output.pptx"
CODE_TIMEOUT = 25
RENDER_TIMEOUT = 180
RENDER_DPI = 80
RENDER_MAX_PX = 1400
RENDER_MAX_SLIDES = 4
MAX_OUT_CHARS = 4000

AGENTIC_SYSTEM = """You are an expert presentation designer who builds PowerPoint slides with python-pptx.

You have three tools:
  run_python(code)  - run a Python script in your working directory. Your script MUST save the
                      presentation to 'output.pptx' in the current directory.
  render_slides()   - render the current output.pptx and SHOW you the resulting image(s).
                      Use this to check for text overflow, overlapping shapes, content running
                      off the slide, unreadable color contrast, and unbalanced layout.
  submit()          - submit the finished deck and end the task.

Work iteratively: build the deck with run_python, then call render_slides to LOOK at it, then
fix any defects you can see and re-render. Only call submit when the slide actually looks good.

The slide canvas is 16:9 widescreen: set prs.slide_width = Inches(13.333) and
prs.slide_height = Inches(7.5) before adding slides, and keep all content inside it.
If the instruction asks for an image, photo, logo, or picture, a placeholder image file named
'image.png' is available in your working directory -- use it via
slide.shapes.add_picture('image.png', left, top, width, height). Do not reference any other
image filename."""

TOOLS = [
    {
        "name": "run_python",
        "description": "Run a Python script in the working directory. The script must save the deck to 'output.pptx'. Returns exit code, stdout, stderr, and whether output.pptx now exists.",
        "input_schema": {
            "type": "object",
            "properties": {"code": {"type": "string", "description": "The complete Python script to run."}},
            "required": ["code"],
        },
    },
    {
        "name": "render_slides",
        "description": "Render the current output.pptx to one image per slide and return them so you can SEE your deck and check for overflow, overlap, off-colors, or bad layout. Call this after building/editing, then fix issues.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "submit",
        "description": "Submit the finished deck for grading and end the task. Call this once output.pptx is correct and you are done. Only call when finished.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
]


def _truncate(s, n=MAX_OUT_CHARS):
    s = s or ""
    return s if len(s) <= n else s[:n] + "\n...[truncated]"


def _provision_assets(workdir):
    """Same placeholder image the training sandbox provides, under every name the prompt
    might reference."""
    src = None
    for cand in ("/home/ubuntu/powerbench/agentic/assets/image.png",
                 "/home/ubuntu/powerbench/agentic/image.png"):
        if os.path.isfile(cand):
            src = cand
            break
    if src is None:
        try:
            from PIL import Image, ImageDraw
            img = Image.new("RGB", (1024, 768), (222, 226, 232))
            d = ImageDraw.Draw(img)
            d.line((0, 0, 1023, 767), fill=(150, 155, 165), width=3)
            d.line((0, 767, 1023, 0), fill=(150, 155, 165), width=3)
            src = os.path.join(workdir, "image.png")
            img.save(src)
        except Exception:
            return
    for name in ("image.png", "photo.png", "logo.png", "picture.png", "trophy.png",
                 "target.png", "background.png", "icon.png", "chart.png"):
        dst = os.path.join(workdir, name)
        if not os.path.isfile(dst):
            try:
                shutil.copy(src, dst)
            except Exception:
                pass


def tool_run_python(workdir, code):
    script = os.path.join(workdir, "_run.py")
    with open(script, "w") as f:
        f.write(code or "")
    env = dict(os.environ)
    env.pop("ANTHROPIC_API_KEY", None)  # never expose the key to model-authored code
    try:
        p = subprocess.run([PYTHON, "_run.py"], cwd=workdir, capture_output=True,
                           text=True, timeout=CODE_TIMEOUT, env=env)
        body = "[exit %d]\n%s" % (p.returncode, _truncate(p.stdout))
        if (p.stderr or "").strip():
            body += "\n[stderr]\n" + _truncate(p.stderr, 2000)
    except subprocess.TimeoutExpired:
        body = "[timeout after %ds -- your script ran too long]" % CODE_TIMEOUT
    deck = os.path.join(workdir, DECK_NAME)
    body += "\n[%s: %s]" % (DECK_NAME,
                            "present" if os.path.isfile(deck) else "NOT FOUND -- remember to save to output.pptx")
    return [{"type": "text", "text": body}]


def tool_render_slides(workdir):
    deck = os.path.join(workdir, DECK_NAME)
    if not os.path.isfile(deck):
        return [{"type": "text", "text": "No %s to render yet -- build it with run_python first." % DECK_NAME}]
    outdir = os.path.join(workdir, "_render")
    shutil.rmtree(outdir, ignore_errors=True)
    os.makedirs(outdir, exist_ok=True)
    try:
        subprocess.run(["soffice", "--headless", "--convert-to", "pdf", "--outdir", outdir, deck],
                       cwd=workdir, capture_output=True, text=True, timeout=RENDER_TIMEOUT, check=True)
        pdf = os.path.join(outdir, os.path.splitext(DECK_NAME)[0] + ".pdf")
        if not os.path.isfile(pdf):
            return [{"type": "text", "text": "Render failed: LibreOffice produced no PDF."}]
        subprocess.run(["pdftoppm", "-png", "-r", str(RENDER_DPI), pdf, os.path.join(outdir, "slide")],
                       cwd=workdir, capture_output=True, text=True, timeout=RENDER_TIMEOUT, check=True)
    except subprocess.TimeoutExpired:
        return [{"type": "text", "text": "Render timed out."}]
    except subprocess.CalledProcessError as e:
        return [{"type": "text", "text": "Render error: " + _truncate(e.stderr or str(e), 1000)}]

    from PIL import Image
    pngs = sorted(p for p in os.listdir(outdir) if p.startswith("slide") and p.endswith(".png"))
    blocks = []
    for name in pngs[:RENDER_MAX_SLIDES]:
        img = Image.open(os.path.join(outdir, name)).convert("RGB")
        if max(img.size) > RENDER_MAX_PX:
            scale = RENDER_MAX_PX / max(img.size)
            img = img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        blocks.append({"type": "image", "source": {
            "type": "base64", "media_type": "image/png",
            "data": base64.b64encode(buf.getvalue()).decode("ascii")}})
    if not blocks:
        return [{"type": "text", "text": "Render produced no images."}]
    return [{"type": "text",
             "text": "Rendered %d slide(s). Inspect each and fix any defects." % len(blocks)}] + blocks


def run_episode(client, model, task_name, user_msg, max_turns, max_tokens, keep_dir=None):
    workdir = tempfile.mkdtemp(prefix="fabagent_")
    _provision_assets(workdir)
    messages = [{"role": "user", "content": user_msg}]
    turns = 0
    submitted = False
    usage_in = usage_out = 0
    err = None
    try:
        while turns < max_turns:
            turns += 1
            try:
                resp = client.messages.create(model=model, max_tokens=max_tokens,
                                              system=AGENTIC_SYSTEM, tools=TOOLS, messages=messages)
            except Exception as e:
                code = getattr(e, "status_code", None)
                if code in (400, 401, 403, 404):
                    raise
                err = str(e)[:160]
                break
            usage_in += getattr(resp.usage, "input_tokens", 0) or 0
            usage_out += getattr(resp.usage, "output_tokens", 0) or 0
            messages.append({"role": "assistant", "content": resp.content})

            calls = [b for b in resp.content if getattr(b, "type", "") == "tool_use"]
            if not calls:
                break  # model stopped without calling a tool

            results = []
            for b in calls:
                if b.name == "run_python":
                    content = tool_run_python(workdir, (b.input or {}).get("code", ""))
                elif b.name == "render_slides":
                    content = tool_render_slides(workdir)
                elif b.name == "submit":
                    content = [{"type": "text", "text": "Submitted."}]
                    submitted = True
                else:
                    content = [{"type": "text", "text": "Unknown tool."}]
                results.append({"type": "tool_result", "tool_use_id": b.id, "content": content})
            messages.append({"role": "user", "content": results})
            if submitted:
                break

        deck = os.path.join(workdir, DECK_NAME)
        from geometric_reward import score_deck
        if os.path.isfile(deck):
            try:
                r = score_deck(deck)
                out = {"task": task_name, "score": r["score"], "valid": bool(r.get("valid", True)),
                       "metrics": r.get("metrics", {})}
            except Exception as e:
                out = {"task": task_name, "score": 0.0, "valid": False, "err": "score: " + str(e)[:80]}
        else:
            # no deck at all is a genuine task failure, not an API error -> 0.0 is correct
            out = {"task": task_name, "score": 0.0, "valid": False, "err": "no output.pptx"}
        out.update(turns=turns, submitted=submitted, in_tok=usage_in, out_tok=usage_out)
        if err:
            out["api_error"] = err
            if not os.path.isfile(deck):
                out["score"] = None  # API died before producing anything -> exclude, don't score 0
        if keep_dir and os.path.isfile(deck):
            # save deck + the transcript + metrics, so an agentic deck can be audited and
            # rendered exactly like one of ours (and so we can see how many render/fix
            # iterations it actually took).
            os.makedirs(keep_dir, exist_ok=True)
            base = re.sub(r"\W+", "_", task_name)
            shutil.copy(deck, os.path.join(keep_dir, "deck_%s.pptx" % base))
            with open(os.path.join(keep_dir, "meta_%s.json" % base), "w") as f:
                json.dump({k: v for k, v in out.items() if k != "metrics"} |
                          {"metrics": out.get("metrics", {}), "task": task_name,
                           "harness": "agentic"}, f)
            try:
                code_blocks = []
                for m in messages:
                    if m.get("role") != "assistant":
                        continue
                    for b in m.get("content", []):
                        if getattr(b, "type", "") == "tool_use" and b.name == "run_python":
                            code_blocks.append((b.input or {}).get("code", ""))
                with open(os.path.join(keep_dir, "code_%s.py" % base), "w") as f:
                    f.write(("\n\n# ---- next run_python call ----\n\n").join(code_blocks))
            except Exception:
                pass
        return out
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--n", type=int, default=45)
    ap.add_argument("--max-turns", type=int, default=10)
    ap.add_argument("--max-tokens", type=int, default=8192)
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--out", default="/tmp/frontier_agentic.json")
    ap.add_argument("--keep-decks", default="/tmp/fable_agentic_decks")
    a = ap.parse_args()

    import anthropic
    import pandas as pd

    client = anthropic.Anthropic()
    df = pd.read_parquet(HELD_OUT).head(a.n)
    tasks = []
    for _, row in df.iterrows():
        pr = row["prompt"]
        tasks.append((dict(row["extra_info"]).get("task", "?"), pr[1]["content"]))

    print("model=%s  AGENTIC harness (run_python / render_slides / submit)" % a.model)
    print("tasks=%d  max_turns=%d  concurrency=%d" % (len(tasks), a.max_turns, a.concurrency))
    print("scored with the SAME geometric grader as the RL run\n")

    def one(t):
        name, user = t
        try:
            return run_episode(client, a.model, name, user, a.max_turns, a.max_tokens, a.keep_decks)
        except Exception as e:
            return {"task": name, "score": None, "valid": False, "api_error": str(e)[:160]}

    results = []
    with cf.ThreadPoolExecutor(max_workers=a.concurrency) as ex:
        for i, res in enumerate(ex.map(one, tasks), 1):
            results.append(res)
            sc = "APIERR" if res.get("score") is None else "%.3f" % res["score"]
            print("  [%2d/%d] %-24s %s  turns=%s sub=%s%s" % (
                i, len(tasks), res["task"][:24], sc, res.get("turns", "?"),
                res.get("submitted"), "  " + res["api_error"] if res.get("api_error") else ""))

    apierr = [r for r in results if r.get("score") is None]
    ok = [r for r in results if r.get("score") is not None]
    if not ok:
        print("\nALL %d EPISODES FAILED. first error: %s" % (
            len(results), apierr[0].get("api_error") if apierr else "?"))
        sys.exit(2)
    if apierr:
        print("\n%d/%d episodes died at the API and are EXCLUDED from the mean." % (len(apierr), len(results)))

    sc = [r["score"] for r in ok]
    summary = {
        "model": a.model, "harness": "agentic", "n": len(sc),
        "mean_score": round(st.mean(sc), 4),
        "median": round(st.median(sc), 4),
        "sd": round(st.pstdev(sc), 4),
        "valid_rate": round(st.mean([1.0 if r["valid"] else 0.0 for r in ok]), 4),
        "mean_turns": round(st.mean([r.get("turns", 0) for r in ok]), 2),
        "submitted_rate": round(st.mean([1.0 if r.get("submitted") else 0.0 for r in ok]), 3),
        "in_tokens": sum(r.get("in_tok", 0) for r in ok),
        "out_tokens": sum(r.get("out_tok", 0) for r in ok),
        "api_errors": len(apierr),
    }
    per = {}
    for k in ("collision", "overflow", "textfit", "density", "contrast", "imbalance",
              "picfit", "alignment", "aspect"):
        v = [r["metrics"][k] for r in ok if r.get("metrics") and k in r["metrics"]]
        if v:
            per[k] = round(st.mean(v), 3)
    summary["per_metric"] = per
    json.dump({"summary": summary, "results": results}, open(a.out, "w"), indent=1)

    print("\n" + "=" * 66)
    print("%s on our held-out set -- AGENTIC (can render and self-correct)" % a.model)
    print("=" * 66)
    print("  mean score   : %.4f   (sd %.3f, median %.3f)" % (
        summary["mean_score"], summary["sd"], summary["median"]))
    print("  valid decks  : %.0f%%" % (100 * summary["valid_rate"]))
    print("  mean turns   : %.2f   submitted: %.0f%%" % (
        summary["mean_turns"], 100 * summary["submitted_rate"]))
    print("  tokens       : %s in / %s out" % (f"{summary['in_tokens']:,}", f"{summary['out_tokens']:,}"))
    print("  per-metric   : %s" % per)
    print("\n  for comparison (same held-out set, same geometric grader):")
    print("    our 27B baseline, single-turn   0.5591   valid 73%")
    print("    our 27B after RL, single-turn   0.8381   valid 93%   (step 24)")
    print("    frontier single-turn            see /tmp/frontier_baseline.json")
    print("  NOTE: our 27B was RL-trained against this grader; the frontier model was not.")
    print("  decks kept in %s" % a.keep_decks)
    print("=" * 66)


if __name__ == "__main__":
    main()
