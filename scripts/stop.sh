#!/bin/bash
cd "$(dirname "$0")/.." || exit 1
[[ -f .env ]] && data=$(sed -n 's/^MYTV_DATA_DIR=//p' .env | tail -1)
data=${MYTV_DATA_DIR:-${data:-data}}; data="${data/#\~/$HOME}"
label=com.mytv.server
if launchctl print "gui/$(id -u)/$label" >/dev/null 2>&1; then
  launchctl bootout "gui/$(id -u)/$label"
  echo "Stopped until the next login. To start it now: scripts/autostart.sh on"
  exit 0
fi
pid=$(cat "$data/mytv.pid" 2>/dev/null)
if [[ -z $pid ]] || ! ps -p "$pid" -o command= | grep -q run.sh; then echo "MyTV is not running."; exit 0; fi
kill "$pid"
for _ in $(seq 30); do kill -0 "$pid" 2>/dev/null || { echo "MyTV stopped."; exit 0; }; sleep 1; done
echo "It did not stop in time; forcing."
pkill -9 -P "$pid"; kill -9 "$pid"
