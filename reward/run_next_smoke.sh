#!/usr/bin/env bash
# 1-step end-to-end smoke of the EXACT next-run config (run_next_geo.sh), with only the
# scale knobs shrunk via env overrides so there is ONE source of truth for the real config.
# Proves: reward wiring + fixed-key schema, vLLM init (prefix caching off), GDN Triton
# compile, one full GRPO step, AND a checkpoint save (save_freq=1) — the save path is the
# one that crashed geo2 at step 9, so we prove it BEFORE the 17h real run.
#
#   real_train_batch_size = TRAIN_BSZ(4) * N_ROLLOUT(2) = 8  (divisible by 8 GPUs)  OK
#   MINI_BSZ(4) <= TRAIN_BSZ(4)  OK
set -euo pipefail
export N_ROLLOUT=2
export TRAIN_BSZ=4
export MINI_BSZ=4
export TOTAL_STEPS=1
export SAVE_FREQ=1            # prove the checkpoint save path
export TEST_FREQ=1000        # skip periodic eval
export VAL_BEFORE=False      # skip the 45-deck baseline (saves ~10 min; real run keeps it)
export EXP_NAME=next-geo-smoke
export CKPT_DIR=$HOME/powerbench/ckpt_smoke
export GEO_GALLERY=$HOME/powerbench/agentic/gallery_smoke
rm -rf "$CKPT_DIR"
exec bash ~/powerbench/run_next_geo.sh
