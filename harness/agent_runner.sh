#!/usr/bin/env bash
set -euo pipefail

PROBLEM_STATEMENT="$1"
OUTPUT_DIR="$2"
MAX_TURNS="${3:-30}"

mkdir -p "$OUTPUT_DIR"

# Mount Gemini credentials from host
export GOOGLE_APPLICATION_CREDENTIALS="/root/.gemini/credentials.json"

# Run Gemini CLI non-interactively on the problem statement
gemini \
  -p "$PROBLEM_STATEMENT" \
  --approval-mode yolo \
  --output-format json \
  > "$OUTPUT_DIR/agent_log.json" 2>&1 || true

# Capture whatever the agent changed
git diff HEAD > "$OUTPUT_DIR/agent.patch"
git diff --name-only HEAD > "$OUTPUT_DIR/files_touched.txt"

# Count context tokens used (from agent log if available)
python3 -c "
import json
try:
    log = json.load(open('$OUTPUT_DIR/agent_log.json'))
    turns = len(log.get('turns', []))
    tokens = log.get('usage', {}).get('total_tokens', 'unknown')
    print(json.dumps({'turns': turns, 'tokens_used': tokens}))
except Exception:
    print(json.dumps({'turns': 'unknown', 'tokens_used': 'unknown'}))
" > "$OUTPUT_DIR/agent_stats.json"

echo "Agent run complete. Patch saved to $OUTPUT_DIR/agent.patch"
