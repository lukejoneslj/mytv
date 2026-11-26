#!/bin/bash
# Checks this Mac's MyTV setup and prints what to fix. Safe to run any time.
cd "$(dirname "$0")/.." || exit 1
for p in /opt/homebrew/bin /usr/local/bin; do [[ -d $p ]] && PATH="$PATH:$p"; done
[[ -f .env ]] && port=$(sed -n 's/^MYTV_PORT=//p' .env | tail -1)
port=${MYTV_PORT:-${port:-8400}}
bad=0
pass() { printf '  \033[32mok\033[0m    %s\n' "$*"; }
warn() { printf '  \033[33mwarn\033[0m  %s\n' "$*"; }
fail() { printf '  \033[31mfail\033[0m  %s\n' "$*"; bad=1; }

echo "Setup"
[[ "$(uname)" == Darwin ]] && pass "macOS $(sw_vers -productVersion), $(uname -m)" || fail "MyTV needs macOS"
command -v brew >/dev/null && pass "Homebrew" || fail "Homebrew missing: install from https://brew.sh (needs your password), then run scripts/setup.sh"
command -v ffmpeg >/dev/null && pass "ffmpeg" || fail "ffmpeg missing: run scripts/setup.sh"
[[ -x .venv/bin/python ]] && .venv/bin/python -c "import mytv" 2>/dev/null && pass "Python environment" || fail "Python environment missing or broken: run scripts/setup.sh"
[[ -f frontend/dist/index.html ]] && pass "dashboard built" || fail "dashboard not built: run scripts/setup.sh"
if [[ -d "/Applications/Google Chrome.app" || -d "/Applications/Microsoft Edge.app" ]]; then pass "Chrome or Edge (needed to stream tabs)"
else warn "No Chrome or Edge: streaming tabs needs one. brew install --cask google-chrome"; fi
case "$(pwd)" in /Volumes/*) warn "Project is on an external drive: autostart cannot be used, add 'Start MyTV.command' to Login Items instead" ;; esac

echo "Running"
if curl -s -o /dev/null -m 3 "http://127.0.0.1:$port/api/status"; then
  pass "server answers on port $port"
  h=$(curl -s -m 5 "http://127.0.0.1:$port/health")
  echo "$h" | grep -q '"ok":true' && pass "health" || warn "health reports a problem (normal for the first minutes): $h"
  if curl -s -m 5 "http://127.0.0.1:$port/onair/1.m3u8" | grep -q EXTINF; then pass "live channels serve video"
  else warn "live channels not ready yet (their standby cards take up to a minute after the first start)"; fi
  ip=$(ipconfig getifaddr en0 || ipconfig getifaddr en1)
  if [[ -n $ip ]] && curl -s -o /dev/null -m 3 "http://$ip:$port/playlist.m3u"; then pass "reachable on the network at http://$ip:$port"
  else warn "not reachable on the network address; check the macOS firewall allows Python"; fi
  echo
  echo "Apple TV playlist:  http://${ip:-<mac-ip>}:$port/playlist.m3u"
  echo "Survives IP change: http://$(scutil --get LocalHostName).local:$port/playlist.m3u"
  echo "Guide:              http://${ip:-<mac-ip>}:$port/epg.xml"
  echo "Stream page:        http://localhost:$port/stream   (open in Chrome on this Mac)"
else
  fail "server not running: scripts/start.sh"
fi
exit $bad
