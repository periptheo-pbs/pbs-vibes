#!/usr/bin/env bash
# Run pbs_vibe_check.py and output the result for the Hermes cronjob
cd /home/bakoe/pbs-vibes
python3 pbs_vibe_check.py --coins BTC ETH SOL 2>&1
