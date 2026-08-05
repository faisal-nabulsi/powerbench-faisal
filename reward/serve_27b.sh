#!/usr/bin/env bash
# Serve a checkpoint via vLLM (OpenAI-compatible, port 8000) for the INDEPENDENT judge eval.
# Uses 2 GPUs so it can run on the idle box; KILL IT before launching training (needs all 8).
#   MODEL=~/powerbench/models/Qwen3.6-27B ./serve_27b.sh        # baseline
#   MODEL=~/powerbench/hf_export           ./serve_27b.sh        # trained (after model_merger)
set -xeuo pipefail
cd ~/powerbench
export PATH="$HOME/powerbench/.venv/bin:$PATH"
unset PYTORCH_CUDA_ALLOC_CONF
MODEL=${MODEL:-$HOME/powerbench/models/Qwen3.6-27B}
export CUDA_VISIBLE_DEVICES=${GPUS:-0,1}
python -m vllm.entrypoints.openai.api_server \
  --model "$MODEL" \
  --served-model-name qwen \
  --tensor-parallel-size 2 \
  --port 8000 \
  --trust-remote-code \
  --no-enable-prefix-caching \
  --max-model-len 32768 \
  --gpu-memory-utilization 0.85 \
  --enforce-eager
