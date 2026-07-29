#!/usr/bin/env python3
"""Frontier baseline on OUR held-out set, scored by OUR grader.

Answers the question the run report cannot: is 0.811 good? Without an external reference,
a 45% self-improvement has no meaning to anyone outside the project.

Apples-to-apples by construction: identical held-out prompts, identical code extraction,
identical execution sandbox, identical geometric grader. The ONLY thing that changes is
which model writes the python-pptx code.

Usage:
    export ANTHROPIC_API_KEY=...        # and/or OPENAI_API_KEY
    python frontier_baseline.py --model claude-opus-4-6 --n 45
    python frontier_baseline.py --model gpt-5.2 --provider openai --n 45

Cost: ~45 calls (~$1-3 for a frontier model). Runtime ~10 min with concurrency.
"""
import argparse
import concurrent.futures as cf
import json
import os
import statistics as st
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

HELD_OUT = "/home/ubuntu/powerbench/data/slidesbench_highlevel/test.parquet"


def call_anthropic(model, system, user, max_tokens, temperature=0.0, thinking_budget=0):
    """Retry transient failures. A 404/auth error is fatal and must NOT be retried --
    it means the model id is wrong, and silently retrying it 4 times just wastes a minute
    before reporting the same thing."""
    import time as _t
    import anthropic
    c = anthropic.Anthropic()
    last = None
    for attempt in range(4):
        try:
            kw = dict(model=model, max_tokens=max_tokens, system=system,
                      messages=[{"role": "user", "content": user}])
            if thinking_budget:
                # PARITY: our 27B runs thinking-on, and extract_code parses past </think>.
                # Comparing a reasoning model against a non-reasoning call is not the same
                # setup. The API requires temperature=1 whenever thinking is enabled.
                kw["thinking"] = {"type": "enabled", "budget_tokens": thinking_budget}
                kw["temperature"] = 1.0
            else:
                # PARITY: verl val_kwargs is {do_sample: False, temperature: 0} -- greedy.
                kw["temperature"] = temperature
            r = c.messages.create(**kw)
            return "".join(b.text for b in r.content if getattr(b, "type", "") == "text")
        except Exception as e:
            last = e
            code = getattr(e, "status_code", None)
            if code in (400, 401, 403, 404):
                raise
            _t.sleep(2 ** attempt)
    raise last


