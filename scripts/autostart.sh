#!/bin/bash
# scripts/autostart.sh on   start MyTV at login and keep it running
# scripts/autostart.sh off  undo
cd "$(dirname "$0")/.." || exit 1
root=$(pwd); label=com.mytv.server; plist="$HOME/Library/LaunchAgents/$label.plist"; gui="gui/$(id -u)"
case "${1:-}" in
on)
  [[ -x .venv/bin/python ]] || { echo "Run scripts/setup.sh first."; exit 1; }
  case "$root" in /Volumes/*)
    echo "macOS does not let background services read external drives."
    echo "Either copy this folder to the internal disk (for example ~/MyTV), run scripts/setup.sh there and try again,"
    echo "or add 'Start MyTV.command' under System Settings > General > Login Items."
    exit 1 ;;
  esac
  launchctl print "$gui/$label" >/dev/null 2>&1 || scripts/stop.sh >/dev/null
  mkdir -p "$HOME/Library/LaunchAgents"
  cat > "$plist" <<P
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key><array><string>/bin/bash</string><string>-c</string>
    <string>for i in {1..120}; do [ -x "\$0" ] &amp;&amp; exec "\$0"; sleep 5; done; exit 1</string><string>$root/scripts/run.sh</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>15</integer>
  <key>StandardErrorPath</key><string>/tmp/$label.log</string>
</dict></plist>
P
  launchctl bootout "$gui/$label" 2>/dev/null; sleep 1
  launchctl bootstrap "$gui" "$plist" && echo "MyTV now starts at login." ;;
off)
  launchctl bootout "$gui/$label" 2>/dev/null; rm -f "$plist"; echo "Autostart removed." ;;
*) echo "usage: scripts/autostart.sh on|off"; exit 1 ;;
esac
