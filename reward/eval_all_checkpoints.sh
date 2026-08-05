#!/usr/bin/env bash
# Post-training pipeline: for each checkpoint stage, merge FSDP2 -> HF, serve, generate the 45
# held-out decks (greedy = val parity), render. Baseline (step 0) already done separately.
# Runs unattended; prints STAGE N DONE per stage and ALL_STAGES_DONE at the end.
set -uo pipefail
cd ~/powerbench
export PATH="$HOME/powerbench/.venv/bin:$PATH"
unset PYTORCH_CUDA_ALLOC_CONF

declare -A CKPT
CKPT[30]=$HOME/powerbench/ckpt_next/global_step_30/actor
CKPT[20]=$HOME/powerbench/ckpt_next/global_step_20/actor
CKPT[10]=$HOME/powerbench/ckpt_preserve/global_step_10/actor

kill_gpus(){
  for _ in $(seq 1 5); do
    p=$(nvidia-smi --query-compute-apps=pid --format=csv,noheader | tr -d ' ' | grep -v '^$')
    [ -z "$p" ] && break; for x in $p; do kill -9 "$x" 2>/dev/null; done; sleep 3
  done
  pkill -9 -f vllm.entrypoints 2>/dev/null; sleep 2
}

for S in 30 20 10; do
  echo "===== STAGE $S ====="
  HF=$HOME/powerbench/hf_export_$S
  if [ ! -f "$HF/config.json" ]; then
    echo "[stage $S] merging..."
    python -m verl.model_merger merge --backend fsdp --local_dir "${CKPT[$S]}" --target_dir "$HF" 2>&1 | tail -4
  fi
  [ -f "$HF/config.json" ] || { echo "[stage $S] MERGE FAILED, skipping"; continue; }
  kill_gpus
  echo "[stage $S] serving..."
  CUDA_VISIBLE_DEVICES=0,1 setsid nohup python -m vllm.entrypoints.openai.api_server \
    --model "$HF" --served-model-name qwen --tensor-parallel-size 2 --port 8000 \
    --trust-remote-code --no-enable-prefix-caching --max-model-len 32768 \
    --gpu-memory-utilization 0.85 --enforce-eager > "$HOME/powerbench/serve_$S.log" 2>&1 &
  up=0; for i in $(seq 1 40); do
    curl -s --max-time 5 http://localhost:8000/v1/models 2>/dev/null | grep -q qwen && { up=1; break; }
    sleep 15
  done
  [ "$up" = 1 ] || { echo "[stage $S] SERVE FAILED"; tail -5 "$HOME/powerbench/serve_$S.log"; kill_gpus; continue; }
  echo "[stage $S] generating 45 held-out decks..."
  OUT=$HOME/powerbench/agentic/eval_step_$S; rm -rf "$OUT"; mkdir -p "$OUT"
  python "$HOME/powerbench/agentic/frontier_baseline.py" --model qwen --provider local --n 45 \
    --max-tokens 8192 --concurrency 8 --temperature 0 --keep-decks "$OUT" \
    --out /tmp/step_${S}_local.json 2>&1 | tail -3
  python "$HOME/powerbench/agentic/render_decks.py" "$OUT" 2>&1 | tail -1
  kill_gpus
  echo "STAGE $S DONE: $(ls "$OUT"/deck_*.png 2>/dev/null | wc -l) decks rendered"
done
echo "ALL_STAGES_DONE"