def call_openai(model, system, user, max_tokens, temperature=0.0, thinking_budget=0):
    from openai import OpenAI
    c = OpenAI()
    r = c.chat.completions.create(model=model, max_completion_tokens=max_tokens,
                                  messages=[{"role": "system", "content": system},
                                            {"role": "user", "content": user}])
    return r.choices[0].message.content or ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--provider", default="anthropic", choices=["anthropic", "openai"])
    ap.add_argument("--n", type=int, default=45)
    ap.add_argument("--max-tokens", type=int, default=8192)
    ap.add_argument("--concurrency", type=int, default=6)
    ap.add_argument("--temperature", type=float, default=0.0,
                    help="PARITY with verl val_kwargs.temperature=0 (greedy). Default 0.")
    ap.add_argument("--thinking", type=int, default=0, metavar="BUDGET",
                    help="Enable extended thinking with this token budget, to match our "
                         "27B running thinking-on. Forces temperature=1 (API requirement).")
    ap.add_argument("--out", default="/tmp/frontier_baseline.json")
    ap.add_argument("--keep-decks", default="/home/ubuntu/powerbench/agentic/fable_singleturn",
                    help="every deck is saved here; NEVER the training gallery")
    a = ap.parse_args()

    import pandas as pd
    from singleturn_geometric_reward import score_solution

    df = pd.read_parquet(HELD_OUT).head(a.n)
    tasks = []
    for _, row in df.iterrows():
        pr = row["prompt"]
        sysmsg = pr[0]["content"]
        usermsg = pr[1]["content"]
        tasks.append((dict(row["extra_info"]).get("task", "?"), sysmsg, usermsg))

    call = call_anthropic if a.provider == "anthropic" else call_openai
    mode = ("thinking budget=%d, temperature=1 (API-forced)" % a.thinking) if a.thinking \
           else ("no thinking, temperature=%.2f" % a.temperature)
    print("model=%s  provider=%s  tasks=%d" % (a.model, a.provider, len(tasks)))
    print("sampling: %s" % mode)
    print("our 27B is evaluated at: thinking-on, val_kwargs{do_sample:False, temperature:0, n:1}")
    print("scoring with the SAME grader and SAME sandbox as the RL run\n")

    def one(t):
        name, sysmsg, usermsg = t
        try:
            out = call(a.model, sysmsg, usermsg, a.max_tokens, a.temperature, a.thinking)
        except Exception as e:
            # api_error is NOT a score of 0.0 -- a rate limit is not the model failing the
            # task. Scoring it 0.0 would understate the baseline and flatter our own model.
            return {"task": name, "score": None, "valid": False, "api_error": str(e)[:120]}
        try:
            r = score_solution(out, keep_dir=a.keep_decks, keep_name=name)
        except Exception as e:
            return {"task": name, "score": 0.0, "valid": False, "err": "score: %s" % str(e)[:80]}
        return {"task": name, "score": r["score"], "valid": bool(r["valid"]),
                "metrics": r.get("metrics", {}), "chars": len(out)}

    results = []
    with cf.ThreadPoolExecutor(max_workers=a.concurrency) as ex:
        for i, res in enumerate(ex.map(one, tasks), 1):
            results.append(res)
            print("  [%2d/%d] %-24s %s%s" % (i, len(tasks), res["task"][:24],
                                               "APIERR" if res["score"] is None else "%.3f" % res["score"],
                                               "  " + (res.get("api_error") or res.get("err") or "") if (res.get("err") or res.get("api_error")) else ""))

    apierr = [r for r in results if r.get("score") is None]
    ok = [r for r in results if r.get("score") is not None]
    if not ok:
        print("\nALL %d CALLS FAILED -- no baseline produced." % len(results))
        print("first error: %s" % (apierr[0]["api_error"] if apierr else "?"))
        print("\nlist the model ids this key can actually reach:")
        print("  curl -s https://api.anthropic.com/v1/models -H \"x-api-key: $ANTHROPIC_API_KEY\" \\")
        print("       -H 'anthropic-version: 2023-06-01' | python3 -m json.tool | grep '\"id\"'")
        sys.exit(2)
    if apierr:
        print("\n%d/%d calls failed at the API and are EXCLUDED from the mean "
              "(not scored 0.0)." % (len(apierr), len(results)))
    sc = [r["score"] for r in ok]
    valid = [1.0 if r["valid"] else 0.0 for r in ok]
    summary = {
        "model": a.model, "n": len(sc),
        "mean_score": round(st.mean(sc), 4),
        "median": round(st.median(sc), 4),
        "sd": round(st.pstdev(sc), 4),
        "valid_rate": round(st.mean(valid), 4),
        "mean_chars": round(st.mean([r.get("chars", 0) for r in ok])),
        "temperature": (1.0 if a.thinking else a.temperature),
        "thinking_budget": a.thinking,
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

    print("\n" + "=" * 62)
    print("%s on our held-out set" % a.model)
    print("=" * 62)
    print("  mean score   : %.4f   (sd %.3f, median %.3f)" % (
        summary["mean_score"], summary["sd"], summary["median"]))
    print("  valid decks  : %.0f%%" % (100 * summary["valid_rate"]))
    print("  per-metric   : %s" % per)
    print("  decks saved  : %s" % a.keep_decks)
    print("\n  OUR 27B for comparison:")
    print("    baseline (untrained) 0.5591   valid 73%%")
    print("    after RL (step 21)   0.8110   valid 93%%")
    print("=" * 62)


if __name__ == "__main__":
    main()
