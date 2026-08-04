#!/usr/bin/env bash
# kalliope: start the picker/state server and the liquidsoap mixer together.
# Ctrl-C stops both. Logs land in /tmp/claude-output/ style paths under state.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(dirname "$here")"

# .env (gitignored) feeds both the server and this script's own knobs
if [[ -f "$repo/.env" ]]; then set -a; source "$repo/.env"; set +a; fi

# --- self-update -----------------------------------------------------------
# Keeps casual installs current with origin/main. Opt out with
# KALLIOPE_AUTO_UPDATE=off (recommended in a development checkout).
# Every failure path falls through to starting the station as-is:
# an update must never keep the radio off the air.
self_update() {
  [[ "${KALLIOPE_AUTO_UPDATE:-on}" == "off" ]] && return 1
  [[ -n "${KALLIOPE_UPDATED:-}" ]] && return 1        # already updated this boot
  command -v git >/dev/null 2>&1 || return 1
  git -C "$repo" rev-parse --git-dir >/dev/null 2>&1 || return 1  # zip install
  local branch
  branch="$(git -C "$repo" branch --show-current 2>/dev/null)"
  [[ "$branch" == "main" ]] || { echo "[kalliope] on branch '${branch:-?}' — skipping auto-update"; return 1; }

  GIT_TERMINAL_PROMPT=0 timeout 20 git -C "$repo" fetch --quiet origin main 2>/dev/null \
    || { echo "[kalliope] update check skipped (offline?)"; return 1; }
  local behind
  behind="$(git -C "$repo" rev-list --count HEAD..origin/main 2>/dev/null || echo 0)"
  [[ "$behind" -gt 0 ]] || return 1

  echo "[kalliope] $behind commit(s) behind origin/main — updating"
  local stashed=0
  if ! git -C "$repo" diff --quiet 2>/dev/null \
     || ! git -C "$repo" diff --cached --quiet 2>/dev/null; then
    if git -C "$repo" stash push --quiet -m "kalliope-auto-update $(date +%F-%H%M%S)"; then
      stashed=1
      echo "[kalliope] local changes stashed"
    else
      echo "[kalliope] could not stash local changes — skipping update"
      return 1
    fi
  fi

  local updated=1
  if git -C "$repo" merge --ff-only --quiet origin/main 2>/dev/null; then
    echo "[kalliope] now at $(git -C "$repo" rev-parse --short HEAD)"
    (cd "$repo" && uv sync --quiet) || echo "[kalliope] warning: uv sync failed"
  else
    echo "[kalliope] cannot fast-forward (local commits?) — staying on current version"
    updated=0
  fi

  if [[ $stashed -eq 1 ]]; then
    if git -C "$repo" stash pop --quiet 2>/dev/null; then
      echo "[kalliope] local changes restored"
    else
      # pop conflicted: keep the tree clean on the updated code; the edits
      # stay safe in the stash for manual resolution
      git -C "$repo" checkout --force --quiet 2>/dev/null || true
      echo "[kalliope] WARNING: your local changes conflict with the update."
      echo "[kalliope] They are preserved in 'git stash list' — resolve when convenient."
    fi
  fi
  [[ $updated -eq 1 ]]
}

if self_update; then
  export KALLIOPE_UPDATED=1
  echo "[kalliope] relaunching updated station script..."
  exec "$repo/station/run.sh"
fi

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
