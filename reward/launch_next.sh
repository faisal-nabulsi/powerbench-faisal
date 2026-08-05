#!/usr/bin/env bash
# ==========================================================================================
# ONE-COMMAND LAUNCH for the next GRPO run. Encodes every relaunch lesson from TODO.md so a
# stale-vLLM/orphan-GPU/stale-IPC race can't silently poison the run.
#
#   ./launch_next.sh          # clean GPUs, verify free, launch run_next_geo.sh under setsid
#   ./launch_next.sh --smoke  # same cleanup, then a 1-step smoke (n=2, val off, save_freq=1)
#
# WHY EACH STEP (all bit us before):
#   * kill by GPU-OWNER PID, not name — pkill -f main_ppo/vllm misses VLLM::Worker_TP /
#     EngineCore children, which orphan and hold GPU memory (found procs 5.6h old).
#   * rm /dev/shm/cuda.shm.* + /tmp/ray/* — stale IPC/Ray state deadlocks a fresh vLLM.
#   * verify every GPU ~0 MiB before launch (not just "no main_ppo"): a single leftover vLLM
#     stacks a 2nd server per GPU -> actor OOM.
#   * setsid nohup — detached launches kept not sticking otherwise.
#   * box has no `bc` — use awk for GPU-mem math.
# ==========================================================================================
set -uo pipefail
cd ~/powerbench

SMOKE=0
[ "${1:-}" = "--smoke" ] && SMOKE=1

echo "[launch] killing GPU-owning processes..."
for _ in $(seq 1 10); do
  pids=$(nvidia-smi --query-compute-apps=pid --format=csv,noheader 2>/dev/null | tr -d ' ' | grep -v '^$' || true)
  [ -z "$pids" ] && break
  echo "  killing: $pids"
  for p in $pids; do kill -9 "$p" 2>/dev/null || true; done
  sleep 3
done
pkill -9 -f 'verl.trainer.main_ppo' 2>/dev/null || true
ray stop --force 2>/dev/null || true

echo "[launch] clearing stale IPC / Ray state..."
rm -f /dev/shm/cuda.shm.* 2>/dev/null || true
rm -rf /tmp/ray/* 2>/dev/null || true

echo "[launch] waiting for all GPUs to report ~0 MiB used..."
for _ in $(seq 1 20); do
  busy=$(nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits | awk '$1>500{n++} END{print n+0}')
  [ "$busy" = "0" ] && { echo "  all GPUs free."; break; }
  echo "  $busy GPU(s) still >500 MiB, waiting..."; sleep 5
done
busy=$(nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits | awk '$1>500{n++} END{print n+0}')
if [ "$busy" != "0" ]; then
  echo "[launch] ABORT: $busy GPU(s) still busy after cleanup. Inspect: nvidia-smi"; exit 1
fi

TS=$(date +%Y%m%d_%H%M%S 2>/dev/null || echo run)
if [ "$SMOKE" = "1" ]; then
  LOG=~/powerbench/smoke_next_${TS}.log
  echo "[launch] SMOKE: 1 step, n=2, val off, save_freq=1 -> $LOG"
  setsid nohup bash ~/powerbench/run_next_smoke.sh > "$LOG" 2>&1 &
else
  LOG=~/powerbench/next_geo_${TS}.log
  echo "[launch] REAL RUN -> $LOG"
  setsid nohup bash ~/powerbench/run_next_geo.sh > "$LOG" 2>&1 &
fi
echo "[launch] pid $! ; tail -f $LOG"
echo "[launch] REMINDER: first forward compiles the GDN Triton kernel (~15-25 min, GPUs pinned,"
echo "         log silent). That is NOT a hang. Watch gallery deck count as the progress signal."
echo "$LOG"
