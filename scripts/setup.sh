#!/bin/bash
# One-time setup. Safe to run again, and required once after moving to another Mac.
set -euo pipefail
cd "$(dirname "$0")/.."
say() { printf '\n\033[1m%s\033[0m\n' "$*"; }

[[ "$(uname)" == Darwin ]] || { echo "MyTV needs macOS."; exit 1; }
for p in /opt/homebrew/bin /usr/local/bin; do [[ -d $p ]] && PATH="$PATH:$p"; done
export PATH

if ! command -v brew >/dev/null && { ! command -v ffmpeg >/dev/null || ! command -v node >/dev/null; }; then
  echo "Homebrew is required to install ffmpeg and node. Install it from https://brew.sh and run this again."; exit 1
fi
command -v ffmpeg >/dev/null || { say "Installing ffmpeg"; brew install ffmpeg; }
# the dashboard build needs Node 22.12 or newer; an already built dashboard needs no Node at all
good_node() { "$1" -e 'const [a,b]=process.versions.node.split(".").map(Number);process.exit(a>22||a==22&&b>=12?0:1)' 2>/dev/null; }
node_dir=""
for n in $(which -a node 2>/dev/null) /opt/homebrew/bin/node /usr/local/bin/node "$HOME"/.nvm/versions/node/*/bin/node; do
  [[ -x $n ]] && good_node "$n" && { node_dir=$(dirname "$n"); break; }
done
if [[ -z $node_dir && ! -f frontend/dist/index.html ]]; then
  say "Installing node"; brew install node || brew upgrade node; node_dir="$(brew --prefix)/bin"
fi
command -v uv >/dev/null || { say "Installing uv (Python manager)"; brew install uv; }
# streaming tabs to the live channels needs Chrome or Edge on this Mac
if [[ ! -d "/Applications/Google Chrome.app" && ! -d "/Applications/Microsoft Edge.app" ]] && command -v brew >/dev/null; then
  say "Installing Google Chrome (used to stream tabs to the live channels)"; brew install --cask google-chrome || echo "Chrome could not be installed; get it from google.com/chrome"
fi

say "Creating the Python environment"
# an environment copied from another Mac points at paths that do not exist here
if [[ -d .venv ]] && ! .venv/bin/python -c 'import sys' >/dev/null 2>&1; then rm -rf .venv; fi
[[ -d .venv ]] || uv venv --python 3.13 .venv
uv pip install --quiet --python .venv/bin/python -e .

if [[ -n $node_dir ]]; then
  say "Building the dashboard"
  (cd frontend && PATH="$node_dir:$PATH" && npm install --silent --no-audit --no-fund && npm run build --silent >/dev/null)
fi

[[ -f .env ]] || cp .env.example .env
say "Done. Start MyTV with: scripts/start.sh   (then check it with scripts/doctor.sh)"
