#!/usr/bin/env bash
# Resumable PostgreSQL -> Azure embeddings. Safe default: 1,000-row pilot.
# Confirmed immbot configuration lives in profiles/immbot_approved_1536.json:
# RPM=30 (conservative/unverified limit), TPM=750000 (75% of gateway header),
# batch_size=128, batch_tokens=24000, max_attempts=5, timeout=90, lease=300.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
BACK_DIR="${BACK_DIR:-$SCRIPT_DIR}"
PROFILE="${EMBED_PROFILE:-$BACK_DIR/profiles/immbot_approved_1536.json}"
# Relative profile paths are always resolved from this checkout.
if [[ "$PROFILE" != /* ]]; then PROFILE="$BACK_DIR/$PROFILE"; fi
WORKERS="${EMBED_WORKERS:-4}"
MODE="${EMBED_MODE:-pilot}"  # pilot | full | dry-run
PIDFILE="${EMBED_PIDFILE:-$BACK_DIR/embed.pid}"
LOGFILE="${EMBED_LOGFILE:-$BACK_DIR/embedlogs.log}"

if [[ ! "$WORKERS" =~ ^[1-9][0-9]*$ ]] || (( WORKERS > 128 )); then
  echo "EMBED_WORKERS must be an integer between 1 and 128" >&2; exit 2
fi
if [[ ! -d "$BACK_DIR" || ! -f "$PROFILE" ]]; then
  echo "Embedding workdir/profile missing: $BACK_DIR / $PROFILE" >&2; exit 2
fi
cd "$BACK_DIR"
if [[ -z "${PY:-}" ]]; then
  if [[ -x "$BACK_DIR/venv/bin/python" ]]; then
    PY="$BACK_DIR/venv/bin/python"
  elif [[ -x "$BACK_DIR/.venv/bin/python" ]]; then
    PY="$BACK_DIR/.venv/bin/python"
  else
    PY="python3"
  fi
fi
ARGS=(--embed-existing --profile "$PROFILE" --doc-workers "$WORKERS")

# Leave per-deployment quotas and batching in the profile by default;
# these environment settings intentionally override the profile if provided.
for pair in \
  "EMBED_RPM:embedding.rpm" \
  "EMBED_TPM:embedding.tpm" \
  "EMBED_BATCH_SIZE:embedding.batch_size" \
  "EMBED_BATCH_TOKENS:embedding.batch_tokens" \
  "EMBED_MAX_ATTEMPTS:embedding.max_attempts" \
  "EMBED_TIMEOUT_SECONDS:embedding.timeout_seconds" \
  "EMBED_LEASE_SECONDS:embedding.lease_seconds"; do
  env_key="${pair%%:*}"
  profile_key="${pair#*:}"
  value="${!env_key:-}"
  if [[ -n "$value" ]]; then
    if [[ ! "$value" =~ ^[0-9]+$ ]] || (( 10#$value < 1 )); then
      echo "$env_key must be a positive integer" >&2; exit 2
    fi
    ARGS+=(--set "$profile_key=$value")
  fi
done

case "$MODE" in
  dry-run)
    ARGS+=(--dry-run)
    echo "DRY RUN: no embeddings or source writes will occur"
    exec "$PY" -u main.py "${ARGS[@]}"
    ;;
  pilot)
    MAX_FILES="${EMBED_MAX_FILES:-1000}"
    if [[ ! "$MAX_FILES" =~ ^[1-9][0-9]*$ ]]; then
      echo "EMBED_MAX_FILES must be a positive integer in pilot mode" >&2; exit 2
    fi
    ARGS+=(--max-files "$MAX_FILES")
    ;;
  full)
    if [[ "${EMBED_CONFIRM_FULL:-}" != "YES" ]]; then
      echo "Full backfill is explicit: EMBED_MODE=full EMBED_CONFIRM_FULL=YES ./start_embed.sh" >&2
      exit 2
    fi
    ;;
  *)
    echo "EMBED_MODE must be dry-run, pilot, or full" >&2; exit 2
    ;;
esac

if [[ "${EMBED_RETRY_FAILED:-0}" == "1" ]]; then ARGS+=(--retry-embed-failures); fi
if [[ "${EMBED_RESCAN:-0}" == "1" ]]; then ARGS+=(--rescan-embeddings); fi

if [[ -f "$PIDFILE" ]]; then
  PID="$(cat "$PIDFILE")"
  if [[ "$PID" =~ ^[0-9]+$ ]] && kill -0 "$PID" 2>/dev/null &&
     ps -p "$PID" -o args= | grep -Fq -- "--embed-existing"; then
    echo "Embedding already running locally (PID $PID). Log: $LOGFILE"
    exit 0
  fi
  rm -f "$PIDFILE"
fi

echo "Starting $MODE embeddings: profile=$PROFILE workers=$WORKERS"
echo "Quota/batch defaults come from profile; explicit EMBED_* overrides take precedence."
nohup "$PY" -u main.py "${ARGS[@]}" > "$LOGFILE" 2>&1 < /dev/null &
echo "$!" > "$PIDFILE"
echo "Started PID $(cat "$PIDFILE"); monitor: tail -f '$LOGFILE'"
if [[ "$MODE" == "full" ]]; then
  echo "WARNING: Full mode has no row cap and writes source vectors."
else
  echo "Mode=$MODE; review embedlogs.log and PostgreSQL task statuses for progress."
fi
