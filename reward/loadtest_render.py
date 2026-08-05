"""64-concurrent render load test for the v2 grader.

Reproduces the worst case that took a box down at geo2 (2026-07-24): the reward rendering
every rollout with soffice, 64 at once. Measures whether the render grader's own footprint
is safe on THIS box (240 cores, 1771 GB), and prints a GO/NO-GO on stability + overhead.

We deliberately fire all 64 truly concurrently (ThreadPoolExecutor, soffice is subprocess-
bound so the GIL is irrelevant) rather than the amortised batch path, because verl's reward
computation is the concurrent case and a semaphore cap is the fallback if this fails.
"""
import concurrent.futures as cf
import glob, os, subprocess, sys, threading, time

sys.path.insert(0, "/home/ubuntu/powerbench/agentic")
import render_metrics as rm  # noqa: E402

N = 64
DECKS = sorted(glob.glob("/home/ubuntu/powerbench/PPTArena/Original/*.pptx"))[:N]
DECKS = (DECKS * ((N // max(1, len(DECKS))) + 1))[:N]  # pad to 64 if fewer
print("decks: %d unique, %d total slots" % (len(set(DECKS)), len(DECKS)), flush=True)

_stop = threading.Event()
peak = {"load1": 0.0, "soffice": 0, "mem_used_gb": 0.0}
samples = []


def soffice_count():
    try:
        out = subprocess.run(["pgrep", "-c", "-f", "soffice"], capture_output=True, text=True)
        return int(out.stdout.strip() or 0)
    except Exception:
        return -1


def watcher():
    """Every 2s: record load, soffice count, mem, and a responsiveness ping (a trivial
    command must return in <1s or the host is wedged)."""
    while not _stop.is_set():
        load1 = os.getloadavg()[0]
        sc = soffice_count()
        try:
            with open("/proc/meminfo") as f:
                mi = {l.split(":")[0]: int(l.split()[1]) for l in f}
            mem_used = (mi["MemTotal"] - mi["MemAvailable"]) / 1024.0 / 1024.0
        except Exception:
            mem_used = -1
        t0 = time.time()
        subprocess.run(["true"], capture_output=True)
        ping_ms = (time.time() - t0) * 1000
        peak["load1"] = max(peak["load1"], load1)
        peak["soffice"] = max(peak["soffice"], sc)
        peak["mem_used_gb"] = max(peak["mem_used_gb"], mem_used)
        samples.append((round(load1, 1), sc, round(mem_used), round(ping_ms)))
        _stop.wait(2.0)


def score_one(path):
    t0 = time.time()
    r = rm.score_render(path, use_cache=False)
    return time.time() - t0, (r is not None)


def main():
    w = threading.Thread(target=watcher, daemon=True); w.start()
    t0 = time.time()
    times, oks = [], 0
    with cf.ThreadPoolExecutor(max_workers=N) as ex:
        for dt, ok in ex.map(score_one, DECKS):
            times.append(dt); oks += int(ok)
    wall = time.time() - t0
    _stop.set(); w.join(timeout=3)

    times.sort()
    p50 = times[len(times) // 2]
    p95 = times[int(len(times) * 0.95)]
    print("\n===== 64-CONCURRENT RENDER LOAD TEST =====", flush=True)
    print("wall-clock (all 64)      : %.1f s" % wall)
    print("per-deck  mean/p50/p95   : %.2f / %.2f / %.2f s"
          % (sum(times) / len(times), p50, p95))
    print("renders succeeded        : %d / %d" % (oks, N))
    print("PEAK load-avg(1m)        : %.1f  (cores=%d)" % (peak["load1"], os.cpu_count()))
    print("PEAK soffice procs       : %d" % peak["soffice"])
    print("PEAK mem used            : %.0f GB" % peak["mem_used_gb"])
    max_ping = max((s[3] for s in samples), default=0)
    print("worst responsiveness ping: %d ms  (host wedged if >1000)" % max_ping)
    print("\nsamples (load1, soffice, mem_gb, ping_ms):")
    for s in samples:
        print("  ", s)

    # ---- GO / NO-GO ----------------------------------------------------------
    # Overhead budget: a training step is ~37 min. If 64 renders finish in <2 min AND the
    # host never wedged (ping <1s) AND all renders succeeded, the grader is train-safe.
    ok = (wall < 120 and max_ping < 1000 and oks == N)
    print("\n%s" % ("GO   -- render grader is host-safe at 64-concurrent (%.0fs, %.0f%% of a "
                    "37-min step, host stayed responsive)." % (wall, 100 * wall / (37 * 60))
                    if ok else
                    "NO-GO -- cap reward render concurrency with a semaphore; see peaks above."))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
