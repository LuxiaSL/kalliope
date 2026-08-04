#!/usr/bin/env bash
# kalliope: start the picker/state server and the liquidsoap mixer together.
# Ctrl-C stops both. Logs land in /tmp/claude-output/ style paths under state.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(dirname "$here")"

cleanup() {
  trap - TERM INT EXIT
  [[ -n "${server_pid:-}" ]] && kill "$server_pid" 2>/dev/null || true
  [[ -n "${liq_pid:-}" ]] && kill "$liq_pid" 2>/dev/null || true
  wait 2>/dev/null || true
}
trap cleanup TERM INT EXIT

echo "[kalliope] starting server..."
(cd "$repo" && exec uv run kalliope-server) &
server_pid=$!

# wait for the server to answer before letting liquidsoap loose
for _ in $(seq 1 50); do
  curl -sf --max-time 1 "http://127.0.0.1:${KALLIOPE_PORT:-8321}/now" >/dev/null 2>&1 && break
  if ! kill -0 "$server_pid" 2>/dev/null; then
    echo "[kalliope] server died during startup" >&2
    exit 1
  fi
  sleep 0.2
done

echo "[kalliope] starting liquidsoap..."
liquidsoap "$here/station.liq" &
liq_pid=$!

echo "[kalliope] on air — player: http://127.0.0.1:${KALLIOPE_PORT:-8321}/"
wait -n "$server_pid" "$liq_pid"
echo "[kalliope] a component exited; shutting down" >&2
