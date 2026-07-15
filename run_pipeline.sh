#!/bin/bash
# Step 0 (calibration) -> size N -> main composition sweep. Fully resumable.
cd "$(dirname "$0")"
echo "=== PIPELINE start $(date) ===" >> sweep_pipeline.log
python3 -m experiments.sweep calibrate --m 0.5 --seed 1 --reps 5 \
  --provider openai --out results_sweep >> sweep_pipeline.log 2>&1
python3 -m experiments.calib_n --out results_sweep >> sweep_pipeline.log 2>&1
N=$(cat results_sweep/calib_N.txt 2>/dev/null || echo 10)
SEEDS=$(seq 1 "$N" | tr '\n' ' ')
echo "=== SWEEP start $(date) N=$N seeds=$SEEDS ===" >> sweep_pipeline.log
python3 -m experiments.sweep sweep --m-levels 0.3 0.5 0.7 --seeds $SEEDS \
  --provider openai --out results_sweep >> sweep_pipeline.log 2>&1
echo "=== PIPELINE done $(date) ===" >> sweep_pipeline.log
