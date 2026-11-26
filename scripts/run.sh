#!/bin/bash
# Keeps the server alive: restarts it if it exits or stops answering. Used by start.sh and by launchd.
cd "$(dirname "$0")/.." || exit 1
for p in /opt/homebrew/bin /usr/local/bin; do [[ -d $p ]] && PATH="$PATH:$p"; done
export PATH
[[ -f .env ]] && { port=$(sed -n 's/^MYTV_PORT=//p' .env | tail -1); data=$(sed -n 's/^MYTV_DATA_DIR=//p' .env | tail -1); }
port=${MYTV_PORT:-${port:-8400}}; data=${MYTV_DATA_DIR:-${data:-data}}; data="${data/#\~/$HOME}"
mkdir -p "$data/logs"
echo $$ > "$data/mytv.pid"
child=""
finish() { [[ -n $child ]] && kill "$child" 2>/dev/null && wait "$child" 2>/dev/null; rm -f "$data/mytv.pid"; exit 0; }
trap finish TERM INT

# idle sleep is blocked for as long as this script runs
caffeinate -i -s -w $$ &

while true; do
  .venv/bin/python -m mytv >> "$data/logs/stdout.log" 2>&1 &
  child=$!
  misses=0; grace=$((SECONDS + 90))
  while kill -0 "$child" 2>/dev/null; do
    sleep 5 & wait $!
    (( SECONDS < grace )) && continue
    if curl -s -o /dev/null -m 10 "http://127.0.0.1:$port/api/status"; then misses=0; else misses=$((misses + 1)); fi
    if (( misses >= 4 )); then echo "$(date) not answering, restarting" >> "$data/logs/stdout.log"; kill -9 "$child"; fi
  done
  wait "$child"
  echo "$(date) server exited, restarting in 3s" >> "$data/logs/stdout.log"
  sleep 3 & wait $!
done
