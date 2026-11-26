#!/bin/bash
cd "$(dirname "$0")/.." || exit 1
[[ -x .venv/bin/python ]] || { echo "Run scripts/setup.sh first."; exit 1; }
[[ -f .env ]] && { port=$(sed -n 's/^MYTV_PORT=//p' .env | tail -1); data=$(sed -n 's/^MYTV_DATA_DIR=//p' .env | tail -1); }
port=${MYTV_PORT:-${port:-8400}}; data=${MYTV_DATA_DIR:-${data:-data}}; data="${data/#\~/$HOME}"
if curl -s -o /dev/null -m 3 "http://127.0.0.1:$port/api/status"; then
  echo "MyTV is already running: http://localhost:$port"; exit 0
fi
mkdir -p "$data/logs"
nohup scripts/run.sh > /dev/null 2>&1 &
disown
printf "Starting"
for _ in $(seq 60); do
  if curl -s -o /dev/null -m 2 "http://127.0.0.1:$port/api/status"; then
    ip=$(ipconfig getifaddr en0 || ipconfig getifaddr en1 || echo localhost)
    printf "\n\nDashboard:          http://localhost:%s\nApple TV playlist:  http://%s:%s/playlist.m3u\nApple TV guide:     http://%s:%s/epg.xml\n" "$port" "$ip" "$port" "$ip" "$port"
    exit 0
  fi
  printf "."; sleep 1
done
printf "\nMyTV did not start. Last log lines:\n"; tail -20 "$data/logs/stdout.log" "$data/logs/mytv.log"; exit 1
