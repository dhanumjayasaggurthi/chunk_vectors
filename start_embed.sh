#!/bin/bash
set -euo pipefail
BACK_DIR="/home/sa-prd-rdi-react-prd/rimdocs_s3_pg"
PY="/home/sa-prd-rdi-react-prd/rimdocs_s3_pg/venv/bin/python"

PIDFILE="$BACK_DIR/embed.pid"
LOGFILE="$BACK_DIR/embedlogs.log"
PROFILE="$BACK_DIR/profiles/clinical_1536.json"

cd "$BACK_DIR"

# Validate profile before starting
if [ ! -f "$PROFILE" ]; then
    echo "ERROR: Embedding profile not found: $PROFILE"
    exit 1
fi

# If already running, do nothing
if [ -f "$PIDFILE" ]; then
    PID=$(cat "$PIDFILE")

    if [[ "$PID" =~ ^[0-9]+$ ]] &&
       kill -0 "$PID" 2>/dev/null &&
       ps -p "$PID" -o args= | grep -Fq -- "--embed-existing"; then
        echo "Embedding already running (PID $PID)"
        exit 0
    fi

    # Remove stale PID file
    rm -f "$PIDFILE"
fi

# Start embedding in background
nohup "$PY" -u main.py \
    --embed-existing \
    --profile "$PROFILE" \
    > "$LOGFILE" 2>&1 < /dev/null &

echo $! > "$PIDFILE"

echo "Embedding started (PID $(cat "$PIDFILE"))"
echo "Logs: $LOGFILE"