#!/bin/bash
# High-throughput, durable embedding backfill; run independently on any host.
set -euo pipefail
BACK_DIR="${BACK_DIR:-/home/sa-prd-rdi-react-prd/rimdocs_s3_pg}"
PY="${PY:-$BACK_DIR/venv/bin/python}"
PROFILE="${EMBED_PROFILE:-$BACK_DIR/profiles/clinical_1536.json}"
WORKERS="${EMBED_WORKERS:-16}"
BATCH_SIZE="${EMBED_BATCH_SIZE:-128}"
PIDFILE="${EMBED_PIDFILE:-$BACK_DIR/embed.pid}"
LOGFILE="${EMBED_LOGFILE:-$BACK_DIR/embedlogs.log}"
cd "$BACK_DIR"
if [ ! -f "$PROFILE" ]; then echo "Profile not found: $PROFILE" >&2; exit 1; fi
if [ -f "$PIDFILE" ]; then
  PID=$(cat "$PIDFILE")
  if [[ "$PID" =~ ^[0-9]+$ ]] && kill -0 "$PID" 2>/dev/null &&
     ps -p "$PID" -o args= | grep -Fq -- "--embed-existing"; then
    echo "Embedding already running locally (PID $PID)"; exit 0
  fi
  rm -f "$PIDFILE"
fi
ARGS=(--embed-existing --profile "$PROFILE" --doc-workers "$WORKERS"
      --set "embedding.batch_size=$BATCH_SIZE")
if [[ -n "${EMBED_RPM:-}" ]]; then ARGS+=(--set "embedding.rpm=$EMBED_RPM"); fi
if [[ -n "${EMBED_TPM:-}" ]]; then ARGS+=(--set "embedding.tpm=$EMBED_TPM"); fi
if [[ "${EMBED_RETRY_FAILED:-0}" == "1" ]]; then ARGS+=(--retry-embed-failures); fi
if [[ "${EMBED_RESCAN:-0}" == "1" ]]; then ARGS+=(--rescan-embeddings); fi
nohup "$PY" -u main.py "${ARGS[@]}" > "$LOGFILE" 2>&1 < /dev/null &
echo $! > "$PIDFILE"
echo "Embedding started on this host (PID $(cat "$PIDFILE")); log: $LOGFILE"
