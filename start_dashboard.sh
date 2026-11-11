#!/usr/bin/env bash
# Local-only Streamlit embedding status dashboard. Does not touch running workers.
set -euo pipefail
APP_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$APP_DIR"
PORT="${EMBED_DASHBOARD_PORT:-8501}"
PIDFILE="${EMBED_DASHBOARD_PIDFILE:-$APP_DIR/dashboard.pid}"
LOGFILE="${EMBED_DASHBOARD_LOGFILE:-$APP_DIR/dashboard.log}"

if [[ ! "$PORT" =~ ^[0-9]+$ ]] || (( PORT < 1024 || PORT > 65535 )); then
  echo "EMBED_DASHBOARD_PORT must be 1024..65535" >&2; exit 2
fi

if [[ -z "${PY:-}" ]]; then
  if [[ -x "$APP_DIR/venv/bin/python" ]]; then PY="$APP_DIR/venv/bin/python"
  elif [[ -x "$APP_DIR/.venv/bin/python" ]]; then PY="$APP_DIR/.venv/bin/python"
  else PY=python3
  fi
fi

if [[ -f "$PIDFILE" ]]; then
  PID="$(cat "$PIDFILE")"
  if [[ "$PID" =~ ^[0-9]+$ ]] && kill -0 "$PID" 2>/dev/null &&
     ps -p "$PID" -o args= | grep -Fq -- "embedding_dashboard.py"; then
    echo "Dashboard already running (PID $PID). View via SSH tunnel on port $PORT."
    exit 0
  fi
  rm -f "$PIDFILE"
fi

printf '\n----- %s dashboard start -----\n' "$(date -Is)" >> "$LOGFILE"
nohup "$PY" -m streamlit run embedding_dashboard.py \
  --server.address 127.0.0.1 --server.port "$PORT" \
  >> "$LOGFILE" 2>&1 < /dev/null &
echo "$!" > "$PIDFILE"
echo "Dashboard PID $(cat "$PIDFILE"); localhost:$PORT (SSH tunnel required for remote access)"
echo "Logs: $LOGFILE"
echo "Embedding workers are unaffected by starting/stopping this dashboard."
