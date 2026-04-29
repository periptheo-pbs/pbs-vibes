#!/usr/bin/env bash
################################################################################
# Hermes Vibes Trader — 5-minute cron entry point (simplified printf version)
################################################################################

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

# Load environment variables from .env (if present)
if [ -f "$PROJECT_ROOT/.env" ]; then
    set -o allexport
    source "$PROJECT_ROOT/.env"
    set +o allexport
fi

mkdir -p live logs

RUN_ID="$(date -u +%Y%m%d_%H%M%S)"
LOG_FILE="logs/vibes_trader_$(date -u +%Y%m%d).jsonl"
PID_FILE="live/vibes_trader.pid"

# ── Overlap guard ─────────────────────────────────────────────────────────────
if [ -f "$PID_FILE" ]; then
    OLD_PID=$(cat "$PID_FILE" 2>/dev/null || echo "")
    if [ -n "$OLD_PID" ] && kill -0 "$OLD_PID" 2>/dev/null; then
        echo "[$RUN_ID] Another instance (PID $OLD_PID) still running — exiting"
        exit 1
    else
        echo "[$RUN_ID] Removing stale PID file"
        rm -f "$PID_FILE"
    fi
fi
echo $$ > "$PID_FILE"
trap 'rm -f "$PID_FILE"' EXIT

################################################################################
# STEP 1 — Collect market data and portfolio state (top 5 assets)
################################################################################
echo "[$RUN_ID] Step 1: Running kz_calculator.py for top 5 assets..."

TOP_ASSETS=("BTC" "ETH" "SOL" "MATIC" "DOGE")
KZ_AGGREGATED=""

for asset in "${TOP_ASSETS[@]}"; do
    echo "  Calculating KZ for $asset..."
    KZ_FILE="live/kz_output_${asset}_${RUN_ID}.txt"
    if python3 scripts/kz_calculator.py --asset "$asset" > "$KZ_FILE" 2>&1; then
        KZ_AGGREGATED+=$'\n\n=== '"$asset"' ===\n'"$(cat "$KZ_FILE")"
    else
        echo "  WARN: kz_calculator.py failed for $asset; continuing"
        KZ_AGGREGATED+=$'\n\n=== '"$asset"' ===\n[Failed to generate KZ output]'
    fi
done

KZ_BODY="$KZ_AGGREGATED"

PORTFOLIO_STATE="$(cat live/vibes_positions.json 2>/dev/null || echo '{}')"
if [ "$PORTFOLIO_STATE" = "{}" ] || [ -z "$PORTFOLIO_STATE" ]; then
    PORTFOLIO_SECTION="No open positions."
else
    # Pretty-print the entire multi-asset portfolio state
    PORTFOLIO_SECTION="$(echo "$PORTFOLIO_STATE" | python3 -c 'import json, sys; s=json.load(sys.stdin); print(json.dumps(s, indent=2))')"
fi

################################################################################
# STEP 2 — Build prompt and query Hermes
################################################################################
echo "[$RUN_ID] Step 2: Sending prompt to Hermes..."

# Read the two-%s template (Market Context placeholder, Portfolio placeholder, rest)
PROMPT_TEMPLATE="$(cat scripts/prompt_template.txt)"

# Assemble full message; printf does safe %s substitution (no shell expansion on KZ_BODY)
FULL_MESSAGE="$(printf "%s" "$PROMPT_TEMPLATE" "$KZ_BODY" "$PORTFOLIO_SECTION")"

HERMES_REPLY="$(hermes chat -q "$FULL_MESSAGE" 2>&1)"
HERMES_EXIT=$?

if [ $HERMES_EXIT -ne 0 ]; then
    echo "ERROR: hermes chat failed (exit $HERMES_EXIT)" >&2
    echo "Hermes stderr: $HERMES_REPLY" >&2
    exit 1
fi
echo "$HERMES_REPLY" > "logs/hermes_reply_${RUN_ID}.txt"

################################################################################
# STEP 3 — Parse decision JSON
################################################################################
echo "[$RUN_ID] Step 3: Parsing Hermes' decision..."
DECISION_JSON="$(echo "$HERMES_REPLY" | python3 scripts/hermes_parse_decision.py)"

if echo "$DECISION_JSON" | python3 -c "import json,sys; d=json.load(sys.stdin); exit(0 if d.get('action') else 1)" 2>/dev/null; then
    :
else
    echo "WARN: Invalid decision JSON — defaulting to HOLD"
    DECISION_JSON='{"action":"HOLD","side":null,"size_btc":0.0,"limit_offset_bps":0,"leverage":1,"reason":"parse_failed"}'
fi

echo "Decision: $DECISION_JSON"

################################################################################
# STEP 4 — Execute on Hyperliquid testnet (do NOT abort on failure)
################################################################################
echo "[$RUN_ID] Step 4: Executing on HL testnet..."
set +e
EXECUTION_LOG="$(python3 scripts/hl_execute_decision.py \
    --decision "$DECISION_JSON" \
    --run-id "$RUN_ID" \
    2>&1)"
EXEC_EXIT=$?
set -e

################################################################################
# STEP 5 — Build structured log entry
################################################################################
export RUN_ID KZ_OUTPUT HERMES_REPLY DECISION_JSON EXECUTION_LOG EXEC_EXIT HERMES_REPLY_FILE
HERMES_REPLY_FILE="logs/hermes_reply_${RUN_ID}.txt"
LOG_ENTRY="$(python3 scripts/hermes_build_log.py)"

echo "$LOG_ENTRY" >> "$LOG_FILE"
echo "[$RUN_ID] Logged to $LOG_FILE"

################################################################################
# DONE
################################################################################
ACTION=$(echo "$DECISION_JSON" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("action","UNKNOWN"))' 2>/dev/null || echo 'UNKNOWN')
echo "[$RUN_ID] Cycle complete. Action: $ACTION"

exit 0
