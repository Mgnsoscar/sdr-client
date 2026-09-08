#!/usr/bin/env bash
# run_local.sh — launch the real client pointed at a LOCAL agent, for headless
# integration testing. Writes a units.yaml into a scratch data dir (once) and
# starts main.py. Full recipe: sdr-agent/docs/local-integration-run.md.
#
# Start the agent first (sdr-agent/deploy/run_local.sh, backgrounded), then run
# this. Defaults to Qt's offscreen platform so it needs no monitor; set
# QT_QPA_PLATFORM= (empty) or =xcb if you have a display.
#
# Env overrides:
#   SDR_LOCAL_RUN_DIR   scratch root (default /tmp/sdr-local); data → <root>/client-data
#   SDR_AGENT_HOST      agent address written into units.yaml (default: this host's
#                       first IP). MUST be a bare host — no port, no ":", not 127.*
#                       (the client drops loopback/colon addresses on load).
#   QT_QPA_PLATFORM     Qt platform (default offscreen)
set -euo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"                 # sdr-client repo root
RUN_DIR="${SDR_LOCAL_RUN_DIR:-/tmp/sdr-local}"
DATA="$RUN_DIR/client-data"
AGENT_HOST="${SDR_AGENT_HOST:-$(hostname -I 2>/dev/null | awk '{print $1}')}"
mkdir -p "$DATA"

if [ ! -f "$DATA/units.yaml" ]; then
    cat > "$DATA/units.yaml" <<YAML
api_key: ""
units:
  - label: Broadcaster Lab
    type: broadcaster
    addresses:
      - ${AGENT_HOST}
YAML
    echo "seeded $DATA/units.yaml → agent ${AGENT_HOST}:8765"
fi

echo "==> client data=$DATA  agent=${AGENT_HOST}:8765  platform=${QT_QPA_PLATFORM:-offscreen}"
exec env \
    QT_QPA_PLATFORM="${QT_QPA_PLATFORM:-offscreen}" \
    PYTHONPATH="$HERE" \
    SDR_CLIENT_DATA_DIR="$DATA" \
    python3 "$HERE/main.py"
