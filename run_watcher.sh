#!/usr/bin/env bash
# run_watcher.sh — keep the watcher alive 24/7. If it dies, restart it.
cd "$(dirname "$0")"
while true; do
  echo "[keepalive] starting watcher at $(date -u)" >> watcher.log
  python3 -u watcher.py 8 >> watcher.log 2>&1
  echo "[keepalive] watcher exited ($?) at $(date -u); restarting in 5s" >> watcher.log
  sleep 5
done
