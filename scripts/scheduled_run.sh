#!/usr/bin/env bash
# Run hourly with cron on a persistent Linux host. Never enable shell tracing: .env has secrets.
set -uo pipefail

cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." || exit 1
mkdir -p data || exit 1
exec 9>data/scheduler.lock
flock -n 9 || exit 0

if [[ ! -f .env ]]; then
    printf '%s\n' 'Missing .env: copy .env.example and configure Supabase.' >&2
    exit 1
fi
set -a
source .env || exit 1
set +a

if [[ -x .venv/bin/python ]]; then
    tracker_python='.venv/bin/python'
else
    tracker_python='python3'
fi

date -u '+Scheduler started: %Y-%m-%dT%H:%M:%SZ'
sync_status=0
"$tracker_python" -m pelikan_tracker sync || sync_status=$?
# A failed upload must not prevent the next scheduled collection from saving locally.
tick_status=0
"$tracker_python" -m pelikan_tracker tick || tick_status=$?
printf 'Scheduler finished: sync=%s tick=%s\n' "$sync_status" "$tick_status"
if (( sync_status != 0 || tick_status != 0 )); then
    exit 1
fi
